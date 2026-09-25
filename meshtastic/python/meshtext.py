"""Renders a decoded payload as one line of text, for `listen()`.

Split from mesh_protocol so that it is not resident on a node that never
prints one. A merged .mpy is all or nothing -- loading one allocates every
module in it -- so the only way to make this optional was to make it a file of
its own. `Payload.describe` imports it on demand and degrades to the portnum
and a byte count if it is not on the drive.

Nothing here decides anything. Every value has already been read out of the
protobuf by the Rust in meshtastic.mpy; what is left is formatting, which is
why it is the part that could be dropped.
"""

import struct

import meshtastic as _mt
from meshtastic import meshlib as mt


def describe(payload):
    """The `port=... <detail> time=... mqtt=...` line for one payload."""
    parts = ["port=%s" % mt.PORTNUM_NAMES.get(payload.portnum,
                                              "%d" % payload.portnum)]
    detail = _DETAIL.get(payload.portnum)
    if detail is not None:
        try:
            parts.append(detail(payload.body))
        except (ValueError, UnicodeError, IndexError, ArithmeticError):
            # The envelope parsed but its contents did not. Worth saying so
            # rather than dropping the packet: the portnum is still useful.
            parts.append(_kv((("error", "undecodable"),
                              ("bytes", len(payload.body)))))
    else:
        parts.append(_kv((("bytes", len(payload.body)),)))
    # Only meaningful when present, and presence is tracked because the field
    # is `optional`: an explicit 0 is the sender refusing MQTT.
    mqtt = None
    if payload.bitfield is not None:
        mqtt = "yes" if payload.bitfield & 1 else "no"
    parts.append(_kv((("time", mt.stamp(payload.time)), ("mqtt", mqtt))))
    return " ".join(parts)


#: Routing.Error, lowercased and hyphenated. Sparse above 9: the low run is
#: delivery failures, the 32+ block is refusals.
_ROUTING_ERRORS = {
    0: "none", 1: "no-route", 2: "got-nak", 3: "timeout", 4: "no-interface",
    5: "max-retransmit", 6: "no-channel", 7: "too-large", 8: "no-response",
    9: "duty-cycle-limit", 32: "bad-request", 33: "not-authorized",
    34: "pki-failed", 35: "pki-unknown-pubkey", 36: "admin-bad-session-key",
    37: "admin-public-key-unauthorized", 38: "rate-limit-exceeded",
}


#: Config.DeviceConfig.Role.
ROLE_NAMES = (
    "client", "client-mute", "router", "router-client", "repeater",
    "tracker", "sensor", "tak", "client-hidden", "lost-and-found",
    "tak-tracker", "router-late", "client-base",
)


#: HardwareModel, lowercased and hyphenated. The enum is contiguous from 0, so
#: the index is the value; 255 is the only member outside the run. Kept packed
#: in one string because 149 separate constants would cost far more in the .mpy
#: than the one split this pays for the first time a nodeinfo arrives.
#:
#: Regenerated from the schema rather than appended to: upstream renames values
#: in place, and 27, 28 and 128 have all been something else since this table
#: was last written.
_HW_NAMES = (
    "unset tlora-v2 tlora-v1 tlora-v2-1-1p6 tbeam heltec-v2-0 tbeam-v0p7 "
    "t-echo tlora-v1-1p3 rak4631 heltec-v2-1 heltec-v1 lilygo-tbeam-s3-core "
    "rak11200 nano-g1 tlora-v2-1-1p8 tlora-t3-s3 nano-g1-explorer "
    "nano-g2-ultra lora-type wiphone wio-wm1110 rak2560 heltec-hru-3601 "
    "heltec-wireless-bridge station-g1 rak11310 makerfabs-tracker "
    "makerfabs-reserved canaryone rp2040-lora station-g2 lora-relay-v1 "
    "t-echo-plus ppr genieblocks nrf52-unknown portduino android-sim diy-v1 "
    "nrf52840-pca10059 dr-dev m5stack heltec-v3 heltec-wsl-v3 "
    "betafpv-2400-tx betafpv-900-nano-tx rpi-pico heltec-wireless-tracker "
    "heltec-wireless-paper t-deck t-watch-s3 picomputer-s3 heltec-ht62 "
    "ebyte-esp32-s3 esp32-s3-pico chatter-2 heltec-wireless-paper-v1-0 "
    "heltec-wireless-tracker-v1-0 unphone td-lorac cdebyte-eora-s3 "
    "twc-mesh-v4 nrf52-promicro-diy radiomaster-900-bandit-nano "
    "heltec-capsule-sensor-v3 heltec-vision-master-t190 "
    "heltec-vision-master-e213 heltec-vision-master-e290 "
    "heltec-mesh-node-t114 sensecap-indicator tracker-t1000-e rak3172 "
    "wio-e5 radiomaster-900-bandit me25ls01-4y10td rp2040-feather-rfm95 "
    "m5stack-corebasic m5stack-core2 rpi-pico2 m5stack-cores3 seeed-xiao-s3 "
    "ms24sf1 tlora-c6 wismesh-tap routastic mesh-tab meshlink "
    "xiao-nrf52-kit thinknode-m1 thinknode-m2 t-eth-elite heltec-sensor-hub "
    "muzi-base heltec-mesh-pocket seeed-solar-node nomadstar-meteor-pro "
    "crowpanel link-32 seeed-wio-tracker-l1 seeed-wio-tracker-l1-eink "
    "muzi-r1-neo t-deck-pro t-lora-pager m5stack-reserved wismesh-tag "
    "rak3312 thinknode-m5 heltec-mesh-solar t-echo-lite heltec-v4 "
    "m5stack-c6l m5stack-cardputer-adv heltec-wireless-tracker-v2 "
    "t-watch-ultra thinknode-m3 wismesh-tap-v2 rak3401 rak6421 thinknode-m4 "
    "thinknode-m6 meshstick-1262 tbeam-1-watt t5-s3-epaper-pro tbeam-bpf "
    "mini-epaper-s3 tdisplay-s3-pro heltec-mesh-node-t096 mesh-tracker-x1 "
    "thinknode-m7 thinknode-m8 thinknode-m9 heltec-v4-r8 "
    "heltec-mesh-node-t1 station-g3 t-impulse-plus t-echo-card "
    "seeed-wio-tracker-l2 crowpanel-p4 heltec-mesh-tower-v2 meshnology-w10 "
    "heltec-rc32 heltec-rc52 heltec-rcc6 seeed-wio-tracker-l1-pro-1w "
    "meshnology-w12 meshpager-x2 t-connect-pro axiometa-genesis-mini"
)


def _kv(pairs):
    """Renders a fixed field list, absent values included as `--`.

    Every formatter emits its whole schema every time. A line that omits what it
    does not know cannot be told apart from one that never carries the field,
    and column positions that shift with content defeat both eyes and awk.
    """
    return " ".join("%s=%s" % (key, "--" if value is None else value)
                    for key, value in pairs)


def _f32(bits):
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def _protocol_text(body):
    try:
        return _kv((("text", mt._quoted(body.decode())), ("bytes", len(body))))
    except UnicodeError:
        return _kv((("text", None), ("bytes", len(body))))


def _position(body):
    lat, lon, alt, sats, precision = _mt.payload_position(body)
    if lat is not None:
        lat /= 1e7
    if lon is not None:
        lon /= 1e7
    lat_s = lon_s = err = None
    if lat is not None and lon is not None:
        digits, radius = 7, 0.0
        if precision and precision < 32:
            # The sender snapped its coordinates onto a 2**(32-precision) grid
            # before transmitting, so finer digits than that would be invented.
            quantum = (1 << (32 - precision)) / 1e7
            radius = quantum * 111320 / 2
            digits, scaled = 1, quantum
            while scaled < 1.0 and digits < 8:
                scaled *= 10.0
                digits += 1
        fmt = "%." + str(digits) + "f"
        lat_s, lon_s = fmt % lat, fmt % lon
        if radius >= 1000:
            err = "%.1fkm" % (radius / 1000)
        elif radius:
            err = "%.0fm" % radius
    return _kv((("lat", lat_s), ("lon", lon_s), ("err", err),
                ("alt", None if alt is None else "%dm" % alt),
                ("sats", sats)))


def node_name(node):
    """A node's short name if one has been overheard, else its number."""
    known = mt.NAMES.get(node)
    return known if known else "0x%08x" % node


def hardware(model):
    """Names a HardwareModel, falling back to the number for anything newer.

    The names are walked in the packed string rather than split out of it. The
    list `split` returns is ninety-odd separate string objects and outlives
    every one of them being wanted, which is kilobytes to answer a question
    that arrives once per node per three hours.
    """
    if model == 255:
        return "private-hw"
    at = 0
    for _ in range(model):
        step = _HW_NAMES.find(" ", at)
        if step < 0:
            return "hw %d" % model
        at = step + 1
    end = _HW_NAMES.find(" ", at)
    return _HW_NAMES[at:] if end < 0 else _HW_NAMES[at:end]


def _user(body):
    (long_off, long_len, short_off, short_len,
     hw_model, role, licensed) = _mt.payload_user(body)
    long_name = short_name = None
    if long_off is not None:
        long_name = body[long_off:long_off + long_len].decode()
    if short_off is not None:
        short_name = body[short_off:short_off + short_len].decode()
    if role is None:
        role_s = None
    elif role < len(ROLE_NAMES):
        role_s = ROLE_NAMES[role]
    else:
        role_s = "role%d" % role
    return _kv((("long", mt._quoted(long_name)),
                ("short", mt._quoted(short_name)),
                ("role", role_s),
                ("hw", None if hw_model is None else hardware(hw_model)),
                ("licensed", "yes" if licensed else "no")))


def _device_metrics(body):
    batt, volt, chan, tx, up = _mt.payload_device_metrics(body)
    if batt is not None:
        # The protobuf documents anything over 100 as "powered", so a node on
        # mains reports 101 rather than a battery percentage.
        batt = "powered" if batt > 100 else "%d%%" % batt
    return _kv((("batt", batt),
                ("volt", None if volt is None else "%.2fV" % _f32(volt)),
                ("chan", None if chan is None else "%.1f%%" % _f32(chan)),
                ("tx", None if tx is None else "%.1f%%" % _f32(tx)),
                ("up", None if up is None else "%ds" % up)))


def _environment_metrics(body):
    temp, rh, hpa = _mt.payload_environment_metrics(body)
    return _kv((("temp", None if temp is None else "%.1fC" % _f32(temp)),
                ("rh", None if rh is None else "%.0f%%" % _f32(rh)),
                ("hpa", None if hpa is None else "%.1f" % _f32(hpa))))


def _telemetry(body):
    found = _mt.payload_telemetry(body)
    if found is None:
        return _kv((("variant", None),))
    number, off, length = found
    if number == 2:
        return _device_metrics(body[off:off + length])
    if number == 3:
        return _environment_metrics(body[off:off + length])
    # Air quality, power, local stats and the rest share this envelope and
    # nothing else, so naming the variant is the whole of what is knowable.
    return _kv((("variant", number),))


def _hops(chunk):
    out = []
    for at in range(0, len(chunk) - 3, 4):
        node = struct.unpack("<I", chunk[at:at + 4])[0]
        # A relay that could not name a hop writes the broadcast address there.
        out.append(None if node == 0xFFFFFFFF else node)
    return out


def _snrs(chunk):
    """Packed int32s, each an int8 of quarter-dB, with -128 meaning unknown."""
    out = []
    at = 0
    end = len(chunk)
    while at < end:
        value, at = _mt.payload_snr_at(chunk, at)
        out.append(None if value == -128 else value / 4.0)
    return out


def _chain(route, snrs):
    parts = []
    for at in range(len(route) if len(route) > len(snrs) else len(snrs)):
        if at >= len(route):
            # One SNR past the route is the link into the far endpoint, which
            # the header names and the payload never does.
            name = "dst"
        elif route[at] is None:
            name = "?"
        else:
            name = node_name(route[at])
        snr = snrs[at] if at < len(snrs) else None
        parts.append(name if snr is None else "%s(%.2fdB)" % (name, snr))
    return ">".join(parts)


def _traceroute(body):
    """RouteDiscovery: who carried this each way, and how well each one heard.

    Unlike every other payload, this one is rewritten by each relay as it
    passes, so two receptions of the same packet id legitimately differ.
    """
    (route_off, route_len, out_off, out_len,
     back_off, back_len, snr_back_off, snr_back_len) = _mt.payload_traceroute(body)
    route = () if route_off is None else _hops(body[route_off:route_off + route_len])
    snr_out = () if out_off is None else _snrs(body[out_off:out_off + out_len])
    back = () if back_off is None else _hops(body[back_off:back_off + back_len])
    snr_back = (() if snr_back_off is None
                else _snrs(body[snr_back_off:snr_back_off + snr_back_len]))
    return _kv((("out", _chain(route, snr_out) if route or snr_out else None),
                ("back", _chain(back, snr_back) if back or snr_back else None)))


def _routing(body):
    """Routing: the reply that says a wanted ack arrived, or why it did not.

    The three arms are a oneof, so the selected one is always on the wire even
    when its value is zero -- which is why a plain ack is two bytes and not an
    empty payload, and why absence here means something is wrong.
    """
    kind, value = _mt.payload_routing(body)
    if kind == _mt.ROUTE_ERROR:
        return _kv((("ack", "yes" if value == 0 else "no"),
                    ("reason", _ROUTING_ERRORS.get(value, "error%d" % value))))
    if kind != _mt.ROUTE_NONE:
        # Route discovery also rides portnum 70, where it is not wrapped.
        return _kv ((("ack", None),
                     ("reason", "route-%s" % ("request"
                                              if kind == _mt.ROUTE_REQUEST
                                              else "reply"))))
    return _kv((("ack", None), ("reason", None)))


_DETAIL = {
    1: _protocol_text,
    3: _position,
    4: _user,
    5: _routing,
    67: _telemetry,
    70: _traceroute,
}
