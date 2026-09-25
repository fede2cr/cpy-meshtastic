"""Join the local Meshtastic mesh, and let the phone app drive it.

The line under SETTINGS is the one to edit. It is written to NVM at every boot,
so changing it here and resetting is how this node is retuned; the
`configure()` prompt in `repl.py` changes it for a session, and this file wins
again at the next reset. Every other setting -- preset, channel, power, duty
cycle -- is left at its default in `mesh_config`, and is the board's own. The
node's name is not here on purpose: unnamed, it derives one the way stock
firmware does, and a name declared here would overwrite whatever the app had
set at every reset. settings.toml is where to bake one in.

Everything this does lives in `mesh`, `meshnode` and `meshble`, so this file
stays short enough to be worth editing on the drive. `start()` generates an
identity and a channel key in NVM on first run and brings the radio up,
`introduce()` broadcasts this node's name on the mesh, and `serve()` advertises
over BLE and then runs the loop that answers the app until ctrl-C.

What the app gets is a client: the mesh, the messages, and the ability to send.
What it does not get is this node's settings. They are in NVM and the board can
write them, but only from its own prompt -- `settings()` and `configure()` in
`repl.py`. `meshapi` says why the phone is not allowed to.

Running this also answers whether the SoftDevice and a LoRa receive loop
coexist -- the SoftDevice takes hard timing priority on the nRF52, and receiving
is SPI on the main thread. Compare `missed` in `mesh.status()` after a run of
this against one that never calls `serve()`.

Copy code.py, repl.py and settings.toml to the root of CIRCUITPY and the
contents of lib/ to CIRCUITPY/lib. settings.toml carries one key that has to be
read before this file runs and so cannot live here. Attach an antenna:
transmitting without one can damage the PA.
"""

from meshtastic import meshlib as mesh

# Before start(), not where it is used: the cache restores dozens of nodes and
# messages, and a collected heap is not a compacted one. Loaded last, this asks
# for a contiguous kilobyte that no longer exists and raises MemoryError with
# 18 kB free. serve() below pays for this either way.
from meshtastic import meshphone  # noqa: F401

# ---------------------------------------------------------------- SETTINGS

#: Which band plan to obey. `meshtastic.REGION_NAMES` lists the rest.
REGION = "US"

# --------------------------------------------------------------------------

mesh.start(region=REGION)
mesh.introduce()
mesh.serve()
