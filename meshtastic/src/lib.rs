//! Meshtastic wire-format decoding, built to be linked into a CircuitPython
//! native module.
//!
//! Scope is the frame structure: the 16-byte header, the channel hash, the
//! region and modem-preset tables needed to tune the radio to the right
//! frequency slot, and a protobuf reader for the `Data` envelope that sits
//! inside an encrypted payload. Decryption itself stays in Python, where
//! CircuitPython's `aesio` already provides AES-CTR.
//!
//! Everything is transcribed from Meshtastic firmware **v2.7.26.54e0d8d**
//! (commit `54e0d8d`). The header layout and the slot arithmetic are wire
//! compatibility, not preference: they have to match whatever the rest of the
//! mesh is running, so the version is pinned and recorded rather than tracked.
//!
//! All frequencies are integer hertz. The firmware uses megahertz floats, but
//! float maths here would pull in soft-float helpers the `.mpy` linker cannot
//! resolve, and hertz are exact for every value in the region table anyway.

#![cfg_attr(not(test), no_std)]

pub mod airtime;
pub mod cache;
pub mod duty;
pub mod ffi;
pub mod hash;
pub mod header;
pub mod inbox;
pub mod le;
pub mod nmea;
pub mod payload;
pub mod pbuf;
pub mod power;
pub mod preset;
pub mod proto;
pub mod region;
pub mod roster;
pub mod store;
pub mod stream;

#[cfg(test)]
mod tests;

pub const OK: i32 = 0;
/// Frame shorter than a header.
pub const ERR_SHORT_FRAME: i32 = -1;
pub const ERR_NULL: i32 = -2;
pub const ERR_UNKNOWN_REGION: i32 = -3;
pub const ERR_UNKNOWN_PRESET: i32 = -4;
/// Slot index at or beyond the channel count for this region and bandwidth.
pub const ERR_BAD_SLOT: i32 = -5;
pub const ERR_BAD_BANDWIDTH: i32 = -6;
/// Ran off the end of the buffer mid-field.
pub const ERR_TRUNCATED: i32 = -7;
/// Not protobuf: a wire type that does not exist, or field number zero.
pub const ERR_BAD_WIRE: i32 = -8;
/// Output buffer too small for what was being written.
pub const ERR_NO_SPACE: i32 = -9;
/// Spreading factor, bandwidth or coding rate outside what LoRa defines.
pub const ERR_BAD_MODEM: i32 = -10;
/// Roster offset that is past the end of the table or not on a row boundary.
pub const ERR_BAD_ROW: i32 = -11;
/// Inbox offset that is past the end of the arena or not on a record boundary.
pub const ERR_BAD_OFFSET: i32 = -12;
/// A GNSS state buffer shorter than `nmea::STATE_BYTES`, or a talker index
/// past the end of its table.
pub const ERR_BAD_STATE: i32 = -13;
/// An NMEA line that is not a sentence, or whose checksum does not match.
pub const ERR_BAD_SENTENCE: i32 = -14;
/// Asked to encode a position the receiver has not actually established.
pub const ERR_NO_FIX: i32 = -15;

/// Required to link the `no_std` staticlib.
#[cfg(not(test))]
#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    loop {}
}
