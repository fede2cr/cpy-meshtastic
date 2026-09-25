//! Writing protobuf into a buffer the caller already owns.
//!
//! The reading half in [`crate::proto`] is a strict walker, because what
//! arrives from the air is hostile input. This half has the opposite problem:
//! what leaves is built from our own settings and cannot be malformed, so
//! correctness was never the reason to move it here. Allocation was.
//!
//! Built in Python, one field cost five short-lived objects -- a bytearray and
//! a bytes for the tag, the same again for the value, and the concatenation --
//! and a message is a handful of fields. Sending the roster to a phone wrote
//! several hundred fields in a burst, at the one moment the heap is smallest,
//! and small short-lived objects are what leaves it too fragmented to hand out
//! the next few hundred bytes. Here the whole message is written into one
//! buffer and nothing is allocated at all.
//!
//! Proto3 omits a scalar holding its type's default, but that decision stays
//! with the caller: a `oneof` member is written even when empty, because the
//! point of it is that it is present. These functions write what they are
//! asked to write.

use crate::proto::{WIRE_I32, WIRE_LEN, WIRE_VARINT};
use crate::ERR_NO_SPACE;

pub fn put_varint(out: &mut [u8], at: usize, mut value: u64) -> Result<usize, i32> {
    let mut i = at;
    loop {
        if i >= out.len() {
            return Err(ERR_NO_SPACE);
        }
        let byte = (value & 0x7F) as u8;
        value >>= 7;
        if value == 0 {
            out[i] = byte;
            return Ok(i + 1);
        }
        out[i] = byte | 0x80;
        i += 1;
    }
}

pub fn put_tag(out: &mut [u8], at: usize, number: u32, wire: u32) -> Result<usize, i32> {
    put_varint(out, at, ((number << 3) | wire) as u64)
}

pub fn uint(out: &mut [u8], at: usize, number: u32, value: u64) -> Result<usize, i32> {
    let at = put_tag(out, at, number, WIRE_VARINT)?;
    put_varint(out, at, value)
}

/// A signed `int32`, which proto3 sign-extends to 64 bits and so always writes
/// as ten bytes when it is negative.
pub fn int32(out: &mut [u8], at: usize, number: u32, value: i32) -> Result<usize, i32> {
    uint(out, at, number, value as i64 as u64)
}

pub fn fixed32(out: &mut [u8], at: usize, number: u32, value: u32) -> Result<usize, i32> {
    let at = put_tag(out, at, number, WIRE_I32)?;
    if at + 4 > out.len() {
        return Err(ERR_NO_SPACE);
    }
    for k in 0..4 {
        out[at + k] = (value >> (8 * k)) as u8;
    }
    Ok(at + 4)
}

/// The tag and length that precede a length-delimited field, without its data.
///
/// The data is already one object on the Python side, so copying it through a
/// scratch buffer here would need that buffer sized for the largest payload on
/// the wire -- 320 bytes of permanent heap to save one concatenation. The
/// header is at most six.
pub fn blob_head(out: &mut [u8], at: usize, number: u32, length: usize) -> Result<usize, i32> {
    let at = put_tag(out, at, number, WIRE_LEN)?;
    put_varint(out, at, length as u64)
}
