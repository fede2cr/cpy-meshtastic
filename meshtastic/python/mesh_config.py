"""The node's settings, which live in NVM.

They used to live in settings.toml, and the reason they moved is that a text
file on the CIRCUITPY volume can only be edited by a host: the board cannot
change its own settings while it is running, which is exactly what a node has to
do when someone retunes it from a REPL or, later, from the app. NVM it can
write. So NVM is where region, preset, channel and power now are, alongside the
key and the node number that were always there.

A handful are worth saying out loud rather than remembering, so `code.py`
declares them and they arrive here as arguments to `config_provision`. Those are
re-applied at every boot: the file is the statement of what this node is, and
editing it and resetting is how you change one. Everything else is filled in
once, from the defaults below, and is then the board's to change -- from the
prompt with `configure()`, and never overwritten from outside again.

Re-applying costs nothing when nothing moved. `keystore_save` compares the
encoded store against what is already in NVM and returns without writing if they
match, so a boot that changes no setting spends no flash erase cycle.

Every setting has a default here, so a board that declares nothing still comes
up as a stock US LongFast listener rather than failing. Names are validated on
the way in, by `config_configure`, rather than on the way out: a typo should be
refused at the prompt or the boot that made it, not at the next one.
"""

import binascii
import struct

from meshtastic import meshlib as keystore
from meshtastic import meshlib as mt

#: Meshtastic's default channel key, from Channels.cpp at the pinned tag. Every
#: stock node ships with it, so it keeps unrelated traffic out and nothing else.
DEFAULT_KEY = (b"\xd4\xf1\xbb\x3a\x20\x29\x07\x59"
               b"\xf0\xbc\xff\xab\xcf\x4e\x69\x01")

DEFAULT_REGION = "US"
DEFAULT_PRESET = "LongFast"
DEFAULT_KEY_SPEC = "AQ=="
#: The firmware's own, from Default.h: three hours, and never faster than one
#: however it is configured.
DEFAULT_NODEINFO_S = 3 * 60 * 60
MIN_NODEINFO_S = 3600

_HEX_DIGITS = "0123456789abcdefABCDEF"


def _looks_hex(text):
    # 32 characters is ambiguous: valid hex is also valid base64. Hex wins,
    # because base64 of that length decodes to 24 bytes, which is not a key size.
    if len(text) not in (2, 32, 64):
        return False
    for ch in text:
        if ch not in _HEX_DIGITS:
            return False
    return True


def channel_key(spec):
    """Turns a written or wire-format channel key into raw AES key bytes.

    Accepts base64, as the apps display it, bare hex, or the bytes themselves
    as a `psk` arrives from the phone. A single byte is not a key but an index
    into the default one, which is how Meshtastic writes the stock channels
    compactly: 0 means unencrypted, 1 means the default key itself, and n
    advances its last byte by n-1.
    """
    if not isinstance(spec, str):
        return _key_bytes(bytes(spec))
    spec = spec.strip()
    if not spec:
        return b""
    return _key_bytes(binascii.unhexlify(spec.encode()) if _looks_hex(spec)
                      else binascii.a2b_base64(spec.encode()))


def _key_bytes(raw):
    if not raw:
        return b""
    if len(raw) == 1:
        if raw[0] == 0:
            return b""
        key = bytearray(DEFAULT_KEY)
        key[15] = (key[15] + raw[0] - 1) & 0xFF
        return bytes(key)
    if len(raw) not in (16, 32):
        raise ValueError(
            "channel key must be 16 or 32 bytes, got %d" % len(raw))
    return bytes(raw)


def _config_text(records, tag, default):
    raw = records.get(tag)
    return default if raw is None else str(raw, "utf-8")


def _number(records, tag, default):
    raw = records.get(tag)
    return default if raw is None else struct.unpack("<I", raw)[0]


class Config:
    """Everything the node stores about itself, already parsed and checked."""

    def __init__(self):
        stored = keystore.keystore_load()
        self.region = _config_text(stored, keystore.REGION, DEFAULT_REGION)
        self.preset = mt.preset_index(
            _config_text(stored, keystore.PRESET, DEFAULT_PRESET))
        self.channel_name = _config_text(stored, keystore.CHANNEL_NAME, "") or None
        self.channel_num = _number(stored, keystore.CHANNEL_NUM, 0)
        key = stored.get(keystore.CHANNEL_KEY)
        self.key_source = "nvm" if key is not None else "default"
        self.channel_key = (bytes(key) if key is not None
                            else channel_key(DEFAULT_KEY_SPEC))
        self.node_num = _number(stored, keystore.NODE_NUM, 0) or None
        # 0 means "the region's maximum", which only the region table knows.
        self.tx_power_dbm = _number(stored, keystore.TX_POWER_DBM, 0) or None
        # 0 means "the region's", which only the region table knows.
        self.duty_cycle_pct = (
            _number(stored, keystore.DUTY_CYCLE_PCT, 0) or None)
        self.nodeinfo_s = max(MIN_NODEINFO_S, _number(
            stored, keystore.NODEINFO_S, DEFAULT_NODEINFO_S))

    def settings(self):
        """The radio settings this configuration resolves to."""
        return mt.Settings(
            mt.region_index(self.region), self.preset,
            channel_name=self.channel_name, channel_num=self.channel_num,
        )


def _checked(values):
    """Validates a batch of settings, or raises. Returns them ready to store.

    Checked as a whole and before anything is written: a call that names one
    bad value stores none of them. The alternative is a board that refuses to
    boot because of a typo, with no prompt left to correct it from.
    """
    checked = {}
    for name, value in values.items():
        if value is None:
            checked[name] = None
        elif name == "region":
            mt.region_index(value)
            checked[name] = value
        elif name == "preset":
            mt.preset_index(value)
            checked[name] = value
        elif name == "channel_key":
            checked[name] = channel_key(value)
        elif name == "channel_name":
            checked[name] = str(value)
        elif name in ("long_name", "short_name"):
            # Not length-checked here: `nodeinfo` truncates on the way onto the
            # air, where the limit actually is, and a name is not worth
            # refusing.
            checked[name] = str(value)
        elif name == "duty_cycle_pct":
            if not 0 <= value <= 100:
                raise ValueError("duty cycle is a percentage, got %r" % (value,))
            checked[name] = int(value)
        elif name in ("channel_num", "tx_power_dbm", "nodeinfo_s", "node_num"):
            if value < 0:
                raise ValueError("%s cannot be negative" % name)
            checked[name] = int(value)
        else:
            raise ValueError("no such setting %r" % (name,))
    return checked


def config_configure(**values):
    """Stores settings. Returns True if flash was written.

    Names are `keystore.TAG_NAMES`; `channel_key` also accepts a written key in
    either of the forms `channel_key` reads. Passing None removes a record,
    which restores the default rather than leaving a hole.

    A setting `code.py` declares can be changed here, and goes back to what the
    file says at the next reset. That is the point of declaring it: the file is
    what the node is, and this is what it is doing for now.
    """
    return keystore.keystore_provision(**_checked(values))


def config_provision(**declared):
    """Applies declared settings and fills in whatever else is missing.

    True if flash was actually written. Declared settings are written at every
    boot; the rest are generated only when absent, so anything set from the
    prompt survives. Both are safe to run at every boot because an unchanged
    store is not rewritten.
    """
    stored = keystore.keystore_load()
    fresh = _checked(declared)
    if keystore.CHANNEL_KEY not in stored:
        fresh["channel_key"] = channel_key(DEFAULT_KEY_SPEC)
    if keystore.NODE_NUM not in stored:
        fresh["node_num"] = keystore.node_number()
    if not fresh:
        return False
    return keystore.keystore_provision(**fresh)


def config_describe():
    """The settings as stored, for a prompt. Values, since none are secret."""
    cfg = Config()
    return ("region %s, preset %s, channel %s slot %d, %s dBm, %s%%, "
            "nodeinfo %dm" % (
                cfg.region, mt.PRESET_NAMES[cfg.preset],
                cfg.channel_name or "unnamed", cfg.channel_num,
                cfg.tx_power_dbm if cfg.tx_power_dbm else "region",
                cfg.duty_cycle_pct if cfg.duty_cycle_pct else "region",
                cfg.nodeinfo_s // 60))
