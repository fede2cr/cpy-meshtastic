//! The two hashes Meshtastic uses, which are different functions for different
//! jobs and easy to confuse.

/// djb2, by Dan Bernstein. Picks the default frequency slot from a channel name.
pub fn djb2(bytes: &[u8]) -> u32 {
    let mut hash: u32 = 5381;
    let mut i = 0;
    while i < bytes.len() {
        // hash * 33 + c, allowed to wrap exactly as the C does.
        hash = hash.wrapping_shl(5).wrapping_add(hash).wrapping_add(bytes[i] as u32);
        i += 1;
    }
    hash
}

/// XOR of every byte. Only used as an ingredient of [`channel_hash`].
pub fn xor_hash(bytes: &[u8]) -> u8 {
    let mut h: u8 = 0;
    let mut i = 0;
    while i < bytes.len() {
        h ^= bytes[i];
        i += 1;
    }
    h
}

/// The `channel` byte in the header.
///
/// It is a hint, not an identifier: it narrows which keys are worth trying and
/// collides freely, so a match does not prove the packet is on that channel.
pub fn channel_hash(name: &[u8], psk: &[u8]) -> u8 {
    xor_hash(name) ^ xor_hash(psk)
}
