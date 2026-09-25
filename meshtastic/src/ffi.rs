//! The C ABI the native module's shim calls.
//!
//! Every function returns a status code from [`crate`], with results written
//! through out-pointers. Frequencies need the full u32 range -- 2.4 GHz does not
//! fit in an i32 -- so none of them can be returned in the status.

use crate::header::Header;
use crate::{airtime, duty, hash, nmea, payload, pbuf, power, preset, proto, region, stream};
use crate::{ERR_NULL, OK};

/// A parsed header, with the flag byte already unpacked so the shim does not
/// have to know the bit layout.
#[repr(C)]
pub struct HeaderOut {
    pub to: u32,
    pub from: u32,
    pub id: u32,
    pub flags: u8,
    pub channel: u8,
    pub next_hop: u8,
    pub relay_node: u8,
    pub hop_limit: u8,
    pub hop_start: u8,
    pub want_ack: u8,
    pub via_mqtt: u8,
    /// Hops taken, or 0xFF when `hop_start` was unset and it cannot be known.
    pub hops_away: u8,
}

#[repr(C)]
pub struct PresetOut {
    pub bw_hz: u32,
    pub sf: u8,
    pub cr: u8,
}

#[repr(C)]
pub struct RegionOut {
    pub start_hz: u32,
    pub end_hz: u32,
    pub spacing_hz: u32,
    pub duty_cycle_pct: u16,
    pub power_limit_dbm: u8,
    pub wide_lora: u8,
}

/// A parsed `Data` envelope. The payload is reported as a span rather than
/// copied, so the caller slices its own buffer and nothing is allocated here.
#[repr(C)]
pub struct DataOut {
    pub portnum: u32,
    pub payload_off: u32,
    pub payload_len: u32,
    pub dest: u32,
    pub source: u32,
    pub request_id: u32,
    pub reply_id: u32,
    pub emoji: u32,
    pub bitfield: u32,
    pub want_response: u8,
    pub has_bitfield: u8,
}

/// One raw protobuf field. The value is split in half because the native module
/// header has no constructor for a 64-bit Python int; the caller reassembles it.
#[repr(C)]
pub struct FieldOut {
    pub value_lo: u32,
    pub value_hi: u32,
    pub number: u32,
    pub wire: u32,
    pub data_off: u32,
    pub data_len: u32,
    pub next: u32,
}

/// Borrows a caller-supplied buffer.
///
/// A null pointer with a non-zero length is rejected; a null pointer with zero
/// length becomes an empty slice, because an empty channel name is legitimate
/// and CPython hands back null for a zero-length buffer.
unsafe fn slice<'a>(ptr: *const u8, len: usize) -> Option<&'a [u8]> {
    if ptr.is_null() {
        if len == 0 {
            return Some(&[]);
        }
        return None;
    }
    Some(core::slice::from_raw_parts(ptr, len))
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_header_parse(
    frame: *const u8,
    len: usize,
    out: *mut HeaderOut,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let bytes = match slice(frame, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let h = match Header::parse(bytes) {
        Ok(h) => h,
        Err(e) => return e,
    };
    *out = HeaderOut {
        to: h.to,
        from: h.from,
        id: h.id,
        flags: h.flags,
        channel: h.channel,
        next_hop: h.next_hop,
        relay_node: h.relay_node,
        hop_limit: h.hop_limit(),
        hop_start: h.hop_start(),
        want_ack: h.want_ack() as u8,
        via_mqtt: h.via_mqtt() as u8,
        hops_away: h.hops_away().unwrap_or(0xFF),
    };
    OK
}

/// Percent remaining for a cell reading `millivolts`. Never negative, so the
/// answer comes back in the status like `meshtastic_channel_hash` does.
#[no_mangle]
pub extern "C" fn meshtastic_battery_percent(millivolts: u32) -> i32 {
    let mv = if millivolts > u16::MAX as u32 {
        u16::MAX
    } else {
        millivolts as u16
    };
    power::percent(mv) as i32
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_djb2(ptr: *const u8, len: usize, out: *mut u32) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    match slice(ptr, len) {
        Some(b) => {
            *out = hash::djb2(b);
            OK
        }
        None => ERR_NULL,
    }
}

/// Returns the hash in 0..=255, or a negative status.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_channel_hash(
    name: *const u8,
    name_len: usize,
    psk: *const u8,
    psk_len: usize,
) -> i32 {
    let (n, k) = match (slice(name, name_len), slice(psk, psk_len)) {
        (Some(n), Some(k)) => (n, k),
        _ => return ERR_NULL,
    };
    hash::channel_hash(n, k) as i32
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_preset_params(
    preset_index: u8,
    wide: u8,
    out: *mut PresetOut,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    match preset::params(preset_index, wide != 0) {
        Ok(p) => {
            *out = PresetOut {
                bw_hz: p.bw_hz,
                sf: p.sf,
                cr: p.cr,
            };
            OK
        }
        Err(e) => e,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_region(index: u8, out: *mut RegionOut) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    match region::region(index) {
        Ok(r) => {
            *out = RegionOut {
                start_hz: r.start_hz,
                end_hz: r.end_hz,
                spacing_hz: r.spacing_hz,
                duty_cycle_pct: r.duty_cycle_pct,
                power_limit_dbm: r.power_limit_dbm,
                wide_lora: r.wide_lora as u8,
            };
            OK
        }
        Err(e) => e,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_num_channels(
    region_index: u8,
    bw_hz: u32,
    out: *mut u32,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let r = match region::region(region_index) {
        Ok(r) => r,
        Err(e) => return e,
    };
    match region::num_channels(&r, bw_hz) {
        Ok(n) => {
            *out = n;
            OK
        }
        Err(e) => e,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_slot_frequency(
    region_index: u8,
    bw_hz: u32,
    slot: u32,
    out: *mut u32,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let r = match region::region(region_index) {
        Ok(r) => r,
        Err(e) => return e,
    };
    match region::slot_frequency_hz(&r, bw_hz, slot) {
        Ok(f) => {
            *out = f;
            OK
        }
        Err(e) => e,
    }
}

/// `channel_num` 0 hashes `name`; anything else is a one-based slot index.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_channel_frequency(
    region_index: u8,
    bw_hz: u32,
    channel_num: u32,
    name: *const u8,
    name_len: usize,
    out: *mut u32,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let n = match slice(name, name_len) {
        Some(n) => n,
        None => return ERR_NULL,
    };
    let r = match region::region(region_index) {
        Ok(r) => r,
        Err(e) => return e,
    };
    match region::channel_frequency_hz(&r, bw_hz, channel_num, n) {
        Ok(f) => {
            *out = f;
            OK
        }
        Err(e) => e,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_data_parse(
    ptr: *const u8,
    len: usize,
    out: *mut DataOut,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let bytes = match slice(ptr, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let d = match proto::parse_data(bytes) {
        Ok(d) => d,
        Err(e) => return e,
    };
    *out = DataOut {
        portnum: d.portnum,
        payload_off: d.payload_off as u32,
        payload_len: d.payload_len as u32,
        dest: d.dest,
        source: d.source,
        request_id: d.request_id,
        reply_id: d.reply_id,
        emoji: d.emoji,
        bitfield: d.bitfield,
        want_response: d.want_response as u8,
        has_bitfield: d.has_bitfield as u8,
    };
    OK
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_proto_field(
    ptr: *const u8,
    len: usize,
    at: usize,
    out: *mut FieldOut,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let bytes = match slice(ptr, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let f = match proto::field(bytes, at) {
        Ok(f) => f,
        Err(e) => return e,
    };
    *out = FieldOut {
        value_lo: f.value as u32,
        value_hi: (f.value >> 32) as u32,
        number: f.number,
        wire: f.wire,
        data_off: f.data_off as u32,
        data_len: f.data_len as u32,
        next: f.next as u32,
    };
    OK
}

/// Borrows a caller-supplied buffer for writing.
unsafe fn slice_mut<'a>(ptr: *mut u8, len: usize) -> Option<&'a mut [u8]> {
    if ptr.is_null() {
        if len == 0 {
            return Some(&mut []);
        }
        return None;
    }
    Some(core::slice::from_raw_parts_mut(ptr, len))
}

/// Lays down a 16-byte header. Fields are passed flat rather than in a struct
/// so the shim never has to agree with this crate about padding.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_header_write(
    to: u32,
    from: u32,
    id: u32,
    flags: u8,
    channel: u8,
    next_hop: u8,
    relay_node: u8,
    out: *mut u8,
    len: usize,
) -> i32 {
    let bytes = match slice_mut(out, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let h = Header {
        to,
        from,
        id,
        flags,
        channel,
        next_hop,
        relay_node,
    };
    match h.write(bytes) {
        Ok(()) => OK,
        Err(e) => e,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_data_encode(
    portnum: u32,
    payload: *const u8,
    payload_len: usize,
    want_response: u8,
    bitfield: u32,
    has_bitfield: u8,
    out: *mut u8,
    out_len: usize,
    used: *mut u32,
) -> i32 {
    if used.is_null() {
        return ERR_NULL;
    }
    let body = match slice(payload, payload_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let buf = match slice_mut(out, out_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let bits = if has_bitfield != 0 { Some(bitfield) } else { None };
    match proto::encode_data(portnum, body, want_response != 0, bits, buf) {
        Ok(n) => {
            *used = n as u32;
            OK
        }
        Err(e) => e,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_airtime_us(
    payload_len: usize,
    sf: u8,
    bw_hz: u32,
    cr: u8,
    out: *mut u32,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    match airtime::time_on_air_us(payload_len, sf, bw_hz, cr) {
        Ok(us) => {
            *out = us;
            OK
        }
        Err(e) => e,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_duty_record(
    window: *mut u8,
    len: usize,
    slot: u32,
    us: u32,
) -> i32 {
    if window.is_null() {
        return ERR_NULL;
    }
    match duty::record(core::slice::from_raw_parts_mut(window, len), slot, us) {
        Ok(()) => OK,
        Err(e) => e,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_duty_used(
    window: *mut u8,
    len: usize,
    slot: u32,
    out: *mut u32,
) -> i32 {
    if window.is_null() || out.is_null() {
        return ERR_NULL;
    }
    match duty::used(core::slice::from_raw_parts_mut(window, len), slot) {
        Ok(total) => {
            *out = total;
            OK
        }
        Err(e) => e,
    }
}

/// These four return the new offset, so the shim can chain them, and a
/// negative status otherwise.
fn wrote(r: Result<usize, i32>) -> i32 {
    match r {
        Ok(at) => at as i32,
        Err(e) => e,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_pb_uint(
    out: *mut u8,
    room: usize,
    at: usize,
    number: u32,
    value: u32,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    wrote(pbuf::uint(
        core::slice::from_raw_parts_mut(out, room),
        at,
        number,
        value as u64,
    ))
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_pb_int32(
    out: *mut u8,
    room: usize,
    at: usize,
    number: u32,
    value: i32,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    wrote(pbuf::int32(
        core::slice::from_raw_parts_mut(out, room),
        at,
        number,
        value,
    ))
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_pb_fixed32(
    out: *mut u8,
    room: usize,
    at: usize,
    number: u32,
    value: u32,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    wrote(pbuf::fixed32(
        core::slice::from_raw_parts_mut(out, room),
        at,
        number,
        value,
    ))
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_pb_blob_head(
    out: *mut u8,
    room: usize,
    at: usize,
    number: u32,
    length: usize,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    wrote(pbuf::blob_head(
        core::slice::from_raw_parts_mut(out, room),
        at,
        number,
        length,
    ))
}

/// The running fix, flattened for the shim. Written a field at a time by the
/// wrapper below rather than assigned whole: a struct this size turns into a
/// memcpy the natmod linker cannot resolve.
#[repr(C)]
pub struct FixOut {
    pub lat_e7: i32,
    pub lon_e7: i32,
    pub alt_mm: i32,
    pub when: u32,
    pub good: u32,
    pub bad: u32,
    pub hdop_cm: u16,
    pub sats: u8,
    pub in_view: u8,
    pub quality: u8,
    pub status: u8,
    pub mode: u8,
    pub nav: u8,
    pub have: u8,
    pub valid: u8,
}

#[repr(C)]
pub struct CivilOut {
    pub year: u16,
    pub month: u8,
    pub day: u8,
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
}

/// How much of the echo buffer a feed filled, alongside the sentence count.
#[repr(C)]
pub struct FeedOut {
    pub landed: u32,
    pub echo_used: u32,
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_nmea_reset(state: *mut u8, len: usize) -> i32 {
    if state.is_null() {
        return ERR_NULL;
    }
    nmea::reset(core::slice::from_raw_parts_mut(state, len))
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_nmea_sentence(
    state: *mut u8,
    len: usize,
    line: *const u8,
    line_len: usize,
) -> i32 {
    if state.is_null() {
        return ERR_NULL;
    }
    let text = match slice(line, line_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    nmea::sentence(core::slice::from_raw_parts_mut(state, len), text)
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_nmea_feed(
    state: *mut u8,
    len: usize,
    chunk: *const u8,
    chunk_len: usize,
    echo: *mut u8,
    echo_len: usize,
    echo_at: usize,
    out: *mut FeedOut,
) -> i32 {
    if state.is_null() || out.is_null() {
        return ERR_NULL;
    }
    let bytes = match slice(chunk, chunk_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let sink: &mut [u8] = if echo.is_null() || echo_len == 0 {
        &mut []
    } else {
        core::slice::from_raw_parts_mut(echo, echo_len)
    };
    let (landed, used) = nmea::feed(
        core::slice::from_raw_parts_mut(state, len),
        bytes,
        sink,
        echo_at,
    );
    if landed < 0 {
        return landed;
    }
    (*out).landed = landed as u32;
    (*out).echo_used = used as u32;
    OK
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_nmea_fix(
    state: *const u8,
    len: usize,
    out: *mut FixOut,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let bytes = match slice(state, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let f = match nmea::read(bytes) {
        Ok(f) => f,
        Err(e) => return e,
    };
    (*out).lat_e7 = f.lat_e7;
    (*out).lon_e7 = f.lon_e7;
    (*out).alt_mm = f.alt_mm;
    (*out).when = f.when;
    (*out).good = f.good;
    (*out).bad = f.bad;
    (*out).hdop_cm = f.hdop_cm;
    (*out).sats = f.sats;
    (*out).in_view = f.in_view;
    (*out).quality = f.quality;
    (*out).status = f.status;
    (*out).mode = f.mode;
    (*out).nav = f.nav;
    (*out).have = f.have;
    (*out).valid = f.valid;
    OK
}

/// The talker at `index`: 1 with the entry filled in, 0 past the last one.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_nmea_talker(
    state: *const u8,
    len: usize,
    index: usize,
    id: *mut u8,
    count: *mut u8,
) -> i32 {
    if id.is_null() || count.is_null() {
        return ERR_NULL;
    }
    let bytes = match slice(state, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    match nmea::talker(bytes, index) {
        Err(e) => e,
        Ok(None) => 0,
        Ok(Some((a, b, n))) => {
            *id = a;
            *id.add(1) = b;
            *count = n;
            1
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_nmea_civil(when: u32, out: *mut CivilOut) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let c = nmea::civil(when);
    (*out).year = c.year;
    (*out).month = c.month;
    (*out).day = c.day;
    (*out).hour = c.hour;
    (*out).minute = c.minute;
    (*out).second = c.second;
    OK
}

/// How far `stream_feed` got, and how long the frame it finished is.
#[repr(C)]
pub struct StreamOut {
    pub frame_len: i32,
    pub used: u32,
}

/// What the reader has seen, for `stream_counts`.
#[repr(C)]
pub struct StreamCounts {
    pub lost: u32,
    pub frames: u32,
    pub partial: u32,
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_stream_reset(state: *mut u8, len: usize) -> i32 {
    match slice_mut(state, len) {
        Some(b) => stream::reset(b),
        None => ERR_NULL,
    }
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_stream_feed(
    state: *mut u8,
    state_len: usize,
    chunk: *const u8,
    chunk_len: usize,
    at: usize,
    out: *mut StreamOut,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let s = match slice_mut(state, state_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let c = match slice(chunk, chunk_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let (frame_len, used) = stream::feed(s, c, at);
    if frame_len < 0 {
        return frame_len;
    }
    (*out).frame_len = frame_len;
    (*out).used = used as u32;
    OK
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_stream_counts(
    state: *const u8,
    len: usize,
    out: *mut StreamCounts,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let s = match slice(state, len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let c = match stream::counts(s) {
        Ok(c) => c,
        Err(e) => return e,
    };
    (*out).lost = c.lost;
    (*out).frames = c.frames;
    (*out).partial = c.partial as u32;
    OK
}

#[no_mangle]
pub unsafe extern "C" fn meshtastic_stream_frame(
    out: *mut u8,
    out_len: usize,
    at: usize,
    payload: *const u8,
    payload_len: usize,
) -> i32 {
    let dst = match slice_mut(out, out_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let src = match slice(payload, payload_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    stream::frame(dst, at, src)
}

/// Writes a `Position` for the fix held in a GNSS state buffer.
///
/// Takes the state rather than a decoded fix so that nothing has to cross the
/// boundary twice: the caller already owns the buffer the parser fills.
/// Returns the encoded length, or a negative error.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_position_encode(
    out: *mut u8,
    out_len: usize,
    state: *const u8,
    state_len: usize,
    precision: u32,
) -> i32 {
    let dst = match slice_mut(out, out_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    let src = match slice(state, state_len) {
        Some(b) => b,
        None => return ERR_NULL,
    };
    match payload::encode_position(dst, src, precision) {
        Ok(n) => n as i32,
        Err(e) => e,
    }
}
