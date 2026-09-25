"""The part drivers, against a recording stand-in for the native modules.

`hosttest.py` fakes `meshradio`'s contract rather than any particular radio,
and `hoststub` leaves the part drivers out for the same reason: from up there a
chip register written at the wrong moment is invisible. This file is the other
half of that split, and it exists because one such register cost the mesh every
node info it should have heard.

The register is the LoRa payload length. The part has one field for both
directions -- transmitting sets it to the length going out, receiving reads it
as the largest payload the modem will accept -- so a driver that arms it for a
transmit and never re-arms it for the next receive leaves the receiver deaf to
everything longer than the last thing it said. Nothing reports this. The modem
abandons the packet while decoding its header, before the payload is
demodulated, so there is no interrupt, no CRC error, and no counter moving
except the chip's own header-error tally. On a Meshtastic mesh, where node info
is the one routinely large packet, the symptom is a node that hears telemetry
and positions for days and never learns anybody's name.

    make test          # runs this
    venv/bin/python tools/radiotest.py
"""

import sys
import types

sys.path.insert(0, "tools")

import hoststub  # noqa: F401  -- stubs the board and merges the library

#: The part drivers live beside the library rather than in it, and `hoststub`
#: does not put them on the path because nothing it tests imports them.
sys.path.insert(0, "meshtastic/python")
sys.path.insert(0, "lr1121/python")
sys.path.insert(0, "../lr1121/python")

#: `busio` and `digitalio` are the board modules `hoststub` deliberately does
#: not stand in for. The drivers only ever hand the bus straight back to the
#: native module, which is recorded here, so empty placeholders are the whole
#: of what is needed.
for _name in ("busio", "digitalio"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

#: The drivers time their own transmits, which nothing `hosttest` covers does,
#: so `hoststub`'s supervisor has no clock. A real advancing one rather than a
#: constant: a driver waiting on an interrupt that never comes has to be able
#: to reach its deadline, or a regression here would hang instead of failing.
if not hasattr(sys.modules["supervisor"], "ticks_ms"):
    import time as _time

    sys.modules["supervisor"].ticks_ms = (
        lambda: (_time.monotonic_ns() // 1_000_000) & ((1 << 29) - 1))

#: On a board `meshradio` is merged into `meshlib`, so the drivers reach the
#: tick arithmetic through it. `hoststub` leaves `meshradio` out of its merge
#: because it once pulled the whole radio stack in with it; it no longer does,
#: importing nothing but `supervisor`, so the real thing is folded in here
#: rather than reimplemented. A second copy of a wrap-around comparison is
#: exactly the kind of thing that drifts and then only fails after 6 days.
import meshradio as _meshradio  # noqa: E402
from meshtastic import meshlib as _meshlib  # noqa: E402

if not hasattr(_meshlib, "ticks_diff"):
    _meshlib.ticks_diff = _meshradio.ticks_diff

FAILED = []



def check(label, got, want):
    ok = got == want
    print("%-44s %s%s" % (label, got, "" if ok else "   WANT %s" % (want,)))
    if not ok:
        FAILED.append(label)


class Native(types.ModuleType):
    """Answers to any native entry point and remembers the call."""

    def __init__(self, name, **returns):
        types.ModuleType.__init__(self, name)
        self.calls = []
        self.returns = returns

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            # The bus handle leads every call and says nothing about intent.
            self.calls.append((name, args[1:]))
            return self.returns.get(name, 0)
        return record

    def names(self, since=0):
        return [name for name, _args in self.calls[since:]]

    def lengths(self):
        """The payload length field, once per time it was written."""
        return [args[2] for name, args in self.calls
                if name == "lora_set_packet_params"]


def ordered(names, *wanted):
    """True if `wanted` appears in `names`, in that order."""
    at = -1
    for name in wanted:
        if name not in names[at + 1:]:
            return False
        at = names.index(name, at + 1)
    return True


#: What this node's own node info weighs on the air. The number matters: it is
#: the ceiling a broken driver leaves behind, and it sits just under the
#: shortest node info a neighbour carrying a public key can send, which is why
#: the mesh looked alive while the names never arrived.
OWN_NODEINFO_LEN = 57

#: Driver directories that are separate repositories. The LR1121 half of this
#: file tests a file that is published from cpy-lr1121 and is only here while
#: the tree still carries it; the LR2021 half tests `radio_lr2021.py`, which is
#: this library's own adapter and stays. So a missing LR1121 driver is a skip
#: rather than a failure, and the run says which halves it actually proved.
SKIPPED = []


# ------------------------------------------------------------------- lr1121

def lr1121_driver():
    # GetStatus with TxDone latched, so transmit() returns rather than spinning
    # to its deadline.
    native = Native("_lr1121", get_status=(0, 0, 1 << 2))
    native.wait_ready = lambda *a, **k: 0
    sys.modules["_lr1121"] = native
    try:
        import lr1121 as lr
    except ImportError as err:
        SKIPPED.append("lr1121 (%s)" % err)
        print("lr1121 driver not in this tree, skipped")
        print("  to test it, put the driver on the path:")
        print("  PYTHONPATH=../cpy-lr1121/python venv/bin/python tools/radiotest.py")
        return

    # Built field by field rather than through begin(), which wants a real SPI
    # bus and a real part to answer it. What is under test is the ordering of
    # two methods, and that needs the state they read, not the chip.
    radio = object.__new__(lr.LR1121)
    radio._bus = ("spi",)
    radio._packet_type = lr.PACKET_TYPE_LORA
    radio._power_dbm = 22
    radio._header = lr.LORA_HEADER_EXPLICIT
    radio._implicit_length = 0
    radio._preamble_length = 16
    radio._crc = True
    radio._invert_iq = False
    radio._receiving = None

    radio.transmit(bytes(OWN_NODEINFO_LEN))
    check("lr1121 transmit arms its own length",
          native.lengths()[-1], OWN_NODEINFO_LEN)
    check("lr1121 transmit keeps the configuration",
          (radio._preamble_length, radio._implicit_length), (16, 0))

    mark = len(native.calls)
    radio.start_receive(lr.RX_CONTINUOUS)
    check("lr1121 receive lifts the ceiling", native.lengths()[-1], 0xFF)

    # Packet params are only accepted in standby, and only help if they land
    # before the receiver starts.
    check("lr1121 re-arms in standby, before SetRx",
          ordered(native.names(mark),
                  "set_standby", "lora_set_packet_params", "set_rx"), True)

    # Implicit header mode carries no length on air, so there the configured
    # length is the one the receiver has to keep.
    radio._header = lr.LORA_HEADER_IMPLICIT
    radio._implicit_length = 32
    radio.transmit(bytes(32))
    radio.start_receive(lr.RX_CONTINUOUS)
    check("lr1121 implicit keeps its exact length", native.lengths()[-1], 32)


# ------------------------------------------------------------------- lr2021

def lr2021_driver():
    import radio_lr2021 as rl

    native = Native("lr2021", get_and_clear_irq_status=rl.IRQ_TX_DONE)
    # Bound by open_lr2021() on a board, which imports the real natmod.
    rl._lr = native

    radio = object.__new__(rl.LR2021)
    radio._bus = ("spi",)
    radio._preamble = 16
    radio._sf, radio._bw_hz, radio._cr = 11, 250_000, 5
    radio._power_dbm = 22
    radio._listening = False
    radio._mode = "standby"
    radio.gain_mode = rl.GAIN_MODE_LF

    check("lr2021 sends what it was given",
          radio.send(bytes(OWN_NODEINFO_LEN)), True)
    check("lr2021 transmit arms its own length",
          native.lengths()[-1], OWN_NODEINFO_LEN)

    mark = len(native.calls)
    radio.listen()
    check("lr2021 receive lifts the ceiling", native.lengths()[-1], 0xFF)
    check("lr2021 re-arms before SetRx",
          ordered(native.names(mark), "lora_set_packet_params", "set_rx"), True)


lr1121_driver()
print()
lr2021_driver()

print()
if FAILED:
    print("FAILED: %s" % ", ".join(FAILED))
    sys.exit(1)
if SKIPPED:
    # Named rather than silent: a run that skipped every driver proves nothing,
    # and a green tick that means "tested nothing" is worse than a red one.
    print("radio tests pass (skipped: %s)" % ", ".join(SKIPPED))
else:
    print("radio tests pass")
