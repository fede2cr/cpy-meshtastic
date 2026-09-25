//! Little-endian scalars, read and written a byte at a time.
//!
//! Written out rather than transmuted or `from_le_bytes`d because these run on
//! buffers the caller owns at offsets the caller chose, so nothing here can
//! assume alignment, and because the array-slicing forms pull in out-of-line
//! `core` generics that the natmod linker does not keep.

pub fn u32_at(buf: &[u8], at: usize) -> u32 {
    (buf[at] as u32)
        | ((buf[at + 1] as u32) << 8)
        | ((buf[at + 2] as u32) << 16)
        | ((buf[at + 3] as u32) << 24)
}

pub fn put_u32(buf: &mut [u8], at: usize, value: u32) {
    buf[at] = value as u8;
    buf[at + 1] = (value >> 8) as u8;
    buf[at + 2] = (value >> 16) as u8;
    buf[at + 3] = (value >> 24) as u8;
}

pub fn u16_at(buf: &[u8], at: usize) -> u16 {
    (buf[at] as u16) | ((buf[at + 1] as u16) << 8)
}

pub fn put_u16(buf: &mut [u8], at: usize, value: u16) {
    buf[at] = value as u8;
    buf[at + 1] = (value >> 8) as u8;
}
