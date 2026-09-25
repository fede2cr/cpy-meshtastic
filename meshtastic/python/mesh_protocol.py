"""The Meshtastic protocol: tuning the radio, and reading what it hears.

Two halves that share a wire format. The first turns Meshtastic's way of
describing a radio -- a region and a modem preset -- into a frequency in hertz,
a spreading factor and a coding rate. The second takes a received frame apart:
header, then decryption, then the protobuf inside.

They are one module because they are one thing to get right, and because a
frame's header is what the decryptor needs to build its counter block. The
arithmetic and the protobuf parser live in `meshtastic`, the native module; what
is here is naming, validation, and every payload shape, which is Python because
adding one should cost a few lines and no rebuild. As the migration proceeds
this file shrinks and that one grows.

Nothing here names a radio part. `configure` speaks the vocabulary in
`meshradio`, stated in hertz and dBm, and an adapter turns that into whatever
register codes its chip wants. An LR1121 is one of several radios a
CircuitPython board might carry, and Meshtastic runs over SX126x and SX127x
parts as well.

Everything is transcribed from Meshtastic firmware v2.7.26.54e0d8d. The wire
format is not ours to choose: these values have to match whatever the rest of
the mesh is running, so the version is pinned and written down rather than
tracked.

Transmitting lives in mesh_tx.py, which is still source rather than bytecode
because it has not yet been proven on air.
"""

import struct
import time

import aesio
import meshtastic as _mt

#: The firmware release the tables were transcribed from.
PINNED_FIRMWARE = "v2.7.26.54e0d8d"

# ------------------------------------------------------------------ presets

# These indices must match src/preset.rs. Nothing on the wire carries a preset
# number -- both ends are configured out of band -- so the numbering is ours,
# but the two halves still have to agree with each other.
SHORT_TURBO = 0
SHORT_FAST = 1
SHORT_SLOW = 2
MEDIUM_FAST = 3
MEDIUM_SLOW = 4
LONG_TURBO = 5
LONG_MODERATE = 6
LONG_SLOW = 7
LONG_FAST = 8

#: Display names, which double as the default channel name. An unnamed primary
#: channel takes its preset's name, and that name is what gets hashed to pick
#: the frequency slot, so these strings are load-bearing and not decoration.
PRESET_NAMES = (
    "ShortTurbo",
    "ShortFast",
    "ShortSlow",
    "MediumFast",
    "MediumSlow",
    "LongTurbo",
    "LongModerate",
    "LongSlow",
    "LongFast",
)

# ------------------------------------------------------------------ regions

# Must match src/region.rs, in the same order.
REGION_NAMES = (
    "UNSET",
    "US",
    "EU_433",
    "EU_868",
    "CN",
    "JP",
    "ANZ",
    "ANZ_433",
    "KR",
    "TW",
    "RU",
    "IN",
    "NZ_865",
    "TH",
    "UA_433",
    "UA_868",
    "MY_433",
    "MY_919",
    "SG_923",
    "PH_433",
    "PH_868",
    "PH_915",
    "KZ_433",
    "KZ_863",
    "NP_865",
    "BR_902",
    "LORA_24",
)

UNSET = 0
US = 1
EU_433 = 2
EU_868 = 3
LORA_24 = 26


def region_index(name):
    """Looks a region up by name, so scripts can say "US" and not 1."""
    try:
        return REGION_NAMES.index(name)
    except ValueError:
        raise ValueError("unknown region %r" % (name,))


def preset_index(name):
    """Looks a modem preset up by name, matching the spelling the apps use."""
    try:
        return PRESET_NAMES.index(name)
    except ValueError:
        raise ValueError("unknown preset %r" % (name,))


# ------------------------------------------------------------- radio config

#: Meshtastic's LoRa sync word. This single byte decides whether the radio hears
#: the mesh at all: a wrong value looks exactly like an empty band, with no
#: error anywhere, so if a capture stays silent this is the first thing to
#: doubt. 0x12 is the LoRaWAN private default and is *not* what Meshtastic uses.
SYNC_WORD = 0x2B

#: Meshtastic uses a 16-symbol preamble rather than the usual 8, to give
#: receivers more sleep time between wakes. A receiver with the default 8 will
#: still hear it, but matching is free.
PREAMBLE_LENGTH = 16


class Settings:
    """A resolved region + preset: everything needed to tune the radio."""

    def __init__(self, region=US, preset=LONG_FAST, channel_name=None,
                 channel_num=0):
        """`channel_name` defaults to the preset's name, as an unnamed primary
        channel does. `channel_num` is one-based; 0 means hash the name.
        """
        self.region = region
        self.region_name = REGION_NAMES[region]
        self.preset = preset
        self.preset_name = PRESET_NAMES[preset]
        self.channel_name = (
            self.preset_name if channel_name is None else channel_name
        )
        self.channel_num = channel_num

        info = _mt.region_info(region)
        self.band_start_hz = info[0]
        self.band_end_hz = info[1]
        self.duty_cycle_pct = info[3]
        self.power_limit_dbm = info[4]
        self.wide_lora = info[5]

        self.sf, self.bw_hz, self.cr = _mt.preset_params(preset, self.wide_lora)
        self.num_channels = _mt.num_channels(region, self.bw_hz)
        name_bytes = self.channel_name.encode("utf-8")
        self.frequency_hz = _mt.channel_frequency(
            region, self.bw_hz, channel_num, name_bytes
        )
        # One-based, to match how Meshtastic presents channel numbers. An
        # explicit channel number past the end wraps, exactly as the firmware
        # does, so this is not simply channel_num.
        if channel_num:
            self.slot = (channel_num - 1) % self.num_channels + 1
        else:
            self.slot = _mt.djb2(name_bytes) % self.num_channels + 1

    def __str__(self):
        return "%s/%s ch %d/%d @ %.4f MHz SF%d BW%.2fkHz CR4/%d" % (
            self.region_name,
            self.preset_name,
            self.slot,
            self.num_channels,
            self.frequency_hz / 1e6,
            self.sf,
            self.bw_hz / 1000.0,
            self.cr,
        )


def protocol_configure(radio, settings, *, power_dbm=None, rx_boosted=True):
    """Applies `settings` to an opened radio.

    `radio` is anything with the `meshradio.Radio` vocabulary, which is stated
    in hertz and dBm rather than in any one part's register codes. That is what
    keeps this module about Meshtastic instead of about a chip.

    `power_dbm` of None means as much as is allowed and possible, which are two
    different limits: the region caps what is legal and the amplifier caps what
    the part can produce. US permits 30 dBm and no LR1121 reaches it, so the
    smaller of the two is the only sensible reading of "maximum".
    """
    if settings.region == UNSET:
        raise ValueError("region UNSET is a placeholder, not a band; pick one")
    if power_dbm is None:
        power_dbm = min(settings.power_limit_dbm,
                        radio.power_ceiling(settings.frequency_hz))
    elif power_dbm > settings.power_limit_dbm:
        raise ValueError(
            "%+d dBm is above the %s limit of %+d dBm"
            % (power_dbm, settings.region_name, settings.power_limit_dbm)
        )

    radio.tune(
        frequency_hz=settings.frequency_hz,
        sf=settings.sf,
        bw_hz=settings.bw_hz,
        cr=settings.cr,
        preamble=PREAMBLE_LENGTH,
        sync_word=SYNC_WORD,
        power_dbm=power_dbm,
        rx_boosted=rx_boosted,
    )


def self_check():
    """Confirms the Python tables still line up with the Rust ones.

    The region and preset indices are duplicated between this module and
    src/region.rs, and a drift between them would not raise anything -- it would
    just tune the radio to a plausible wrong frequency and capture nothing. US
    LongFast on 906.875 MHz is externally known, so checking it here catches
    that. EU_868 is checked too because it is exactly one channel wide and so
    exercises a different corner of the arithmetic.
    """
    for region, preset, expected in (
        ("US", LONG_FAST, 906_875_000),
        ("EU_868", LONG_FAST, 869_525_000),
    ):
        got = Settings(region_index(region), preset).frequency_hz
        if got != expected:
            raise RuntimeError(
                "%s/%s should be %d Hz but the tables say %d; meshtastic.py "
                "and src/region.rs have drifted apart"
                % (region, PRESET_NAMES[preset], expected, got)
            )


# ------------------------------------------------------------------ packets

HEADER_LEN = 16
BROADCAST = 0xFFFFFFFF


class Packet:
    """A received frame with its header decoded and its payload left alone."""

    def __init__(self, frame, rssi=None, snr=None, signal_rssi=None):
        (
            self.to, self.from_, self.id, self.flags, self.channel,
            self.next_hop, self.relay_node, self.hop_limit, self.hop_start,
            self.want_ack, self.via_mqtt, self.hops_away,
        ) = _mt.parse_header(frame)
        self.payload = frame[HEADER_LEN:]
        self.rssi = rssi
        self.snr = snr
        self.signal_rssi = signal_rssi

    def describe(self, payload=None):
        """The firmware's own `printPacket` line, so logs read the same.

        Field names, order and formatting are copied from `RadioInterface.cpp`
        rather than chosen. Fields a sniffer cannot know (transport, rxtime,
        priority) are omitted; `signalRSSI` is the one addition, and is named
        unlike anything in the firmware so it cannot be mistaken for one.
        """
        out = "(id=0x%08x fr=0x%08x to=0x%08x, WantAck=%d, HopLim=%d Ch=0x%x" % (
            self.id, self.from_, self.to, self.want_ack,
            self.hop_limit, self.channel,
        )
        if payload is None:
            # Firmware counts the header in this length; matching it keeps the
            # numbers comparable between the two logs.
            out += " encrypted len=%d" % (len(self.payload) + HEADER_LEN)
        else:
            out += " Portnum=%d" % payload.portnum
            if payload.want_response:
                out += " WANTRESP"
            if payload.source:
                out += " source=%08x" % payload.source
            if payload.dest:
                out += " dest=%08x" % payload.dest
            if payload.request_id:
                out += " requestId=%x" % payload.request_id
        if self.snr is not None:
            out += " rxSNR=%g" % self.snr
        if self.rssi is not None:
            out += " rxRSSI=%d" % round(self.rssi)
        if self.signal_rssi is not None:
            out += " signalRSSI=%d" % round(self.signal_rssi)
        if self.via_mqtt:
            out += " via MQTT"
        # Zero means "not set" for all three: a pre-2.3 sender leaves hop_start
        # at zero, which also invalidates next_hop and relay_node.
        if self.hop_start:
            out += " hopStart=%d" % self.hop_start
        if self.next_hop:
            out += " nextHop=0x%x" % self.next_hop
        if self.relay_node:
            out += " relay=0x%x" % self.relay_node
        return out + ")"


# --------------------------------------------------------------- encryption

#: Reused across packets. `aesio.AES` copies the counter block at construction,
#: and the eight zero bytes are written once here and never again, so the only
#: work per packet is the eight that change.
_NONCE = bytearray(16)


def nonce(frame):
    """The AES-CTR initial counter block for a frame.

    Packet id as a little-endian u64, then the sender as a little-endian u32,
    then four bytes of extra nonce that only public-key packets use. Both
    numbers are already little-endian in the header, so this is slicing rather
    than any byte-swapping. Only bytes 4..12 are read, so a bare 16-byte header
    works as well as a whole frame -- which is what the transmit path has.

    The returned buffer is shared; use it before calling this again.
    """
    for i in range(4):
        _NONCE[i] = frame[8 + i]
        _NONCE[i + 8] = frame[4 + i]
    return _NONCE


def crypt(header, body, key):
    """Applies the keystream to a frame body.

    CTR is its own inverse, so this is both the encryptor and the decryptor.
    That is not a shortcut but the reason the header may not be rewritten in
    flight: the counter is derived from the packet id and sender, so a relay
    that touched either would make the payload unreadable to everyone else.

    Returns the bytearray it worked in rather than a bytes copy of it. Nothing
    else references that buffer, and with the phone connected there are only a
    few kilobytes free -- a spare copy of every packet in each direction is
    both the allocation that fails and the churn that fragments the heap.
    """
    if not key:
        return bytes(body)
    out = bytearray(len(body))
    aesio.AES(key, aesio.MODE_CTR, nonce(header)).encrypt_into(body, out)
    return out


def decrypt(frame, key):
    """Returns the frame's payload with the keystream removed."""
    return crypt(frame[:HEADER_LEN], frame[HEADER_LEN:], key)


# ------------------------------------------------------------------ payloads

# Decryption cannot fail: AES-CTR is a keystream XOR, so a wrong key produces
# the right number of wrong bytes rather than an error. Decoding is where a
# wrong key shows up, because random bytes are almost never valid protobuf that
# ends exactly on the buffer boundary. Nothing in a Meshtastic frame
# authenticates the ciphertext, so "did the protobuf parse" is the only key
# check available -- and it is the reason the parser in the Rust crate refuses
# to skip malformed fields the way protobuf normally would.

#: Nodes with no clock send 0, and CircuitPython's time.localtime() refuses
#: anything earlier than 2000 outright, so both are screened out together.
MIN_EPOCH = 946684800

#: Portnum -> the field number carrying a unix epoch, for the payload types
#: that timestamp themselves. Both are fixed32.
_TIME_FIELD = {3: 4, 67: 1}

#: Portnums are an enum in the protobufs, but only the ones worth naming in a
#: capture are listed. An unlisted number still decodes; it just prints as a
#: number, which is a better outcome than pretending it is unknown.
PORTNUM_NAMES = {
    0: "unknown",
    1: "text",
    2: "remote-hardware",
    3: "position",
    4: "nodeinfo",
    5: "routing",
    6: "admin",
    7: "text-compressed",
    8: "waypoint",
    9: "audio",
    10: "detection-sensor",
    11: "alert",
    32: "reply",
    33: "ip-tunnel",
    34: "paxcounter",
    64: "serial",
    65: "store-forward",
    66: "range-test",
    67: "telemetry",
    68: "zps",
    69: "simulator",
    70: "traceroute",
    71: "neighborinfo",
    72: "atak-plugin",
    73: "map-report",
    74: "powerstress",
    256: "private",
    257: "atak-forwarder",
}

#: Portnums this code sends on, by name rather than number at the call site.
PORT_TEXT_MESSAGE = 1

#: The enum is 32 bits wide but nothing above this is allocated, so a larger
#: value means the plaintext is not plaintext.
_MAX_PORTNUM = 511


#: This board's own HardwareModel, for the nodeinfo it will eventually send.
HW_MUZI_BASE = 93

#: What the enum offers for hardware it does not list. The W12 has no allocated
#: model, and claiming a neighbour's is worse than admitting to none.
HW_PRIVATE = 255

#: The model this board announces. Replaced at `start()` by the radio adapter's,
#: which is the only module that knows which board it is running on; the default
#: is the nRF board's so a node that never starts still describes itself.
HW_MODEL = HW_MUZI_BASE

#: Node number -> short name, learned from every nodeinfo that decodes. Names
#: reach the air only in that one payload type and only every few hours, so
#: without this most lines can never show anything but a number.
NAMES = {}

#: Learning stops here rather than evicting. A capture that has already met 64
#: nodes is better served by keeping the ones it knows than by churning, and an
#: uncapped dict on a 256 KB part is a leak with extra steps.
_MAX_NAMES = 64

WIRE_VARINT = 0
WIRE_I64 = 1
WIRE_LEN = 2
WIRE_I32 = 5


def fields(buf):
    """Walks a protobuf message, yielding (number, wire, value, offset, length).

    `value` carries varints and the raw bits of fixed32/fixed64; `offset` and
    `length` locate the body of a length-delimited field within `buf`.
    """
    at = 0
    end = len(buf)
    while at < end:
        number, wire, lo, hi, off, length, nxt = _mt.proto_field(buf, at)
        yield number, wire, lo | (hi << 32), off, length
        at = nxt


def stamp(epoch):
    """Formats a unix epoch as UTC, or returns None if it is not a real time."""
    if not epoch or epoch < MIN_EPOCH:
        return None
    t = time.localtime(epoch)
    # ISO 8601 rather than a space, so the value survives being split on spaces.
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (
        t[0], t[1], t[2], t[3], t[4], t[5])


def _quoted(text):
    """Quotes a user-supplied string so it stays one field on one line.

    Not `repr`, which is free to escape non-ASCII and would turn the emoji that
    are all over these names into hex. Only the four characters that could end
    the value early are touched.
    """
    if text is None:
        return None
    for bad, good in (("\\", "\\\\"), ('"', '\\"'),
                      ("\n", "\\n"), ("\r", "\\r")):
        text = text.replace(bad, good)
    return '"%s"' % text


def _sender_time(portnum, body):
    want = _TIME_FIELD.get(portnum)
    if want is None:
        return None
    at = _mt.payload_sender_time(body, want)
    # Rust reads the field; whether the value is a believable date is a
    # judgement and stays here.
    return at if at is not None and at >= MIN_EPOCH else None


def _learn(node, body):
    if node in NAMES or len(NAMES) >= _MAX_NAMES:
        return
    # Short name only: it is what the firmware itself prints, and four bytes per
    # node is what makes keeping every one of them affordable.
    where = _mt.payload_short_name(body)
    if where is None:
        return
    off, length = where
    try:
        NAMES[node] = body[off:off + length].decode()
    except UnicodeError:
        pass


#: The meshtext module once loaded, False once it is known to be unavailable.
_text_layer = None

#: Whether a payload may be rendered as text, which means loading meshtext.mpy
#: and keeping it for the session. Off, because the node that runs from code.py
#: is serving the phone app, and the app renders every packet itself -- the five
#: kilobytes would go on describing packets to a console nobody is reading.
#: `listen()` turns it on, which is the prompt asking to be shown them.
WANT_TEXT = False


class Payload:
    """A decrypted, decoded payload."""

    def __init__(self, portnum, body, want_response, dest, source,
                 request_id, reply_id, bitfield):
        self.portnum = portnum
        self.body = body
        self.want_response = want_response
        self.dest = dest
        self.source = source
        self.request_id = request_id
        self.reply_id = reply_id
        self.bitfield = bitfield
        self.time = _sender_time(portnum, body)

    @property
    def name(self):
        return PORTNUM_NAMES.get(self.portnum, "%d" % self.portnum)

    def describe(self):
        """One line of text, if it was asked for and meshtext.mpy is there.

        Resolved once and remembered either way. Retrying per packet would
        mean asking a heap that is already too full for 5 kB to find it again
        every few seconds, from the deepest point of the receive path.
        """
        global _text_layer
        if not WANT_TEXT:
            return "port=%s bytes=%d" % (self.name, len(self.body))
        if _text_layer is None:
            try:
                from meshtastic import meshtext
                _text_layer = meshtext
            except (ImportError, MemoryError):
                _text_layer = False
        if _text_layer is False:
            return "port=%s bytes=%d" % (self.name, len(self.body))
        return _text_layer.describe(self)


def decode(frame, key):
    """Decrypts and decodes a frame's payload, or returns None.

    None means the key does not fit this packet: either it was for a different
    channel, or the packet is public-key encrypted to someone else. There is no
    way to tell those apart, and no way to be certain the key *did* fit -- only
    that the plaintext was structurally valid protobuf, which is enough in
    practice and is all the format offers.
    """
    if not key:
        return None
    plain = decrypt(frame, key)
    try:
        (portnum, off, length, dest, source, request_id, reply_id, _emoji,
         want_response, bitfield) = _mt.parse_data(plain)
    except ValueError:
        return None
    if portnum > _MAX_PORTNUM:
        return None
    body = plain[off:off + length]
    if portnum == 4:
        # The header's sender, not the User.id inside, so a name learned here
        # matches the number every other line is keyed by.
        _learn(struct.unpack("<I", frame[4:8])[0], body)
    return Payload(
        portnum, body, want_response, dest, source,
        request_id, reply_id, bitfield,
    )
