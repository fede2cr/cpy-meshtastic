"""Narrows the pin-interrupt hang down to a single call, with no radio involved.

`radio.unwatch()` is one line -- `lr2021.detach_irq(board.LORA_DIO1)` -- and on
this board it kills the prompt even when nothing was ever attached, which is a
path that should return immediately without touching anything. Everything here
exists to find out what that call really does.

Run it from the REPL, one stage at a time:

    >>> import pintest
    >>> pintest.run()

Each stage prints its number before it runs anything, so the last number on the
wire is the stage that hung. Resume after a reset with `run(4)`.

Stages 1-9 need no radio and are what `run()` covers. Stages 10-13 bring the
node up and sit in receive, so they want a reset between them and are called by
name: `pintest.stage10()`. They exist because 1-9 all passed, which means the
call blamed for the hang is harmless on its own and the radio being live is the
only thing left that distinguishes them from the failing session.

Two things make this sharper than poking at the prompt by hand. Every risky call
is followed by `settle()`, which sleeps -- and sleeping is what runs the
scheduler and the background tasks, so damage that would otherwise surface
minutes later at the next keystroke surfaces here instead, on a numbered line,
while there is still a console to print it. And `claimed()` asks the question
`detach_irq` is supposed to answer, by trying to take the pin as an ordinary
input: that says whether the detach did its job, independently of whether it
also broke something.

BOOT is GPIO0 with a pull-up, so its edge is falling; the radio's DIO8 arrives
on LORA_DIO1 (GPIO14) and rises. Both are used, because "only the radio pin is
affected" and "any pin is affected" want very different fixes.
"""

import time

import board
import digitalio

import lr2021

#: How long to sleep after a suspect call. Any sleep at all reaches a VM safe
#: point and drains the background tasks; the length only decides how many.
SETTLE_S = 0.5

_stage = 0


def settle(seconds=SETTLE_S):
    """Sleeps, which is the only way to make the damage land where it is seen."""
    time.sleep(seconds)


def claimed(pin):
    """True if something holds the pin, decided by trying to take it."""
    try:
        io = digitalio.DigitalInOut(pin)
    except ValueError:
        return True
    io.deinit()
    return False


def _say(text):
    print("     %s" % text)


def _stage_header(number, text):
    global _stage
    _stage = number
    print("-- stage %d: %s" % (number, text))


def stage1():
    """Detach a pin that was never attached. This is the reported hang."""
    _stage_header(1, "detach_irq(LORA_DIO1), never attached")
    _say("claimed before: %s" % claimed(board.LORA_DIO1))
    _say("calling detach_irq")
    lr2021.detach_irq(board.LORA_DIO1)
    _say("returned; settling")
    settle()
    _say("claimed after: %s" % claimed(board.LORA_DIO1))


def stage2():
    """The same on a different pin, to see whether LORA_DIO1 is special."""
    _stage_header(2, "detach_irq(BUTTON), never attached")
    _say("calling detach_irq")
    lr2021.detach_irq(board.BUTTON)
    _say("returned; settling")
    settle()


def stage3():
    """An honest attach and detach on BOOT, the pin irqtest already uses."""
    _stage_header(3, "attach + detach on BUTTON")
    lr2021.attach_irq(board.BUTTON, _noop, lr2021.IRQ_FALLING)
    _say("attached, claimed: %s" % claimed(board.BUTTON))
    settle()
    lr2021.detach_irq(board.BUTTON)
    _say("detached, claimed: %s" % claimed(board.BUTTON))
    settle()


def stage4():
    """The same on the radio's line, which rises rather than falls."""
    _stage_header(4, "attach + detach on LORA_DIO1")
    lr2021.attach_irq(board.LORA_DIO1, _noop, lr2021.IRQ_RISING)
    _say("attached, claimed: %s" % claimed(board.LORA_DIO1))
    settle()
    lr2021.detach_irq(board.LORA_DIO1)
    _say("detached, claimed: %s" % claimed(board.LORA_DIO1))
    settle()


def stage5():
    """Detach the wrong pin while another is live: does it hit the live slot?"""
    _stage_header(5, "attach BUTTON, detach LORA_DIO1, detach BUTTON")
    lr2021.attach_irq(board.BUTTON, _noop, lr2021.IRQ_FALLING)
    settle()
    _say("detaching the pin that is not attached")
    lr2021.detach_irq(board.LORA_DIO1)
    settle()
    _say("BUTTON still claimed: %s" % claimed(board.BUTTON))
    lr2021.detach_irq(board.BUTTON)
    settle()
    _say("BUTTON claimed after detach: %s" % claimed(board.BUTTON))


def stage6():
    """Detach twice. The second call is the never-attached case after a real one."""
    _stage_header(6, "attach BUTTON, detach twice")
    lr2021.attach_irq(board.BUTTON, _noop, lr2021.IRQ_FALLING)
    settle()
    lr2021.detach_irq(board.BUTTON)
    _say("first detach returned")
    settle()
    lr2021.detach_irq(board.BUTTON)
    _say("second detach returned")
    settle()


def stage7(rounds=5):
    """Cycle the same slot, which is where a stale queue entry would show."""
    _stage_header(7, "attach/detach BUTTON %d times" % rounds)
    for i in range(rounds):
        lr2021.attach_irq(board.BUTTON, _noop, lr2021.IRQ_FALLING)
        lr2021.detach_irq(board.BUTTON)
        _say("round %d done" % (i + 1))
        settle(0.1)
    settle()


def stage8():
    """Fire a real edge, then detach while the handler has run at least once.

    The only stage that needs you: hold and release BOOT while it waits. An edge
    that has actually been through the scheduler is the case that distinguishes
    a slot that was merely armed from one that has been used.
    """
    _stage_header(8, "edge on BUTTON, then detach -- press BOOT")
    global _fired
    _fired = 0
    lr2021.attach_irq(board.BUTTON, _count, lr2021.IRQ_FALLING)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _fired == 0:
        time.sleep(0.05)
    _say("edges seen: %d" % _fired)
    lr2021.detach_irq(board.BUTTON)
    _say("detached after %d edges" % _fired)
    settle()


def stage9():
    """Take every slot and give them all back, in case exhaustion is involved."""
    _stage_header(9, "fill all interrupt slots and release")
    pins = (board.BUTTON, board.LORA_DIO1, board.LORA_DIO7, board.GPS_PPS)
    taken = []
    for pin in pins:
        try:
            ok = lr2021.attach_irq(pin, _noop, lr2021.IRQ_RISING)
        except (ValueError, RuntimeError) as error:
            _say("%s refused: %s" % (pin, error))
            continue
        _say("%s attached: %r" % (pin, ok))
        taken.append(pin)
    settle()
    for pin in taken:
        lr2021.detach_irq(pin)
        _say("%s detached" % pin)
    settle()


_fired = 0


def _noop(pin):
    pass


def _count(pin):
    global _fired
    _fired += 1


def soak(seconds=20):
    """Sleeps in one-second ticks, printing each one.

    The stages below can take a while to die, and a plain sleep cannot tell
    "died instantly" from "died after eleven seconds". The tick that fails to
    print is the answer, and a time-dependent death means traffic, not a call.
    """
    for tick in range(seconds):
        time.sleep(1)
        print("     t+%ds" % (tick + 1))


def _radio():
    """The node's own radio, brought up exactly as the failing session did."""
    from meshtastic import meshlib as mesh
    return mesh.start(watch=False).radio


def stage10(seconds=20):
    """Receiving, nothing watched, nothing detached. The control that matters.

    If this dies, `unwatch()` was never the cause and the board cannot sit in Rx
    at the prompt at all -- which would mean a packet arriving is what kills it.
    """
    _stage_header(10, "radio listening, no detach, soak %ds" % seconds)
    radio = _radio()
    radio.listen()
    _say("listening")
    soak(seconds)


def stage11(seconds=20):
    """The same but in standby, so the receiver cannot raise anything."""
    _stage_header(11, "radio in standby, soak %ds" % seconds)
    radio = _radio()
    radio.standby()
    _say("in standby")
    soak(seconds)


def stage12(seconds=20):
    """Receiving, then the detach that was blamed. Compare with stage 10."""
    _stage_header(12, "radio listening, detach_irq, soak %ds" % seconds)
    radio = _radio()
    radio.listen()
    _say("listening; detaching a pin nothing attached")
    radio.unwatch()
    _say("unwatch returned")
    soak(seconds)


def stage13(seconds=20):
    """Receiving and genuinely watched, which is what the node actually wants."""
    _stage_header(13, "radio listening and watched, soak %ds" % seconds)
    from meshtastic import meshlib as mesh
    radio = mesh.start(watch=False).radio
    radio.watch(_count)
    radio.listen()
    _say("watched and listening")
    soak(seconds)
    _say("edges seen: %d" % _fired)


STAGES = (stage1, stage2, stage3, stage4, stage5,
          stage6, stage7, stage8, stage9,
          stage10, stage11, stage12, stage13)


# The nine stages above all passed and then Ctrl-D hung, so the damage is not
# visible while the VM is running and only lands in teardown. Each of these does
# one thing and stops; press Ctrl-D afterwards and report whether it reboots.
# Run them one per reset, and with lib/repl.py renamed, or its mesh.start() has
# already attached an interrupt before the prompt appears.

def td1():
    """Control: touches nothing. If Ctrl-D hangs here, none of this is mine."""
    _say("nothing done; press Ctrl-D")


def td2():
    """Attached and left attached, so teardown finds a live slot."""
    lr2021.attach_irq(board.BUTTON, _noop, lr2021.IRQ_FALLING)
    _say("attached and left that way; press Ctrl-D")


def td3():
    """Attached and detached, so teardown finds an empty slot that was used."""
    lr2021.attach_irq(board.BUTTON, _noop, lr2021.IRQ_FALLING)
    lr2021.detach_irq(board.BUTTON)
    _say("attached then detached; press Ctrl-D")


def td4(seconds=10):
    """An edge really fired, then detached: the scheduler node has been used."""
    global _fired
    _fired = 0
    lr2021.attach_irq(board.BUTTON, _count, lr2021.IRQ_FALLING)
    _say("press BOOT now")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and _fired == 0:
        time.sleep(0.1)
    lr2021.detach_irq(board.BUTTON)
    _say("edges %d, detached; press Ctrl-D" % _fired)


def td5(seconds=10):
    """An edge really fired and the watch is left in place."""
    global _fired
    _fired = 0
    lr2021.attach_irq(board.BUTTON, _count, lr2021.IRQ_FALLING)
    _say("press BOOT now")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and _fired == 0:
        time.sleep(0.1)
    _say("edges %d, still attached; press Ctrl-D" % _fired)


# Stages 10-13 all die, including 11, which has no watch, no detach and a radio
# in standby. So the interrupt was never involved and `mesh.start()` plus time
# is the whole trigger. These split that into its layers, cheapest first.

def s14(seconds=30):
    """Control: the soak alone, with no radio and no meshlib imported.

    If this dies, the printing or the sleeping is the problem and every result
    from stages 10-13 says nothing about the radio.
    """
    _stage_header(14, "soak %ds with nothing brought up" % seconds)
    soak(seconds)


def s15(seconds=30):
    """meshlib imported and the adapter built, but the chip never spoken to.

    `open_radio` constructs; it does not claim pins or power anything.
    """
    _stage_header(15, "adapter built, not opened, soak %ds" % seconds)
    from meshtastic import meshlib as mesh
    radio = mesh.open_radio("lr2021")
    _say("built %s" % radio.description)
    soak(seconds)


def s16(seconds=30):
    """The radio opened: pins claimed, VEXT on, the PA enabled, chip reset.

    This is the first stage that draws current, and the last one before any of
    the node, the keystore or NVM is involved.
    """
    _stage_header(16, "radio opened, untuned, soak %ds" % seconds)
    from meshtastic import meshlib as mesh
    radio = mesh.open_radio("lr2021")
    radio.open()
    _say("open: %s, state %s" % (radio.description, radio.state()))
    soak(seconds)


# s14 tore down fine and s15 did not, and all 30 ticks printed in both: nothing
# fails while code runs, only in teardown. s15 constructs the adapter, whose
# __init__ builds the SPI, spins on try_lock() and never unlocks it, then claims
# five pins. The ladder below takes that apart in plain CircuitPython, with no
# meshlib and no natmod, so a hang here belongs to the port and not to us.
#
# References go in _held: a local would be collected before Ctrl-D and the
# object under test would not survive to teardown.

_held = []


def s17():
    """meshlib imported, nothing built. Splits the import from the adapter."""
    _stage_header(17, "import meshtastic.meshlib only")
    from meshtastic import meshlib
    _held.append(meshlib)
    _say("imported; press Ctrl-D")


def s18():
    """A bare SPI bus, never locked."""
    _stage_header(18, "busio.SPI, unlocked")
    import busio
    _held.append(busio.SPI(board.LORA_SCK, board.LORA_MOSI, board.LORA_MISO))
    _say("built; press Ctrl-D")


def s19():
    """Locked and left locked, which is the state the adapter leaves behind."""
    _stage_header(19, "busio.SPI, locked and left locked")
    import busio
    spi = busio.SPI(board.LORA_SCK, board.LORA_MOSI, board.LORA_MISO)
    while not spi.try_lock():
        pass
    spi.configure(baudrate=8_000_000, polarity=0, phase=0)
    _held.append(spi)
    _say("locked; press Ctrl-D")


def s20():
    """The same, unlocked again before the prompt. Isolates the lock itself."""
    _stage_header(20, "busio.SPI, locked then unlocked")
    import busio
    spi = busio.SPI(board.LORA_SCK, board.LORA_MOSI, board.LORA_MISO)
    while not spi.try_lock():
        pass
    spi.configure(baudrate=8_000_000, polarity=0, phase=0)
    spi.unlock()
    _held.append(spi)
    _say("unlocked; press Ctrl-D")


def s21():
    """The five pins the adapter claims, with no bus involved."""
    _stage_header(21, "the adapter's five pins, no SPI")
    for pin, value in ((board.LORA_CS, True), (board.LORA_RESET, True),
                       (board.VEXT_ENABLE, False), (board.PA_EN_SUBGHZ, True)):
        io = digitalio.DigitalInOut(pin)
        io.switch_to_output(value=value)
        _held.append(io)
    io = digitalio.DigitalInOut(board.LORA_BUSY)
    io.switch_to_input()
    _held.append(io)
    _say("claimed; press Ctrl-D")


def s22():
    """VEXT_ENABLE alone, which is GPIO45: the VDD_SPI strapping pin.

    Teardown runs filesystem_flush(), so a claimed flash-rail strap and a flash
    write meet in the same code path.
    """
    _stage_header(22, "VEXT_ENABLE (GPIO45) alone, driven low")
    io = digitalio.DigitalInOut(board.VEXT_ENABLE)
    io.switch_to_output(value=False)
    _held.append(io)
    _say("claimed; press Ctrl-D")


def s23():
    """PA_EN_SUBGHZ alone, the other pin left driven."""
    _stage_header(23, "PA_EN_SUBGHZ (GPIO4) alone, driven high")
    io = digitalio.DigitalInOut(board.PA_EN_SUBGHZ)
    io.switch_to_output(value=True)
    _held.append(io)
    _say("claimed; press Ctrl-D")


def run(first=1, last=9):
    """Runs the stages in order. The last number printed is the one that hung.

    Stops at 9 by default: 10 and up bring the radio up and cache a node inside
    `meshlib`, so they want a reset between them and are run by name.
    """
    # On the panel too: a stage that hangs takes the USB console with it, and
    # then the screen is the only thing still saying which one it was.
    _mirror()
    for number in range(first, last + 1):
        STAGES[number - 1]()
        print("   stage %d survived" % number)
    print("stages %d-%d survived" % (first, last))


def _mirror(on=True):
    """Echoes what this prints onto the OLED, if the drive has one."""
    try:
        from meshtastic import oled
    except ImportError:
        return
    oled.tee(on)
