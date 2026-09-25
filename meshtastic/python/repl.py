"""Runs when the REPL starts, and puts the mesh within reach of the prompt.

CircuitPython looks for this file just before showing the prompt, and runs it
inside the REPL's own VM -- so unlike boot.py or code.py, the names below are
still here when you start typing. Which is why the radio is brought up here and
not in boot.py: that runs three VMs earlier, and everything it built is gone by
the time there is a prompt to use it from.

It stops at the Python prompt, which is the whole point: tab completion, history
and the freedom to write a loop or read a sensor are worth more than any
hand-rolled command line. Traffic is no longer the price of that -- the radio's
DIO9 line schedules a collector that the prompt's own wait loop runs between
keystrokes, so what arrives while you think goes into the inbox. `read()` is how
to see it and `status()` says whether anything was still missed.

Costs a second or so at every REPL entry, and leaves the radio drawing receive
current until `stop()`. The link to the Meshtastic app is not started here:
`phone()` opens it and `serve()` runs the loop that answers it, which is a
decision about the board's one advertising set, or about joining a network,
rather than a default. Which of the two it opens is the MESH_PHONE setting, or
`phone("tcp")` to say so once.
"""

from meshtastic import meshlib as mesh
# From meshtastic.meshlib, not meshtastic itself: the package is the Rust half
# and carries none of these.
from meshtastic.meshlib import (ask, channels, configure, introduce,  # noqa: F401
                               listen, node, nodes, peek, phone, poll, read,
                               release, send, serve, settings, status, stop,
                               unread)

mesh.start()
print()
mesh.commands()

# Only the boards that have a panel have the module, so this is also the test
# for one. Not turned on by default: `listen()` prints a paragraph per packet,
# and a repaint each time would put a kilobyte over the panel's bus in the
# middle of the loop that is meant to be draining the radio.
try:
    from meshtastic import oled  # noqa: F401
except ImportError:
    pass
else:
    print("# oled.tee() mirrors what you print onto the screen, oled.hush() stops")
