"""Runs the Python layers on a host, against the stubs in hoststub.

What this can prove is that the modules agree with each other: that a frame
built by mesh_tx decodes back through meshtastic, that the keystore survives a
round trip through a page of flash, that the duty cycle refuses when it should.
What it cannot prove is anything about the radio or the Rust, which need
hardware and `cargo test` respectively.

    make test          # runs this
    venv/bin/python tools/hosttest.py
"""

import sys
import time

sys.path.insert(0, "tools")

import hoststub  # noqa: F401  -- stubs the board, and merges the library

# Every one of these is the same module object. The library ships as a single
# merged `meshtastic`, and the names below are kept only so that each call site
# still reads as the module the function came from.
import meshtastic as _mt
from meshtastic import meshlib as _pb  # the protobuf writers are the Python half
from meshtastic import meshlib as inbox
from meshtastic import meshlib as keystore
from meshtastic import meshlib as mesh_config
from meshtastic import meshlib as mesh_tx
from meshtastic import meshcache
from meshtastic import meshnode
from meshtastic import meshtext
from meshtastic import meshlib as mt
from meshtastic import meshlib as nodedb
from meshtastic import meshlib as nodeinfo

#: Writers that now take a buffer and an offset, because the shipped encoders
#: build a whole message in one. The tests below build protobuf by hand to
#: stand in for the phone, a field at a time, so they get the older shape back
#: through `pb` -- one field, one bytes object, concatenated with `+`.
_WRITERS = ("uint", "int32", "boolean", "fixed32", "float32", "blob", "string")


class _OneField:
    def __getattr__(self, name):
        value = getattr(_pb, name)
        if name not in _WRITERS:
            return value

        def build(*args, **kwargs):
            buf = _pb.msg_buffer()
            return _pb.msg_take(buf, value(buf, 0, *args, **kwargs))
        return build


pb = _OneField()

FAILED = []


def check(label, got, want):
    ok = got == want
    print("%-12s %s%s" % (label, got, "" if ok else "   WANT %s" % (want,)))
    if not ok:
        FAILED.append(label)


def frames():
    settings = mt.Settings(mt.region_index("US"), mt.preset_index("LongFast"))
    key = mesh_config.channel_key("AQ==")
    radio = hoststub.Radio()
    tx = mesh_tx.Transmitter(radio, settings, key, 0x4B8F1234)

    print("settings   -> %s" % (settings,))
    check("channel", "0x%02x" % tx.channel, "0x08")
    check("relay", "0x%02x" % tx.relay_node, "0x34")

    # Unset power means the smaller of the region's 30 dBm and the PA's 22.
    mt.protocol_configure(radio, settings)
    check("power", radio.power_dbm, 22)
    mt.protocol_configure(radio, settings, power_dbm=5)
    check("power set", radio.power_dbm, 5)
    try:
        mt.protocol_configure(radio, settings, power_dbm=31)
    except ValueError:
        check("over limit", "refused", "refused")
    else:
        check("over limit", "allowed", "refused")

    message = "Se reciben chistes en espanol para robby"
    frame = tx.frame(message.encode("utf-8"), mt.PORT_TEXT_MESSAGE)
    check("frame", len(frame), 60)

    payload = mt.decode(frame, key)
    check("round trip", payload.body, message.encode("utf-8"))
    print("printPacket-> %s" % mt.Packet(frame).describe(payload))
    check("wrong key", mt.decode(frame, b"\x00" * 16), None)
    check("airtime", "%d ms" % (tx.airtime_us(len(frame)) // 1000), "681 ms")

    ids = [tx.next_id() for _ in range(4)]
    print("ids        -> %s" % " ".join("0x%08x" % i for i in ids))
    lows = [i & 0x3FF for i in ids]
    check("ids count up", lows == sorted(lows), True)

    zero = mesh_tx.Transmitter(radio, settings, key, 0x11223300)
    check("relay 0x00", "0x%02x" % zero.relay_node, "0xff")
    try:
        mesh_tx.Transmitter(radio, settings, key, 0)
    except ValueError:
        check("node 0", "refused", "refused")
    else:
        check("node 0", "allowed", "refused")


def budget():
    settings = mt.Settings(mt.region_index("EU_868"),
                           mt.preset_index("LongFast"))
    radio = hoststub.Radio()
    tx = mesh_tx.Transmitter(radio, settings, mesh_config.channel_key("AQ=="),
                             0x4B8F1234, duty_cycle=mesh_tx.DutyCycle(1))
    sent = refused = 0
    for _ in range(200):
        if tx.text("x" * 30) is None:
            refused += 1
        else:
            sent += 1
    print("budget     -> %d sent, %d refused; %s" % (sent, refused,
                                                     tx.duty_cycle))
    check("refuses", refused > 0, True)


def store():
    """The keystore against a page that counts its erase cycles."""
    nvm = hoststub._mc.nvm
    check("blank", keystore.keystore_load(), {})

    before = nvm.writes
    keystore.keystore_provision(node_num=0x4B8F1234,
                       channel_key=bytes(range(16)),
                       channel_name="LongFast")
    check("wrote", nvm.writes - before, 1)
    check("node num", "0x%08x" % keystore.node_num(), "0x4b8f1234")
    check("key back", keystore.keystore_load()[keystore.CHANNEL_KEY],
          bytes(range(16)))
    check("name back", keystore.keystore_load()[keystore.CHANNEL_NAME],
          b"LongFast")
    # Only the bounded region is written. Building an image of the whole 4 KB
    # page is what ran the board out of heap at boot.
    tail = bytes(nvm[keystore.STORE_BYTES:len(nvm)])
    check("bounded", tail.count(0xFF), len(tail))

    # The point of reading before writing: provisioning twice is one erase.
    before = nvm.writes
    keystore.keystore_provision(node_num=0x4B8F1234)
    check("no rewrite", nvm.writes - before, 0)

    keystore.keystore_provision(channel_name=None)
    check("removed", keystore.CHANNEL_NAME in keystore.keystore_load(), False)
    check("survived", keystore.node_num(), 0x4B8F1234)

    print("describe   -> %s" % keystore.keystore_describe())
    check("no secrets", "000102" in keystore.keystore_describe(), False)

    # A store whose CRC does not match reads as empty rather than raising: a
    # board interrupted mid-write must still boot.
    nvm[8:9] = b"\x00"
    check("corrupt", keystore.keystore_load(), {})

    keystore.keystore_erase()
    check("erased", keystore.keystore_load(), {})


def stored_settings():
    """Settings in NVM: defaults, round trip, validation and seeding."""
    keystore.keystore_erase()
    cfg = mesh_config.Config()
    check("default region", cfg.region, "US")
    check("default preset", cfg.preset, mt.preset_index("LongFast"))
    check("default key", cfg.key_source, "default")

    mesh_config.config_configure(region="EU_868", preset="LongSlow",
                          long_name="Muzi Base Duo", short_name="muzi",
                          tx_power_dbm=14, duty_cycle_pct=10)
    cfg = mesh_config.Config()
    check("stored region", cfg.region, "EU_868")
    check("stored preset", cfg.preset, mt.preset_index("LongSlow"))
    check("stored power", cfg.tx_power_dbm, 14)
    check("stored duty", cfg.duty_cycle_pct, 10)
    check("stored names", nodeinfo.names(0x0B9DBDE1),
          ("Muzi Base Duo", "muzi"))

    # NVM, then settings.toml, then the pair derived from the node number.
    # Stock firmware has no settings.toml, so the derived pair is what a board
    # nobody has named must report -- and a rename typed into the phone has to
    # outrank a name baked into the file, or it would not survive the reboot.
    hoststub._sup.settings["MESH_LONG_NAME"] = "From The File"
    hoststub._sup.settings["MESH_SHORT_NAME"] = "file"
    check("nvm beats file", nodeinfo.names(0x0B9DBDE1),
          ("Muzi Base Duo", "muzi"))
    mesh_config.config_configure(long_name=None, short_name=None)
    check("file beats derived", nodeinfo.names(0x0B9DBDE1),
          ("From The File", "file"))
    hoststub._sup.settings.pop("MESH_LONG_NAME")
    hoststub._sup.settings.pop("MESH_SHORT_NAME")
    check("derived when unset", nodeinfo.names(0x0B9DBDE1),
          ("Meshtastic bde1", "bde1"))
    mesh_config.config_configure(long_name="Muzi Base Duo", short_name="muzi")

    print("settings   -> %s" % mesh_config.config_describe())

    # A bad name in a call that also carries good ones stores neither, so a
    # typo cannot leave the board half-configured.
    before = keystore.keystore_load()
    try:
        mesh_config.config_configure(region="ATLANTIS", preset="ShortFast")
        raise AssertionError("bad region was accepted")
    except ValueError:
        pass
    check("all or none", keystore.keystore_load(), before)

    check("removed", mesh_config.config_configure(region=None), True)
    check("back to default", mesh_config.Config().region, "US")

    # What code.py declares is applied at every boot; what it does not name is
    # left for the defaults, and stays the board's own.
    keystore.keystore_erase()
    mesh_config.config_provision(region="JP", nodeinfo_s=7200)
    check("declared", mesh_config.Config().region, "JP")
    check("declared interval", mesh_config.Config().nodeinfo_s, 7200)
    check("undeclared", keystore.PRESET in keystore.keystore_load(), False)

    # Unchanged is not rewritten, so a boot that moves nothing costs no erase.
    check("no rewrite", mesh_config.config_provision(region="JP",
                                                    nodeinfo_s=7200), False)

    # The prompt wins for the session, the file wins again at the next reset.
    mesh_config.config_configure(region="IN")
    check("prompt", mesh_config.Config().region, "IN")
    mesh_config.config_provision(region="JP")
    check("file wins on reset", mesh_config.Config().region, "JP")

    # A declared setting is checked before any of it is written, same as the
    # prompt: a typo in code.py must not take the rest of the file with it.
    try:
        mesh_config.config_provision(region="ATLANTIS", nodeinfo_s=3600)
        raise AssertionError("bad declared region was accepted")
    except ValueError:
        pass
    check("bad declaration stored nothing",
          mesh_config.Config().nodeinfo_s, 7200)
    keystore.keystore_erase()


def identity():
    """The nodeinfo encoder, read back by the decoder that reads the air."""
    node_num = 0x0B9DBDE1
    check("names", nodeinfo.names(node_num), ("Meshtastic bde1", "bde1"))

    # What the prompt does when it asks to be shown packets.
    mt.WANT_TEXT = True

    body = nodeinfo.user(node_num, "Muzi Base Duo", "muzi")
    settings = mt.Settings(mt.region_index("US"), mt.preset_index("LongFast"))
    radio = hoststub.Radio()
    tx = mesh_tx.Transmitter(radio, settings,
                             mesh_config.channel_key("AQ=="), node_num)
    frame = tx.frame(body, nodeinfo.PORT_NODEINFO)
    payload = mt.decode(frame, mesh_config.channel_key("AQ=="))
    print("nodeinfo   -> %s" % payload.describe())
    check("portnum", payload.portnum, nodeinfo.PORT_NODEINFO)
    check("body", payload.body, body)
    check("readable", 'long="Muzi Base Duo"' in payload.describe(), True)
    check("hw", "muzi-base" in payload.describe(), True)

    # Five bytes is the store's limit, and a name that long is cut here rather
    # than differently by each receiver.
    long_body = nodeinfo.user(node_num, "x" * 60, "toolong")
    described = mt.Payload(nodeinfo.PORT_NODEINFO, long_body,
                           False, None, None, None, None, None).describe()
    check("truncated", 'short="toolo"' in described, True)

    # A node serving the phone app renders nothing itself, so the text layer is
    # never loaded and the line says only what the header knows.
    mt.WANT_TEXT = False
    check("terse for the phone", payload.describe(),
          "port=nodeinfo bytes=%d" % len(body))
    mt.WANT_TEXT = True

    # Named by walking the packed string, so the ends and the gaps past it are
    # worth pinning: a split list would have made all four the same code path.
    check("hw first", meshtext.hardware(0), "unset")
    check("hw ours", meshtext.hardware(mt.HW_MUZI_BASE), "muzi-base")
    check("hw last", meshtext.hardware(
        len(meshtext._HW_NAMES.split(" ")) - 1), "axiometa-genesis-mini")
    check("hw this board", meshtext.hardware(145), "meshnology-w12")
    check("hw past the end", meshtext.hardware(250), "hw 250")
    check("hw private", meshtext.hardware(255), "private-hw")


#: Telemetry. Answers to a request arrive on it addressed to whoever asked.
PORT_TELEMETRY = 67


def answers():
    """Replies addressed to this node, read the way broadcasts are.

    `mesh_tx.frame` cannot build one of these -- `encode_data` has no
    `request_id` -- so the wire form is assembled here from the same protobuf
    pieces the firmware uses. That is the point of the test: what is decoded is
    a frame this repository cannot produce, only receive.
    """
    us = 0x4B8F1234
    them = 0x0B9DBDE1
    key = mesh_config.channel_key("AQ==")

    def answer(to, portnum, body, request_id=0):
        header = _mt.write_header(to, them, 0xA7C16FC0,
                                  _mt.build_flags(2, False, False, 3),
                                  0x08, 0, them & 0xFF)
        data = b"".join((pb.uint(1, portnum), pb.blob(2, body),
                         pb.boolean(3, False), pb.fixed32(4, to),
                         pb.fixed32(6, request_id)))
        return header + mt.crypt(header, data, key)

    metrics = pb.blob(3, b"".join((pb.uint(1, 87), pb.uint(4, 1))))
    frame = answer(us, PORT_TELEMETRY, metrics, 0xDEADBEEF)
    payload = mt.decode(frame, key)
    print("answer     -> %s" % mt.Packet(frame).describe(payload))
    check("unicast to", mt.Packet(frame).to, us)
    check("unicast port", payload.portnum, PORT_TELEMETRY)
    check("request id", payload.request_id, 0xDEADBEEF)
    check("dest", payload.dest, us)

    # The same payload sent to everybody. Equal bodies is the whole claim: the
    # receive path never reads `to`, so an answer cannot be lost for being one.
    everyone = mt.decode(answer(mt.BROADCAST, PORT_TELEMETRY, metrics), key)
    check("same as broadcast", everyone.body, payload.body)

    named = mt.decode(answer(us, nodeinfo.PORT_NODEINFO,
                             nodeinfo.user(them, "Muzi Base Duo", "muzi"),
                             0xDEADBEEF), key)
    check("unicast nodeinfo", 'short="muzi"' in named.describe(), True)

    reply = answer(us, mt.PORT_TEXT_MESSAGE, b"pong", 0xDEADBEEF)
    box = inbox.Inbox(us, "LongFast")
    message = box.add(mt.Packet(reply), mt.decode(reply, key))
    check("filed direct", message.direct, True)
    check("filed under", message.where, "!%08x" % them)


def neighbours():
    """The node database: bounded, aged, and fed from real headers."""
    import struct

    def packet(from_, hop_start=3, hop_limit=3):
        flags = hop_limit | (hop_start << 5)
        frame = struct.pack("<III4B", mt.BROADCAST, from_, 0x1234, flags,
                            0x08, 0x00, from_ & 0xFF) + b"\x00"
        return mt.Packet(frame, rssi=-90.0, snr=7.25, signal_rssi=-92.0)

    db = nodedb.NodeDB(limit=4)
    for i in range(6):
        db.heard(packet(0x1000 + i))
    check("bounded", len(db), 4)
    check("forgotten", db.forgotten, 2)
    check("evicted oldest", 0x1000 in db, False)
    check("kept newest", 0x1005 in db, True)

    # Hearing a node again is what keeps it, so a quiet stranger goes before a
    # noisy neighbour no matter which arrived first.
    db.heard(packet(0x1002))
    db.heard(packet(0x2000))
    check("lru kept", 0x1002 in db, True)
    check("lru dropped", 0x1003 in db, False)

    peer = db.heard(packet(0x0B9DBDE1))
    check("unnamed", peer.name, "0x0b9dbde1")
    check("hops", peer.hops, 0)
    db.learn(0x0B9DBDE1, nodeinfo.user(0x0B9DBDE1, "Muzi Base Duo", "muzi"))
    check("learned", db.name(0x0B9DBDE1), "muzi")
    check("long name", peer.long, "Muzi Base Duo")
    check("seen twice", db.heard(packet(0x0B9DBDE1)).count, 2)

    # Every node repeats its nodeinfo for as long as it is switched on, and a
    # repeat that says nothing new must not cost a flash erase.
    db.dirty = False
    db.learn(0x0B9DBDE1, nodeinfo.user(0x0B9DBDE1, "Muzi Base Duo", "muzi"))
    check("repeat is not news", db.dirty, False)
    db.learn(0x0B9DBDE1, nodeinfo.user(0x0B9DBDE1, "Muzi Base Duo", "duo"))
    check("a new name is", db.dirty, True)

    check("unheard", db.name(0x4B8F1234), "0x4b8f1234")
    check("unheard learn", db.learn(0x4B8F1234, b"\x1a\x04muzi"), None)

    # Names are chosen by strangers, so neither their length nor their content
    # may decide what a line looks like.
    db.learn(0x0B9DBDE1, nodeinfo.user(0x0B9DBDE1, "y" * 200, "ab\ncd"))
    check("name capped", len(peer.long), nodeinfo.MAX_LONG_NAME)
    check("one line", "\n" in peer.line(), False)
    check("short scrubbed", peer.short, "ab?cd")

    print("roster     -> %s" % db.roster()[0].line())
    check("newest first", db.roster()[0].num, 0x0B9DBDE1)


def messages():
    """The inbox: grouped, bounded, and read only once."""
    import struct

    us = 0x4B8F1234
    box = inbox.Inbox(us, "LongFast", limit=4)

    def text(from_, body, to=mt.BROADCAST):
        frame = struct.pack("<III4B", to, from_, 0x1234, 0x63,
                            0x08, 0x00, from_ & 0xFF)
        packet = mt.Packet(frame, rssi=-90.0, snr=7.25, signal_rssi=-92.0)
        payload = mt.Payload(mt.PORT_TEXT_MESSAGE, body.encode(),
                             False, None, None, None, None, None)
        return box.add(packet, payload)

    check("empty", box.unread, False)
    text(0x0B9DBDE1, "hello mesh")
    check("stored", len(box), 1)
    check("unread", box.unread, True)

    # A message addressed to this node is its own conversation, which is the
    # whole reason for grouping when only one channel can be decrypted.
    text(0x0B9DBDE1, "just you", to=us)
    check("conversations", sorted(box.conversations()),
          ["!0b9dbde1", "LongFast"])
    check("direct", box.read("!0b9dbde1")[0].text, "just you")
    check("marked read", box.unread_count("!0b9dbde1"), 0)
    check("other unread", box.unread_count("LongFast"), 1)

    # Not "\xff\xfe" as a str, which encodes to perfectly good UTF-8. The
    # payload has to carry the raw bytes to be undecodable at all.
    bad = mt.Payload(mt.PORT_TEXT_MESSAGE, b"\xff\xfe",
                     False, None, None, None, None, None)
    check("non text", box.add(mt.Packet(b"\x00" * 16), bad), None)
    other = mt.Payload(mt.PORT_TEXT_MESSAGE + 1, b"x",
                       False, None, None, None, None, None)
    check("wrong port", box.add(mt.Packet(b"\x00" * 16), other), None)

    for i in range(6):
        text(0x0B9DBDE1, "filler %d" % i)
    check("bounded", len(box), 4)
    check("oldest gone", box.read()[0].text, "filler 2")
    check("counted", box.dropped, 4)

    long_one = text(0x0B9DBDE1, "z" * 500)
    check("text capped", len(long_one.text), inbox.MAX_TEXT)
    newline = text(0x0B9DBDE1, "two\nlines")
    check("one line", "\n" in newline.line("muzi"), False)
    print("message    -> %s" % newline.line("muzi"))
    box.read()
    check("read once", box.unread, False)

    # The other bound. A count limit alone cannot be held against the free
    # heap, because what a message costs is up to whoever typed it.
    small = inbox.Inbox(us, "LongFast",
                        capacity=(inbox.RECORD_BYTES + 4) * 2)
    for i in range(3):
        packet = mt.Packet(struct.pack("<III4B", mt.BROADCAST, 1, i, 0x63,
                                       0x08, 0x00, 0x01))
        small.add(packet, mt.Payload(mt.PORT_TEXT_MESSAGE, b"abcd",
                                     False, None, None, None, None, None))
    check("arena bounded", len(small), 2)
    check("arena dropped", small.dropped, 1)


def cache():
    """The roster and the inbox through flash and back."""

    class Node:
        def __init__(self):
            self.nodes = nodedb.NodeDB(limit=8)
            self.inbox = inbox.Inbox(0x4B8F1234, "LongFast", limit=4)

    now = nodedb.seconds()
    node = Node()
    node.nodes.restore(0x0B9DBDE1, short="muzi", long="Muzi Base Duo",
                       hw=9, role=1, count=3, hops=0, snr=7.25, last=now)
    node.nodes.restore(0x0B9DBD01, count=2, hops=1, last=now)
    node.inbox.restore(0x9C3C42B3, 0xC38C5A53, mt.BROADCAST, 0, 0, 29,
                       b"hello", now, False)
    check("saved", meshcache.cache_save(node), True)

    back = Node()
    check("restored", meshcache.cache_load(back), (2, 1))
    # A peer with no name still earns its place on file: it is eleven bytes,
    # and dropping it emptied the roster of any board whose neighbours had not
    # got round to sending a nodeinfo.
    check("nameless kept", 0x0B9DBD01 in back.nodes, True)
    kept = back.nodes.get(0x0B9DBDE1)
    check("short kept", kept.short, "muzi")
    check("long kept", kept.long, "Muzi Base Duo")
    check("hw kept", kept.hw, 9)
    check("message kept", back.inbox.read(mark=False)[0].text, "hello")

    # The blob is assembled in one buffer now, so the comparison that spares an
    # erase cycle has to hold for a bytearray as well as for bytes.
    check("rewrite refused", meshcache.cache_save(node), False)


def strictness():
    """The stubs take away what CircuitPython does not have."""
    import struct
    check("no Struct", hasattr(struct, "Struct"), False)


def bluetooth():
    """What a phone scanning for Meshtastic nodes would see."""
    from meshtastic import meshphone as meshble
    node_num = 0x0B9DBDE1

    check("unnamed", meshble.device_name(node_num), "Meshtastic_bde1")
    check("named", meshble.device_name(node_num, "muzi"), "muzi_bde1")
    # The firmware suppresses a short name that is only the hex tail, because
    # that is what it already falls back to.
    check("default short", meshble.device_name(node_num, "bde1"),
          "Meshtastic_bde1")

    name = meshble.advertise(node_num, "muzi")
    adapter = hoststub._bleio.adapter
    print("advertising -> %s %s" % (name, adapter.data.hex()))
    check("gap name", adapter.name, "muzi_bde1")
    # A 128-bit UUID goes out reversed, so the canonical form reads backwards
    # in the packet. Getting this wrong makes the node simply invisible.
    check("service uuid", adapter.data[-16:].hex(),
          "fdea73e2ca5da89f1f46a81518b2a16b")
    check("flags", adapter.data[:3], bytes.fromhex("020106"))
    check("fits", len(adapter.data) <= 31, True)
    check("name in scan response", adapter.scan_response,
          bytes([10, 0x09]) + b"muzi_bde1")

    meshble.ble_stop()
    check("stopped", adapter.advertising, False)


class _FakeNode:
    """Just the parts of a Node that the phone API reaches for.

    A real Node needs pins, a bus and a flash page. What is being tested here is
    protobuf and policy, so the node is reduced to the handful of attributes
    that feed them, plus a `send` that records rather than transmits.
    """

    def __init__(self, node_num=0x0B9DBDE1):
        self.config = mesh_config.Config()
        self.config.node_num = node_num
        self.config.channel_key = mesh_config.channel_key("AQ==")
        self.settings = self.config.settings()
        self.radio = hoststub.Radio()
        self.power = hoststub.Power()
        self.tx = mesh_tx.Transmitter(self.radio, self.settings,
                                      self.config.channel_key, node_num)
        self.nodes = nodedb.NodeDB()
        self.clock_set = False
        self.clock_from_phone = False
        self.attached = False
        self.on_receive = None
        self.sent = 0
        self.heard = 0
        self.decoded = 0
        self.dropped = 0
        #: What `send` was asked to transmit, in order.
        self.outbox = []
        self.restarts = 0

    def send(self, payload, portnum, **kwargs):
        self.outbox.append((portnum, payload, kwargs))
        self.sent += 1
        return kwargs.get("packet_id") or 0x11112222

    def set_clock(self, when):
        self.clock_set = True
        self.clock_from_phone = True
        self.clock_at = when

    def reload(self):
        # The node number is the fake's own rather than the store's, so it is
        # the one thing a re-read must not take back.
        node_num = self.config.node_num
        self.config = mesh_config.Config()
        self.config.node_num = node_num
        self.settings = self.config.settings()

    def factory_reset(self, full=False):
        keystore.keystore_erase()
        if full:
            self.nodes.forget_all()

    def restart(self):
        self.restarts += 1


def _wrapper(data):
    """The one field a FromRadio or Config wrapper carries."""
    for number, _wire, value, off, length in mt.fields(data):
        return number, value, bytes(data[off:off + length])
    return None, None, b""


#: Every encoder's output as the app has always been given it, captured from
#: the implementation these were checked against on hardware. They exist to be
#: compared byte for byte after the encoders are rewritten: an encoder can be
#: made to allocate less without changing what it says, and this is what says
#: so. A diff here is a protocol change, whether or not one was meant.
GOLDEN = (
    ("my_node_info",
     "08e1fbf65c58f8eb016a1b63697263756974707974686f6e2d6d757a695f626173"
     "655f64756f7807"),
    ("device_metadata", "0a0e322e372e32362e353465306438642801485d"),
    ("node_info_bare", "08e1fbf65c"),
    ("node_info_full", "08e1fbf65c12020102250000e8c02d00f153654803"),
    ("channel_off", "08031200"),
    ("channel_on",
     "121c1210010101010101010101010101010101011a084c6f6e67466173741801"),
    ("lora_preset", "0801380140034801501658146801"),
    ("lora_custom", "18fa01200b2805380140034801501658146801"),
    ("data", "0801120268691801250900000035050000003d060000004801"),
    ("queue_status", "1001180120c4e6888901"),
    ("mesh_packet",
     "0de1bd9d0b15ffffffff2202080135443322113d00f15365450000e8c04803500160"
     "9cffffffffffffffff0178039001229801e101"),
)


def _golden_cases(meshapi):
    """The call behind each name in GOLDEN, chosen to reach every branch."""
    settings = mt.Settings(mt.region_index("US"), mt.preset_index("LongFast"))
    # A preset the app has no number for, which is what makes the encoder send
    # bandwidth, spreading factor and coding rate instead.
    custom = mt.Settings(mt.region_index("US"), mt.preset_index("LongFast"))
    custom.preset_name = "Custom"
    return {
        "my_node_info": lambda: meshapi.my_node_info(0x0B9DBDE1, 7),
        "device_metadata": lambda: meshapi.device_metadata(),
        "node_info_bare": lambda: meshapi.node_info(0x0B9DBDE1),
        "node_info_full": lambda: meshapi.node_info(
            0x0B9DBDE1, user=b"\x01\x02", snr=-7.25,
            last_heard=1700000000, hops=3),
        "channel_off": lambda: meshapi.channel(3),
        "channel_on": lambda: meshapi.channel(
            0, name="LongFast", psk=b"\x01" * 16, role=1),
        "lora_preset": lambda: meshapi.lora_config(settings, 3, 22, 20),
        "lora_custom": lambda: meshapi.lora_config(custom, 3, 22, 20),
        "data": lambda: meshapi._data(
            1, b"hi", want_response=True, dest=9, request_id=5, reply_id=6,
            bitfield=1),
        "queue_status": lambda: meshapi.queue_status(0x11223344),
        "mesh_packet": lambda: meshapi.mesh_packet(
            0x0B9DBDE1, 0xFFFFFFFF, 0, 0x11223344, decoded=b"\x08\x01",
            snr=-7.25, rssi=-100, rx_time=1700000000, hop_limit=3,
            hop_start=3, want_ack=True, via_mqtt=False, next_hop=0x22,
            relay_node=0xE1),
    }


def wire():
    """Every encoder against the bytes it has always produced."""
    from meshtastic import meshphone as meshapi

    cases = _golden_cases(meshapi)
    check("golden covers all", sorted(cases), sorted(n for n, _ in GOLDEN))
    for name, want in GOLDEN:
        got = cases[name]()
        check(name, got.hex(), want)

    # An encoder called to make a submessage of the message being written must
    # not be handed the buffer that message is half-built in. Nothing shipped
    # does this today, and this is what says so if something starts to.
    buf = _pb.msg_buffer()
    at = _pb.uint(buf, 0, 1, 7)
    inner = meshapi.device_metadata()
    at = _pb.nested(buf, at, 2, inner)
    check("nesting does not clobber", _pb.msg_take(buf, at).hex(),
          "0807" + "12%02x" % len(inner) + inner.hex())

    # The buffer is handed back afterwards, or every message would allocate a
    # fresh one and this would all have been for nothing.
    first = _pb.msg_buffer()
    _pb.msg_take(first, 0)
    check("buffer is reused", _pb.msg_buffer() is first, True)
    _pb.msg_take(first, 0)

    # Too big is refused rather than quietly truncated or grown.
    buf = _pb.msg_buffer()
    try:
        _pb.blob(buf, 0, 1, b"x" * len(buf))
        raise AssertionError("oversized message was accepted")
    except ValueError:
        pass
    _pb.msg_take(buf, 0)


def phone():
    """A whole app session, from want_config_id to a message going out."""
    from meshtastic import meshphone as meshapi
    from meshtastic import meshphone as meshble

    # The two enums that are renumbered on the way out. A silent mistake here
    # tells the phone a plausible lie about which band this node is on.
    check("proto region", meshapi.PROTO_REGIONS["US"], 1)
    check("proto preset", meshapi.PROTO_PRESETS["LongFast"], 0)
    check("no LongTurbo", "LongTurbo" in meshapi.PROTO_PRESETS, False)
    check("negative int32", pb.int32(12, -120).hex(), "6088ffffffffffffffff01")

    node = _FakeNode()
    node.nodes.heard(mt.Packet(mesh_tx.Transmitter(
        node.radio, node.settings, node.config.channel_key,
        0x4B8F1234).frame(b"hi", mt.PORT_TEXT_MESSAGE)))
    api = meshapi.PhoneAPI(node)

    dump = [_wrapper(m) for m in api.config(0xDEADBEEF)]
    counts = {}
    for number, _value, _body in dump:
        counts[number] = counts.get(number, 0) + 1
    print("dump       -> %d messages, %d bytes"
          % (len(dump), sum(len(b) + 2 for _n, _v, b in dump)))
    check("channels", counts[meshapi.FROM_CHANNEL], meshapi.NUM_CHANNELS)
    check("configs", counts[meshapi.FROM_CONFIG], 8)
    check("modules", counts[meshapi.FROM_MODULE_CONFIG],
          meshapi.MODULE_CONFIGS)
    # Ourselves plus the one node heard above.
    check("node infos", counts[meshapi.FROM_NODE_INFO], 2)
    check("terminator", dump[-1][0], meshapi.FROM_COMPLETE_ID)
    check("id echoed", dump[-1][1], 0xDEADBEEF)
    check("asked", api.config_id, 0xDEADBEEF)
    # It has to fit in one go: the phone reads the first empty answer as the end
    # of the dump, so a queue that fills mid-way is a node with no channels.
    check("fits the queue",
          sum(len(b) + 2 for _n, _v, b in dump) < meshble.QUEUE_BYTES, True)

    lora = None
    for number, _value, body in dump:
        if number == meshapi.FROM_CONFIG:
            which, _v, inner = _wrapper(body)
            if which == meshapi.CONFIG_LORA:
                lora = inner
    fields = {n: v for n, _w, v, _o, _l in mt.fields(lora)}
    check("use_preset", fields[1], 1)
    check("lora region", fields[7], 1)
    check("lora preset", fields.get(2, 0), 0)

    # The two-stage handshake. The app asks for the halves separately, and the
    # half it did not ask for must not be there: `my_info` arriving in stage two
    # resets its handshake and makes it reject the terminator that follows.
    stage1 = [_wrapper(m) for m in api.config(meshapi.CONFIG_NONCE)]
    kinds = set(n for n, _v, _b in stage1)
    check("stage 1 ends", stage1[-1][1], meshapi.CONFIG_NONCE)
    check("stage 1 has my_info", meshapi.FROM_MY_INFO in kinds, True)
    check("stage 1 has no nodes", meshapi.FROM_NODE_INFO in kinds, False)
    stage2 = [_wrapper(m) for m in api.config(meshapi.NODE_INFO_NONCE)]
    kinds = set(n for n, _v, _b in stage2)
    check("stage 2 ends", stage2[-1][1], meshapi.NODE_INFO_NONCE)
    check("stage 2 has no my_info", meshapi.FROM_MY_INFO in kinds, False)
    check("stage 2 nodes", len(stage2) - 1, 2)
    # Split in two, nothing is lost and nothing is said twice.
    check("halves make a whole", len(stage1) + len(stage2) - 1, len(dump))

    # The transport. One read hands out one message, and a read past the end
    # comes back empty, which is the only way the phone is told the dump ended.
    session = meshble.Phone(node)
    check("hooked up", node.on_receive == session.heard, True)
    session._to_radio.written.append(pb.uint(meshapi.TO_WANT_CONFIG_ID, 4242))
    session.pump()
    read = []
    while True:
        one = session._from_radio.read()
        if not one:
            break
        read.append(_wrapper(one))
    check("read back", len(read), len(dump))
    check("read order", read[0][0], meshapi.FROM_MY_INFO)
    check("read ends", read[-1][1], 4242)
    check("nothing dropped", session.dropped, 0)
    # The doorbell is rung once per pump, after everything is queued.
    check("doorbell", session._from_num.value,
          hoststub._struct.pack("<I", len(dump)))

    # Sending. The app builds the whole MeshPacket, including the id it will
    # track the message by, and expects it back as proof the radio took it.
    data = meshapi._data(mt.PORT_TEXT_MESSAGE, b"hola")
    session._to_radio.written.append(pb.message(
        meshapi.TO_PACKET,
        pb.fixed32(2, mt.BROADCAST) + pb.message(4, data)
        + pb.fixed32(6, 0x0BAD1DEA) + pb.boolean(10, True)))
    session.pump()
    check("transmitted", node.outbox[0][0], mt.PORT_TEXT_MESSAGE)
    check("text", bytes(node.outbox[0][1]), b"hola")
    check("id kept", node.outbox[0][2]["packet_id"], 0x0BAD1DEA)
    check("ack asked", node.outbox[0][2]["want_ack"], True)
    # The app will not hand over a second packet until it has heard what became
    # of the first, so the status comes back before the echo.
    queued = _wrapper(session._from_radio.read())
    check("queue status", queued[0], meshapi.FROM_QUEUE_STATUS)
    status = {n: v for n, _w, v, _o, _l in mt.fields(queued[2])}
    check("queue ok", status.get(1, 0), 0)
    check("queue names it", status[4], 0x0BAD1DEA)
    echoed = _wrapper(session._from_radio.read())
    check("echoed", echoed[0], meshapi.FROM_PACKET)
    echo = {n: v for n, _w, v, _o, _l in mt.fields(echoed[2])}
    check("echo from", echo[1], node.config.node_num)
    check("echo id", echo[6], 0x0BAD1DEA)

    # Addressed to this node rather than through it, on a port with no module
    # behind it. Nothing goes on the air and nothing is refused: a node with the
    # module switched off drops the packet and tells the client the radio took
    # it. The app tries this every session -- port 65 is store-and-forward
    # asking for history -- and answering NOT_AUTHORIZED claimed a permission
    # problem that was never the reason.
    session._to_radio.written.append(pb.message(
        meshapi.TO_PACKET,
        pb.fixed32(2, node.config.node_num) + pb.message(4, data)))
    session.pump()
    check("not on the air", len(node.outbox), 1)
    check("not refused", session.api.refused, 0)
    check("ignored", session.api.ignored, 1)
    taken = _wrapper(session._from_radio.read())
    check("taken", taken[0], meshapi.FROM_QUEUE_STATUS)
    check("no error", {n: v for n, _w, v, _o, _l in
                       mt.fields(taken[2])}.get(1, 0), meshapi.ROUTING_NONE)
    check("nothing else back", session._from_radio.read(), b"")

    # The same packet with an answer wanted cannot be left at that: the client
    # blocks on a reply this node has no module to produce.
    session._to_radio.written.append(pb.message(
        meshapi.TO_PACKET,
        pb.fixed32(2, node.config.node_num)
        + pb.message(4, meshapi._data(mt.PORT_TEXT_MESSAGE, b"hola",
                                      want_response=True))))
    session.pump()
    check("still not refused", session.api.refused, 0)
    unanswerable = _wrapper(session._from_radio.read())
    check("no response",
          {n: v for n, _w, v, _o, _l in mt.fields(unanswerable[2])}[1],
          meshapi.ROUTING_NO_RESPONSE)
    check("nothing else back", session._from_radio.read(), b"")

    # A heartbeat every thirty seconds, and the app drops the link after sixty
    # with nothing to read. An empty read is not an answer -- it only means the
    # queue is empty -- so the queue status going back is the whole of what
    # keeps a connection up over a mesh that has nothing else to say.
    beats = session.api.beats
    refused = session.api.refused
    session._to_radio.written.append(
        pb.message(meshapi.TO_HEARTBEAT, pb.uint(1, 3)))
    session.pump()
    check("heard the beat", session.api.beats - beats, 1)
    check("beat not refused", session.api.refused - refused, 0)
    beat = _wrapper(session._from_radio.read())
    check("beat answered", beat[0], meshapi.FROM_QUEUE_STATUS)
    alive = {n: v for n, _w, v, _o, _l in mt.fields(beat[2])}
    check("beat ok", alive.get(1, 0), meshapi.ROUTING_NONE)
    check("queue free", alive.get(2, 0), meshapi.QUEUE_LEN)
    # Not about any packet in particular: naming one would tell the app a
    # message it is still waiting on has been dealt with.
    check("beat names no packet", alive.get(4, 0), 0)
    check("one answer only", session._from_radio.read(), b"")

    # The app's heartbeat tick is the same tick that decides the link is dead,
    # so when Android suspends it the check wakes up already failing and no
    # answer can arrive in time. What survives that is a frame the app read
    # before the tick, so an idle link is given the queue state unprompted.
    session.pump()
    check("nothing while busy", session._from_radio.read(), b"")
    session._spoke -= meshble.KEEPALIVE_S
    session.pump()
    kept = _wrapper(session._from_radio.read())
    check("kept alive", kept[0], meshapi.FROM_QUEUE_STATUS)
    check("keepalive names no packet",
          {n: v for n, _w, v, _o, _l in mt.fields(kept[2])}.get(4, 0), 0)
    check("one keepalive only", session._from_radio.read(), b"")
    session.pump()
    check("and then quiet again", session._from_radio.read(), b"")

    # Telemetry about ourselves is a question about a client, not a command to
    # one, so it is answered. The app asks by sending the variant it wants,
    # empty, and the answer comes back in the same field.
    for kind, name in ((meshapi.TELEMETRY_DEVICE, "device"),
                       (meshapi.TELEMETRY_LOCAL_STATS, "stats")):
        request = meshapi._data(meshapi.PORT_TELEMETRY, pb.message(kind, b""),
                                want_response=True, dest=node.config.node_num)
        session._to_radio.written.append(pb.message(
            meshapi.TO_PACKET,
            pb.fixed32(2, node.config.node_num) + pb.message(4, request)
            + pb.fixed32(6, 0x0DEFACED)))
        session.pump()
        status = _wrapper(session._from_radio.read())
        check("%s accepted" % name,
              {n: v for n, _w, v, _o, _l in mt.fields(status[2])}.get(1, 0), 0)
        reply = _wrapper(session._from_radio.read())
        check("%s answered" % name, reply[0], meshapi.FROM_PACKET)
        packet = {n: (v, o, l) for n, _w, v, o, l in mt.fields(reply[2])}
        check("%s to us" % name, packet[2][0], node.config.node_num)
        _v, off, length = packet[4]
        data = bytes(reply[2][off:off + length])
        body = {n: (v, o, l) for n, _w, v, o, l in mt.fields(data)}
        check("%s port" % name, body[1][0], meshapi.PORT_TELEMETRY)
        # Without request_id the app cannot tell this from an unrelated
        # message that happened to arrive next, and goes on waiting.
        check("%s request id" % name, body[6][0], 0x0DEFACED)
        _v, off, length = body[2]
        kinds = set(n for n, _w, _v, _o, _l in mt.fields(data[off:off + length]))
        check("%s variant" % name, kind in kinds, True)
    check("telemetry not on the air", len(node.outbox), 1)
    check("telemetry not refused", session.api.refused, 0)

    # DeviceMetrics carries the battery only when there is one to report. The
    # app draws an absent level as unknown and a present one as a gauge, so
    # sending 0% for a board running on its power path would read as an
    # emergency rather than as a mains-powered node.
    def metrics(millivolts, state=0):
        node.power = hoststub.Power(millivolts, state)
        body = meshapi.device_metrics(node, 60)
        return {n: v for n, _w, v, _o, _l in mt.fields(body)}

    check("battery level", metrics(3800).get(1), 60)
    check("battery voltage", metrics(3800).get(2) is not None, True)
    # Charging holds at 99 until the charger releases STAT2, so a node does not
    # sit at 100% for the last half hour of a charge.
    check("full and charging", metrics(4200, 1).get(1), 99)
    check("full and idle", metrics(4200).get(1), 100)
    check("no pack is powered", metrics(1200).get(1), 101)
    check("no pack has no voltage", metrics(1200).get(2), None)
    check("flat pack still reports", metrics(3050).get(1), 0)
    node.power = hoststub.Power()

    # The one admin field this node honours, because it is the only clock a
    # board with no RTC, no GPS and no network is ever going to get.
    admin = meshapi._data(meshapi.PORT_ADMIN, pb.fixed32(43, 1767225600),
                          dest=node.config.node_num)
    session._to_radio.written.append(pb.message(
        meshapi.TO_PACKET,
        pb.fixed32(2, node.config.node_num) + pb.message(4, admin)))
    session.pump()
    check("clock taken", node.clock_at, 1767225600)
    check("clock is the phone's", node.clock_from_phone, True)
    check("time not refused", session.api.refused, 0)
    accepted = _wrapper(session._from_radio.read())
    check("time accepted",
          {n: v for n, _w, v, _o, _l in mt.fields(accepted[2])}.get(1, 0), 0)

    def admin(body, packet_id=0x0ADD1000):
        """Sends one AdminMessage and returns the reply body, or None."""
        session._to_radio.written.append(pb.message(
            meshapi.TO_PACKET,
            pb.fixed32(2, node.config.node_num)
            + pb.message(4, meshapi._data(meshapi.PORT_ADMIN, body,
                                          dest=node.config.node_num))
            + pb.fixed32(6, packet_id)))
        session.pump()
        status = _wrapper(session._from_radio.read())
        if {n: v for n, _w, v, _o, _l in mt.fields(status[2])}.get(1, 0):
            return None
        reply = _wrapper(session._from_radio.read())
        if not reply[0]:
            return b""
        packet = {n: (o, l) for n, _w, _v, o, l in mt.fields(reply[2])}
        off, length = packet[4]
        data = bytes(reply[2][off:off + length])
        fields = {n: (v, o, l) for n, _w, v, o, l in mt.fields(data)}
        check("admin request id", fields[6][0], packet_id)
        _v, off, length = fields[2]
        return bytes(data[off:off + length])

    # A read. It is answered, and the answer carries the passkey that every
    # write afterwards has to quote back -- the app discards an empty one, so
    # a response without it leaves the config screen waiting forever.
    owner = admin(pb.boolean(meshapi.ADMIN_GET_OWNER, True))
    parts = {n: (o, l) for n, _w, _v, o, l in mt.fields(owner)}
    check("owner answered", meshapi.ADMIN_OWNER_RESPONSE in parts, True)
    off, length = parts[meshapi.ADMIN_SESSION_PASSKEY]
    passkey = bytes(owner[off:off + length])
    check("passkey sent", len(passkey), meshapi.PASSKEY_BYTES)
    off, length = parts[meshapi.ADMIN_OWNER_RESPONSE]
    # Offsets only mean anything against the buffer they were walked from, so
    # the slice has to be kept rather than re-cut from its parent.
    body = bytes(owner[off:off + length])
    user = {n: (o, l) for n, _w, _v, o, l in mt.fields(body)}
    off, length = user[2]
    check("owner name", str(body[off:off + length], "utf-8"),
          "Meshtastic bde1")

    # Config comes back by a number one lower than the one it goes out under:
    # AdminMessage.ConfigType counts from zero and Config's oneof from one.
    reply = admin(pb.uint(meshapi.ADMIN_GET_CONFIG,
                          meshapi.CONFIG_LORA - meshapi.CONFIG_TYPE_OFFSET))
    parts = {n: (o, l) for n, _w, _v, o, l in mt.fields(reply)}
    off, length = parts[meshapi.ADMIN_CONFIG_RESPONSE]
    check("config section", _wrapper(reply[off:off + length])[0],
          meshapi.CONFIG_LORA)
    # And the channel is asked for by index plus one, so that channel zero is
    # not the same as a field the client left unset.
    reply = admin(pb.uint(meshapi.ADMIN_GET_CHANNEL, 1))
    parts = {n: (o, l) for n, _w, _v, o, l in mt.fields(reply)}
    off, length = parts[meshapi.ADMIN_CHANNEL_RESPONSE]
    fields = {n: v for n, _w, v, _o, _l in mt.fields(reply[off:off + length])}
    check("channel role", fields[3], meshapi.CHANNEL_PRIMARY)
    check("reads not refused", session.api.refused, 0)

    # A write without the passkey is a replay as far as this node can tell.
    check("no passkey", admin(pb.message(
        meshapi.ADMIN_SET_OWNER, pb.string(2, "Nobody"))), None)
    check("no passkey refused", session.api.refused, 1)
    check("nothing stored", session.api.wrote, 0)

    def signed(body):
        return body + pb.blob(meshapi.ADMIN_SESSION_PASSKEY, passkey)

    # With it, the names are stored and read back changed.
    check("owner set", admin(signed(pb.message(
        meshapi.ADMIN_SET_OWNER,
        pb.string(2, "Muzi Duo") + pb.string(3, "duo")))), b"")
    check("names stored", nodeinfo.names(node.config.node_num),
          ("Muzi Duo", "duo"))
    check("write not refused", session.api.refused, 1)

    # An edit bracket. Every store erases the same flash page, so what is set
    # between begin and commit lands in one write rather than three.
    before = hoststub._mc.nvm.writes
    admin(signed(pb.boolean(meshapi.ADMIN_BEGIN_EDIT, True)))
    admin(signed(pb.message(meshapi.ADMIN_SET_CONFIG, pb.message(
        meshapi.CONFIG_LORA,
        pb.boolean(1, True) + pb.uint(2, meshapi.PROTO_PRESETS["LongSlow"])
        + pb.uint(7, meshapi.PROTO_REGIONS["EU_868"]) + pb.int32(10, 14)))))
    admin(signed(pb.message(meshapi.ADMIN_SET_CHANNEL, pb.message(
        2, pb.blob(2, mesh_config.DEFAULT_KEY) + pb.string(3, "muzi")))))
    check("held back", mesh_config.Config().region, "US")
    admin(signed(pb.boolean(meshapi.ADMIN_COMMIT_EDIT, True)))
    check("one page write", hoststub._mc.nvm.writes - before, 1)
    stored = mesh_config.Config()
    check("region stored", stored.region, "EU_868")
    check("preset stored", stored.preset, mt.preset_index("LongSlow"))
    check("power stored", stored.tx_power_dbm, 14)
    check("channel stored", stored.channel_name, "muzi")
    # What the node reports has to move with what was stored, or the app reads
    # back the value it replaced and retries forever.
    check("reported", node.config.region, "EU_868")
    # Read straight back: the radio is still running the old power until a
    # reset, so reporting what it is set to would read as a lost write.
    reply = admin(pb.uint(meshapi.ADMIN_GET_CONFIG,
                          meshapi.CONFIG_LORA - meshapi.CONFIG_TYPE_OFFSET))
    parts = {n: (o, l) for n, _w, _v, o, l in mt.fields(reply)}
    off, length = parts[meshapi.ADMIN_CONFIG_RESPONSE]
    body = bytes(reply[off:off + length])
    _n, _v, body = _wrapper(body)
    fields = {n: v for n, _w, v, _o, _l in mt.fields(body)}
    check("reads back region", fields[7], meshapi.PROTO_REGIONS["EU_868"])
    check("reads back power", fields[10], 14)

    # Writes this node cannot honestly carry out are refused rather than
    # acknowledged and dropped.
    refused = session.api.refused
    check("hand-tuned modem", admin(signed(pb.message(
        meshapi.ADMIN_SET_CONFIG, pb.message(
            meshapi.CONFIG_LORA, pb.boolean(1, False) + pb.uint(3, 250))))),
        None)
    # An omitted region is UNSET, which `region_index` accepts and no radio can
    # be tuned to, so it has to be caught before it reaches the store.
    check("region unset", admin(signed(pb.message(
        meshapi.ADMIN_SET_CONFIG, pb.message(
            meshapi.CONFIG_LORA, pb.boolean(1, True))))), None)
    check("second channel", admin(signed(pb.message(
        meshapi.ADMIN_SET_CHANNEL,
        pb.uint(1, 2) + pb.message(2, pb.string(3, "spare"))))), None)
    check("unencrypted channel", admin(signed(pb.message(
        meshapi.ADMIN_SET_CHANNEL, pb.message(2, pb.string(3, "open"))))),
        None)
    check("ham mode", admin(signed(pb.message(18, pb.string(1, "N0CALL")))),
          None)
    check("bad writes refused", session.api.refused - refused, 5)
    check("nothing more stored", mesh_config.Config().channel_name, "muzi")
    check("region kept", mesh_config.Config().region, "EU_868")
    check("admin stayed off the air", len(node.outbox), 1)

    # Commands that take effect by throwing something away. The node database
    # is emptied in place; the rest need a restart, which is armed rather than
    # done, so that the reply saying so is written before the board goes.
    check("nodes known", len(node.nodes), 1)
    admin(signed(pb.boolean(meshapi.ADMIN_NODEDB_RESET, True)))
    check("nodes cleared", len(node.nodes), 0)
    check("no restart for that", session.api.restart_at, None)

    admin(signed(pb.int32(meshapi.ADMIN_REBOOT, 5)))
    check("restart armed", session.api.restart_at is None, False)
    check("not yet", session.api.restart_due(), False)
    check("board still here", node.restarts, 0)
    # A negative delay calls it off, which is how the app cancels the countdown
    # it shows.
    admin(signed(pb.int32(meshapi.ADMIN_REBOOT, -1)))
    check("restart cancelled", session.api.restart_at, None)

    # A factory reset arms one of its own, because the settings this session is
    # running on have stopped existing.
    admin(signed(pb.int32(meshapi.ADMIN_FACTORY_RESET_CONFIG, 1)))
    check("settings erased", keystore.keystore_load(), {})
    check("reset restarts", session.api.restart_at is None, False)
    session.api.restart_at = 0
    session.pump()
    check("board restarted", node.restarts, 1)

    # Shutdown is refused: the metadata says this board cannot power itself
    # off, and agreeing would tell the app it is gone when it is not.
    refused = session.api.refused
    check("shutdown", admin(signed(pb.int32(meshapi.ADMIN_SHUTDOWN, 5))), None)
    check("shutdown refused", session.api.refused - refused, 1)

    # A client leaving takes its queue with it: what is in there was addressed
    # to it, and the next one has not asked for anything yet.
    hoststub._bleio.adapter.connections = ("client",)
    session.pump()
    hoststub._bleio.adapter.connections = ()
    session.api.config_id = 99
    session._offer(b"\x08\x01")
    session.pump()
    check("session reset", session.api.config_id, None)
    check("queue emptied", session._from_radio.read(), b"")
    meshble.ble_stop()


class _FakeGPS:
    """A GNSS reader as the node sees one: something to poll, and a fix to
    read afterwards."""

    class _Fix:
        def __init__(self, when):
            self.when = when

    def __init__(self, when=None):
        self.fix = self._Fix(when)
        self.polls = 0

    def poll(self):
        self.polls += 1


def nmea():
    """The GNSS parser, against the same sentences the Rust tests use.

    Two implementations of one format, checked against each other: the natmod
    on the board and the stub here. They were written from the sentences
    rather than from each other, so agreeing is evidence and not a tautology.
    """
    rmc_no_fix = b"$GNRMC,023018.901,V,,,,,,,170926,,,M,V*21"
    gga_fix = (b"$GNGGA,123519.000,1000.8952,N,08428.1706,W,1,13,0.80,"
               b"1373.4,M,-8.5,M,,*44")
    gga_estimated = (b"$GNGGA,123519.000,1000.8952,N,08428.1706,W,6,04,25.50,"
                     b"1373.4,M,-8.5,M,,*7F")
    rmc_fix = (b"$GNRMC,123519.000,A,1000.8952,N,08428.1706,W,0.02,0.00,"
               b"160926,,,A,V*1C")
    gpgsv = b"$GPGSV,3,1,11,01,05,123,20,03,45,200,30,06,70,045,35,11,20,300,22,1*64"
    glgsv = b"$GLGSV,2,1,07,65,30,100,25,66,55,210,31,1*7B"

    state = bytearray(_mt.NMEA_STATE)
    _mt.nmea_reset(state)
    check("cold", _mt.nmea_fix(state)[:6], (None,) * 6)

    # A clock with no position at all: what the board sees indoors, and the
    # reason it can set its RTC without ever having a fix.
    check("rmc seen", _mt.nmea_sentence(state, rmc_no_fix), _mt.NMEA_SAW_RMC)
    read = _mt.nmea_fix(state)
    check("gps time", read[3], 1789612218)
    check("no place", read[0], None)
    check("rmc says V", read[7], ord("V"))
    check("not valid", read[13], False)
    check("utc text", _mt.nmea_civil(read[3]), (2026, 9, 17, 2, 30, 18))

    # A stamp from before 2025 is the receiver's free-running counter, not a
    # satellite.
    early = bytearray(_mt.NMEA_STATE)
    _mt.nmea_reset(early)
    _mt.nmea_sentence(early, b"$GNRMC,023018.901,V,,,,,,,170906,,,M,V*23")
    check("pre-floor", _mt.nmea_fix(early)[3], None)

    # A corrupted line is refused rather than half-parsed.
    check("bad sum", _mt.nmea_sentence(state, rmc_no_fix[:-1] + b"2"), -1)
    check("not a line", _mt.nmea_sentence(state, b"GNRMC,1,2*21"), -1)

    _mt.nmea_sentence(state, gga_fix)
    read = _mt.nmea_fix(state)
    check("latitude", read[0], 100149200)
    check("longitude", read[1], -844695100)
    check("altitude", read[2], 1373400)
    check("used", read[4], 13)
    check("hdop", read[10], 80)
    # GGA alone is one opinion; the fix is only good once RMC agrees.
    check("gga alone", read[13], False)
    _mt.nmea_sentence(state, rmc_fix)
    check("both agree", _mt.nmea_fix(state)[13], True)

    # Quality 6 is the dead-reckoning bridge, which fills in a position the
    # receiver does not stand behind. The outdoor run produced one of these.
    _mt.nmea_sentence(state, gga_estimated)
    read = _mt.nmea_fix(state)
    check("estimated", read[6], 6)
    check("refused", read[13], False)
    check("no-fix hdop", read[10], 2550)

    # Each talker counts only its own sky.
    sky = bytearray(_mt.NMEA_STATE)
    _mt.nmea_reset(sky)
    _mt.nmea_sentence(sky, gpgsv)
    check("one talker", _mt.nmea_fix(sky)[5], 11)
    _mt.nmea_sentence(sky, glgsv)
    check("two talkers", _mt.nmea_fix(sky)[5], 18)
    _mt.nmea_sentence(sky, gpgsv)
    check("no double", _mt.nmea_fix(sky)[5], 18)
    check("talker 0", _mt.nmea_talker(sky, 0), (b"GP", 11))
    check("talker 1", _mt.nmea_talker(sky, 1), (b"GL", 7))
    check("talker 2", _mt.nmea_talker(sky, 2), None)

    # The UART hands over whatever has arrived, which cuts sentences in half.
    split = bytearray(_mt.NMEA_STATE)
    _mt.nmea_reset(split)
    echo = bytearray(256)
    got, used = _mt.nmea_feed(split, rmc_no_fix[:20], echo, 0)
    check("half a line", (got, used), (0, 0))
    got, used = _mt.nmea_feed(split, rmc_no_fix[20:] + b"\r\n", echo, used)
    check("rejoined", got, 1)
    check("echoed", bytes(echo[:used - 1]), rmc_no_fix)
    check("fed time", _mt.nmea_fix(split)[3], 1789612218)

    # Noise between sentences is counted, not spliced onto the next one.
    got, _ = _mt.nmea_feed(split, b"not a sentence\r\n" + gga_fix + b"\r\n",
                           None, 0)
    read = _mt.nmea_fix(split)
    check("landed", got, 1)
    check("counted good", read[11], 2)
    check("counted bad", read[12], 1)
    check("fed place", read[0], 100149200)



def stream():
    """The stream framing, against the vectors the app's own codec is tested on.

    Same arrangement as `nmea`: the Rust in the natmod and the Python in
    hoststub were written from the protocol separately, so what is checked
    here is that two readings of it agree.
    """
    state = bytearray(_mt.STREAM_STATE)
    _mt.stream_reset(state)

    def drain(chunk):
        """Every frame in one write, as a list of bytes."""
        out = []
        at = 0
        while True:
            length, at = _mt.stream_feed(state, chunk, at)
            if not length:
                return out
            out.append(bytes(state[_mt.STREAM_BODY:_mt.STREAM_BODY + length]))

    hello = b"\x94\xc3\x00\x05hello"
    check("whole", drain(hello), [b"hello"])

    # Split at every byte: the reassembler has to survive a socket handing it
    # one byte at a time, which is what a slow link during the config dump
    # looks like from here.
    ragged = True
    for cut in range(1, len(hello)):
        _mt.stream_reset(state)
        first = drain(hello[:cut])
        second = drain(hello[cut:])
        if first + second != [b"hello"]:
            ragged = False
    check("split", ragged, True)

    _mt.stream_reset(state)
    check("coalesced", drain(hello + b"\x94\xc3\x00\x02hi"), [b"hello", b"hi"])

    # A board that prints over the same link is the reason resynchronisation
    # exists at all.
    _mt.stream_reset(state)
    check("debug text", drain(b"booting\n" + hello), [b"hello"])
    check("skipped", _mt.stream_counts(state)[0], 8)

    # The Android codec's own test vector: 0x94 0x00 is not a header.
    _mt.stream_reset(state)
    check("bad start2", drain(b"\x94\x00\x94\xc3\x00\x01\x55"), [b"\x55"])
    check("skipped 2", _mt.stream_counts(state)[0], 2)

    # A repeated start byte is still a candidate header, not a restart.
    _mt.stream_reset(state)
    check("doubled 94", drain(b"\x94\x94\xc3\x00\x01\x55"), [b"\x55"])
    check("skipped 1", _mt.stream_counts(state)[0], 1)

    # 513 is past MAX_TO_FROM_RADIO_SIZE, so those four bytes were noise.
    _mt.stream_reset(state)
    check("oversize", drain(b"\x94\xc3\x02\x01" + hello), [b"hello"])
    check("skipped 4", _mt.stream_counts(state)[0], 4)

    _mt.stream_reset(state)
    biggest = bytes(range(256)) * 2
    got = drain(b"\x94\xc3\x02\x00" + biggest)
    check("largest", [len(f) for f in got] + [got == [biggest]], [512, True])

    # An empty frame is not a frame, and must not stop the one behind it.
    _mt.stream_reset(state)
    check("empty", drain(b"\x94\xc3\x00\x00" + hello), [b"hello"])

    _mt.stream_reset(state)
    check("half", drain(hello[:6]), [])
    check("partial", _mt.stream_counts(state)[2], 2)
    check("counted", _mt.stream_counts(state)[1], 0)

    # What we send has to be what we can read back.
    out = bytearray(600)
    _mt.stream_reset(state)
    size = _mt.stream_frame(out, 0, b"round trip")
    check("framed", size, 14)
    check("round trip", drain(memoryview(out)[:size]), [b"round trip"])

    try:
        _mt.stream_frame(bytearray(8), 0, b"too long for this")
        check("no space", "accepted", "ValueError")
    except ValueError:
        check("no space", "refused", "refused")


def position():
    """Encoding a position, checked by decoding it again.

    The decoder was written from the schema and the encoder from the same
    schema separately, so a round trip agreeing is two readings of the field
    numbers agreeing rather than one reading checked against itself.
    """
    gga_fix = (b"$GNGGA,123519.000,1000.8952,N,08428.1706,W,1,13,0.80,"
               b"1373.4,M,-8.5,M,,*44")
    rmc_fix = (b"$GNRMC,123519.000,A,1000.8952,N,08428.1706,W,0.02,0.00,"
               b"160926,,,A,V*1C")
    state = bytearray(_mt.NMEA_STATE)
    _mt.nmea_reset(state)

    # Nothing to send before the receiver stands behind a position.
    _mt.nmea_sentence(state, gga_fix)
    out = bytearray(64)
    try:
        _mt.position_encode(out, state, 32)
        check("refuses unconfirmed", "sent", "refused")
    except ValueError:
        check("refuses unconfirmed", "refused", "refused")

    _mt.nmea_sentence(state, rmc_fix)
    used = _mt.position_encode(out, state, 32)
    got = _mt.payload_position(bytes(out[:used]))
    check("latitude", got[0], 100149200)
    check("longitude", got[1], -844695100)
    check("altitude m", got[2], 1373)
    check("sats", got[3], 13)
    check("precision", got[4], 32)

    # Coarse, and centred in the square it admits to rather than at a corner.
    used = _mt.position_encode(out, state, 13)
    coarse = _mt.payload_position(bytes(out[:used]))
    mask = (0xFFFFFFFF << 19) & 0xFFFFFFFF
    half = 1 << 18
    check("coarse lat", coarse[0], (100149200 & mask) + half)
    check("coarse precision", coarse[4], 13)

    # Withheld entirely, while still reporting that there is a fix.
    used = _mt.position_encode(out, state, 0)
    hidden = _mt.payload_position(bytes(out[:used]))
    check("no coordinates", hidden[:2], (None, None))
    check("still counted", hidden[3], 13)

    small = bytearray(6)
    try:
        _mt.position_encode(small, state, 32)
        check("refuses small buffer", "wrote", "refused")
    except ValueError:
        check("refuses small buffer", "refused", "refused")


def clocks():
    """Which source is allowed to set the clock, on the real `Node`."""
    # Built without its constructor, which opens a radio and claims the pins
    # of the battery gauge. None of that is involved in ranking a timestamp.
    node = meshnode.Node.__new__(meshnode.Node)
    node.clock_set = False
    node.clock_rank = meshnode.CLOCK_NONE
    node.attached = False
    node.gps = None
    node._next_gps = None

    packet = _OneField()
    packet.from_ = 0x4F80DA67
    payload = _OneField()
    payload.portnum = 67
    payload.time = 1789600000

    node._seed_clock(packet, payload)
    check("mesh seeds", node.clock_rank, meshnode.CLOCK_MESH)
    check("mesh is weak", node.clock_from_phone, False)

    check("phone beats mesh", node.set_clock(1789600100), True)
    check("phone ranked", node.clock_rank, meshnode.CLOCK_PHONE)

    payload.time = 1789600200
    node._seed_clock(packet, payload)
    check("mesh locked out", hoststub._RTC.last, time.localtime(1789600100))

    check("gps beats phone",
          node.set_clock(1789600300, meshnode.CLOCK_GPS), True)
    check("gps ranked", node.clock_rank, meshnode.CLOCK_GPS)
    check("phone refused", node.set_clock(1789600400), False)
    check("gps kept", hoststub._RTC.last, time.localtime(1789600300))

    # A board whose GNSS has time but no fix yet still sets the clock: the
    # receiver reads the date off one satellite long before it can place itself.
    fresh = meshnode.Node.__new__(meshnode.Node)
    fresh.clock_set = False
    fresh.clock_rank = meshnode.CLOCK_NONE
    fresh.attached = False
    fresh._next_gps = None
    fresh.gps = _FakeGPS(1789601000)
    fresh._service_gps()
    check("gps sets", fresh.clock_rank, meshnode.CLOCK_GPS)
    check("gps polled", fresh.gps.polls, 1)

    fresh.gps.fix.when = 1789601001
    fresh._service_gps()
    check("no resync yet", hoststub._RTC.last, time.localtime(1789601000))
    fresh._next_gps = 0
    fresh._service_gps()
    check("resync quietly", hoststub._RTC.last, time.localtime(1789601001))

    blind = meshnode.Node.__new__(meshnode.Node)
    blind.clock_rank = meshnode.CLOCK_NONE
    blind.clock_set = False
    blind._next_gps = None
    blind.gps = _FakeGPS(None)
    blind._service_gps()
    check("no fix no clock", blind.clock_set, False)

    check("no gnss named", meshnode.open_gps(), None)
    check("gnss missing", meshnode.open_gps("nosuchmodule"), None)

    # MESH_GPS names a submodule of the meshtastic package on the W12, and
    # __import__ hands back the package for a dotted name, so the walk that
    # gets from one to the other is worth pinning.
    package = type(sys)("fakepkg")
    package.__path__ = []
    package.sub = type(sys)("fakepkg.sub")
    package.sub.Reader = lambda: "reader"
    sys.modules["fakepkg"] = package
    sys.modules["fakepkg.sub"] = package.sub
    check("gnss dotted", meshnode.open_gps("fakepkg.sub"), "reader")
    check("gnss dotted missing", meshnode.open_gps("fakepkg.nope"), None)


frames()
print()
budget()
print()
store()
print()
stored_settings()
print()
identity()
print()
answers()
print()
neighbours()
print()
messages()
print()
cache()
print()
strictness()
print()
bluetooth()
print()
wire()
print()
phone()
print()
nmea()
print()
stream()
print()
position()
print()
clocks()

print()
if FAILED:
    print("FAILED: %s" % ", ".join(FAILED))
    sys.exit(1)
print("host tests pass")
