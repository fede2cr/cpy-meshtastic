"""What the node remembers across a reset: the roster and the text.

Everything above this holds its state on the heap, and the heap does not survive
ctrl-D, let alone a flat battery. For the roster that was a considered trade --
the mesh re-sends every name for free, so a reboot costs a few hours of
listening and nothing else. For the inbox it was never true: nothing re-sends a
message, so a reset threw away the one thing on this board that only existed
here. That is what this fixes, and the roster comes along because it shares the
write.

The cost is flash, and it is worth being exact about. The cache is one region of
the same 4096-byte page the keystore is in, the backend erases and rewrites that
whole page for a write of any length, and the page is rated for something like
ten thousand erase cycles. So a save is not free and cannot be done per packet:
`AUTOSAVE_S` is six hours, which spends about fifteen hundred cycles a year in
the worst case, and the writes that actually matter are the ones a caller asks
for on the way out of a session. A save is also skipped when nothing worth
keeping has changed -- see `dirty` on `NodeDB` and `Inbox` -- and `write_cache`
compares the region before writing, so an unchanged one costs nothing at all.

The region is smaller than what it is asked to hold, and that is the interesting
constraint rather than an oversight. Thirty-two peers and thirty-two messages of
two hundred characters is several times a 2048-byte region at its worst, so the
cache is explicitly lossy: the roster gets a fixed share and the messages get
what is left, both are filled newest first, and whatever does not fit is simply
not saved. Nothing is truncated. A message half kept is a message misquoted,
which is worse than a message missing.

What is deliberately not stored:

* rssi, which describes one reception and is meaningless once the receiver has
  been off;
* `where` and `direct` for a message, which are this node's own view of it and
  are worked out again on the way back in, so a board renumbered or retuned
  while it was off files old text the way it files new;
* peers' public keys, which are 32 bytes each against a table that has to fit
  beside the SoftDevice, and which this node cannot use.

Times are the awkward part. `nodedb` counts seconds since boot, which means
nothing after one, so what is written is each peer's age at the moment of the
save. Reading it back makes the restore instant the new "now", so a peer heard
an hour before a save that was followed by a week off the mains comes back
looking an hour old rather than a week. That is a lie in the node's favour and
it is the only one available: the board has no clock that runs while it is off,
so the length of the gap is not a thing it can know.
"""

import gc

import meshtastic as _mt
from meshtastic import meshlib as keystore
from meshtastic import meshlib as nodedb

#: Identifies a formatted cache, and would identify a format change. Not the
#: keystore's, so that a region left over from the other layout reads as blank
#: rather than as records. The magic itself, and the record layout under it,
#: are in `meshtastic/src/cache.rs`: the region is filled at the moments the
#: heap is tightest, and building it out of joined `struct.pack` results cost
#: half a dozen throwaway objects per record.
_CACHE_HEADER = _mt.CACHE_HEADER

#: Record kinds. Numbered rather than positional so a later format can add one
#: without moving the others, and so an unknown kind can be walked past.
NODE = _mt.CACHE_NODE
MESSAGE = _mt.CACHE_MESSAGE

#: `Message.read`, the only per-message state that is ours rather than the
#: sender's, and the only reason a save can be owed after nothing arrived.
READ = 0x01

#: The most the roster may take before messages start losing. Roughly the whole
#: table at typical name lengths, and a third of the region either way, so a
#: crowded band cannot fill the cache with node numbers.
NODE_BYTES = 768

#: Between automatic saves. Long because each one is an erase cycle: see above.
#: A caller that knows the session is ending should not wait for it.
AUTOSAVE_S = 6 * 3600

#: How long new information waits for company before it is written. A name
#: costs hours of listening to reacquire and a reset is never announced, so
#: waiting out `AUTOSAVE_S` loses the one thing the cache exists to keep. This
#: is affordable only because `dirty` means a peer that is new or that has just
#: changed what it says about itself, which stops happening once a mesh is
#: known: repeats of a nodeinfo already on file do not set it.
SETTLE_S = 120


def _peer_record(peer, now):
    """What one NODE record will hold, and how many bytes it will take.

    Measured before it is written because the records are chosen newest first
    and written oldest first, so the last one in has to be known to fit before
    the first one is placed.
    """
    num, _, last, _, count, hops, hw, role, snr_q, _ = peer.row()
    short = None if peer.short is None else peer.short.encode("utf-8")
    long_name = None if peer.long is None else peer.long.encode("utf-8")
    age = now - last
    return (_mt.cache_peer_len(None if short is None else len(short),
                               None if long_name is None else len(long_name)),
            num, age if age > 0 else 0, count, hops, hw, role, snr_q,
            short, long_name)


def _message_record(message):
    ident, from_, to, when, channel, hops, snr_q, read, text = message
    return (_mt.cache_message_len(len(text)), ident, from_, to, when or 0,
            READ if read else 0, channel & 0xFF, hops, snr_q, text)


def cache_blob(node, now=None):
    """The cache as bytes: as much of the roster and the inbox as will fit."""
    if now is None:
        now = nodedb.seconds()
    room = keystore.CACHE_BYTES - _CACHE_HEADER
    # Both are collected newest first so that what is dropped is the oldest,
    # then written oldest first so that reading them back in order restores the
    # sighting order they were saved in.
    # Every peer, named or not. Keeping only the named ones was worth its cost
    # when a restored peer was an object on the heap; now that the roster is a
    # packed table sized once at boot, an unnamed peer costs eleven bytes here
    # and nothing at all after, while dropping it threw away the whole roster
    # on any board whose neighbours had not sent a nodeinfo yet.
    peers, used = _fill((_peer_record(peer, now)
                         for peer in node.nodes.roster()),
                        NODE_BYTES if NODE_BYTES < room else room)
    messages, spent = _fill((_message_record(message)
                             for message in node.inbox.newest()),
                            room - used)
    used += spent
    # Sized once and filled in place, header included. Growing a buffer and
    # then copying it to put ten bytes in front of it wants three times the
    # cache at the moment the BLE queue is holding its two kilobytes.
    out = bytearray(_CACHE_HEADER + used)
    at = _CACHE_HEADER
    for r in reversed(peers):
        at = _mt.cache_write_peer(out, at, r[1], r[2], r[3], r[4], r[5], r[6],
                                  r[7], r[8], r[9])
    for r in reversed(messages):
        at = _mt.cache_write_message(out, at, r[1], r[2], r[3], r[4], r[5],
                                     r[6], r[7], r[8], r[9])
    _mt.cache_finish(out, used)
    return out


def _fill(records, room):
    """As many records as fit, in the order given, and the bytes they take.

    Stops at the first that does not fit rather than skipping it. The order is
    by age, so carrying on would trade the record just refused for an older one.
    """
    kept = []
    used = 0
    for record in records:
        if record[0] > room:
            break
        kept.append(record)
        room -= record[0]
        used += record[0]
    return kept, used


def _saved():
    """The bytes a save left behind, and not the rest of the region.

    The header is read on its own so that the body can be asked for at its
    real length. Reading the region whole would allocate two kilobytes during
    startup and leave the hole behind it for the BLE queue to fall into.
    """
    length = _mt.cache_length(keystore.read_cache(_CACHE_HEADER))
    if length is None:
        return b""
    return keystore.read_cache(_CACHE_HEADER + length)


def _records(raw):
    """Walks a saved cache. Yields nothing at all if there is not one there.

    Offsets into `raw` rather than slices of it: most records are restored
    without their bytes ever being copied.
    """
    end = _mt.cache_verify(raw)
    if end is None:
        return
    at = _CACHE_HEADER
    while True:
        found = _mt.cache_record(raw, at, end)
        if found is None:
            return
        kind, off, length, at = found
        yield kind, off, length


def _restore_peer(db, raw, off, length, now):
    (num, age, count, hops, hw, role, snr_q,
     short_off, short_len, long_off, long_len) = _mt.cache_read_peer(
        raw, off, length)
    # Dated behind the restore instant by the age it had when it was saved, so
    # the roster reads in the right order and the phone gets a plausible
    # `last_heard`. The time the board spent off is not in here; it cannot be.
    return db.restore(
        num, short=_cache_text(raw, short_off, short_len),
        long=_cache_text(raw, long_off, long_len), hw=hw, role=role, count=count,
        hops=hops, snr=None if snr_q is None else snr_q / 4.0, last=now - age)


def _restore_message(box, raw, off, length):
    (ident, from_, to, when, flags, channel, hops, snr_q,
     text_off, text_len) = _mt.cache_read_message(raw, off, length)
    # The bytes as saved, not a string: the inbox stores UTF-8 and will check
    # that this is some.
    return box.restore(ident, from_, to, channel, hops, snr_q,
                       raw[text_off:text_off + text_len],
                       when or None, bool(flags & READ))


def _cache_text(raw, off, length):
    """A name, or None for a peer that never sent one."""
    return None if length < 0 else str(raw[off:off + length], "utf-8")


def cache_load(node):
    """Puts a saved roster and inbox back. Returns (peers, messages).

    Anything unreadable restores as nothing rather than raising. This runs
    while the node is being built, and a cache that was interrupted mid-write
    must not be able to stop a board from booting.
    """
    now = nodedb.seconds()
    peers = messages = 0
    raw = _saved()
    try:
        for kind, off, length in _records(raw):
            if kind == NODE:
                if _restore_peer(node.nodes, raw, off, length, now) is not None:
                    peers += 1
            elif kind == MESSAGE:
                if _restore_message(node.inbox, raw, off, length) is not None:
                    messages += 1
    except (ValueError, IndexError, UnicodeError) as err:
        print("# cache: unreadable, ignored (%s)" % err)
    node.nodes.dirty = False
    node.inbox.dirty = False
    return peers, messages


def cache_save(node):
    """Writes the roster and the inbox. True if flash was actually written."""
    # Assembled in one piece, and the moments a save happens at are the moments
    # the BLE queue is holding its two kilobytes.
    gc.collect()
    wrote = keystore.write_cache(cache_blob(node))
    node.nodes.dirty = False
    node.inbox.dirty = False
    return wrote


def cache_erase():
    """Blanks the cache. True if flash was actually written.

    The header is enough: everything after it is only reachable through the
    length and the magic that are in it.
    """
    return keystore.write_cache(b"\xff" * _CACHE_HEADER)