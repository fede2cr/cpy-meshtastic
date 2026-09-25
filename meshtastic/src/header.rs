//! The 16-byte cleartext header that precedes every Meshtastic payload.

use crate::{ERR_SHORT_FRAME, OK};

/// `MESHTASTIC_HEADER_LENGTH`.
pub const HEADER_LEN: usize = 16;
/// `MESHTASTIC_PKC_OVERHEAD`: extra bytes on a PKI-encrypted payload.
pub const PKC_OVERHEAD: usize = 12;

pub const FLAGS_HOP_LIMIT_MASK: u8 = 0x07;
pub const FLAGS_WANT_ACK_MASK: u8 = 0x08;
pub const FLAGS_VIA_MQTT_MASK: u8 = 0x10;
pub const FLAGS_HOP_START_MASK: u8 = 0xE0;
pub const FLAGS_HOP_START_SHIFT: u32 = 5;

/// `to` address meaning "everyone".
pub const BROADCAST_ADDR: u32 = 0xFFFF_FFFF;

/// Largest hop limit the three-bit field can carry.
pub const HOP_MAX: u8 = 7;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Header {
    pub to: u32,
    pub from: u32,
    pub id: u32,
    pub flags: u8,
    /// Hint for which channel's key to try; see [`crate::hash::channel_hash`].
    pub channel: u8,
    /// Low byte of the next hop's node number, 0 when unset.
    pub next_hop: u8,
    /// Low byte of the relaying node's number, 0 when unset.
    pub relay_node: u8,
}

fn le_u32(bytes: &[u8], at: usize) -> u32 {
    (bytes[at] as u32)
        | ((bytes[at + 1] as u32) << 8)
        | ((bytes[at + 2] as u32) << 16)
        | ((bytes[at + 3] as u32) << 24)
}

impl Header {
    /// Decodes the header from the front of a received frame.
    ///
    /// The frame may be longer; the remainder is the encrypted payload.
    pub fn parse(frame: &[u8]) -> Result<Header, i32> {
        if frame.len() < HEADER_LEN {
            return Err(ERR_SHORT_FRAME);
        }
        Ok(Header {
            to: le_u32(frame, 0),
            from: le_u32(frame, 4),
            id: le_u32(frame, 8),
            flags: frame[12],
            channel: frame[13],
            next_hop: frame[14],
            relay_node: frame[15],
        })
    }

    /// Hops this packet may still take.
    pub fn hop_limit(&self) -> u8 {
        self.flags & FLAGS_HOP_LIMIT_MASK
    }

    /// Hop limit the sender started with, or 0 when the sender did not set it.
    pub fn hop_start(&self) -> u8 {
        (self.flags & FLAGS_HOP_START_MASK) >> FLAGS_HOP_START_SHIFT
    }

    pub fn want_ack(&self) -> bool {
        self.flags & FLAGS_WANT_ACK_MASK != 0
    }

    pub fn via_mqtt(&self) -> bool {
        self.flags & FLAGS_VIA_MQTT_MASK != 0
    }

    pub fn is_broadcast(&self) -> bool {
        self.to == BROADCAST_ADDR
    }

    /// Hops taken so far, or `None` when the sender left `hop_start` unset.
    ///
    /// Older senders always sent 0, and a packet that has genuinely used every
    /// hop is indistinguishable from that, so this cannot be a plain subtraction.
    pub fn hops_away(&self) -> Option<u8> {
        let start = self.hop_start();
        if start == 0 || start < self.hop_limit() {
            return None;
        }
        Some(start - self.hop_limit())
    }

    /// Writes the header into the first [`HEADER_LEN`] bytes of `out`.
    pub fn write(&self, out: &mut [u8]) -> Result<(), i32> {
        if out.len() < HEADER_LEN {
            return Err(ERR_SHORT_FRAME);
        }
        let words = [self.to, self.from, self.id];
        let mut i = 0;
        while i < 3 {
            let v = words[i];
            out[i * 4] = v as u8;
            out[i * 4 + 1] = (v >> 8) as u8;
            out[i * 4 + 2] = (v >> 16) as u8;
            out[i * 4 + 3] = (v >> 24) as u8;
            i += 1;
        }
        out[12] = self.flags;
        out[13] = self.channel;
        out[14] = self.next_hop;
        out[15] = self.relay_node;
        Ok(())
    }
}

/// Packs the flag byte the way `beginSending` does.
pub fn build_flags(hop_limit: u8, want_ack: bool, via_mqtt: bool, hop_start: u8) -> u8 {
    let mut flags = hop_limit.min(HOP_MAX);
    if want_ack {
        flags |= FLAGS_WANT_ACK_MASK;
    }
    if via_mqtt {
        flags |= FLAGS_VIA_MQTT_MASK;
    }
    flags |= (hop_start << FLAGS_HOP_START_SHIFT) & FLAGS_HOP_START_MASK;
    flags
}

/// Status code form of [`Header::parse`], for the C ABI.
pub fn parse_status(frame: &[u8]) -> i32 {
    match Header::parse(frame) {
        Ok(_) => OK,
        Err(e) => e,
    }
}
