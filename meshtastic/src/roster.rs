//! The packed table behind `nodedb.NodeDB`: who has been heard, and how well.
//!
//! One row per peer, at fixed offsets, in a buffer the caller owns and sizes
//! once. That shape is the reason this is worth having in Rust at all. A row
//! is written on every packet that arrives, and read whenever a roster is
//! printed or saved, and doing it in Python meant a `struct.unpack_from` call
//! and a throwaway tuple for every individual field. Here a row is a slice.
//!
//! Names are deliberately not here. They are rare, they vary in length, and
//! most rows never get one, so they stay in Python dictionaries keyed by node
//! number where a missing one costs nothing. What this module owns is only
//! what is the same width for every peer.
//!
//! The layout is `<IiiIHBBBbh` and must stay byte compatible with it: the same
//! bytes are what `meshcache` writes to flash, and a board that reboots into a
//! new build has to be able to read back what the old one saved.

use crate::le::{put_u16, put_u32, u16_at, u32_at};
use crate::{ERR_BAD_ROW, ERR_NULL, OK};

/// Bytes per row. Twenty-four, and the row is its own alignment.
pub const ROW_BYTES: usize = 24;

const AT_NUM: usize = 0;
const AT_FIRST: usize = 4;
const AT_LAST: usize = 8;
const AT_SEEN: usize = 12;
const AT_COUNT: usize = 16;
const AT_HOPS: usize = 18;
const AT_HW: usize = 19;
const AT_ROLE: usize = 20;
const AT_SNR: usize = 21;
const AT_RSSI: usize = 22;

/// Absent, in the byte holding `snr * 4`. Leaves about +-31 dB, wider than any
/// receiver reports.
pub const SNR_UNKNOWN: i8 = -128;

/// What the caller passes for "no reading", in the wider types the arguments
/// arrive in. A real quarter-dB SNR never reaches these.
pub const NO_SNR: i32 = i32::MIN;
pub const NO_HOPS: i32 = -1;

/// One row, unpacked. Sentinels are kept rather than resolved: turning them
/// into `None` is the caller's business and only the caller has a `None`.
#[repr(C)]
pub struct RowOut {
    pub num: u32,
    pub first: i32,
    pub last: i32,
    pub seen: u32,
    pub count: u16,
    pub hops: u8,
    pub hw: u8,
    pub role: u8,
    pub snr: i8,
    pub rssi: i16,
}

/// Hops as a byte, with zero for "no idea", which is not the same as zero hops.
pub fn pack_hops(hops: i32) -> u8 {
    if hops < 0 {
        0
    } else if hops >= 254 {
        255
    } else {
        (hops + 1) as u8
    }
}

/// Quarter-decibel SNR, clamped inside the sentinel rather than onto it.
pub fn pack_snr(quarters: i32) -> i8 {
    if quarters == NO_SNR {
        SNR_UNKNOWN
    } else if quarters <= SNR_UNKNOWN as i32 {
        SNR_UNKNOWN + 1
    } else if quarters > 127 {
        127
    } else {
        quarters as i8
    }
}

/// Whole dBm. Zero is the absence: no receiver reports it as a reading.
pub fn pack_rssi(dbm: i32) -> i16 {
    if dbm <= -32768 {
        -32768
    } else if dbm > 0 {
        0
    } else {
        dbm as i16
    }
}

/// How many whole rows fit. A trailing partial row is not one.
pub fn rows(table: &[u8]) -> usize {
    table.len() / ROW_BYTES
}

fn row(table: &[u8], at: usize) -> &[u8] {
    &table[at..at + ROW_BYTES]
}

/// Byte offset of the row holding `num`, or `None`.
///
/// Linear, and that is not an oversight. The table holds thirty-two rows and
/// the comparison is four bytes, so a scan is a few hundred cycles; a hash
/// would need a second structure to keep in step with eviction and with what
/// the cache writes to flash.
pub fn slot(table: &[u8], num: u32) -> Option<usize> {
    if num == 0 {
        return None;
    }
    for i in 0..rows(table) {
        let at = i * ROW_BYTES;
        if u32_at(table, at + AT_NUM) == num {
            return Some(at);
        }
    }
    None
}

/// Byte offset of the first unused row, or `None` when the table is full.
pub fn free(table: &[u8]) -> Option<usize> {
    for i in 0..rows(table) {
        let at = i * ROW_BYTES;
        if u32_at(table, at + AT_NUM) == 0 {
            return Some(at);
        }
    }
    None
}

/// Byte offset of the occupied row heard longest ago, or `None` when empty.
///
/// Ordered on `seen`, a counter, rather than on `last`, a clock: `last` is for
/// showing a human and rounds to the second, so a burst of packets would tie
/// and the choice would fall to whichever row came first in the table.
pub fn oldest(table: &[u8]) -> Option<usize> {
    let mut best: Option<(u32, usize)> = None;
    for i in 0..rows(table) {
        let at = i * ROW_BYTES;
        if u32_at(table, at + AT_NUM) == 0 {
            continue;
        }
        let seen = u32_at(table, at + AT_SEEN);
        if best.is_none() || seen < best.unwrap().0 {
            best = Some((seen, at));
        }
    }
    best.map(|(_, at)| at)
}

/// How many rows are occupied.
pub fn used(table: &[u8]) -> usize {
    let mut n = 0;
    for i in 0..rows(table) {
        if u32_at(table, i * ROW_BYTES + AT_NUM) != 0 {
            n += 1;
        }
    }
    n
}

pub fn read(table: &[u8], at: usize) -> RowOut {
    let r = row(table, at);
    RowOut {
        num: u32_at(r, AT_NUM),
        first: u32_at(r, AT_FIRST) as i32,
        last: u32_at(r, AT_LAST) as i32,
        seen: u32_at(r, AT_SEEN),
        count: u16_at(r, AT_COUNT),
        hops: r[AT_HOPS],
        hw: r[AT_HW],
        role: r[AT_ROLE],
        snr: r[AT_SNR] as i8,
        rssi: u16_at(r, AT_RSSI) as i16,
    }
}

/// Records one sighting. Returns true if the row was free and is now this
/// node's, which is what tells the caller a peer is new.
///
/// The counter saturates rather than wrapping: a peer seen 65535 times is
/// already "constantly", and rolling back to zero would read as a stranger.
pub fn touch(
    table: &mut [u8],
    at: usize,
    num: u32,
    now: i32,
    tick: u32,
    hops: i32,
    snr_q: i32,
    rssi: i32,
) -> bool {
    let r = &mut table[at..at + ROW_BYTES];
    let fresh = u32_at(r, AT_NUM) == 0;
    if fresh {
        put_u32(r, AT_NUM, num);
        put_u32(r, AT_FIRST, now as u32);
    }
    put_u32(r, AT_LAST, now as u32);
    put_u32(r, AT_SEEN, tick);
    let count = u16_at(r, AT_COUNT);
    if count < 0xFFFF {
        put_u16(r, AT_COUNT, count + 1);
    }
    r[AT_HOPS] = pack_hops(hops);
    r[AT_SNR] = pack_snr(snr_q) as u8;
    put_u16(r, AT_RSSI, pack_rssi(rssi) as u16);
    fresh
}

/// Puts a saved peer back, with its counters as they were rather than as a
/// sighting would leave them.
#[allow(clippy::too_many_arguments)]
pub fn restore(
    table: &mut [u8],
    at: usize,
    num: u32,
    when: i32,
    tick: u32,
    count: u32,
    hops: i32,
    hw: u32,
    role: u32,
    snr_q: i32,
) {
    let r = &mut table[at..at + ROW_BYTES];
    put_u32(r, AT_NUM, num);
    put_u32(r, AT_FIRST, when as u32);
    put_u32(r, AT_LAST, when as u32);
    put_u32(r, AT_SEEN, tick);
    put_u16(r, AT_COUNT, if count > 0xFFFF { 0xFFFF } else { count as u16 });
    r[AT_HOPS] = pack_hops(hops);
    r[AT_HW] = hw as u8;
    r[AT_ROLE] = role as u8;
    r[AT_SNR] = pack_snr(snr_q) as u8;
    put_u16(r, AT_RSSI, 0);
}

/// What a `User` message claims about itself: hardware model and role, both
/// enums and so both bounded by a byte whatever the sender wrote.
pub fn identify(table: &mut [u8], at: usize, hw: u32, role: u32) {
    table[at + AT_HW] = hw as u8;
    table[at + AT_ROLE] = role as u8;
}

/// Frees a row. Zero in `num` is what marks it free, but the whole row goes:
/// leaving stale counters behind would surface on the next peer to land here.
pub fn clear(table: &mut [u8], at: usize) {
    for b in table[at..at + ROW_BYTES].iter_mut() {
        *b = 0;
    }
}

fn bounds(table: &[u8], at: usize) -> i32 {
    if at + ROW_BYTES > table.len() || at % ROW_BYTES != 0 {
        ERR_BAD_ROW
    } else {
        OK
    }
}

// ------------------------------------------------------------------- C ABI

/// # Safety
/// `table` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_roster_slot(
    table: *const u8,
    len: usize,
    num: u32,
) -> i32 {
    if table.is_null() {
        return ERR_NULL;
    }
    let table = core::slice::from_raw_parts(table, len);
    slot(table, num).map_or(-1, |at| at as i32)
}

/// # Safety
/// `table` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_roster_free(table: *const u8, len: usize) -> i32 {
    if table.is_null() {
        return ERR_NULL;
    }
    free(core::slice::from_raw_parts(table, len)).map_or(-1, |at| at as i32)
}

/// # Safety
/// `table` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_roster_oldest(table: *const u8, len: usize) -> i32 {
    if table.is_null() {
        return ERR_NULL;
    }
    oldest(core::slice::from_raw_parts(table, len)).map_or(-1, |at| at as i32)
}

/// # Safety
/// `table` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_roster_used(table: *const u8, len: usize) -> i32 {
    if table.is_null() {
        return ERR_NULL;
    }
    used(core::slice::from_raw_parts(table, len)) as i32
}

/// # Safety
/// `table` must point to `len` readable bytes and `out` must be writable.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_roster_read(
    table: *const u8,
    len: usize,
    at: usize,
    out: *mut RowOut,
) -> i32 {
    if table.is_null() || out.is_null() {
        return ERR_NULL;
    }
    let table = core::slice::from_raw_parts(table, len);
    let rc = bounds(table, at);
    if rc != OK {
        return rc;
    }
    *out = read(table, at);
    OK
}

/// # Safety
/// `table` must point to `len` writable bytes.
#[no_mangle]
#[allow(clippy::too_many_arguments)]
pub unsafe extern "C" fn meshtastic_roster_touch(
    table: *mut u8,
    len: usize,
    at: usize,
    num: u32,
    now: i32,
    tick: u32,
    hops: i32,
    snr_q: i32,
    rssi: i32,
) -> i32 {
    if table.is_null() {
        return ERR_NULL;
    }
    let table = core::slice::from_raw_parts_mut(table, len);
    let rc = bounds(table, at);
    if rc != OK {
        return rc;
    }
    touch(table, at, num, now, tick, hops, snr_q, rssi) as i32
}

/// # Safety
/// `table` must point to `len` writable bytes.
#[no_mangle]
#[allow(clippy::too_many_arguments)]
pub unsafe extern "C" fn meshtastic_roster_restore(
    table: *mut u8,
    len: usize,
    at: usize,
    num: u32,
    when: i32,
    tick: u32,
    count: u32,
    hops: i32,
    hw: u32,
    role: u32,
    snr_q: i32,
) -> i32 {
    if table.is_null() {
        return ERR_NULL;
    }
    let table = core::slice::from_raw_parts_mut(table, len);
    let rc = bounds(table, at);
    if rc != OK {
        return rc;
    }
    restore(table, at, num, when, tick, count, hops, hw, role, snr_q);
    OK
}

/// # Safety
/// `table` must point to `len` writable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_roster_identify(
    table: *mut u8,
    len: usize,
    at: usize,
    hw: u32,
    role: u32,
) -> i32 {
    if table.is_null() {
        return ERR_NULL;
    }
    let table = core::slice::from_raw_parts_mut(table, len);
    let rc = bounds(table, at);
    if rc != OK {
        return rc;
    }
    identify(table, at, hw, role);
    OK
}

/// # Safety
/// `table` must point to `len` writable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_roster_clear(
    table: *mut u8,
    len: usize,
    at: usize,
) -> i32 {
    if table.is_null() {
        return ERR_NULL;
    }
    let table = core::slice::from_raw_parts_mut(table, len);
    let rc = bounds(table, at);
    if rc != OK {
        return rc;
    }
    clear(table, at);
    OK
}
