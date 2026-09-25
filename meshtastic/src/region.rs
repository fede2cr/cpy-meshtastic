//! Regional band limits and the channel-slot arithmetic built on them.
//!
//! Transcribed from the `RDEF` table and `applyModemConfig` in
//! `src/mesh/RadioInterface.cpp`.

use crate::hash::djb2;
use crate::{ERR_BAD_BANDWIDTH, ERR_BAD_SLOT, ERR_UNKNOWN_REGION};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Region {
    pub start_hz: u32,
    pub end_hz: u32,
    /// Percentage of an hour a transmitter may occupy. Advisory here; nothing in
    /// this crate enforces it.
    pub duty_cycle_pct: u16,
    /// Guard band between slots. Zero for every region in the pinned tag, which
    /// matters because of the quirk noted on [`slot_frequency_hz`].
    pub spacing_hz: u32,
    /// Legal ceiling in dBm, before antenna gain.
    pub power_limit_dbm: u8,
    /// True for the 2.4 GHz band, which uses the wide preset bandwidths.
    pub wide_lora: bool,
}

// As with presets, these indices are ours. Region is configured out of band and
// never appears on the wire.
pub const UNSET: u8 = 0;
pub const US: u8 = 1;
pub const EU_433: u8 = 2;
pub const EU_868: u8 = 3;
pub const CN: u8 = 4;
pub const JP: u8 = 5;
pub const ANZ: u8 = 6;
pub const ANZ_433: u8 = 7;
pub const KR: u8 = 8;
pub const TW: u8 = 9;
pub const RU: u8 = 10;
pub const IN: u8 = 11;
pub const NZ_865: u8 = 12;
pub const TH: u8 = 13;
pub const UA_433: u8 = 14;
pub const UA_868: u8 = 15;
pub const MY_433: u8 = 16;
pub const MY_919: u8 = 17;
pub const SG_923: u8 = 18;
pub const PH_433: u8 = 19;
pub const PH_868: u8 = 20;
pub const PH_915: u8 = 21;
pub const KZ_433: u8 = 22;
pub const KZ_863: u8 = 23;
pub const NP_865: u8 = 24;
pub const BR_902: u8 = 25;
pub const LORA_24: u8 = 26;

pub const REGION_COUNT: u8 = 27;

/// This is a transcribed subset, not necessarily every region in the pinned tag.
///
/// An index past the end is an error rather than a fallback to US, so a region
/// that was missed fails loudly instead of quietly transmitting on a band it is
/// not allowed to use. Append to the end; renumbering would break stored config.
const REGIONS: [(u32, u32, u16, u32, u8, bool); REGION_COUNT as usize] = [
    // start_hz, end_hz, duty%, spacing_hz, power_dbm, wide
    (902_000_000, 928_000_000, 100, 0, 30, false), // UNSET: the firmware's
    // placeholder shares the US band, but a node in this state refuses to
    // transmit. Treat a config that lands here as unconfigured, not as US.
    (902_000_000, 928_000_000, 100, 0, 30, false), // US
    (433_000_000, 434_000_000, 10, 0, 10, false),  // EU_433
    (869_400_000, 869_650_000, 10, 0, 27, false),  // EU_868
    (470_000_000, 510_000_000, 100, 0, 19, false), // CN
    (920_500_000, 923_500_000, 100, 0, 13, false), // JP
    (915_000_000, 928_000_000, 100, 0, 30, false), // ANZ
    (433_050_000, 434_790_000, 100, 0, 14, false), // ANZ_433
    (920_000_000, 923_000_000, 100, 0, 23, false), // KR
    (920_000_000, 925_000_000, 100, 0, 27, false), // TW
    (868_700_000, 869_200_000, 100, 0, 20, false), // RU
    (865_000_000, 867_000_000, 100, 0, 30, false), // IN
    (864_000_000, 868_000_000, 100, 0, 36, false), // NZ_865
    (920_000_000, 925_000_000, 10, 0, 27, false),  // TH
    (433_000_000, 434_700_000, 10, 0, 10, false),  // UA_433
    (868_000_000, 868_600_000, 1, 0, 14, false),   // UA_868
    (433_000_000, 435_000_000, 100, 0, 20, false), // MY_433
    (919_000_000, 924_000_000, 100, 0, 27, false), // MY_919
    (917_000_000, 925_000_000, 100, 0, 20, false), // SG_923
    (433_000_000, 434_700_000, 100, 0, 10, false), // PH_433
    (868_000_000, 869_400_000, 100, 0, 14, false), // PH_868
    (915_000_000, 918_000_000, 100, 0, 24, false), // PH_915
    (433_075_000, 434_775_000, 100, 0, 10, false), // KZ_433
    (863_000_000, 868_000_000, 100, 0, 30, false), // KZ_863
    (865_000_000, 868_000_000, 100, 0, 30, false), // NP_865
    (902_000_000, 907_500_000, 100, 0, 30, false), // BR_902
    (2_400_000_000, 2_483_500_000, 100, 0, 10, true), // LORA_24
];

pub fn region(index: u8) -> Result<Region, i32> {
    if index >= REGION_COUNT {
        return Err(ERR_UNKNOWN_REGION);
    }
    let (start_hz, end_hz, duty_cycle_pct, spacing_hz, power_limit_dbm, wide_lora) =
        REGIONS[index as usize];
    Ok(Region {
        start_hz,
        end_hz,
        duty_cycle_pct,
        spacing_hz,
        power_limit_dbm,
        wide_lora,
    })
}

/// How many slots of `bw_hz` fit in the region.
pub fn num_channels(region: &Region, bw_hz: u32) -> Result<u32, i32> {
    if bw_hz == 0 || region.end_hz <= region.start_hz {
        return Err(ERR_BAD_BANDWIDTH);
    }
    let n = (region.end_hz - region.start_hz) / (region.spacing_hz + bw_hz);
    if n == 0 {
        return Err(ERR_BAD_BANDWIDTH);
    }
    Ok(n)
}

/// Centre frequency of slot `slot`, counting from zero.
///
/// Note that `spacing_hz` is part of the slot *count* but not of the slot
/// *position*. That looks like an oversight in the firmware, and it would space
/// channels wrongly for any region with a non-zero guard band, but every region
/// in the pinned tag has `spacing_hz == 0` so it has no effect today. Copied as
/// is, because matching the rest of the mesh beats being right on our own.
pub fn slot_frequency_hz(region: &Region, bw_hz: u32, slot: u32) -> Result<u32, i32> {
    let n = num_channels(region, bw_hz)?;
    if slot >= n {
        return Err(ERR_BAD_SLOT);
    }
    Ok(region.start_hz + bw_hz / 2 + slot * bw_hz)
}

/// The slot a channel lands on when the user has not pinned one.
///
/// `name` is the channel name, which for an unnamed primary channel is the
/// preset's display name, so a default US LongFast node hashes `"LongFast"`.
pub fn default_slot(region: &Region, bw_hz: u32, name: &[u8]) -> Result<u32, i32> {
    let n = num_channels(region, bw_hz)?;
    Ok(djb2(name) % n)
}

/// Frequency for a channel number as the user sees it: 0 means "hash the name",
/// and any other value is a one-based slot index.
pub fn channel_frequency_hz(
    region: &Region,
    bw_hz: u32,
    channel_num: u32,
    name: &[u8],
) -> Result<u32, i32> {
    let n = num_channels(region, bw_hz)?;
    let slot = if channel_num != 0 {
        (channel_num - 1) % n
    } else {
        djb2(name) % n
    };
    slot_frequency_hz(region, bw_hz, slot)
}
