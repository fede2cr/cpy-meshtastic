// Native CircuitPython module `meshtastic`: a thin bridge between MicroPython
// objects and the Rust decoder in ../src.
//
// Like the `lr1121` module this holds no state, but for a simpler reason: every
// function here is pure. Bytes in, numbers out. Nothing touches the radio.
//
// The two modules are separate .mpy files and cannot link against each other's
// symbols, so they never call one another; the Python layer owns both.
//
// This file builds two ways. As a natmod it loads from the drive and its code
// sits in the GC heap; built into the firmware through USER_C_MODULES it sits
// in flash and costs no heap at all. dynruntime.h spells its helpers with the
// same names as the real runtime, so only the registration at the bottom of
// this file differs between the two.

#if defined(MICROPY_ENABLE_DYNRUNTIME) && MICROPY_ENABLE_DYNRUNTIME
#include "py/dynruntime.h"
#else
#include "py/runtime.h"
#endif

#if defined(MICROPY_ENABLE_DYNRUNTIME) && MICROPY_ENABLE_DYNRUNTIME && defined(__XTENSA__)
// rustc's Xtensa backend calls memcpy for block moves that its Thumb backend
// expands in place, so the ESP32-S3 build of the crate needs a memcpy and the
// nRF one does not. A natmod links against neither libc nor compiler_builtins:
// LINK_RUNTIME supplies libgcc and libm, and memcpy is in neither. Taking
// compiler_builtins out of the archive would supply it and add 130 kB, because
// that object is one code generation unit and comes whole.
//
// The runtime already exports a memmove, which is a memcpy with a guarantee
// this caller does not need. Going through the function pointer also stops the
// compiler from recognising the body as a memcpy and calling this back into
// itself.
void *memcpy(void *dest, const void *src, size_t n) {
    return mp_fun_table.memmove_(dest, src, n);
}
#endif

// Must match `struct HeaderOut` in ../src/ffi.rs.
typedef struct _mesh_header_t {
    uint32_t to;
    uint32_t from;
    uint32_t id;
    uint8_t flags;
    uint8_t channel;
    uint8_t next_hop;
    uint8_t relay_node;
    uint8_t hop_limit;
    uint8_t hop_start;
    uint8_t want_ack;
    uint8_t via_mqtt;
    uint8_t hops_away;
} mesh_header_t;

// Must match `struct PresetOut`.
typedef struct _mesh_preset_t {
    uint32_t bw_hz;
    uint8_t sf;
    uint8_t cr;
} mesh_preset_t;

// Must match `struct RegionOut`.
typedef struct _mesh_region_t {
    uint32_t start_hz;
    uint32_t end_hz;
    uint32_t spacing_hz;
    uint16_t duty_cycle_pct;
    uint8_t power_limit_dbm;
    uint8_t wide_lora;
} mesh_region_t;

// Must match `struct DataOut`.
typedef struct _mesh_data_t {
    uint32_t portnum;
    uint32_t payload_off;
    uint32_t payload_len;
    uint32_t dest;
    uint32_t source;
    uint32_t request_id;
    uint32_t reply_id;
    uint32_t emoji;
    uint32_t bitfield;
    uint8_t want_response;
    uint8_t has_bitfield;
} mesh_data_t;

// Must match `struct FieldOut`.
typedef struct _mesh_field_t {
    uint32_t value_lo;
    uint32_t value_hi;
    uint32_t number;
    uint32_t wire;
    uint32_t data_off;
    uint32_t data_len;
    uint32_t next;
} mesh_field_t;

// Must match `struct RowOut` in ../src/roster.rs, which is also the on-flash
// row layout: the same bytes are what meshcache saves.
typedef struct _mesh_row_t {
    uint32_t num;
    int32_t first;
    int32_t last;
    uint32_t seen;
    uint16_t count;
    uint8_t hops;
    uint8_t hw;
    uint8_t role;
    int8_t snr;
    int16_t rssi;
} mesh_row_t;

// Must match the out-structs in ../src/cache.rs. Offsets into the buffer that
// was handed in, rather than copies of the bytes: the caller already holds it.
typedef struct _mesh_record_t {
    uint32_t kind;
    uint32_t off;
    uint32_t len;
    uint32_t next;
} mesh_record_t;

typedef struct _mesh_cpeer_t {
    uint32_t num;
    uint32_t age;
    uint16_t count;
    uint8_t hops;
    uint8_t hw;
    uint8_t role;
    int8_t snr;
    uint16_t short_off;
    int16_t short_len;
    uint16_t long_off;
    int16_t long_len;
} mesh_cpeer_t;

typedef struct _mesh_cmessage_t {
    uint32_t id;
    uint32_t from;
    uint32_t to;
    uint32_t time;
    uint8_t flags;
    uint8_t channel;
    uint8_t hops;
    int8_t snr;
    uint32_t text_off;
    uint32_t text_len;
} mesh_cmessage_t;

// Must match the out-structs in ../src/payload.rs. `have` is a presence mask
// because zero is a legal latitude, altitude, role and battery level, so the
// bindings below turn it into None rather than letting a default look sent.
typedef struct _mesh_position_t {
    uint32_t have;
    int32_t lat;
    int32_t lon;
    int32_t alt;
    uint32_t sats;
    uint32_t precision;
} mesh_position_t;

typedef struct _mesh_user_t {
    uint32_t have;
    uint32_t long_off;
    uint32_t long_len;
    uint32_t short_off;
    uint32_t short_len;
    uint32_t hw;
    uint32_t role;
    uint32_t licensed;
} mesh_user_t;

// volt, chan and tx are IEEE-754 bits, not numbers: no float crosses here.
typedef struct _mesh_device_t {
    uint32_t have;
    uint32_t batt;
    uint32_t volt;
    uint32_t chan;
    uint32_t tx;
    uint32_t up;
} mesh_device_t;

typedef struct _mesh_env_t {
    uint32_t have;
    uint32_t temp;
    uint32_t rh;
    uint32_t hpa;
} mesh_env_t;

typedef struct _mesh_variant_t {
    uint32_t number;
    uint32_t off;
    uint32_t len;
} mesh_variant_t;

typedef struct _mesh_routing_t {
    uint32_t kind;
    uint32_t value;
} mesh_routing_t;

typedef struct _mesh_route_t {
    uint32_t have;
    uint32_t route_off;
    uint32_t route_len;
    uint32_t snr_out_off;
    uint32_t snr_out_len;
    uint32_t back_off;
    uint32_t back_len;
    uint32_t snr_back_off;
    uint32_t snr_back_len;
} mesh_route_t;

// Must match the structs in ../src/inbox.rs. Pre-scaled on the way in: SNR in
// quarter decibels and RSSI in whole ones, because nothing crosses this
// boundary as a float.
typedef struct _mesh_inmsg_t {
    uint32_t id;
    uint32_t from;
    uint32_t to;
    uint32_t time;
    int16_t rssi;
    uint8_t channel;
    uint8_t hops;
    int8_t snr;
    uint8_t flags;
} mesh_inmsg_t;

typedef struct _mesh_append_t {
    int32_t used;
    int32_t dropped;
    int32_t off;
} mesh_append_t;

typedef struct _mesh_imessage_t {
    uint32_t id;
    uint32_t from;
    uint32_t to;
    uint32_t time;
    int32_t rssi;
    int32_t channel;
    int32_t hops;
    int32_t snr;
    int32_t flags;
    int32_t text_off;
    int32_t text_len;
} mesh_imessage_t;

typedef struct _mesh_brief_t {
    uint32_t peer;
    int32_t direct;
    int32_t read;
} mesh_brief_t;

// Must match the structs in ../src/ffi.rs. The position is in 1e-7 degrees and
// the altitude in millimetres, which is what the wire wants, so nothing here
// is scaled twice on its way to a Position message.
typedef struct _mesh_fix_t {
    int32_t lat_e7;
    int32_t lon_e7;
    int32_t alt_mm;
    uint32_t when;
    uint32_t good;
    uint32_t bad;
    uint16_t hdop_cm;
    uint8_t sats;
    uint8_t in_view;
    uint8_t quality;
    uint8_t status;
    uint8_t mode;
    uint8_t nav;
    uint8_t have;
    uint8_t valid;
} mesh_fix_t;

typedef struct _mesh_civil_t {
    uint16_t year;
    uint8_t month;
    uint8_t day;
    uint8_t hour;
    uint8_t minute;
    uint8_t second;
} mesh_civil_t;

typedef struct _mesh_feed_t {
    uint32_t landed;
    uint32_t echo_used;
} mesh_feed_t;

typedef struct _mesh_stream_t {
    int32_t frame_len;
    uint32_t used;
} mesh_stream_t;

typedef struct _mesh_stream_counts_t {
    uint32_t lost;
    uint32_t frames;
    uint32_t partial;
} mesh_stream_counts_t;

extern int32_t meshtastic_header_parse(const uint8_t *frame, size_t len, mesh_header_t *out);
extern int32_t meshtastic_djb2(const uint8_t *ptr, size_t len, uint32_t *out);
extern int32_t meshtastic_battery_percent(uint32_t millivolts);
extern int32_t meshtastic_channel_hash(const uint8_t *name, size_t name_len,
    const uint8_t *psk, size_t psk_len);
extern int32_t meshtastic_preset_params(uint8_t preset, uint8_t wide, mesh_preset_t *out);
extern int32_t meshtastic_region(uint8_t index, mesh_region_t *out);
extern int32_t meshtastic_num_channels(uint8_t region, uint32_t bw_hz, uint32_t *out);
extern int32_t meshtastic_slot_frequency(uint8_t region, uint32_t bw_hz, uint32_t slot,
    uint32_t *out);
extern int32_t meshtastic_channel_frequency(uint8_t region, uint32_t bw_hz, uint32_t channel_num,
    const uint8_t *name, size_t name_len, uint32_t *out);
extern int32_t meshtastic_data_parse(const uint8_t *ptr, size_t len, mesh_data_t *out);
extern int32_t meshtastic_proto_field(const uint8_t *ptr, size_t len, size_t at,
    mesh_field_t *out);
extern int32_t meshtastic_header_write(uint32_t to, uint32_t from, uint32_t id,
    uint8_t flags, uint8_t channel, uint8_t next_hop, uint8_t relay_node,
    uint8_t *out, size_t len);
extern int32_t meshtastic_data_encode(uint32_t portnum, const uint8_t *payload,
    size_t payload_len, uint8_t want_response, uint32_t bitfield,
    uint8_t has_bitfield, uint8_t *out, size_t out_len, uint32_t *used);
extern int32_t meshtastic_airtime_us(size_t payload_len, uint8_t sf, uint32_t bw_hz,
    uint8_t cr, uint32_t *out);
extern int32_t meshtastic_duty_record(uint8_t *window, size_t len, uint32_t slot,
    uint32_t us);
extern int32_t meshtastic_duty_used(uint8_t *window, size_t len, uint32_t slot,
    uint32_t *out);
extern int32_t meshtastic_pb_uint(uint8_t *out, size_t room, size_t at, uint32_t number,
    uint32_t value);
extern int32_t meshtastic_pb_int32(uint8_t *out, size_t room, size_t at, uint32_t number,
    int32_t value);
extern int32_t meshtastic_pb_fixed32(uint8_t *out, size_t room, size_t at, uint32_t number,
    uint32_t value);
extern int32_t meshtastic_pb_blob_head(uint8_t *out, size_t room, size_t at, uint32_t number,
    size_t length);
extern int32_t meshtastic_roster_slot(const uint8_t *table, size_t len, uint32_t num);
extern int32_t meshtastic_roster_free(const uint8_t *table, size_t len);
extern int32_t meshtastic_roster_oldest(const uint8_t *table, size_t len);
extern int32_t meshtastic_roster_used(const uint8_t *table, size_t len);
extern int32_t meshtastic_roster_read(const uint8_t *table, size_t len, size_t at,
    mesh_row_t *out);
extern int32_t meshtastic_roster_touch(uint8_t *table, size_t len, size_t at, uint32_t num,
    int32_t now, uint32_t tick, int32_t hops, int32_t snr_q, int32_t rssi);
extern int32_t meshtastic_roster_restore(uint8_t *table, size_t len, size_t at, uint32_t num,
    int32_t when, uint32_t tick, uint32_t count, int32_t hops, uint32_t hw, uint32_t role,
    int32_t snr_q);
extern int32_t meshtastic_roster_identify(uint8_t *table, size_t len, size_t at,
    uint32_t hw, uint32_t role);
extern int32_t meshtastic_roster_clear(uint8_t *table, size_t len, size_t at);
extern int32_t meshtastic_cache_length(const uint8_t *head, size_t len);
extern int32_t meshtastic_cache_verify(const uint8_t *raw, size_t len);
extern int32_t meshtastic_cache_record(const uint8_t *raw, size_t len, size_t at, size_t end,
    mesh_record_t *out);
extern int32_t meshtastic_cache_read_peer(const uint8_t *raw, size_t len, size_t off,
    size_t payload_len, mesh_cpeer_t *out);
extern int32_t meshtastic_cache_read_message(const uint8_t *raw, size_t len, size_t off,
    size_t payload_len, mesh_cmessage_t *out);
extern int32_t meshtastic_cache_peer_len(int32_t short_len, int32_t long_len);
extern int32_t meshtastic_cache_message_len(size_t text_len);
extern int32_t meshtastic_cache_write_peer(uint8_t *out, size_t len, size_t at, uint32_t num,
    uint32_t age, uint32_t count, uint8_t hops, uint8_t hw, uint8_t role, int8_t snr,
    const uint8_t *short_name, int32_t short_len, const uint8_t *long_name, int32_t long_len);
extern int32_t meshtastic_cache_write_message(uint8_t *out, size_t len, size_t at, uint32_t id,
    uint32_t from, uint32_t to, uint32_t time, uint8_t flags, uint8_t channel, uint8_t hops,
    int8_t snr, const uint8_t *text, size_t text_len);
extern int32_t meshtastic_cache_finish(uint8_t *out, size_t len, size_t used);

// ../src/store.rs. `mesh_record_t` again: the keystore's records differ from
// the cache's in what a tag means and in how long a length is, not in what a
// walk needs to report.
extern int32_t meshtastic_store_length(const uint8_t *head, size_t len);
extern int32_t meshtastic_store_verify(const uint8_t *raw, size_t len);
extern int32_t meshtastic_store_record(const uint8_t *raw, size_t len, size_t at, size_t end,
    mesh_record_t *out);
extern int32_t meshtastic_store_put(uint8_t *out, size_t room, size_t at, uint32_t tag,
    const uint8_t *value, size_t value_len);
extern int32_t meshtastic_store_finish(uint8_t *out, size_t room, size_t used);
extern int32_t meshtastic_inbox_append(uint8_t *buf, size_t len, size_t used, int32_t limit,
    const mesh_inmsg_t *m, const uint8_t *text, size_t text_len, mesh_append_t *out);
extern int32_t meshtastic_inbox_next(const uint8_t *buf, size_t len, size_t used, int32_t after,
    uint32_t node_num, int32_t want, uint32_t peer);
extern int32_t meshtastic_inbox_count(const uint8_t *buf, size_t len, size_t used,
    uint32_t node_num, int32_t want, uint32_t peer, int32_t unread);
extern int32_t meshtastic_inbox_mark(uint8_t *buf, size_t len, size_t used, uint32_t node_num,
    int32_t want, uint32_t peer);
extern int32_t meshtastic_inbox_read(const uint8_t *buf, size_t len, size_t used, size_t off,
    mesh_imessage_t *out);
extern int32_t meshtastic_inbox_brief(const uint8_t *buf, size_t len, size_t used, size_t off,
    uint32_t node_num, mesh_brief_t *out);

extern int32_t meshtastic_payload_position(const uint8_t *body, size_t len, mesh_position_t *out);
extern int32_t meshtastic_payload_user(const uint8_t *body, size_t len, mesh_user_t *out);
extern int32_t meshtastic_payload_short_name(const uint8_t *body, size_t len, mesh_variant_t *out);
extern int32_t meshtastic_payload_device_metrics(const uint8_t *body, size_t len,
    mesh_device_t *out);
extern int32_t meshtastic_payload_environment_metrics(const uint8_t *body, size_t len,
    mesh_env_t *out);
extern int32_t meshtastic_payload_telemetry(const uint8_t *body, size_t len, mesh_variant_t *out);
extern int32_t meshtastic_payload_routing(const uint8_t *body, size_t len, mesh_routing_t *out);
extern int32_t meshtastic_payload_traceroute(const uint8_t *body, size_t len, mesh_route_t *out);
extern int32_t meshtastic_payload_sender_time(const uint8_t *body, size_t len, uint32_t want,
    uint32_t *out);
extern int32_t meshtastic_payload_snr_at(const uint8_t *chunk, size_t len, size_t at, int32_t *out);
extern int32_t meshtastic_nmea_reset(uint8_t *state, size_t len);
extern int32_t meshtastic_nmea_sentence(uint8_t *state, size_t len, const uint8_t *line,
    size_t line_len);
extern int32_t meshtastic_nmea_feed(uint8_t *state, size_t len, const uint8_t *chunk,
    size_t chunk_len, uint8_t *echo, size_t echo_len, size_t echo_at, mesh_feed_t *out);
extern int32_t meshtastic_nmea_fix(const uint8_t *state, size_t len, mesh_fix_t *out);
extern int32_t meshtastic_nmea_talker(const uint8_t *state, size_t len, size_t index,
    uint8_t *id, uint8_t *count);
extern int32_t meshtastic_nmea_civil(uint32_t when, mesh_civil_t *out);
extern int32_t meshtastic_stream_reset(uint8_t *state, size_t len);
extern int32_t meshtastic_stream_feed(uint8_t *state, size_t state_len,
    const uint8_t *chunk, size_t chunk_len, size_t at, mesh_stream_t *out);
extern int32_t meshtastic_stream_counts(const uint8_t *state, size_t len,
    mesh_stream_counts_t *out);
extern int32_t meshtastic_stream_frame(uint8_t *out, size_t out_len, size_t at,
    const uint8_t *payload, size_t payload_len);
extern int32_t meshtastic_position_encode(uint8_t *out, size_t out_len,
    const uint8_t *state, size_t state_len, uint32_t precision);

// Every failure here is a caller mistake -- a truncated frame, an index off the
// end of a table, a bandwidth that does not fit the band -- so they all map to
// ValueError rather than RuntimeError. Nothing in this module can fail for an
// external reason.
static void check_err(int32_t rc) {
    if (rc >= 0) {
        return;
    }
    switch (rc) {
        case -1:
            mp_raise_ValueError(MP_ERROR_TEXT("frame is shorter than a 16-byte header"));
        case -2:
            mp_raise_ValueError(MP_ERROR_TEXT("null buffer"));
        case -3:
            mp_raise_ValueError(MP_ERROR_TEXT("unknown region index"));
        case -4:
            mp_raise_ValueError(MP_ERROR_TEXT("unknown modem preset index"));
        case -5:
            mp_raise_ValueError(MP_ERROR_TEXT("channel slot out of range for this region"));
        case -6:
            mp_raise_ValueError(MP_ERROR_TEXT("bandwidth does not fit in this region"));
        case -7:
            mp_raise_ValueError(MP_ERROR_TEXT("protobuf ends mid-field; wrong channel key?"));
        case -8:
            mp_raise_ValueError(MP_ERROR_TEXT("not protobuf; wrong channel key?"));
        case -9:
            mp_raise_ValueError(MP_ERROR_TEXT("output buffer too small"));
        case -10:
            mp_raise_ValueError(MP_ERROR_TEXT("spreading factor, bandwidth or coding rate is not LoRa"));
        case -11:
            mp_raise_ValueError(MP_ERROR_TEXT("roster offset is not a row in this table"));
        case -12:
            mp_raise_ValueError(MP_ERROR_TEXT("inbox offset is not a record in this arena"));
        case -13:
            mp_raise_ValueError(MP_ERROR_TEXT("GNSS state buffer is the wrong size"));
        case -14:
            mp_raise_ValueError(MP_ERROR_TEXT("not an NMEA sentence, or its checksum is wrong"));
        case -15:
            mp_raise_ValueError(MP_ERROR_TEXT("no confirmed fix to send"));
        default:
            mp_raise_ValueError(MP_ERROR_TEXT("meshtastic decode failed"));
    }
}

// Node numbers and packet ids use the full 32 bits, so they have to go out
// unsigned; a small int would show 0xFFFFFFFF as -1.
static mp_obj_t mod_parse_header(mp_obj_t frame_in) {
    mp_buffer_info_t frame;
    mp_get_buffer_raise(frame_in, &frame, MP_BUFFER_READ);
    mesh_header_t h;
    check_err(meshtastic_header_parse(frame.buf, frame.len, &h));
    mp_obj_t items[12] = {
        mp_obj_new_int_from_uint(h.to),
        mp_obj_new_int_from_uint(h.from),
        mp_obj_new_int_from_uint(h.id),
        MP_OBJ_NEW_SMALL_INT(h.flags),
        MP_OBJ_NEW_SMALL_INT(h.channel),
        MP_OBJ_NEW_SMALL_INT(h.next_hop),
        MP_OBJ_NEW_SMALL_INT(h.relay_node),
        MP_OBJ_NEW_SMALL_INT(h.hop_limit),
        MP_OBJ_NEW_SMALL_INT(h.hop_start),
        mp_obj_new_bool(h.want_ack),
        mp_obj_new_bool(h.via_mqtt),
        // None, not a number: the sender did not say how far this has come, and
        // reporting a guess would be worse than admitting ignorance.
        h.hops_away == 0xFF ? mp_const_none : MP_OBJ_NEW_SMALL_INT(h.hops_away),
    };
    return mp_obj_new_tuple(12, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_parse_header_obj, mod_parse_header);

static mp_obj_t mod_build_flags(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    uint8_t hop_limit = (uint8_t)mp_obj_get_int(args[0]);
    bool want_ack = mp_obj_is_true(args[1]);
    bool via_mqtt = mp_obj_is_true(args[2]);
    uint8_t hop_start = (uint8_t)mp_obj_get_int(args[3]);
    uint8_t flags = hop_limit > 7 ? 7 : hop_limit;
    if (want_ack) {
        flags |= 0x08;
    }
    if (via_mqtt) {
        flags |= 0x10;
    }
    flags |= (uint8_t)((hop_start << 5) & 0xE0);
    return MP_OBJ_NEW_SMALL_INT(flags);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_build_flags_obj, 4, 4, mod_build_flags);

static mp_obj_t mod_djb2(mp_obj_t data_in) {
    mp_buffer_info_t data;
    mp_get_buffer_raise(data_in, &data, MP_BUFFER_READ);
    uint32_t out;
    check_err(meshtastic_djb2(data.buf, data.len, &out));
    return mp_obj_new_int_from_uint(out);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_djb2_obj, mod_djb2);

static mp_obj_t mod_battery_percent(mp_obj_t millivolts_in) {
    mp_int_t mv = mp_obj_get_int(millivolts_in);
    // Negative would wrap to a full pack on the way through the unsigned ABI.
    int32_t rc = meshtastic_battery_percent(mv < 0 ? 0 : (uint32_t)mv);
    check_err(rc);
    return MP_OBJ_NEW_SMALL_INT(rc);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_battery_percent_obj, mod_battery_percent);

static mp_obj_t mod_channel_hash(mp_obj_t name_in, mp_obj_t psk_in) {
    mp_buffer_info_t name, psk;
    mp_get_buffer_raise(name_in, &name, MP_BUFFER_READ);
    mp_get_buffer_raise(psk_in, &psk, MP_BUFFER_READ);
    int32_t rc = meshtastic_channel_hash(name.buf, name.len, psk.buf, psk.len);
    check_err(rc);
    return MP_OBJ_NEW_SMALL_INT(rc);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_channel_hash_obj, mod_channel_hash);

static mp_obj_t mod_preset_params(mp_obj_t preset_in, mp_obj_t wide_in) {
    mesh_preset_t p;
    check_err(meshtastic_preset_params((uint8_t)mp_obj_get_int(preset_in),
        mp_obj_is_true(wide_in), &p));
    mp_obj_t items[3] = {
        MP_OBJ_NEW_SMALL_INT(p.sf),
        mp_obj_new_int_from_uint(p.bw_hz),
        MP_OBJ_NEW_SMALL_INT(p.cr),
    };
    return mp_obj_new_tuple(3, items);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_preset_params_obj, mod_preset_params);

static mp_obj_t mod_region_info(mp_obj_t index_in) {
    mesh_region_t r;
    check_err(meshtastic_region((uint8_t)mp_obj_get_int(index_in), &r));
    mp_obj_t items[6] = {
        mp_obj_new_int_from_uint(r.start_hz),
        mp_obj_new_int_from_uint(r.end_hz),
        mp_obj_new_int_from_uint(r.spacing_hz),
        MP_OBJ_NEW_SMALL_INT(r.duty_cycle_pct),
        MP_OBJ_NEW_SMALL_INT(r.power_limit_dbm),
        mp_obj_new_bool(r.wide_lora),
    };
    return mp_obj_new_tuple(6, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_region_info_obj, mod_region_info);

static mp_obj_t mod_num_channels(mp_obj_t region_in, mp_obj_t bw_in) {
    uint32_t out;
    check_err(meshtastic_num_channels((uint8_t)mp_obj_get_int(region_in),
        (uint32_t)mp_obj_get_int(bw_in), &out));
    return mp_obj_new_int_from_uint(out);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_num_channels_obj, mod_num_channels);

static mp_obj_t mod_slot_frequency(mp_obj_t region_in, mp_obj_t bw_in, mp_obj_t slot_in) {
    uint32_t out;
    check_err(meshtastic_slot_frequency((uint8_t)mp_obj_get_int(region_in),
        (uint32_t)mp_obj_get_int(bw_in), (uint32_t)mp_obj_get_int(slot_in), &out));
    return mp_obj_new_int_from_uint(out);
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_slot_frequency_obj, mod_slot_frequency);

// channel_num 0 hashes the name; anything else is a one-based slot index.
static mp_obj_t mod_channel_frequency(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t name;
    mp_get_buffer_raise(args[3], &name, MP_BUFFER_READ);
    uint32_t out;
    check_err(meshtastic_channel_frequency((uint8_t)mp_obj_get_int(args[0]),
        (uint32_t)mp_obj_get_int(args[1]), (uint32_t)mp_obj_get_int(args[2]),
        name.buf, name.len, &out));
    return mp_obj_new_int_from_uint(out);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_channel_frequency_obj, 4, 4, mod_channel_frequency);

// The payload comes back as an offset and a length into the caller's own
// buffer rather than as a copy, so decoding a packet allocates nothing here.
static mp_obj_t mod_parse_data(mp_obj_t buf_in) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(buf_in, &buf, MP_BUFFER_READ);
    mesh_data_t d;
    check_err(meshtastic_data_parse(buf.buf, buf.len, &d));
    mp_obj_t items[10] = {
        mp_obj_new_int_from_uint(d.portnum),
        mp_obj_new_int_from_uint(d.payload_off),
        mp_obj_new_int_from_uint(d.payload_len),
        mp_obj_new_int_from_uint(d.dest),
        mp_obj_new_int_from_uint(d.source),
        mp_obj_new_int_from_uint(d.request_id),
        mp_obj_new_int_from_uint(d.reply_id),
        mp_obj_new_int_from_uint(d.emoji),
        mp_obj_new_bool(d.want_response),
        d.has_bitfield ? mp_obj_new_int_from_uint(d.bitfield) : mp_const_none,
    };
    return mp_obj_new_tuple(10, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_parse_data_obj, mod_parse_data);

// Split in halves because dynruntime.h has no 64-bit int constructor; only
// fixed64 fields can exceed 32 bits and Meshtastic does not currently use any.
static mp_obj_t mod_proto_field(mp_obj_t buf_in, mp_obj_t at_in) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(buf_in, &buf, MP_BUFFER_READ);
    mesh_field_t f;
    check_err(meshtastic_proto_field(buf.buf, buf.len, (size_t)mp_obj_get_int(at_in), &f));
    mp_obj_t items[7] = {
        mp_obj_new_int_from_uint(f.number),
        mp_obj_new_int_from_uint(f.wire),
        mp_obj_new_int_from_uint(f.value_lo),
        mp_obj_new_int_from_uint(f.value_hi),
        mp_obj_new_int_from_uint(f.data_off),
        mp_obj_new_int_from_uint(f.data_len),
        mp_obj_new_int_from_uint(f.next),
    };
    return mp_obj_new_tuple(7, items);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_proto_field_obj, mod_proto_field);

// Frames are built into a stack buffer and copied out as bytes. 256 is the LoRa
// physical maximum, so nothing this module can be asked to build will not fit.
#define MESH_MAX_FRAME 256

static mp_obj_t mod_write_header(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    uint8_t out[16];
    check_err(meshtastic_header_write(
        (uint32_t)mp_obj_get_int_truncated(args[0]),
        (uint32_t)mp_obj_get_int_truncated(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (uint8_t)mp_obj_get_int(args[3]),
        (uint8_t)mp_obj_get_int(args[4]),
        (uint8_t)mp_obj_get_int(args[5]),
        (uint8_t)mp_obj_get_int(args[6]),
        out, sizeof(out)));
    return mp_obj_new_bytes(out, sizeof(out));
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_write_header_obj, 7, 7, mod_write_header);

// bitfield is None or an int, because the field is `optional` and an explicit
// zero says something a missing field does not.
static mp_obj_t mod_encode_data(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t payload;
    mp_get_buffer_raise(args[1], &payload, MP_BUFFER_READ);
    bool has_bitfield = args[3] != mp_const_none;
    uint8_t out[MESH_MAX_FRAME];
    uint32_t used = 0;
    check_err(meshtastic_data_encode(
        (uint32_t)mp_obj_get_int_truncated(args[0]),
        payload.buf, payload.len,
        mp_obj_is_true(args[2]),
        has_bitfield ? (uint32_t)mp_obj_get_int_truncated(args[3]) : 0,
        has_bitfield,
        out, sizeof(out), &used));
    return mp_obj_new_bytes(out, used);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_encode_data_obj, 4, 4, mod_encode_data);

static mp_obj_t mod_airtime_us(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    uint32_t out;
    check_err(meshtastic_airtime_us(
        (size_t)mp_obj_get_int(args[0]),
        (uint8_t)mp_obj_get_int(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (uint8_t)mp_obj_get_int(args[3]),
        &out));
    return mp_obj_new_int_from_uint(out);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_airtime_us_obj, 4, 4, mod_airtime_us);

// The roster table is a bytearray the Python side owns and sizes once. Every
// call below writes through it in place; nothing here allocates, which is the
// reason the table is packed rather than a list of objects.
static mp_obj_t mod_roster_slot(mp_obj_t table_in, mp_obj_t num_in) {
    mp_buffer_info_t table;
    mp_get_buffer_raise(table_in, &table, MP_BUFFER_READ);
    int32_t at = meshtastic_roster_slot(table.buf, table.len,
        (uint32_t)mp_obj_get_int_truncated(num_in));
    check_err(at < -1 ? at : 0);
    // None rather than -1: a byte offset of -1 would index from the end.
    return at < 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(at);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_roster_slot_obj, mod_roster_slot);

static mp_obj_t mod_roster_free(mp_obj_t table_in) {
    mp_buffer_info_t table;
    mp_get_buffer_raise(table_in, &table, MP_BUFFER_READ);
    int32_t at = meshtastic_roster_free(table.buf, table.len);
    check_err(at < -1 ? at : 0);
    return at < 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(at);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_roster_free_obj, mod_roster_free);

static mp_obj_t mod_roster_oldest(mp_obj_t table_in) {
    mp_buffer_info_t table;
    mp_get_buffer_raise(table_in, &table, MP_BUFFER_READ);
    int32_t at = meshtastic_roster_oldest(table.buf, table.len);
    check_err(at < -1 ? at : 0);
    return at < 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(at);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_roster_oldest_obj, mod_roster_oldest);

static mp_obj_t mod_roster_used(mp_obj_t table_in) {
    mp_buffer_info_t table;
    mp_get_buffer_raise(table_in, &table, MP_BUFFER_READ);
    int32_t n = meshtastic_roster_used(table.buf, table.len);
    check_err(n);
    return MP_OBJ_NEW_SMALL_INT(n);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_roster_used_obj, mod_roster_used);

// Sentinels become None here rather than in Python, so that every reader of a
// row gets the same answer without repeating the rule.
static mp_obj_t mod_roster_read(mp_obj_t table_in, mp_obj_t at_in) {
    mp_buffer_info_t table;
    mp_get_buffer_raise(table_in, &table, MP_BUFFER_READ);
    mesh_row_t r;
    check_err(meshtastic_roster_read(table.buf, table.len,
        (size_t)mp_obj_get_int(at_in), &r));
    mp_obj_t items[10] = {
        mp_obj_new_int_from_uint(r.num),
        mp_obj_new_int(r.first),
        mp_obj_new_int(r.last),
        mp_obj_new_int_from_uint(r.seen),
        MP_OBJ_NEW_SMALL_INT(r.count),
        r.hops == 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(r.hops - 1),
        MP_OBJ_NEW_SMALL_INT(r.hw),
        MP_OBJ_NEW_SMALL_INT(r.role),
        r.snr == -128 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(r.snr),
        r.rssi == 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(r.rssi),
    };
    return mp_obj_new_tuple(10, items);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_roster_read_obj, mod_roster_read);

static mp_obj_t mod_roster_touch(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t table;
    mp_get_buffer_raise(args[0], &table, MP_BUFFER_RW);
    int32_t fresh = meshtastic_roster_touch(table.buf, table.len,
        (size_t)mp_obj_get_int(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (int32_t)mp_obj_get_int(args[3]),
        (uint32_t)mp_obj_get_int_truncated(args[4]),
        args[5] == mp_const_none ? -1 : (int32_t)mp_obj_get_int(args[5]),
        args[6] == mp_const_none ? INT32_MIN : (int32_t)mp_obj_get_int(args[6]),
        args[7] == mp_const_none ? 0 : (int32_t)mp_obj_get_int(args[7]));
    check_err(fresh);
    return mp_obj_new_bool(fresh);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_roster_touch_obj, 8, 8, mod_roster_touch);

static mp_obj_t mod_roster_restore(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t table;
    mp_get_buffer_raise(args[0], &table, MP_BUFFER_RW);
    check_err(meshtastic_roster_restore(table.buf, table.len,
        (size_t)mp_obj_get_int(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (int32_t)mp_obj_get_int(args[3]),
        (uint32_t)mp_obj_get_int_truncated(args[4]),
        (uint32_t)mp_obj_get_int_truncated(args[5]),
        args[6] == mp_const_none ? -1 : (int32_t)mp_obj_get_int(args[6]),
        (uint32_t)mp_obj_get_int(args[7]),
        (uint32_t)mp_obj_get_int(args[8]),
        args[9] == mp_const_none ? INT32_MIN : (int32_t)mp_obj_get_int(args[9])));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_roster_restore_obj, 10, 10, mod_roster_restore);

static mp_obj_t mod_roster_identify(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t table;
    mp_get_buffer_raise(args[0], &table, MP_BUFFER_RW);
    check_err(meshtastic_roster_identify(table.buf, table.len,
        (size_t)mp_obj_get_int(args[1]),
        (uint32_t)mp_obj_get_int(args[2]),
        (uint32_t)mp_obj_get_int(args[3])));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_roster_identify_obj, 4, 4, mod_roster_identify);

static mp_obj_t mod_roster_clear(mp_obj_t table_in, mp_obj_t at_in) {
    mp_buffer_info_t table;
    mp_get_buffer_raise(table_in, &table, MP_BUFFER_RW);
    check_err(meshtastic_roster_clear(table.buf, table.len,
        (size_t)mp_obj_get_int(at_in)));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_roster_clear_obj, mod_roster_clear);

// The cache region, same arrangement as the roster: the Python side owns the
// buffer and these write through it. Offsets come back rather than slices, so
// walking a saved cache costs no allocation until a name is actually wanted.
static mp_obj_t mod_cache_length(mp_obj_t head_in) {
    mp_buffer_info_t head;
    mp_get_buffer_raise(head_in, &head, MP_BUFFER_READ);
    int32_t len = meshtastic_cache_length(head.buf, head.len);
    check_err(len < -1 ? len : 0);
    return len < 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(len);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_cache_length_obj, mod_cache_length);

static mp_obj_t mod_cache_verify(mp_obj_t raw_in) {
    mp_buffer_info_t raw;
    mp_get_buffer_raise(raw_in, &raw, MP_BUFFER_READ);
    int32_t end = meshtastic_cache_verify(raw.buf, raw.len);
    check_err(end < -1 ? end : 0);
    return end < 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(end);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_cache_verify_obj, mod_cache_verify);

static mp_obj_t mod_cache_record(mp_obj_t raw_in, mp_obj_t at_in, mp_obj_t end_in) {
    mp_buffer_info_t raw;
    mp_get_buffer_raise(raw_in, &raw, MP_BUFFER_READ);
    mesh_record_t r;
    int32_t rc = meshtastic_cache_record(raw.buf, raw.len,
        (size_t)mp_obj_get_int(at_in), (size_t)mp_obj_get_int(end_in), &r);
    check_err(rc);
    if (rc == 0) {
        return mp_const_none;
    }
    mp_obj_t items[4] = {
        MP_OBJ_NEW_SMALL_INT(r.kind),
        MP_OBJ_NEW_SMALL_INT(r.off),
        MP_OBJ_NEW_SMALL_INT(r.len),
        MP_OBJ_NEW_SMALL_INT(r.next),
    };
    return mp_obj_new_tuple(4, items);
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_cache_record_obj, mod_cache_record);

static mp_obj_t mod_cache_read_peer(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t raw;
    mp_get_buffer_raise(args[0], &raw, MP_BUFFER_READ);
    mesh_cpeer_t p;
    check_err(meshtastic_cache_read_peer(raw.buf, raw.len,
        (size_t)mp_obj_get_int(args[1]), (size_t)mp_obj_get_int(args[2]), &p));
    mp_obj_t items[11] = {
        mp_obj_new_int_from_uint(p.num),
        mp_obj_new_int_from_uint(p.age),
        MP_OBJ_NEW_SMALL_INT(p.count),
        p.hops == 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(p.hops - 1),
        MP_OBJ_NEW_SMALL_INT(p.hw),
        MP_OBJ_NEW_SMALL_INT(p.role),
        p.snr == -128 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(p.snr),
        MP_OBJ_NEW_SMALL_INT(p.short_off),
        MP_OBJ_NEW_SMALL_INT(p.short_len),
        MP_OBJ_NEW_SMALL_INT(p.long_off),
        MP_OBJ_NEW_SMALL_INT(p.long_len),
    };
    return mp_obj_new_tuple(11, items);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_cache_read_peer_obj, 3, 3, mod_cache_read_peer);

static mp_obj_t mod_cache_read_message(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t raw;
    mp_get_buffer_raise(args[0], &raw, MP_BUFFER_READ);
    mesh_cmessage_t m;
    check_err(meshtastic_cache_read_message(raw.buf, raw.len,
        (size_t)mp_obj_get_int(args[1]), (size_t)mp_obj_get_int(args[2]), &m));
    mp_obj_t items[10] = {
        mp_obj_new_int_from_uint(m.id),
        mp_obj_new_int_from_uint(m.from),
        mp_obj_new_int_from_uint(m.to),
        mp_obj_new_int_from_uint(m.time),
        MP_OBJ_NEW_SMALL_INT(m.flags),
        MP_OBJ_NEW_SMALL_INT(m.channel),
        m.hops == 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(m.hops - 1),
        m.snr == -128 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(m.snr),
        MP_OBJ_NEW_SMALL_INT(m.text_off),
        MP_OBJ_NEW_SMALL_INT(m.text_len),
    };
    return mp_obj_new_tuple(10, items);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_cache_read_message_obj, 3, 3, mod_cache_read_message);

// -1 for a name the peer has never sent, which is not the same as an empty one.
static mp_obj_t mod_cache_peer_len(mp_obj_t short_in, mp_obj_t long_in) {
    return MP_OBJ_NEW_SMALL_INT(meshtastic_cache_peer_len(
        short_in == mp_const_none ? -1 : (int32_t)mp_obj_get_int(short_in),
        long_in == mp_const_none ? -1 : (int32_t)mp_obj_get_int(long_in)));
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_cache_peer_len_obj, mod_cache_peer_len);

static mp_obj_t mod_cache_message_len(mp_obj_t text_in) {
    return MP_OBJ_NEW_SMALL_INT(
        meshtastic_cache_message_len((size_t)mp_obj_get_int(text_in)));
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_cache_message_len_obj, mod_cache_message_len);

static mp_obj_t mod_cache_write_peer(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t out, name;
    mp_get_buffer_raise(args[0], &out, MP_BUFFER_RW);
    const uint8_t *short_name = NULL, *long_name = NULL;
    int32_t short_len = -1, long_len = -1;
    if (args[9] != mp_const_none) {
        mp_get_buffer_raise(args[9], &name, MP_BUFFER_READ);
        short_name = name.buf;
        short_len = (int32_t)name.len;
    }
    if (args[10] != mp_const_none) {
        mp_get_buffer_raise(args[10], &name, MP_BUFFER_READ);
        long_name = name.buf;
        long_len = (int32_t)name.len;
    }
    int32_t next = meshtastic_cache_write_peer(out.buf, out.len,
        (size_t)mp_obj_get_int(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (uint32_t)mp_obj_get_int_truncated(args[3]),
        (uint32_t)mp_obj_get_int_truncated(args[4]),
        args[5] == mp_const_none ? 0 : (uint8_t)(mp_obj_get_int(args[5]) + 1),
        (uint8_t)mp_obj_get_int(args[6]),
        (uint8_t)mp_obj_get_int(args[7]),
        args[8] == mp_const_none ? -128 : (int8_t)mp_obj_get_int(args[8]),
        short_name, short_len, long_name, long_len);
    check_err(next);
    return MP_OBJ_NEW_SMALL_INT(next);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_cache_write_peer_obj, 11, 11, mod_cache_write_peer);

static mp_obj_t mod_cache_write_message(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t out, text;
    mp_get_buffer_raise(args[0], &out, MP_BUFFER_RW);
    mp_get_buffer_raise(args[10], &text, MP_BUFFER_READ);
    int32_t next = meshtastic_cache_write_message(out.buf, out.len,
        (size_t)mp_obj_get_int(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (uint32_t)mp_obj_get_int_truncated(args[3]),
        (uint32_t)mp_obj_get_int_truncated(args[4]),
        (uint32_t)mp_obj_get_int_truncated(args[5]),
        (uint8_t)mp_obj_get_int(args[6]),
        (uint8_t)mp_obj_get_int(args[7]),
        args[8] == mp_const_none ? 0 : (uint8_t)(mp_obj_get_int(args[8]) + 1),
        args[9] == mp_const_none ? -128 : (int8_t)mp_obj_get_int(args[9]),
        text.buf, text.len);
    check_err(next);
    return MP_OBJ_NEW_SMALL_INT(next);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_cache_write_message_obj, 11, 11, mod_cache_write_message);

static mp_obj_t mod_cache_finish(mp_obj_t out_in, mp_obj_t used_in) {
    mp_buffer_info_t out;
    mp_get_buffer_raise(out_in, &out, MP_BUFFER_RW);
    check_err(meshtastic_cache_finish(out.buf, out.len,
        (size_t)mp_obj_get_int(used_in)));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_cache_finish_obj, mod_cache_finish);

// The keystore region. Same shape as the cache above, and the same division:
// the layout is here, what a tag means is keystore.py's.
static mp_obj_t mod_store_length(mp_obj_t head_in) {
    mp_buffer_info_t head;
    mp_get_buffer_raise(head_in, &head, MP_BUFFER_READ);
    int32_t len = meshtastic_store_length(head.buf, head.len);
    check_err(len < -1 ? len : 0);
    return len < 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(len);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_store_length_obj, mod_store_length);

static mp_obj_t mod_store_verify(mp_obj_t raw_in) {
    mp_buffer_info_t raw;
    mp_get_buffer_raise(raw_in, &raw, MP_BUFFER_READ);
    int32_t end = meshtastic_store_verify(raw.buf, raw.len);
    check_err(end < -1 ? end : 0);
    return end < 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(end);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_store_verify_obj, mod_store_verify);

static mp_obj_t mod_store_record(mp_obj_t raw_in, mp_obj_t at_in, mp_obj_t end_in) {
    mp_buffer_info_t raw;
    mp_get_buffer_raise(raw_in, &raw, MP_BUFFER_READ);
    mesh_record_t r;
    int32_t rc = meshtastic_store_record(raw.buf, raw.len,
        (size_t)mp_obj_get_int(at_in), (size_t)mp_obj_get_int(end_in), &r);
    check_err(rc);
    if (rc == 0) {
        return mp_const_none;
    }
    mp_obj_t items[4] = {
        MP_OBJ_NEW_SMALL_INT(r.kind),
        MP_OBJ_NEW_SMALL_INT(r.off),
        MP_OBJ_NEW_SMALL_INT(r.len),
        MP_OBJ_NEW_SMALL_INT(r.next),
    };
    return mp_obj_new_tuple(4, items);
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_store_record_obj, mod_store_record);

static mp_obj_t mod_store_put(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t out, value;
    mp_get_buffer_raise(args[0], &out, MP_BUFFER_RW);
    mp_get_buffer_raise(args[3], &value, MP_BUFFER_READ);
    int32_t next = meshtastic_store_put(out.buf, out.len,
        (size_t)mp_obj_get_int(args[1]), (uint32_t)mp_obj_get_int(args[2]),
        value.buf, value.len);
    check_err(next);
    return MP_OBJ_NEW_SMALL_INT(next);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_store_put_obj, 4, 4, mod_store_put);

static mp_obj_t mod_store_finish(mp_obj_t out_in, mp_obj_t used_in) {
    mp_buffer_info_t out;
    mp_get_buffer_raise(out_in, &out, MP_BUFFER_RW);
    int32_t len = meshtastic_store_finish(out.buf, out.len,
        (size_t)mp_obj_get_int(used_in));
    check_err(len);
    return MP_OBJ_NEW_SMALL_INT(len);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_store_finish_obj, mod_store_finish);

// The transmit budget. Python owns a bytearray of buckets and passes the bucket
// index rather than a time, because the clock here is a float and the bucket
// width is the caller's choice.
static mp_obj_t mod_duty_record(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t window;
    mp_get_buffer_raise(args[0], &window, MP_BUFFER_RW);
    check_err(meshtastic_duty_record(window.buf, window.len,
        (uint32_t)mp_obj_get_int_truncated(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2])));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_duty_record_obj, 3, 3, mod_duty_record);

static mp_obj_t mod_duty_used(mp_obj_t window_in, mp_obj_t slot_in) {
    mp_buffer_info_t window;
    mp_get_buffer_raise(window_in, &window, MP_BUFFER_RW);
    uint32_t out;
    check_err(meshtastic_duty_used(window.buf, window.len,
        (uint32_t)mp_obj_get_int_truncated(slot_in), &out));
    // Microseconds over an hour do not fit in a small int.
    return mp_obj_new_int_from_uint(out);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_duty_used_obj, mod_duty_used);

// Writing protobuf. Each of these builds one field into the caller's buffer at
// the offset it is given and hands back the offset past it, so a whole message
// costs one allocation -- the `pb_take` at the end -- rather than one per field
// plus a tuple plus a join.
static mp_obj_t mod_pb_uint(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t out;
    mp_get_buffer_raise(args[0], &out, MP_BUFFER_RW);
    int32_t n = meshtastic_pb_uint(out.buf, out.len,
        (size_t)mp_obj_get_int_truncated(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (uint32_t)mp_obj_get_int_truncated(args[3]));
    check_err(n);
    return MP_OBJ_NEW_SMALL_INT(n);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_pb_uint_obj, 4, 4, mod_pb_uint);

static mp_obj_t mod_pb_int32(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t out;
    mp_get_buffer_raise(args[0], &out, MP_BUFFER_RW);
    int32_t n = meshtastic_pb_int32(out.buf, out.len,
        (size_t)mp_obj_get_int_truncated(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (int32_t)mp_obj_get_int(args[3]));
    check_err(n);
    return MP_OBJ_NEW_SMALL_INT(n);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_pb_int32_obj, 4, 4, mod_pb_int32);

static mp_obj_t mod_pb_fixed32(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t out;
    mp_get_buffer_raise(args[0], &out, MP_BUFFER_RW);
    int32_t n = meshtastic_pb_fixed32(out.buf, out.len,
        (size_t)mp_obj_get_int_truncated(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (uint32_t)mp_obj_get_int_truncated(args[3]));
    check_err(n);
    return MP_OBJ_NEW_SMALL_INT(n);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_pb_fixed32_obj, 4, 4, mod_pb_fixed32);

static mp_obj_t mod_pb_blob_head(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t out;
    mp_get_buffer_raise(args[0], &out, MP_BUFFER_RW);
    int32_t n = meshtastic_pb_blob_head(out.buf, out.len,
        (size_t)mp_obj_get_int_truncated(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]),
        (size_t)mp_obj_get_int_truncated(args[3]));
    check_err(n);
    return MP_OBJ_NEW_SMALL_INT(n);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_pb_blob_head_obj, 4, 4, mod_pb_blob_head);

// The one allocation a message costs: the finished bytes, copied out of the
// buffer the fields were built in.
static mp_obj_t mod_pb_take(mp_obj_t buf_in, mp_obj_t at_in) {
    mp_buffer_info_t out;
    mp_get_buffer_raise(buf_in, &out, MP_BUFFER_READ);
    size_t at = (size_t)mp_obj_get_int_truncated(at_in);
    if (at > out.len) {
        mp_raise_ValueError(MP_ERROR_TEXT("past the end"));
    }
    return mp_obj_new_bytes(out.buf, at);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_pb_take_obj, mod_pb_take);

// The message arena. Same arrangement again: Python owns one bytearray sized at
// boot, and these pack records into it. `used` is passed in and the new one
// comes back, so the arena has no state here between calls.
static mp_obj_t mod_inbox_append(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t buf, text;
    mp_get_buffer_raise(args[0], &buf, MP_BUFFER_RW);
    mp_get_buffer_raise(args[12], &text, MP_BUFFER_READ);
    mesh_inmsg_t m = {
        .id = (uint32_t)mp_obj_get_int_truncated(args[3]),
        .from = (uint32_t)mp_obj_get_int_truncated(args[4]),
        .to = (uint32_t)mp_obj_get_int_truncated(args[5]),
        .time = args[6] == mp_const_none ? 0 : (uint32_t)mp_obj_get_int_truncated(args[6]),
        .rssi = args[7] == mp_const_none ? -32768 : (int16_t)mp_obj_get_int(args[7]),
        .channel = (uint8_t)mp_obj_get_int(args[8]),
        .hops = args[9] == mp_const_none ? 255 : (uint8_t)mp_obj_get_int(args[9]),
        .snr = args[10] == mp_const_none ? -128 : (int8_t)mp_obj_get_int(args[10]),
        .flags = (uint8_t)mp_obj_get_int(args[11]),
    };
    mesh_append_t got;
    check_err(meshtastic_inbox_append(buf.buf, buf.len,
        (size_t)mp_obj_get_int(args[1]), (int32_t)mp_obj_get_int(args[2]),
        &m, text.buf, text.len, &got));
    mp_obj_t items[3] = {
        MP_OBJ_NEW_SMALL_INT(got.used),
        MP_OBJ_NEW_SMALL_INT(got.dropped),
        MP_OBJ_NEW_SMALL_INT(got.off),
    };
    return mp_obj_new_tuple(3, items);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_inbox_append_obj, 13, 13, mod_inbox_append);

// `after` is the offset of the previous match, or -1 to start. None back rather
// than -1, so a walk ends on a value that cannot be fed in again by accident.
static mp_obj_t mod_inbox_next(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t buf;
    mp_get_buffer_raise(args[0], &buf, MP_BUFFER_READ);
    int32_t at = meshtastic_inbox_next(buf.buf, buf.len,
        (size_t)mp_obj_get_int(args[1]), (int32_t)mp_obj_get_int(args[2]),
        (uint32_t)mp_obj_get_int_truncated(args[3]), (int32_t)mp_obj_get_int(args[4]),
        (uint32_t)mp_obj_get_int_truncated(args[5]));
    check_err(at < -1 ? at : 0);
    return at < 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(at);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_inbox_next_obj, 6, 6, mod_inbox_next);

static mp_obj_t mod_inbox_count(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t buf;
    mp_get_buffer_raise(args[0], &buf, MP_BUFFER_READ);
    int32_t n = meshtastic_inbox_count(buf.buf, buf.len,
        (size_t)mp_obj_get_int(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]), (int32_t)mp_obj_get_int(args[3]),
        (uint32_t)mp_obj_get_int_truncated(args[4]), mp_obj_is_true(args[5]));
    check_err(n);
    return MP_OBJ_NEW_SMALL_INT(n);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_inbox_count_obj, 6, 6, mod_inbox_count);

static mp_obj_t mod_inbox_mark(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t buf;
    mp_get_buffer_raise(args[0], &buf, MP_BUFFER_RW);
    int32_t n = meshtastic_inbox_mark(buf.buf, buf.len,
        (size_t)mp_obj_get_int(args[1]),
        (uint32_t)mp_obj_get_int_truncated(args[2]), (int32_t)mp_obj_get_int(args[3]),
        (uint32_t)mp_obj_get_int_truncated(args[4]));
    check_err(n);
    return MP_OBJ_NEW_SMALL_INT(n);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_inbox_mark_obj, 5, 5, mod_inbox_mark);

static mp_obj_t mod_inbox_read(mp_obj_t buf_in, mp_obj_t used_in, mp_obj_t off_in) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(buf_in, &buf, MP_BUFFER_READ);
    mesh_imessage_t m;
    check_err(meshtastic_inbox_read(buf.buf, buf.len,
        (size_t)mp_obj_get_int(used_in), (size_t)mp_obj_get_int(off_in), &m));
    mp_obj_t items[11] = {
        mp_obj_new_int_from_uint(m.id),
        mp_obj_new_int_from_uint(m.from),
        mp_obj_new_int_from_uint(m.to),
        m.time == 0 ? mp_const_none : mp_obj_new_int_from_uint(m.time),
        m.rssi == -32768 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(m.rssi),
        MP_OBJ_NEW_SMALL_INT(m.channel),
        m.hops == 255 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(m.hops),
        m.snr == -128 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(m.snr),
        MP_OBJ_NEW_SMALL_INT(m.flags),
        MP_OBJ_NEW_SMALL_INT(m.text_off),
        MP_OBJ_NEW_SMALL_INT(m.text_len),
    };
    return mp_obj_new_tuple(11, items);
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_inbox_read_obj, mod_inbox_read);

static mp_obj_t mod_inbox_brief(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t buf;
    mp_get_buffer_raise(args[0], &buf, MP_BUFFER_READ);
    mesh_brief_t b;
    check_err(meshtastic_inbox_brief(buf.buf, buf.len,
        (size_t)mp_obj_get_int(args[1]), (size_t)mp_obj_get_int(args[2]),
        (uint32_t)mp_obj_get_int_truncated(args[3]), &b));
    mp_obj_t items[3] = {
        mp_obj_new_int_from_uint(b.peer),
        mp_obj_new_bool(b.direct),
        mp_obj_new_bool(b.read),
    };
    return mp_obj_new_tuple(3, items);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_inbox_brief_obj, 4, 4, mod_inbox_brief);

// A field that was never sent comes back None. Absence is not zero: a node at
// the equator, at sea level, in role 0, with a flat battery, sends real zeros.
#define MESH_HAS(mask, bit) (((mask) & (1u << (bit))) != 0)
#define MESH_OPT_UINT(mask, bit, value) \
    (MESH_HAS(mask, bit) ? mp_obj_new_int_from_uint(value) : mp_const_none)
#define MESH_OPT_INT(mask, bit, value) \
    (MESH_HAS(mask, bit) ? mp_obj_new_int(value) : mp_const_none)

static mp_obj_t mod_payload_position(mp_obj_t body_in) {
    mp_buffer_info_t body;
    mp_get_buffer_raise(body_in, &body, MP_BUFFER_READ);
    mesh_position_t p;
    check_err(meshtastic_payload_position(body.buf, body.len, &p));
    // Latitude and longitude stay as the wire's 1e-7 degrees. Scaling them is
    // floating point, and floating point is the caller's side of this line.
    mp_obj_t items[5] = {
        MESH_OPT_INT(p.have, 0, p.lat),
        MESH_OPT_INT(p.have, 1, p.lon),
        MESH_OPT_INT(p.have, 2, p.alt),
        MESH_OPT_UINT(p.have, 3, p.sats),
        MESH_OPT_UINT(p.have, 4, p.precision),
    };
    return mp_obj_new_tuple(5, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_payload_position_obj, mod_payload_position);

static mp_obj_t mod_payload_user(mp_obj_t body_in) {
    mp_buffer_info_t body;
    mp_get_buffer_raise(body_in, &body, MP_BUFFER_READ);
    mesh_user_t u;
    check_err(meshtastic_payload_user(body.buf, body.len, &u));
    mp_obj_t items[7] = {
        MESH_OPT_UINT(u.have, 0, u.long_off),
        MESH_OPT_UINT(u.have, 0, u.long_len),
        MESH_OPT_UINT(u.have, 1, u.short_off),
        MESH_OPT_UINT(u.have, 1, u.short_len),
        MESH_OPT_UINT(u.have, 2, u.hw),
        MESH_OPT_UINT(u.have, 4, u.role),
        MESH_OPT_UINT(u.have, 3, u.licensed),
    };
    return mp_obj_new_tuple(7, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_payload_user_obj, mod_payload_user);

static mp_obj_t mod_payload_short_name(mp_obj_t body_in) {
    mp_buffer_info_t body;
    mp_get_buffer_raise(body_in, &body, MP_BUFFER_READ);
    mesh_variant_t v;
    check_err(meshtastic_payload_short_name(body.buf, body.len, &v));
    if (v.number == 0) {
        return mp_const_none;
    }
    mp_obj_t items[2] = {
        mp_obj_new_int_from_uint(v.off),
        mp_obj_new_int_from_uint(v.len),
    };
    return mp_obj_new_tuple(2, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_payload_short_name_obj, mod_payload_short_name);

static mp_obj_t mod_payload_device_metrics(mp_obj_t body_in) {
    mp_buffer_info_t body;
    mp_get_buffer_raise(body_in, &body, MP_BUFFER_READ);
    mesh_device_t d;
    check_err(meshtastic_payload_device_metrics(body.buf, body.len, &d));
    mp_obj_t items[5] = {
        MESH_OPT_UINT(d.have, 0, d.batt),
        MESH_OPT_UINT(d.have, 1, d.volt),
        MESH_OPT_UINT(d.have, 2, d.chan),
        MESH_OPT_UINT(d.have, 3, d.tx),
        MESH_OPT_UINT(d.have, 4, d.up),
    };
    return mp_obj_new_tuple(5, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_payload_device_metrics_obj, mod_payload_device_metrics);

static mp_obj_t mod_payload_environment_metrics(mp_obj_t body_in) {
    mp_buffer_info_t body;
    mp_get_buffer_raise(body_in, &body, MP_BUFFER_READ);
    mesh_env_t e;
    check_err(meshtastic_payload_environment_metrics(body.buf, body.len, &e));
    mp_obj_t items[3] = {
        MESH_OPT_UINT(e.have, 0, e.temp),
        MESH_OPT_UINT(e.have, 1, e.rh),
        MESH_OPT_UINT(e.have, 2, e.hpa),
    };
    return mp_obj_new_tuple(3, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_payload_environment_metrics_obj,
    mod_payload_environment_metrics);

static mp_obj_t mod_payload_telemetry(mp_obj_t body_in) {
    mp_buffer_info_t body;
    mp_get_buffer_raise(body_in, &body, MP_BUFFER_READ);
    mesh_variant_t v;
    check_err(meshtastic_payload_telemetry(body.buf, body.len, &v));
    if (v.number == 0) {
        return mp_const_none;
    }
    mp_obj_t items[3] = {
        mp_obj_new_int_from_uint(v.number),
        mp_obj_new_int_from_uint(v.off),
        mp_obj_new_int_from_uint(v.len),
    };
    return mp_obj_new_tuple(3, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_payload_telemetry_obj, mod_payload_telemetry);

static mp_obj_t mod_payload_traceroute(mp_obj_t body_in) {
    mp_buffer_info_t body;
    mp_get_buffer_raise(body_in, &body, MP_BUFFER_READ);
    mesh_route_t r;
    check_err(meshtastic_payload_traceroute(body.buf, body.len, &r));
    mp_obj_t items[8] = {
        MESH_OPT_UINT(r.have, 0, r.route_off),
        MESH_OPT_UINT(r.have, 0, r.route_len),
        MESH_OPT_UINT(r.have, 1, r.snr_out_off),
        MESH_OPT_UINT(r.have, 1, r.snr_out_len),
        MESH_OPT_UINT(r.have, 2, r.back_off),
        MESH_OPT_UINT(r.have, 2, r.back_len),
        MESH_OPT_UINT(r.have, 3, r.snr_back_off),
        MESH_OPT_UINT(r.have, 3, r.snr_back_len),
    };
    return mp_obj_new_tuple(8, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_payload_traceroute_obj, mod_payload_traceroute);

static mp_obj_t mod_payload_routing(mp_obj_t body_in) {
    mp_buffer_info_t body;
    mp_get_buffer_raise(body_in, &body, MP_BUFFER_READ);
    mesh_routing_t r;
    check_err(meshtastic_payload_routing(body.buf, body.len, &r));
    mp_obj_t items[2] = {
        mp_obj_new_int_from_uint(r.kind),
        mp_obj_new_int_from_uint(r.value),
    };
    return mp_obj_new_tuple(2, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_payload_routing_obj, mod_payload_routing);

static mp_obj_t mod_payload_sender_time(mp_obj_t body_in, mp_obj_t want_in) {
    mp_buffer_info_t body;
    mp_get_buffer_raise(body_in, &body, MP_BUFFER_READ);
    uint32_t out = 0;
    int32_t rc = meshtastic_payload_sender_time(
        body.buf, body.len, (uint32_t)mp_obj_get_int(want_in), &out);
    check_err(rc);
    // 1 is "no such field", which is an answer and not a failure.
    return rc == 1 ? mp_const_none : mp_obj_new_int_from_uint(out);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_payload_sender_time_obj, mod_payload_sender_time);

static mp_obj_t mod_payload_snr_at(mp_obj_t chunk_in, mp_obj_t at_in) {
    mp_buffer_info_t chunk;
    mp_get_buffer_raise(chunk_in, &chunk, MP_BUFFER_READ);
    int32_t snr = 0;
    int32_t next = meshtastic_payload_snr_at(
        chunk.buf, chunk.len, (size_t)mp_obj_get_int(at_in), &snr);
    check_err(next);
    mp_obj_t items[2] = {
        mp_obj_new_int(snr),
        mp_obj_new_int(next),
    };
    return mp_obj_new_tuple(2, items);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_payload_snr_at_obj, mod_payload_snr_at);

static mp_obj_t mod_nmea_reset(mp_obj_t state_in) {
    mp_buffer_info_t state;
    mp_get_buffer_raise(state_in, &state, MP_BUFFER_RW);
    check_err(meshtastic_nmea_reset(state.buf, state.len));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_nmea_reset_obj, mod_nmea_reset);

static mp_obj_t mod_nmea_sentence(mp_obj_t state_in, mp_obj_t line_in) {
    mp_buffer_info_t state, line;
    mp_get_buffer_raise(state_in, &state, MP_BUFFER_RW);
    mp_get_buffer_raise(line_in, &line, MP_BUFFER_READ);
    int32_t saw = meshtastic_nmea_sentence(state.buf, state.len, line.buf, line.len);
    // A sentence that failed its checksum is an answer about the line, not a
    // fault in the caller, so it comes back as a value rather than an error.
    if (saw == -14) {
        return MP_OBJ_NEW_SMALL_INT(-1);
    }
    check_err(saw);
    return MP_OBJ_NEW_SMALL_INT(saw);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_nmea_sentence_obj, mod_nmea_sentence);

static mp_obj_t mod_nmea_feed(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t state, chunk, echo;
    mp_get_buffer_raise(args[0], &state, MP_BUFFER_RW);
    mp_get_buffer_raise(args[1], &chunk, MP_BUFFER_READ);
    // The echo is optional: a caller that only wants the fix passes None and
    // the sentences are parsed without ever being copied anywhere.
    if (args[2] == mp_const_none || !mp_get_buffer(args[2], &echo, MP_BUFFER_RW)) {
        echo.buf = NULL;
        echo.len = 0;
    }
    mesh_feed_t f;
    check_err(meshtastic_nmea_feed(state.buf, state.len, chunk.buf, chunk.len,
        echo.buf, echo.len, (size_t)mp_obj_get_int(args[3]), &f));
    mp_obj_t items[2] = {
        MP_OBJ_NEW_SMALL_INT(f.landed),
        MP_OBJ_NEW_SMALL_INT(f.echo_used),
    };
    return mp_obj_new_tuple(2, items);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_nmea_feed_obj, 4, 4, mod_nmea_feed);

static mp_obj_t mod_nmea_fix(mp_obj_t state_in) {
    mp_buffer_info_t state;
    mp_get_buffer_raise(state_in, &state, MP_BUFFER_READ);
    mesh_fix_t f;
    check_err(meshtastic_nmea_fix(state.buf, state.len, &f));
    mp_obj_t items[14] = {
        MESH_OPT_INT(f.have, 0, f.lat_e7),
        MESH_OPT_INT(f.have, 0, f.lon_e7),
        MESH_OPT_INT(f.have, 1, f.alt_mm),
        MESH_OPT_UINT(f.have, 2, f.when),
        f.sats == 255 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(f.sats),
        f.in_view == 255 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(f.in_view),
        MP_OBJ_NEW_SMALL_INT(f.quality),
        f.status == 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(f.status),
        f.mode == 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(f.mode),
        f.nav == 0 ? mp_const_none : MP_OBJ_NEW_SMALL_INT(f.nav),
        f.hdop_cm == 0xFFFF ? mp_const_none : MP_OBJ_NEW_SMALL_INT(f.hdop_cm),
        mp_obj_new_int_from_uint(f.good),
        mp_obj_new_int_from_uint(f.bad),
        mp_obj_new_bool(f.valid),
    };
    return mp_obj_new_tuple(14, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_nmea_fix_obj, mod_nmea_fix);

static mp_obj_t mod_nmea_talker(mp_obj_t state_in, mp_obj_t index_in) {
    mp_buffer_info_t state;
    mp_get_buffer_raise(state_in, &state, MP_BUFFER_READ);
    uint8_t id[2] = { 0, 0 };
    uint8_t count = 0;
    int32_t got = meshtastic_nmea_talker(state.buf, state.len,
        (size_t)mp_obj_get_int(index_in), id, &count);
    check_err(got);
    if (got == 0) {
        return mp_const_none;
    }
    mp_obj_t items[2] = {
        mp_obj_new_bytes(id, 2),
        MP_OBJ_NEW_SMALL_INT(count),
    };
    return mp_obj_new_tuple(2, items);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_nmea_talker_obj, mod_nmea_talker);

static mp_obj_t mod_nmea_civil(mp_obj_t when_in) {
    mesh_civil_t c;
    check_err(meshtastic_nmea_civil((uint32_t)mp_obj_get_int_truncated(when_in), &c));
    mp_obj_t items[6] = {
        MP_OBJ_NEW_SMALL_INT(c.year),
        MP_OBJ_NEW_SMALL_INT(c.month),
        MP_OBJ_NEW_SMALL_INT(c.day),
        MP_OBJ_NEW_SMALL_INT(c.hour),
        MP_OBJ_NEW_SMALL_INT(c.minute),
        MP_OBJ_NEW_SMALL_INT(c.second),
    };
    return mp_obj_new_tuple(6, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_nmea_civil_obj, mod_nmea_civil);

static mp_obj_t mod_stream_reset(mp_obj_t state_in) {
    mp_buffer_info_t state;
    mp_get_buffer_raise(state_in, &state, MP_BUFFER_RW);
    check_err(meshtastic_stream_reset(state.buf, state.len));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_stream_reset_obj, mod_stream_reset);

// Returns (frame_len, used). The frame itself stays in the state at
// STREAM_BODY, so a caller reads it with a memoryview and nothing is copied.
static mp_obj_t mod_stream_feed(mp_obj_t state_in, mp_obj_t chunk_in, mp_obj_t at_in) {
    mp_buffer_info_t state, chunk;
    mp_get_buffer_raise(state_in, &state, MP_BUFFER_RW);
    mp_get_buffer_raise(chunk_in, &chunk, MP_BUFFER_READ);
    mesh_stream_t s;
    check_err(meshtastic_stream_feed(state.buf, state.len, chunk.buf, chunk.len,
        (size_t)mp_obj_get_int(at_in), &s));
    mp_obj_t items[2] = {
        MP_OBJ_NEW_SMALL_INT(s.frame_len),
        MP_OBJ_NEW_SMALL_INT(s.used),
    };
    return mp_obj_new_tuple(2, items);
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_stream_feed_obj, mod_stream_feed);

static mp_obj_t mod_stream_counts(mp_obj_t state_in) {
    mp_buffer_info_t state;
    mp_get_buffer_raise(state_in, &state, MP_BUFFER_READ);
    mesh_stream_counts_t c;
    check_err(meshtastic_stream_counts(state.buf, state.len, &c));
    mp_obj_t items[3] = {
        mp_obj_new_int_from_uint(c.lost),
        mp_obj_new_int_from_uint(c.frames),
        MP_OBJ_NEW_SMALL_INT(c.partial),
    };
    return mp_obj_new_tuple(3, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_stream_counts_obj, mod_stream_counts);

static mp_obj_t mod_stream_frame(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t out, payload;
    mp_get_buffer_raise(args[0], &out, MP_BUFFER_RW);
    mp_get_buffer_raise(args[2], &payload, MP_BUFFER_READ);
    int32_t used = meshtastic_stream_frame(out.buf, out.len,
        (size_t)mp_obj_get_int(args[1]), payload.buf, payload.len);
    check_err(used);
    return MP_OBJ_NEW_SMALL_INT(used);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_stream_frame_obj, 3, 3, mod_stream_frame);

static mp_obj_t mod_position_encode(size_t n_args, const mp_obj_t *args) {
    mp_buffer_info_t out, state;
    mp_get_buffer_raise(args[0], &out, MP_BUFFER_RW);
    mp_get_buffer_raise(args[1], &state, MP_BUFFER_READ);
    uint32_t precision = n_args > 2 ? (uint32_t)mp_obj_get_int(args[2]) : 32;
    int32_t used = meshtastic_position_encode(out.buf, out.len,
        state.buf, state.len, precision);
    check_err(used);
    return MP_OBJ_NEW_SMALL_INT(used);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_position_encode_obj, 2, 3, mod_position_encode);


// Everything the module exports, named once. A natmod stores these into its
// globals at load; a built-in lays them out as a const table in flash.
#define MESHTASTIC_EXPORTS(FUN, INT) \
    FUN(MP_QSTR_parse_header, mod_parse_header_obj) \
    FUN(MP_QSTR_build_flags, mod_build_flags_obj) \
    FUN(MP_QSTR_djb2, mod_djb2_obj) \
    FUN(MP_QSTR_battery_percent, mod_battery_percent_obj) \
    INT(MP_QSTR_NO_BATTERY_MV, 2600) \
    FUN(MP_QSTR_channel_hash, mod_channel_hash_obj) \
    FUN(MP_QSTR_preset_params, mod_preset_params_obj) \
    FUN(MP_QSTR_region_info, mod_region_info_obj) \
    FUN(MP_QSTR_num_channels, mod_num_channels_obj) \
    FUN(MP_QSTR_slot_frequency, mod_slot_frequency_obj) \
    FUN(MP_QSTR_channel_frequency, mod_channel_frequency_obj) \
    FUN(MP_QSTR_parse_data, mod_parse_data_obj) \
    FUN(MP_QSTR_proto_field, mod_proto_field_obj) \
    FUN(MP_QSTR_write_header, mod_write_header_obj) \
    FUN(MP_QSTR_encode_data, mod_encode_data_obj) \
    FUN(MP_QSTR_airtime_us, mod_airtime_us_obj) \
    FUN(MP_QSTR_roster_slot, mod_roster_slot_obj) \
    FUN(MP_QSTR_roster_free, mod_roster_free_obj) \
    FUN(MP_QSTR_roster_oldest, mod_roster_oldest_obj) \
    FUN(MP_QSTR_roster_used, mod_roster_used_obj) \
    FUN(MP_QSTR_roster_read, mod_roster_read_obj) \
    FUN(MP_QSTR_roster_touch, mod_roster_touch_obj) \
    FUN(MP_QSTR_roster_restore, mod_roster_restore_obj) \
    FUN(MP_QSTR_roster_identify, mod_roster_identify_obj) \
    FUN(MP_QSTR_roster_clear, mod_roster_clear_obj) \
    INT(MP_QSTR_ROW_BYTES, 24) \
    FUN(MP_QSTR_cache_length, mod_cache_length_obj) \
    FUN(MP_QSTR_cache_verify, mod_cache_verify_obj) \
    FUN(MP_QSTR_cache_record, mod_cache_record_obj) \
    FUN(MP_QSTR_cache_read_peer, mod_cache_read_peer_obj) \
    FUN(MP_QSTR_cache_read_message, mod_cache_read_message_obj) \
    FUN(MP_QSTR_cache_peer_len, mod_cache_peer_len_obj) \
    FUN(MP_QSTR_cache_message_len, mod_cache_message_len_obj) \
    FUN(MP_QSTR_cache_write_peer, mod_cache_write_peer_obj) \
    FUN(MP_QSTR_cache_write_message, mod_cache_write_message_obj) \
    FUN(MP_QSTR_cache_finish, mod_cache_finish_obj) \
    INT(MP_QSTR_CACHE_HEADER, 10) \
    INT(MP_QSTR_CACHE_NODE, 1) \
    INT(MP_QSTR_CACHE_MESSAGE, 2) \
    FUN(MP_QSTR_store_length, mod_store_length_obj) \
    FUN(MP_QSTR_store_verify, mod_store_verify_obj) \
    FUN(MP_QSTR_store_record, mod_store_record_obj) \
    FUN(MP_QSTR_store_put, mod_store_put_obj) \
    FUN(MP_QSTR_store_finish, mod_store_finish_obj) \
    FUN(MP_QSTR_duty_record, mod_duty_record_obj) \
    FUN(MP_QSTR_duty_used, mod_duty_used_obj) \
    FUN(MP_QSTR_pb_uint, mod_pb_uint_obj) \
    FUN(MP_QSTR_pb_int32, mod_pb_int32_obj) \
    FUN(MP_QSTR_pb_fixed32, mod_pb_fixed32_obj) \
    FUN(MP_QSTR_pb_blob_head, mod_pb_blob_head_obj) \
    FUN(MP_QSTR_pb_take, mod_pb_take_obj) \
    INT(MP_QSTR_STORE_HEADER, 10) \
    INT(MP_QSTR_STORE_TAG, 2) \
    INT(MP_QSTR_STORE_MAX_VALUE, 255) \
    FUN(MP_QSTR_inbox_append, mod_inbox_append_obj) \
    FUN(MP_QSTR_inbox_next, mod_inbox_next_obj) \
    FUN(MP_QSTR_inbox_count, mod_inbox_count_obj) \
    FUN(MP_QSTR_inbox_mark, mod_inbox_mark_obj) \
    FUN(MP_QSTR_inbox_read, mod_inbox_read_obj) \
    FUN(MP_QSTR_inbox_brief, mod_inbox_brief_obj) \
    INT(MP_QSTR_INBOX_FIXED, 23) \
    INT(MP_QSTR_INBOX_MAX_TEXT, 200) \
    INT(MP_QSTR_INBOX_READ, 1) \
    INT(MP_QSTR_INBOX_ALL, 0) \
    INT(MP_QSTR_INBOX_CHANNEL, 1) \
    INT(MP_QSTR_INBOX_DIRECT, 2) \
    FUN(MP_QSTR_payload_position, mod_payload_position_obj) \
    FUN(MP_QSTR_payload_user, mod_payload_user_obj) \
    FUN(MP_QSTR_payload_short_name, mod_payload_short_name_obj) \
    FUN(MP_QSTR_payload_device_metrics, mod_payload_device_metrics_obj) \
    FUN(MP_QSTR_payload_environment_metrics, mod_payload_environment_metrics_obj) \
    FUN(MP_QSTR_payload_telemetry, mod_payload_telemetry_obj) \
    FUN(MP_QSTR_payload_traceroute, mod_payload_traceroute_obj) \
    FUN(MP_QSTR_payload_routing, mod_payload_routing_obj) \
    FUN(MP_QSTR_payload_sender_time, mod_payload_sender_time_obj) \
    FUN(MP_QSTR_payload_snr_at, mod_payload_snr_at_obj) \
    INT(MP_QSTR_ROUTE_NONE, 0) \
    INT(MP_QSTR_ROUTE_ERROR, 1) \
    INT(MP_QSTR_ROUTE_REQUEST, 2) \
    INT(MP_QSTR_ROUTE_REPLY, 3) \
    FUN(MP_QSTR_nmea_reset, mod_nmea_reset_obj) \
    FUN(MP_QSTR_nmea_sentence, mod_nmea_sentence_obj) \
    FUN(MP_QSTR_nmea_feed, mod_nmea_feed_obj) \
    FUN(MP_QSTR_nmea_fix, mod_nmea_fix_obj) \
    FUN(MP_QSTR_nmea_talker, mod_nmea_talker_obj) \
    FUN(MP_QSTR_nmea_civil, mod_nmea_civil_obj) \
    INT(MP_QSTR_NMEA_STATE, 144) \
    INT(MP_QSTR_NMEA_TALKERS, 8) \
    INT(MP_QSTR_NMEA_SAW_GGA, 1) \
    INT(MP_QSTR_NMEA_SAW_RMC, 2) \
    INT(MP_QSTR_NMEA_SAW_GSV, 4) \
    FUN(MP_QSTR_stream_reset, mod_stream_reset_obj) \
    FUN(MP_QSTR_stream_feed, mod_stream_feed_obj) \
    FUN(MP_QSTR_stream_counts, mod_stream_counts_obj) \
    FUN(MP_QSTR_stream_frame, mod_stream_frame_obj) \
    INT(MP_QSTR_STREAM_STATE, 528) \
    INT(MP_QSTR_STREAM_BODY, 16) \
    INT(MP_QSTR_STREAM_MAX, 512) \
    INT(MP_QSTR_STREAM_HEADER, 4) \
    FUN(MP_QSTR_position_encode, mod_position_encode_obj) \
    INT(MP_QSTR_PRECISION_FULL, 32)

#if defined(MICROPY_ENABLE_DYNRUNTIME) && MICROPY_ENABLE_DYNRUNTIME

#define MESHTASTIC_STORE_FUN(qstr, obj) mp_store_global(qstr, MP_OBJ_FROM_PTR(&obj));
#define MESHTASTIC_STORE_INT(qstr, value) mp_store_global(qstr, MP_OBJ_NEW_SMALL_INT(value));

mp_obj_t mpy_init(mp_obj_fun_bc_t *self, size_t n_args, size_t n_kw, mp_obj_t *args) {
    MP_DYNRUNTIME_INIT_ENTRY

    MESHTASTIC_EXPORTS(MESHTASTIC_STORE_FUN, MESHTASTIC_STORE_INT)

    MP_DYNRUNTIME_INIT_EXIT
}

#else

#define MESHTASTIC_ROW_FUN(qstr, obj) { MP_ROM_QSTR(qstr), MP_ROM_PTR(&obj) },
#define MESHTASTIC_ROW_INT(qstr, value) { MP_ROM_QSTR(qstr), MP_ROM_INT(value) },

static const mp_rom_map_elem_t meshtastic_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_meshtastic) },
    MESHTASTIC_EXPORTS(MESHTASTIC_ROW_FUN, MESHTASTIC_ROW_INT)
};
static MP_DEFINE_CONST_DICT(meshtastic_module_globals, meshtastic_module_globals_table);

const mp_obj_module_t meshtastic_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&meshtastic_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_meshtastic, meshtastic_module);

#endif
