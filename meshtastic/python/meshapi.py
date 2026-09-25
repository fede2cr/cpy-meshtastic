"""Phase 8: the device API the Meshtastic phone app speaks.

The transport is a mailbox rather than a stream, and `meshble` owns it. This is
the part that has the opinions: what a client is told when it connects, and what
it is allowed to ask for once it has.

The shape is fixed by the app. On connect it writes `ToRadio.want_config_id`
with a number of its choosing, and expects the device to stream back the whole
of itself as `FromRadio` messages -- who this node is, what its channels and
settings are, everyone it has heard -- and then echo the number back as
`config_complete_id` to say that was all of it. Anything after that is live
traffic, in both directions. A client that never sees the terminator sits
waiting, so the dump is queued in full before the first read is answered rather
than generated as the reads arrive.

**Administration is accepted from the connected client and from nowhere else.**
The app's other half is `AdminMessage`: reading and writing config, renaming the
node, editing the channel. All of it arrives as a packet the client addresses to
this node itself, and all of it is handled here in `PhoneAPI`, on the near side
of the BLE link. `meshnode` does not look at port 6 off the air and must not
start: a broadcast that can retune the radio or replace the channel key is not
something to accept from a stranger, and the app never sends one -- remote
administration goes over the PKI channel, which this node has no key for.

So the trust boundary is the bonded link, and the session passkey is the replay
guard on top of it. Every answer to a `get_x_request` carries a freshly minted
key; every `set_x` has to quote it back, and it expires. That is the firmware's
own scheme and the app already implements its half: it records whatever passkey
arrives and attaches it to everything it sends afterwards. It also *discards an
empty one*, which is why a read cannot simply answer without one -- the app
would have the answer, no session, and would sit waiting on the config screen.

What can be written is what this node can honestly store: its two names, the
LoRa section of config, and the primary channel. The rest of `AdminMessage` is
refused rather than acknowledged, because a node that says yes to `set_ham_mode`
and then does nothing is worse than one that says no. Refusals are still
answered, with a `QueueStatus` carrying `NOT_AUTHORIZED`: the app holds its send
queue open until every packet it handed over has been accounted for, so a
request dropped in silence does not read as "no", it reads as a link that has
stopped working.

Writes reach flash, not the radio. NVM is a single page that is erased and
rewritten in full every time, and a config screen sets a dozen fields, so
`begin_edit_settings` and `commit_edit_settings` are honoured for what they are
worth here: everything between them is collected and stored once. The radio
itself keeps the settings it was started with until a reset, while what is
*reported* changes immediately -- otherwise the app reads back the value it just
replaced and takes the write for lost.

Two other self-addressed requests are answered rather than refused. A
`Telemetry` request asks what this node's own uptime and airtime are, which is a
question about a client, not a command to one. And `AdminMessage.set_time_only`
needs no passkey and no session, because it changes nothing that is stored --
which, on a board with no RTC, no GPS and no network, is worth more from a phone
than from anything else it will ever hear.

The config sent is honest about the rest. Every `Config` and `ModuleConfig`
variant is sent, and all but LoRa are empty, which in proto3 says "this section
exists and is entirely default" rather than "this section is missing". They are
default because nothing here reads them.

Three enums have to be translated rather than passed through. `meshtastic.py`
numbers regions and presets in its own order -- deliberately, since neither
number ever goes on the air -- so both tables here are keyed by name. And
`AdminMessage.ConfigType` counts the same sections as `Config`'s own oneof but
starts at zero where the oneof starts at one. Getting any of the three wrong
would tell the phone a plausible lie about which band this node is on, or hand
back the wrong section entirely, which is exactly the kind of error that never
surfaces as an error.
"""

import struct
import time

from meshtastic import meshlib as mesh_config
from meshtastic import meshlib as pb
from meshtastic import meshlib as mt
from meshtastic import meshlib as nodedb
from meshtastic import meshlib as nodeinfo

# ---------------------------------------------------------- message numbers

# FromRadio, from mesh.proto. Field 1 is the id, which nothing here sets.
FROM_PACKET = 2
FROM_MY_INFO = 3
FROM_NODE_INFO = 4
FROM_CONFIG = 5
FROM_COMPLETE_ID = 7
FROM_MODULE_CONFIG = 9
FROM_CHANNEL = 10
FROM_QUEUE_STATUS = 11
FROM_METADATA = 13

# ToRadio. 5 and 6 are xmodem and the MQTT proxy, neither of which apply.
TO_PACKET = 1
TO_WANT_CONFIG_ID = 3
TO_DISCONNECT = 4
TO_HEARTBEAT = 7

#: The two `want_config_id` values the app uses to ask for one half of the dump
#: at a time. Agreed constants rather than nonces, and answering either with the
#: other half's messages is what keeps a client from ever finishing.
CONFIG_NONCE = 69420
NODE_INFO_NONCE = 69421

#: Config's oneof, in order. Every one of them is sent.
CONFIG_DEVICE = 1
CONFIG_LORA = 6
CONFIG_SECURITY = 8

#: ModuleConfig's oneof runs 1..13 and all of them are sent empty.
MODULE_CONFIGS = 13

#: How far Config's oneof runs. Two further than the dump sends: session key
#: and device UI exist to be asked for, and are as default as the rest.
CONFIG_SECTIONS = 10

#: `AdminMessage.ConfigType` and `ModuleConfigType` name the same sections as
#: the two oneofs above but count from zero, so every section has two numbers
#: one apart. A read that forgets this returns the wrong section and no error.
CONFIG_TYPE_OFFSET = 1

#: The two `PortNum` values a client sends to this node itself and gets an
#: answer to.
PORT_ADMIN = 6
PORT_TELEMETRY = 67

#: `Telemetry`'s oneof. The app asks for a kind by sending that variant empty,
#: and the answer comes back in the same field.
TELEMETRY_DEVICE = 2
TELEMETRY_LOCAL_STATS = 6

#: `AdminMessage`'s oneof: the gets, the sets, and the edit bracket. Each get
#: and its response are two apart, but only by coincidence, so both are named.
ADMIN_GET_CHANNEL = 1
ADMIN_CHANNEL_RESPONSE = 2
ADMIN_GET_OWNER = 3
ADMIN_OWNER_RESPONSE = 4
ADMIN_GET_CONFIG = 5
ADMIN_CONFIG_RESPONSE = 6
ADMIN_GET_MODULE_CONFIG = 7
ADMIN_MODULE_CONFIG_RESPONSE = 8
ADMIN_GET_METADATA = 12
ADMIN_METADATA_RESPONSE = 13
ADMIN_SET_OWNER = 32
ADMIN_SET_CHANNEL = 33
ADMIN_SET_CONFIG = 34
ADMIN_SET_TIME_ONLY = 43
ADMIN_BEGIN_EDIT = 64
ADMIN_COMMIT_EDIT = 65
ADMIN_FACTORY_RESET_DEVICE = 94
ADMIN_REBOOT = 97
ADMIN_SHUTDOWN = 98
ADMIN_FACTORY_RESET_CONFIG = 99
ADMIN_NODEDB_RESET = 100
ADMIN_SESSION_PASSKEY = 101

#: The writes this node accepts. Everything else `AdminMessage` can carry --
#: ham mode, fixed position, canned messages, ringtones, DFU, OTA, remote
#: hardware -- is refused, because storing it would be a claim to a module or a
#: peripheral this board does not have. `shutdown_seconds` is among them: the
#: metadata says `canShutdown` is false, and a node that powers nothing off
#: after agreeing to has told the app it is gone when it is not.
ADMIN_WRITES = (ADMIN_SET_OWNER, ADMIN_SET_CHANNEL, ADMIN_SET_CONFIG,
                ADMIN_BEGIN_EDIT, ADMIN_COMMIT_EDIT,
                ADMIN_FACTORY_RESET_DEVICE, ADMIN_REBOOT,
                ADMIN_FACTORY_RESET_CONFIG, ADMIN_NODEDB_RESET)

#: The shortest a restart can be put off for. The client is still waiting to
#: read the `QueueStatus` that says the command was taken, and a board that
#: resets before it has been read has refused the request as far as the app can
#: tell. The app's own default is five seconds; this is the floor under it.
MIN_RESTART_S = 2

#: The replay guard on admin writes, in the firmware's own numbers: a key is
#: dead after five minutes, and is replaced on the first response sent after
#: the halfway mark, which the client records as it arrives.
PASSKEY_BYTES = 8
PASSKEY_TTL_S = 300
PASSKEY_ROTATE_S = 150

#: How recently a node must have been heard to be counted as online, which is
#: the firmware's own two hours.
ONLINE_S = 7200

#: `MAX_NUM_CHANNELS` in the firmware. A client expects all eight slots, with
#: the unused ones present and disabled rather than absent.
NUM_CHANNELS = 8

CHANNEL_DISABLED = 0
CHANNEL_PRIMARY = 1

#: The slot this node's one key lives in, and therefore the only channel index
#: any packet it can read or write belongs to. Named rather than written as a
#: bare zero because the number it replaced -- the channel hash -- is also
#: called `channel`, and the two being interchangeable was the bug.
PRIMARY_CHANNEL = 0

#: `Routing.Error`, which is also what `QueueStatus.res` carries. Only the few
#: this node can honestly claim are here. `NOT_AUTHORIZED` is a decision and
#: `NO_RESPONSE` is an absence, and telling them apart matters to whoever reads
#: the app's log: one is this node refusing, the other is a module it lacks.
ROUTING_NONE = 0
ROUTING_NO_RESPONSE = 8
ROUTING_DUTY_CYCLE_LIMIT = 9
ROUTING_BAD_REQUEST = 32
ROUTING_NOT_AUTHORIZED = 33

#: There is no outgoing queue to report on: `Node.send` has already succeeded or
#: failed by the time the answer is written. One slot, always free, says that.
QUEUE_LEN = 1

#: `myNodeInfo.min_app_version` in the firmware: the oldest app that understands
#: this protocol. Sent because an app that is older says so instead of failing
#: in some more interesting way.
MIN_APP_VERSION = 30200

#: What the app shows as the build. The firmware sends its own version string
#: here, and ours is the tag the wire format was read off, without the `v` the
#: firmware's own string does not carry.
FIRMWARE_VERSION = mt.PINNED_FIRMWARE[1:]

#: `pio_env`, the build the app thinks it is talking to. Not a real PlatformIO
#: environment, and saying so is better than borrowing a name that is.
PIO_ENV = "circuitpython-muzi_base_duo"

#: RegionCode in config.proto. Keyed by name because `meshtastic.REGION_NAMES`
#: is in a different order and always will be.
PROTO_REGIONS = {
    "UNSET": 0, "US": 1, "EU_433": 2, "EU_868": 3, "CN": 4, "JP": 5,
    "ANZ": 6, "KR": 7, "TW": 8, "RU": 9, "IN": 10, "NZ_865": 11, "TH": 12,
    "LORA_24": 13, "UA_433": 14, "UA_868": 15, "MY_433": 16, "MY_919": 17,
    "SG_923": 18, "PH_433": 19, "PH_868": 20, "PH_915": 21, "ANZ_433": 22,
    "KZ_433": 23, "KZ_863": 24, "NP_865": 25, "BR_902": 26,
}

#: ModemPreset, same caveat. LongTurbo is missing on purpose: it has no number
#: in the app's enum, so a node using it describes its modem in full rather than
#: naming something the phone cannot look up.
PROTO_PRESETS = {
    "LongFast": 0, "LongSlow": 1, "MediumSlow": 3, "MediumFast": 4,
    "ShortSlow": 5, "ShortFast": 6, "LongModerate": 7, "ShortTurbo": 8,
}

# -------------------------------------------------------------- the device

def my_node_info(node_num, nodedb_count=0):
    """`MyNodeInfo`: the few facts a client needs before anything else."""
    buf = pb.msg_buffer()
    at = pb.uint(buf, 0, 1, node_num)
    at = pb.uint(buf, at, 11, MIN_APP_VERSION)
    at = pb.string(buf, at, 13, PIO_ENV)
    at = pb.uint(buf, at, 15, nodedb_count)
    return pb.msg_take(buf, at)


def device_metadata(role=nodeinfo.ROLE_CLIENT):
    """`DeviceMetadata`: what this node can do, which is mostly nothing.

    `device_state_version` is left out rather than guessed. It versions the
    firmware's own flash layout, which this node does not have and the app does
    not read, and a wrong number there would be a claim about a format.
    """
    buf = pb.msg_buffer()
    at = pb.string(buf, 0, 1, FIRMWARE_VERSION)
    at = pb.boolean(buf, at, 3, False)   # canShutdown
    at = pb.boolean(buf, at, 4, False)   # hasWifi
    at = pb.boolean(buf, at, 5, True)    # hasBluetooth
    at = pb.boolean(buf, at, 6, False)   # hasEthernet
    at = pb.uint(buf, at, 7, role)
    at = pb.uint(buf, at, 9, mt.HW_MODEL)
    at = pb.boolean(buf, at, 10, False)  # hasRemoteHardware
    at = pb.boolean(buf, at, 11, False)  # hasPKC
    return pb.msg_take(buf, at)


def node_info(num, user=None, snr=None, last_heard=0, hops=None):
    """`NodeInfo`: one entry in the list the app draws as the mesh."""
    buf = pb.msg_buffer()
    at = pb.uint(buf, 0, 1, num)
    if user is not None:
        at = pb.nested(buf, at, 2, user)
    if snr is not None:
        at = pb.float32(buf, at, 4, snr, always=True)
    at = pb.fixed32(buf, at, 5, last_heard)
    # `hops_away` is an optional field, so zero hops and no idea how many
    # are different answers and have to stay that way.
    if hops is not None:
        at = pb.uint(buf, at, 9, hops, always=True)
    return pb.msg_take(buf, at)


def channel(index, name=None, psk=None, role=CHANNEL_DISABLED):
    """`Channel`: one of the eight slots, configured or empty."""
    settings = b""
    if role != CHANNEL_DISABLED:
        inner = pb.msg_buffer()
        at = pb.blob(inner, 0, 2, psk or b"")
        # Empty means unnamed, and an unnamed primary channel is displayed by
        # its preset name. That substitution is the app's to make, not ours.
        at = pb.string(inner, at, 3, name or "", 12)
        settings = pb.msg_take(inner, at)
    buf = pb.msg_buffer()
    at = pb.uint(buf, 0, 1, index)
    at = pb.nested(buf, at, 2, settings)
    at = pb.uint(buf, at, 3, role)
    return pb.msg_take(buf, at)


def lora_config(settings, hop_limit, tx_power_dbm, channel_num=0):
    """`Config.LoRaConfig`: the only section of config that is not default."""
    preset = PROTO_PRESETS.get(settings.preset_name)
    buf = pb.msg_buffer()
    at = pb.boolean(buf, 0, 1, preset is not None)
    if preset is None:
        at = pb.uint(buf, at, 3, settings.bw_hz // 1000)
        at = pb.uint(buf, at, 4, settings.sf)
        at = pb.uint(buf, at, 5, settings.cr)
    else:
        at = pb.uint(buf, at, 2, preset)
    at = pb.uint(buf, at, 7, PROTO_REGIONS.get(settings.region_name, 0))
    at = pb.uint(buf, at, 8, hop_limit)
    at = pb.boolean(buf, at, 9, True)                 # tx_enabled
    at = pb.int32(buf, at, 10, tx_power_dbm)
    at = pb.uint(buf, at, 11, channel_num)
    at = pb.boolean(buf, at, 13, True)                # sx126x_rx_boosted_gain
    return pb.msg_take(buf, at)


# --------------------------------------------------------------- packets

def mesh_packet(from_, to, channel_index, packet_id, decoded=None,
                snr=None, rssi=None, rx_time=0, hop_limit=0,
                hop_start=0, want_ack=False, via_mqtt=False, next_hop=0,
                relay_node=0):
    """`MeshPacket`, the same message in both directions.

    `decoded` is the already-decrypted `Data` submessage, passed through as the
    bytes that came off the air rather than re-encoded from the parse of them:
    what the phone gets is then exactly what the sender wrote, including any
    field this project has never learned to read.

    The field is called `channel` and carries an index, which is not the number
    the header field of the same name carries. On the air the byte is a hash,
    which is how a receiver picks which key to try; `Router::perhapsDecode`
    overwrites it with the index of the key that fitted before the packet ever
    reaches a client. So a client reads this as a slot in the eight-channel
    list, files both the message and the sender under it, and given a hash of,
    say, 0x08 files them under a channel it was never told about and does not
    draw. The parameter is spelled out here so the two cannot be confused again.
    """
    buf = pb.msg_buffer()
    at = pb.fixed32(buf, 0, 1, from_)
    at = pb.fixed32(buf, at, 2, to)
    at = pb.uint(buf, at, 3, channel_index)
    at = pb.nested(buf, at, 4, decoded)
    at = pb.fixed32(buf, at, 6, packet_id)
    at = pb.fixed32(buf, at, 7, rx_time)
    if snr is not None:
        at = pb.float32(buf, at, 8, snr, always=True)
    at = pb.uint(buf, at, 9, hop_limit)
    at = pb.boolean(buf, at, 10, want_ack)
    if rssi is not None:
        at = pb.int32(buf, at, 12, int(rssi), always=True)
    at = pb.boolean(buf, at, 14, via_mqtt)
    at = pb.uint(buf, at, 15, hop_start)
    at = pb.uint(buf, at, 18, next_hop)
    at = pb.uint(buf, at, 19, relay_node)
    return pb.msg_take(buf, at)


def received(packet, plain, rx_time=0):
    """The `MeshPacket` for a frame off the air.

    `plain` is the decrypted payload, and there is no path here without one. A
    client drops a `MeshPacket` carrying only ciphertext before it looks at
    anything else in it -- it can do no more with the bytes than we could -- so
    forwarding one would spend a slot in the read queue that a readable packet
    needs, and would not so much as add the sender to the node list.
    """
    return mesh_packet(
        packet.from_, packet.to, PRIMARY_CHANNEL, packet.id,
        decoded=plain,
        snr=packet.snr, rssi=None if packet.rssi is None else round(packet.rssi),
        rx_time=rx_time, hop_limit=packet.hop_limit,
        hop_start=packet.hop_start, want_ack=packet.want_ack,
        via_mqtt=packet.via_mqtt, next_hop=packet.next_hop,
        relay_node=packet.relay_node)


def _data(portnum, payload, want_response=False, dest=0, request_id=0,
          reply_id=0, bitfield=None):
    """The `Data` submessage, for packets built here rather than received."""
    buf = pb.msg_buffer()
    at = pb.uint(buf, 0, 1, portnum)
    at = pb.blob(buf, at, 2, payload)
    at = pb.boolean(buf, at, 3, want_response)
    at = pb.fixed32(buf, at, 4, dest)
    # What this is an answer to. Without it an answer is just an unsolicited
    # message that happened to arrive, and the client goes on waiting.
    at = pb.fixed32(buf, at, 6, request_id)
    at = pb.fixed32(buf, at, 7, reply_id)
    if bitfield is not None:
        at = pb.uint(buf, at, 9, bitfield, always=True)
    return pb.msg_take(buf, at)


def queue_status(packet_id, error=ROUTING_NONE):
    """`QueueStatus`: what became of one packet the phone handed over.

    The app will not send a second packet until the first has been answered, so
    this goes back on every path out of `_transmit`, including the ones that
    refuse. Leaving it out does not lose a message, it stops the conversation.
    """
    buf = pb.msg_buffer()
    at = pb.int32(buf, 0, 1, error)
    at = pb.uint(buf, at, 2, QUEUE_LEN)
    at = pb.uint(buf, at, 3, QUEUE_LEN)
    at = pb.uint(buf, at, 4, packet_id)
    return pb.msg_take(buf, at)


# --------------------------------------------------------------- telemetry

def air_util_tx(node):
    """Percent of the last hour spent transmitting.

    Which is what the duty cycle already tracks, and for the same reason: a
    percentage of an hour only means anything if you keep the hour.
    """
    duty = node.tx.duty_cycle
    return 100.0 * duty.used_us() / (duty.window_s * 1_000_000)


def battery(node):
    """(level, volts) for `DeviceMetrics`, or (None, None) if unmeasurable.

    A level above 100 is how Meshtastic says "mains, no pack": the charger's
    power path runs the board perfectly well with nothing in the battery
    connector, and 0% there would be a lie the app draws as an emergency. The
    voltage is left out in that case rather than reporting the power path's own
    rail as a cell.
    """
    power = getattr(node, "power", None)
    if power is None:
        return None, None
    level, millivolts = power.report()
    if level is None:
        return 101, None
    return level, millivolts / 1000.0


def device_metrics(node, uptime):
    """`DeviceMetrics`, which the app draws as the node's vital signs.

    Channel utilisation is the one still missing. It needs the receiver sampled
    for how much of the time the band is busy, which nothing here does yet, and
    the field is `optional`: absent means unknown, while zero means a silent
    channel. The app draws the difference.
    """
    buf = pb.msg_buffer()
    at = 0
    level, volts = battery(node)
    if level is not None:
        at = pb.uint(buf, at, 1, level, always=True)
    if volts is not None:
        at = pb.float32(buf, at, 2, volts, always=True)
    at = pb.float32(buf, at, 4, air_util_tx(node), always=True)
    at = pb.uint(buf, at, 5, uptime, always=True)
    return pb.msg_take(buf, at)


def local_stats(node, uptime):
    """`LocalStats`: what this node has done and seen since it came up.

    Not `optional` fields, so everything is present and the ones nothing counts
    here read as zero. Channel utilisation is the only one of those: it needs a
    receiver that measures the noise floor, and this one does not.
    """
    now = nodedb.seconds()
    online = sum(1 for peer in node.nodes.roster()
                 if now - peer.last < ONLINE_S)
    buf = pb.msg_buffer()
    at = pb.uint(buf, 0, 1, uptime)
    at = pb.float32(buf, at, 3, air_util_tx(node))
    at = pb.uint(buf, at, 4, node.sent)
    at = pb.uint(buf, at, 5, node.heard)
    at = pb.uint(buf, at, 6, node.dropped)
    # Ourselves included in both, as `nodedb_count` already counts us.
    at = pb.uint(buf, at, 7, online + 1)
    at = pb.uint(buf, at, 8, len(node.nodes) + 1)
    return pb.msg_take(buf, at)


def telemetry(node, variant):
    """`Telemetry` of the kind asked for, or None if this node does not keep it.

    Uptime is the node's own counter rather than a wall clock, so it is right
    from the first second; the timestamp is not sent until there is a clock to
    read, since an unset one would stamp everything with 1970.
    """
    uptime = nodedb.seconds()
    if variant == TELEMETRY_DEVICE:
        body = device_metrics(node, uptime)
    elif variant == TELEMETRY_LOCAL_STATS:
        body = local_stats(node, uptime)
    else:
        return None
    at = int(time.time()) if node.clock_set else 0
    out = pb.msg_buffer()
    end = pb.fixed32(out, 0, 1, at)
    end = pb.nested(out, end, variant, body)
    return pb.msg_take(out, end)


def _variant(request):
    """Which `Telemetry` a client asked for: it sends that one, and empty."""
    for number, wire, _value, _off, _length in mt.fields(request):
        if wire == pb.WIRE_LEN:
            return number
    return 0


# ---------------------------------------------------------- administration

def node_channel(node, index):
    """One of the eight channel slots as this node has it configured."""
    if index:
        return channel(index)
    cfg = node.config
    return channel(0, cfg.channel_name, cfg.channel_key, CHANNEL_PRIMARY)


def node_lora(node):
    """The LoRa section of config, as this node has it stored.

    Power comes from the store rather than the radio when it is set, because
    after a write the radio is still running the old one until a reset and it is
    the stored value the app is asking about. Unset means the region's maximum,
    which only the radio knows the number for.
    """
    cfg = node.config
    return lora_config(node.settings, node.tx.hop_limit,
                       cfg.tx_power_dbm or node.radio.power_dbm,
                       cfg.channel_num)


def _command(admin):
    """The one `AdminMessage` field that is set, and the passkey beside it.

    `payload_variant` is a oneof, so there is exactly one command, and the
    passkey is a separate field riding along with it rather than inside it.
    Bodies are copied out here because an offset from `fields` only means
    anything against the buffer it was walked from.
    """
    number = wire = value = 0
    body = passkey = b""
    for found, kind, raw, off, length in mt.fields(admin):
        chunk = bytes(admin[off:off + length]) if kind == pb.WIRE_LEN else b""
        if found == ADMIN_SESSION_PASSKEY:
            passkey = chunk
        elif not number:
            number, wire, value, body = found, kind, raw, chunk
    return number, wire, value, body, passkey


def _named(table, number, what):
    """The name a renumbered enum value has here, or a refusal."""
    for name, value in table.items():
        if value == number:
            return name
    raise ValueError("no %s numbered %d" % (what, number))


def _signed(value):
    """A varint that was written as a sign-extended int32, read back as one.

    Which the delays are: `reboot_seconds` counts down, and a negative one
    calls the whole thing off.
    """
    return value - (1 << 64) if value >> 63 else value


def admin_read(node, number, value):
    """Answers a `get_x_request`, or None if this node does not keep it."""
    if number == ADMIN_GET_OWNER:
        num = node.config.node_num
        long_name, short_name = nodeinfo.names(num)
        return pb.message(ADMIN_OWNER_RESPONSE,
                          nodeinfo.user(num, long_name, short_name))
    if number == ADMIN_GET_METADATA:
        return pb.message(ADMIN_METADATA_RESPONSE, device_metadata())
    if number == ADMIN_GET_CONFIG:
        section = value + CONFIG_TYPE_OFFSET
        if not CONFIG_DEVICE <= section <= CONFIG_SECTIONS:
            return None
        return pb.message(ADMIN_CONFIG_RESPONSE, pb.message(
            section, node_lora(node) if section == CONFIG_LORA else b""))
    if number == ADMIN_GET_MODULE_CONFIG:
        section = value + CONFIG_TYPE_OFFSET
        if not 1 <= section <= MODULE_CONFIGS:
            return None
        return pb.message(ADMIN_MODULE_CONFIG_RESPONSE,
                          pb.message(section, b""))
    if number == ADMIN_GET_CHANNEL:
        # Sent as the index plus one, so that asking for channel zero is not
        # the same as a client that did not set the field at all.
        index = value - 1
        if not 0 <= index < NUM_CHANNELS:
            return None
        return pb.message(ADMIN_CHANNEL_RESPONSE, node_channel(node, index))
    return None


def _owner(body):
    """`set_owner`: the two fields of a `User` that this node stores.

    The id is ignored rather than refused. It is derived from the node number
    and the app sends back whatever we told it, so honouring it would only be a
    way to be renamed into someone else.
    """
    values = {}
    for number, wire, _value, off, length in mt.fields(body):
        if wire != pb.WIRE_LEN:
            continue
        if number == 2:
            values["long_name"] = str(body[off:off + length], "utf-8")
        elif number == 3:
            values["short_name"] = str(body[off:off + length], "utf-8")
    if not values:
        raise ValueError("an owner with no name")
    return values


def _lora_settings(body):
    """`Config.LoRaConfig` reduced to the four things this node keeps.

    Proto3 omits a default and the app sends the whole section, so a field that
    is not here is one being set back to its default rather than one being left
    alone: they are filled in, not skipped. `use_preset` is the sharp edge --
    its default is false, so a node running a preset says so explicitly and an
    absent field means a hand-tuned modem rather than a stock one.
    """
    use_preset = False
    region = 0
    values = {"preset": "LongFast", "tx_power_dbm": 0, "channel_num": 0}
    for number, _wire, value, _off, _length in mt.fields(body):
        if number == 1:
            use_preset = bool(value)
        elif number == 2:
            values["preset"] = _named(PROTO_PRESETS, value, "preset")
        elif number == 7:
            region = value
        elif number == 10:
            values["tx_power_dbm"] = value
        elif number == 11:
            values["channel_num"] = value
    if not use_preset:
        # A hand-tuned bandwidth, spreading factor and coding rate. The radio
        # could run it; the keystore has nowhere to put it, since what it keeps
        # is a preset name.
        raise ValueError("a hand-tuned modem has no name to store")
    if not region:
        # `region_index` accepts UNSET, so this has to be refused here or a
        # section that simply omitted the field would retune the board to a
        # band that does not exist.
        raise ValueError("region unset")
    values["region"] = _named(PROTO_REGIONS, region, "region")
    return values


def _config_write(body):
    """`set_config`: the LoRa section, which is the only one that is kept."""
    for number, wire, _value, off, length in mt.fields(body):
        if wire != pb.WIRE_LEN:
            continue
        if number != CONFIG_LORA:
            raise ValueError("config section %d is not stored here" % number)
        return _lora_settings(bytes(body[off:off + length]))
    raise ValueError("a config with no section")


def _channel_write(body):
    """`set_channel` for the primary channel, which is the only one there is."""
    index = 0
    name = ""
    psk = b""
    for number, wire, value, off, length in mt.fields(body):
        if number == 1:
            index = value
        elif number == 2 and wire == pb.WIRE_LEN:
            settings = bytes(body[off:off + length])
            for inner, kind, _v, at, size in mt.fields(settings):
                if kind != pb.WIRE_LEN:
                    continue
                if inner == 2:
                    psk = bytes(settings[at:at + size])
                elif inner == 3:
                    name = str(settings[at:at + size], "utf-8")
    if index != 0:
        raise ValueError("only the primary channel is stored")
    if not psk:
        # Absent and empty are the same thing in proto3, and both of them mean
        # an unencrypted channel, which this stack cannot run.
        raise ValueError("a channel with no key")
    return {"channel_name": name, "channel_key": mesh_config.channel_key(psk)}


class Session:
    """The passkey a client quotes back before it is allowed to change anything.

    Not a login. What says the client may be here at all is that it is on the
    other end of a bonded BLE link; this is the replay guard on top of that, so
    that a `set_config` captured once is worth nothing five minutes later.

    Minted on the first read and rotated when it is half spent, always on the
    way out with a response, which is where the app takes it from. Only the
    current key is accepted: the client has just been handed it in the same
    message that answered its question, so it is never the one holding the
    older of the two.
    """

    def __init__(self, radio):
        self.radio = radio
        self.key = b""
        self.issued = 0

    def passkey(self):
        """The key to send with a response, minted or rotated as needed."""
        now = nodedb.seconds()
        if not self.key or now - self.issued >= PASSKEY_ROTATE_S:
            self.key = b"".join(struct.pack("<I", self.radio.entropy())
                                for _ in range(PASSKEY_BYTES // 4))
            self.issued = now
        return self.key

    def accepts(self, offered):
        """Whether a write may go ahead. No key and an expired key are both no."""
        if not self.key or not offered:
            return False
        if nodedb.seconds() - self.issued >= PASSKEY_TTL_S:
            return False
        return offered == self.key

    def forget(self):
        """Ends the session, so the next client starts without a key."""
        self.key = b""
        self.issued = 0


# ------------------------------------------------------------ the session

class PhoneAPI:
    """One client's view of this node, and what it is allowed to do with it.

    Stateless apart from knowing whether the config dump has been asked for. A
    client that reconnects asks again, and a client that asks twice gets it
    twice, which is what the firmware does and what the app relies on after it
    loses the link.
    """

    def __init__(self, node):
        self.node = node
        #: The number the client last asked us to echo back, or None before it
        #: has asked. Also the answer to "is anyone actually listening".
        self.config_id = None
        self.sent = 0
        self.refused = 0
        #: Packets for a module this node does not have. Kept apart from
        #: `refused` because nothing was decided: see `_local`.
        self.ignored = 0
        #: Heartbeats seen. Counted only so the caller can tell a write that was
        #: silent by design from one that was not understood.
        self.beats = 0
        #: The replay guard on writes, which lives as long as the session does.
        self.session = Session(node.radio)
        #: What has been set since `begin_edit_settings`, or None when no edit
        #: is open and a write goes straight to flash.
        self.pending = None
        self.wrote = 0
        #: When an ordered restart comes due, or None. Deliberately outside
        #: `reset`: the app hands over a reboot and then drops the link, and a
        #: restart forgotten on the way out is one that never happens.
        self.restart_at = None

    def reset(self):
        """Forgets the client. Either edge of a connection is a new session."""
        self.config_id = None
        self.pending = None
        self.session.forget()

    # ---------------------------------------------------------- to the phone

    def config(self, config_id):
        """Yields every `FromRadio` a client expects after `want_config_id`.

        The number is not only a nonce to echo back. Two values of it are agreed
        constants meaning "the device half" and "the node list half", and they
        are not a hint: a client that asked for the second half and is sent
        `my_info` again has its handshake reset to the first, and then rejects
        the terminator it was waiting for. It retries, is answered the same way,
        and never connects. Any other number is a client that wants the whole
        thing at once, which is what this used to be and still is for the CLI.
        """
        if config_id != NODE_INFO_NONCE:
            for message in self._device():
                yield message
        if config_id != CONFIG_NONCE:
            for message in self._roster():
                yield message
        # The terminator, and the reason it is a number rather than a flag: it
        # says which half of the dump has just ended.
        yield pb.one_uint(FROM_COMPLETE_ID, config_id)
        self.config_id = config_id

    def _device(self):
        """What this node is: everything except who it has heard."""
        node = self.node
        cfg = node.config

        yield pb.message(FROM_MY_INFO,
                         my_node_info(cfg.node_num, len(node.nodes) + 1))
        yield pb.message(FROM_METADATA, device_metadata())

        yield pb.message(FROM_CHANNEL, node_channel(node, 0))
        for index in range(1, NUM_CHANNELS):
            yield pb.message(FROM_CHANNEL, node_channel(node, index))

        lora = node_lora(node)
        for number in range(CONFIG_DEVICE, CONFIG_SECURITY + 1):
            yield pb.message(FROM_CONFIG,
                             pb.message(number, lora if number == CONFIG_LORA
                                        else b""))
        for number in range(1, MODULE_CONFIGS + 1):
            yield pb.message(FROM_MODULE_CONFIG, pb.message(number, b""))

    def _roster(self):
        """The mesh as this node sees it, itself included."""
        node = self.node
        num = node.config.node_num
        long_name, short_name = nodeinfo.names(num)
        # Ourselves first, and with no snr or last_heard: those describe hearing
        # a node, and we have never heard this one.
        yield pb.message(FROM_NODE_INFO, node_info(
            num, user=nodeinfo.user(num, long_name, short_name)))
        named = 0
        for peer in node.nodes.roster():
            if peer.long is not None or peer.short is not None:
                named += 1
            yield pb.message(FROM_NODE_INFO, self.peer_info(peer))
        # A peer with no name is sent as a bare number, and the app draws that
        # as "Meshtastic" and the last four digits. So this line says whether a
        # placeholder on the phone is the app inventing one or us sending one.
        # The frame counts sit here because `serve` prints nothing else.
        print("# phone: roster of %d, %d named, %d of %d frames decoded"
              % (len(node.nodes) + 1, named + 1, node.decoded, node.heard))

    def peer_info(self, peer):
        """One entry of the node database, as the app wants to see it.

        The hardware model is the peer's own, not a zero, and that matters more
        than it looks. A client reads an unset model as the mark of an entry it
        invented from a bare node number, so it files the names beside one under
        "made up", refuses to index them for search, and lets a later placeholder
        overwrite them. Passing on what the peer said it was is the difference
        between a node in the list and a node in the list with a name.
        """
        user = None
        if peer.long is not None or peer.short is not None:
            long_name, short_name = nodeinfo.default_names(peer.num)
            user = nodeinfo.user(
                peer.num,
                peer.long if peer.long is not None else long_name,
                peer.short if peer.short is not None else short_name,
                hw_model=peer.hw, role=peer.role)
        return node_info(peer.num, user=user, snr=peer.snr,
                         last_heard=self._epoch(peer.last), hops=peer.hops)

    def _epoch(self, at):
        """A `nodedb` timestamp as unix time, or 0 while there is no clock."""
        if not self.node.clock_set:
            return 0
        return int(time.time()) - (nodedb.seconds() - at)

    def heard(self, packet, frame, payload):
        """The `FromRadio` for one received frame, or None to say nothing.

        A frame the channel key did not open is not passed on. See `received`:
        a client throws away a packet it cannot read before it does anything
        else with it, so the only effect of forwarding one is to crowd out a
        packet the client would have used. For `Node.on_receive`.
        """
        if payload is None:
            return None
        plain = mt.decrypt(frame, self.node.config.channel_key)
        return pb.message(FROM_PACKET, received(
            packet, plain, rx_time=self._epoch(nodedb.seconds())))

    # -------------------------------------------------------- from the phone

    def alive(self):
        """The queue state, naming no packet: the smallest true thing to say.

        Naming a packet would tell the app that a message it is still waiting
        on has been dealt with, so this names none. It answers a heartbeat, and
        `meshble.Phone._keepalive` sends it unprompted for the same reason.
        """
        return pb.message(FROM_QUEUE_STATUS, queue_status(0))

    def receive(self, data):
        """Handles one `ToRadio`. Yields whatever should go back.

        Hostile input, and parsed as such: `meshtastic.fields` is the strict
        walker the crate uses on packets off the air, so a truncated length or
        an impossible wire type stops here rather than being skipped over.
        """
        for number, wire, value, off, length in mt.fields(data):
            if number == TO_WANT_CONFIG_ID and wire == pb.WIRE_VARINT:
                for message in self.config(value):
                    yield message
                return
            if number == TO_PACKET and wire == pb.WIRE_LEN:
                for message in self._transmit(data[off:off + length]):
                    yield message
                return
            if number == TO_DISCONNECT and wire == pb.WIRE_VARINT and value:
                self.reset()
                return
            # A heartbeat is the client asking whether the link is still there,
            # and the answer is the whole point of it. `getFromRadio` sends a
            # QueueStatus back for every one, and the app's `checkLiveness`
            # tears the connection down after sixty seconds of hearing nothing
            # at all -- an empty read does not count. Saying nothing here is a
            # disconnect and a reconnect every ninety seconds on a quiet mesh.
            if number == TO_HEARTBEAT:
                self.beats += 1
                yield self.alive()
                return

    def _transmit(self, raw):
        """Puts a packet the phone built on the air, or says why it did not."""
        to = mt.BROADCAST
        packet_id = 0
        want_ack = False
        decoded = None
        for number, wire, value, off, length in mt.fields(raw):
            if number == 2:
                to = value
            elif number == 4 and wire == pb.WIRE_LEN:
                decoded = raw[off:off + length]
            elif number == 6:
                packet_id = value
            elif number == 10:
                want_ack = bool(value)

        if decoded is None:
            # Either the app encrypted it itself, which it does not, or this is
            # a message shape that did not exist when this was written.
            self.refused += 1
            print("# phone: packet with no readable payload, ignored")
            yield pb.message(FROM_QUEUE_STATUS,
                             queue_status(packet_id, ROUTING_BAD_REQUEST))
            return

        portnum = 0
        payload = b""
        want_response = False
        bitfield = None
        dropped = False
        for number, wire, value, off, length in mt.fields(decoded):
            if number == 1:
                portnum = value
            elif number == 2 and wire == pb.WIRE_LEN:
                payload = decoded[off:off + length]
            elif number == 3:
                want_response = bool(value)
            elif number == 9:
                bitfield = value
            elif number in (7, 8):
                # reply_id and emoji: threading and reactions. `encode_data` in
                # the crate has no room for either, so the message goes as plain
                # text and the app is not told it lost its context.
                dropped = True

        if to == self.node.tx.node_num:
            # Addressed to this node rather than through it. Nothing about it
            # goes on the air; it is answered here or refused here.
            for message in self._local(portnum, payload, packet_id,
                                       want_response):
                yield message
            return

        if dropped:
            print("# phone: reply or reaction sent as plain text")

        sent_id = self.node.send(payload, portnum, to=to, want_ack=want_ack,
                                 want_response=want_response,
                                 bitfield=bitfield,
                                 packet_id=packet_id or None)
        if sent_id is None:
            # `Node.send` has already printed which guard refused it, and they
            # are all forms of "not now": the airtime rules, not the request.
            self.refused += 1
            yield pb.message(FROM_QUEUE_STATUS,
                             queue_status(packet_id, ROUTING_DUTY_CYCLE_LIMIT))
            return
        self.sent += 1
        yield pb.message(FROM_QUEUE_STATUS, queue_status(sent_id))
        # The firmware echoes an outgoing packet back over the same link, which
        # is how the app knows the radio accepted it and can put it in the
        # thread. Rebuilt from the fields that were actually used rather than
        # echoing what arrived, so that what the phone files is what went out.
        # Delivery is a separate question and this is not an answer to it.
        yield pb.message(FROM_PACKET, mesh_packet(
            self.node.tx.node_num, to, PRIMARY_CHANNEL, sent_id,
            decoded=_data(portnum, payload, want_response, bitfield=bitfield),
            rx_time=self._epoch(nodedb.seconds()),
            hop_limit=self.node.tx.hop_limit,
            hop_start=self.node.tx.hop_limit, want_ack=want_ack,
            relay_node=self.node.tx.relay_node))

    def _local(self, portnum, payload, packet_id, want_response):
        """A packet the client addressed to this node itself.

        Telemetry and administration, and the only place either is accepted:
        arriving here means it came over the bonded link rather than off the
        air. Anything else is a module this node does not have, which is a
        different answer from a refusal. All three are answered with the same
        `QueueStatus` a real transmission would get, because from the app's
        side the packet was handed over and has to be accounted for either way.
        """
        if portnum == PORT_TELEMETRY:
            answer = telemetry(self.node, _variant(payload))
        elif portnum == PORT_ADMIN:
            answer = self._admin(payload)
        else:
            # A module this node does not have, which the app tries on every
            # session -- port 65 is store-and-forward asking for history. A
            # node with the module switched off drops the packet and tells the
            # client the radio took it, so that is what happens here. Refusing
            # claimed a permission problem that was never the reason.
            self.ignored += 1
            print("# phone: port %s ignored, no such module"
                  % mt.PORTNUM_NAMES.get(portnum, portnum))
            yield pb.message(FROM_QUEUE_STATUS, queue_status(
                packet_id,
                ROUTING_NO_RESPONSE if want_response else ROUTING_NONE))
            return

        if answer is not None:
            yield pb.message(FROM_QUEUE_STATUS, queue_status(packet_id))
            # An empty answer is a command that was carried out and has nothing
            # to report; sending an empty `AdminMessage` back would be a reply
            # that says nothing rather than no reply at all.
            if answer:
                yield pb.message(FROM_PACKET,
                                 self._answer(portnum, answer, packet_id))
            return

        self.refused += 1
        # The portnum and whether it wanted an answer are printed because a
        # client left waiting behaves very differently from one that is not.
        print("# phone: port %s%s refused"
              % (mt.PORTNUM_NAMES.get(portnum, portnum),
                 ", answer wanted" if want_response else ""))
        yield pb.message(FROM_QUEUE_STATUS,
                         queue_status(packet_id, ROUTING_NOT_AUTHORIZED))

    def _admin(self, payload):
        """Carries out one `AdminMessage`. None refuses it.

        Returns the body of the reply to send back, empty for a command that
        succeeded with nothing to say. Reads are free and mint the passkey;
        every write has to quote that passkey back, which is what stops one
        captured off the link being replayed afterwards.
        """
        number, wire, value, body, passkey = _command(payload)

        # The clock is the exception that predates all of this: it needs no
        # session because it changes nothing that is stored, and it arrives
        # before the app has asked us anything it could have got a key from.
        if number == ADMIN_SET_TIME_ONLY and wire == pb.WIRE_I32:
            self.node.set_clock(value)
            return b""

        answer = admin_read(self.node, number, value)
        if answer is not None:
            return answer + pb.one_blob(ADMIN_SESSION_PASSKEY,
                                        self.session.passkey())

        if number not in ADMIN_WRITES:
            return None
        if not self.session.accepts(passkey):
            # Either a client that has never read anything from us, or a packet
            # older than the key it was built with.
            print("# phone: admin %d without a live passkey, refused" % number)
            return None
        try:
            return self._write(number, value, body)
        except ValueError as err:
            print("# phone: admin %d refused, %s" % (number, err))
            return None

    def _write(self, number, value, body):
        """One `set_x` that quoted the passkey. Raises ValueError to refuse."""
        if number == ADMIN_BEGIN_EDIT:
            self.pending = {}
            return b""
        if number == ADMIN_COMMIT_EDIT:
            values, self.pending = self.pending, None
            if values:
                self._store(values)
            return b""

        if number == ADMIN_NODEDB_RESET:
            self.node.nodes.forget_all()
            print("# phone: node database cleared")
            return b""
        if number == ADMIN_REBOOT:
            self._schedule(_signed(value))
            return b""
        if number in (ADMIN_FACTORY_RESET_CONFIG, ADMIN_FACTORY_RESET_DEVICE):
            self.node.factory_reset(number == ADMIN_FACTORY_RESET_DEVICE)
            # The settings this session is running on no longer exist, and
            # nothing re-reads them without a restart.
            self._schedule(MIN_RESTART_S)
            return b""

        if number == ADMIN_SET_OWNER:
            values = _owner(body)
        elif number == ADMIN_SET_CONFIG:
            values = _config_write(body)
        else:
            values = _channel_write(body)

        if self.pending is None:
            self._store(values)
            return b""
        # Inside an edit. Every store erases and rewrites the same flash page,
        # and a config screen sets a dozen fields, so they are collected and
        # written once at the commit.
        self.pending.update(values)
        return b""

    def _schedule(self, seconds):
        """Arms or cancels a restart, which `restart_due` later reports.

        Not carried out here. The reply to this command has not been written
        yet, let alone read, and a board that resets first has refused the
        request as far as the client can tell.
        """
        if seconds < 0:
            self.restart_at = None
            print("# phone: restart cancelled")
            return
        delay = max(seconds, MIN_RESTART_S)
        self.restart_at = nodedb.seconds() + delay
        print("# phone: restarting in %ds" % delay)

    def restart_due(self):
        """Whether an armed restart has come round. For the caller's loop."""
        return (self.restart_at is not None
                and nodedb.seconds() >= self.restart_at)

    def _store(self, values):
        """Puts settings in flash and makes this session report them."""
        wrote = mesh_config.config_configure(**values)
        self.wrote += 1
        # The radio keeps the settings it was started with until a reset. What
        # is reported has to change now regardless, or the app reads back the
        # value it just replaced and takes the write for lost.
        self.node.reload()
        print("# phone: stored %s%s"
              % (", ".join(sorted(values)), "" if wrote else " (no change)"))

    def _answer(self, portnum, payload, request_id):
        """A reply this node made up, on its way back to the client that asked.

        From this node to this node, which is how the request arrived: it never
        went on the air and neither does this. `request_id` is the only thing
        tying the two together, since the app has no other way to tell an answer
        from an unrelated message that happened to arrive next.
        """
        tx = self.node.tx
        return mesh_packet(
            tx.node_num, tx.node_num, PRIMARY_CHANNEL, tx.next_id(),
            decoded=_data(portnum, payload, dest=tx.node_num,
                          request_id=request_id),
            rx_time=self._epoch(nodedb.seconds()))
