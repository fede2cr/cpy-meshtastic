"""Writing protobuf, which is the half `meshtastic.fields` does not do.

Reading and writing are both in Rust now, but for different reasons. What
arrives from the air or from a phone is hostile input, where a length field can
lie, so it is parsed by a strict walker that refuses anything malformed. What
leaves cannot be malformed, and moved across for a plainer reason: built out of
Python bytes objects, one field cost five short-lived allocations, and sending
the roster to a phone writes several hundred fields in a burst at the moment
the heap is smallest. What is left here is the proto3 emptiness rule, which is
a decision rather than a byte layout.

Proto3 omits a scalar that holds its type's default, and every other node does
the same, so a message built here is byte-identical to the same one built by
nanopb. The exception is a `oneof` member, where the point of the field is that
it is present at all -- an empty `Config.device` says "these are the device
settings and they are all default", while omitting it says nothing. That is
what `always` is for, and why `message` has no such switch: a submessage is
only ever written when it was chosen.

There is no schema here and no code generation, for the same reason the reader
has none. Field numbers appear at the call site, next to the message they
belong to, where they can be checked against the .proto by eye.
"""

import struct

import meshtastic as _mt

#: Room for the largest message anyone builds here: a `FromRadio` wrapping a
#: `MeshPacket` that carries a full 237-byte payload. Writing past it is refused
#: by the Rust side rather than truncated, so this being too small is an error
#: and never a corrupt message.
_SIZE = 384

#: The buffer, when nobody is using it. A message is built in it and copied out
#: once at the end, so the fields themselves cost nothing.
_free = bytearray(_SIZE)


def msg_buffer():
    """A buffer to build one message in. Give it back to `msg_take`.

    Almost always the shared one. A second build started while the first is
    still open -- an encoder called to make a submessage of the message being
    written -- gets its own instead, because handing out the same bytearray
    twice would have the inner message overwrite the outer one's fields.
    """
    global _free
    buf = _free
    if buf is None:
        return bytearray(_SIZE)
    _free = None
    return buf


def msg_take(buf, at):
    """The finished message. The one allocation a message costs."""
    global _free
    out = _mt.pb_take(buf, at)
    if _free is None and len(buf) == _SIZE:
        _free = buf
    return out


def uint(buf, at, number, value, always=False):
    if not value and not always:
        return at
    return _mt.pb_uint(buf, at, number, value)


def int32(buf, at, number, value, always=False):
    if not value and not always:
        return at
    return _mt.pb_int32(buf, at, number, value)


def boolean(buf, at, number, value, always=False):
    return uint(buf, at, number, 1 if value else 0, always)


def fixed32(buf, at, number, value, always=False):
    if not value and not always:
        return at
    return _mt.pb_fixed32(buf, at, number, value & 0xFFFFFFFF)


def float32(buf, at, number, value, always=False):
    if not value and not always:
        return at
    # Floats do not cross into the native module, so the bit pattern goes
    # instead and is written as a plain fixed32.
    bits = struct.unpack("<I", struct.pack("<f", value))[0]
    return _mt.pb_fixed32(buf, at, number, bits)


def blob(buf, at, number, data, always=False):
    if not data and not always:
        return at
    at = _mt.pb_blob_head(buf, at, number, len(data))
    end = at + len(data)
    # Checked because a bytearray grows to fit a slice assignment that does not,
    # which would move the buffer and silently change its size.
    if end > len(buf):
        raise ValueError("message does not fit")
    buf[at:end] = data
    return end


def string(buf, at, number, text, limit=None, always=False):
    raw = text.encode("utf-8")
    if limit is not None:
        raw = raw[:limit]
    return blob(buf, at, number, raw, always)


def nested(buf, at, number, body):
    """A submessage of the message being built. Always written, even when
    empty: see the module docstring."""
    return blob(buf, at, number, body, always=True)


def message(number, body):
    """A message that is nothing but one submessage, as an envelope is."""
    buf = msg_buffer()
    return msg_take(buf, nested(buf, 0, number, body))


def one_blob(number, data):
    """A message that is nothing but one length-delimited field."""
    buf = msg_buffer()
    return msg_take(buf, blob(buf, 0, number, data, always=True))


def one_uint(number, value):
    """A message that is nothing but one varint field."""
    buf = msg_buffer()
    return msg_take(buf, uint(buf, 0, number, value, always=True))
