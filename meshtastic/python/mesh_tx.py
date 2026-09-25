"""Phase 5: building and sending frames as a node.

Separate from `meshtastic` because it is the part that has not been proven on
air yet, and separate files are what let this one stay editable on the drive
while everything under it is bytecode.

Nothing here decides *whether* to transmit except the duty cycle. Listening
before talking needs the radio in a way frame construction does not, so it lives
with the application.
"""

import time

import meshtastic as _mt

from meshtastic import meshlib as mt

#: `HOP_RELIABLE` in the firmware, and what a stock node sends with.
DEFAULT_HOP_LIMIT = 3

#: `ID_COUNTER_MASK`, `UINT32_MAX >> 22`: the low ten bits of a packet id.
_ID_COUNTER_MASK = 0x3FF


class DutyCycle:
    """A transmit budget, and the authority to refuse.

    Worth being clear about what this is for. In the US the region table says
    100%, so nothing here is required by regulation; the budget is a courtesy to
    a shared channel, and a brake on a bug that would otherwise transmit in a
    loop. In the 868 MHz regions it is the law, and the same code enforces it.

    The window is a percentage over a stated interval, so it has to be a window
    and not a running total: an hour that is 10% full at the start and 10% full
    at the end is not the same as one that spent its whole allowance in the
    first minute. It is kept as fixed buckets in Rust rather than a list of
    transmissions here, because the list grew by one tuple per packet and held
    them for an hour, and this node has a few kilobytes free once the phone is
    connected. The cost is that the interval is rounded up to a bucket.
    """

    def __init__(self, percent, window_s=3600, buckets=60):
        self.percent = percent
        self.window_s = window_s
        self._bucket_s = max(1, window_s // buckets)
        self._window = bytearray(4 + 4 * buckets)

    def _slot(self, now):
        now = time.monotonic() if now is None else now
        return int(now // self._bucket_s)

    def used_us(self, now=None):
        return _mt.duty_used(self._window, self._slot(now))

    def budget_us(self):
        return self.window_s * self.percent * 10_000

    def allows(self, airtime_us, now=None):
        if self.percent >= 100:
            return True
        return self.used_us(now) + airtime_us <= self.budget_us()

    def record(self, airtime_us, now=None):
        _mt.duty_record(self._window, self._slot(now), airtime_us)

    def __str__(self):
        used = self.used_us()
        return "duty cycle %.2f%% of %d%% over %ds" % (
            100.0 * used / (self.window_s * 1_000_000), self.percent,
            self.window_s)


class Transmitter:
    """Builds and sends frames as one node.

    An object rather than a function because a node's packet ids are a sequence:
    the low ten bits count up and the top twenty-two are random, so consecutive
    packets from one sender are recognisably consecutive -- which is how a
    receiver spots a duplicate -- while collisions between senders stay
    unlikely. Losing the counter on every send would lose the first half of that.
    """

    def __init__(self, radio, settings, key, node_num, *,
                 hop_limit=DEFAULT_HOP_LIMIT, duty_cycle=None):
        # The firmware asserts on this, and it is worth doing the same: a zero
        # sender means the node database never initialised, and the packet would
        # be unanswerable.
        if not node_num:
            raise ValueError("node number 0 means uninitialised, not broadcast")
        if not 0 <= hop_limit <= 7:
            raise ValueError("hop limit must be 0..7, got %r" % (hop_limit,))
        self.radio = radio
        self.settings = settings
        self.key = bytes(key)
        self.node_num = node_num
        self.hop_limit = hop_limit
        self.channel = _mt.channel_hash(
            settings.channel_name.encode("utf-8"), self.key)
        # Zero is the "no relay" sentinel, so a node whose number ends in 0x00
        # relays as 0xFF instead -- getLastByteOfNodeNum in NodeDB.h.
        self.relay_node = (node_num & 0xFF) or 0xFF
        self.duty_cycle = (DutyCycle(settings.duty_cycle_pct)
                           if duty_cycle is None else duty_cycle)
        self._counter = radio.entropy() & _ID_COUNTER_MASK

    def next_id(self):
        self._counter = (self._counter + 1) & _ID_COUNTER_MASK
        return ((self.radio.entropy() << 10) & 0xFFFFFC00) | self._counter

    def airtime_us(self, frame_len):
        return _mt.airtime_us(frame_len, self.settings.sf,
                              self.settings.bw_hz, self.settings.cr)

    def frame(self, payload, portnum, *, to=mt.BROADCAST, want_ack=False,
              want_response=False, bitfield=None, packet_id=None):
        """The bytes that would go on air, without sending them."""
        data = _mt.encode_data(portnum, payload, want_response, bitfield)
        # hop_start is set to the same value hop_limit starts at; that is what
        # lets every later receiver work out how far the packet has come.
        header = _mt.write_header(
            to, self.node_num,
            self.next_id() if packet_id is None else packet_id,
            _mt.build_flags(self.hop_limit, want_ack, False, self.hop_limit),
            self.channel, 0, self.relay_node,
        )
        return header + mt.crypt(header, data, self.key)

    def send(self, payload, portnum, *, force=False, **kwargs):
        """Builds and transmits. Returns the frame, or None if refused."""
        frame = self.frame(payload, portnum, **kwargs)
        us = self.airtime_us(len(frame))
        if not force and not self.duty_cycle.allows(us):
            return None
        self.radio.send(frame)
        self.duty_cycle.record(us)
        return frame

    def text(self, message, *, to=mt.BROADCAST, want_ack=False, force=False):
        return self.send(message.encode("utf-8"), mt.PORT_TEXT_MESSAGE,
                         to=to, want_ack=want_ack, force=force)
