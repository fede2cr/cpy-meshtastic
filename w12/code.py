"""Join the local Meshtastic mesh from the W12, and then get out of the way.

The SETTINGS block below is what to edit. `REGION` is written to NVM at every
boot, so changing it here and resetting is how this node is retuned; the
`configure()` prompt in `repl.py` changes it for a session, and this file wins
again at the next reset. Everything else -- preset, channel, power, duty cycle
-- is left at its default in `mesh_config` and is the board's own. The node's
name is not here on purpose: unnamed, it derives one the way stock firmware
does, and a name declared here would overwrite whatever the app had set at
every reset. settings.toml is where to bake one in.

It waits for the console before it does anything, and says what it is about to
do before each step. USB output written before the host has opened the port is
thrown away, so without the wait a board that dies during bring-up and one that
died before the console attached look identical on the wire.

ANNOUNCE keys the sub-GHz amplifier, which `loratest.transmit()` has now done
from the REPL: three packets, TxDone at 355-357 ms against a modelled 354 ms of
airtime. That says the modem keyed for the right length of time. It does not
say anything radiated -- no receiver has heard this board yet, and the PA enable
is on a pin taken from a variant file this project has already caught being
wrong once. A link test is what settles that.

This board's phone link is `MESH_PHONE = "tcp"` in settings.toml -- the Network
entry in the app rather than the Bluetooth one, because nothing has ever been
seen advertising from this board. So `serve()` runs here, and the panel gets a
page with the address on it. The link needs the access point, which is one more
thing than a radio needs; if it is not there the node comes up anyway and says
why.

code.py and the REPL are separate VM runs with the heap cleared and the buses
released between them, so nothing built here is still alive at the prompt --
`repl.py` brings the radio up again on the other side.

So this does not return. CircuitPython takes every display back the moment the
last line runs, which means a panel is only up for as long as something is
running: a code.py that finished would leave a blank screen and a radio nobody
is draining. It ends in `oled.Status.run()` instead, which polls the node and
repaints once a second. Ctrl-C leaves that loop, stops the radio and gives you
the prompt -- standby is a kinder state to hand over in than an unread receiver.

The self-test that used to live in this slot is `selftest.py` now; `import
selftest` runs it. `loratest.py` is the radio-only ladder underneath it.
"""

import time

import microcontroller
import supervisor

from meshtastic import meshlib as mesh

try:
    from meshtastic import oled
except ImportError:      # a drive without the panel module still boots
    oled = None

# ---------------------------------------------------------------- SETTINGS

#: Which band plan to obey. `meshtastic.REGION_NAMES` lists the rest.
REGION = "US"

#: Whether to broadcast this node's name at boot, so other nodes can list it.
ANNOUNCE = True

#: Whether to serve the Meshtastic app. MESH_PHONE in settings.toml picks the
#: transport; on this board that is "tcp", so it needs the access point to be
#: up. A link that cannot be opened is reported and the node runs without it.
PHONE = True

#: How long to let the host open the port before starting. Spent at every boot
#: with nothing attached, which is what the watched boots being legible costs.
CONSOLE_WAIT_S = 3

# --------------------------------------------------------------------------

_deadline = time.monotonic() + CONSOLE_WAIT_S
while not supervisor.runtime.serial_connected and time.monotonic() < _deadline:
    time.sleep(0.1)

if oled is not None:
    # Every step below goes to the panel as well, so a boot nobody was watching
    # over USB still says on the glass how far it got.
    oled.tee()


def _last_run():
    """What ended the previous run.

    A crash takes the console with it, so this is the only witness there is to
    one that happened while nobody was attached. POWER_ON and SOFTWARE are the
    two ordinary answers -- a plug and a Ctrl-D. BROWNOUT is the supply,
    WATCHDOG is something that blocked too long, and RESET_PIN is the button.
    """
    why = str(microcontroller.cpu.reset_reason).rsplit(".", 1)[-1]
    safe = str(supervisor.runtime.safe_mode_reason).rsplit(".", 1)[-1]
    return why if safe == "NONE" else "%s, safe mode %s" % (why, safe)


print("# code.py: last reset %s" % _last_run())
print("# code.py: bringing the node up, Ctrl-C stops it here")
mesh.start(region=REGION)
if ANNOUNCE:
    print("# code.py: announcing, which keys the sub-GHz amplifier")
    mesh.introduce()

session = None
if PHONE:
    try:
        session = mesh.phone()
    except (OSError, RuntimeError, ValueError) as error:
        # No access point, or no mDNS. Neither is a reason to stop being a
        # node: the radio is already up and the mesh does not need the phone.
        print("# code.py: no phone link, %s" % (error,))

status = None
if oled is not None:
    # The scrolled log has done its job; from here the panel is the node's.
    oled.tee(False)
    # Asked for again rather than assumed: on a cold boot the rail has now been
    # up since the first print, which is the whole difference.
    if oled.panel(force=True) is None:
        print("# code.py: node up, no panel -- Ctrl-C for the prompt")
    else:
        print("# code.py: node up, panel running -- Ctrl-C for the prompt")
        status = oled.Status(mesh.node(), session=session)

if session is not None:
    mesh.serve(each=None if status is None else status.tick)
elif status is not None:
    status.run()

mesh.stop()
print("# code.py: settings saved, radio in standby, prompt brings it back up")
