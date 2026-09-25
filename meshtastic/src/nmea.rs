//! NMEA 0183 framing and parsing, and the running fix built out of it.
//!
//! The receiver talks in decimal text -- `ddmm.mmmm` degrees, `hhmmss.sss`
//! clocks, a dilution figure with two decimals -- and every one of those is
//! held here as a scaled integer. Latitude and longitude are 1e-7 degrees,
//! which is what Meshtastic's `Position` puts on the wire, so the value this
//! produces is already the value that gets broadcast and nothing in between
//! has to be floating point.
//!
//! The fix lives in a buffer the caller owns. Nothing here allocates, and a
//! second reader over the same bytes sees the same fix.

use crate::le;
use crate::{ERR_BAD_SENTENCE, ERR_BAD_STATE};

pub const STATE_BYTES: usize = 144;

/// How many constellations the talker table holds. Each sends its own GSV run
/// reporting only its own satellites, so the counts have to be kept apart and
/// summed; six are defined today and the rest is slack.
pub const TALKERS: usize = 8;

const OFF_LAT: usize = 0;
const OFF_LON: usize = 4;
const OFF_ALT: usize = 8;
const OFF_WHEN: usize = 12;
const OFF_GOOD: usize = 16;
const OFF_BAD: usize = 20;
const OFF_HDOP: usize = 24;
const OFF_SATS: usize = 26;
const OFF_VIEW: usize = 27;
const OFF_QUALITY: usize = 28;
const OFF_STATUS: usize = 29;
const OFF_MODE: usize = 30;
const OFF_NAV: usize = 31;
const OFF_HAVE: usize = 32;
const OFF_PENDING_LEN: usize = 33;
const OFF_TALKERS: usize = 36;
const OFF_PENDING: usize = 60;
const TALKER_END: usize = OFF_TALKERS + TALKERS * 3;

/// A sentence is 82 characters at most, delimiters included.
const PENDING_MAX: usize = STATE_BYTES - OFF_PENDING;

/// Absence is not zero for a position: a node at the equator sends a real one.
pub const HAVE_POSITION: u8 = 1 << 0;
pub const HAVE_ALTITUDE: u8 = 1 << 1;
pub const HAVE_TIME: u8 = 1 << 2;

pub const NO_SATS: u8 = 0xFF;
pub const NO_HDOP: u16 = 0xFFFF;

/// 2025-01-01. A receiver that has decoded nothing still stamps its sentences,
/// from a counter that starts at its own epoch, and those are the readings this
/// rejects. Anything past this floor was read off a satellite.
pub const TIME_FLOOR: u32 = 1_735_689_600;

/// What a sentence carried, so a caller can tell a clock apart from a position
/// without re-reading the whole fix.
pub const SAW_GGA: i32 = 1;
pub const SAW_RMC: i32 = 2;
pub const SAW_GSV: i32 = 4;

/// Everything the receiver has said, read back in one go.
pub struct Fix {
    pub lat_e7: i32,
    pub lon_e7: i32,
    /// Millimetres above the geoid, as the receiver reports it.
    pub alt_mm: i32,
    /// Seconds since 1970 UTC, or 0 when no real clock has arrived.
    pub when: u32,
    pub good: u32,
    pub bad: u32,
    /// Horizontal dilution of precision, hundredths. 25.50 is the receiver's
    /// no-fix placeholder rather than a measurement.
    pub hdop_cm: u16,
    pub sats: u8,
    pub in_view: u8,
    pub quality: u8,
    /// RMC's own verdict and its two letters, as ASCII, 0 when unsent.
    pub status: u8,
    pub mode: u8,
    pub nav: u8,
    pub have: u8,
    pub valid: u8,
}

/// A civil date and time, for showing a UTC stamp taken before the clock was.
pub struct Civil {
    pub year: u16,
    pub month: u8,
    pub day: u8,
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
}

fn sized(state: &[u8]) -> bool {
    state.len() >= STATE_BYTES
}

pub fn reset(state: &mut [u8]) -> i32 {
    if !sized(state) {
        return ERR_BAD_STATE;
    }
    for i in 0..STATE_BYTES {
        state[i] = 0;
    }
    state[OFF_SATS] = NO_SATS;
    state[OFF_VIEW] = NO_SATS;
    le::put_u16(state, OFF_HDOP, NO_HDOP);
    0
}

pub fn read(state: &[u8]) -> Result<Fix, i32> {
    if !sized(state) {
        return Err(ERR_BAD_STATE);
    }
    let have = state[OFF_HAVE];
    let quality = state[OFF_QUALITY];
    let status = state[OFF_STATUS];
    // The receiver has to stand behind it twice: a quality it names, and an
    // RMC that agrees. Quality 6 is dead reckoning, which fails both.
    let valid = (have & HAVE_POSITION) != 0 && quality >= 1 && quality <= 5 && status == b'A';
    Ok(Fix {
        lat_e7: le::u32_at(state, OFF_LAT) as i32,
        lon_e7: le::u32_at(state, OFF_LON) as i32,
        alt_mm: le::u32_at(state, OFF_ALT) as i32,
        when: le::u32_at(state, OFF_WHEN),
        good: le::u32_at(state, OFF_GOOD),
        bad: le::u32_at(state, OFF_BAD),
        hdop_cm: le::u16_at(state, OFF_HDOP),
        sats: state[OFF_SATS],
        in_view: state[OFF_VIEW],
        quality,
        status,
        mode: state[OFF_MODE],
        nav: state[OFF_NAV],
        have,
        valid: valid as u8,
    })
}

/// One entry of the talker table: two ASCII characters and a count, or None
/// past the last constellation heard from.
pub fn talker(state: &[u8], index: usize) -> Result<Option<(u8, u8, u8)>, i32> {
    if !sized(state) || index >= TALKERS {
        return Err(ERR_BAD_STATE);
    }
    let at = OFF_TALKERS + index * 3;
    if state[at] == 0 {
        return Ok(None);
    }
    Ok(Some((state[at], state[at + 1], state[at + 2])))
}

/// The field at `want`, as an offset and a length into `line`.
fn field(line: &[u8], want: usize) -> Option<(usize, usize)> {
    let mut n = 0;
    let mut start = 0;
    let mut i = 0;
    while i <= line.len() {
        if i == line.len() || line[i] == b',' {
            if n == want {
                return Some((start, i - start));
            }
            n += 1;
            start = i + 1;
        }
        i += 1;
    }
    None
}

fn digit(c: u8) -> Option<u32> {
    if c < b'0' || c > b'9' {
        return None;
    }
    Some((c - b'0') as u32)
}

fn digits(line: &[u8], at: usize, len: usize) -> Option<u32> {
    if len == 0 || at + len > line.len() {
        return None;
    }
    let mut value = 0u32;
    for i in 0..len {
        value = value.checked_mul(10)?.checked_add(digit(line[at + i])?)?;
    }
    Some(value)
}

/// A decimal field as an integer scaled by ten to the `places`. Digits finer
/// than that are dropped rather than rounded, which is under a millimetre at
/// every scale used here.
fn scaled(line: &[u8], at: usize, len: usize, places: u32) -> Option<i32> {
    if len == 0 || at + len > line.len() {
        return None;
    }
    let mut i = 0;
    let mut negative = false;
    if line[at] == b'-' {
        negative = true;
        i = 1;
    } else if line[at] == b'+' {
        i = 1;
    }
    let mut value = 0u32;
    let mut seen = false;
    while i < len && line[at + i] != b'.' {
        value = value.checked_mul(10)?.checked_add(digit(line[at + i])?)?;
        seen = true;
        i += 1;
    }
    if !seen {
        return None;
    }
    let mut unit = 1u32;
    for _ in 0..places {
        unit *= 10;
    }
    value = value.checked_mul(unit)?;
    if i < len {
        i += 1;
        while i < len {
            let d = digit(line[at + i])?;
            if unit > 1 {
                unit /= 10;
                value = value.checked_add(d * unit)?;
            }
            i += 1;
        }
    }
    if value > i32::MAX as u32 {
        return None;
    }
    let signed = value as i32;
    Some(if negative { -signed } else { signed })
}

/// NMEA `ddmm.mmmm` and a hemisphere letter, as 1e-7 degrees.
fn degrees_e7(line: &[u8], at: usize, len: usize, hemisphere: u8) -> Option<i32> {
    if at + len > line.len() {
        return None;
    }
    // The minutes are always the two digits before the point; whatever comes
    // before those is the degrees, two characters for a latitude and three for
    // a longitude.
    let mut dot = len;
    for i in 0..len {
        if line[at + i] == b'.' {
            dot = i;
            break;
        }
    }
    if dot < 3 {
        return None;
    }
    let whole = digits(line, at, dot - 2)?;
    let minutes_e5 = scaled(line, at + dot - 2, len - (dot - 2), 5)? as u32;
    if whole > 180 || minutes_e5 >= 6_000_000 {
        return None;
    }
    let total = (whole * 10_000_000 + (minutes_e5 * 100 + 30) / 60) as i32;
    if hemisphere == b'S' || hemisphere == b'W' {
        return Some(-total);
    }
    Some(total)
}

/// Days from 1970-01-01, Hinnant's: a March-based year puts the leap day last,
/// so the month lengths repeat and no table is needed.
fn days_from_civil(year: u32, month: u32, day: u32) -> u32 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y / 400;
    let yoe = y - era * 400;
    let mp = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// `ddmmyy` and `hhmmss.s` as seconds since 1970, UTC.
///
/// Not the C library's: that works off the clock this is trying to set, and on
/// a board whose clock has never been set the answer would be built on 1970.
fn epoch(line: &[u8], date: usize, date_len: usize, clock: usize, clock_len: usize) -> Option<u32> {
    if date_len < 6 || clock_len < 6 {
        return None;
    }
    let day = digits(line, date, 2)?;
    let month = digits(line, date + 2, 2)?;
    let year = 2000 + digits(line, date + 4, 2)?;
    let hour = digits(line, clock, 2)?;
    let minute = digits(line, clock + 2, 2)?;
    let second = digits(line, clock + 4, 2)?;
    if month < 1 || month > 12 || day < 1 || day > 31 {
        return None;
    }
    if hour > 23 || minute > 59 || second > 60 {
        return None;
    }
    Some(days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second)
}

/// The calendar date an epoch falls on, so a stamp can be shown before the
/// board's own clock has been set from it.
pub fn civil(when: u32) -> Civil {
    let seconds = when % 86400;
    let z = when / 86400 + 719468;
    let era = z / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    Civil {
        year: (if month <= 2 { y + 1 } else { y }) as u16,
        month: month as u8,
        day: day as u8,
        hour: (seconds / 3600) as u8,
        minute: (seconds / 60 % 60) as u8,
        second: (seconds % 60) as u8,
    }
}

fn hex(c: u8) -> Option<u8> {
    if c >= b'0' && c <= b'9' {
        return Some(c - b'0');
    }
    if c >= b'A' && c <= b'F' {
        return Some(c - b'A' + 10);
    }
    if c >= b'a' && c <= b'f' {
        return Some(c - b'a' + 10);
    }
    None
}

/// XOR of everything between the `$` and the `*`, against the two hex digits
/// that follow. Returns the length of the body, checksum excluded.
fn checked(line: &[u8]) -> Option<usize> {
    if line.len() < 4 || line[0] != b'$' {
        return None;
    }
    let mut star = 0;
    let mut i = line.len();
    while i > 0 {
        i -= 1;
        if line[i] == b'*' {
            star = i;
            break;
        }
    }
    if star == 0 || star + 3 > line.len() {
        return None;
    }
    let mut total = 0u8;
    for i in 1..star {
        total ^= line[i];
    }
    if total != hex(line[star + 1])? * 16 + hex(line[star + 2])? {
        return None;
    }
    Some(star)
}

fn first(line: &[u8], at: usize, len: usize) -> u8 {
    if len == 0 {
        return 0;
    }
    line[at]
}

/// Records one constellation's count and re-totals the sky.
fn note_talker(state: &mut [u8], a: u8, b: u8, count: u8) {
    let mut slot = TALKER_END;
    for i in 0..TALKERS {
        let at = OFF_TALKERS + i * 3;
        if state[at] == a && state[at + 1] == b {
            slot = at;
            break;
        }
        if state[at] == 0 && slot == TALKER_END {
            slot = at;
        }
    }
    if slot == TALKER_END {
        // Table full, and this is a constellation it has never heard from. Its
        // satellites go uncounted rather than displacing ones that are counted.
        return;
    }
    state[slot] = a;
    state[slot + 1] = b;
    state[slot + 2] = count;
    let mut total = 0u32;
    for i in 0..TALKERS {
        let at = OFF_TALKERS + i * 3;
        if state[at] != 0 {
            total += state[at + 2] as u32;
        }
    }
    state[OFF_VIEW] = if total > 254 { 254 } else { total as u8 };
}

/// Takes one whole sentence, checksum included. Returns which sentence it was,
/// 0 for one this does not act on, or an error for one that failed its check.
pub fn sentence(state: &mut [u8], line: &[u8]) -> i32 {
    if !sized(state) {
        return ERR_BAD_STATE;
    }
    parse(state, line)
}

/// The same, on just the fields. Everything written here lives below
/// `OFF_PENDING`, which is what lets a line still sitting in the state buffer
/// be parsed without being copied out of it first.
fn parse(fields: &mut [u8], line: &[u8]) -> i32 {
    let body = match checked(line) {
        Some(n) => n,
        None => return ERR_BAD_SENTENCE,
    };
    let line = &line[..body];
    if line.len() < 6 {
        return 0;
    }
    if line[3] == b'G' && line[4] == b'G' && line[5] == b'A' {
        return gga(fields, line);
    }
    if line[3] == b'R' && line[4] == b'M' && line[5] == b'C' {
        return rmc(fields, line);
    }
    if line[3] == b'G' && line[4] == b'S' && line[5] == b'V' {
        return gsv(fields, line);
    }
    0
}

fn gga(state: &mut [u8], line: &[u8]) -> i32 {
    // Field 9 is the altitude and the last one read, so a sentence stopping
    // short of it is a truncated GGA and none of it is trusted.
    let (alt_at, alt_len) = match field(line, 9) {
        Some(f) => f,
        None => return 0,
    };
    let (quality_at, quality_len) = field(line, 6).unwrap_or((0, 0));
    let quality = digits(line, quality_at, quality_len).unwrap_or(0);
    state[OFF_QUALITY] = if quality > 255 { 255 } else { quality as u8 };
    let (sats_at, sats_len) = field(line, 7).unwrap_or((0, 0));
    state[OFF_SATS] = match digits(line, sats_at, sats_len) {
        Some(n) if n < NO_SATS as u32 => n as u8,
        _ => NO_SATS,
    };
    let (hdop_at, hdop_len) = field(line, 8).unwrap_or((0, 0));
    let hdop = match scaled(line, hdop_at, hdop_len, 2) {
        Some(n) if n >= 0 && n < NO_HDOP as i32 => n as u16,
        _ => NO_HDOP,
    };
    le::put_u16(state, OFF_HDOP, hdop);
    if quality == 0 {
        return SAW_GGA;
    }
    let (lat_at, lat_len) = field(line, 2).unwrap_or((0, 0));
    let (ns_at, ns_len) = field(line, 3).unwrap_or((0, 0));
    let (lon_at, lon_len) = field(line, 4).unwrap_or((0, 0));
    let (ew_at, ew_len) = field(line, 5).unwrap_or((0, 0));
    let lat = degrees_e7(line, lat_at, lat_len, first(line, ns_at, ns_len));
    let lon = degrees_e7(line, lon_at, lon_len, first(line, ew_at, ew_len));
    if let (Some(lat), Some(lon)) = (lat, lon) {
        le::put_u32(state, OFF_LAT, lat as u32);
        le::put_u32(state, OFF_LON, lon as u32);
        state[OFF_HAVE] |= HAVE_POSITION;
    }
    if let Some(alt) = scaled(line, alt_at, alt_len, 3) {
        le::put_u32(state, OFF_ALT, alt as u32);
        state[OFF_HAVE] |= HAVE_ALTITUDE;
    }
    SAW_GGA
}

fn rmc(state: &mut [u8], line: &[u8]) -> i32 {
    // Field 9 is the date, without which the clock is only a time of day.
    let (date_at, date_len) = match field(line, 9) {
        Some(f) => f,
        None => return 0,
    };
    let (clock_at, clock_len) = field(line, 1).unwrap_or((0, 0));
    let (status_at, status_len) = field(line, 2).unwrap_or((0, 0));
    state[OFF_STATUS] = first(line, status_at, status_len);
    let (mode_at, mode_len) = field(line, 12).unwrap_or((0, 0));
    state[OFF_MODE] = first(line, mode_at, mode_len);
    let (nav_at, nav_len) = field(line, 13).unwrap_or((0, 0));
    state[OFF_NAV] = first(line, nav_at, nav_len);
    // Not gated on the status: that flag is about the position. Time comes off
    // one satellite, a position needs four placed well, and indoors the first
    // arrives while the second never does.
    if let Some(when) = epoch(line, date_at, date_len, clock_at, clock_len) {
        if when >= TIME_FLOOR {
            le::put_u32(state, OFF_WHEN, when);
            state[OFF_HAVE] |= HAVE_TIME;
        }
    }
    SAW_RMC
}

fn gsv(state: &mut [u8], line: &[u8]) -> i32 {
    let (at, len) = match field(line, 3) {
        Some(f) => f,
        None => return 0,
    };
    let count = digits(line, at, len).unwrap_or(0);
    note_talker(state, line[1], line[2], if count > 254 { 254 } else { count as u8 });
    SAW_GSV
}

/// Takes whatever arrived from the UART, however it happens to be cut up.
///
/// Returns how many whole sentences passed their checksum in this chunk, and
/// how much of `echo` is now used. Each accepted sentence is copied there,
/// newline separated, so a caller that wants to show the stream itself does
/// not have to parse it a second time; pass an empty slice to skip that.
pub fn feed(state: &mut [u8], chunk: &[u8], echo: &mut [u8], echo_at: usize) -> (i32, usize) {
    if !sized(state) {
        return (ERR_BAD_STATE, echo_at);
    }
    let mut landed = 0;
    let mut used = if echo_at > echo.len() { echo.len() } else { echo_at };
    for i in 0..chunk.len() {
        let c = chunk[i];
        if c == b'\r' || c == b'\n' {
            let (got, at) = flush(state, echo, used);
            landed += got;
            used = at;
            continue;
        }
        let len = state[OFF_PENDING_LEN] as usize;
        if len >= PENDING_MAX {
            // Longer than any legal sentence, so its start was noise or a lost
            // delimiter. Drop the lot rather than splice two halves together.
            state[OFF_PENDING_LEN] = 0;
            bump(state, OFF_BAD);
            continue;
        }
        state[OFF_PENDING + len] = c;
        state[OFF_PENDING_LEN] = (len + 1) as u8;
    }
    (landed, used)
}

/// Hands the assembled line to the parser and starts the next one.
fn flush(state: &mut [u8], echo: &mut [u8], echo_at: usize) -> (i32, usize) {
    let len = state[OFF_PENDING_LEN] as usize;
    state[OFF_PENDING_LEN] = 0;
    if len == 0 {
        return (0, echo_at);
    }
    // The fields the parser writes and the line it reads are both inside the
    // state, so the borrow is cut in two rather than the line copied onto the
    // stack. A buffer that size costs a `memclr` the natmod cannot link.
    let (fields, rest) = state.split_at_mut(OFF_PENDING);
    let line = &rest[..len];
    if parse(fields, line) < 0 {
        bump(fields, OFF_BAD);
        return (0, echo_at);
    }
    bump(fields, OFF_GOOD);
    let mut at = echo_at;
    if at + len + 1 <= echo.len() {
        for i in 0..len {
            echo[at + i] = line[i];
        }
        echo[at + len] = b'\n';
        at += len + 1;
    }
    (1, at)
}

fn bump(state: &mut [u8], at: usize) {
    le::put_u32(state, at, le::u32_at(state, at).wrapping_add(1));
}
