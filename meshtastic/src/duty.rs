//! A transmit budget, as a sliding window of accumulated airtime.
//!
//! The window is a fixed set of buckets rather than a list of transmissions.
//! A list is the obvious shape and was the first version, but it grows with
//! traffic: one entry per packet, held for the length of the window, on a heap
//! that has a few kilobytes free once the phone is connected. Buckets cost the
//! same few hundred bytes whether the node sends once an hour or once a
//! second, and nothing is allocated per packet.
//!
//! What that costs is resolution. Airtime is credited to the bucket the
//! transmission started in, and the window expires in whole buckets, so the
//! interval measured is the requested one rounded up to a bucket. For a duty
//! cycle that is a limit on an hour, a minute of slack is not worth a byte.
//!
//! Time arrives as a bucket index rather than a timestamp: the caller divides,
//! because it holds the clock and knows the bucket width, and because seconds
//! since boot are a float in CircuitPython and floats do not cross this
//! boundary.

use crate::le;
use crate::ERR_NO_SPACE;

/// The last bucket index written, ahead of the bucket array.
pub const HEADER_LEN: usize = 4;

/// Bytes per bucket.
pub const BUCKET_LEN: usize = 4;

fn count(len: usize) -> usize {
    (len - HEADER_LEN) / BUCKET_LEN
}

fn zero_all(window: &mut [u8], n: usize) {
    for k in 0..n {
        le::put_u32(window, HEADER_LEN + k * BUCKET_LEN, 0);
    }
}

/// Clears whatever the move from the last bucket index to `slot` has expired.
///
/// Reading has to do this as well as writing. A node that sends a burst and
/// then goes quiet would otherwise report that burst forever, because nothing
/// would have come along to overwrite it.
fn expire(window: &mut [u8], n: usize, slot: u32) {
    let last = le::u32_at(window, 0);
    if slot == last {
        return;
    }
    if slot < last {
        // Only reachable if the clock went backwards, which on this board
        // means a reboot with the window somehow preserved. Nothing in the
        // buckets can be placed on the new timeline, so none of it is kept.
        zero_all(window, n);
    } else {
        let ahead = (slot - last) as usize;
        if ahead >= n {
            zero_all(window, n);
        } else {
            for k in 1..=ahead {
                let at = (last as usize + k) % n;
                le::put_u32(window, HEADER_LEN + at * BUCKET_LEN, 0);
            }
        }
    }
    le::put_u32(window, 0, slot);
}

/// Adds `us` of airtime to the bucket for `slot`.
pub fn record(window: &mut [u8], slot: u32, us: u32) -> Result<(), i32> {
    if window.len() < HEADER_LEN + BUCKET_LEN {
        return Err(ERR_NO_SPACE);
    }
    let n = count(window.len());
    expire(window, n, slot);
    let at = HEADER_LEN + (slot as usize % n) * BUCKET_LEN;
    // Saturating rather than wrapping: an overflowed budget that reads as
    // nearly empty would hand out permission to transmit, which is the one
    // wrong answer this function can give.
    le::put_u32(window, at, le::u32_at(window, at).saturating_add(us));
    Ok(())
}

/// Total airtime in microseconds still inside the window at `slot`.
pub fn used(window: &mut [u8], slot: u32) -> Result<u32, i32> {
    if window.len() < HEADER_LEN + BUCKET_LEN {
        return Err(ERR_NO_SPACE);
    }
    let n = count(window.len());
    expire(window, n, slot);
    let mut total: u32 = 0;
    for k in 0..n {
        total = total.saturating_add(le::u32_at(window, HEADER_LEN + k * BUCKET_LEN));
    }
    Ok(total)
}
