//! Pulling the scalars out of the payloads this node decodes.
//!
//! Each function walks one protobuf message and reports what it found in a
//! fixed struct. What the values *mean* -- degrees, volts, a role's name, the
//! text of a line -- stays in Python, because all of that is formatting and
//! formatting allocates.
//!
//! Three conventions make that split work:
//!
//! Presence is a bitmask, not a sentinel. Zero is a legal latitude, altitude,
//! role and battery level, so "absent" cannot be encoded as a value. The mask
//! is what lets Python keep printing `--` for a field that was never sent.
//!
//! Floats cross as their raw bits. Nothing here interprets them, and keeping
//! the boundary integer-only means no float ever passes through a natmod
//! binding.
//!
//! Strings cross as an offset and a length into the caller's buffer. Slicing
//! and decoding are the caller's, on the same principle as `cache`.

use crate::proto::{field, WIRE_I32, WIRE_LEN, WIRE_VARINT};
use crate::{nmea, pbuf, ERR_NO_FIX, ERR_NULL};

pub const HAS_1: u32 = 1 << 0;
pub const HAS_2: u32 = 1 << 1;
pub const HAS_3: u32 = 1 << 2;
pub const HAS_4: u32 = 1 << 3;
pub const HAS_5: u32 = 1 << 4;
pub const HAS_6: u32 = 1 << 5;

/// Routing's oneof, which arm was taken.
pub const ROUTE_NONE: u32 = 0;
pub const ROUTE_ERROR: u32 = 1;
pub const ROUTE_REQUEST: u32 = 2;
pub const ROUTE_REPLY: u32 = 3;

#[repr(C)]
pub struct PositionOut {
    pub have: u32,
    pub lat: i32,
    pub lon: i32,
    pub alt: i32,
    pub sats: u32,
    pub precision: u32,
}

#[repr(C)]
pub struct UserOut {
    pub have: u32,
    pub long_off: u32,
    pub long_len: u32,
    pub short_off: u32,
    pub short_len: u32,
    pub hw: u32,
    pub role: u32,
    pub licensed: u32,
}

#[repr(C)]
pub struct DeviceOut {
    pub have: u32,
    pub batt: u32,
    pub volt: u32,
    pub chan: u32,
    pub tx: u32,
    pub up: u32,
}

#[repr(C)]
pub struct EnvOut {
    pub have: u32,
    pub temp: u32,
    pub rh: u32,
    pub hpa: u32,
}

/// Which arm of a oneof envelope was on the wire, and where its body is.
#[repr(C)]
pub struct VariantOut {
    pub number: u32,
    pub off: u32,
    pub len: u32,
}

#[repr(C)]
pub struct RoutingOut {
    pub kind: u32,
    pub value: u32,
}

/// Where a RouteDiscovery's four packed arrays are, as offsets into the body.
#[repr(C)]
pub struct RouteOut {
    pub have: u32,
    pub route_off: u32,
    pub route_len: u32,
    pub snr_out_off: u32,
    pub snr_out_len: u32,
    pub back_off: u32,
    pub back_len: u32,
    pub snr_back_off: u32,
    pub snr_back_len: u32,
}

/// A varint that carried an int32 arrives sign-extended to 64 bits, so the
/// wanted value is the low word whatever precedes it. Masking is what makes a
/// negative altitude decode instead of overflowing.
fn as_i32(value: u64) -> i32 {
    value as u32 as i32
}

pub fn position(body: &[u8]) -> Result<PositionOut, i32> {
    let mut have = 0;
    let mut lat = 0;
    let mut lon = 0;
    let mut alt = 0;
    let mut sats = 0;
    let mut precision = 0;
    let mut at = 0;
    while at < body.len() {
        let f = field(body, at)?;
        at = f.next;
        match f.number {
            1 => {
                lat = as_i32(f.value);
                have |= HAS_1;
            }
            2 => {
                lon = as_i32(f.value);
                have |= HAS_2;
            }
            3 => {
                alt = as_i32(f.value);
                have |= HAS_3;
            }
            19 => {
                sats = f.value as u32;
                have |= HAS_4;
            }
            23 => {
                precision = f.value as u32;
                have |= HAS_5;
            }
            _ => {}
        }
    }
    Ok(PositionOut { have, lat, lon, alt, sats, precision })
}

/// `Position.LocSource.LOC_INTERNAL`: this node's own receiver said so.
pub const LOC_INTERNAL: u64 = 2;

/// Every bit of both coordinates. Fewer is a node stating a coarser square
/// than it actually knows.
pub const PRECISION_FULL: u32 = 32;

/// Clears the bits a node will not state, then centres the result in what is
/// left.
///
/// Truncation alone would move every position to the south-west corner of its
/// square, and a receiver averaging those would pull a whole mesh steadily one
/// way. Adding back half a square is what the firmware does, so an imprecise
/// position from here sits where an imprecise position from anything else
/// would.
fn blur(lat: i32, lon: i32, precision: u32) -> (i32, i32) {
    if precision >= PRECISION_FULL {
        return (lat, lon);
    }
    let mask = (u32::MAX << (PRECISION_FULL - precision)) as i32;
    let half = 1i32 << (31 - precision);
    (
        (lat & mask).wrapping_add(half),
        (lon & mask).wrapping_add(half),
    )
}

/// Writes a `Position` for what the receiver has actually established.
///
/// Refuses a fix the receiver has not confirmed, rather than sending zeroes: a
/// node claiming to be at the origin is worse than a node saying nothing, and
/// it is the one mistake that a position broadcast can make permanently, since
/// every node that hears it stores it.
///
/// `precision` is a privacy setting and not a measurement, which is why it is
/// the caller's and why it is stated on the wire: a receiver has to be able to
/// tell a coarse position from a precise one that happens to land on a round
/// number. Zero withholds the coordinates while still reporting the fix.
/// Takes the raw parser state rather than a [`nmea::Fix`], because a `Fix` is
/// thirty-six bytes and so crosses a call boundary by pointer either way: the
/// caller reads one, the callee copies it, and the copy is an `__aeabi_memcpy`
/// the .mpy linker has no runtime to resolve. Reading it here leaves one
/// struct, written once, read in place.
pub fn encode_position(out: &mut [u8], state: &[u8], precision: u32) -> Result<usize, i32> {
    let fix = nmea::read(state)?;
    if fix.valid == 0 {
        return Err(ERR_NO_FIX);
    }
    let mut at = 0usize;
    if fix.have & nmea::HAVE_POSITION != 0 && precision > 0 {
        let (lat, lon) = blur(fix.lat_e7, fix.lon_e7, precision);
        at = pbuf::fixed32(out, at, 1, lat as u32)?;
        at = pbuf::fixed32(out, at, 2, lon as u32)?;
    }
    if fix.have & nmea::HAVE_ALTITUDE != 0 {
        // The schema is metres above MSL; the receiver reports millimetres.
        at = pbuf::int32(out, at, 3, fix.alt_mm / 1000)?;
    }
    if fix.have & nmea::HAVE_TIME != 0 && fix.when != 0 {
        at = pbuf::fixed32(out, at, 4, fix.when)?;
    }
    at = pbuf::uint(out, at, 5, LOC_INTERNAL)?;
    if fix.hdop_cm != nmea::NO_HDOP {
        // Both are hundredths, so this is the one dilution field that needs no
        // conversion. PDOP and VDOP have no source here and are left out.
        at = pbuf::uint(out, at, 12, fix.hdop_cm as u64)?;
    }
    if fix.quality != 0 {
        at = pbuf::uint(out, at, 17, fix.quality as u64)?;
    }
    // Field 18 `fix_type` wants GSA's 2D/3D answer. RMC's `nav` is a status
    // letter, not that, so nothing here can fill it honestly.
    let seen = if fix.in_view != nmea::NO_SATS {
        fix.in_view
    } else {
        fix.sats
    };
    if seen != nmea::NO_SATS {
        at = pbuf::uint(out, at, 19, seen as u64)?;
    }
    at = pbuf::uint(out, at, 23, precision as u64)?;
    Ok(at)
}

pub fn user(body: &[u8]) -> Result<UserOut, i32> {
    let mut have = 0;
    let mut long_off = 0;
    let mut long_len = 0;
    let mut short_off = 0;
    let mut short_len = 0;
    let mut hw = 0;
    let mut role = 0;
    let mut licensed = 0;
    let mut at = 0;
    while at < body.len() {
        let f = field(body, at)?;
        at = f.next;
        match f.number {
            2 if f.wire == WIRE_LEN => {
                long_off = f.data_off as u32;
                long_len = f.data_len as u32;
                have |= HAS_1;
            }
            3 if f.wire == WIRE_LEN => {
                short_off = f.data_off as u32;
                short_len = f.data_len as u32;
                have |= HAS_2;
            }
            // Wire type checked as well as number: a length-delimited field
            // reports its LENGTH as `value`, so an hw_model sent as bytes
            // would otherwise be read as however long the blob was.
            5 if f.wire == WIRE_VARINT => {
                hw = f.value as u32;
                have |= HAS_3;
            }
            6 if f.wire == WIRE_VARINT => {
                licensed = if f.value != 0 { 1 } else { 0 };
                have |= HAS_4;
            }
            7 if f.wire == WIRE_VARINT => {
                role = f.value as u32;
                have |= HAS_5;
            }
            _ => {}
        }
    }
    Ok(UserOut {
        have,
        long_off,
        long_len,
        short_off,
        short_len,
        hw,
        role,
        licensed,
    })
}

/// The short name alone, which is all the name table keeps.
pub fn short_name(body: &[u8]) -> Result<VariantOut, i32> {
    let mut at = 0;
    while at < body.len() {
        let f = field(body, at)?;
        at = f.next;
        if f.number == 3 && f.wire == WIRE_LEN {
            return Ok(VariantOut {
                number: 3,
                off: f.data_off as u32,
                len: f.data_len as u32,
            });
        }
    }
    Ok(VariantOut { number: 0, off: 0, len: 0 })
}

pub fn device_metrics(body: &[u8]) -> Result<DeviceOut, i32> {
    let mut have = 0;
    let mut batt = 0;
    let mut volt = 0;
    let mut chan = 0;
    let mut tx = 0;
    let mut up = 0;
    let mut at = 0;
    while at < body.len() {
        let f = field(body, at)?;
        at = f.next;
        match f.number {
            1 => {
                batt = f.value as u32;
                have |= HAS_1;
            }
            2 => {
                volt = f.value as u32;
                have |= HAS_2;
            }
            3 => {
                chan = f.value as u32;
                have |= HAS_3;
            }
            4 => {
                tx = f.value as u32;
                have |= HAS_4;
            }
            5 => {
                up = f.value as u32;
                have |= HAS_5;
            }
            _ => {}
        }
    }
    Ok(DeviceOut { have, batt, volt, chan, tx, up })
}

pub fn environment_metrics(body: &[u8]) -> Result<EnvOut, i32> {
    let mut have = 0;
    let mut temp = 0;
    let mut rh = 0;
    let mut hpa = 0;
    let mut at = 0;
    while at < body.len() {
        let f = field(body, at)?;
        at = f.next;
        match f.number {
            1 => {
                temp = f.value as u32;
                have |= HAS_1;
            }
            2 => {
                rh = f.value as u32;
                have |= HAS_2;
            }
            3 => {
                hpa = f.value as u32;
                have |= HAS_3;
            }
            _ => {}
        }
    }
    Ok(EnvOut { have, temp, rh, hpa })
}

/// The first field past 1 in a Telemetry, which is the variant it carries.
/// Field 1 is the timestamp and is not one of the arms.
pub fn telemetry(body: &[u8]) -> Result<VariantOut, i32> {
    let mut at = 0;
    while at < body.len() {
        let f = field(body, at)?;
        at = f.next;
        if f.number > 1 {
            return Ok(VariantOut {
                number: f.number,
                off: f.data_off as u32,
                len: f.data_len as u32,
            });
        }
    }
    Ok(VariantOut { number: 0, off: 0, len: 0 })
}

/// Routing's three arms are a oneof, so the selected one is always on the wire
/// even when its value is zero. That is why a plain ack is two bytes rather
/// than an empty payload, and why finding nothing here means something is
/// wrong rather than that the packet was uninteresting.
pub fn routing(body: &[u8]) -> Result<RoutingOut, i32> {
    let mut at = 0;
    while at < body.len() {
        let f = field(body, at)?;
        at = f.next;
        if f.number == 3 {
            return Ok(RoutingOut { kind: ROUTE_ERROR, value: f.value as u32 });
        }
        if f.number == 1 || f.number == 2 {
            let kind = if f.number == 1 { ROUTE_REQUEST } else { ROUTE_REPLY };
            return Ok(RoutingOut { kind, value: 0 });
        }
    }
    Ok(RoutingOut { kind: ROUTE_NONE, value: 0 })
}

/// The sender's own timestamp, as a fixed32 in the field the caller names.
/// Whether the value is a believable date is the caller's judgement.
pub fn sender_time(body: &[u8], want: u32) -> Result<Option<u32>, i32> {
    let mut at = 0;
    while at < body.len() {
        let f = field(body, at)?;
        at = f.next;
        if f.number == want && f.wire == WIRE_I32 {
            return Ok(Some(f.value as u32));
        }
    }
    Ok(None)
}

/// A RouteDiscovery's four packed arrays, located but not read.
///
/// nanopb packs repeated scalars and is the only encoder that ever fills these
/// in, so an unpacked element would be a wrong guess rather than a tolerated
/// one: anything not length-delimited is passed over. The elements themselves
/// are left to `snr_at` and to the caller, because the two lists are different
/// widths -- fixed32 hops, varint SNRs -- and neither is worth a copy.
pub fn traceroute(body: &[u8]) -> Result<RouteOut, i32> {
    let mut out = RouteOut {
        have: 0,
        route_off: 0,
        route_len: 0,
        snr_out_off: 0,
        snr_out_len: 0,
        back_off: 0,
        back_len: 0,
        snr_back_off: 0,
        snr_back_len: 0,
    };
    let mut at = 0;
    while at < body.len() {
        let f = field(body, at)?;
        at = f.next;
        if f.wire != WIRE_LEN {
            continue;
        }
        let (off, len) = (f.data_off as u32, f.data_len as u32);
        match f.number {
            1 => {
                out.route_off = off;
                out.route_len = len;
                out.have |= HAS_1;
            }
            2 => {
                out.snr_out_off = off;
                out.snr_out_len = len;
                out.have |= HAS_2;
            }
            3 => {
                out.back_off = off;
                out.back_len = len;
                out.have |= HAS_3;
            }
            4 => {
                out.snr_back_off = off;
                out.snr_back_len = len;
                out.have |= HAS_4;
            }
            _ => {}
        }
    }
    Ok(out)
}

/// One packed int32 of a traceroute's SNR list, as the sender's int8.
///
/// The list is a run of varints rather than fixed-width, so it can only be read
/// forwards, one call per element. Negatives went on the wire sign-extended to
/// 64 bits, so the int8 survives as the low byte however many 0xff precede it.
pub fn snr_at(chunk: &[u8], at: usize) -> Result<(i32, usize), i32> {
    let f = field_value(chunk, at)?;
    Ok(((f.0 & 0xFF) as u8 as i8 as i32, f.1))
}

/// A bare varint, with no tag in front of it.
fn field_value(buf: &[u8], at: usize) -> Result<(u64, usize), i32> {
    // The packed encoding has no tags, so `field` cannot read it. Rebuilding a
    // varint reader here is still better than one in Python, which is where
    // this used to live.
    let mut value: u64 = 0;
    let mut shift: u32 = 0;
    let mut at = at;
    loop {
        if at >= buf.len() {
            return Err(crate::ERR_TRUNCATED);
        }
        if shift >= 64 {
            return Err(crate::ERR_BAD_WIRE);
        }
        let byte = buf[at];
        at += 1;
        value |= ((byte & 0x7F) as u64) << shift;
        if byte & 0x80 == 0 {
            return Ok((value, at));
        }
        shift += 7;
    }
}

// ---------------------------------------------------------------------- C ABI

unsafe fn borrow<'a>(ptr: *const u8, len: usize) -> Option<&'a [u8]> {
    if ptr.is_null() {
        None
    } else {
        Some(core::slice::from_raw_parts(ptr, len))
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_position(
    body: *const u8,
    len: usize,
    out: *mut PositionOut,
) -> i32 {
    let body = match borrow(body, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match position(body) {
        Ok(v) => {
            *out = v;
            0
        }
        Err(rc) => rc,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_user(
    body: *const u8,
    len: usize,
    out: *mut UserOut,
) -> i32 {
    let body = match borrow(body, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match user(body) {
        Ok(v) => {
            // Field by field. Eight words assigned at once becomes a call to
            // __aeabi_memcpy, which a natmod has no way to resolve.
            let out = &mut *out;
            out.have = v.have;
            out.long_off = v.long_off;
            out.long_len = v.long_len;
            out.short_off = v.short_off;
            out.short_len = v.short_len;
            out.hw = v.hw;
            out.role = v.role;
            out.licensed = v.licensed;
            0
        }
        Err(rc) => rc,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_short_name(
    body: *const u8,
    len: usize,
    out: *mut VariantOut,
) -> i32 {
    let body = match borrow(body, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match short_name(body) {
        Ok(v) => {
            *out = v;
            0
        }
        Err(rc) => rc,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_device_metrics(
    body: *const u8,
    len: usize,
    out: *mut DeviceOut,
) -> i32 {
    let body = match borrow(body, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match device_metrics(body) {
        Ok(v) => {
            *out = v;
            0
        }
        Err(rc) => rc,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_environment_metrics(
    body: *const u8,
    len: usize,
    out: *mut EnvOut,
) -> i32 {
    let body = match borrow(body, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match environment_metrics(body) {
        Ok(v) => {
            *out = v;
            0
        }
        Err(rc) => rc,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_telemetry(
    body: *const u8,
    len: usize,
    out: *mut VariantOut,
) -> i32 {
    let body = match borrow(body, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match telemetry(body) {
        Ok(v) => {
            *out = v;
            0
        }
        Err(rc) => rc,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_routing(
    body: *const u8,
    len: usize,
    out: *mut RoutingOut,
) -> i32 {
    let body = match borrow(body, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match routing(body) {
        Ok(v) => {
            *out = v;
            0
        }
        Err(rc) => rc,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_traceroute(
    body: *const u8,
    len: usize,
    out: *mut RouteOut,
) -> i32 {
    let body = match borrow(body, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match traceroute(body) {
        Ok(v) => {
            // Field by field, as in `user` above: nine words assigned at once
            // becomes a call to __aeabi_memcpy, which a natmod cannot resolve.
            let out = &mut *out;
            out.have = v.have;
            out.route_off = v.route_off;
            out.route_len = v.route_len;
            out.snr_out_off = v.snr_out_off;
            out.snr_out_len = v.snr_out_len;
            out.back_off = v.back_off;
            out.back_len = v.back_len;
            out.snr_back_off = v.snr_back_off;
            out.snr_back_len = v.snr_back_len;
            0
        }
        Err(rc) => rc,
    }
}

/// Returns the value, or -1 when the field is not there. A fixed32 that really
/// is 0xffffffff cannot be told from absence, and is not a date.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_sender_time(
    body: *const u8,
    len: usize,
    want: u32,
    out: *mut u32,
) -> i32 {
    let body = match borrow(body, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match sender_time(body, want) {
        Ok(Some(value)) => {
            *out = value;
            0
        }
        Ok(None) => 1,
        Err(rc) => rc,
    }
}

/// Writes the SNR into `out` and returns the next offset.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_payload_snr_at(
    chunk: *const u8,
    len: usize,
    at: usize,
    out: *mut i32,
) -> i32 {
    let chunk = match borrow(chunk, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    if out.is_null() {
        return ERR_NULL;
    }
    match snr_at(chunk, at) {
        Ok((snr, next)) => {
            *out = snr;
            next as i32
        }
        Err(rc) => rc,
    }
}
