"""Phase 6: remembering who else is out there.

A node's name reaches the air in one payload type, every few hours. Everything
else it sends is keyed by number alone. So a capture full of `0x0b9dbde1` is not
missing the names -- it just was not listening in the minute they went past --
and the whole fix is to write one down the first time it appears.

This is separate from meshtastic.py because that module is a codec: frames in,
text out, nothing remembered between calls. Who has been heard is session
state, and mixing the two is what put a silent global mutation inside `decode`.
Here the table belongs to the caller, who can size it, read it and print it.

The table itself -- finding a row, filling one, giving one up -- is in Rust, in
`meshtastic/src/roster.rs`. What stayed here is everything with a shape: the
names, which are dictionaries because most rows never get one; the protobuf
walk that reads a `User` message; and the formatting. The rule for the split is
whether the data is the same width for every peer.

Nothing here touches NVM. The table can be written to flash and read back, but
that is `meshcache`'s job and it is deliberately somewhere else: this module
must stay usable, and testable, on a board with no store at all. What it owes
the cache is `dirty` -- whether anything worth a flash write has been learned
since the last one -- because a save is an erase cycle and the counters that
change on every packet are not worth one.
"""

import time

import meshtastic as _mt
from meshtastic import meshlib as mt
from meshtastic import meshlib as mesh_budget
from meshtastic import meshlib as nodeinfo

#: Entries. Small on purpose: the nodes worth knowing about are the near ones,
#: and a roster longer than a screen stops being something anyone reads. Each
#: costs `ROW_BYTES` of one contiguous block, so this is also a RAM decision --
#: see `mesh_budget`.
MAX_NODES = mesh_budget.budget_setting("MESH_MAX_NODES", 32)

#: One row of the table, `<IiiIHBBBbh` on the Rust side: the fields a sighting
#: changes, at fixed offsets so a packet is a write in place instead of a new
#: object. A `num` of zero marks the row free, which no real node number is.
#: Names are not in there -- they are rare, they vary in length, and most rows
#: never get one.
ROW_BYTES = _mt.ROW_BYTES


def quarters(snr):
    """SNR as the integer the table takes. None passes straight through.

    Nothing crosses into Rust as a float. The natmod linker has no soft-float
    helpers to hand a `no_std` crate, and a hard-float ABI mismatch between the
    two halves would go unnoticed until a reading came back as nonsense.
    """
    return None if snr is None else int(round(snr * 4))


def whole(rssi):
    """RSSI as the integer the table takes, for the same reason."""
    return None if rssi is None else int(round(rssi))


def seconds():
    # Not monotonic(), whose float loses whole seconds after a few days up, and
    # not ticks_ms(), which wraps at six. This counter outlasts the battery.
    return time.monotonic_ns() // 1_000_000_000


def _ago(then):
    span = seconds() - then
    for limit, scale, unit in ((60, 1, "s"), (3600, 60, "m"),
                               (86400, 3600, "h")):
        if span < limit:
            return "%d%s" % (span // scale, unit)
    return "%dd" % (span // 86400)


def _clean(text, limit):
    """A name fit to print: bounded, one line, no control characters.

    Done on the way in rather than at each print, because the name is read back
    by anything that mentions the node and only one of those places is a table.
    Cut by character, not by byte, which cannot land mid sequence.
    """
    return "".join(c if " " <= c < "\x7f" or c > "\xa0" else "?"
                   for c in text[:limit])


def _name(body, off, length, limit):
    """One name field out of a `User`, cleaned, or None if it is not text.

    A name that does not decode is dropped rather than replacing what is
    already known: a corrupt repeat should not cost a name heard cleanly once.
    """
    try:
        return _clean(body[off:off + length].decode(), limit)
    except UnicodeError:
        return None


class Peer:
    """One row of a `NodeDB`, read through rather than copied out.

    Live: `heard` hands one back and the caller may still be holding it when
    `learn` changes the same row, which is how a nodeinfo is noticed. It stays
    valid only while the row is that node's -- an eviction rebinds the row, so
    a view is worth keeping for a turn, not across one.
    """

    def __init__(self, db, at):
        self._db = db
        self._at = at
        self.num = _mt.roster_read(db._table, at)[0]

    def row(self):
        """The whole row at once, with the sentinels already turned to None.

        `(num, first, last, seen, count, hops, hw, role, snr_q, rssi)`, where
        the SNR is still in quarter decibels. Anything wanting more than one
        field should come through here: each property below costs a call across
        to Rust and a tuple, and there are ten of them.
        """
        return _mt.roster_read(self._db._table, self._at)

    @property
    def short(self):
        return self._db._short.get(self.num)

    @property
    def long(self):
        return self._db._long.get(self.num)

    #: `HardwareModel` and `DeviceRole`, as the node last claimed them. Zero
    #: for both means unset, which is what a node that has never sent its name
    #: is. Kept because a client reads an unset hardware model as "this entry
    #: is a placeholder someone made up" and treats the names beside it as made
    #: up too.
    @property
    def hw(self):
        return self.row()[6]

    @property
    def role(self):
        return self.row()[7]

    @property
    def count(self):
        return self.row()[4]

    @property
    def hops(self):
        return self.row()[5]

    @property
    def snr(self):
        quarters = self.row()[8]
        return None if quarters is None else quarters / 4.0

    @property
    def rssi(self):
        return self.row()[9]

    @property
    def first(self):
        return self.row()[1]

    @property
    def last(self):
        return self.row()[2]

    #: Sighting order. `last` is for showing a human and rounds to the second,
    #: so a burst would tie and evict whichever came out of the table first.
    #: This never ties.
    @property
    def seen(self):
        return self.row()[3]

    @property
    def name(self):
        return self.short if self.short else "0x%08x" % self.num

    def line(self):
        """One roster row, in fixed columns so the list can be read down."""
        num, _, last, _, count, hops, _, _, snr_q, _ = self.row()
        long = self.long
        return "0x%08x %-6s %4s %5d %6s %5s  %s" % (
            num,
            self.short if self.short else "",
            "--" if hops is None else "%dh" % hops,
            count,
            "--" if snr_q is None else "%.1f" % (snr_q / 4.0),
            _ago(last),
            # Quoted through meshtastic's own helper because these names arrive
            # from the air, and one containing a newline would break the table.
            "" if long is None else mt._quoted(long),
        )


class NodeDB:
    """Who has been heard, bounded in both directions.

    The bound is on entries and on the size of each, because both are chosen by
    strangers. Full, it drops the node heard longest ago: keeping the first 32
    forever would fill the table with everything that went past once during a
    walk and lock out the neighbours that are always there.

    There is no timeout. A name that was true an hour ago is still true, so the
    only reason to forget one is to make room for another.
    """

    def __init__(self, limit=MAX_NODES):
        self.limit = limit
        # Sized once and never grown. An object per peer instead lands
        # scattered through the heap on the boot that restores a roster, and
        # what wants the heap next is the pair of BLE buffers, which have to be
        # contiguous and cannot be given what a sweep will not compact.
        self._table = bytearray(limit * ROW_BYTES)
        self._short = {}
        self._long = {}
        self.forgotten = 0
        self._used = 0
        self._tick = 0
        #: Whether anything a saved copy would not already have has been
        #: learned. Sightings do not set it: a peer heard again is a changed
        #: counter and a changed timestamp, and neither is worth an erase cycle
        #: of a page rated in thousands of them. A peer that is new, or that has
        #: just said who it is, is.
        self.dirty = False

    def __len__(self):
        return self._used

    def __contains__(self, num):
        return _mt.roster_slot(self._table, num) is not None

    def get(self, num):
        """The peer with this number, or None if it has not been heard."""
        at = _mt.roster_slot(self._table, num)
        return None if at is None else Peer(self, at)

    def name(self, num):
        short = self._short.get(num)
        return short if short else "0x%08x" % num

    def _rows(self):
        return range(0, self.limit * ROW_BYTES, ROW_BYTES)

    def heard(self, packet):
        """Records one sighting and returns the peer it came from."""
        num = packet.from_
        at = _mt.roster_slot(self._table, num)
        if at is None:
            at = _mt.roster_free(self._table)
            if at is None:
                at = self._forget()
        self._tick += 1
        # True only when the row was free, which is what makes this peer new.
        if _mt.roster_touch(self._table, at, num, seconds(), self._tick,
                            packet.hops_away, quarters(packet.snr),
                            whole(packet.rssi)):
            self._used += 1
            self.dirty = True
        return Peer(self, at)

    def learn(self, num, body):
        """Takes what a `User` message says, for a node already heard.

        Field 8 is the node's public key and is deliberately not kept. It is
        thirty-two bytes per peer against a table that has to fit beside the
        SoftDevice, and nothing here can use one: this node does not speak PKI,
        and a client that wants a peer's key gets it from the peer's own
        nodeinfo as it goes past.
        """
        at = _mt.roster_slot(self._table, num)
        if at is None:
            return None
        row = _mt.roster_read(self._table, at)
        hw, role = row[6], row[7]
        before = (self._long.get(num), self._short.get(num), hw, role)
        long_off, long_len, short_off, short_len, said_hw, said_role, _lic = \
            _mt.payload_user(body)
        # Absent is not the same as zero: a nodeinfo that omits the field
        # leaves what the roster already had.
        if said_hw is not None:
            # Both are enums, so both are bounded by a byte no matter what the
            # sender wrote; the numbers themselves are theirs to claim.
            hw = said_hw & 0xFF
        if said_role is not None:
            role = said_role & 0xFF
        if long_off is not None:
            text = _name(body, long_off, long_len, nodeinfo.MAX_LONG_NAME)
            if text is not None:
                self._long[num] = text
        if short_off is not None:
            text = _name(body, short_off, short_len, nodeinfo.MAX_SHORT_NAME)
            if text is not None:
                self._short[num] = text
        _mt.roster_identify(self._table, at, hw, role)
        # Compared rather than set as we go: a node repeats its nodeinfo every
        # few hours and almost none of those repeats change anything, and
        # `dirty` is what decides whether a flash page gets erased.
        if (self._long.get(num), self._short.get(num), hw, role) != before:
            self.dirty = True
        return Peer(self, at)

    def roster(self):
        """Every peer, most recently heard first."""
        peers = [Peer(self, at) for at in self._rows()
                 if _mt.roster_read(self._table, at)[0]]
        peers.sort(key=lambda peer: -peer.seen)
        return peers

    def restore(self, num, *, short=None, long=None, hw=0, role=0,
                count=0, hops=None, snr=None, last=None):
        """Puts a saved peer back. Returns it, or None if there was no room.

        Sighting order is taken from the order they arrive in, so a caller
        restoring a saved roster has to hand them over oldest first. Nothing
        has been heard yet at that point, so every restored peer still sorts
        behind everything this session hears.
        """
        if not num or _mt.roster_slot(self._table, num) is not None:
            return None
        at = _mt.roster_free(self._table)
        if at is None:
            return None
        when = seconds() if last is None else last
        self._tick += 1
        _mt.roster_restore(self._table, at, num, when, self._tick, count,
                           hops, hw & 0xFF, role & 0xFF, quarters(snr))
        if short is not None:
            self._short[num] = short
        if long is not None:
            self._long[num] = long
        self._used += 1
        return Peer(self, at)

    def forget_all(self):
        """Empties the database. The mesh refills it as it speaks up again."""
        self.forgotten += self._used
        for at in self._rows():
            _mt.roster_clear(self._table, at)
        self._short = {}
        self._long = {}
        self._used = 0
        self.dirty = True

    def _forget(self):
        """Frees the row heard longest ago and returns its offset."""
        at = _mt.roster_oldest(self._table)
        num = _mt.roster_read(self._table, at)[0]
        self._short.pop(num, None)
        self._long.pop(num, None)
        _mt.roster_clear(self._table, at)
        self._used -= 1
        self.forgotten += 1
        return at
