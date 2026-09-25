//! The message store behind `inbox.Inbox`: text packed into one arena.
//!
//! A message is a 23-byte head followed by its UTF-8 bytes, laid against the
//! one before it, oldest first. There is no free list and nothing is ever
//! written into the middle: records join at the end and leave from the front.
//!
//! The arena is the reason this is worth having in Rust. Held as Python objects
//! a message was a dozen attributes and a string that lived as long as the
//! message did, so a busy channel left the heap dotted with small permanent
//! allocations at exactly the moments the BLE queue wanted two contiguous
//! kilobytes. Here the store is one buffer taken at boot, and the objects the
//! REPL prints are built on demand and collected immediately.
//!
//! Two bounds, whichever bites first: a count, and the arena. The second is the
//! one worth stating, because it is bytes that were actually taken rather than
//! a worst case that depends on how much strangers type.

use crate::le::{put_u16, put_u32, u16_at, u32_at};
use crate::{ERR_BAD_OFFSET, ERR_NO_SPACE, ERR_NULL, OK};

/// The head, before the text.
pub const FIXED: usize = 23;

/// Bytes of text kept per message. The wire allows more in a payload and the
/// text is written by a stranger, so the store sets its own limit.
pub const MAX_TEXT: usize = 200;

/// The only per-message state that is ours rather than the sender's.
pub const READ: u8 = 0x01;

pub const NO_HOPS: u8 = 255;
pub const NO_SNR: i8 = -128;
pub const NO_RSSI: i16 = -32768;

/// Which conversation a walk is about. `DIRECT` also needs the far node.
pub const ALL: i32 = 0;
pub const CHANNEL: i32 = 1;
pub const DIRECT: i32 = 2;

const AT_ID: usize = 0;
const AT_FROM: usize = 4;
const AT_TO: usize = 8;
const AT_TIME: usize = 12;
const AT_RSSI: usize = 16;
const AT_CHANNEL: usize = 18;
const AT_HOPS: usize = 19;
const AT_SNR: usize = 20;
const AT_FLAGS: usize = 21;
const AT_TEXT: usize = 22;

/// What a caller hands over to store one message. Pre-scaled: SNR in quarter
/// decibels and RSSI in whole ones, because nothing crosses this boundary as a
/// float.
#[repr(C)]
pub struct MessageIn {
    pub id: u32,
    pub from: u32,
    pub to: u32,
    pub time: u32,
    pub rssi: i16,
    pub channel: u8,
    pub hops: u8,
    pub snr: i8,
    pub flags: u8,
}

#[repr(C)]
pub struct AppendOut {
    pub used: i32,
    pub dropped: i32,
    pub off: i32,
}

#[repr(C)]
pub struct MessageOut {
    pub id: u32,
    pub from: u32,
    pub to: u32,
    pub time: u32,
    pub rssi: i32,
    pub channel: i32,
    pub hops: i32,
    pub snr: i32,
    pub flags: i32,
    pub text_off: i32,
    pub text_len: i32,
}

/// Enough to group a message without reading it.
#[repr(C)]
pub struct BriefOut {
    pub peer: u32,
    pub direct: i32,
    pub read: i32,
}

/// Bytes one record takes, head and text together.
pub fn length(buf: &[u8], off: usize) -> usize {
    FIXED + buf[off + AT_TEXT] as usize
}

/// The longest prefix of `text` that fits, never splitting a UTF-8 sequence.
///
/// The caller counts characters and this counts bytes, so a message of emoji
/// is cut where one of them ends. Cutting mid-sequence would store bytes that
/// no longer decode, and the store would then be the thing that corrupted them.
pub fn fit(text: &[u8]) -> usize {
    if text.len() <= MAX_TEXT {
        return text.len();
    }
    let mut n = MAX_TEXT;
    // Continuation bytes are 0b10xxxxxx; back up to the start of a sequence.
    while n > 0 && text[n] & 0xC0 == 0x80 {
        n -= 1;
    }
    n
}

fn selected(buf: &[u8], off: usize, node_num: u32, want: i32, peer: u32) -> bool {
    // `direct` and the conversation a message belongs to are this node's view
    // of it rather than anything the sender said, so they are worked out on
    // every walk instead of being stored. A board that was renumbered while it
    // was off then files what it kept the way it files what arrives next.
    let direct = u32_at(buf, off + AT_TO) == node_num;
    if want == CHANNEL {
        !direct
    } else if want == DIRECT {
        direct && u32_at(buf, off + AT_FROM) == peer
    } else {
        true
    }
}

/// The first selected record at or after the one at `after`, or -1.
///
/// `after` is the previous match rather than a place to resume, so a caller
/// never has to know how long a record was; a negative starts from the front.
pub fn next(buf: &[u8], used: usize, after: i32, node_num: u32, want: i32, peer: u32) -> i32 {
    let mut off = 0;
    if after >= 0 {
        let at = after as usize;
        if at + FIXED > used {
            return -1;
        }
        off = at + length(buf, at);
    }
    while off + FIXED <= used {
        if selected(buf, off, node_num, want, peer) {
            return off as i32;
        }
        off += length(buf, off);
    }
    -1
}

/// How many selected records there are, or how many of them are unread.
pub fn count(buf: &[u8], used: usize, node_num: u32, want: i32, peer: u32, unread: bool) -> i32 {
    let mut off = 0;
    let mut found = 0;
    while off + FIXED <= used {
        if selected(buf, off, node_num, want, peer)
            && (!unread || buf[off + AT_FLAGS] & READ == 0)
        {
            found += 1;
        }
        off += length(buf, off);
    }
    found
}

/// Marks every selected record read. Returns how many actually changed, which
/// is what says whether the change is worth an erase cycle.
pub fn mark(buf: &mut [u8], used: usize, node_num: u32, want: i32, peer: u32) -> i32 {
    let mut off = 0;
    let mut changed = 0;
    while off + FIXED <= used {
        if selected(buf, off, node_num, want, peer) && buf[off + AT_FLAGS] & READ == 0 {
            buf[off + AT_FLAGS] |= READ;
            changed += 1;
        }
        off += length(buf, off);
    }
    changed
}

/// Stores one message, dropping the oldest until it fits.
///
/// `limit` of zero means the arena is the only bound.
pub fn append(
    buf: &mut [u8],
    used: usize,
    limit: i32,
    m: &MessageIn,
    text: &[u8],
    out: &mut AppendOut,
) -> i32 {
    let take = fit(text);
    let need = FIXED + take;
    if need > buf.len() {
        return ERR_NO_SPACE;
    }
    let mut at = used;
    let mut held = count(buf, at, 0, ALL, 0, false);
    let mut dropped = 0;
    while at > 0 && (at + need > buf.len() || (limit > 0 && held >= limit)) {
        let first = length(buf, 0);
        // Forward, so the overlap is harmless, and byte at a time because the
        // slice moves in `core` are out-of-line generics the natmod linker
        // does not keep.
        for i in 0..(at - first) {
            buf[i] = buf[i + first];
        }
        at -= first;
        held -= 1;
        dropped += 1;
    }

    let off = at;
    put_u32(buf, off + AT_ID, m.id);
    put_u32(buf, off + AT_FROM, m.from);
    put_u32(buf, off + AT_TO, m.to);
    put_u32(buf, off + AT_TIME, m.time);
    put_u16(buf, off + AT_RSSI, m.rssi as u16);
    buf[off + AT_CHANNEL] = m.channel;
    buf[off + AT_HOPS] = m.hops;
    buf[off + AT_SNR] = m.snr as u8;
    buf[off + AT_FLAGS] = m.flags;
    buf[off + AT_TEXT] = take as u8;
    for i in 0..take {
        buf[off + FIXED + i] = text[i];
    }

    out.used = (off + need) as i32;
    out.dropped = dropped;
    out.off = off as i32;
    OK
}

/// Everything about one record. The text is left where it is: `text_off` is an
/// offset into the arena the caller already holds.
pub fn read(buf: &[u8], used: usize, off: usize, out: &mut MessageOut) -> i32 {
    if off + FIXED > used {
        return ERR_BAD_OFFSET;
    }
    let take = buf[off + AT_TEXT] as usize;
    if off + FIXED + take > used {
        return ERR_BAD_OFFSET;
    }
    out.id = u32_at(buf, off + AT_ID);
    out.from = u32_at(buf, off + AT_FROM);
    out.to = u32_at(buf, off + AT_TO);
    out.time = u32_at(buf, off + AT_TIME);
    out.rssi = u16_at(buf, off + AT_RSSI) as i16 as i32;
    out.channel = buf[off + AT_CHANNEL] as i32;
    out.hops = buf[off + AT_HOPS] as i32;
    out.snr = buf[off + AT_SNR] as i8 as i32;
    out.flags = buf[off + AT_FLAGS] as i32;
    out.text_off = (off + FIXED) as i32;
    out.text_len = take as i32;
    OK
}

/// Which conversation a record is in, and whether it has been read.
pub fn brief(buf: &[u8], used: usize, off: usize, node_num: u32, out: &mut BriefOut) -> i32 {
    if off + FIXED > used {
        return ERR_BAD_OFFSET;
    }
    let direct = u32_at(buf, off + AT_TO) == node_num;
    out.peer = u32_at(buf, off + AT_FROM);
    out.direct = direct as i32;
    out.read = (buf[off + AT_FLAGS] & READ != 0) as i32;
    OK
}

// ------------------------------------------------------------------- C ABI

/// # Safety
/// `buf` must point to `len` writable bytes, `text` to `text_len` readable
/// ones, and `m` and `out` must be valid.
#[no_mangle]
#[allow(clippy::too_many_arguments)]
pub unsafe extern "C" fn meshtastic_inbox_append(
    buf: *mut u8,
    len: usize,
    used: usize,
    limit: i32,
    m: *const MessageIn,
    text: *const u8,
    text_len: usize,
    out: *mut AppendOut,
) -> i32 {
    if buf.is_null() || m.is_null() || out.is_null() || used > len {
        return ERR_NULL;
    }
    let body = if text.is_null() {
        &[][..]
    } else {
        core::slice::from_raw_parts(text, text_len)
    };
    let mut got = AppendOut {
        used: 0,
        dropped: 0,
        off: 0,
    };
    let rc = append(
        core::slice::from_raw_parts_mut(buf, len),
        used,
        limit,
        &*m,
        body,
        &mut got,
    );
    if rc == OK {
        (*out).used = got.used;
        (*out).dropped = got.dropped;
        (*out).off = got.off;
    }
    rc
}

/// # Safety
/// `buf` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_inbox_next(
    buf: *const u8,
    len: usize,
    used: usize,
    after: i32,
    node_num: u32,
    want: i32,
    peer: u32,
) -> i32 {
    if buf.is_null() || used > len {
        return ERR_NULL;
    }
    next(
        core::slice::from_raw_parts(buf, len),
        used,
        after,
        node_num,
        want,
        peer,
    )
}

/// # Safety
/// `buf` must point to `len` readable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_inbox_count(
    buf: *const u8,
    len: usize,
    used: usize,
    node_num: u32,
    want: i32,
    peer: u32,
    unread: i32,
) -> i32 {
    if buf.is_null() || used > len {
        return ERR_NULL;
    }
    count(
        core::slice::from_raw_parts(buf, len),
        used,
        node_num,
        want,
        peer,
        unread != 0,
    )
}

/// # Safety
/// `buf` must point to `len` writable bytes.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_inbox_mark(
    buf: *mut u8,
    len: usize,
    used: usize,
    node_num: u32,
    want: i32,
    peer: u32,
) -> i32 {
    if buf.is_null() || used > len {
        return ERR_NULL;
    }
    mark(
        core::slice::from_raw_parts_mut(buf, len),
        used,
        node_num,
        want,
        peer,
    )
}

/// # Safety
/// `buf` must point to `len` readable bytes and `out` must be writable.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_inbox_read(
    buf: *const u8,
    len: usize,
    used: usize,
    off: usize,
    out: *mut MessageOut,
) -> i32 {
    if buf.is_null() || out.is_null() || used > len {
        return ERR_NULL;
    }
    // Written straight through the pointer rather than into a local that is
    // then assigned: eleven words is far past the size the compiler copies
    // inline, and the memcpy it reaches for instead is a symbol the natmod
    // linker has nowhere to get.
    read(core::slice::from_raw_parts(buf, len), used, off, &mut *out)
}

/// # Safety
/// `buf` must point to `len` readable bytes and `out` must be writable.
#[no_mangle]
pub unsafe extern "C" fn meshtastic_inbox_brief(
    buf: *const u8,
    len: usize,
    used: usize,
    off: usize,
    node_num: u32,
    out: *mut BriefOut,
) -> i32 {
    if buf.is_null() || out.is_null() || used > len {
        return ERR_NULL;
    }
    brief(
        core::slice::from_raw_parts(buf, len),
        used,
        off,
        node_num,
        &mut *out,
    )
}
