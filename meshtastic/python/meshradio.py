"""What the node needs from a LoRa radio, and nothing about any one chip.

The node used to import `lr1121` directly, which quietly made an LR1121 part of
what Meshtastic *is* on this board. It is not: Meshtastic runs on SX1262,
SX1276, SX1280 and LLCC68 parts too, and CircuitPython boards carry all of
them. Everything below is stated in the units the air uses -- hertz, spreading
factor, dBm -- because those are the same on every part. Register codes are the
adapter's business.

An adapter is a plain object with the attributes listed in `Radio`. There is no
base class to inherit: CircuitPython pays for every class in the chain at import
time, and a contract this small is cheaper to read than to enforce.

To add a part, write an `open_<family>()` beside this file that returns such an
object, then name it in the MESH_RADIO setting. A driver kept outside the
library works too, as `radio_<family>.py` with an `open_radio()`. The node has
no other opinion about which chip it is talking to.
"""

import supervisor

#: The adapter used when MESH_RADIO says nothing. A board that carries one radio
#: should not have to be told twice which one it is.
DEFAULT = "lr11xx"


class Radio:
    """The contract. Documentation, not a base class -- nothing inherits it.

    Bring-up
      `description`   what the part said it was, for the log header.
      `open()`        power up, verify identity, leave in standby. Raises if
                      the part is absent or is not the one claimed.
      `close()`       release the bus and the pins.

    Tuning
      `tune(...)`     one call, because a half-applied channel is not a state
                      worth being able to reach. Takes `frequency_hz`, `sf`,
                      `bw_hz`, `cr` (5..8, meaning 4/5..4/8), `preamble`,
                      `sync_word`, `power_dbm` and `rx_boosted`.
      `power_ceiling(frequency_hz)`
                      the most this part can produce in that band, which is not
                      what the region permits. The smaller of the two wins.
      `power_dbm`     what is actually set, after the part has rounded it.

    Traffic
      `listen()`      continuous receive, the resting state.
      `listening`     whether it is in it.
      `standby()`     stop receiving without powering down.
      `send(data)`    True if the packet left, False on the part's own timeout.
      `collect()`     one received packet, or None. Non-blocking.
      `quality()`     (rssi_dbm, snr_db, signal_rssi_dbm) for the last packet.
      `noise()`       the current floor in dBm, with nothing being received.
      `busy()`        channel activity detection: True if someone is talking.

    Odds and ends
      `entropy()`     an integer from the part's noise, for packet ids.
      `state()`       the current mode, named, for a status line.
      `counters()`    (received, crc_errors, header_errors, false_syncs).
      `reset_counters()`
      `watch(handler)` / `unwatch()`
                      the interrupt seam. No pin argument: which line the part's
                      interrupt DIO reaches is board wiring, and the adapter is
                      the only thing here that knows it. See `mesh.py` for why it
                      is registered from Python rather than from the native
                      module.
    """


#: `supervisor.ticks_ms()` wraps at 2**29, so a plain subtraction is wrong for
#: about a fortnight in every seventy-two days. Every caller here is timing a
#: radio operation, which is why this lives with the radio and not with the
#: roster's wall clock.
_TICKS_PERIOD = 1 << 29
_TICKS_HALF = _TICKS_PERIOD // 2


def ticks_diff(a, b):
    """`a - b`, correct across the wrap. Negative means `a` came first."""
    return ((a - b + _TICKS_HALF) & (_TICKS_PERIOD - 1)) - _TICKS_HALF


def deadline(timeout_ms):
    """A tick value `timeout_ms` from now, or None for no deadline."""
    if timeout_ms is None:
        return None
    return (supervisor.ticks_ms() + timeout_ms) & (_TICKS_PERIOD - 1)


def expired(at):
    return at is not None and ticks_diff(supervisor.ticks_ms(), at) >= 0


def open_radio(name=None):
    """The radio this board carries, brought up and left in standby.

    An adapter merged into this library is found by name among its own globals;
    one shipped as a separate radio_<name>.py file is imported, so a driver for
    a part this library has never heard of needs no change here.
    """
    if name is None:
        name = supervisor.get_setting("MESH_RADIO", DEFAULT)
    factory = globals().get("open_" + name)
    if factory is None:
        try:
            factory = __import__("radio_" + name).open_radio
        except ImportError:
            raise ValueError(
                "no driver for radio %r; expected open_%s in the library, or a "
                "radio_%s module in lib" % (name, name, name))
    return factory()
