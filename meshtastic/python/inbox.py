"""Phase 6: keeping the text.

A sniffer prints a message and forgets it. A node keeps it, because the question
anyone actually asks a radio is "what did I miss", and that cannot be answered
by something which only formats packets as they go past.

Messages are grouped the way a phone groups them: broadcasts under the channel
they arrived on, anything addressed to this node under whoever sent it. Reading
a channel takes its key and this node holds one, so in practice there is a
single readable channel and the grouping looks like overkill. It is not: it is
what stops a message meant for us from being buried in the broadcast traffic,
and that distinction exists no matter how many keys are configured.

The messages themselves are not objects. They live packed in one `bytearray`
taken at boot -- the layout is in `meshtastic.inbox` -- and the `Message` a
caller gets back is a view built when it is asked for and collected as soon as
it has been printed. Held as objects, a message was a dozen attributes and a
string that lived as long as the message did, so a busy channel left the heap
dotted with small permanent allocations at exactly the moments the BLE queue
wanted two contiguous kilobytes. What is bounded now is bytes that were really
taken, rather than a worst case that depends on how much strangers type.

Text is the one thing here that nothing re-sends. A node heard again announces
itself again, but a message read once is gone the moment the board resets, and
"what did I miss" is exactly the question a reset should not be able to erase.
So this survives in NVM -- see `meshcache`, which owns that and does the sizing;
what this module owes it is `newest`, `restore` to put a message back without a
packet to build one from, and `dirty` to say when a write would be worth its
cost.
"""

import time

import meshtastic as _mt
from meshtastic import meshlib as mt
from meshtastic import meshlib as mesh_budget
from meshtastic import meshlib as nodedb

#: Held at once, across every conversation. The arena below is the other bound,
#: and either can bite first.
MAX_MESSAGES = 32

#: Bytes of text kept per message. The wire allows more in a payload and the
#: text is written by a stranger, so the store sets its own limit. Bytes rather
#: than characters, because bytes are what has to be budgeted; a message of
#: emoji is cut where one of them ends and never in the middle of one.
MAX_TEXT = _mt.INBOX_MAX_TEXT

#: What a record costs before its text.
RECORD_BYTES = _mt.INBOX_FIXED

#: The arena, taken once and never grown.
#:
#: Sized against flash rather than against a guess. The cache region is 2048
#: bytes and the roster is promised 768 of it, so a little over 1200 bytes of
#: messages is all that can ever survive a reboot; an arena much larger than
#: that spends scarce contiguous RAM on history the next power cycle discards
#: anyway. The slack above it is room for what has arrived since the last save.
#:
#: This is the largest single allocation the node makes, which is why it is
#: also the first -- see `meshnode.Node.__init__`. It is also the first thing
#: to give up on a board that cannot afford it: `MESH_INBOX_BYTES` in
#: settings.toml, and see `mesh_budget`.
INBOX_BYTES = mesh_budget.budget_setting("MESH_INBOX_BYTES", 1536)


def now():
    """The wall clock as whole seconds, or None while nobody has told this node
    one. Whole because the store keeps a u32 and nothing crosses into it as a
    float; CircuitPython's `time.time` is already an int, CPython's is not."""
    at = time.time()
    return int(at) if at >= mt.MIN_EPOCH else None


class Message:
    """One text message, and how it reached us.

    A view of a record in the arena rather than the record itself: built when a
    caller asks for one, and not written back through. Changing a stored
    message means going through the `Inbox`.
    """

    def __init__(self, ident, from_, to, channel, hops, snr, rssi, text,
                 where, direct, at=None, read=False):
        self.id = ident
        self.from_ = from_
        self.to = to
        self.channel = channel
        self.hops = hops
        self.snr = snr
        self.rssi = rssi
        self.text = text
        self.where = where
        self.direct = direct
        self.read = read
        #: A real time only if the mesh has told us one; there is no other
        #: clock, and None is the honest answer when it has not.
        self.time = at

    def line(self, name=None):
        """One row. `name` comes from the node database, which this does not
        have."""
        return "%s %2s %-10s %s" % (
            mt.stamp(self.time) or "--",
            "dm" if self.direct else "",
            name if name else "!%08x" % self.from_,
            # Quoted, so a message containing a newline is still one row and a
            # message that is only whitespace is still visible.
            mt._quoted(self.text),
        )

    def __repr__(self):
        return self.line()


class Inbox:
    """Text messages, newest last, grouped into conversations."""

    def __init__(self, node_num, channel_name, limit=MAX_MESSAGES,
                 capacity=INBOX_BYTES):
        self.node_num = node_num
        self.channel_name = channel_name
        self.limit = limit
        self.buf = bytearray(capacity)
        self.used = 0
        self.dropped = 0
        #: Whether anything has arrived, or been read, since the last save. See
        #: `nodedb.NodeDB.dirty`: a saved copy costs an erase cycle, so
        #: something has to say when one is worth spending.
        self.dirty = False

    def __len__(self):
        return _mt.inbox_count(self.buf, self.used, self.node_num,
                               _mt.INBOX_ALL, 0, False)

    def _select(self, where):
        """A conversation name as the pair the store selects on, or None.

        None for a name that belongs to no conversation, which reads as empty
        rather than raising: the names come from `conversations`, and one that
        has since been pushed out is a fair thing to ask about.
        """
        if where is None:
            return _mt.INBOX_ALL, 0
        if where == self.channel_name:
            return _mt.INBOX_CHANNEL, 0
        if not where.startswith("!"):
            return None
        try:
            return _mt.INBOX_DIRECT, int(where[1:], 16)
        except ValueError:
            return None

    def _store(self, ident, from_, to, channel, hops, snr_q, rssi, text, at,
               read):
        used, dropped, off = _mt.inbox_append(
            self.buf, self.used, self.limit, ident, from_, to, at, rssi,
            channel, hops, snr_q, _mt.INBOX_READ if read else 0, text)
        self.used = used
        self.dropped += dropped
        return off

    def _message(self, off):
        (ident, from_, to, at, rssi, channel, hops, snr_q, flags,
         text_off, text_len) = _mt.inbox_read(self.buf, self.used, off)
        direct = to == self.node_num
        return Message(
            ident, from_, to, channel, hops,
            None if snr_q is None else snr_q / 4.0, rssi,
            str(self.buf[text_off:text_off + text_len], "utf-8"),
            ("!%08x" % from_) if direct else self.channel_name, direct,
            at=at, read=bool(flags & _mt.INBOX_READ))

    def add(self, packet, payload):
        """Files one payload, or returns None if it was not readable text."""
        if payload.portnum != mt.PORT_TEXT_MESSAGE:
            return None
        try:
            payload.body.decode()
        except UnicodeError:
            # Portnum says text, bytes say otherwise. The packet is still in the
            # node database; only the claim to be readable is rejected.
            return None
        # The bytes go in, not the string that proved they decode: the store
        # holds UTF-8, and the decoded copy would only have to be encoded again.
        off = self._store(packet.id, packet.from_, packet.to, packet.channel,
                          packet.hops_away, nodedb.quarters(packet.snr),
                          nodedb.whole(packet.rssi), payload.body, now(),
                          False)
        self.dirty = True
        return self._message(off)

    def restore(self, ident, from_, to, channel, hops, snr_q, text, at, read):
        """Puts a saved message back, addressed the way this node is now.

        `where` and `direct` are not stored at all: both are this node's own
        view of a message rather than the message, and a board that was
        renumbered or retuned while it was off should file what it kept the way
        it files what arrives next.

        `text` is the bytes as saved. Decoding it here is the only chance to
        notice that the flash holds something that is no longer text, and
        `meshcache.cache_load` treats that as an unreadable cache rather than letting
        it surface one message at a time.
        """
        str(text, "utf-8")
        return self._message(self._store(ident, from_, to, channel, hops,
                                         snr_q, None, text, at, read))

    @property
    def unread(self):
        """Whether anything has arrived that has not been read."""
        return self.unread_count() > 0

    def unread_count(self, where=None):
        want = self._select(where)
        if want is None:
            return 0
        return _mt.inbox_count(self.buf, self.used, self.node_num,
                               want[0], want[1], True)

    def conversations(self):
        """Conversation name -> (held, unread)."""
        out = {}
        off = _mt.inbox_next(self.buf, self.used, -1, self.node_num,
                             _mt.INBOX_ALL, 0)
        while off is not None:
            # Enough to group by without building a message: the text is the
            # expensive part and nothing here is going to look at it.
            peer, direct, read = _mt.inbox_brief(self.buf, self.used, off,
                                                 self.node_num)
            where = ("!%08x" % peer) if direct else self.channel_name
            held, unread = out.get(where, (0, 0))
            out[where] = (held + 1, unread + (0 if read else 1))
            off = _mt.inbox_next(self.buf, self.used, off, self.node_num,
                                 _mt.INBOX_ALL, 0)
        return out

    def read(self, where=None, mark=True):
        """The messages in one conversation, or in all of them."""
        want = self._select(where)
        if want is None:
            return []
        found = []
        off = _mt.inbox_next(self.buf, self.used, -1, self.node_num,
                             want[0], want[1])
        while off is not None:
            found.append(self._message(off))
            off = _mt.inbox_next(self.buf, self.used, off, self.node_num,
                                 want[0], want[1])
        if mark and _mt.inbox_mark(self.buf, self.used, self.node_num,
                                   want[0], want[1]):
            self.dirty = True
            for message in found:
                message.read = True
        return found

    def newest(self):
        """Every message newest first, as the plain values a save wants.

        Yields `(id, from_, to, time, channel, hops, snr_q, read, text)`, with
        SNR in quarter decibels and the text as a view of the bytes already in
        the arena. No `Message` is built and no text is copied: a save happens
        at the moment the heap is tightest, which is most of the reason the
        store looks like this.
        """
        offsets = []
        off = _mt.inbox_next(self.buf, self.used, -1, self.node_num,
                             _mt.INBOX_ALL, 0)
        while off is not None:
            offsets.append(off)
            off = _mt.inbox_next(self.buf, self.used, off, self.node_num,
                                 _mt.INBOX_ALL, 0)
        view = memoryview(self.buf)
        for off in reversed(offsets):
            (ident, from_, to, at, _rssi, channel, hops, snr_q, flags,
             text_off, text_len) = _mt.inbox_read(self.buf, self.used, off)
            yield (ident, from_, to, at, channel, hops, snr_q,
                   bool(flags & _mt.INBOX_READ),
                   view[text_off:text_off + text_len])

    def forget_all(self):
        """Throws the text away. Nothing re-sends it, so this is not a cache."""
        self.dropped += len(self)
        self.used = 0
        self.dirty = True
