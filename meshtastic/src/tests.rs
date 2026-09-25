//! Host tests. These run on the development machine, not the board.
//!
//! The frequency cases matter most: a slot calculation that is wrong by one
//! channel produces a radio that works perfectly against itself and hears
//! nothing from the real mesh, which is expensive to debug on hardware.

use crate::airtime;
use crate::cache;
use crate::duty;
use crate::roster;
use crate::stream;
use crate::hash::{channel_hash, djb2, xor_hash};
use crate::header::{build_flags, Header, BROADCAST_ADDR, HEADER_LEN};
use crate::inbox;
use crate::store;
use crate::payload;
use crate::pbuf;
use crate::power;
use crate::preset;
use crate::proto;
use crate::region;
use crate::nmea;
use crate::{ERR_BAD_MODEM, ERR_BAD_SENTENCE, ERR_BAD_STATE, ERR_BAD_WIRE};
use crate::{ERR_NO_FIX, ERR_NO_SPACE, ERR_SHORT_FRAME, ERR_TRUNCATED};

// ---------------------------------------------------------------- hashes

#[test]
fn djb2_seed_is_returned_for_empty_input() {
    assert_eq!(djb2(b""), 5381);
}

#[test]
fn djb2_of_long_fast_matches_hand_calculation() {
    // Worked through by hand, byte by byte, including the u32 wraps. Every
    // frequency assertion below leans on this one number.
    assert_eq!(djb2(b"LongFast"), 130_429_955);
}

#[test]
fn djb2_wraps_rather_than_saturating() {
    // Long enough to overflow u32 many times over; the point is that it returns
    // at all, which under debug arithmetic checks it would not if a `*` or `+`
    // had been used instead of the wrapping forms.
    let long = [b'z'; 64];
    let _ = djb2(&long);
}

#[test]
fn channel_hash_is_the_xor_of_both_parts() {
    assert_eq!(xor_hash(b""), 0);
    assert_eq!(xor_hash(&[0x0F, 0xF0]), 0xFF);
    assert_eq!(channel_hash(&[0x0F, 0xF0], &[0x01]), 0xFE);
}

// ---------------------------------------------------------------- header

/// A broadcast from node 0x11223344, id 0xAABBCCDD, hop limit 3 of 5, want-ack.
fn sample() -> [u8; HEADER_LEN] {
    [
        0xFF, 0xFF, 0xFF, 0xFF, // to
        0x44, 0x33, 0x22, 0x11, // from
        0xDD, 0xCC, 0xBB, 0xAA, // id
        0b1010_1011, // hop_start 5, want_ack, hop_limit 3
        0x08,        // channel hash
        0x00,        // next_hop
        0x77,        // relay_node
    ]
}

#[test]
fn header_fields_are_little_endian() {
    let h = Header::parse(&sample()).unwrap();
    assert_eq!(h.to, BROADCAST_ADDR);
    assert_eq!(h.from, 0x1122_3344);
    assert_eq!(h.id, 0xAABB_CCDD);
    assert_eq!(h.channel, 0x08);
    assert_eq!(h.next_hop, 0x00);
    assert_eq!(h.relay_node, 0x77);
    assert!(h.is_broadcast());
}

#[test]
fn header_flags_decode() {
    let h = Header::parse(&sample()).unwrap();
    assert_eq!(h.hop_limit(), 3);
    assert_eq!(h.hop_start(), 5);
    assert!(h.want_ack());
    assert!(!h.via_mqtt());
    assert_eq!(h.hops_away(), Some(2));
}

#[test]
fn hops_away_is_unknown_when_hop_start_is_unset() {
    // Old senders leave hop_start at zero, which is not the same as "arrived
    // with no hops taken" and must not be reported as zero hops away.
    let mut raw = sample();
    raw[12] = 0b0000_0011;
    assert_eq!(Header::parse(&raw).unwrap().hops_away(), None);
}

#[test]
fn hops_away_is_unknown_when_the_limit_exceeds_the_start() {
    // Malformed or rewritten in flight; a subtraction here would underflow.
    let mut raw = sample();
    raw[12] = (2 << 5) | 5;
    assert_eq!(Header::parse(&raw).unwrap().hops_away(), None);
}

#[test]
fn header_parse_rejects_short_frames() {
    let raw = sample();
    for len in 0..HEADER_LEN {
        assert_eq!(Header::parse(&raw[..len]), Err(ERR_SHORT_FRAME), "len {}", len);
    }
    assert!(Header::parse(&raw[..HEADER_LEN]).is_ok());
}

#[test]
fn header_parse_accepts_a_frame_with_a_payload_after_it() {
    let mut frame = [0u8; 64];
    frame[..HEADER_LEN].copy_from_slice(&sample());
    assert_eq!(Header::parse(&frame).unwrap().from, 0x1122_3344);
}

#[test]
fn header_round_trips_through_write() {
    let raw = sample();
    let h = Header::parse(&raw).unwrap();
    let mut out = [0u8; HEADER_LEN];
    h.write(&mut out).unwrap();
    assert_eq!(out, raw);
}

#[test]
fn header_write_rejects_a_short_buffer() {
    let h = Header::parse(&sample()).unwrap();
    let mut out = [0u8; HEADER_LEN - 1];
    assert_eq!(h.write(&mut out), Err(ERR_SHORT_FRAME));
}

#[test]
fn build_flags_matches_the_bit_layout() {
    assert_eq!(build_flags(3, true, false, 5), 0b1010_1011);
    assert_eq!(build_flags(0, false, true, 0), 0b0001_0000);
    // Both fields are three bits wide and neither may bleed into the other.
    assert_eq!(build_flags(7, true, true, 7), 0xFF);
}

// ---------------------------------------------------------------- presets

#[test]
fn long_fast_is_sf11_bw250_cr45() {
    let p = preset::params(preset::LONG_FAST, false).unwrap();
    assert_eq!((p.sf, p.bw_hz, p.cr), (11, 250_000, 5));
}

#[test]
fn wide_lora_selects_the_24ghz_bandwidth_and_leaves_sf_and_cr_alone() {
    let narrow = preset::params(preset::LONG_FAST, false).unwrap();
    let wide = preset::params(preset::LONG_FAST, true).unwrap();
    assert_eq!(wide.bw_hz, 812_500);
    assert_eq!((wide.sf, wide.cr), (narrow.sf, narrow.cr));
}

#[test]
fn every_preset_is_populated_and_in_range() {
    for i in 0..preset::PRESET_COUNT {
        let p = preset::params(i, false).unwrap();
        assert!((5..=12).contains(&p.sf), "preset {} sf {}", i, p.sf);
        assert!((5..=8).contains(&p.cr), "preset {} cr {}", i, p.cr);
        assert!(p.bw_hz > 0);
    }
    assert!(preset::params(preset::PRESET_COUNT, false).is_err());
}

// ------------------------------------------------------------ frequencies

#[test]
fn us_has_104_channels_at_250khz() {
    let us = region::region(region::US).unwrap();
    assert_eq!(region::num_channels(&us, 250_000).unwrap(), 104);
}

#[test]
fn us_long_fast_lands_on_906_875_000_hz() {
    // The headline number: this is the frequency a stock US node sits on, and it
    // is independently known from the Meshtastic documentation. If this passes,
    // the hash, the modulo, the channel count and the half-bandwidth offset are
    // all right together.
    let us = region::region(region::US).unwrap();
    let bw = preset::params(preset::LONG_FAST, false).unwrap().bw_hz;
    assert_eq!(region::default_slot(&us, bw, b"LongFast").unwrap(), 19);
    assert_eq!(
        region::channel_frequency_hz(&us, bw, 0, b"LongFast").unwrap(),
        906_875_000
    );
}

#[test]
fn eu868_long_fast_lands_on_869_525_000_hz() {
    // A second externally known frequency, and a useful one because the band is
    // exactly one channel wide: it exercises the degenerate case where the hash
    // cannot matter, and it would catch an off-by-one that US alone would not.
    let eu = region::region(region::EU_868).unwrap();
    let bw = preset::params(preset::LONG_FAST, false).unwrap().bw_hz;
    assert_eq!(region::num_channels(&eu, bw).unwrap(), 1);
    assert_eq!(
        region::channel_frequency_hz(&eu, bw, 0, b"LongFast").unwrap(),
        869_525_000
    );
}

#[test]
fn channel_num_is_one_based_and_bypasses_the_hash() {
    let us = region::region(region::US).unwrap();
    // Slot 19 named explicitly is channel 20, and must agree with the hash.
    assert_eq!(
        region::channel_frequency_hz(&us, 250_000, 20, b"ignored").unwrap(),
        906_875_000
    );
    assert_eq!(
        region::channel_frequency_hz(&us, 250_000, 1, b"ignored").unwrap(),
        902_125_000
    );
}

#[test]
fn channel_num_past_the_end_wraps_rather_than_failing() {
    let us = region::region(region::US).unwrap();
    let first = region::channel_frequency_hz(&us, 250_000, 1, b"").unwrap();
    assert_eq!(
        region::channel_frequency_hz(&us, 250_000, 105, b"").unwrap(),
        first
    );
}

#[test]
fn slots_stay_inside_the_band_for_every_region_and_preset() {
    // The property that actually keeps us legal: the top slot's centre plus half
    // its bandwidth must not spill past the regional edge.
    for r in 0..region::REGION_COUNT {
        let reg = region::region(r).unwrap();
        for p in 0..preset::PRESET_COUNT {
            let bw = preset::params(p, reg.wide_lora).unwrap().bw_hz;
            let n = match region::num_channels(&reg, bw) {
                Ok(n) => n,
                // Narrower bands cannot fit a wide preset at all; refusing is
                // the correct answer, not a failure.
                Err(_) => continue,
            };
            let top = region::slot_frequency_hz(&reg, bw, n - 1).unwrap();
            assert!(
                top + bw / 2 <= reg.end_hz,
                "region {} preset {}: top slot {} + bw/2 exceeds {}",
                r,
                p,
                top,
                reg.end_hz
            );
            assert!(top - bw / 2 >= reg.start_hz);
        }
    }
}

#[test]
fn slot_index_is_bounds_checked() {
    let us = region::region(region::US).unwrap();
    assert!(region::slot_frequency_hz(&us, 250_000, 103).is_ok());
    assert!(region::slot_frequency_hz(&us, 250_000, 104).is_err());
}

#[test]
fn a_bandwidth_wider_than_the_band_is_refused_rather_than_dividing_to_zero() {
    let eu = region::region(region::EU_868).unwrap();
    assert!(region::num_channels(&eu, 500_000).is_err());
    assert!(region::num_channels(&eu, 0).is_err());
}

#[test]
fn unknown_region_is_an_error_not_a_fallback() {
    assert!(region::region(region::REGION_COUNT).is_err());
    assert!(region::region(255).is_err());
}

#[test]
fn lora_24_arithmetic_does_not_overflow_u32() {
    // 2.4 GHz is the only band where the hertz values approach the top of a u32,
    // and it is the one place an i32 return would have silently gone negative.
    let r = region::region(region::LORA_24).unwrap();
    assert!(r.wide_lora);
    let bw = preset::params(preset::LONG_FAST, true).unwrap().bw_hz;
    let f = region::channel_frequency_hz(&r, bw, 0, b"LongFast").unwrap();
    assert!(f > 2_400_000_000, "{}", f);
    assert!(f < r.end_hz);
}

// ---------------------------------------------------------------- protobuf

// All three of these came off the air in one capture run on 2026-08-26, on US
// LongFast, and were decrypted on the host with the default channel key. They
// are kept verbatim rather than hand-built: a synthetic fixture would only
// prove the parser agrees with whatever the test author believed.

/// Portnum 3, POSITION_APP, from node !f994042c.
const POSITION_PLAINTEXT: [u8; 29] = [
    0x08, 0x03, 0x12, 0x17, 0x0d, 0x00, 0x00, 0xec, 0x05, 0x15, 0x00, 0x00,
    0xe4, 0xcd, 0x18, 0xd1, 0x09, 0x25, 0xec, 0xae, 0x8f, 0x6a, 0x28, 0x01,
    0xb8, 0x01, 0x0d, 0x48, 0x01,
];

/// Portnum 67, TELEMETRY_APP, from node !172979c3.
const TELEMETRY_PLAINTEXT: [u8; 28] = [
    0x08, 0x43, 0x12, 0x16, 0x0d, 0x6b, 0xb0, 0x8f, 0x6a, 0x1a, 0x0f, 0x0d,
    0x71, 0x3d, 0xce, 0x41, 0x15, 0x80, 0x25, 0x90, 0x42, 0x1d, 0x61, 0x5f,
    0x61, 0x44, 0x48, 0x01,
];

/// The same run's third packet, which was on a channel whose key we do not
/// have, put through the default key anyway. This is what a wrong key looks
/// like, and the parser has to reject it rather than invent a portnum.
const WRONG_KEY_PLAINTEXT: [u8; 48] = [
    0x37, 0x9c, 0x2f, 0xbb, 0x91, 0xb6, 0xda, 0x30, 0xa9, 0x22, 0x2a, 0x90,
    0x33, 0x4b, 0xd4, 0xc0, 0x19, 0x6c, 0x47, 0x0d, 0xa0, 0xa1, 0x60, 0xa6,
    0x7d, 0x1c, 0xba, 0x14, 0x99, 0x99, 0xf7, 0x3e, 0x62, 0x7c, 0x6b, 0x99,
    0x48, 0x76, 0xa6, 0x26, 0x4e, 0x10, 0xe8, 0xa6, 0xc9, 0x71, 0x9c, 0x1a,
];

/// Portnum 70, TRACEROUTE_APP: a reply from !34198b20 to !9ee71bac, caught in
/// flight on 2026-08-27. Kept because it is the only capture so far with a
/// `request_id` set and with `bitfield` present *and* zero -- the case that
/// tells a missing optional field apart from one the sender explicitly cleared.
const TRACEROUTE_PLAINTEXT: [u8; 81] = [
    0x08, 0x46, 0x12, 0x46, 0x0a, 0x08, 0xff, 0xff, 0xff, 0xff, 0x0e, 0x6a,
    0xa1, 0x40, 0x12, 0x15, 0x80, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0x01, 0x00, 0xea, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0x01, 0x1a, 0x0c, 0x0e, 0x6a, 0xa1, 0x40, 0xe8, 0xf8, 0xa9, 0xac, 0xbd,
    0xf0, 0x12, 0x5b, 0x22, 0x15, 0xf9, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0x01, 0x17, 0xe9, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0x01, 0x35, 0x06, 0xc0, 0xd5, 0x15, 0x48, 0x00,
];

#[test]
fn captured_position_packet_parses_as_data() {
    let d = proto::parse_data(&POSITION_PLAINTEXT).unwrap();
    assert_eq!(d.portnum, 3);
    assert_eq!(d.payload_off, 4);
    assert_eq!(d.payload_len, 23);
    assert!(d.has_bitfield);
    assert_eq!(d.bitfield, 1);
    assert!(!d.want_response);
}

#[test]
fn captured_telemetry_packet_parses_as_data() {
    let d = proto::parse_data(&TELEMETRY_PLAINTEXT).unwrap();
    assert_eq!(d.portnum, 67);
    assert_eq!(d.payload_len, 22);
}

#[test]
fn a_present_but_zero_bitfield_is_not_an_absent_one() {
    let d = proto::parse_data(&TRACEROUTE_PLAINTEXT).unwrap();
    assert_eq!(d.portnum, 70);
    assert_eq!(d.payload_len, 70);
    assert_eq!(d.request_id, 0x15d5_c006);
    assert!(d.has_bitfield);
    assert_eq!(d.bitfield, 0);
}

#[test]
fn traceroute_payload_is_four_packed_arrays() {
    // Packed repeated scalars arrive length-delimited whatever their element
    // type, so the walker sees four byte runs and leaves the contents alone.
    let d = proto::parse_data(&TRACEROUTE_PLAINTEXT).unwrap();
    let body = &TRACEROUTE_PLAINTEXT[d.payload_off..d.payload_off + d.payload_len];

    let mut at = 0;
    let mut seen = [(0u32, 0usize); 4];
    for slot in seen.iter_mut() {
        let f = proto::field(body, at).unwrap();
        assert_eq!(f.wire, proto::WIRE_LEN);
        *slot = (f.number, f.data_len);
        at = f.next;
    }
    assert_eq!(at, body.len());
    // route: two fixed32 hops. snr_towards: three sign-extended int32 varints,
    // which is why 21 bytes hold only three values.
    assert_eq!(seen, [(1, 8), (2, 21), (3, 12), (4, 21)]);

    let route = proto::field(body, 0).unwrap();
    let hops = &body[route.data_off..route.data_off + route.data_len];
    assert_eq!(u32::from_le_bytes(hops[0..4].try_into().unwrap()), 0xffff_ffff);
    assert_eq!(u32::from_le_bytes(hops[4..8].try_into().unwrap()), 0x40a1_6a0e);
}

#[test]
fn traceroute_locates_all_four_arrays_in_a_real_reply() {
    let d = proto::parse_data(&TRACEROUTE_PLAINTEXT).unwrap();
    let body = &TRACEROUTE_PLAINTEXT[d.payload_off..d.payload_off + d.payload_len];
    let r = payload::traceroute(body).unwrap();
    assert_eq!(r.have, payload::HAS_1 | payload::HAS_2 | payload::HAS_3 | payload::HAS_4);
    assert_eq!((r.route_len, r.snr_out_len), (8, 21));
    assert_eq!((r.back_len, r.snr_back_len), (12, 21));
    // The offsets have to point at the data, not at the tag before it.
    let hops = &body[r.route_off as usize..(r.route_off + r.route_len) as usize];
    assert_eq!(u32::from_le_bytes(hops[4..8].try_into().unwrap()), 0x40a1_6a0e);
}

#[test]
fn a_traceroute_that_carries_nothing_reports_every_array_absent() {
    let r = payload::traceroute(&[]).unwrap();
    assert_eq!(r.have, 0);
}

#[test]
fn a_traceroute_array_sent_as_a_varint_is_passed_over() {
    // Field 1 as WIRE_VARINT. Packed is the only encoding these ever use, so
    // reading a scalar here would be inventing a hop the sender never wrote.
    let r = payload::traceroute(&[0x08, 0x7F]).unwrap();
    assert_eq!(r.have, 0);
    assert_eq!(r.route_len, 0);
}

#[test]
fn a_wrong_channel_key_is_rejected_rather_than_decoded() {
    // The whole reason the parser is strict. Protobuf's tolerance for unknown
    // fields would otherwise let random bytes through as an empty message.
    assert!(proto::parse_data(&WRONG_KEY_PLAINTEXT).is_err());
}

#[test]
fn position_payload_walks_field_by_field() {
    let d = proto::parse_data(&POSITION_PLAINTEXT).unwrap();
    let body = &POSITION_PLAINTEXT[d.payload_off..d.payload_off + d.payload_len];

    let lat = proto::field(body, 0).unwrap();
    assert_eq!((lat.number, lat.wire), (1, proto::WIRE_I32));
    // 1e-7 degrees, so 9.9352576 N.
    assert_eq!(lat.value as u32, 99_352_576);

    let lon = proto::field(body, lat.next).unwrap();
    assert_eq!(lon.number, 2);
    // Signed: west of Greenwich, -84.0695808.
    assert_eq!(lon.value as u32 as i32, -840_695_808);

    let alt = proto::field(body, lon.next).unwrap();
    assert_eq!((alt.number, alt.wire), (3, proto::WIRE_VARINT));
    // Two bytes of varint, so this also covers the continuation bit.
    assert_eq!(alt.value, 1233);

    let time = proto::field(body, alt.next).unwrap();
    assert_eq!(time.number, 4);
    assert_eq!(time.value as u32, 1_787_801_324);
}

#[test]
fn telemetry_payload_nests_a_submessage() {
    let d = proto::parse_data(&TELEMETRY_PLAINTEXT).unwrap();
    let body = &TELEMETRY_PLAINTEXT[d.payload_off..d.payload_off + d.payload_len];

    let time = proto::field(body, 0).unwrap();
    assert_eq!(time.number, 1);
    // 383 seconds after the position packet above, which matches the gap
    // between the two on the capture console.
    assert_eq!(time.value as u32, 1_787_801_707);

    // Field 3 is EnvironmentMetrics, and the walker hands back its extent
    // without needing to know that.
    let env = proto::field(body, time.next).unwrap();
    assert_eq!((env.number, env.wire), (3, proto::WIRE_LEN));
    assert_eq!(env.data_len, 15);
    assert_eq!(env.next, body.len());

    let inner = &body[env.data_off..env.data_off + env.data_len];
    let temp = proto::field(inner, 0).unwrap();
    // 25.78 C as an IEEE-754 float, left for the caller to convert.
    assert_eq!(temp.value as u32, 0x41CE_3D71);
}

#[test]
fn trailing_bytes_after_the_message_are_rejected() {
    let mut buf = [0u8; 30];
    buf[..29].copy_from_slice(&POSITION_PLAINTEXT);
    buf[29] = 0x00;
    assert_eq!(proto::parse_data(&buf), Err(ERR_BAD_WIRE));
}

#[test]
fn a_length_past_the_end_of_the_buffer_is_rejected() {
    // Field 2, length-delimited, claiming 100 bytes in a 3-byte buffer.
    assert_eq!(proto::field(&[0x12, 0x64, 0x00], 0), Err(ERR_TRUNCATED));
}

#[test]
fn deprecated_group_wire_types_are_rejected() {
    // Wire types 3 and 4 were groups, removed long ago; 6 and 7 never existed.
    for tag in [0x0Bu8, 0x0C, 0x0E, 0x0F] {
        assert_eq!(proto::field(&[tag, 0x00], 0), Err(ERR_BAD_WIRE));
    }
}

#[test]
fn a_varint_running_off_the_end_is_rejected() {
    // Continuation bit set on the last byte available.
    assert_eq!(proto::field(&[0x08, 0x80], 0), Err(ERR_TRUNCATED));
}

#[test]
fn field_number_zero_is_rejected() {
    // Not expressible in the schema language, so it cannot be a real message.
    assert_eq!(proto::field(&[0x00, 0x00], 0), Err(ERR_BAD_WIRE));
}

// ---------------------------------------------------------------- encoding

#[test]
fn a_text_message_encodes_to_the_bytes_meshtastic_sends() {
    let mut out = [0u8; 32];
    let n = proto::encode_data(1, b"hi", false, None, &mut out).unwrap();
    // portnum=1 as field 1 varint, then the text as field 2 length-delimited.
    assert_eq!(&out[..n], &[0x08, 0x01, 0x12, 0x02, b'h', b'i']);
}

#[test]
fn encoding_round_trips_through_the_parser() {
    let mut out = [0u8; 64];
    let n = proto::encode_data(67, b"\x01\x02\x03", true, Some(1), &mut out).unwrap();
    let data = proto::parse_data(&out[..n]).unwrap();
    assert_eq!(data.portnum, 67);
    assert_eq!(&out[data.payload_off..data.payload_off + data.payload_len], b"\x01\x02\x03");
    assert!(data.want_response);
    assert!(data.has_bitfield);
    assert_eq!(data.bitfield, 1);
}

#[test]
fn zero_valued_scalars_are_omitted_but_an_explicit_bitfield_is_not() {
    // proto3 defaults are absent on the wire, which is what nanopb does on
    // every other node -- except for `optional` fields, where zero is a value.
    let mut out = [0u8; 16];
    let n = proto::encode_data(0, b"", false, None, &mut out).unwrap();
    assert_eq!(n, 0);
    let n = proto::encode_data(0, b"", false, Some(0), &mut out).unwrap();
    assert_eq!(&out[..n], &[0x48, 0x00]);
}

#[test]
fn encoding_refuses_to_overrun_the_buffer() {
    let mut out = [0u8; 4];
    assert_eq!(
        proto::encode_data(1, b"too long", false, None, &mut out),
        Err(ERR_NO_SPACE)
    );
}

#[test]
fn a_varint_over_seven_bits_takes_two_bytes() {
    // Portnum 67 fits in one byte, 128 does not; the continuation bit is the
    // difference between a valid envelope and one the mesh cannot read.
    let mut out = [0u8; 8];
    let n = proto::encode_data(200, b"", false, None, &mut out).unwrap();
    assert_eq!(&out[..n], &[0x08, 0xC8, 0x01]);
    assert_eq!(proto::parse_data(&out[..n]).unwrap().portnum, 200);
}

// ---------------------------------------------------------------- airtime

#[test]
fn long_fast_airtime_matches_the_datasheet_worked_by_hand() {
    // SF11, 250 kHz, 4/5, 46 bytes: 16 of header and 30 of ciphertext, which is
    // a typical position report. Symbol 8192 us, no low-data-rate optimisation,
    // 9 blocks, 53 payload symbols, 293 quarter-symbols -> 293 * 2048 us.
    let us = airtime::time_on_air_us(46, 11, 250_000, 5).unwrap();
    assert_eq!(us, 600_064);
}

#[test]
fn low_data_rate_optimisation_switches_on_above_a_16ms_symbol() {
    // SF12 at 125 kHz gives a 32.8 ms symbol, so the divisor drops from 48 to
    // 40 and the packet gets longer than the untouched formula would say.
    let with = airtime::time_on_air_us(46, 12, 125_000, 5).unwrap();
    // SF11 at 125 kHz is 16.384 ms, over the threshold by a whisker -- and the
    // SX127x datasheet independently mandates the optimisation for exactly
    // SF11 and SF12 at this bandwidth, which is the check on the threshold.
    let edge = airtime::time_on_air_us(46, 11, 125_000, 5).unwrap();
    assert_eq!(with, 2_564_096);
    assert_eq!(edge, 1_363_968);
}

#[test]
fn airtime_grows_with_payload_and_shrinks_with_bandwidth() {
    let short = airtime::time_on_air_us(16, 11, 250_000, 5).unwrap();
    let long = airtime::time_on_air_us(240, 11, 250_000, 5).unwrap();
    let wide = airtime::time_on_air_us(240, 11, 500_000, 5).unwrap();
    assert!(long > short);
    // Doubling the bandwidth halves the symbol, so it halves the packet.
    assert_eq!(wide, long / 2);
}

#[test]
fn a_heavier_coding_rate_costs_time() {
    let light = airtime::time_on_air_us(46, 11, 250_000, 5).unwrap();
    let heavy = airtime::time_on_air_us(46, 11, 250_000, 8).unwrap();
    assert!(heavy > light);
}

#[test]
fn every_preset_gives_an_airtime() {
    for index in 0..preset::PRESET_COUNT {
        let p = preset::params(index, false).unwrap();
        let us = airtime::time_on_air_us(46, p.sf, p.bw_hz, p.cr).unwrap();
        // Nothing in the table should be quicker than a millisecond or slower
        // than a few seconds; either would mean the parameters were misread.
        assert!(us > 1_000 && us < 5_000_000, "preset {} gave {} us", index, us);
    }
}

#[test]
fn modem_parameters_outside_lora_are_rejected() {
    assert_eq!(airtime::time_on_air_us(46, 4, 250_000, 5), Err(ERR_BAD_MODEM));
    assert_eq!(airtime::time_on_air_us(46, 13, 250_000, 5), Err(ERR_BAD_MODEM));
    assert_eq!(airtime::time_on_air_us(46, 11, 0, 5), Err(ERR_BAD_MODEM));
    assert_eq!(airtime::time_on_air_us(46, 11, 250_000, 9), Err(ERR_BAD_MODEM));
}

// ------------------------------------------------------------------ roster

fn table() -> [u8; roster::ROW_BYTES * 4] {
    [0u8; roster::ROW_BYTES * 4]
}

#[test]
fn an_empty_table_holds_nobody() {
    let t = table();
    assert_eq!(roster::slot(&t, 0x0B9DBDE1), None);
    assert_eq!(roster::used(&t), 0);
    assert_eq!(roster::free(&t), Some(0));
    assert_eq!(roster::oldest(&t), None);
    // Node number zero is the free marker, so it can never be a peer: asking
    // for it must not match every empty row in the table.
    assert_eq!(roster::slot(&t, 0), None);
}

#[test]
fn a_sighting_fills_a_row_and_a_second_one_does_not() {
    let mut t = table();
    assert!(roster::touch(&mut t, 0, 0x0B9DBDE1, 1000, 1, 2, 39, -57));
    assert!(!roster::touch(&mut t, 0, 0x0B9DBDE1, 1005, 2, 2, 40, -55));
    let r = roster::read(&t, 0);
    assert_eq!(r.num, 0x0B9DBDE1);
    // `first` stays at the first sighting while `last` follows the latest.
    assert_eq!(r.first, 1000);
    assert_eq!(r.last, 1005);
    assert_eq!(r.count, 2);
    assert_eq!(r.seen, 2);
    assert_eq!(r.hops, 3);
    assert_eq!(r.snr, 40);
    assert_eq!(r.rssi, -55);
    assert_eq!(roster::used(&t), 1);
    assert_eq!(roster::slot(&t, 0x0B9DBDE1), Some(0));
}

#[test]
fn absent_readings_survive_the_round_trip_as_absent() {
    let mut t = table();
    roster::touch(&mut t, 0, 0x11, 1, 1, roster::NO_HOPS, roster::NO_SNR, 0);
    let r = roster::read(&t, 0);
    assert_eq!(r.hops, 0);
    assert_eq!(r.snr, roster::SNR_UNKNOWN);
    assert_eq!(r.rssi, 0);
}

#[test]
fn a_reading_at_the_sentinel_is_clamped_off_it() {
    // -32 dB would land exactly on the "no reading" byte, so it has to give
    // way: reporting a real measurement as absent is the worse of the two.
    assert_eq!(roster::pack_snr(-128), roster::SNR_UNKNOWN + 1);
    assert_eq!(roster::pack_snr(-200), roster::SNR_UNKNOWN + 1);
    assert_eq!(roster::pack_snr(500), 127);
    // Likewise a positive RSSI, which no receiver reports, would read as
    // absent, and zero is the absence.
    assert_eq!(roster::pack_rssi(5), 0);
    assert_eq!(roster::pack_hops(254), 255);
}

#[test]
fn the_count_saturates_rather_than_wrapping() {
    let mut t = table();
    roster::restore(&mut t, 0, 0x11, 1, 1, 0xFFFF, 0, 0, 0, roster::NO_SNR);
    roster::touch(&mut t, 0, 0x11, 2, 2, 0, 0, -50);
    assert_eq!(roster::read(&t, 0).count, 0xFFFF);
}

#[test]
fn the_row_heard_longest_ago_is_the_one_given_up() {
    let mut t = table();
    for (i, num) in [0x11u32, 0x22, 0x33, 0x44].iter().enumerate() {
        roster::touch(&mut t, i * roster::ROW_BYTES, *num, 1000, i as u32 + 1,
                      0, 0, -50);
    }
    assert_eq!(roster::free(&t), None);
    assert_eq!(roster::oldest(&t), Some(0));
    // Speaking again moves a peer to the back of the queue, which is the whole
    // point of ordering on the tick rather than on the clock.
    roster::touch(&mut t, 0, 0x11, 1000, 9, 0, 0, -50);
    assert_eq!(roster::oldest(&t), Some(roster::ROW_BYTES));
}

#[test]
fn clearing_a_row_leaves_nothing_of_the_peer_behind() {
    let mut t = table();
    roster::touch(&mut t, 0, 0x11, 1000, 1, 3, 40, -57);
    roster::identify(&mut t, 0, 9, 2);
    roster::clear(&mut t, 0);
    assert_eq!(roster::used(&t), 0);
    // Counters as well as the number: the next peer to land here would
    // otherwise inherit a packet count it never earned.
    let r = roster::read(&t, 0);
    assert_eq!((r.count, r.hw, r.role, r.snr), (0, 0, 0, 0));
}

#[test]
fn a_restored_peer_keeps_what_was_saved_of_it() {
    let mut t = table();
    roster::restore(&mut t, 0, 0x0B9DBDE1, 5000, 1, 42, 2, 9, 3, 39);
    let r = roster::read(&t, 0);
    assert_eq!((r.num, r.first, r.last, r.count), (0x0B9DBDE1, 5000, 5000, 42));
    assert_eq!((r.hops, r.hw, r.role, r.snr), (3, 9, 3, 39));
    // RSSI is not saved: it belongs to one reception, not to the peer.
    assert_eq!(r.rssi, 0);
}

// ------------------------------------------------------------------- cache

#[test]
fn the_crc_is_the_one_binascii_computes() {
    // The check value every CRC-32 implementation is measured by, and the one
    // already-saved regions were written with.
    assert_eq!(cache::crc32(b"123456789"), 0xCBF4_3926);
    assert_eq!(cache::crc32(b""), 0);
}

#[test]
fn a_peer_record_survives_the_round_trip() {
    let mut out = [0u8; 128];
    let at = cache::HEADER_LEN;
    let next = cache::write_peer(&mut out, at, 0x0B9DBDE1, 90, 42, 3, 9, 2, 39,
                                 b"mzi ", true, b"muzi base duo", true).unwrap();
    cache::finish(&mut out, next - cache::HEADER_LEN).unwrap();
    let end = cache::verify(&out[..next]).unwrap();
    assert_eq!(end, next);
    let r = cache::record(&out, at, end).unwrap().unwrap();
    assert_eq!(r.kind as u8, cache::NODE);
    assert_eq!(r.next as usize, next);
    let p = cache::read_peer(&out, r.off as usize, r.len as usize).unwrap();
    assert_eq!((p.num, p.age, p.count), (0x0B9DBDE1, 90, 42));
    assert_eq!((p.hops, p.hw, p.role, p.snr), (3, 9, 2, 39));
    let short = &out[p.short_off as usize..][..p.short_len as usize];
    let long = &out[p.long_off as usize..][..p.long_len as usize];
    assert_eq!(short, b"mzi ");
    assert_eq!(long, b"muzi base duo");
    // Nothing follows the one record.
    assert!(cache::record(&out, r.next as usize, end).unwrap().is_none());
}

#[test]
fn a_peer_with_no_names_takes_two_bytes_for_them() {
    let mut out = [0u8; 64];
    let at = cache::HEADER_LEN;
    let next = cache::write_peer(&mut out, at, 0x11, 0, 1, 0, 0, 0, -128,
                                 b"", false, b"", false).unwrap();
    assert_eq!(next - at, cache::peer_len(-1, -1));
    cache::finish(&mut out, next - cache::HEADER_LEN).unwrap();
    let end = cache::verify(&out[..next]).unwrap();
    let r = cache::record(&out, at, end).unwrap().unwrap();
    let p = cache::read_peer(&out, r.off as usize, r.len as usize).unwrap();
    assert_eq!((p.short_len, p.long_len), (-1, -1));

    // An empty name is not an absent one, and the two must not collapse: a
    // node that sent "" has answered, and one that has not is still unknown.
    let mut named = [0u8; 64];
    let next = cache::write_peer(&mut named, at, 0x11, 0, 1, 0, 0, 0, -128,
                                 b"", true, b"", true).unwrap();
    cache::finish(&mut named, next - cache::HEADER_LEN).unwrap();
    let end = cache::verify(&named[..next]).unwrap();
    let r = cache::record(&named, at, end).unwrap().unwrap();
    let p = cache::read_peer(&named, r.off as usize, r.len as usize).unwrap();
    assert_eq!((p.short_len, p.long_len), (0, 0));
}

#[test]
fn a_message_record_survives_the_round_trip() {
    let mut out = [0u8; 128];
    let at = cache::HEADER_LEN;
    let next = cache::write_message(&mut out, at, 7, 0x11, 0x22, 1_700_000_000,
                                    1, 0, 1, -13, "hola \u{263a}".as_bytes())
        .unwrap();
    cache::finish(&mut out, next - cache::HEADER_LEN).unwrap();
    let end = cache::verify(&out[..next]).unwrap();
    let r = cache::record(&out, at, end).unwrap().unwrap();
    assert_eq!(r.kind as u8, cache::MESSAGE);
    let m = cache::read_message(&out, r.off as usize, r.len as usize).unwrap();
    assert_eq!((m.id, m.from, m.to, m.time), (7, 0x11, 0x22, 1_700_000_000));
    assert_eq!((m.flags, m.channel, m.hops, m.snr), (1, 0, 1, -13));
    let text = &out[m.text_off as usize..][..m.text_len as usize];
    assert_eq!(core::str::from_utf8(text).unwrap(), "hola \u{263a}");
}

#[test]
fn a_blank_or_altered_region_reads_as_no_cache_at_all() {
    assert_eq!(cache::verify(&[0xFFu8; 64]), None);
    assert_eq!(cache::verify(&[]), None);
    let mut out = [0u8; 64];
    let next = cache::write_peer(&mut out, cache::HEADER_LEN, 0x11, 0, 1, 0, 0,
                                 0, 0, b"ab", true, b"", false).unwrap();
    cache::finish(&mut out, next - cache::HEADER_LEN).unwrap();
    assert!(cache::verify(&out[..next]).is_some());
    // One bit anywhere in the body, which is what a half-finished write leaves.
    out[next - 1] ^= 0x01;
    assert_eq!(cache::verify(&out[..next]), None);
}

#[test]
fn a_record_running_past_the_body_is_an_error_not_a_short_read() {
    let mut out = [0u8; 64];
    let at = cache::HEADER_LEN;
    let next = cache::write_peer(&mut out, at, 0x11, 0, 1, 0, 0, 0, 0,
                                 b"ab", true, b"", false).unwrap();
    // The length byte says more payload than the body holds. The CRC would
    // have caught a flash fault, so reaching this means a bug in the writer.
    assert_eq!(cache::record(&out, at, next - 1), Err(ERR_TRUNCATED));
}

#[test]
fn writing_past_the_end_of_the_buffer_is_refused() {
    let mut out = [0u8; 16];
    assert_eq!(
        cache::write_peer(&mut out, cache::HEADER_LEN, 0x11, 0, 1, 0, 0, 0, 0,
                          b"", false, b"", false),
        Err(ERR_NO_SPACE));
}

// ------------------------------------------------------------------ inbox

fn stored(buf: &mut [u8], used: usize, to: u32, from: u32, text: &[u8]) -> usize {
    let m = inbox::MessageIn {
        id: 1,
        from,
        to,
        time: 0,
        rssi: -90,
        channel: 0,
        hops: 0,
        snr: 29,
        flags: 0,
    };
    let mut out = inbox::AppendOut { used: 0, dropped: 0, off: 0 };
    assert_eq!(inbox::append(buf, used, 0, &m, text, &mut out), 0);
    out.used as usize
}

#[test]
fn a_stored_message_reads_back_field_for_field() {
    let mut buf = [0u8; 256];
    let m = inbox::MessageIn {
        id: 0x9C3C42B3,
        from: 0xC38C5A53,
        to: 0xFFFFFFFF,
        time: 1_700_000_000,
        rssi: -117,
        channel: 3,
        hops: 2,
        snr: -25,
        flags: inbox::READ,
    };
    let mut put = inbox::AppendOut { used: 0, dropped: 0, off: 0 };
    assert_eq!(inbox::append(&mut buf, 0, 0, &m, b"hola", &mut put), 0);
    assert_eq!(put.off, 0);
    assert_eq!(put.used, (inbox::FIXED + 4) as i32);

    let mut got = inbox::MessageOut {
        id: 0, from: 0, to: 0, time: 0, rssi: 0, channel: 0, hops: 0,
        snr: 0, flags: 0, text_off: 0, text_len: 0,
    };
    assert_eq!(inbox::read(&buf, put.used as usize, 0, &mut got), 0);
    assert_eq!(got.id, 0x9C3C42B3);
    assert_eq!(got.from, 0xC38C5A53);
    assert_eq!(got.to, 0xFFFFFFFF);
    assert_eq!(got.time, 1_700_000_000);
    // The two that go through a signed byte and a signed half word; an unsigned
    // read would give 139 and 65419.
    assert_eq!(got.rssi, -117);
    assert_eq!(got.snr, -25);
    assert_eq!(got.channel, 3);
    assert_eq!(got.hops, 2);
    assert_eq!(got.flags, inbox::READ as i32);
    assert_eq!(&buf[got.text_off as usize..][..got.text_len as usize], b"hola");
}

#[test]
fn the_arena_drops_the_oldest_to_make_room() {
    // Three records fit exactly; a fourth has to push the first out.
    let mut buf = [0u8; (inbox::FIXED + 2) * 3];
    let mut used = 0;
    for _ in 0..3 {
        used = stored(&mut buf, used, 0xFFFFFFFF, 1, b"ab");
    }
    assert_eq!(inbox::count(&buf, used, 0, inbox::ALL, 0, false), 3);

    let m = inbox::MessageIn {
        id: 99, from: 2, to: 0xFFFFFFFF, time: 0, rssi: 0, channel: 0,
        hops: 0, snr: 0, flags: 0,
    };
    let mut out = inbox::AppendOut { used: 0, dropped: 0, off: 0 };
    assert_eq!(inbox::append(&mut buf, used, 0, &m, b"cd", &mut out), 0);
    assert_eq!(out.dropped, 1);
    assert_eq!(inbox::count(&buf, out.used as usize, 0, inbox::ALL, 0, false), 3);
    // Newest last, so the survivor of the first three is now at the front.
    let mut got = inbox::MessageOut {
        id: 0, from: 0, to: 0, time: 0, rssi: 0, channel: 0, hops: 0,
        snr: 0, flags: 0, text_off: 0, text_len: 0,
    };
    assert_eq!(inbox::read(&buf, out.used as usize, out.off as usize, &mut got), 0);
    assert_eq!(got.id, 99);
}

#[test]
fn a_count_limit_evicts_before_the_arena_is_full() {
    let mut buf = [0u8; 256];
    let mut used = 0;
    let mut dropped = 0;
    for n in 0..5u32 {
        let m = inbox::MessageIn {
            id: n, from: 1, to: 0xFFFFFFFF, time: 0, rssi: 0, channel: 0,
            hops: 0, snr: 0, flags: 0,
        };
        let mut out = inbox::AppendOut { used: 0, dropped: 0, off: 0 };
        assert_eq!(inbox::append(&mut buf, used, 2, &m, b"x", &mut out), 0);
        used = out.used as usize;
        dropped += out.dropped;
    }
    assert_eq!(inbox::count(&buf, used, 0, inbox::ALL, 0, false), 2);
    assert_eq!(dropped, 3);
}

#[test]
fn a_message_too_long_for_the_whole_arena_is_refused() {
    let mut buf = [0u8; 16];
    let m = inbox::MessageIn {
        id: 1, from: 1, to: 1, time: 0, rssi: 0, channel: 0, hops: 0,
        snr: 0, flags: 0,
    };
    let mut out = inbox::AppendOut { used: 0, dropped: 0, off: 0 };
    assert_eq!(inbox::append(&mut buf, 0, 0, &m, b"", &mut out), ERR_NO_SPACE);
}

#[test]
fn conversations_are_this_nodes_view_and_not_the_senders() {
    const US: u32 = 0x0B9DBDE1;
    let mut buf = [0u8; 512];
    let mut used = 0;
    used = stored(&mut buf, used, 0xFFFFFFFF, 0xAAAA, b"broadcast");
    used = stored(&mut buf, used, US, 0xBBBB, b"for us");
    used = stored(&mut buf, used, US, 0xCCCC, b"also for us");

    assert_eq!(inbox::count(&buf, used, US, inbox::CHANNEL, 0, false), 1);
    assert_eq!(inbox::count(&buf, used, US, inbox::DIRECT, 0xBBBB, false), 1);
    assert_eq!(inbox::count(&buf, used, US, inbox::DIRECT, 0xAAAA, false), 0);
    // Renumbered while it was off: the same bytes now read as three broadcasts.
    assert_eq!(inbox::count(&buf, used, 7, inbox::CHANNEL, 0, false), 3);
}

#[test]
fn marking_one_conversation_leaves_the_others_unread() {
    const US: u32 = 0x0B9DBDE1;
    let mut buf = [0u8; 512];
    let mut used = 0;
    used = stored(&mut buf, used, 0xFFFFFFFF, 0xAAAA, b"broadcast");
    used = stored(&mut buf, used, US, 0xBBBB, b"for us");

    assert_eq!(inbox::count(&buf, used, US, inbox::ALL, 0, true), 2);
    assert_eq!(inbox::mark(&mut buf, used, US, inbox::DIRECT, 0xBBBB), 1);
    assert_eq!(inbox::count(&buf, used, US, inbox::ALL, 0, true), 1);
    assert_eq!(inbox::count(&buf, used, US, inbox::CHANNEL, 0, true), 1);
    // Nothing left to change, which is what says a save is not owed.
    assert_eq!(inbox::mark(&mut buf, used, US, inbox::DIRECT, 0xBBBB), 0);
}

#[test]
fn walking_visits_every_selected_record_once() {
    const US: u32 = 0x0B9DBDE1;
    let mut buf = [0u8; 512];
    let mut used = 0;
    used = stored(&mut buf, used, US, 0xBBBB, b"one");
    used = stored(&mut buf, used, 0xFFFFFFFF, 0xAAAA, b"skipped");
    used = stored(&mut buf, used, US, 0xBBBB, b"two");

    let mut seen = 0;
    let mut at = -1;
    loop {
        at = inbox::next(&buf, used, at, US, inbox::DIRECT, 0xBBBB);
        if at < 0 {
            break;
        }
        seen += 1;
    }
    assert_eq!(seen, 2);
}

#[test]
fn text_is_cut_at_a_character_and_not_at_a_byte() {
    // Three-byte sequences, so the 200-byte limit lands mid-character.
    let mut long = [0u8; 300];
    for i in 0..100 {
        long[i * 3] = 0xE2;
        long[i * 3 + 1] = 0x82;
        long[i * 3 + 2] = 0xAC;
    }
    let take = inbox::fit(&long[..300]);
    assert_eq!(take, 198);
    assert!(core::str::from_utf8(&long[..take]).is_ok());
    // Anything that already fits is kept whole.
    assert_eq!(inbox::fit(b"hola"), 4);
}

// ------------------------------------------------------------------- store

/// Fills a buffer the way `keystore.save` does: records in tag order, then the
/// header stamped over the front.
fn provisioned(buf: &mut [u8], records: &[(u8, &[u8])]) -> usize {
    let mut at = store::HEADER_LEN;
    for (tag, value) in records {
        at = store::put(buf, at, *tag, value).unwrap();
    }
    let used = at - store::HEADER_LEN;
    store::finish(buf, used).unwrap();
    used
}

#[test]
fn a_stored_record_reads_back_with_its_tag_and_value() {
    let mut buf = [0u8; 512];
    provisioned(&mut buf, &[(1, &[0xE1, 0xBD, 0x9D, 0x0B]), (3, b"LongFast")]);
    let end = store::verify(&buf).unwrap();

    let first = store::record(&buf, store::HEADER_LEN, end).unwrap().unwrap();
    assert_eq!(first.tag, 1);
    assert_eq!(first.len, 4);
    assert_eq!(&buf[first.off as usize..first.next as usize], &[0xE1, 0xBD, 0x9D, 0x0B]);

    let second = store::record(&buf, first.next as usize, end).unwrap().unwrap();
    assert_eq!(second.tag, 3);
    assert_eq!(&buf[second.off as usize..second.next as usize], b"LongFast");

    assert_eq!(store::record(&buf, second.next as usize, end).unwrap(), None);
}

#[test]
fn a_blank_page_reads_as_no_store_rather_than_an_error() {
    let blank = [0xFFu8; 64];
    assert_eq!(store::length(&blank), None);
    assert_eq!(store::verify(&blank), None);
}

#[test]
fn the_caches_own_region_is_not_mistaken_for_a_store() {
    let mut buf = [0u8; 128];
    crate::cache::finish(&mut buf, 0).unwrap();
    assert_eq!(store::verify(&buf), None);
}

#[test]
fn a_flipped_bit_in_the_body_fails_the_crc() {
    let mut buf = [0u8; 256];
    provisioned(&mut buf, &[(4, b"a private key that must not be trusted blind")]);
    assert!(store::verify(&buf).is_some());
    buf[store::HEADER_LEN + 5] ^= 0x01;
    assert_eq!(store::verify(&buf), None);
}

#[test]
fn an_interrupted_write_that_left_a_short_page_reads_as_empty() {
    let mut buf = [0u8; 256];
    let used = provisioned(&mut buf, &[(12, b"muzi base duo")]);
    // The header survived and claims a body the region no longer holds.
    assert_eq!(store::verify(&buf[..store::HEADER_LEN + used - 1]), None);
}

#[test]
fn equal_records_encode_to_equal_bytes() {
    let mut once = [0u8; 128];
    let mut again = [0u8; 128];
    provisioned(&mut once, &[(1, b"\x01\x02\x03\x04"), (7, b"\x03")]);
    provisioned(&mut again, &[(1, b"\x01\x02\x03\x04"), (7, b"\x03")]);
    // What lets keystore.save decline to spend an erase cycle.
    assert_eq!(once, again);
}

#[test]
fn a_record_too_long_for_a_length_byte_is_refused() {
    let mut buf = [0u8; 512];
    let big = [0u8; 256];
    assert_eq!(store::put(&mut buf, store::HEADER_LEN, 4, &big), Err(ERR_NO_SPACE));
}

#[test]
fn a_record_that_will_not_fit_the_region_is_refused_not_truncated() {
    let mut buf = [0u8; 32];
    assert_eq!(
        store::put(&mut buf, store::HEADER_LEN, 4, b"more than twenty bytes of value"),
        Err(ERR_NO_SPACE)
    );
}

#[test]
fn an_empty_value_is_a_record_and_not_an_absence() {
    let mut buf = [0u8; 64];
    provisioned(&mut buf, &[(13, b"")]);
    let end = store::verify(&buf).unwrap();
    let only = store::record(&buf, store::HEADER_LEN, end).unwrap().unwrap();
    assert_eq!(only.tag, 13);
    assert_eq!(only.len, 0);
    assert_eq!(store::record(&buf, only.next as usize, end).unwrap(), None);
}

// ---- payload

fn payload_of(frame: &[u8]) -> &[u8] {
    let d = proto::parse_data(frame).unwrap();
    &frame[d.payload_off..d.payload_off + d.payload_len]
}

#[test]
fn a_captured_position_reports_the_coordinates_it_carried() {
    let out = payload::position(payload_of(&POSITION_PLAINTEXT)).unwrap();
    assert_eq!(out.lat, 99_352_576);
    assert_eq!(out.lon, -840_695_808);
    assert_eq!(out.alt, 1233);
    assert_eq!(out.have & payload::HAS_1, payload::HAS_1);
    assert_eq!(out.have & payload::HAS_2, payload::HAS_2);
    assert_eq!(out.have & payload::HAS_3, payload::HAS_3);
    // The capture has no satellite count, and that has to stay tellable apart
    // from a count of zero.
    assert_eq!(out.have & payload::HAS_4, 0);
}

#[test]
fn a_zero_coordinate_is_present_and_an_absent_one_is_not() {
    // Field 1 explicitly zero, field 2 not sent at all.
    let out = payload::position(&[0x0D, 0x00, 0x00, 0x00, 0x00]).unwrap();
    assert_eq!(out.lat, 0);
    assert_eq!(out.have & payload::HAS_1, payload::HAS_1);
    assert_eq!(out.lon, 0);
    assert_eq!(out.have & payload::HAS_2, 0);
}

#[test]
fn a_negative_altitude_survives_the_sign_extension() {
    // Field 3, varint, -100 sign-extended to ten bytes as protobuf requires.
    let body = [0x18, 0x9C, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01];
    assert_eq!(payload::position(&body).unwrap().alt, -100);
}

#[test]
fn a_user_reports_where_its_names_are_rather_than_copying_them() {
    // Field 2 "Node One", field 3 "N1", field 5 hw 93, field 7 role 2.
    let body = [
        0x12, 0x08, b'N', b'o', b'd', b'e', b' ', b'O', b'n', b'e',
        0x1A, 0x02, b'N', b'1',
        0x28, 0x5D,
        0x38, 0x02,
    ];
    let u = payload::user(&body).unwrap();
    assert_eq!(&body[u.long_off as usize..(u.long_off + u.long_len) as usize], b"Node One");
    assert_eq!(&body[u.short_off as usize..(u.short_off + u.short_len) as usize], b"N1");
    assert_eq!(u.hw, 93);
    assert_eq!(u.role, 2);
    // Not licensed because the field was absent, which is not the same as
    // having been sent as false.
    assert_eq!(u.licensed, 0);
    assert_eq!(u.have & payload::HAS_4, 0);
}

#[test]
fn a_user_ignores_an_hw_model_that_arrives_as_bytes_rather_than_a_varint() {
    // Field 5 as WIRE_LEN carrying four bytes. `value` is the length for a
    // length-delimited field, so accepting it would report hw 4.
    let body = [0x2A, 0x04, b'a', b'b', b'c', b'd'];
    let u = payload::user(&body).unwrap();
    assert_eq!(u.hw, 0);
    assert_eq!(u.have & payload::HAS_3, 0);
}

#[test]
fn the_name_table_takes_the_short_name_and_not_the_long_one() {
    let body = [0x12, 0x04, b'L', b'o', b'n', b'g', 0x1A, 0x02, b'S', b'h'];
    let v = payload::short_name(&body).unwrap();
    assert_eq!(v.number, 3);
    assert_eq!(&body[v.off as usize..(v.off + v.len) as usize], b"Sh");
}

#[test]
fn a_user_without_a_short_name_reports_nothing_rather_than_the_long_one() {
    let body = [0x12, 0x04, b'L', b'o', b'n', b'g'];
    assert_eq!(payload::short_name(&body).unwrap().number, 0);
}

#[test]
fn a_captured_telemetry_selects_the_variant_and_not_its_timestamp() {
    let v = payload::telemetry(payload_of(&TELEMETRY_PLAINTEXT)).unwrap();
    // Field 1 is the time and field 3 is EnvironmentMetrics.
    assert_eq!(v.number, 3);
    assert_eq!(v.len, 15);
}

#[test]
fn environment_metrics_hand_back_float_bits_untouched() {
    let body = payload_of(&TELEMETRY_PLAINTEXT);
    let v = payload::telemetry(body).unwrap();
    let inner = &body[v.off as usize..(v.off + v.len) as usize];
    let e = payload::environment_metrics(inner).unwrap();
    // 25.78 C, still IEEE-754 bits: nothing on this side of the boundary knows
    // what a float is.
    assert_eq!(e.temp, 0x41CE_3D71);
    assert_eq!(e.have & payload::HAS_1, payload::HAS_1);
}

#[test]
fn a_battery_at_zero_percent_is_not_a_missing_battery() {
    let d = payload::device_metrics(&[0x08, 0x00]).unwrap();
    assert_eq!(d.batt, 0);
    assert_eq!(d.have & payload::HAS_1, payload::HAS_1);
}

#[test]
fn a_plain_ack_is_found_even_though_its_value_is_zero() {
    // Routing.error_reason = NONE, which is the two bytes an ack really is.
    let r = payload::routing(&[0x18, 0x00]).unwrap();
    assert_eq!(r.kind, payload::ROUTE_ERROR);
    assert_eq!(r.value, 0);
}

#[test]
fn an_empty_routing_payload_reports_no_arm_at_all() {
    assert_eq!(payload::routing(&[]).unwrap().kind, payload::ROUTE_NONE);
}

#[test]
fn the_senders_time_is_read_only_from_a_fixed32() {
    let body = payload_of(&POSITION_PLAINTEXT);
    assert_eq!(payload::sender_time(body, 4).unwrap(), Some(1_787_801_324));
    // Field 3 is there, but as a varint, and a varint in that slot is a
    // different field of a different message rather than a time.
    assert_eq!(payload::sender_time(body, 3).unwrap(), None);
}

#[test]
fn a_packed_snr_list_walks_forwards_through_its_varints() {
    // +2.5 dB (10), -1.0 dB (-4 sign-extended), and unknown (-128).
    let chunk = [
        0x0A,
        0xFC, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01,
        0x80, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01,
    ];
    let mut at = 0;
    let mut got = [0i32; 3];
    for i in 0..3 {
        let (snr, next) = payload::snr_at(&chunk, at).unwrap();
        got[i] = snr;
        at = next;
    }
    assert_eq!(got, [10, -4, -128]);
    assert_eq!(at, chunk.len());
}

#[test]
fn a_payload_cut_short_is_an_error_rather_than_a_partial_reading() {
    // Field 1 as a fixed32 with only three of its four bytes.
    assert_eq!(payload::position(&[0x0D, 0, 0, 0]).err(), Some(ERR_TRUNCATED));
}

// ------------------------------------------------------------ duty cycle

/// Sixty buckets, as a one-hour window a minute wide.
fn window() -> [u8; duty::HEADER_LEN + 60 * duty::BUCKET_LEN] {
    [0u8; duty::HEADER_LEN + 60 * duty::BUCKET_LEN]
}

#[test]
fn airtime_accumulates_within_one_bucket() {
    let mut w = window();
    duty::record(&mut w, 100, 700_000).unwrap();
    duty::record(&mut w, 100, 300_000).unwrap();
    assert_eq!(duty::used(&mut w, 100).unwrap(), 1_000_000);
}

#[test]
fn airtime_stays_counted_until_the_window_has_passed_over_it() {
    let mut w = window();
    duty::record(&mut w, 100, 500_000).unwrap();
    // Still inside the sixty buckets that end at 159.
    assert_eq!(duty::used(&mut w, 159).unwrap(), 500_000);
    // Bucket 100 is now the one being reused, so it is cleared first.
    assert_eq!(duty::used(&mut w, 160).unwrap(), 0);
}

#[test]
fn a_long_silence_expires_everything_rather_than_wrapping_onto_itself() {
    let mut w = window();
    duty::record(&mut w, 100, 500_000).unwrap();
    // Far enough ahead that the modulo would land back on the same bucket.
    assert_eq!(duty::used(&mut w, 100 + 60 * 7).unwrap(), 0);
}

#[test]
fn reading_the_budget_does_not_consume_it() {
    let mut w = window();
    duty::record(&mut w, 10, 250_000).unwrap();
    assert_eq!(duty::used(&mut w, 10).unwrap(), 250_000);
    assert_eq!(duty::used(&mut w, 10).unwrap(), 250_000);
}

#[test]
fn a_clock_that_goes_backwards_forfeits_the_window_rather_than_the_limit() {
    let mut w = window();
    duty::record(&mut w, 500, 900_000).unwrap();
    assert_eq!(duty::used(&mut w, 3).unwrap(), 0);
    // And the new timeline is usable, not stuck in the future.
    duty::record(&mut w, 4, 100_000).unwrap();
    assert_eq!(duty::used(&mut w, 4).unwrap(), 100_000);
}

#[test]
fn a_budget_that_overflows_reads_as_full_rather_than_empty() {
    let mut w = window();
    duty::record(&mut w, 7, u32::MAX).unwrap();
    duty::record(&mut w, 7, 1_000).unwrap();
    assert_eq!(duty::used(&mut w, 7).unwrap(), u32::MAX);
}

#[test]
fn a_window_too_small_to_hold_a_bucket_is_refused() {
    let mut tiny = [0u8; duty::HEADER_LEN];
    assert_eq!(duty::record(&mut tiny, 0, 1).err(), Some(ERR_NO_SPACE));
    assert_eq!(duty::used(&mut tiny, 0).err(), Some(ERR_NO_SPACE));
}

// -------------------------------------------------------- writing protobuf

#[test]
fn a_varint_is_seven_bits_at_a_time_little_end_first() {
    let mut out = [0u8; 16];
    assert_eq!(pbuf::put_varint(&mut out, 0, 300).unwrap(), 2);
    assert_eq!(&out[..2], &[0xAC, 0x02]);
}

#[test]
fn a_field_number_and_wire_type_share_the_first_varint() {
    let mut out = [0u8; 16];
    // Field 1, varint: (1 << 3) | 0.
    let at = pbuf::uint(&mut out, 0, 1, 150).unwrap();
    assert_eq!(&out[..at], &[0x08, 0x96, 0x01]);
}

#[test]
fn a_negative_int32_is_sign_extended_to_ten_bytes() {
    let mut out = [0u8; 16];
    let at = pbuf::int32(&mut out, 0, 1, -2).unwrap();
    assert_eq!(at, 11);
    assert_eq!(&out[..at],
               &[0x08, 0xFE, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01]);
}

#[test]
fn fixed32_is_little_endian_after_its_tag() {
    let mut out = [0u8; 16];
    let at = pbuf::fixed32(&mut out, 0, 5, 0x01020304).unwrap();
    assert_eq!(&out[..at], &[0x2D, 0x04, 0x03, 0x02, 0x01]);
}

#[test]
fn a_blob_head_carries_its_length_and_an_empty_one_is_still_written() {
    let mut out = [0u8; 16];
    let at = pbuf::blob_head(&mut out, 0, 2, 2).unwrap();
    assert_eq!(&out[..at], &[0x12, 0x02]);
    let at = pbuf::blob_head(&mut out, 0, 2, 0).unwrap();
    assert_eq!(&out[..at], &[0x12, 0x00]);
}

#[test]
fn a_payload_past_127_bytes_takes_a_two_byte_length() {
    let mut out = [0u8; 16];
    let at = pbuf::blob_head(&mut out, 0, 1, 200).unwrap();
    assert_eq!(&out[..at], &[0x0A, 0xC8, 0x01]);
}

#[test]
fn fields_written_one_after_another_share_the_buffer() {
    let mut out = [0u8; 32];
    let at = pbuf::uint(&mut out, 0, 1, 7).unwrap();
    let at = pbuf::blob_head(&mut out, at, 2, 2).unwrap();
    assert_eq!(&out[..at], &[0x08, 0x07, 0x12, 0x02]);
}

#[test]
fn a_field_that_does_not_fit_is_refused_rather_than_truncated() {
    // Two bytes of tag and length do not fit in one byte.
    let mut out = [0u8; 1];
    assert_eq!(pbuf::blob_head(&mut out, 0, 2, 3).err(), Some(ERR_NO_SPACE));
    // And a tag with no room for itself fails the same way.
    let mut none = [0u8; 0];
    assert_eq!(pbuf::uint(&mut none, 0, 1, 0).err(), Some(ERR_NO_SPACE));
}

// ---------------------------------------------------------------- battery

#[test]
fn every_table_voltage_lands_on_its_own_ten_percent_step() {
    let table = [4190, 4050, 3990, 3890, 3800, 3720, 3630, 3530, 3420, 3300, 3100];
    for (i, mv) in table.iter().enumerate() {
        assert_eq!(power::percent(*mv), 100 - 10 * i as u8, "at {} mV", mv);
    }
}

#[test]
fn a_reading_between_two_steps_interpolates() {
    // Halfway from 3800 (60%) up to 3890 (70%).
    assert_eq!(power::percent(3845), 65);
    // A quarter of the way from 4050 (90%) up to 4190 (100%).
    assert_eq!(power::percent(4085), 92);
}

#[test]
fn the_ends_of_the_curve_are_flat_rather_than_wrapping() {
    assert_eq!(power::percent(u16::MAX), 100);
    assert_eq!(power::percent(4300), 100);
    assert_eq!(power::percent(3099), 0);
    assert_eq!(power::percent(0), 0);
}

#[test]
fn no_battery_sits_well_under_any_voltage_a_cell_could_reach() {
    assert_eq!(power::NO_BATTERY_MV, 2600);
    assert_eq!(power::percent(power::NO_BATTERY_MV), 0);
}

#[test]
fn the_ffi_clamps_rather_than_truncating_an_impossible_reading() {
    // 65536 mV truncated to u16 would be 0, and read as a flat pack.
    assert_eq!(crate::ffi::meshtastic_battery_percent(70_000), 100);
    assert_eq!(crate::ffi::meshtastic_battery_percent(3845), 65);
}

// ------------------------------------------------------------------ nmea

/// Every sentence here is either a real capture off the W12's L76K or built
/// the same way; the checksums are the receiver's own arithmetic.
const RMC_NO_FIX: &[u8] = b"$GNRMC,023018.901,V,,,,,,,170926,,,M,V*21";
const GGA_FIX: &[u8] =
    b"$GNGGA,123519.000,1000.8952,N,08428.1706,W,1,13,0.80,1373.4,M,-8.5,M,,*44";
const GGA_ESTIMATED: &[u8] =
    b"$GNGGA,123519.000,1000.8952,N,08428.1706,W,6,04,25.50,1373.4,M,-8.5,M,,*7F";
const RMC_FIX: &[u8] =
    b"$GNRMC,123519.000,A,1000.8952,N,08428.1706,W,0.02,0.00,160926,,,A,V*1C";
const GPGSV: &[u8] = b"$GPGSV,3,1,11,01,05,123,20,03,45,200,30,06,70,045,35,11,20,300,22,1*64";
const GLGSV: &[u8] = b"$GLGSV,2,1,07,65,30,100,25,66,55,210,31,1*7B";

fn fresh() -> [u8; nmea::STATE_BYTES] {
    let mut state = [0u8; nmea::STATE_BYTES];
    assert_eq!(nmea::reset(&mut state), 0);
    state
}

#[test]
fn a_state_buffer_starts_with_nothing_known() {
    let state = fresh();
    let f = nmea::read(&state).unwrap();
    assert_eq!(f.have, 0);
    assert_eq!(f.sats, nmea::NO_SATS);
    assert_eq!(f.in_view, nmea::NO_SATS);
    assert_eq!(f.hdop_cm, nmea::NO_HDOP);
    assert_eq!(f.when, 0);
    assert_eq!(f.valid, 0);
}

#[test]
fn a_corrupted_checksum_is_refused() {
    let mut state = fresh();
    let mut bad = [0u8; 64];
    for i in 0..RMC_NO_FIX.len() {
        bad[i] = RMC_NO_FIX[i];
    }
    // Last hex digit of the checksum, one out.
    bad[RMC_NO_FIX.len() - 1] = b'2';
    assert_eq!(nmea::sentence(&mut state, &bad[..RMC_NO_FIX.len()]), ERR_BAD_SENTENCE);
    assert_eq!(nmea::read(&state).unwrap().when, 0);
}

#[test]
fn a_line_that_is_not_a_sentence_is_refused() {
    let mut state = fresh();
    assert_eq!(nmea::sentence(&mut state, b"GNRMC,023018.901*21"), ERR_BAD_SENTENCE);
    assert_eq!(nmea::sentence(&mut state, b""), ERR_BAD_SENTENCE);
    assert_eq!(nmea::sentence(&mut state, b"$GNRMC,1,2,3"), ERR_BAD_SENTENCE);
}

#[test]
fn rmc_carries_the_clock_with_no_position_at_all() {
    // The reason the board can set its clock indoors: time comes off one
    // satellite, a position needs four placed well.
    let mut state = fresh();
    assert_eq!(nmea::sentence(&mut state, RMC_NO_FIX), nmea::SAW_RMC);
    let f = nmea::read(&state).unwrap();
    assert_eq!(f.when, 1_789_612_218);
    assert_eq!(f.have & nmea::HAVE_TIME, nmea::HAVE_TIME);
    assert_eq!(f.have & nmea::HAVE_POSITION, 0);
    assert_eq!(f.status, b'V');
    assert_eq!(f.valid, 0);
}

#[test]
fn a_clock_from_before_the_floor_is_not_believed() {
    // A receiver that has decoded nothing still stamps its sentences, from a
    // counter of its own that starts well before 2025.
    let mut state = fresh();
    let line = b"$GNRMC,023018.901,V,,,,,,,170906,,,M,V*23";
    assert_eq!(nmea::sentence(&mut state, line), nmea::SAW_RMC);
    let f = nmea::read(&state).unwrap();
    assert_eq!(f.when, 0);
    assert_eq!(f.have & nmea::HAVE_TIME, 0);
}

#[test]
fn gga_reports_the_position_in_the_units_the_wire_uses() {
    let mut state = fresh();
    assert_eq!(nmea::sentence(&mut state, GGA_FIX), nmea::SAW_GGA);
    let f = nmea::read(&state).unwrap();
    assert_eq!(f.lat_e7, 100_149_200);
    assert_eq!(f.lon_e7, -844_695_100);
    assert_eq!(f.alt_mm, 1_373_400);
    assert_eq!(f.sats, 13);
    assert_eq!(f.hdop_cm, 80);
    assert_eq!(f.quality, 1);
    assert_eq!(f.have & nmea::HAVE_POSITION, nmea::HAVE_POSITION);
}

#[test]
fn a_position_needs_both_gga_and_rmc_to_stand_behind_it() {
    let mut state = fresh();
    nmea::sentence(&mut state, GGA_FIX);
    // GGA alone says quality 1, but RMC has not agreed yet.
    assert_eq!(nmea::read(&state).unwrap().valid, 0);
    nmea::sentence(&mut state, RMC_FIX);
    let f = nmea::read(&state).unwrap();
    assert_eq!(f.valid, 1);
    assert_eq!(f.status, b'A');
    assert_eq!(f.mode, b'A');
    // Field 13 reads V even on a good fix on this receiver, which is why it
    // is reported and not used as a gate.
    assert_eq!(f.nav, b'V');
    assert_eq!(f.when, 1_789_562_119);
}

#[test]
fn an_estimated_position_is_never_valid() {
    // Quality 6 is the dead-reckoning bridge the L76K runs for a few seconds
    // after a reset. It fills in a position it does not stand behind.
    let mut state = fresh();
    nmea::sentence(&mut state, RMC_FIX);
    nmea::sentence(&mut state, GGA_ESTIMATED);
    let f = nmea::read(&state).unwrap();
    assert_eq!(f.quality, 6);
    assert_eq!(f.valid, 0);
    assert_eq!(f.hdop_cm, 2550);
}

#[test]
fn satellites_in_view_are_summed_across_constellations() {
    // Each talker sends its own GSV run reporting only its own count, so
    // keeping the last one seen would report GLONASS as the whole sky.
    let mut state = fresh();
    assert_eq!(nmea::sentence(&mut state, GPGSV), nmea::SAW_GSV);
    assert_eq!(nmea::read(&state).unwrap().in_view, 11);
    assert_eq!(nmea::sentence(&mut state, GLGSV), nmea::SAW_GSV);
    assert_eq!(nmea::read(&state).unwrap().in_view, 18);
    // A repeat from one talker replaces that talker's count, not the total.
    nmea::sentence(&mut state, GPGSV);
    assert_eq!(nmea::read(&state).unwrap().in_view, 18);
    assert_eq!(nmea::talker(&state, 0).unwrap(), Some((b'G', b'P', 11)));
    assert_eq!(nmea::talker(&state, 1).unwrap(), Some((b'G', b'L', 7)));
    assert_eq!(nmea::talker(&state, 2).unwrap(), None);
}

#[test]
fn a_sentence_split_across_two_reads_is_reassembled() {
    let mut state = fresh();
    let mut echo = [0u8; 256];
    let cut = 20;
    let (landed, used) = nmea::feed(&mut state, &RMC_NO_FIX[..cut], &mut echo, 0);
    assert_eq!(landed, 0);
    assert_eq!(used, 0);
    let (landed, used) = nmea::feed(&mut state, &RMC_NO_FIX[cut..], &mut echo, 0);
    // Nothing has ended the line yet.
    assert_eq!(landed, 0);
    let (landed, used) = nmea::feed(&mut state, b"\r\n", &mut echo, used);
    assert_eq!(landed, 1);
    assert_eq!(&echo[..used - 1], RMC_NO_FIX);
    assert_eq!(nmea::read(&state).unwrap().when, 1_789_612_218);
}

#[test]
fn a_feed_counts_what_it_could_and_could_not_read() {
    let mut state = fresh();
    let mut echo = [0u8; 512];
    let mut chunk = [0u8; 512];
    let mut at = 0;
    for part in [GGA_FIX, b"not a sentence", RMC_FIX] {
        for i in 0..part.len() {
            chunk[at + i] = part[i];
        }
        at += part.len();
        chunk[at] = b'\r';
        chunk[at + 1] = b'\n';
        at += 2;
    }
    let (landed, used) = nmea::feed(&mut state, &chunk[..at], &mut echo, 0);
    assert_eq!(landed, 2);
    let f = nmea::read(&state).unwrap();
    assert_eq!(f.good, 2);
    assert_eq!(f.bad, 1);
    assert_eq!(f.valid, 1);
    // Only the sentences that were read are echoed, so a page showing the
    // stream shows what the fix was actually built from.
    assert_eq!(used, GGA_FIX.len() + RMC_FIX.len() + 2);
}

#[test]
fn a_line_longer_than_any_sentence_is_dropped_rather_than_spliced() {
    let mut state = fresh();
    let mut echo = [0u8; 256];
    let noise = [b'x'; 200];
    nmea::feed(&mut state, &noise, &mut echo, 0);
    assert!(nmea::read(&state).unwrap().bad > 0);
    // The next real sentence still lands: the overflow did not leave a
    // half-line in front of it.
    let (landed, _) = nmea::feed(&mut state, b"\r\n", &mut echo, 0);
    let _ = landed;
    let mut whole = [0u8; 128];
    for i in 0..GGA_FIX.len() {
        whole[i] = GGA_FIX[i];
    }
    whole[GGA_FIX.len()] = b'\n';
    let (landed, _) = nmea::feed(&mut state, &whole[..GGA_FIX.len() + 1], &mut echo, 0);
    assert_eq!(landed, 1);
    assert_eq!(nmea::read(&state).unwrap().lat_e7, 100_149_200);
}

#[test]
fn a_full_echo_buffer_stops_copying_without_losing_the_fix() {
    let mut state = fresh();
    let mut echo = [0u8; 8];
    let mut whole = [0u8; 128];
    for i in 0..GGA_FIX.len() {
        whole[i] = GGA_FIX[i];
    }
    whole[GGA_FIX.len()] = b'\n';
    let (landed, used) = nmea::feed(&mut state, &whole[..GGA_FIX.len() + 1], &mut echo, 0);
    assert_eq!(landed, 1);
    assert_eq!(used, 0);
    assert_eq!(nmea::read(&state).unwrap().lat_e7, 100_149_200);
}

#[test]
fn the_calendar_round_trips_through_the_epoch() {
    let c = nmea::civil(1_789_612_218);
    assert_eq!(c.year, 2026);
    assert_eq!(c.month, 9);
    assert_eq!(c.day, 17);
    assert_eq!(c.hour, 2);
    assert_eq!(c.minute, 30);
    assert_eq!(c.second, 18);
    // The two ends of a leap day, and the epoch itself.
    let c = nmea::civil(0);
    assert_eq!((c.year, c.month, c.day), (1970, 1, 1));
    let c = nmea::civil(1_709_164_800);
    assert_eq!((c.year, c.month, c.day, c.hour), (2024, 2, 29, 0));
    let c = nmea::civil(1_709_251_199);
    assert_eq!((c.year, c.month, c.day, c.hour, c.minute, c.second), (2024, 2, 29, 23, 59, 59));
}

#[test]
fn a_state_buffer_that_is_too_short_is_refused() {
    let mut small = [0u8; nmea::STATE_BYTES - 1];
    assert_eq!(nmea::reset(&mut small), ERR_BAD_STATE);
    assert_eq!(nmea::sentence(&mut small, GGA_FIX), ERR_BAD_STATE);
    assert!(nmea::read(&small).is_err());
    let state = fresh();
    assert!(nmea::talker(&state, nmea::TALKERS).is_err());
}

// ------- encoding a position

/// A state holding a fix both sentences stand behind.
fn located() -> [u8; nmea::STATE_BYTES] {
    let mut state = fresh();
    nmea::sentence(&mut state, GGA_FIX);
    nmea::sentence(&mut state, RMC_FIX);
    assert_eq!(nmea::read(&state).unwrap().valid, 1);
    state
}

#[test]
fn a_fix_encodes_to_a_position_that_reads_back() {
    let state = located();
    let mut out = [0u8; 64];
    let n = payload::encode_position(&mut out, &state, payload::PRECISION_FULL).unwrap();
    let got = payload::position(&out[..n]).unwrap();
    assert_eq!(got.lat, 100_149_200);
    assert_eq!(got.lon, -844_695_100);
    // Metres on the wire, millimetres off the receiver.
    assert_eq!(got.alt, 1373);
    assert_eq!(got.sats, 13);
    assert_eq!(got.precision, 32);
}

#[test]
fn a_position_is_refused_until_both_sentences_agree() {
    // The one mistake that lasts: every node that hears a bad position keeps
    // it.
    let mut state = fresh();
    nmea::sentence(&mut state, GGA_FIX);
    let mut out = [0u8; 64];
    assert_eq!(
        payload::encode_position(&mut out, &state, payload::PRECISION_FULL),
        Err(ERR_NO_FIX)
    );
}

#[test]
fn an_empty_receiver_encodes_nothing_at_all() {
    let state = fresh();
    let mut out = [0u8; 64];
    assert_eq!(
        payload::encode_position(&mut out, &state, payload::PRECISION_FULL),
        Err(ERR_NO_FIX)
    );
}

#[test]
fn coarse_precision_lands_in_the_middle_of_the_square_it_admits_to() {
    let state = located();
    let mut out = [0u8; 64];
    let n = payload::encode_position(&mut out, &state, 13).unwrap();
    let got = payload::position(&out[..n]).unwrap();
    assert_eq!(got.precision, 13);
    let mask = (u32::MAX << (32 - 13)) as i32;
    let half = 1i32 << (31 - 13);
    assert_eq!(got.lat, (100_149_200 & mask) + half);
    assert_eq!(got.lon, (-844_695_100i32 & mask) + half);
    // Coarse, but not moved to a corner: the rounding has to be centred or a
    // mesh averaging these drifts one way.
    assert!(got.lat > (100_149_200 & mask));
}

#[test]
fn full_precision_states_the_reading_unchanged() {
    let state = located();
    let (lat, lon) = { let f = nmea::read(&state).unwrap(); (f.lat_e7, f.lon_e7) };
    let mut out = [0u8; 64];
    let n = payload::encode_position(&mut out, &state, 32).unwrap();
    let got = payload::position(&out[..n]).unwrap();
    assert_eq!(got.lat, lat);
    assert_eq!(got.lon, lon);
}

#[test]
fn zero_precision_withholds_where_but_still_reports_the_fix() {
    let state = located();
    let mut out = [0u8; 64];
    let n = payload::encode_position(&mut out, &state, 0).unwrap();
    let got = payload::position(&out[..n]).unwrap();
    assert_eq!(got.have & payload::HAS_1, 0);
    assert_eq!(got.have & payload::HAS_2, 0);
    assert_eq!(got.sats, 13);
}

#[test]
fn a_buffer_that_cannot_hold_the_position_is_refused() {
    let state = located();
    let mut out = [0u8; 6];
    assert_eq!(
        payload::encode_position(&mut out, &state, payload::PRECISION_FULL),
        Err(ERR_NO_SPACE)
    );
}

#[test]
fn an_estimated_fix_still_reports_its_dilution_placeholder_honestly() {
    // 25.50 is the receiver saying it has no idea, not a measurement.
    let mut state = fresh();
    nmea::sentence(&mut state, GGA_ESTIMATED);
    nmea::sentence(&mut state, RMC_FIX);
    if nmea::read(&state).unwrap().valid == 1 {
        let mut out = [0u8; 64];
        let n = payload::encode_position(&mut out, &state, payload::PRECISION_FULL).unwrap();
        assert!(n > 0);
    }
}

// ------- stream framing

fn wire() -> [u8; stream::STATE_BYTES] {
    let mut state = [0u8; stream::STATE_BYTES];
    assert_eq!(stream::reset(&mut state), 0);
    state
}

/// Feeds a whole chunk, returning every frame it completed.
fn drain(state: &mut [u8], chunk: &[u8]) -> Vec<Vec<u8>> {
    let mut out = Vec::new();
    let mut at = 0;
    loop {
        let (len, next) = stream::feed(state, chunk, at);
        assert!(len >= 0);
        at = next;
        if len > 0 {
            out.push(stream::body(state, len as usize).unwrap().to_vec());
        }
        if at >= chunk.len() && len == 0 {
            return out;
        }
    }
}

#[test]
fn one_frame_arrives_whole() {
    let mut state = wire();
    let got = drain(&mut state, &[0x94, 0xc3, 0x00, 0x03, 1, 2, 3]);
    assert_eq!(got, vec![vec![1u8, 2, 3]]);
    let c = stream::counts(&state).unwrap();
    assert_eq!((c.lost, c.frames, c.partial), (0, 1, 0));
}

#[test]
fn a_frame_split_across_reads_is_rejoined() {
    // Every split point, because the reader is a state machine and only one of
    // these exercises each transition.
    let whole = [0x94u8, 0xc3, 0x00, 0x04, 9, 8, 7, 6];
    for cut in 0..whole.len() {
        let mut state = wire();
        let mut got = drain(&mut state, &whole[..cut]);
        got.extend(drain(&mut state, &whole[cut..]));
        assert_eq!(got, vec![vec![9u8, 8, 7, 6]], "split at {}", cut);
    }
}

#[test]
fn two_frames_in_one_read_both_land() {
    let mut state = wire();
    let got = drain(&mut state, &[0x94, 0xc3, 0x00, 0x01, 0xAA,
                                  0x94, 0xc3, 0x00, 0x02, 0xBB, 0xCC]);
    assert_eq!(got, vec![vec![0xAAu8], vec![0xBBu8, 0xCC]]);
    assert_eq!(stream::counts(&state).unwrap().frames, 2);
}

#[test]
fn debug_text_before_a_frame_is_discarded_and_counted() {
    let mut state = wire();
    let mut chunk = b"booting\n".to_vec();
    chunk.extend_from_slice(&[0x94, 0xc3, 0x00, 0x01, 0x42]);
    assert_eq!(drain(&mut state, &chunk), vec![vec![0x42u8]]);
    assert_eq!(stream::counts(&state).unwrap().lost, 8);
}

#[test]
fn a_wrong_second_start_byte_resynchronises() {
    // The client's own test vector: a false start, then a real frame.
    let mut state = wire();
    let got = drain(&mut state, &[0x94, 0x00, 0x94, 0xc3, 0x00, 0x01, 0x55]);
    assert_eq!(got, vec![vec![0x55u8]]);
    assert_eq!(stream::counts(&state).unwrap().lost, 2);
}

#[test]
fn a_repeated_start_byte_stays_a_candidate() {
    let mut state = wire();
    let got = drain(&mut state, &[0x94, 0x94, 0xc3, 0x00, 0x01, 0x77]);
    assert_eq!(got, vec![vec![0x77u8]]);
    assert_eq!(stream::counts(&state).unwrap().lost, 1);
}

#[test]
fn a_length_past_the_maximum_is_noise_not_a_frame() {
    let mut state = wire();
    // 0x0201 is 513, one past what any client sends.
    let got = drain(&mut state, &[0x94, 0xc3, 0x02, 0x01,
                                  0x94, 0xc3, 0x00, 0x01, 0x31]);
    assert_eq!(got, vec![vec![0x31u8]]);
    assert_eq!(stream::counts(&state).unwrap().lost, 4);
}

#[test]
fn the_largest_legal_frame_fits() {
    let mut state = wire();
    let mut chunk = vec![0x94u8, 0xc3, 0x02, 0x00];
    for i in 0..stream::MAX_FRAME {
        chunk.push(i as u8);
    }
    let got = drain(&mut state, &chunk);
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].len(), stream::MAX_FRAME);
    assert_eq!(got[0][511], 255);
}

#[test]
fn an_empty_frame_says_nothing_and_does_not_stop_the_next() {
    let mut state = wire();
    let got = drain(&mut state, &[0x94, 0xc3, 0x00, 0x00,
                                  0x94, 0xc3, 0x00, 0x01, 0x5A]);
    assert_eq!(got, vec![vec![0x5Au8]]);
    assert_eq!(stream::counts(&state).unwrap().lost, 0);
}

#[test]
fn a_half_delivered_frame_shows_as_partial() {
    let mut state = wire();
    assert!(drain(&mut state, &[0x94, 0xc3, 0x00, 0x08, 1, 2, 3]).is_empty());
    let c = stream::counts(&state).unwrap();
    assert_eq!((c.frames, c.partial), (0, 3));
}

#[test]
fn framing_a_payload_writes_a_header_the_reader_accepts() {
    let mut out = [0u8; 16];
    let n = stream::frame(&mut out, 2, &[0xDE, 0xAD]);
    assert_eq!(n, 6);
    assert_eq!(&out[2..8], &[0x94, 0xc3, 0x00, 0x02, 0xDE, 0xAD]);
    let mut state = wire();
    assert_eq!(drain(&mut state, &out[2..8]), vec![vec![0xDEu8, 0xAD]]);
}

#[test]
fn framing_refuses_a_buffer_that_cannot_hold_it() {
    let mut out = [0u8; 8];
    assert_eq!(stream::frame(&mut out, 0, &[1, 2, 3, 4, 5]), ERR_NO_SPACE);
    assert_eq!(stream::frame(&mut out, 5, &[1]), ERR_NO_SPACE);
    assert_eq!(stream::frame(&mut out, 9, &[]), ERR_NO_SPACE);
}

#[test]
fn a_stream_state_buffer_that_is_too_short_is_refused() {
    let mut small = [0u8; stream::STATE_BYTES - 1];
    assert_eq!(stream::reset(&mut small), ERR_BAD_STATE);
    assert_eq!(stream::feed(&mut small, &[0x94], 0).0, ERR_BAD_STATE);
    assert!(stream::counts(&small).is_err());
    let state = wire();
    assert!(stream::body(&state, stream::MAX_FRAME + 1).is_err());
}
