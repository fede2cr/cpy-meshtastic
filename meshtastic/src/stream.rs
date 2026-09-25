//! The Meshtastic stream framing, as used over TCP and serial.
//!
//! A `ToRadio` or `FromRadio` is protobuf, which has no framing of its own, so
//! each one is prefixed with `0x94 0xc3` and a big-endian length. Over GATT the
//! transport delivered whole messages and none of this was needed; over a
//! stream, writes split and run together, and the header is the only thing that
//! says where one message ends.
//!
//! The reader resynchronises rather than failing: the two start bytes were
//! chosen so they cannot appear in 7-bit ASCII, which is what lets a device
//! print debug text on the same wire. Anything before a valid header is
//! counted and thrown away.
//!
//! The state lives in a buffer the caller owns and nothing here allocates.

use crate::le;
use crate::ERR_BAD_STATE;

pub const START1: u8 = 0x94;
pub const START2: u8 = 0xc3;

/// The client refuses anything longer, so a length above this is proof the
/// header was noise that happened to look right.
pub const MAX_FRAME: usize = 512;

pub const HEADER: usize = 4;

const OFF_STAGE: usize = 0;
const OFF_WANT: usize = 2;
const OFF_HAVE: usize = 4;
const OFF_LOST: usize = 8;
const OFF_FRAMES: usize = 12;

/// Where the frame in progress is assembled. Public because the Python side
/// reads the finished frame straight out of the state rather than being handed
/// a copy: half a kilobyte per message, on every message, is the alternative.
pub const OFF_BODY: usize = 16;

pub const STATE_BYTES: usize = OFF_BODY + MAX_FRAME;

/// Hunting for the first start byte.
const HUNT: u8 = 0;
/// `START1` seen, and the next byte either confirms it or does not.
const SAW_START1: u8 = 1;
const WANT_MSB: u8 = 2;
const WANT_LSB: u8 = 3;
const BODY: u8 = 4;

/// What the reader has seen, for a caller that wants to say why a link is bad.
pub struct Counts {
    /// Bytes discarded outside a frame. Non-zero on a healthy link only at the
    /// very start, when a client connects mid-sentence.
    pub lost: u32,
    pub frames: u32,
    /// Bytes of the frame in progress, so a stalled sender is visible.
    pub partial: u16,
}

fn sized(state: &[u8]) -> bool {
    state.len() >= STATE_BYTES
}

pub fn reset(state: &mut [u8]) -> i32 {
    if !sized(state) {
        return ERR_BAD_STATE;
    }
    // The header fields only. The body is never read past `OFF_HAVE` bytes, so
    // clearing half a kilobyte here would cost a `memclr` for nothing.
    for i in 0..OFF_BODY {
        state[i] = 0;
    }
    0
}

pub fn counts(state: &[u8]) -> Result<Counts, i32> {
    if !sized(state) {
        return Err(ERR_BAD_STATE);
    }
    Ok(Counts {
        lost: le::u32_at(state, OFF_LOST),
        frames: le::u32_at(state, OFF_FRAMES),
        partial: if state[OFF_STAGE] == BODY {
            le::u16_at(state, OFF_HAVE)
        } else {
            0
        },
    })
}

/// Reads `chunk` from `at` until one frame is complete or the chunk runs out.
///
/// Returns how far into `chunk` it got and the length of the finished frame, or
/// zero when there is not one yet. The frame is left in the state rather than
/// copied out: the caller reads it with `body`, hands it to the protocol, and
/// calls again from the returned offset.
pub fn feed(state: &mut [u8], chunk: &[u8], at: usize) -> (i32, usize) {
    if !sized(state) {
        return (ERR_BAD_STATE, at);
    }
    let mut i = if at > chunk.len() { chunk.len() } else { at };
    while i < chunk.len() {
        let c = chunk[i];
        i += 1;
        match state[OFF_STAGE] {
            HUNT => {
                if c == START1 {
                    state[OFF_STAGE] = SAW_START1;
                } else {
                    lost(state, 1);
                }
            }
            SAW_START1 => {
                if c == START2 {
                    state[OFF_STAGE] = WANT_MSB;
                } else if c == START1 {
                    // Still a candidate, and the one before it was not. Two
                    // start bytes in a row is what a truncated frame followed
                    // by a real one looks like.
                    lost(state, 1);
                } else {
                    state[OFF_STAGE] = HUNT;
                    lost(state, 2);
                }
            }
            WANT_MSB => {
                le::put_u16(state, OFF_WANT, (c as u16) << 8);
                state[OFF_STAGE] = WANT_LSB;
            }
            WANT_LSB => {
                let want = le::u16_at(state, OFF_WANT) | (c as u16);
                if want as usize > MAX_FRAME {
                    // Past what any client will send, so those four bytes were
                    // noise. Back to hunting from here rather than from the
                    // header: re-reading it would find the same false start.
                    state[OFF_STAGE] = HUNT;
                    lost(state, 4);
                    continue;
                }
                le::put_u16(state, OFF_WANT, want);
                le::put_u16(state, OFF_HAVE, 0);
                if want == 0 {
                    // Legal, and says nothing. Swallowed here rather than
                    // returned, because a zero-length frame and "no frame yet"
                    // are the same answer and the caller would stop early.
                    state[OFF_STAGE] = HUNT;
                    continue;
                }
                state[OFF_STAGE] = BODY;
            }
            _ => {
                let have = le::u16_at(state, OFF_HAVE) as usize;
                state[OFF_BODY + have] = c;
                let have = have + 1;
                le::put_u16(state, OFF_HAVE, have as u16);
                if have >= le::u16_at(state, OFF_WANT) as usize {
                    state[OFF_STAGE] = HUNT;
                    bump(state, OFF_FRAMES);
                    return (have as i32, i);
                }
            }
        }
    }
    (0, i)
}

/// The frame `feed` just finished. Valid until the next call.
pub fn body(state: &[u8], len: usize) -> Result<&[u8], i32> {
    if !sized(state) || len > MAX_FRAME {
        return Err(ERR_BAD_STATE);
    }
    Ok(&state[OFF_BODY..OFF_BODY + len])
}

/// Writes `payload` into `out` at `at` with its header. How many bytes that
/// took, or `ERR_NO_SPACE`.
pub fn frame(out: &mut [u8], at: usize, payload: &[u8]) -> i32 {
    if payload.len() > MAX_FRAME {
        return crate::ERR_NO_SPACE;
    }
    let total = HEADER + payload.len();
    if at > out.len() || out.len() - at < total {
        return crate::ERR_NO_SPACE;
    }
    out[at] = START1;
    out[at + 1] = START2;
    out[at + 2] = (payload.len() >> 8) as u8;
    out[at + 3] = payload.len() as u8;
    for i in 0..payload.len() {
        out[at + HEADER + i] = payload[i];
    }
    total as i32
}

fn lost(state: &mut [u8], n: u32) {
    let was = le::u32_at(state, OFF_LOST);
    le::put_u32(state, OFF_LOST, was.wrapping_add(n));
}

fn bump(state: &mut [u8], at: usize) {
    let was = le::u32_at(state, at);
    le::put_u32(state, at, was.wrapping_add(1));
}
