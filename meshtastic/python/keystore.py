"""What the board stores about itself: secrets first, then settings.

The filesystem is mounted read-write by every host the board is plugged into, so
anything in settings.toml is readable by that host, and gets swept into backups
and repositories by accident. The internal NVM is not: it is one 4096-byte page
of the nRF52840's own flash, reachable only from code running on the board.

That is the whole of the protection, and it is worth being exact about. NVM is
not a secure element and nothing here is encrypted at rest -- a REPL prompt or
an attached debugger reads it as easily as this module does. What changes is
that an attacker must hold the board rather than merely have had it plugged in.

The settings arrived here for an unrelated reason: this is the only store the
board can write to itself, and a node has to be able to retune itself while it
is running. So the records below are of two kinds, and this module treats them
alike -- what they mean is `mesh_config`'s business.

Writing costs more than the API suggests. CircuitPython's nRF backend erases and
rewrites the whole page for an assignment of any length, without comparing
first, so storing one byte spends one erase cycle of a page rated in thousands
of them. `keystore_save` reads back and declines to write when nothing changed;
callers should still treat storing as provisioning rather than something a loop
does.

The layout -- the header, the CRC, the tag/length records -- is in
`meshtastic/src/store.rs`. What a tag means is here.

Every public name here carries a `keystore_` prefix because this module is
merged into one flat namespace with the rest of the library, where a bare
`load` or `save` would collide with `meshcache`'s.
"""

import struct

import microcontroller

import meshtastic as _mt

#: Identifies a formatted store, and would identify a format change. Written
#: and checked in Rust; here so that `keystore_describe` and the tests can name
#: it.
MAGIC = b"MKY1"

#: How much of the page the store uses. The page is 4096 bytes and every write
#: rewrites all of it, so the natural thing is to build the whole page in RAM --
#: which needs three 4 KB blocks at once, on a heap that by then holds every
#: module code.py imported. It does not fit. The records add up to a few hundred
#: bytes, so a bounded region is written instead and the rest is never touched.
STORE_BYTES = 512

#: The cache gets the region after the settings. It is a cache and not a store:
#: `meshcache` owns the format, this file owns only where it sits, so that the
#: two regions cannot be made to overlap by editing one of them.
#:
#: They share a page and therefore share an erase budget -- a write to either
#: costs the other one cycle too. That is the whole reason `meshcache` writes as
#: rarely as it does, and why this region is not somewhere a caller may scribble.
#:
#: Well short of the rest of the page, for the reason `STORE_BYTES` is: a write
#: builds the region once to fill it and once more to compare, and by the time
#: anything saves, the heap is also holding the SoftDevice.
CACHE_START = STORE_BYTES
CACHE_BYTES = 2048

_KEYSTORE_HEADER = _mt.STORE_HEADER

NODE_NUM = 1
CHANNEL_KEY = 2
CHANNEL_NAME = 3
#: Curve25519, for the PKI direct messages Phase 6 might answer.
PRIVATE_KEY = 4
PUBLIC_KEY = 5
# Settings rather than secrets, kept here because this is the only store the
# board can write to itself. See mesh_config.
REGION = 6
PRESET = 7
CHANNEL_NUM = 8
TX_POWER_DBM = 9
DUTY_CYCLE_PCT = 10
NODEINFO_S = 11
LONG_NAME = 12
SHORT_NAME = 13

TAG_NAMES = {
    NODE_NUM: "node_num",
    CHANNEL_KEY: "channel_key",
    CHANNEL_NAME: "channel_name",
    PRIVATE_KEY: "private_key",
    PUBLIC_KEY: "public_key",
    REGION: "region",
    PRESET: "preset",
    CHANNEL_NUM: "channel_num",
    TX_POWER_DBM: "tx_power_dbm",
    DUTY_CYCLE_PCT: "duty_cycle_pct",
    NODEINFO_S: "nodeinfo_s",
    LONG_NAME: "long_name",
    SHORT_NAME: "short_name",
}


def text(tag, default=""):
    """One stored record as a string, for the settings that are names."""
    raw = keystore_one(tag)
    return default if raw is None else str(raw, "utf-8")


def available():
    return microcontroller.nvm is not None


def _nvm():
    nvm = microcontroller.nvm
    if nvm is None:
        raise RuntimeError("this board has no internal NVM")
    return nvm


def _stored():
    """The store as one bytes object and where its records end, or None."""
    nvm = microcontroller.nvm
    if nvm is None or len(nvm) < _KEYSTORE_HEADER:
        return None
    # NVM is not a buffer object, so the region is copied out before Rust can
    # look at it. Bounded by the header's own claim rather than by the page, so
    # a blank board copies ten bytes and not five hundred.
    length = _mt.store_length(bytes(nvm[0:_KEYSTORE_HEADER]))
    if length is None or _KEYSTORE_HEADER + length > len(nvm):
        return None
    raw = bytes(nvm[0:_KEYSTORE_HEADER + length])
    end = _mt.store_verify(raw)
    return None if end is None else (raw, end)


def keystore_one(tag):
    """One stored record, or None. Walks to it and stops.

    `keystore_load` costs a dict plus a bytes object for every record in the
    store, and the phone asks for our names at the point in the boot where the
    heap is smallest -- the dict alone was the allocation that failed there.
    """
    found = _stored()
    if found is None:
        return None
    raw, end = found
    at = _KEYSTORE_HEADER
    while True:
        record = _mt.store_record(raw, at, end)
        if record is None:
            return None
        found_tag, off, size, at = record
        if found_tag == tag:
            return raw[off:off + size]


def keystore_load():
    """Every stored record as {tag: bytes}, or {} if blank or damaged.

    Anything unreadable reads as empty rather than raising. A board whose store
    was interrupted mid-write should still boot as an unprovisioned one.
    """
    found = _stored()
    if found is None:
        return {}
    raw, end = found
    out = {}
    at = _KEYSTORE_HEADER
    while True:
        record = _mt.store_record(raw, at, end)
        if record is None:
            return out
        tag, off, size, at = record
        out[tag] = raw[off:off + size]


def _region(nvm):
    return STORE_BYTES if len(nvm) > STORE_BYTES else len(nvm)


def keystore_save(records):
    """Replaces the whole store. True if flash was actually written."""
    nvm = _nvm()
    size = _region(nvm)
    # Padded rather than written short. The backend reads, erases and rewrites
    # the whole page for a write of any length, so a short one costs the same
    # erase cycle and would leave the tail of a longer previous secret in place.
    page = bytearray(b"\xff") * size
    at = _KEYSTORE_HEADER
    # Sorted so that equal contents always encode to equal bytes, which is what
    # makes the unchanged-write check below reliable.
    for tag in sorted(records):
        at = _mt.store_put(page, at, tag, bytes(records[tag]))
    _mt.store_finish(page, at - _KEYSTORE_HEADER)
    if bytes(nvm[0:size]) == page:
        return False
    nvm[0:size] = page
    return True


def keystore_provision(**values):
    """Merges named records into the store in a single write.

    Passing None removes a record. Names are the values of `TAG_NAMES`; ints go
    in as little-endian u32 and strings as UTF-8.
    """
    tags = {name: tag for tag, name in TAG_NAMES.items()}
    records = keystore_load()
    for name, value in values.items():
        try:
            tag = tags[name]
        except KeyError:
            raise ValueError("no such record %r" % (name,))
        if value is None:
            records.pop(tag, None)
        elif isinstance(value, int):
            records[tag] = struct.pack("<I", value)
        elif isinstance(value, str):
            records[tag] = value.encode("utf-8")
        else:
            records[tag] = bytes(value)
    return keystore_save(records)


def keystore_erase():
    """Blanks the store, overwriting the secrets rather than unlinking them."""
    return keystore_save({})


def _cache_region(nvm):
    """(start, length) of the cache, or (0, 0) if the page is too small."""
    if len(nvm) < CACHE_START + CACHE_BYTES:
        return 0, 0
    return CACHE_START, CACHE_BYTES


def read_cache(size=None):
    """The first `size` bytes of the cache, or all of it, or b"" if none.

    A prefix rather than the whole region because the whole region is two
    kilobytes and the caller almost never wants all of it: `meshcache` reads a
    header, learns how long the blob is, and asks for exactly that. Two
    kilobytes is also within a few bytes of the largest allocation the program
    makes -- the BLE read queue -- and handing one out is enough to leave the
    heap without room for the other.
    """
    nvm = microcontroller.nvm
    if nvm is None:
        return b""
    start, room = _cache_region(nvm)
    if size is None or size > room:
        size = room
    return bytes(nvm[start:start + size])


def _unchanged(nvm, start, blob):
    """True when the cache already begins with these bytes.

    A chunk at a time rather than by taking a copy of the region, for the
    reason in `read_cache`: this runs at the moment the radio is going down,
    which is also the moment the BLE queue is holding the heap.
    """
    at = 0
    while at < len(blob):
        end = at + 64
        if end > len(blob):
            end = len(blob)
        if bytes(nvm[start + at:start + end]) != bytes(blob[at:end]):
            return False
        at = end
    return True


def write_cache(blob):
    """Replaces the front of the cache. True if flash was actually written.

    Compared first for the same reason `keystore_save` is: the backend spends a
    whole erase cycle on a write of any length, so writing an unchanged region
    is pure wear. What is *not* done is padding the rest of the region out,
    which would cost a two-kilobyte buffer to save bytes nobody reads -- the
    blob carries its own length, so whatever an older, longer save left behind
    it is never looked at. Blanking the cache therefore means overwriting the
    front of it, not passing an empty blob.
    """
    nvm = _nvm()
    start, room = _cache_region(nvm)
    if not room or not blob:
        return False
    if len(blob) > room:
        raise ValueError(
            "%d bytes will not fit in the %d-byte cache" % (len(blob), room))
    if _unchanged(nvm, start, blob):
        return False
    nvm[start:start + len(blob)] = blob
    return True


def node_num():
    raw = keystore_one(NODE_NUM)
    return None if raw is None else struct.unpack("<I", raw)[0]


def node_number():
    """A node number for this board, derived from the chip's factory id.

    Meshtastic takes the last four bytes of the device's MAC, which makes a node
    number stable across reflashes and unique without anyone running a registry.
    Same idea here, from the nRF52's DEVICEID. The mask keeps the result clear of
    both sentinels: 0 means uninitialised and 0xFFFFFFFF is broadcast, and
    neither one is an address.
    """
    uid = bytes(microcontroller.cpu.uid)
    return struct.unpack("<I", uid[-4:])[0] & 0x7FFFFFFF or 1


def keystore_describe():
    """What is stored, by name and size. Never the values themselves.

    A store's contents get printed during provisioning, over a serial console,
    into a scrollback buffer that outlives the session. Sizes answer "did that
    take" without putting the key anywhere it can be read later.
    """
    records = keystore_load()
    if not records:
        return "keystore empty"
    return "keystore: " + ", ".join(
        "%s=%d bytes" % (TAG_NAMES.get(tag, "tag%d" % tag), len(value))
        for tag, value in sorted(records.items()))
