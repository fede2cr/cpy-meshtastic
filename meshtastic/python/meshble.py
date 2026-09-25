"""Advertise as a Meshtastic node, and be one when the phone app connects.

Two halves. The advertisement is what a scanner sees, and it is copied from the
firmware: `NRF52Bluetooth` advertises flags, TX power and the mesh service UUID,
and puts the name in the scan response, because a 128-bit UUID is 18 of the 31
bytes on its own and the name does not fit beside it. The name comes from
`getDeviceName()` in `main.cpp`: the short name and the last two bytes of the
MAC, or `Meshtastic_xxxx` if the node has never been renamed. Our tail is the
node number's rather than the BLE MAC's -- the same substitution
`nodeinfo.default_names` already makes, so the name here matches the name the
mesh sees rather than an address only the phone sees.

The other half is the service, three characteristics that are a mailbox rather
than a stream. `toRadio` is written to, one whole `ToRadio` per write.
`fromRadio` is read from, and every read hands back the next message and removes
it, until a read comes back empty to say there is no more. `fromNum` is a
counter the phone subscribes to: when it changes, something is waiting.

That middle characteristic is why this project carries a firmware patch. A GATT
read in CircuitPython is answered by the SoftDevice out of the attribute table,
with no way for Python to see that it happened, and polling for reads that
arrive 15 ms apart would hand out the same message twice or skip one. The
`READ_QUEUE` property and `serve_reads`/`queue_read` were added to `_bleio` to
answer a read from a FIFO instead, which is what the stock firmware does with
`setReadAuthorizeCallback`. What that buys is the ability to queue the entire
config dump up front, before the phone reads anything -- necessary, because a
single empty read in the middle of it is how the phone is told the dump ended.

The nRF52840 has one advertising set, and CircuitPython's own BLE workflow wants
it too. Ours wins: starting a user advertisement stops the internal one, and the
workflow's retries are then refused. The cost is that the CIRCUITPY file service
is not discoverable while this is running. `CIRCUITPY_BLE_WORKFLOW = false` in
settings.toml decides that up front instead of by surprise.

What the app can do with this node is bounded, and `meshapi` is where that is
written down: it is a client, not something the phone configures.
"""

import binascii
import gc
import struct
import time

import _bleio

from meshtastic import meshphone as meshapi
from meshtastic import meshlib as mesh_budget
from meshtastic import meshlib as nodeinfo

#: From `src/mesh/../BluetoothCommon.h` in the firmware.
SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"

#: Advertising data types, from the Bluetooth assigned numbers.
_AD_FLAGS = 0x01
_AD_UUID128_COMPLETE = 0x07
_AD_NAME_COMPLETE = 0x09
_AD_TX_POWER = 0x0A

#: LE General Discoverable, BR/EDR not supported.
_FLAGS = 0x06

#: The firmware advertises fast for 30 s and then slows down. One interval is
#: enough here, and 100 ms is what a phone scan expects to catch.
INTERVAL_S = 0.1

#: The largest `FromRadio` we will hand over in one read. The SoftDevice caps a
#: variable-length attribute at 512 and the negotiated MTU is 512, so this is
#: not the limit -- `mesh_proto._SIZE` is. Nothing on this board can build a
#: message larger than that buffer, because the encoder refuses to write past
#: it, so anything above it here is a value buffer that can never be filled.
MAX_PACKET = 384

#: The largest `ToRadio` we will take, which is a different and much smaller
#: number. The only large thing the app can send is a packet to transmit, and
#: that has to fit in a LoRa frame -- 255 bytes, header and all -- so a whole
#: ToRadio around one is under three hundred. Sized to what the protocol allows
#: rather than to what GATT allows, because this number is paid five times over:
#: the attribute's own value, four slots in the packet buffer, and the scratch
#: buffer `pump` reads into.
MAX_TORADIO = 320

#: Room for queued `FromRadio` messages. A whole dump has to fit in one go: the
#: phone stops reading the first time a read comes back empty, so running out in
#: the middle reads to it as a complete node with no channels and no neighbours.
#: Measured, not guessed -- the device half is 292 bytes and the node list is
#: 27 per peer, so a full 32-node roster is under 1 kB. The rest is headroom for
#: mesh traffic arriving between reads, and the number is no larger than that
#: because this is the biggest single allocation the program makes and the
#: SoftDevice has already taken its share of the heap by the time it happens.
#: Which is also why it is the one knob here worth turning on a small board:
#: `MESH_QUEUE_BYTES` in settings.toml, and see `mesh_budget`. Below about
#: 1200 the dump itself stops fitting and the app never finishes connecting.
QUEUE_BYTES = mesh_budget.budget_setting("MESH_QUEUE_BYTES", 2048)

#: Writes held while nothing is calling `pump()`. The app sends one packet at a
#: time and waits, and `pump` drains the buffer completely every turn of the
#: serve loop, so this only has to cover a write landing while the previous one
#: is being answered. Each slot costs `MAX_TORADIO` + 2 and the whole buffer is
#: one contiguous allocation made after the SoftDevice has taken its share, so
#: slots that would never be used are slots that make the link fail to come up.
INBOX_PACKETS = 2

#: How long an idle link goes without the phone being given anything to read.
#: Its patience is sixty seconds, so this leaves room for two of these to be
#: missed before it gives up on us. See `Phone._keepalive`.
KEEPALIVE_S = 20

#: How long to let a new connection settle before describing it. MTU exchange
#: and pairing both happen after the connect event, so asking immediately
#: reports the defaults rather than what was agreed.
LINK_SETTLE_S = 1.5


def _element(kind, payload):
    return bytes((len(payload) + 1, kind)) + payload


def _uuid_le(text):
    """A UUID string as the little-endian bytes an advertisement carries."""
    return bytes(reversed(bytes.fromhex(text.replace("-", ""))))


def device_name(node_num, short_name=None):
    """What the phone will list this board as.

    Mirrors `getDeviceName()`: the short name only when it is not just the
    hex tail it defaults to, so an unnamed node reads as Meshtastic_xxxx.
    """
    tail = "%04x" % (node_num & 0xFFFF)
    if short_name is None:
        short_name = nodeinfo.names(node_num)[1]
    if short_name and short_name != tail:
        return "%s_%s" % (short_name, tail)
    return "Meshtastic_%s" % tail


def advertise(node_num, short_name=None, *, tx_power=0):
    """Starts advertising as a Meshtastic node. Returns the advertised name."""
    name = device_name(node_num, short_name)
    adapter = _bleio.adapter
    adapter.enabled = True
    # The GAP name, which is what a client reads after connecting. The scan
    # response below is what it sees before that, and they should agree.
    adapter.name = name

    data = (_element(_AD_FLAGS, bytes((_FLAGS,)))
            + _element(_AD_TX_POWER, bytes((tx_power & 0xFF,)))
            + _element(_AD_UUID128_COMPLETE, _uuid_le(SERVICE_UUID)))
    scan_response = _element(_AD_NAME_COMPLETE, name.encode("utf-8"))

    if adapter.advertising:
        adapter.stop_advertising()
    adapter.start_advertising(data, scan_response=scan_response,
                              connectable=True, interval=INTERVAL_S,
                              tx_power=tx_power)
    return name


def ble_stop():
    """Stops advertising and shuts the radio down. Safe to call twice."""
    adapter = _bleio.adapter
    if adapter.enabled:
        if adapter.advertising:
            adapter.stop_advertising()
        adapter.enabled = False


class Phone:
    """The Meshtastic GATT service, and the one client that may be using it.

    Built once and left in place. The service cannot be removed from the
    SoftDevice's attribute table anyway, and a client that disconnects and comes
    back should find the same node rather than a new one; what is reset between
    clients is the queue and whether the config dump has been asked for.

    Nothing happens on its own. `pump` is called from the same loop that polls
    the radio, so every phone-driven transmission happens on the main thread,
    between LoRa packets, with the duty cycle and listen-before-talk guards in
    front of it exactly as if it had been typed at the REPL.
    """

    def __init__(self, node, *, queue_bytes=QUEUE_BYTES):
        self.api = meshapi.PhoneAPI(node)
        #: Messages that would not fit in the queue. Not zero means the phone
        #: has an incomplete picture and does not know it.
        self.dropped = 0
        #: ToRadio writes seen. Zero after a connection that came and went is
        #: the useful symptom: the client found the service and then decided
        #: against it, rather than never being answered.
        self.writes = 0
        self.connected = False
        self.name = None
        self._queue_bytes = queue_bytes
        self._since = 0
        #: When the phone last had something real to read. See `_keepalive`.
        self._spoke = 0
        #: The live connection, kept only so the link can be described once.
        self._link = None
        self._told = False
        #: `_counts` at the start of this session. The counters themselves run
        #: for the life of the program, and a session is read by the difference.
        self._before = self._counts()

        _bleio.adapter.enabled = True
        char = _bleio.Characteristic
        self.service = _bleio.Service(_bleio.UUID(SERVICE_UUID))
        self._to_radio = char.add_to_service(
            self.service, _bleio.UUID(TORADIO_UUID),
            properties=char.WRITE | char.WRITE_NO_RESPONSE,
            max_length=MAX_TORADIO)
        # READ_QUEUE has to be asked for here and cannot be added later: it
        # decides whether the attribute is registered with read authorization,
        # which the SoftDevice fixes when the characteristic is added.
        self._from_radio = char.add_to_service(
            self.service, _bleio.UUID(FROMRADIO_UUID),
            properties=char.READ | char.READ_QUEUE,
            max_length=MAX_PACKET)
        self._from_num = char.add_to_service(
            self.service, _bleio.UUID(FROMNUM_UUID),
            properties=char.READ | char.NOTIFY,
            max_length=4, fixed_length=True, initial_value=b"\0\0\0\0")
        # The three buffers below are the largest allocations the program makes,
        # and they are wanted late, after a boot that has read flash and built a
        # roster. Contiguous is what they need and fragmentation is what would
        # deny it, so the sweep is here rather than left to the failure.
        gc.collect()
        # What the SoftDevice and the attribute table cost, which is otherwise
        # only ever learnt from a MemoryError on the next line.
        print("# BLE service up, %d bytes free" % gc.mem_free())

        # First of the three, because it is by far the largest and the only one
        # that needs a kilobyte-scale run of blocks: the sweep above has just
        # made the biggest hole this heap will ever offer, and the two small
        # buffers below can fit around it wherever they land. `_watch` gives
        # this one back and asks for the same size again at both edges of every
        # connection, which drops it into the hole it just left.
        self._from_radio.serve_reads(queue_bytes)
        # Writes arrive as whole packets and have to stay that way: a `ToRadio`
        # is protobuf, which has no framing of its own, so two writes run
        # together are indistinguishable from one longer message.
        self._inbox = _bleio.PacketBuffer(self._to_radio,
                                          buffer_size=INBOX_PACKETS,
                                          max_packet_size=MAX_TORADIO)
        self._buf = bytearray(MAX_TORADIO)
        self._count = 0
        self._ready = False

        # Everything the node hears goes past the phone as well. The hook is set
        # last so a half-built service is never handed a packet.
        node.on_receive = self.heard

    def advertise(self, **kwargs):
        """Starts, or restarts, the advertisement. Returns the name."""
        self.name = advertise(self.api.node.config.node_num, **kwargs)
        return self.name

    def stop(self):
        """Stops advertising. The service stays in the attribute table -- the
        SoftDevice has no way to take one out -- but nothing reaches it once
        the node is down. Matches `meshtcp.Network.stop`, so `mesh.stop()` can
        put either transport away without knowing which it has.
        """
        ble_stop()

    # ------------------------------------------------------------- plumbing

    def _counts(self):
        """The lifetime counters, in the order the session line prints them."""
        return (self.api.sent, self.api.wrote, self.api.refused,
                self.api.ignored, self.dropped)

    def _offer(self, message):
        """Queues one `FromRadio`. False once the queue is full."""
        if not self._from_radio.queue_read(message):
            self.dropped += 1
            return False
        self._count += 1
        self._ready = True
        self._spoke = time.monotonic()
        return True

    def _keepalive(self):
        """Something to read on a link that has gone quiet.

        The app does not listen for silence, it measures it, and against its own
        clock: `checkLiveness` runs on the same tick as the heartbeat and
        compares the time now against the last frame it managed to read. Android
        suspends that tick -- two and three minute gaps between heartbeats have
        been seen here -- and when it resumes it sends its heartbeat and then
        immediately fails a check against a gap that was entirely its own sleep.
        Answering the heartbeat cannot save that session, because the check runs
        before any answer could arrive. Only a frame it has already read can.

        So the queue state goes out unprompted on an idle link. It is true, it
        is six bytes, and it is the same thing the firmware sends to prove the
        link is alive when it is asked.
        """
        if self.api.config_id is None:
            # Nobody has asked for anything yet, so there is no session to keep.
            return
        if time.monotonic() - self._spoke < KEEPALIVE_S:
            return
        # Stamped whether or not it fit: a full queue is not an idle link, and
        # retrying it every time round the loop would only fill the log.
        self._spoke = time.monotonic()
        self._offer(self.api.alive())

    def _ring(self):
        """Tells the phone there is something to read, if there is.

        Rung after the queue is filled and never before. The counter is the
        doorbell: the phone reads until empty every time it changes, so ringing
        first invites a read of a queue that is still being written.
        """
        if not self._ready:
            return
        self._ready = False
        self._from_num.value = struct.pack("<I", self._count & 0xFFFFFFFF)

    def _watch(self):
        """Notices a client arriving or leaving."""
        connected = bool(_bleio.adapter.connections)
        if connected == self.connected:
            return
        self.connected = connected
        # A client with a real clock is either here or it is not, and while it
        # is the node stops taking the time off the air.
        self.api.node.attached = connected
        # Either edge starts a session, and a session starts empty. What is
        # queued belongs to one client: giving it to the next would put mesh
        # traffic in front of the config dump it has not asked for yet. The
        # passkey goes with it, so no client inherits another's right to write.
        self.api.reset()
        self._from_radio.serve_reads(self._queue_bytes)
        self._ready = False
        if connected:
            self._since = time.monotonic()
            self._spoke = time.monotonic()
            self.writes = 0
            self._before = self._counts()
            links = _bleio.adapter.connections
            self._link = links[0] if links else None
            self._told = False
            print("# phone: connected")
            return
        self._link = None
        # How long it lasted is the most useful single number here. A session
        # that ends in under a second was refused over something structural; one
        # that ends around thirty was waiting for an answer that never came; and
        # one that ends at ninety, over and over, is the app's liveness timeout
        # -- sixty seconds with nothing at all to read. See `PhoneAPI.receive`.
        sent, wrote, refused, ignored, dropped = self._counts()
        was_sent, was_wrote, was_refused, was_ignored, was_dropped = self._before
        print("# phone: disconnected after %.1fs (%d writes, %d sent, %d "
              "stored, %d refused, %d ignored, %d dropped)"
              % (time.monotonic() - self._since, self.writes,
                 sent - was_sent, wrote - was_wrote,
                 refused - was_refused, ignored - was_ignored,
                 dropped - was_dropped))
        # The SoftDevice stops advertising the moment a client connects and does
        # not resume, so without this the board is discoverable exactly once.
        self.advertise()

    def _describe_link(self):
        """One line about the link itself, once it has settled.

        A session that ends with no writes ends before anything above the
        SoftDevice can see it, so what was negotiated is the only evidence
        left. An MTU still at 23 says the app never got as far as asking. And
        `paired` on a board that never bonds says the phone is offering keys
        from an older flash of it, which it will go on doing until it is told
        to forget the device.
        """
        if self._told or self._link is None:
            return
        if time.monotonic() - self._since < LINK_SETTLE_S:
            return
        self._told = True
        try:
            print("# phone: link paired=%s mtu=%d interval=%.1fms"
                  % (self._link.paired, self._link.max_packet_length,
                     self._link.connection_interval))
        except ConnectionError:
            # Dropped between the check and the read; `_watch` will say so.
            pass

    # -------------------------------------------------------------- the loop

    def heard(self, packet, frame, payload):
        """A frame off the air, on its way to the phone. For `Node.on_receive`."""
        # Every frame prints, including the ones that go no further. Whether a
        # packet reached the phone is otherwise unanswerable from either end:
        # silence here means the radio heard nothing, which is a different
        # fault from hearing it and having nowhere to put it.
        who = self.api.node.nodes.name(packet.from_)
        if self.api.config_id is None:
            # Nobody has asked to be told anything yet. A packet queued now
            # would arrive before the config dump and be discarded anyway.
            print("# heard %s, %s, no client asking"
                  % (who, "sealed" if payload is None else payload.name))
            return
        message = self.api.heard(packet, frame, payload)
        if message is None:
            # A frame the channel key did not open. See `PhoneAPI.heard`.
            print("# heard %s, sealed, not passed on" % who)
            return
        print("# heard %s, %s, %d bytes to the phone"
              % (who, payload.name, len(message)))
        self._offer(message)
        self._ring()

    def pump(self):
        """Moves whatever is waiting, in both directions. Cheap when idle."""
        self._watch()
        self._describe_link()
        while True:
            size = self._inbox.readinto(self._buf)
            if not size:
                break
            self.writes += 1
            before = self._count
            beats = self.api.beats
            for message in self.api.receive(memoryview(self._buf)[:size]):
                if not self._offer(message):
                    print("# phone: queue full, %d messages lost" % self.dropped)
                    break
            # One line per client request, which is the only view there is of a
            # conversation happening entirely inside the SoftDevice. The clock
            # is on it because the app's heartbeat is every thirty seconds and
            # its patience sixty, so the spacing of these lines is what says
            # whether a disconnect was the liveness timeout or something else.
            print("# phone: %d bytes in, %d queued, at %.1fs"
                  % (size, self._count - before,
                     time.monotonic() - self._since))
            if self._count == before and self.api.beats == beats:
                # A request that produced no answer and was not a heartbeat is
                # the interesting kind. The bytes say which `ToRadio` field it
                # was, which nothing else here can: the parse walks past a field
                # it does not know in silence.
                print("# phone: nothing to say to %s" % str(binascii.hexlify(
                    bytes(self._buf[:min(size, 24)])), "ascii"))
        self._keepalive()
        self._ring()
        # Last, and after the doorbell: an ordered restart waits until the
        # answer saying it was taken is queued and the client has been told
        # there is something to read.
        if self.api.restart_due():
            self.api.node.restart()

    def describe(self):
        return "phone %s, %d writes, %d queued, %d sent, %d refused, %d dropped" % (
            "connected" if self.connected else "waiting",
            self.writes, self._count, self.api.sent, self.api.refused,
            self.dropped)
