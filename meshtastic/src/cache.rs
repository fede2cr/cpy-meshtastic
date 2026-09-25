//! The record format `meshcache` writes to flash: a header, then a run of
//! length-prefixed records holding peers and messages.
//!
//! This is here rather than in Python for the allocations. The region is
//! filled at the moments the heap is tightest -- a save happens on the way out
//! of a session, with the BLE queue holding its two kilobytes -- and building
//! it out of `struct.pack` results joined with `+` cost half a dozen throwaway
//! objects per record, sixty-odd of them, at exactly the wrong time. Here a
//! record is written straight into the caller's buffer at an offset.
//!
//! Which fields are kept, and why the times are ages rather than timestamps,
//! is `meshcache`'s decision and is documented there. What is here is only the
//! byte layout and the CRC.

use crate::le::{put_u16, put_u32, u16_at, u32_at};
use crate::{ERR_NO_SPACE, ERR_NULL, ERR_TRUNCATED, OK};

/// Identifies a formatted cache, and would identify a format change.
/// `static`, not `const`: a const array is copied to the stack at every use,
/// and the memcpy that takes is a symbol the natmod linker does not have.
static MAGIC: [u8; 4] = [b'M', b'K', b'C', b'1'];

/// magic, then the body length and its CRC.
pub const HEADER_LEN: usize = 10;
/// Kind byte and payload length, in front of every record.
pub const KIND_LEN: usize = 3;

/// Record kinds. Numbered rather than positional so a later format can add one
/// without moving the others, and so an unknown kind can be walked past.
pub const NODE: u8 = 1;
pub const MESSAGE: u8 = 2;

const NODE_FIXED: usize = 14;
const MESSAGE_FIXED: usize = 20;

/// In a name's length byte. Also what an over-long name becomes: names are
/// capped well below this on the way in, so a longer one is a bug elsewhere,
/// and saving a truncation would bring back a split character.
const NAME_ABSENT: u8 = 255;

/// Half a byte of CRC-32 at a time. The full table is a kilobyte of flash to
/// save a few microseconds on a buffer that is written twice a day.
static CRC_NIBBLE: [u32; 16] = [
    0x0000_0000, 0x1DB7_1064, 0x3B6E_20C8, 0x26D9_30AC,
    0x76DC_4190, 0x6B6B_51F4, 0x4DB2_6158, 0x5005_713C,
    0xEDB8_8320, 0xF00F_9344, 0xD6D6_A3E8, 0xCB61_B38C,
    0x9B64_C2B0, 0x86D3_D2D4, 0xA00A_E278, 0xBDBD_F21C,
];

/// The same CRC-32 `binascii.crc32` computes, which is what already-saved
/// regions were written with.
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for i in 0..data.len() {
        crc ^= data[i] as u32;
        crc = (crc >> 4) ^ CRC_NIBBLE[(crc & 0x0F) as usize];
        crc = (crc >> 4) ^ CRC_NIBBLE[(crc & 0x0F) as usize];
    }
    !crc
}

fn name_len(len: i32) -> usize {
    if len < 0 || len > 254 {
        1
    } else {
        1 + len as usize
    }
}

/// Bytes one NODE record takes, given the encoded lengths of its two names.
/// A negative length is a name the peer has not sent.
pub fn peer_len(short: i32, long: i32) -> usize {
    KIND_LEN + NODE_FIXED + name_len(short) + name_len(long)
}

pub fn message_len(text: usize) -> usize {
    KIND_LEN + MESSAGE_FIXED + text
}

fn put_kind(out: &mut [u8], at: usize, kind: u8, size: usize) {
    out[at] = kind;
    put_u16(out, at + 1, size as u16);
}

fn put_name(out: &mut [u8], at: usize, name: &[u8], present: bool) -> usize {
    if !present || name.len() > 254 {
        out[at] = NAME_ABSENT;
        return at + 1;
    }
    out[at] = name.len() as u8;
    for i in 0..name.len() {
        out[at + 1 + i] = name[i];
    }
    at + 1 + name.len()
}

#[allow(clippy::too_many_arguments)]
pub fn write_peer(
    out: &mut [u8],
    at: usize,
    num: u32,
    age: u32,
    count: u32,
    hops: u8,
    hw: u8,
    role: u8,
    snr: i8,
    short: &[u8],
    has_short: bool,
    long: &[u8],
    has_long: bool,
) -> Result<usize, i32> {
    let size = peer_len(
        if has_short { short.len() as i32 } else { -1 },
        if has_long { long.len() as i32 } else { -1 },
    );
    if at + size > out.len() {
        return Err(ERR_NO_SPACE);
    }
    put_kind(out, at, NODE, size - KIND_LEN);
    let body = at + KIND_LEN;
    put_u32(out, body, num);
    put_u32(out, body + 4, age);
    put_u16(out, body + 8, if count > 0xFFFF { 0xFFFF } else { count as u16 });
    out[body + 10] = hops;
    out[body + 11] = hw;
    out[body + 12] = role;
    out[body + 13] = snr as u8;
    let next = put_name(out, body + NODE_FIXED, short, has_short);
    Ok(put_name(out, next, long, has_long))
}

#[allow(clippy::too_many_arguments)]
pub fn write_message(
    out: &mut [u8],
    at: usize,
    id: u32,
    from: u32,
    to: u32,
    time: u32,
    flags: u8,
    channel: u8,
    hops: u8,
    snr: i8,
    text: &[u8],
) -> Result<usize, i32> {
    let size = message_len(text.len());
    if at + size > out.len() {
        return Err(ERR_NO_SPACE);
    }
    put_kind(out, at, MESSAGE, size - KIND_LEN);
    let body = at + KIND_LEN;
    put_u32(out, body, id);
    put_u32(out, body + 4, from);
    put_u32(out, body + 8, to);
    put_u32(out, body + 12, time);
    out[body + 16] = flags;
    out[body + 17] = channel;
    out[body + 18] = hops;
    out[body + 19] = snr as u8;
    let start = body + MESSAGE_FIXED;
    for i in 0..text.len() {
        out[start + i] = text[i];
    }
    Ok(start + text.len())
}

/// Stamps the header over the front of a filled buffer.
///
/// Last, because the CRC covers what the records left behind and the length is
/// not known until they have all gone in.
pub fn finish(out: &mut [u8], used: usize) -> Result<(), i32> {
    if HEADER_LEN + used > out.len() {
        return Err(ERR_NO_SPACE);
    }
    for i in 0..MAGIC.len() {
        out[i] = MAGIC[i];
    }
    put_u16(out, 4, used as u16);
    put_u32(out, 6, crc32(&out[HEADER_LEN..HEADER_LEN + used]));
    Ok(())
}

/// The body length a header claims, or `None` if there is no cache here.
pub fn length(head: &[u8]) -> Option<usize> {
    if head.len() < HEADER_LEN {
        return None;
    }
    for i in 0..MAGIC.len() {
        if head[i] != MAGIC[i] {
            return None;
        }
    }
    Some(u16_at(head, 4) as usize)
}

/// Where the records end, or `None` if what is there cannot be trusted.
///
/// Everything a corrupt region could be -- a blank page, a half-finished
/// write, the keystore's own layout -- comes back the same way, because there
/// is nothing to do about any of them but ignore the region.
pub fn verify(raw: &[u8]) -> Option<usize> {
    let len = length(raw)?;
    let end = HEADER_LEN + len;
    if end > raw.len() {
        return None;
    }
    if crc32(&raw[HEADER_LEN..end]) != u32_at(raw, 6) {
        return None;
    }
    Some(end)
}

/// One record: its kind, where its payload is, and where the next one starts.
#[repr(C)]
#[derive(Debug, PartialEq)]
pub struct RecordOut {
    pub kind: u32,
    pub off: u32,
    pub len: u32,
    pub next: u32,
}

pub fn record(raw: &[u8], at: usize, end: usize) -> Result<Option<RecordOut>, i32> {
    if at + KIND_LEN > end {
        return Ok(None);
    }
    let size = u16_at(raw, at + 1) as usize;
    let off = at + KIND_LEN;
    if off + size > end {
        // The length and the CRC agreed, so this is a bug rather than a
        // half-written page.
        return Err(ERR_TRUNCATED);
    }
    Ok(Some(RecordOut {
        kind: raw[at] as u32,
        off: off as u32,
        len: size as u32,
        next: (off + size) as u32,
    }))
}

/// A NODE payload. The names come back as offsets into the same buffer, with a
/// length of -1 for a peer that never sent one.
#[repr(C)]
pub struct PeerOut {
    pub num: u32,
    pub age: u32,
    pub count: u16,
    pub hops: u8,
    pub hw: u8,
    pub role: u8,
    pub snr: i8,
    pub short_off: u16,
    pub short_len: i16,
    pub long_off: u16,
    pub long_len: i16,
}

fn take_name(raw: &[u8], at: usize, end: usize) -> Result<(u16, i16, usize), i32> {
    if at >= end {
        return Err(ERR_TRUNCATED);
    }
    let size = raw[at];
    if size == NAME_ABSENT {
        return Ok((0, -1, at + 1));
    }
    let size = size as usize;
    if at + 1 + size > end {
        return Err(ERR_TRUNCATED);
    }
    Ok(((at + 1) as u16, size as i16, at + 1 + size))
}

pub fn read_peer(raw: &[u8], off: usize, len: usize) -> Result<PeerOut, i32> {
    if len < NODE_FIXED {
        return Err(ERR_TRUNCATED);
    }
    let end = off + len;
    let (short_off, short_len, next) = take_name(raw, off + NODE_FIXED, end)?;
    let (long_off, long_len, _) = take_name(raw, next, end)?;
    Ok(PeerOut {
        num: u32_at(raw, off),
        age: u32_at(raw, off + 4),
        count: u16_at(raw, off + 8),
        hops: raw[off + 10],
        hw: raw[off + 11],
        role: raw[off + 12],
        snr: raw[off + 13] as i8,
        short_off,
        short_len,
        long_off,
        long_len,
    })
}

/// A MESSAGE payload. The text is whatever follows the fixed fields and needs
/// no length of its own, because the record has one.
#[repr(C)]
pub struct MessageOut {
    pub id: u32,
    pub from: u32,
    pub to: u32,
    pub time: u32,
    pub flags: u8,
    pub channel: u8,
    pub hops: u8,
    pub snr: i8,
    pub text_off: u32,
    pub text_len: u32,
}

pub fn read_message(raw: &[u8], off: usize, len: usize) -> Result<MessageOut, i32> {
    if len < MESSAGE_FIXED {
        return Err(ERR_TRUNCATED);
    }
    Ok(MessageOut {
        id: u32_at(raw, off),
        from: u32_at(raw, off + 4),
        to: u32_at(raw, off + 8),
        time: u32_at(raw, off + 12),
        flags: raw[off + 16],
        channel: raw[off + 17],
        hops: raw[off + 18],
        snr: raw[off + 19] as i8,
        text_off: (off + MESSAGE_FIXED) as u32,
        text_len: (len - MESSAGE_FIXED) as u32,
    })
}

// ------------------------------------------------------------------- C ABI

unsafe fn opt<'a>(ptr: *const u8, len: i32) -> (&'a [u8], bool) {
    if len < 0 || ptr.is_null() {
        (&[], false)
    } else {
        (core::slice::from_raw_parts(ptr, len as usize), true)
    }
}

/// # Safety
/// `head` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_cache_length(head: *const u8, len: usize) -> i32 {
    if head.is_null() {
        return ERR_NULL;
    }
    length(core::slice::from_raw_parts(head, len)).map_or(-1, |n| n as i32)
}

/// # Safety
/// `raw` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_cache_verify(raw: *const u8, len: usize) -> i32 {
    if raw.is_null() {
        return ERR_NULL;
    }
    verify(core::slice::from_raw_parts(raw, len)).map_or(-1, |n| n as i32)
}

/// # Safety
/// `raw` must point to `len` readable bytes and `out` must be writable.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_cache_record(
    raw: *const u8,
    len: usize,
    at: usize,
    end: usize,
    out: *mut RecordOut,
) -> i32 {
    if raw.is_null() || out.is_null() || end > len {
        return ERR_NULL;
    }
    match record(core::slice::from_raw_parts(raw, len), at, end) {
        Err(rc) => rc,
        // 1 for a record, 0 for the end of them: the caller cannot tell those
        // apart from the struct, which is left untouched at the end.
        Ok(None) => 0,
        Ok(Some(r)) => {
            *out = r;
            1
        }
    }
}

/// # Safety
/// `raw` must point to `len` readable bytes and `out` must be writable.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_cache_read_peer(
    raw: *const u8,
    len: usize,
    off: usize,
    payload_len: usize,
    out: *mut PeerOut,
) -> i32 {
    if raw.is_null() || out.is_null() || off + payload_len > len {
        return ERR_NULL;
    }
    match read_peer(core::slice::from_raw_parts(raw, len), off, payload_len) {
        Err(rc) => rc,
        Ok(p) => {
            *out = p;
            OK
        }
    }
}

/// # Safety
/// `raw` must point to `len` readable bytes and `out` must be writable.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_cache_read_message(
    raw: *const u8,
    len: usize,
    off: usize,
    payload_len: usize,
    out: *mut MessageOut,
) -> i32 {
    if raw.is_null() || out.is_null() || off + payload_len > len {
        return ERR_NULL;
    }
    match read_message(core::slice::from_raw_parts(raw, len), off, payload_len) {
        Err(rc) => rc,
        Ok(m) => {
            // Field by field rather than `*out = m`. This struct is seven
            // words, which is past the size the compiler will copy inline,
            // and the memcpy it reaches for instead is a symbol the natmod
            // linker has nowhere to get.
            (*out).id = m.id;
            (*out).from = m.from;
            (*out).to = m.to;
            (*out).time = m.time;
            (*out).flags = m.flags;
            (*out).channel = m.channel;
            (*out).hops = m.hops;
            (*out).snr = m.snr;
            (*out).text_off = m.text_off;
            (*out).text_len = m.text_len;
            OK
        }
    }
}

#[no_mangle]
pub extern "C" fn meshtastic_cache_peer_len(short: i32, long: i32) -> i32 {
    peer_len(short, long) as i32
}

#[no_mangle]
pub extern "C" fn meshtastic_cache_message_len(text: usize) -> i32 {
    message_len(text) as i32
}

/// # Safety
/// `out` must point to `len` writable bytes; the name pointers to their lengths.
#[no_mangle]
#[allow(clippy::too_many_arguments)]
pub unsafe extern "C" fn meshtastic_cache_write_peer(
    out: *mut u8,
    len: usize,
    at: usize,
    num: u32,
    age: u32,
    count: u32,
    hops: u8,
    hw: u8,
    role: u8,
    snr: i8,
    short: *const u8,
    short_len: i32,
    long: *const u8,
    long_len: i32,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    let (short, has_short) = opt(short, short_len);
    let (long, has_long) = opt(long, long_len);
    match write_peer(
        core::slice::from_raw_parts_mut(out, len),
        at, num, age, count, hops, hw, role, snr,
        short, has_short, long, has_long,
    ) {
        Err(rc) => rc,
        Ok(next) => next as i32,
    }
}

/// # Safety
/// `out` must point to `len` writable bytes and `text` to `text_len`.
#[no_mangle]
#[allow(clippy::too_many_arguments)]
pub unsafe extern "C" fn meshtastic_cache_write_message(
    out: *mut u8,
    len: usize,
    at: usize,
    id: u32,
    from: u32,
    to: u32,
    time: u32,
    flags: u8,
    channel: u8,
    hops: u8,
    snr: i8,
    text: *const u8,
    text_len: usize,
) -> i32 {
    if out.is_null() || (text.is_null() && text_len != 0) {
        return ERR_NULL;
    }
    let text = if text_len == 0 {
        &[]
    } else {
        core::slice::from_raw_parts(text, text_len)
    };
    match write_message(
        core::slice::from_raw_parts_mut(out, len),
        at, id, from, to, time, flags, channel, hops, snr, text,
    ) {
        Err(rc) => rc,
        Ok(next) => next as i32,
    }
}

/// # Safety
/// `out` must point to `len` writable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_cache_finish(
    out: *mut u8,
    len: usize,
    used: usize,
) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    match finish(core::slice::from_raw_parts_mut(out, len), used) {
        Err(rc) => rc,
        Ok(()) => OK,
    }
}
