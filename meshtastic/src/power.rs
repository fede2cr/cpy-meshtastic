//! Battery state of charge, from cell voltage.
//!
//! Voltage is not charge. A lithium cell spends most of its capacity between
//! 3.9 V and 3.6 V and then falls off a cliff, so a linear scale reads "about
//! half" for hours and then empties in minutes. The curve below is the
//! firmware's own `OCV_ARRAY`, which is what makes this node and a stock one
//! sitting beside it agree about the same pack.
//!
//! Integer throughout. The firmware interpolates in floats; float maths here
//! would pull in soft-float helpers the `.mpy` linker cannot resolve, and the
//! table is in whole millivolts anyway.

/// Open-circuit voltage of one cell, in millivolts, at 100%, 90% ... 0%.
/// Transcribed from `OCV_ARRAY` in the firmware's `src/power.h`.
const OCV: [u16; 11] = [
    4190, 4050, 3990, 3890, 3800, 3720, 3630, 3530, 3420, 3300, 3100,
];

/// Under this, assume no pack is fitted rather than a very flat one: the
/// charger's power path holds the rail up on USB alone, and its protection cuts
/// in long before a real cell could read this low. The firmware's own
/// threshold, half a volt below the bottom of the table.
pub const NO_BATTERY_MV: u16 = OCV[OCV.len() - 1] - 500;

/// Percent remaining, 0 to 100.
///
/// Above the top of the table this is 100 and below the bottom it is 0, which
/// are both real answers: a cell held at 4.2 V is full, and one at 3.0 V is
/// done. Whether a pack is there at all is [`NO_BATTERY_MV`]'s question.
pub fn percent(millivolts: u16) -> u8 {
    let last = OCV.len() - 1;
    for i in 0..=last {
        if OCV[i] > millivolts {
            continue;
        }
        if i == 0 {
            return 100;
        }
        // Ten percent per step, plus however far into this step the reading is.
        let step = (OCV[i - 1] - OCV[i]) as u32;
        let into = (millivolts - OCV[i]) as u32;
        let soc = 10 * (last - i) as u32 + (10 * into) / step;
        return if soc > 100 { 100 } else { soc as u8 };
    }
    0
}
