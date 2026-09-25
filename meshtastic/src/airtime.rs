//! Time on air: what a duty-cycle budget actually spends.
//!
//! This is the LoRa formula from the Semtech datasheets, not a transcription of
//! Meshtastic's `RadioInterface::getPacketTime`, which is declared in one file
//! and defined somewhere this project has not found. The two should agree,
//! because the firmware is computing the same modem's behaviour from the same
//! datasheet, but the distinction is worth keeping in view: everywhere else in
//! this crate a number that has to match the rest of the mesh was copied from
//! the pinned firmware, and this one was derived.
//!
//! Nothing on the wire depends on it either way. Airtime is used to decide
//! whether to transmit, so an error here makes this node a slightly worse
//! neighbour rather than an unintelligible one.
//!
//! Integer microseconds throughout. The formula is usually written with a 4.25
//! symbol preamble constant, so the arithmetic is carried in quarter-symbols
//! and divided once at the end rather than rounding at every step.

use crate::ERR_BAD_MODEM;

/// Meshtastic's fixed preamble length, from `RadioInterface::PREAMBLE_LEN`.
pub const PREAMBLE_SYMBOLS: u32 = 16;

/// Microseconds to send `payload_len` bytes, header included.
///
/// `payload_len` is the whole PHY payload -- the 16-byte Meshtastic header plus
/// the ciphertext -- because that is what the modem is given.
pub fn time_on_air_us(payload_len: usize, sf: u8, bw_hz: u32, cr: u8) -> Result<u32, i32> {
    if !(5..=12).contains(&sf) || bw_hz == 0 || !(5..=8).contains(&cr) {
        return Err(ERR_BAD_MODEM);
    }
    let sf64 = sf as i64;
    let symbol_us = ((1u64 << sf) * 1_000_000) / bw_hz as u64;
    // Low data rate optimisation is switched on above a 16 ms symbol, and pays
    // for itself by giving up two of the bits the block count divides by.
    let de: i64 = if symbol_us > 16_000 { 1 } else { 0 };

    // 16 for the CRC Meshtastic leaves enabled; the explicit-header term is
    // zero because it is only subtracted when the header is implicit.
    let bits = 8 * payload_len as i64 - 4 * sf64 + 28 + 16;
    let per_block = 4 * (sf64 - 2 * de);
    let blocks = if bits <= 0 { 0 } else { (bits + per_block - 1) / per_block };
    // The datasheet's (CR + 4) with CR = 1..4, which is our denominator 5..8.
    let payload_symbols = 8 + blocks * cr as i64;

    // 4.25 symbols of preamble, in quarters: 17.
    let quarters = 4 * PREAMBLE_SYMBOLS as i64 + 17 + 4 * payload_symbols;
    let us = (quarters as u64 * (1u64 << sf) * 1_000_000) / (4 * bw_hz as u64);
    Ok(us as u32)
}
