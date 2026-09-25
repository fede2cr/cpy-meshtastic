//! Modem presets: the named speed/range trade-offs every node picks from.
//!
//! Transcribed from `modemPresetToParams` in `src/mesh/MeshRadio.h`.

use crate::ERR_UNKNOWN_PRESET;

/// Spreading factor, bandwidth and coding rate for one preset.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Preset {
    pub sf: u8,
    pub bw_hz: u32,
    /// Coding rate denominator: 5 means 4/5.
    pub cr: u8,
}

// These indices are ours, not Meshtastic's protobuf enum. Nothing on the wire
// carries a preset number -- both ends are configured out of band -- so there is
// nothing to be compatible with, and inventing a mapping to an enum whose values
// have not been checked against the pinned tag would be a silent trap later.
pub const SHORT_TURBO: u8 = 0;
pub const SHORT_FAST: u8 = 1;
pub const SHORT_SLOW: u8 = 2;
pub const MEDIUM_FAST: u8 = 3;
pub const MEDIUM_SLOW: u8 = 4;
pub const LONG_TURBO: u8 = 5;
pub const LONG_MODERATE: u8 = 6;
pub const LONG_SLOW: u8 = 7;
/// What a node runs unless someone changed it.
pub const LONG_FAST: u8 = 8;

pub const PRESET_COUNT: u8 = 9;

/// `(sf, bw_hz, wide_bw_hz, cr)`, indexed by the constants above.
///
/// The wide bandwidths are the 2.4 GHz variants, used when the region sets
/// `wide_lora`.
///
/// The display names that go with these -- "LongFast" and friends, which feed
/// the default-slot hash because an unnamed primary channel takes the preset's
/// name -- live in the Python wrapper. They are presentation strings, and
/// keeping string literals out of the crate keeps `.rodata` relocations out of
/// the `.mpy` link.
const PRESETS: [(u8, u32, u32, u8); PRESET_COUNT as usize] = [
    (7, 500_000, 1_625_000, 5),
    (7, 250_000, 812_500, 5),
    (8, 250_000, 812_500, 5),
    (9, 250_000, 812_500, 5),
    (10, 250_000, 812_500, 5),
    (11, 500_000, 1_625_000, 8),
    (11, 125_000, 406_250, 8),
    (12, 125_000, 406_250, 8),
    (11, 250_000, 812_500, 5),
];

/// Modem parameters for a preset. `wide` selects the 2.4 GHz bandwidths.
pub fn params(preset: u8, wide: bool) -> Result<Preset, i32> {
    if preset >= PRESET_COUNT {
        return Err(ERR_UNKNOWN_PRESET);
    }
    let (sf, bw, wide_bw, cr) = PRESETS[preset as usize];
    Ok(Preset {
        sf,
        bw_hz: if wide { wide_bw } else { bw },
        cr,
    })
}
