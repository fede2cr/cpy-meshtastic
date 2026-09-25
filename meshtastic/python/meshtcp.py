"""Serve the Meshtastic app over TCP, for boards whose BLE will not advertise.

The phone app offers three ways in -- Bluetooth, USB serial and Network -- and
all three carry the same two protobufs. What differs is only the envelope. BLE
gets three characteristics and a doorbell counter, because a GATT attribute is
a mailbox with no notion of where one message ends. A socket is a stream, so
Network puts the boundary in the bytes: `0x94 0xc3`, a sixteen-bit big-endian
length, then that many bytes of `ToRadio` or `FromRadio`. The app calls that
`StreamFrameCodec`, the firmware calls it the serial framing, and it is the
same four bytes either way -- which is why the USB and Network transports in
the app share a codec and differ only in what they open.

The framing itself is in Rust, in `meshtastic.stream`. A phone that has just
connected asks for the whole node database and gets it back in one burst of a
few kilobytes, so the byte that says "this frame is complete" is looked at a
few thousand times per session. What is left here is the socket: accepting one
client, reading what has arrived without waiting for more, and pushing out what
the socket will take this turn.

That last part is the one thing a naive version gets wrong. `send()` on a
non-blocking socket sends what fits and tells you how much, and during the
config dump what fits is regularly less than what was offered. So everything
outbound goes through one buffer that `pump()` drains, and a short write just
means the rest waits for the next turn rather than vanishing.

`meshapi.PhoneAPI` is shared with the BLE transport unchanged: it was written
against messages, not against a link, and this is the second link to prove it.

This module is merged into `meshphone.mpy` alongside `meshble`, and a merged
module is one flat namespace -- so everything here is prefixed or named apart
from what is already in there. `Network` is this transport's `Phone`.
"""

import gc
import os
import time

# Guarded, because this file is merged into meshphone.mpy beside `meshble` and
# a merged module runs every half of itself on import. A board with BLE and no
# radio for this must still be able to load that file and use the other
# transport; the refusal belongs in `tcp_join`, where it can say so.
try:
    import wifi
    import socketpool
except ImportError:
    wifi = None
    socketpool = None

try:
    import mdns
except ImportError:
    # Not every build has it, and being found by name is a convenience. The
    # address is printed either way, and the app will take one typed in.
    mdns = None

import meshtastic as _mt
from meshtastic import meshphone as meshapi

#: From `NetworkConstants.kt` in the Android app. Also what the CLI's
#: `--host` uses, and what the ESP32 firmware listens on.
TCP_PORT = 4403
#: The app browses for this, and for `_http._tcp` separately. Advertising it
#: puts the node in the app's device list instead of making someone type an
#: address that DHCP is free to change.
TCP_SERVICE = "_meshtastic"

#: One read. A frame is at most 512 bytes and the config dump arrives as many
#: small ones, so this is sized to swallow a burst rather than one message.
TCP_CHUNK = 512
#: How much unsent output to hold. The config dump is the high-water mark: a
#: few dozen frames of node info, offered faster than a socket with a small
#: window will take them. Past this the link is not keeping up and saying so
#: is better than growing a buffer until the heap ends.
TCP_QUEUE_BYTES = 8192

#: Same twenty seconds as the BLE transport, for the same reason: the app's
#: `checkLiveness` gives up after sixty seconds without a readable frame, and
#: measures that against a clock Android is free to suspend.
TCP_KEEPALIVE_S = 20

#: How long to wait for the access point before giving up and saying so.
TCP_CONNECT_S = 20

#: EAGAIN. A non-blocking `socketpool` socket never returns zero to mean "the
#: peer closed" -- it raises, the same way it raises for "nothing waiting".
#: This number is the whole difference between the two.
TCP_AGAIN = 11


def tcp_join(timeout=TCP_CONNECT_S):
    """Makes sure we are on the network. Returns the address as a string.

    CircuitPython joins by itself at boot when settings.toml names a network,
    so the usual answer is that there is nothing to do. This is for the times
    it did not: a board that woke before the access point did, or a session
    that has been up long enough to have been dropped once.
    """
    if wifi is None:
        raise OSError("this board has no wifi; MESH_PHONE = \"ble\"")
    if wifi.radio.connected:
        return str(wifi.radio.ipv4_address)
    ssid = os.getenv("CIRCUITPY_WIFI_SSID")
    password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
    if not ssid:
        raise OSError("no network: set CIRCUITPY_WIFI_SSID in settings.toml")
    wifi.radio.connect(ssid, password, timeout=timeout)
    if not wifi.radio.connected:
        raise OSError("could not join %s" % ssid)
    return str(wifi.radio.ipv4_address)


def tcp_listen(port=TCP_PORT):
    """A bound, listening, non-blocking socket."""
    pool = socketpool.SocketPool(wifi.radio)
    sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
    # Without this a socket left in TIME_WAIT by the last session refuses the
    # bind, and a node that has just been restarted is exactly when someone is
    # trying to connect to it.
    sock.setsockopt(pool.SOL_SOCKET, pool.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError:
        sock.close()
        raise OSError("port %d is taken" % port)
    # One client. The app opens a single socket and the firmware serves a
    # single socket; a second would see another node's config dump.
    sock.listen(1)
    sock.setblocking(False)
    return sock


def tcp_announce(hostname, port=TCP_PORT, instance=None):
    """Publishes the node over mDNS. The server, or None if it could not.

    The caller has to keep what this returns. `mdns.Server` is allocated with
    a finaliser that calls `mdns_free()`, so a collection with no reference
    left takes the advertisement down with it -- silently, and the socket goes
    on working, so what it looks like from the phone is a node that is there
    when you type its address and invisible when you browse for it.
    """
    if mdns is None:
        return None
    try:
        server = mdns.Server(wifi.radio)
        server.hostname = hostname
        if instance:
            # What the app lists, as opposed to what it resolves. Unset, the
            # IDF falls back to the hostname, and a row reading
            # "meshtastic-4f80da67" is a worse answer than the node's name.
            server.instance_name = instance
        server.advertise_service(service_type=TCP_SERVICE, protocol="_tcp",
                                 port=port)
        return server
    except (RuntimeError, OSError, ValueError):
        # One mDNS server per radio, and something else on the drive may hold
        # it. Being unlisted is not a reason to refuse to serve.
        return None


class Network:
    """The Meshtastic app over a socket. `meshble.Phone`'s other half."""

    def __init__(self, node, *, port=TCP_PORT, queue_bytes=TCP_QUEUE_BYTES):
        self.api = meshapi.PhoneAPI(node)
        self.port = port
        self.address = None
        self.host = None
        self.name = None
        self.connected = False
        self.writes = 0
        self.dropped = 0
        self._sock = None
        self._client = None
        self._mdns = None
        self._queue_bytes = queue_bytes
        #: The reassembler's whole working memory, frame included. Allocated
        #: once: this is touched on every read.
        self._state = bytearray(_mt.STREAM_STATE)
        self._chunk = bytearray(TCP_CHUNK)
        #: Header plus the largest legal body, reused for every outbound frame.
        self._wrap = bytearray(_mt.STREAM_HEADER + _mt.STREAM_MAX)
        self._out = bytearray()
        self._at = 0
        self._spoke = time.monotonic()
        self._since = time.monotonic()

        # Everything the node hears goes past the phone as well. The hook is
        # set last so a half-built session is never handed a packet.
        node.on_receive = self.heard

    # ------------------------------------------------------------- bringing up

    def advertise(self, hostname=None):
        """Starts listening. Returns the address to type into the app."""
        if self._sock is None:
            self.host = tcp_join()
            self._sock = tcp_listen(self.port)
            if hostname is None:
                hostname = "meshtastic-%08x" % self.api.node.config.node_num
            self.address = "%s:%d" % (self.host, self.port)
            self._mdns = tcp_announce(
                hostname, self.port,
                getattr(self.api.node.config, "long_name", None))
            if self._mdns is not None:
                self.name = hostname
                self.address = "%s (%s.local)" % (self.address, hostname)
        return self.address

    def stop(self):
        """Closes the client and the listener. Safe to call twice."""
        self._drop("stopped")
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._mdns is not None:
            # Before the collector gets to it, so `mdns_free()` runs while the
            # network is still up rather than at some later sweep.
            self._mdns.deinit()
            self._mdns = None
        self.address = None
        self.name = None

    # --------------------------------------------------------------- plumbing

    def _accept(self):
        """Takes a waiting client, if there is one and we have no other."""
        if self._sock is None or self._client is not None:
            return
        try:
            client, where = self._sock.accept()
        except OSError:
            # Nothing pending. The common case, every turn of the loop.
            return
        client.setblocking(False)
        self._client = client
        self.connected = True
        self._since = time.monotonic()
        self._spoke = time.monotonic()
        # A session starts empty, and starts the reassembler empty too: bytes
        # left over from a client that went away mid-frame are not a prefix of
        # anything this one is going to say.
        _mt.stream_reset(self._state)
        self._out = bytearray()
        self._at = 0
        # A client with a real clock is here, so the node stops taking the
        # time off the air.
        self.api.node.attached = True
        self.api.reset()
        print("# phone: %s connected from %s" % (self.address, where[0]))

    def _drop(self, why):
        """Ends the session. The listener stays up for the next client."""
        if self._client is None:
            return
        try:
            self._client.close()
        except OSError:
            pass
        self._client = None
        self.connected = False
        self.api.node.attached = False
        self.api.reset()
        lost, frames, partial = _mt.stream_counts(self._state)
        # The framing counters only mean anything at the end of a session, and
        # a non-zero `lost` on a TCP link is worth seeing: the stream is
        # ordered and reliable, so anything the reassembler threw away came
        # from the other end sending it, not from the network.
        print("# phone: %s after %.0fs, %d frames, %d bytes skipped%s"
              % (why, time.monotonic() - self._since, frames, lost,
                 ", %d mid-frame" % partial if partial else ""))
        gc.collect()

    def _offer(self, message):
        """Frames one `FromRadio` into the outbound buffer. False if it did not fit."""
        if len(message) > _mt.STREAM_MAX \
                or self._waiting() + len(message) + _mt.STREAM_HEADER > self._queue_bytes:
            self.dropped += 1
            return False
        size = _mt.stream_frame(self._wrap, 0, message)
        self._out.extend(memoryview(self._wrap)[:size])
        self._spoke = time.monotonic()
        return True

    def _waiting(self):
        """Bytes framed and not yet accepted by the socket."""
        return len(self._out) - self._at

    def _drain(self):
        """Pushes out what the socket will take this turn.

        The sent part is tracked with an offset rather than cut off the front:
        CircuitPython's bytearray has no slice deletion, and even where it does
        that is a copy of everything still waiting on every partial write.
        """
        if self._client is None or not self._waiting():
            return
        try:
            sent = self._client.send(memoryview(self._out)[self._at:])
        except OSError as error:
            if error.errno != TCP_AGAIN:
                self._drop("write failed")
            # Otherwise the window is full and the bytes stay queued.
            return
        self._at += sent
        if self._at >= len(self._out):
            self._out = bytearray()
            self._at = 0
        elif self._at >= self._queue_bytes:
            # A client taking bytes but never catching up, so the buffer is all
            # prefix. Pay for one copy rather than grow until the heap ends.
            self._out = bytearray(memoryview(self._out)[self._at:])
            self._at = 0

    def _keepalive(self):
        """Something to read on a link that has gone quiet. See `meshble`."""
        if self.api.config_id is None:
            return
        if time.monotonic() - self._spoke < TCP_KEEPALIVE_S:
            return
        self._spoke = time.monotonic()
        self._offer(self.api.alive())

    def _read(self):
        """Feeds everything that has arrived through the reassembler."""
        if self._client is None:
            return
        while True:
            try:
                size = self._client.recv_into(self._chunk)
            except OSError as error:
                if error.errno != TCP_AGAIN:
                    self._drop("disconnected")
                return
            self.writes += 1
            chunk = memoryview(self._chunk)[:size]
            at = 0
            queued = 0
            beats = self.api.beats
            while True:
                length, at = _mt.stream_feed(self._state, chunk, at)
                if not length:
                    break
                frame = memoryview(self._state)[_mt.STREAM_BODY:
                                                _mt.STREAM_BODY + length]
                for message in self.api.receive(frame):
                    if not self._offer(message):
                        print("# phone: queue full, %d messages lost"
                              % self.dropped)
                        break
                    queued += 1
                # Sending as we go rather than at the end: the config dump is
                # bigger than the outbound buffer, and draining between frames
                # is what keeps it from being the thing that overflows it.
                self._drain()
                if self._client is None:
                    return
            print("# phone: %d bytes in, %d queued, at %.1fs"
                  % (size, queued, time.monotonic() - self._since))
            if not queued and self.api.beats == beats:
                print("# phone: nothing to say to that")
            if size < TCP_CHUNK:
                # A short read means the socket is empty, so asking again only
                # costs an exception.
                return

    # --------------------------------------------------------------- the loop

    def heard(self, packet, frame, payload):
        """A frame off the air, on its way to the phone. For `Node.on_receive`."""
        who = self.api.node.nodes.name(packet.from_)
        if self.api.config_id is None:
            print("# heard %s, %s, no client asking"
                  % (who, "sealed" if payload is None else payload.name))
            return
        message = self.api.heard(packet, frame, payload)
        if message is None:
            print("# heard %s, sealed, not passed on" % who)
            return
        print("# heard %s, %s, %d bytes to the phone"
              % (who, payload.name, len(message)))
        self._offer(message)
        self._drain()

    def pump(self):
        """Moves whatever is waiting, in both directions. Cheap when idle."""
        self._accept()
        self._read()
        self._keepalive()
        self._drain()
        # Last: an ordered restart waits until the answer saying it was taken
        # is framed and pushed at the socket.
        if self.api.restart_due():
            self.api.node.restart()

    def describe(self):
        return "phone %s, %d reads, %d bytes waiting, %d sent, %d refused, %d dropped" % (
            "connected" if self.connected
            else "listening on %s" % (self.address or "nothing yet"),
            self.writes, self._waiting(), self.api.sent, self.api.refused,
            self.dropped)

    def rows(self):
        """Five lines of 21 columns, for `oled.Status`. The panel layout for a
        link belongs with the link, the way `Fix.rows` does for a fix."""
        return (
            "phone %s" % ("connected" if self.connected else "waiting"),
            self.host or "no network",
            "port %d" % self.port,
            "%s.local" % self.name if self.name else "not advertised",
            "%d in %d out %d drop" % (self.writes, self.api.sent, self.dropped),
        )
