//! The keystore's on-flash layout: a header, then tagged records.
//!
//! What the board knows about itself lives in one region of the nRF52840's
//! internal NVM -- the node number, the channel key, and the settings a node
//! has to be able to rewrite while it is running. This module owns the shape of
//! that region and nothing else. Which tag means what is `keystore.py`'s
//! business, and reaching the flash is `microcontroller.nvm`'s.
//!
//! The layout is a 10-byte header -- magic, a u16 body length, a u32 CRC-32 of
//! the body -- followed by records of a tag byte, a length byte and that many
//! bytes of value. Identical in shape to `cache`, and deliberately so, but with
//! its own magic and a one-byte length, because a record here is a key or a
//! name and never approaches 255 bytes.
//!
//! Nothing allocates. The caller owns one buffer, fills it record by record and
//! stamps the header over the front when the length is finally known, which is
//! the same sequence `cache` uses and for the same reason: the heap this runs
//! on cannot be relied upon to hand out a second copy of anything.

use crate::cache::crc32;
use crate::le::{put_u16, put_u32, u16_at, u32_at};
use crate::{ERR_NO_SPACE, ERR_TRUNCATED};

/// Identifies a formatted store, and would identify a format change.
static MAGIC: [u8; 4] = [b'M', b'K', b'Y', b'1'];

/// Magic, a u16 body length and a u32 CRC of the body.
pub const HEADER_LEN: usize = 10;

/// A tag byte and a length byte before every value.
pub const TAG_LEN: usize = 2;

/// What one record's value can be, which is what a length byte can say.
pub const MAX_VALUE: usize = 255;

/// The body length a header claims, or `None` if there is no store here.
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
/// A blank page, an interrupted write and somebody else's layout all come back
/// the same way. A board whose store was cut mid-write has to boot as an
/// unprovisioned one rather than refuse to boot, so there is nothing to do with
/// any of them but read the region as empty.
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

/// One record: its tag, where its value is, and where the next one starts.
#[repr(C)]
#[derive(Debug, PartialEq)]
pub struct RecordOut {
    pub tag: u32,
    pub off: u32,
    pub len: u32,
    pub next: u32,
}

pub fn record(raw: &[u8], at: usize, end: usize) -> Result<Option<RecordOut>, i32> {
    if at + TAG_LEN > end {
        return Ok(None);
    }
    let size = raw[at + 1] as usize;
    let off = at + TAG_LEN;
    if off + size > end {
        // The length and the CRC agreed, so this is a bug rather than a
        // half-written page.
        return Err(ERR_TRUNCATED);
    }
    Ok(Some(RecordOut {
        tag: raw[at] as u32,
        off: off as u32,
        len: size as u32,
        next: (off + size) as u32,
    }))
}

/// Appends one record, and answers where the next one goes.
///
/// The caller writes them in tag order, so that equal contents always encode to
/// equal bytes; that is what lets `keystore.save` compare before writing and
/// decline to spend an erase cycle on an unchanged store.
pub fn put(out: &mut [u8], at: usize, tag: u8, value: &[u8]) -> Result<usize, i32> {
    if value.len() > MAX_VALUE {
        return Err(ERR_NO_SPACE);
    }
    let end = at + TAG_LEN + value.len();
    if end > out.len() {
        return Err(ERR_NO_SPACE);
    }
    out[at] = tag;
    out[at + 1] = value.len() as u8;
    for (i, b) in value.iter().enumerate() {
        out[at + TAG_LEN + i] = *b;
    }
    Ok(end)
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

// ------------------------------------------------------------------- C ABI

use crate::ERR_NULL;

/// # Safety
/// `head` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_store_length(head: *const u8, len: usize) -> i32 {
    if head.is_null() {
        return ERR_NULL;
    }
    length(core::slice::from_raw_parts(head, len)).map_or(-1, |n| n as i32)
}

/// # Safety
/// `raw` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_store_verify(raw: *const u8, len: usize) -> i32 {
    if raw.is_null() {
        return ERR_NULL;
    }
    verify(core::slice::from_raw_parts(raw, len)).map_or(-1, |n| n as i32)
}

/// # Safety
/// `raw` must point to `len` readable bytes and `out` must be writable.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_store_record(
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
/// `out` must point to `room` writable bytes, `value` to `value_len` readable.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_store_put(
    out: *mut u8,
    room: usize,
    at: usize,
    tag: u32,
    value: *const u8,
    value_len: usize,
) -> i32 {
    if out.is_null() || (value.is_null() && value_len != 0) {
        return ERR_NULL;
    }
    let value = if value_len == 0 {
        &[][..]
    } else {
        core::slice::from_raw_parts(value, value_len)
    };
    match put(
        core::slice::from_raw_parts_mut(out, room),
        at,
        tag as u8,
        value,
    ) {
        Ok(next) => next as i32,
        Err(rc) => rc,
    }
}

/// # Safety
/// `out` must point to `room` writable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_store_finish(out: *mut u8, room: usize, used: usize) -> i32 {
    if out.is_null() {
        return ERR_NULL;
    }
    match finish(core::slice::from_raw_parts_mut(out, room), used) {
        Ok(()) => (HEADER_LEN + used) as i32,
        Err(rc) => rc,
    }
}
