"""Proves the pin-interrupt path, from the GPIO edge to Python at the prompt.

Run it from the REPL rather than as code.py:

    >>> import irqtest
    >>> irqtest.watch()

then press the BOOT button. Each press should print a line *without* anything
polling for it -- the handler is called by the scheduler at a VM safe point, and
the idle REPL is one. That is the property the mesh node needs: LoRa arrives on
the radio's DIO line while the prompt is waiting for a keystroke.

BOOT is GPIO0 with a pull-up, so a press is a falling edge. The radio's DIO8
rises instead, which is why the edge is an argument.

`presses` is deliberately mutated from the handler and read from the prompt: if
the count moves while you are typing, the deferral through
`port_background_task()` is working end to end.
"""

import board
import lr2021

presses = 0
_watching = None


def _edge(name):
    """The named edge, or a legible complaint about a stale lib/lr2021.mpy.

    Resolved on use rather than as a default argument, so a module without the
    constants still imports and can be inspected from the prompt.
    """
    try:
        return getattr(lr2021, name)
    except AttributeError:
        raise RuntimeError(
            "lib/lr2021.mpy has no %s, so it predates attach_irq. Copy the one "
            "from dist-w12/lib (2845 bytes); a 2553-byte file is the old build "
            "left in dist-w12-probe. It exports: %s" % (name, dir(lr2021)))


def _pressed(pin):
    global presses
    presses += 1
    print("BOOT pressed, %d so far" % presses)


def watch(pin=None, edge=None):
    """Starts watching, and returns the pin so `unwatch()` needs no argument."""
    global _watching
    if pin is None:
        pin = board.BUTTON
    if edge is None:
        edge = _edge("IRQ_FALLING")
    if _watching is not None:
        unwatch()
    # The point of this test is to press a button and look somewhere else for
    # the result, which the panel does better than a console does.
    _mirror()
    lr2021.attach_irq(pin, _pressed, edge)
    _watching = pin
    print("watching %s -- press BOOT" % pin)
    return pin


def _mirror(on=True):
    """Echoes what this prints onto the OLED, if the drive has one."""
    try:
        from meshtastic import oled
    except ImportError:
        return
    oled.tee(on)


def unwatch():
    global _watching
    if _watching is not None:
        lr2021.detach_irq(_watching)
        _watching = None
    _mirror(False)


def level(seconds=5):
    """Polls BOOT as a plain input, deciding whether the edge exists at all.

    Nothing here involves the interrupt path, so it separates a pin that never
    changes -- wrong mapping, dead button, no pull -- from an edge that is
    arriving but not being delivered to Python.
    """
    import digitalio
    import time

    unwatch()  # attach_irq claims the pin, so a live watch would block this
    pin = digitalio.DigitalInOut(board.BUTTON)
    try:
        pin.switch_to_input(digitalio.Pull.UP)
        seen = {}
        print("hold and release BOOT a few times, %d s" % seconds)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            seen[pin.value] = True
            time.sleep(0.01)
    finally:
        pin.deinit()
    if len(seen) == 2:
        print("both levels seen: the button reaches GPIO0")
    else:
        print("stuck %s: no press is reaching the pin" % list(seen)[0])
    return len(seen) == 2


def blocking(seconds=10):
    """The harder half of the claim: an edge caught while Python is busy.

    A plain `time.sleep()` loop reaches a safe point on every iteration, so the
    handler runs during it. Nothing here polls the pin.
    """
    import time

    watch()
    start = presses
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
    unwatch()
    print("%d press(es) in %d s" % (presses - start, seconds))
