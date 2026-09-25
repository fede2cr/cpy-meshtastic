"""Enough of CircuitPython and the two native modules to run this on a host.

The Python layers can be exercised without hardware, but only if `aesio`, the
`lr1121`/`meshtastic` natmods and `microcontroller` exist as importable
names. These are stand-ins, not a second implementation: the protobuf and
airtime routines here are short enough to be obviously right, and where one of
them disagreed with the Rust it was this file that was wrong, twice. So a pass
here says the Python wiring holds together, and says nothing at all about the
crates -- `cargo test` covers those.

Where CircuitPython has less than CPython, the stub takes things away rather
than adding them, because the failures worth catching here are the ones the
board would raise and a host would not.

    import sys; sys.path.insert(0, "tools")
    import hoststub
    import meshtastic, mesh_tx
"""

import binascii
import struct
import sys
import types

sys.path.insert(0, "python")

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# --------------------------------------------------------------------- struct

# CircuitPython's struct is five functions with no Struct class. CPython has
# both, so without narrowing it the host accepts code the board rejects at
# import -- which is exactly how `struct.Struct` reached the drive once.
_struct = types.ModuleType("struct")
for _name in ("calcsize", "pack", "pack_into", "unpack", "unpack_from"):
    setattr(_struct, _name, getattr(struct, _name))
sys.modules["struct"] = _struct


# ------------------------------------------------------------------------- gc

# CircuitPython's gc reports the heap; CPython's does not. The number is only
# ever printed, so a constant is enough to keep the code that prints it tested.
import gc as _gc  # noqa: E402

if not hasattr(_gc, "mem_free"):
    _gc.mem_free = lambda: 0


# ------------------------------------------------------------ microcontroller


class _Nvm:
    """One flash page, and a count of how often it was rewritten.

    The nRF backend erases and rewrites the whole page for a write of any
    length, so the count is erase cycles rather than bytes. That is the number
    `keystore.save` exists to keep down, and the only way to assert on it.
    """

    def __init__(self, size=4096):
        self._page = bytearray(b"\xff" * size)
        self.writes = 0

    def __len__(self):
        return len(self._page)

    def __getitem__(self, index):
        return self._page[index]

    def __setitem__(self, index, value):
        self.writes += 1
        size = len(self._page)
        self._page[index] = value
        if len(self._page) != size:
            # A bytearray would resize here; a flash page cannot.
            raise ValueError("a write changed the size of the page")


_mc = types.ModuleType("microcontroller")
_mc.nvm = _Nvm()
_mc.cpu = types.SimpleNamespace(uid=bytes.fromhex("d31b7c4a5e6f8091"))
sys.modules["microcontroller"] = _mc


# --------------------------------------------------------------------- board

# Pins by name, invented on demand: which pins a board has is the one thing the
# host cannot know, and nothing under test does more than pass them along.
_board = types.ModuleType("board")
_board.__getattr__ = lambda name: "pin:%s" % name
sys.modules["board"] = _board


# ----------------------------------------------------------------------- rtc

# The board clock. Keeps what was written somewhere a test can read it back:
# `rtc.RTC()` builds a fresh object every call, so an instance attribute would
# be lost the moment the setter returned.
_rtc = types.ModuleType("rtc")


class _RTC:
    last = None

    @property
    def datetime(self):
        return _RTC.last

    @datetime.setter
    def datetime(self, value):
        _RTC.last = value


_rtc.RTC = _RTC
sys.modules["rtc"] = _rtc


# ----------------------------------------------------------------- supervisor

# Stands in for settings.toml. Left empty so the host exercises the defaults;
# assign into `settings` to test a configured board.
_sup = types.ModuleType("supervisor")
_sup.settings = {}
_sup.get_setting = lambda name, default=None: _sup.settings.get(name, default)
sys.modules["supervisor"] = _sup


# ---------------------------------------------------------------------- aesio

_aesio = types.ModuleType("aesio")
_aesio.MODE_CTR = 6


class AES:
    def __init__(self, key, mode, iv):
        self._c = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()

    def encrypt_into(self, src, dst):
        dst[:] = self._c.update(bytes(src))

    decrypt_into = encrypt_into


_aesio.AES = AES
sys.modules["aesio"] = _aesio


# --------------------------------------------------------------------- _bleio

# Enough of the adapter to check what would be advertised, and enough of the
# GATT server to run a whole phone session through. The SoftDevice is not
# modelled: there is no attribute table, no MTU and no connection. What is
# modelled is the part with behaviour worth testing -- the read queue, whose
# rule is that one read hands out one whole packet and an empty queue reads
# back as zero bytes, and the packet buffer, whose rule is that write
# boundaries survive.


class _Adapter:
    def __init__(self):
        self.enabled = False
        self.name = None
        self.advertising = False
        self.data = None
        self.scan_response = None
        #: Truthy while a client is attached. Tests set it directly.
        self.connections = ()

    def start_advertising(self, data, *, scan_response=None, **kwargs):
        if self.advertising:
            raise RuntimeError("Already advertising.")
        self.advertising = True
        self.data = bytes(data)
        self.scan_response = bytes(scan_response) if scan_response else b""

    def stop_advertising(self):
        self.advertising = False


class _UUID:
    def __init__(self, value):
        self.value = value


class _Service:
    def __init__(self, uuid, **kwargs):
        self.uuid = uuid
        self.characteristics = []


class _Characteristic:
    BROADCAST = 1 << 0
    INDICATE = 1 << 1
    NOTIFY = 1 << 2
    READ = 1 << 3
    WRITE = 1 << 4
    WRITE_NO_RESPONSE = 1 << 5
    READ_QUEUE = 1 << 6

    def __init__(self, uuid, properties, max_length, initial_value):
        self.uuid = uuid
        self.properties = properties
        self.max_length = max_length
        self.value = initial_value
        self._queue = None
        self._limit = 0
        #: What a client wrote, oldest first. Filled by tests, drained by
        #: PacketBuffer.
        self.written = []

    @classmethod
    def add_to_service(cls, service, uuid, *, properties=0, max_length=20,
                       fixed_length=False, initial_value=None, **kwargs):
        char = cls(uuid, properties, max_length, initial_value)
        service.characteristics.append(char)
        return char

    def serve_reads(self, buffer_size):
        if not self.properties & _Characteristic.READ_QUEUE:
            raise ValueError("Characteristic must have READ_QUEUE property")
        self._limit = buffer_size
        self._queue = []

    def queue_read(self, data):
        if self._queue is None:
            raise ValueError("serve_reads() has not been called")
        if len(data) > self.max_length:
            raise ValueError("Value length > max_length")
        held = sum(len(p) + 2 for p in self._queue)
        if held + len(data) + 2 > self._limit:
            return False
        self._queue.append(bytes(data))
        return True

    def read(self):
        """What a client's read would return. Not part of the real API."""
        if not self._queue:
            return b""
        return self._queue.pop(0)


class _PacketBuffer:
    def __init__(self, characteristic, *, buffer_size, max_packet_size=None):
        self.characteristic = characteristic
        self.buffer_size = buffer_size

    def readinto(self, buf):
        written = self.characteristic.written
        if not written:
            return 0
        packet = written.pop(0)
        buf[:len(packet)] = packet
        return len(packet)


_bleio = types.ModuleType("_bleio")
_bleio.adapter = _Adapter()
_bleio.UUID = _UUID
_bleio.Service = _Service
_bleio.Characteristic = _Characteristic
_bleio.PacketBuffer = _PacketBuffer
sys.modules["_bleio"] = _bleio


# --------------------------------------------------------------- _meshtastic

_mt = types.ModuleType("meshtastic")


def _xor(data):
    out = 0
    for byte in data:
        out ^= byte
    return out


def _varint(buf, at):
    value = shift = 0
    while True:
        byte = buf[at]
        at += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, at


def _put(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def parse_header(frame):
    to, frm, pid, flags, ch, nh, relay = struct.unpack("<III4B", frame[:16])
    start = (flags & 0xE0) >> 5
    limit = flags & 7
    return (to, frm, pid, flags, ch, nh, relay, limit, start,
            bool(flags & 8), bool(flags & 0x10),
            (start - limit) if start else None)


def proto_field(buf, at):
    key, at = _varint(buf, at)
    number, wire = key >> 3, key & 7
    off = length = 0
    if wire == 0:
        value, at = _varint(buf, at)
    elif wire == 2:
        length, at = _varint(buf, at)
        off, value = at, 0
        at += length
    elif wire == 5:
        value = struct.unpack("<I", buf[at:at + 4])[0]
        at += 4
    elif wire == 1:
        value = struct.unpack("<Q", buf[at:at + 8])[0]
        at += 8
    else:
        raise ValueError("wire type %d" % wire)
    return number, wire, value & 0xFFFFFFFF, value >> 32, off, length, at


def encode_data(portnum, payload, want_response, bitfield):
    out = bytearray()
    if portnum:
        out += b"\x08" + _put(portnum)
    if len(payload):
        out += b"\x12" + _put(len(payload)) + bytes(payload)
    if want_response:
        out += b"\x18\x01"
    if bitfield is not None:
        out += b"\x48" + _put(bitfield)
    return bytes(out)


def parse_data(buf):
    portnum = off = length = bitfield = 0
    # Addressing fields 4..8 are what an answer carries and a broadcast does
    # not, so a stub that dropped them would make the two look alike here and
    # only here.
    ids = {4: 0, 5: 0, 6: 0, 7: 0, 8: 0}
    want_response = has_bitfield = False
    at = 0
    while at < len(buf):
        number, _wire, lo, _hi, o, l, at = proto_field(buf, at)
        if number == 1:
            portnum = lo
        elif number == 2:
            off, length = o, l
        elif number == 3:
            want_response = bool(lo)
        elif number in ids:
            ids[number] = lo
        elif number == 9:
            bitfield, has_bitfield = lo, True
    return (portnum, off, length, ids[4], ids[5], ids[6], ids[7], ids[8],
            want_response, bitfield if has_bitfield else None)


def airtime_us(payload_len, sf, bw_hz, cr):
    de = 1 if (1 << sf) * 1_000_000 // bw_hz > 16_000 else 0
    bits = 8 * payload_len - 4 * sf + 28 + 16
    per_block = 4 * (sf - 2 * de)
    blocks = 0 if bits <= 0 else -(-bits // per_block)
    quarters = 4 * 16 + 17 + 4 * (8 + blocks * cr)
    return quarters * (1 << sf) * 1_000_000 // (4 * bw_hz)


def _duty_expire(window, slot):
    """Mirrors duty::expire in meshtastic/src/duty.rs."""
    n = (len(window) - 4) // 4
    last = struct.unpack_from("<I", window, 0)[0]
    if slot == last:
        return n
    if slot < last or slot - last >= n:
        for k in range(n):
            struct.pack_into("<I", window, 4 + k * 4, 0)
    else:
        for k in range(1, slot - last + 1):
            struct.pack_into("<I", window, 4 + ((last + k) % n) * 4, 0)
    struct.pack_into("<I", window, 0, slot)
    return n


def duty_record(window, slot, us):
    n = _duty_expire(window, slot)
    at = 4 + (slot % n) * 4
    was = struct.unpack_from("<I", window, at)[0]
    struct.pack_into("<I", window, at, min(0xFFFFFFFF, was + us))


def duty_used(window, slot):
    n = _duty_expire(window, slot)
    return min(0xFFFFFFFF,
               sum(struct.unpack_from("<I", window, 4 + k * 4)[0]
                   for k in range(n)))


def _put_varint(out, at, value):
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out[at] = byte | 0x80
            at += 1
        else:
            out[at] = byte
            return at + 1


def _put_tag(out, at, number, wire):
    return _put_varint(out, at, (number << 3) | wire)


def pb_uint(out, at, number, value):
    return _put_varint(out, _put_tag(out, at, number, 0), value)


def pb_int32(out, at, number, value):
    return pb_uint(out, at, number, value & 0xFFFFFFFFFFFFFFFF)


def pb_fixed32(out, at, number, value):
    at = _put_tag(out, at, number, 5)
    struct.pack_into("<I", out, at, value & 0xFFFFFFFF)
    return at + 4


def pb_blob_head(out, at, number, length):
    return _put_varint(out, _put_tag(out, at, number, 2), length)


def pb_take(out, at):
    if at > len(out):
        raise ValueError("past the end")
    return bytes(out[:at])


# A second reading of the same table as meshtastic/src/power.rs, for the same
# reason as the roster below: a disagreement shows up as a failing host test.
_OCV = (4190, 4050, 3990, 3890, 3800, 3720, 3630, 3530, 3420, 3300, 3100)


def battery_percent(millivolts):
    last = len(_OCV) - 1
    for i, step_mv in enumerate(_OCV):
        if step_mv <= millivolts:
            if i == 0:
                return 100
            span = _OCV[i - 1] - step_mv
            return min(100, 10 * (last - i) + 10 * (millivolts - step_mv) // span)
    return 0


_mt.channel_hash = lambda name, psk: _xor(name) ^ _xor(psk)
_mt.djb2 = lambda data: 5381
_mt.build_flags = lambda hop, ack, mqtt, start: (
    min(hop, 7) | (0x08 if ack else 0) | (0x10 if mqtt else 0)
    | ((start << 5) & 0xE0))
_mt.write_header = lambda to, frm, pid, flags, ch, nh, relay: struct.pack(
    "<III4B", to, frm, pid, flags, ch, nh, relay)
_mt.parse_header = parse_header
_mt.proto_field = proto_field
_mt.encode_data = encode_data
_mt.parse_data = parse_data
_mt.airtime_us = airtime_us
_mt.duty_record = duty_record
_mt.duty_used = duty_used
_mt.pb_uint = pb_uint
_mt.pb_int32 = pb_int32
_mt.pb_fixed32 = pb_fixed32
_mt.pb_blob_head = pb_blob_head
_mt.pb_take = pb_take
_mt.battery_percent = battery_percent
_mt.NO_BATTERY_MV = 2600

# ---- the roster table, mirroring meshtastic/src/roster.rs
#
# A second implementation of the same layout, which is the point: the board
# runs the Rust one and the host runs this, so a disagreement about where a
# field sits shows up as a failing host test rather than as a corrupt roster.

ROW_BYTES = 24
_ROW = "<IiiIHBBBbh"


def _row_at(table, at):
    if at % ROW_BYTES or at + ROW_BYTES > len(table):
        raise ValueError("slot index out of range")
    return at


def roster_slot(table, num):
    if not num:
        return None
    for at in range(0, len(table) - ROW_BYTES + 1, ROW_BYTES):
        if struct.unpack_from("<I", table, at)[0] == num:
            return at
    return None


def roster_free(table):
    for at in range(0, len(table) - ROW_BYTES + 1, ROW_BYTES):
        if not struct.unpack_from("<I", table, at)[0]:
            return at
    return None


def roster_oldest(table):
    best = None
    for at in range(0, len(table) - ROW_BYTES + 1, ROW_BYTES):
        if not struct.unpack_from("<I", table, at)[0]:
            continue
        seen = struct.unpack_from("<I", table, at + 12)[0]
        if best is None or seen < best[0]:
            best = (seen, at)
    return None if best is None else best[1]


def roster_used(table):
    return sum(1 for at in range(0, len(table) - ROW_BYTES + 1, ROW_BYTES)
               if struct.unpack_from("<I", table, at)[0])


def roster_read(table, at):
    num, first, last, seen, count, hops, hw, role, snr, rssi = (
        struct.unpack_from(_ROW, table, _row_at(table, at)))
    return (num, first, last, seen, count,
            None if hops == 0 else hops - 1, hw, role,
            None if snr == -128 else snr, None if rssi == 0 else rssi)


def _pack_hops(hops):
    return 0 if hops is None or hops < 0 else min(hops + 1, 255)


def _pack_snr(quarters):
    if quarters is None:
        return -128
    if not isinstance(quarters, int):
        raise TypeError("can't convert float to int")
    return min(max(quarters, -127), 127)


def _pack_rssi(rssi):
    if rssi is None:
        return 0
    # The C shim takes an int and refuses a float, so refuse one here too: a
    # host that quietly rounded would pass what the board raises on.
    if not isinstance(rssi, int):
        raise TypeError("can't convert float to int")
    return min(max(rssi, -32768), 0)


def roster_touch(table, at, num, now, tick, hops, snr_q, rssi):
    at = _row_at(table, at)
    fresh = not struct.unpack_from("<I", table, at)[0]
    if fresh:
        struct.pack_into("<Ii", table, at, num, now)
    struct.pack_into("<iI", table, at + 8, now, tick)
    count = struct.unpack_from("<H", table, at + 16)[0]
    if count < 0xFFFF:
        struct.pack_into("<H", table, at + 16, count + 1)
    table[at + 18] = _pack_hops(hops)
    struct.pack_into("<b", table, at + 21, _pack_snr(snr_q))
    struct.pack_into("<h", table, at + 22, _pack_rssi(rssi))
    return fresh


def roster_restore(table, at, num, when, tick, count, hops, hw, role, snr_q):
    at = _row_at(table, at)
    struct.pack_into(_ROW, table, at, num, when, when, tick,
                     min(count, 0xFFFF), _pack_hops(hops), hw, role,
                     _pack_snr(snr_q), 0)


def roster_identify(table, at, hw, role):
    at = _row_at(table, at)
    table[at + 19] = hw & 0xFF
    table[at + 20] = role & 0xFF


def roster_clear(table, at):
    at = _row_at(table, at)
    for i in range(ROW_BYTES):
        table[at + i] = 0


_mt.ROW_BYTES = ROW_BYTES
_mt.roster_slot = roster_slot
_mt.roster_free = roster_free
_mt.roster_oldest = roster_oldest
_mt.roster_used = roster_used
_mt.roster_read = roster_read
_mt.roster_touch = roster_touch
_mt.roster_restore = roster_restore
_mt.roster_identify = roster_identify
_mt.roster_clear = roster_clear

# ---- the cache records, mirroring meshtastic/src/cache.rs

CACHE_HEADER = 10
CACHE_NODE = 1
CACHE_MESSAGE = 2
_MAGIC = b"MKC1"
_KIND_LEN = 3
_NODE_FIXED = 14
_MESSAGE_FIXED = 20


def _name_len(size):
    return 1 if size is None or size > 254 else 1 + size


def cache_peer_len(short, long):
    return _KIND_LEN + _NODE_FIXED + _name_len(short) + _name_len(long)


def cache_message_len(text):
    return _KIND_LEN + _MESSAGE_FIXED + text


def _put_name(out, at, name):
    if name is None or len(name) > 254:
        out[at] = 255
        return at + 1
    out[at] = len(name)
    out[at + 1:at + 1 + len(name)] = name
    return at + 1 + len(name)


def _take_name(raw, at, end):
    if at >= end:
        raise ValueError("cache name runs past the record")
    size = raw[at]
    if size == 255:
        return 0, -1, at + 1
    if at + 1 + size > end:
        raise ValueError("cache name runs past the record")
    return at + 1, size, at + 1 + size


def cache_write_peer(out, at, num, age, count, hops, hw, role, snr_q,
                     short, long):
    size = cache_peer_len(None if short is None else len(short),
                          None if long is None else len(long))
    if at + size > len(out):
        raise ValueError("output buffer too small")
    struct.pack_into("<BHIIHBBBb", out, at, CACHE_NODE, size - _KIND_LEN,
                     num, age, min(count, 0xFFFF), _pack_hops(hops),
                     hw, role, _pack_snr(snr_q))
    return _put_name(out, _put_name(out, at + _KIND_LEN + _NODE_FIXED, short),
                     long)


def cache_write_message(out, at, ident, from_, to, when, flags, channel,
                        hops, snr_q, text):
    size = cache_message_len(len(text))
    if at + size > len(out):
        raise ValueError("output buffer too small")
    struct.pack_into("<BHIIIIBBBb", out, at, CACHE_MESSAGE, size - _KIND_LEN,
                     ident, from_, to, when, flags, channel,
                     _pack_hops(hops), _pack_snr(snr_q))
    start = at + _KIND_LEN + _MESSAGE_FIXED
    out[start:start + len(text)] = text
    return start + len(text)


def cache_finish(out, used):
    if CACHE_HEADER + used > len(out):
        raise ValueError("output buffer too small")
    struct.pack_into(
        "<4sHI", out, 0, _MAGIC, used,
        binascii.crc32(bytes(out[CACHE_HEADER:CACHE_HEADER + used]))
        & 0xFFFFFFFF)


def cache_length(head):
    if len(head) < CACHE_HEADER or bytes(head[0:4]) != _MAGIC:
        return None
    return struct.unpack_from("<H", head, 4)[0]


def cache_verify(raw):
    length = cache_length(raw)
    if length is None:
        return None
    end = CACHE_HEADER + length
    if end > len(raw):
        return None
    if (binascii.crc32(bytes(raw[CACHE_HEADER:end])) & 0xFFFFFFFF
            != struct.unpack_from("<I", raw, 6)[0]):
        return None
    return end


def cache_record(raw, at, end):
    if at + _KIND_LEN > end:
        return None
    kind, size = struct.unpack_from("<BH", raw, at)
    off = at + _KIND_LEN
    if off + size > end:
        raise ValueError("cache record %d overruns" % kind)
    return kind, off, size, off + size


def cache_read_peer(raw, off, length):
    if length < _NODE_FIXED:
        raise ValueError("cache node record is short")
    end = off + length
    short_off, short_len, next_at = _take_name(raw, off + _NODE_FIXED, end)
    long_off, long_len, _ = _take_name(raw, next_at, end)
    num, age, count, hops, hw, role, snr = struct.unpack_from(
        "<IIHBBBb", raw, off)
    return (num, age, count, None if hops == 0 else hops - 1, hw, role,
            None if snr == -128 else snr,
            short_off, short_len, long_off, long_len)


def cache_read_message(raw, off, length):
    if length < _MESSAGE_FIXED:
        raise ValueError("cache message record is short")
    ident, from_, to, when, flags, channel, hops, snr = struct.unpack_from(
        "<IIIIBBBb", raw, off)
    return (ident, from_, to, when, flags, channel,
            None if hops == 0 else hops - 1, None if snr == -128 else snr,
            off + _MESSAGE_FIXED, length - _MESSAGE_FIXED)


_mt.CACHE_HEADER = CACHE_HEADER
_mt.CACHE_NODE = CACHE_NODE
_mt.CACHE_MESSAGE = CACHE_MESSAGE
_mt.cache_peer_len = cache_peer_len
_mt.cache_message_len = cache_message_len
_mt.cache_write_peer = cache_write_peer
_mt.cache_write_message = cache_write_message
_mt.cache_finish = cache_finish
_mt.cache_length = cache_length
_mt.cache_verify = cache_verify
_mt.cache_record = cache_record
_mt.cache_read_peer = cache_read_peer
_mt.cache_read_message = cache_read_message


# The keystore layout, written from `keystore.py`'s description of it rather
# than from `store.rs`, so that a disagreement between the two fails here.
STORE_HEADER = 10
STORE_TAG = 2
STORE_MAX_VALUE = 255

_STORE_MAGIC = b"MKY1"


def store_length(head):
    if len(head) < STORE_HEADER or bytes(head[0:4]) != _STORE_MAGIC:
        return None
    return struct.unpack_from("<H", head, 4)[0]


def store_verify(raw):
    length = store_length(raw)
    if length is None:
        return None
    end = STORE_HEADER + length
    if end > len(raw):
        return None
    want = struct.unpack_from("<I", raw, 6)[0]
    if binascii.crc32(bytes(raw[STORE_HEADER:end])) & 0xFFFFFFFF != want:
        return None
    return end


def store_record(raw, at, end):
    if at + STORE_TAG > end:
        return None
    tag, size = struct.unpack_from("<BB", raw, at)
    off = at + STORE_TAG
    if off + size > end:
        raise ValueError("store record %d overruns" % tag)
    return tag, off, size, off + size


def store_put(out, at, tag, value):
    if len(value) > STORE_MAX_VALUE:
        raise ValueError("store record is too long")
    end = at + STORE_TAG + len(value)
    if end > len(out):
        raise ValueError("store record will not fit")
    struct.pack_into("<BB", out, at, tag, len(value))
    out[at + STORE_TAG:end] = value
    return end


def store_finish(out, used):
    if STORE_HEADER + used > len(out):
        raise ValueError("store will not fit")
    struct.pack_into(
        "<4sHI", out, 0, _STORE_MAGIC, used,
        binascii.crc32(bytes(out[STORE_HEADER:STORE_HEADER + used]))
        & 0xFFFFFFFF)
    return STORE_HEADER + used


_mt.STORE_HEADER = STORE_HEADER
_mt.STORE_TAG = STORE_TAG
_mt.STORE_MAX_VALUE = STORE_MAX_VALUE
_mt.store_length = store_length
_mt.store_verify = store_verify
_mt.store_record = store_record
_mt.store_put = store_put
_mt.store_finish = store_finish


# --------------------------------------------------------------------- inbox

# The message arena, written out again rather than shared with the Rust: the
# point of a second implementation is that a disagreement about the layout is a
# failing test here instead of a garbled message on the board.

INBOX_FIXED = 23
INBOX_MAX_TEXT = 200
INBOX_READ = 0x01
INBOX_ALL, INBOX_CHANNEL, INBOX_DIRECT = 0, 1, 2

_RECORD = "<IIIIhBBbBB"


def _fit(text):
    """The most of `text` that fits, never cutting a UTF-8 sequence in half."""
    if len(text) <= INBOX_MAX_TEXT:
        return len(text)
    n = INBOX_MAX_TEXT
    while n > 0 and text[n] & 0xC0 == 0x80:
        n -= 1
    return n


def _length(buf, off):
    return INBOX_FIXED + buf[off + 22]


def _selected(buf, off, node_num, want, peer):
    direct = struct.unpack_from("<I", buf, off + 8)[0] == node_num
    if want == INBOX_CHANNEL:
        return not direct
    if want == INBOX_DIRECT:
        return direct and struct.unpack_from("<I", buf, off + 4)[0] == peer
    return True


def _walk(buf, used):
    off = 0
    while off + INBOX_FIXED <= used:
        yield off
        off += _length(buf, off)


def inbox_append(buf, used, limit, ident, from_, to, when, rssi, channel,
                 hops, snr, flags, text):
    take = _fit(text)
    need = INBOX_FIXED + take
    if need > len(buf):
        raise ValueError("output buffer too small")
    at = used
    held = sum(1 for _ in _walk(buf, at))
    dropped = 0
    while at > 0 and (at + need > len(buf) or (limit > 0 and held >= limit)):
        first = _length(buf, 0)
        buf[0:at - first] = buf[first:at]
        at -= first
        held -= 1
        dropped += 1
    struct.pack_into(
        _RECORD, buf, at, ident, from_, to, 0 if when is None else when,
        -32768 if rssi is None else rssi, channel & 0xFF,
        255 if hops is None else hops, -128 if snr is None else snr,
        flags, take)
    buf[at + INBOX_FIXED:at + need] = text[:take]
    return at + need, dropped, at


def inbox_next(buf, used, after, node_num, want, peer):
    off = 0
    if after >= 0:
        if after + INBOX_FIXED > used:
            return None
        off = after + _length(buf, after)
    while off + INBOX_FIXED <= used:
        if _selected(buf, off, node_num, want, peer):
            return off
        off += _length(buf, off)
    return None


def inbox_count(buf, used, node_num, want, peer, unread):
    return sum(1 for off in _walk(buf, used)
               if _selected(buf, off, node_num, want, peer)
               and not (unread and buf[off + 21] & INBOX_READ))


def inbox_mark(buf, used, node_num, want, peer):
    changed = 0
    for off in _walk(buf, used):
        if (_selected(buf, off, node_num, want, peer)
                and not buf[off + 21] & INBOX_READ):
            buf[off + 21] |= INBOX_READ
            changed += 1
    return changed


def inbox_read(buf, used, off):
    if off + INBOX_FIXED > used:
        raise ValueError("inbox offset is not a record in this arena")
    (ident, from_, to, when, rssi, channel, hops, snr, flags,
     take) = struct.unpack_from(_RECORD, buf, off)
    if off + INBOX_FIXED + take > used:
        raise ValueError("inbox offset is not a record in this arena")
    return (ident, from_, to, when or None,
            None if rssi == -32768 else rssi, channel,
            None if hops == 255 else hops, None if snr == -128 else snr,
            flags, off + INBOX_FIXED, take)


def inbox_brief(buf, used, off, node_num):
    if off + INBOX_FIXED > used:
        raise ValueError("inbox offset is not a record in this arena")
    return (struct.unpack_from("<I", buf, off + 4)[0],
            struct.unpack_from("<I", buf, off + 8)[0] == node_num,
            bool(buf[off + 21] & INBOX_READ))


_mt.INBOX_FIXED = INBOX_FIXED
_mt.INBOX_MAX_TEXT = INBOX_MAX_TEXT
_mt.INBOX_READ = INBOX_READ
_mt.INBOX_ALL = INBOX_ALL
_mt.INBOX_CHANNEL = INBOX_CHANNEL
_mt.INBOX_DIRECT = INBOX_DIRECT
_mt.inbox_append = inbox_append
_mt.inbox_next = inbox_next
_mt.inbox_count = inbox_count
_mt.inbox_mark = inbox_mark
_mt.inbox_read = inbox_read
_mt.inbox_brief = inbox_brief
# US LongFast, the only combination the Python side is exercised against here.
_mt.region_info = lambda index: (902_000_000, 928_000_000, 0, 100, 30, 0)
_mt.preset_params = lambda preset, wide: (11, 250_000, 5)
_mt.num_channels = lambda region, bw_hz: 104
_mt.channel_frequency = lambda region, bw_hz, num, name: 906_875_000
# ---- NMEA, mirroring meshtastic/src/nmea.rs
#
# Written from the NMEA sentences rather than from the Rust: splitting on
# commas and calling float() is what Python would do, and an answer reached
# that way disagreeing with the parser is the point of having both. Only the
# buffer layout is shared, because that is the interface and not the method.

NMEA_STATE = 144
NMEA_TALKERS = 8
NMEA_SAW_GGA = 1
NMEA_SAW_RMC = 2
NMEA_SAW_GSV = 4
_NMEA_FLOOR = 1735689600
_N_LAT, _N_LON, _N_ALT, _N_WHEN, _N_GOOD, _N_BAD = 0, 4, 8, 12, 16, 20
_N_HDOP, _N_SATS, _N_VIEW, _N_QUALITY = 24, 26, 27, 28
_N_STATUS, _N_MODE, _N_NAV, _N_HAVE, _N_PENDLEN = 29, 30, 31, 32, 33
_N_TALKERS, _N_PENDING = 36, 60
_N_PENDMAX = NMEA_STATE - _N_PENDING


def _n_i32(state, at, value):
    state[at:at + 4] = struct.pack("<i", value)


def _n_u32(state, at, value):
    state[at:at + 4] = struct.pack("<I", value)


def _n_get_i32(state, at):
    return struct.unpack("<i", bytes(state[at:at + 4]))[0]


def _n_get_u32(state, at):
    return struct.unpack("<I", bytes(state[at:at + 4]))[0]


def nmea_reset(state):
    if len(state) < NMEA_STATE:
        raise ValueError("state")
    for i in range(NMEA_STATE):
        state[i] = 0
    state[_N_SATS] = 255
    state[_N_VIEW] = 255
    state[_N_HDOP:_N_HDOP + 2] = b"\xff\xff"


def _n_checked(line):
    """The body of a sentence, or None if it is not one or fails its check."""
    if not line.startswith(b"$"):
        return None
    star = line.rfind(b"*")
    if star < 1 or star + 3 > len(line):
        return None
    total = 0
    for byte in line[1:star]:
        total ^= byte
    try:
        if total != int(line[star + 1:star + 3], 16):
            return None
    except ValueError:
        return None
    return line[:star]


def _n_epoch(date, clock):
    if len(date) < 6 or len(clock) < 6:
        return None
    try:
        day, month, year = int(date[0:2]), int(date[2:4]), 2000 + int(date[4:6])
        hour, minute = int(clock[0:2]), int(clock[2:4])
        second = int(clock[4:6])
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if hour > 23 or minute > 59 or second > 60:
        return None
    # datetime is on the host and not on the board, which is why the library
    # cannot use it -- but here it is an independent second opinion.
    import datetime
    when = datetime.datetime(year, month, day, hour, minute, second,
                             tzinfo=datetime.timezone.utc)
    return int(when.timestamp())


def _n_degrees_e7(value, hemisphere):
    dot = value.find(b".")
    if dot < 3:
        return None
    try:
        whole = int(value[:dot - 2])
        minutes = float(value[dot - 2:])
    except ValueError:
        return None
    if whole > 180 or minutes >= 60.0:
        return None
    total = int(round(whole * 10000000 + minutes * 10000000 / 60.0))
    return -total if hemisphere in (b"S", b"W") else total


def _n_talkers(state):
    seen = []
    for i in range(NMEA_TALKERS):
        at = _N_TALKERS + i * 3
        if state[at] == 0:
            break
        seen.append((bytes(state[at:at + 2]), state[at + 2]))
    return seen


def _n_note(state, ident, count):
    slot = None
    for i in range(NMEA_TALKERS):
        at = _N_TALKERS + i * 3
        if bytes(state[at:at + 2]) == ident:
            slot = at
            break
        if state[at] == 0 and slot is None:
            slot = at
    if slot is None:
        return
    state[slot:slot + 2] = ident
    state[slot + 2] = min(count, 254)
    state[_N_VIEW] = min(sum(n for _, n in _n_talkers(state)), 254)


def nmea_sentence(state, line):
    if len(state) < NMEA_STATE:
        raise ValueError("state")
    if isinstance(line, str):
        line = line.encode("ascii")
    body = _n_checked(bytes(line))
    if body is None:
        return -1
    parts = body.split(b",")
    kind = parts[0][3:6]
    if kind == b"GGA":
        if len(parts) < 10:
            return 0
        try:
            quality = int(parts[6]) if parts[6] else 0
        except ValueError:
            quality = 0
        state[_N_QUALITY] = min(quality, 255)
        try:
            state[_N_SATS] = int(parts[7]) if parts[7] else 255
        except ValueError:
            state[_N_SATS] = 255
        try:
            hdop = int(round(float(parts[8]) * 100)) if parts[8] else 0xFFFF
        except ValueError:
            hdop = 0xFFFF
        state[_N_HDOP:_N_HDOP + 2] = struct.pack("<H", min(hdop, 0xFFFF))
        if quality:
            lat = _n_degrees_e7(parts[2], parts[3])
            lon = _n_degrees_e7(parts[4], parts[5])
            if lat is not None and lon is not None:
                _n_i32(state, _N_LAT, lat)
                _n_i32(state, _N_LON, lon)
                state[_N_HAVE] |= 1
            if parts[9]:
                try:
                    _n_i32(state, _N_ALT, int(round(float(parts[9]) * 1000)))
                    state[_N_HAVE] |= 2
                except ValueError:
                    pass
        return NMEA_SAW_GGA
    if kind == b"RMC":
        if len(parts) < 10:
            return 0
        state[_N_STATUS] = parts[2][0] if parts[2] else 0
        state[_N_MODE] = parts[12][0] if len(parts) > 12 and parts[12] else 0
        state[_N_NAV] = parts[13][0] if len(parts) > 13 and parts[13] else 0
        when = _n_epoch(parts[9], parts[1])
        if when is not None and when >= _NMEA_FLOOR:
            _n_u32(state, _N_WHEN, when)
            state[_N_HAVE] |= 4
        return NMEA_SAW_RMC
    if kind == b"GSV":
        if len(parts) < 4:
            return 0
        try:
            count = int(parts[3]) if parts[3] else 0
        except ValueError:
            count = 0
        _n_note(state, bytes(parts[0][1:3]), count)
        return NMEA_SAW_GSV
    return 0


def nmea_fix(state):
    if len(state) < NMEA_STATE:
        raise ValueError("state")
    have = state[_N_HAVE]
    quality = state[_N_QUALITY]
    status = state[_N_STATUS]
    hdop = struct.unpack("<H", bytes(state[_N_HDOP:_N_HDOP + 2]))[0]
    return (
        _n_get_i32(state, _N_LAT) if have & 1 else None,
        _n_get_i32(state, _N_LON) if have & 1 else None,
        _n_get_i32(state, _N_ALT) if have & 2 else None,
        _n_get_u32(state, _N_WHEN) if have & 4 else None,
        None if state[_N_SATS] == 255 else state[_N_SATS],
        None if state[_N_VIEW] == 255 else state[_N_VIEW],
        quality,
        None if status == 0 else status,
        None if state[_N_MODE] == 0 else state[_N_MODE],
        None if state[_N_NAV] == 0 else state[_N_NAV],
        None if hdop == 0xFFFF else hdop,
        _n_get_u32(state, _N_GOOD),
        _n_get_u32(state, _N_BAD),
        bool(have & 1) and 1 <= quality <= 5 and status == ord("A"),
    )


def nmea_talker(state, index):
    if len(state) < NMEA_STATE or index >= NMEA_TALKERS:
        raise ValueError("state")
    seen = _n_talkers(state)
    return seen[index] if index < len(seen) else None


def nmea_civil(when):
    import datetime
    at = datetime.datetime.fromtimestamp(when, datetime.timezone.utc)
    return (at.year, at.month, at.day, at.hour, at.minute, at.second)


def nmea_feed(state, chunk, echo, echo_at):
    if len(state) < NMEA_STATE:
        raise ValueError("state")
    landed = 0
    used = echo_at
    for byte in bytes(chunk):
        if byte in (0x0D, 0x0A):
            length = state[_N_PENDLEN]
            state[_N_PENDLEN] = 0
            if not length:
                continue
            line = bytes(state[_N_PENDING:_N_PENDING + length])
            if nmea_sentence(state, line) < 0:
                _n_u32(state, _N_BAD, _n_get_u32(state, _N_BAD) + 1)
                continue
            _n_u32(state, _N_GOOD, _n_get_u32(state, _N_GOOD) + 1)
            landed += 1
            if echo is not None and used + length + 1 <= len(echo):
                echo[used:used + length] = line
                echo[used + length] = 0x0A
                used += length + 1
            continue
        length = state[_N_PENDLEN]
        if length >= _N_PENDMAX:
            state[_N_PENDLEN] = 0
            _n_u32(state, _N_BAD, _n_get_u32(state, _N_BAD) + 1)
            continue
        state[_N_PENDING + length] = byte
        state[_N_PENDLEN] = length + 1
    return landed, used


_mt.NMEA_STATE = NMEA_STATE
_mt.NMEA_TALKERS = NMEA_TALKERS
_mt.NMEA_SAW_GGA = NMEA_SAW_GGA
_mt.NMEA_SAW_RMC = NMEA_SAW_RMC
_mt.NMEA_SAW_GSV = NMEA_SAW_GSV
_mt.nmea_reset = nmea_reset
_mt.nmea_sentence = nmea_sentence
_mt.nmea_feed = nmea_feed
_mt.nmea_fix = nmea_fix
_mt.nmea_talker = nmea_talker
_mt.nmea_civil = nmea_civil


# ---- stream framing, mirroring meshtastic/src/stream.rs
#
# Written from the protocol rather than from the Rust, and deliberately in a
# different shape: this one keeps its partial frame in a list and slices, where
# the Rust walks a byte at a time through a fixed buffer. Two implementations
# that agree are evidence; one implementation copied twice is not.

STREAM_STATE = 528
STREAM_BODY = 16
STREAM_MAX = 512
STREAM_HEADER = 4

_S_STAGE, _S_WANT, _S_HAVE, _S_LOST, _S_FRAMES = 0, 2, 4, 8, 12


def _s_get(state, at):
    return int.from_bytes(bytes(state[at:at + 4]), "little")


def _s_put(state, at, value):
    state[at:at + 4] = (value & 0xFFFFFFFF).to_bytes(4, "little")


def _s_get16(state, at):
    return int.from_bytes(bytes(state[at:at + 2]), "little")


def _s_put16(state, at, value):
    state[at:at + 2] = (value & 0xFFFF).to_bytes(2, "little")


def stream_reset(state):
    if len(state) < STREAM_STATE:
        raise ValueError("stream state too small")
    state[:STREAM_BODY] = bytes(STREAM_BODY)


def stream_counts(state):
    if len(state) < STREAM_STATE:
        raise ValueError("stream state too small")
    partial = _s_get16(state, _S_HAVE) if state[_S_STAGE] == 4 else 0
    return _s_get(state, _S_LOST), _s_get(state, _S_FRAMES), partial


def stream_feed(state, chunk, at):
    if len(state) < STREAM_STATE:
        raise ValueError("stream state too small")
    i = min(at, len(chunk))
    while i < len(chunk):
        byte = chunk[i]
        i += 1
        stage = state[_S_STAGE]
        if stage == 0:
            if byte == 0x94:
                state[_S_STAGE] = 1
            else:
                _s_put(state, _S_LOST, _s_get(state, _S_LOST) + 1)
        elif stage == 1:
            if byte == 0xC3:
                state[_S_STAGE] = 2
            elif byte == 0x94:
                _s_put(state, _S_LOST, _s_get(state, _S_LOST) + 1)
            else:
                state[_S_STAGE] = 0
                _s_put(state, _S_LOST, _s_get(state, _S_LOST) + 2)
        elif stage == 2:
            _s_put16(state, _S_WANT, byte << 8)
            state[_S_STAGE] = 3
        elif stage == 3:
            want = _s_get16(state, _S_WANT) | byte
            if want > STREAM_MAX:
                state[_S_STAGE] = 0
                _s_put(state, _S_LOST, _s_get(state, _S_LOST) + 4)
                continue
            _s_put16(state, _S_WANT, want)
            _s_put16(state, _S_HAVE, 0)
            state[_S_STAGE] = 0 if want == 0 else 4
        else:
            have = _s_get16(state, _S_HAVE)
            state[STREAM_BODY + have] = byte
            have += 1
            _s_put16(state, _S_HAVE, have)
            if have >= _s_get16(state, _S_WANT):
                state[_S_STAGE] = 0
                _s_put(state, _S_FRAMES, _s_get(state, _S_FRAMES) + 1)
                return have, i
    return 0, i


def stream_frame(out, at, payload):
    if len(payload) > STREAM_MAX or at > len(out) \
            or len(out) - at < STREAM_HEADER + len(payload):
        raise ValueError("output buffer too small")
    out[at] = 0x94
    out[at + 1] = 0xC3
    out[at + 2] = len(payload) >> 8
    out[at + 3] = len(payload) & 0xFF
    out[at + STREAM_HEADER:at + STREAM_HEADER + len(payload)] = payload
    return STREAM_HEADER + len(payload)


_mt.STREAM_STATE = STREAM_STATE
_mt.STREAM_BODY = STREAM_BODY
_mt.STREAM_MAX = STREAM_MAX
_mt.STREAM_HEADER = STREAM_HEADER
_mt.stream_reset = stream_reset
_mt.stream_feed = stream_feed
_mt.stream_counts = stream_counts
_mt.stream_frame = stream_frame


PRECISION_FULL = 32


def position_encode(out, state, precision=PRECISION_FULL):
    if not nmea_fix(state)[13]:
        raise ValueError("no confirmed fix to send")
    try:
        return _position_fields(out, state, precision)
    except (IndexError, struct.error):
        # The shared `pb_` stubs write past the end rather than checking, where
        # Rust returns ERR_NO_SPACE. Mirrored here so the natmod and the stub
        # refuse the same buffer the same way.
        raise ValueError("output buffer too small")


def _position_fields(out, state, precision):
    (lat, lon, alt, when, sats, view, quality, status, mode, nav, hdop,
     _good, _bad, _valid) = nmea_fix(state)
    at = 0
    if lat is not None and precision > 0:
        if precision < 32:
            mask = (0xFFFFFFFF << (32 - precision)) & 0xFFFFFFFF
            half = 1 << (31 - precision)
            lat = _signed32(((lat & mask) + half) & 0xFFFFFFFF)
            lon = _signed32(((lon & mask) + half) & 0xFFFFFFFF)
        at = pb_fixed32(out, at, 1, lat & 0xFFFFFFFF)
        at = pb_fixed32(out, at, 2, lon & 0xFFFFFFFF)
    if alt is not None:
        # Truncating toward zero, the way Rust's `/` does on integers.
        metres = alt // 1000 if alt >= 0 else -((-alt) // 1000)
        at = pb_int32(out, at, 3, metres)
    if when:
        at = pb_fixed32(out, at, 4, when)
    at = pb_uint(out, at, 5, 2)
    if hdop is not None:
        at = pb_uint(out, at, 12, hdop)
    if quality:
        at = pb_uint(out, at, 17, quality)
    seen = view if view is not None else sats
    if seen is not None:
        at = pb_uint(out, at, 19, seen)
    at = pb_uint(out, at, 23, precision)
    if at > len(out):
        raise ValueError("output buffer too small")
    return at


def _signed32(value):
    return value - 0x100000000 if value & 0x80000000 else value


_mt.PRECISION_FULL = PRECISION_FULL
_mt.position_encode = position_encode
sys.modules["meshtastic"] = _mt


# --------------------------------------------------------------------- radio

# No `lr1121` stub any more: the node speaks `meshradio`'s vocabulary, so what
# the host has to fake is the contract rather than a particular part. That the
# driver can be left out of the host build entirely is the point of the seam.


class Power:
    """A battery whose voltage is whatever the test says it is."""

    def __init__(self, millivolts=3800, state=0):
        self.mv = millivolts
        self._state = state

    def millivolts(self):
        return self.mv

    def present(self):
        return self.mv >= _mt.NO_BATTERY_MV

    def state(self):
        return self._state

    def charging(self):
        return self._state == 1

    def report(self):
        if self.mv < _mt.NO_BATTERY_MV:
            return None, self.mv
        level = battery_percent(self.mv)
        return (99 if level > 99 and self.charging() else level), self.mv

    def percent(self):
        return self.report()[0]


class Radio:
    """A transmitter that records instead of radiating."""

    description = "recording radio"

    def __init__(self):
        self.sent = []
        self.lora = {}
        self.listening = False
        self._state = 0x11111111

    def open(self):
        return self

    def close(self):
        pass

    def entropy(self):
        self._state = (self._state * 1103515245 + 12345) & 0xFFFFFFFF
        return self._state

    def power_ceiling(self, frequency_hz):
        return 22 if frequency_hz < 1_000_000_000 else 13

    def tune(self, **kwargs):
        self.lora = kwargs

    @property
    def power_dbm(self):
        return self.lora.get("power_dbm")

    def send(self, data):
        self.sent.append(bytes(data))
        return True

    def listen(self):
        self.listening = True

    def standby(self):
        self.listening = False

    def collect(self):
        return None

    def quality(self):
        return (-90, 7.25, -90)

    def noise(self):
        return -110

    def busy(self):
        return False

    def state(self):
        return "RX" if self.listening else "STBY_RC"

    def counters(self):
        return (len(self.sent), 0, 0, 0)

    def reset_counters(self):
        pass

    def watch(self, pin, handler):
        pass

    def unwatch(self, pin):
        pass


# The payload extractors, written from what `mesh_protocol.py` says each
# message carries rather than from `payload.rs`, so a disagreement about a
# field number or a wire type fails here.
ROUTE_NONE = 0
ROUTE_ERROR = 1
ROUTE_REQUEST = 2
ROUTE_REPLY = 3


def _fields(body):
    at, end = 0, len(body)
    while at < end:
        number, wire, lo, hi, off, length, at = proto_field(body, at)
        yield number, wire, lo | (hi << 32), off, length


def _signed(value):
    return struct.unpack("<i", struct.pack("<I", value & 0xFFFFFFFF))[0]


def payload_position(body):
    lat = lon = alt = sats = precision = None
    for number, _wire, value, _off, _len in _fields(body):
        if number == 1:
            lat = _signed(value)
        elif number == 2:
            lon = _signed(value)
        elif number == 3:
            alt = _signed(value)
        elif number == 19:
            sats = value
        elif number == 23:
            precision = value
    return lat, lon, alt, sats, precision


def payload_user(body):
    names = {}
    hw = role = licensed = None
    for number, wire, value, off, length in _fields(body):
        if number in (2, 3) and wire == 2:
            names[number] = (off, length)
        # Wire type checked here too: `value` is the length for a
        # length-delimited field, so a blob in field 5 would read as an hw.
        elif number == 5 and wire == 0:
            hw = value
        elif number == 6 and wire == 0:
            licensed = 1 if value else 0
        elif number == 7 and wire == 0:
            role = value
    long_off, long_len = names.get(2, (None, None))
    short_off, short_len = names.get(3, (None, None))
    return long_off, long_len, short_off, short_len, hw, role, licensed


def payload_short_name(body):
    for number, wire, _value, off, length in _fields(body):
        if number == 3 and wire == 2:
            return off, length
    return None


def payload_device_metrics(body):
    found = {}
    for number, _wire, value, _off, _len in _fields(body):
        if 1 <= number <= 5:
            found[number] = value
    return tuple(found.get(n) for n in range(1, 6))


def payload_environment_metrics(body):
    found = {}
    for number, _wire, value, _off, _len in _fields(body):
        if 1 <= number <= 3:
            found[number] = value
    return tuple(found.get(n) for n in range(1, 4))


def payload_telemetry(body):
    for number, _wire, _value, off, length in _fields(body):
        if number > 1:
            return number, off, length
    return None


def payload_traceroute(body):
    found = {}
    for number, wire, _value, off, length in _fields(body):
        # Packed is the only encoding these ever use, so anything else is not
        # an array and is passed over rather than guessed at.
        if wire == 2 and 1 <= number <= 4:
            found[number] = (off, length)
    out = []
    for number in (1, 2, 3, 4):
        out.extend(found.get(number, (None, None)))
    return tuple(out)


def payload_routing(body):
    for number, _wire, value, _off, _len in _fields(body):
        if number == 3:
            return ROUTE_ERROR, value
        if number == 1:
            return ROUTE_REQUEST, 0
        if number == 2:
            return ROUTE_REPLY, 0
    return ROUTE_NONE, 0


def payload_sender_time(body, want):
    for number, wire, value, _off, _len in _fields(body):
        if number == want and wire == 5:
            return value
    return None


def payload_snr_at(chunk, at):
    value, at = _varint(chunk, at)
    byte = value & 0xFF
    return (byte - 0x100 if byte > 0x7F else byte), at


_mt.ROUTE_NONE = ROUTE_NONE
_mt.ROUTE_ERROR = ROUTE_ERROR
_mt.ROUTE_REQUEST = ROUTE_REQUEST
_mt.ROUTE_REPLY = ROUTE_REPLY
_mt.payload_position = payload_position
_mt.payload_user = payload_user
_mt.payload_short_name = payload_short_name
_mt.payload_device_metrics = payload_device_metrics
_mt.payload_environment_metrics = payload_environment_metrics
_mt.payload_telemetry = payload_telemetry
_mt.payload_traceroute = payload_traceroute
_mt.payload_routing = payload_routing
_mt.payload_sender_time = payload_sender_time
_mt.payload_snr_at = payload_snr_at


# --------------------------------------------------------- the merged library

# On a board the library's modules are spliced into one `meshtastic` module by
# `mpy-tool.py --merge` and share a single globals dict. Here they are still
# separate files, so the merge is reproduced by executing them into the stub
# module's namespace, in the order the Makefile merges them. The host then sees
# what the board sees, a name defined twice included.
#
# Three are left out -- meshradio, radio_lr11xx and mesh -- because they reach
# for `busio` and the LR1121 driver, which this file does not stand in for.
# Nothing under test imports them.
_MERGED = ("mesh_budget", "mesh_proto", "mesh_protocol", "keystore",
           "nodeinfo", "mesh_config", "nodedb", "inbox", "mesh_tx")

# `meshnode` gets its own namespace rather than joining the merge: on the board
# it is part of meshlib, but folding it in here would shadow names the merged
# modules already define. Only its clock ranking is exercised, which needs
# nothing from the radio it would otherwise build.
_NODE = ("meshnode",)
_mn = types.ModuleType("meshnode")
sys.modules["meshnode"] = _mn

#: The phone layer ships as its own meshphone.mpy, so it gets its own namespace
#: here too rather than being folded in above. Same for the text and cache
#: layers.
_PHONE = ("meshapi", "meshble")
_TEXT = ("meshtext",)
_CACHE = ("meshcache",)

_mp = types.ModuleType("meshphone")
sys.modules["meshphone"] = _mp
_mx = types.ModuleType("meshtext")
sys.modules["meshtext"] = _mx
_mcache = types.ModuleType("meshcache")
sys.modules["meshcache"] = _mcache

#: The Python layers merge into `meshlib`, separately from the `meshtastic`
#: built-in above. Registered before the loop so that the `from meshtastic
#: import meshlib as nodedb` a merged module uses to reach a sibling resolves
#: to the namespace being filled in, exactly as it does on the board.
_ml = types.ModuleType("meshlib")
sys.modules["meshlib"] = _ml

# All of these ship inside lib/meshtastic/ on the drive, so every import of one
# goes through the package. Hung off the stub `meshtastic` before anything is
# executed, because the modules below reach for each other this way as they run.
for _sub, _mod in (("meshlib", _ml), ("meshphone", _mp), ("meshtext", _mx),
                   ("meshcache", _mcache), ("meshnode", _mn)):
    setattr(_mt, _sub, _mod)
    sys.modules["meshtastic." + _sub] = _mod

for _target, _names in ((_ml, _MERGED), (_mp, _PHONE), (_mx, _TEXT),
                        (_mcache, _CACHE), (_mn, _NODE)):
    for _name in _names:
        _path = "meshtastic/python/%s.py" % _name
        with open(_path) as _handle:
            exec(compile(_handle.read(), _path, "exec"), _target.__dict__)
