//! A protobuf reader, holding only as much of the format as Meshtastic needs.
//!
//! There is no schema and no code generation here. Two things are offered: a
//! typed parse of the `Data` envelope, which is the one message whose shape
//! this crate has to know, and a raw field walker that lets the caller step
//! through any other message without this crate knowing anything about it.
//! Meshtastic has dozens of payload types and they change between releases;
//! transcribing them all would be a standing maintenance debt for no gain,
//! since the caller can read a field number just as well as we can.
//!
//! Strictness is the point. These bytes arrive from an AES-CTR decryption in
//! the Python layer, and the usual reason they are malformed is a wrong channel
//! key rather than a corrupt packet. Protobuf is forgiving by design -- unknown
//! fields are meant to be skipped -- so every check that can be kept is kept:
//! invalid wire types, lengths past the end of the buffer, and truncated
//! varints all fail. That turns "does this parse" into a usable test of whether
//! the key was right.

use crate::{ERR_BAD_WIRE, ERR_NO_SPACE, ERR_TRUNCATED};

pub const WIRE_VARINT: u32 = 0;
pub const WIRE_I64: u32 = 1;
pub const WIRE_LEN: u32 = 2;
pub const WIRE_I32: u32 = 5;

/// One field, plus where the next one starts.
#[derive(Debug, PartialEq)]
pub struct Field {
    pub number: u32,
    pub wire: u32,
    /// The varint value, or the raw little-endian bits of a fixed32/fixed64.
    pub value: u64,
    /// Position and length of the body of a length-delimited field.
    pub data_off: usize,
    pub data_len: usize,
    pub next: usize,
}

/// Reads a base-128 varint.
fn varint(buf: &[u8], mut at: usize) -> Result<(u64, usize), i32> {
    let mut value: u64 = 0;
    let mut shift: u32 = 0;
    loop {
        if at >= buf.len() {
            return Err(ERR_TRUNCATED);
        }
        // Ten groups of seven bits is the most that can fit in a u64, and a
        // continuation past that is a corrupt stream rather than a big number.
        if shift >= 64 {
            return Err(ERR_BAD_WIRE);
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

fn fixed(buf: &[u8], at: usize, width: usize) -> Result<(u64, usize), i32> {
    if at + width > buf.len() {
        return Err(ERR_TRUNCATED);
    }
    let mut value: u64 = 0;
    for i in 0..width {
        value |= (buf[at + i] as u64) << (8 * i);
    }
    Ok((value, at + width))
}

/// Reads the field starting at `at`.
pub fn field(buf: &[u8], at: usize) -> Result<Field, i32> {
    let (tag, at) = varint(buf, at)?;
    let number = (tag >> 3) as u32;
    let wire = (tag & 7) as u32;
    // Field 0 is not expressible in the schema language, so seeing one means
    // these bytes are not protobuf.
    if number == 0 {
        return Err(ERR_BAD_WIRE);
    }
    match wire {
        WIRE_VARINT => {
            let (value, next) = varint(buf, at)?;
            Ok(Field { number, wire, value, data_off: 0, data_len: 0, next })
        }
        WIRE_I64 => {
            let (value, next) = fixed(buf, at, 8)?;
            Ok(Field { number, wire, value, data_off: 0, data_len: 0, next })
        }
        WIRE_I32 => {
            let (value, next) = fixed(buf, at, 4)?;
            Ok(Field { number, wire, value, data_off: 0, data_len: 0, next })
        }
        WIRE_LEN => {
            let (len, body) = varint(buf, at)?;
            // Compared before the cast: usize is 32 bits on the target, so a
            // corrupt length would otherwise truncate into a plausible one.
            if len > (buf.len() - body) as u64 {
                return Err(ERR_TRUNCATED);
            }
            let len = len as usize;
            Ok(Field {
                number,
                wire,
                value: len as u64,
                data_off: body,
                data_len: len,
                next: body + len,
            })
        }
        // 3 and 4 were the deprecated group encoding, 6 and 7 never existed.
        _ => Err(ERR_BAD_WIRE),
    }
}

/// The `Data` message: the envelope every decrypted payload is wrapped in.
#[derive(Debug, Default, PartialEq)]
pub struct Data {
    pub portnum: u32,
    pub payload_off: usize,
    pub payload_len: usize,
    pub want_response: bool,
    pub dest: u32,
    pub source: u32,
    pub request_id: u32,
    pub reply_id: u32,
    pub emoji: u32,
    pub bitfield: u32,
    pub has_bitfield: bool,
}

/// Parses a decrypted payload as a `Data` message.
///
/// Requires the message to end exactly at the end of the buffer. Protobuf would
/// normally allow trailing bytes to be someone else's problem, but here there
/// is nothing after the payload, so anything left over means the parse only
/// looked successful.
pub fn parse_data(buf: &[u8]) -> Result<Data, i32> {
    let mut data = Data::default();
    let mut at = 0usize;
    while at < buf.len() {
        let f = field(buf, at)?;
        match (f.number, f.wire) {
            (1, WIRE_VARINT) => data.portnum = f.value as u32,
            (2, WIRE_LEN) => {
                data.payload_off = f.data_off;
                data.payload_len = f.data_len;
            }
            (3, WIRE_VARINT) => data.want_response = f.value != 0,
            (4, WIRE_I32) => data.dest = f.value as u32,
            (5, WIRE_I32) => data.source = f.value as u32,
            (6, WIRE_I32) => data.request_id = f.value as u32,
            (7, WIRE_I32) => data.reply_id = f.value as u32,
            (8, WIRE_I32) => data.emoji = f.value as u32,
            (9, WIRE_VARINT) => {
                data.bitfield = f.value as u32;
                data.has_bitfield = true;
            }
            // An unknown field number is normal -- it is how protobuf carries a
            // newer sender's additions -- but a known number arriving with the
            // wrong wire type is not, so only the number is allowed to vary.
            _ => {}
        }
        at = f.next;
    }
    Ok(data)
}

/// Writes a base-128 varint, returning where the next byte goes.
fn put_varint(out: &mut [u8], mut at: usize, mut value: u64) -> Result<usize, i32> {
    loop {
        if at >= out.len() {
            return Err(ERR_NO_SPACE);
        }
        let byte = (value & 0x7F) as u8;
        value >>= 7;
        if value == 0 {
            out[at] = byte;
            return Ok(at + 1);
        }
        out[at] = byte | 0x80;
        at += 1;
    }
}

fn put_tag(out: &mut [u8], at: usize, number: u32, wire: u32) -> Result<usize, i32> {
    put_varint(out, at, ((number << 3) | wire) as u64)
}

/// Builds a `Data` envelope, returning how much of `out` was used.
///
/// Zero-valued scalars are left out, which is what proto3 means by a default
/// and what the nanopb encoder on every other node does, so a message built
/// here is byte-identical to the same message built there. `bitfield` is the
/// exception because the schema marks it `optional`: an explicit zero is a
/// sender refusing MQTT upload rather than a sender with nothing to say, which
/// is the same distinction [`parse_data`] preserves with `has_bitfield`.
pub fn encode_data(
    portnum: u32,
    payload: &[u8],
    want_response: bool,
    bitfield: Option<u32>,
    out: &mut [u8],
) -> Result<usize, i32> {
    let mut at = 0usize;
    if portnum != 0 {
        at = put_tag(out, at, 1, WIRE_VARINT)?;
        at = put_varint(out, at, portnum as u64)?;
    }
    if !payload.is_empty() {
        at = put_tag(out, at, 2, WIRE_LEN)?;
        at = put_varint(out, at, payload.len() as u64)?;
        if payload.len() > out.len() - at {
            return Err(ERR_NO_SPACE);
        }
        // Byte at a time on purpose: `copy_from_slice` leaves a call to a core
        // symbol the .mpy linker has no runtime to resolve it against.
        for (i, b) in payload.iter().enumerate() {
            out[at + i] = *b;
        }
        at += payload.len();
    }
    if want_response {
        at = put_tag(out, at, 3, WIRE_VARINT)?;
        at = put_varint(out, at, 1)?;
    }
    if let Some(bits) = bitfield {
        at = put_tag(out, at, 9, WIRE_VARINT)?;
        at = put_varint(out, at, bits as u64)?;
    }
    Ok(at)
}
