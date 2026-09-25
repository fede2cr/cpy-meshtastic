# Builds the .mpy files a board carries, and the few files that sit on the
# CIRCUITPY drive next to them.
#
# The Rust and the Python are separate modules: `meshtastic` is the Rust, and
# `meshlib` is every Python layer merged into one file. They were a single
# merged .mpy until the Rust moved into the firmware image -- see the `firmware`
# target below. A built-in shadows the drive, so the Python could not keep
# sharing the name.
#
# The Python is compiled a module at a time by mpy-cross and spliced by
# `mpy-tool.py --merge` into one .mpy whose top level is a bytecode stub that
# calls each part in turn. Every part runs into the same globals, so the result
# is one module, in one file. lr1121 still merges its Rust and Python halves,
# which is what --merge was for; at most one input may contain native code.
#
# That shared namespace is why the modules carry prefixes -- `keystore_load`
# rather than `load`. Merged, they have one globals dict between them, and
# Python resolves a global when a function runs rather than when it is defined,
# so a name defined twice would break whichever module defined it first, not
# just its callers. The collisions were real: `load`, `save`, `erase`,
# `configure`, `describe`, `provision`, `stop`, `blob`, `_text` and `open_radio`
# each had two or three definitions.
#
# LIBRARY below is a topological sort and not an alphabetical one. A default
# argument such as `to=mt.BROADCAST` is evaluated when its `def` executes, so
# mesh_protocol has to be in the namespace before mesh_tx and nodeinfo arrive.
#
# This Makefile produces dist/ and stops there. Getting those files onto a board
# is a drag-and-drop onto the CIRCUITPY drive, and building an image for the
# board belongs to the CircuitPython tree, not here.
#
# The image does have to enable CIRCUITPY_ENABLE_MPY_NATIVE, or both .mpy files
# fail to import with "native code in .mpy unsupported". That is set in the
# board's own mpconfigboard.mk, so every build of the board carries it.
#
#   make            the natmods, then dist/
#   make test       cargo test, then the Python layers against tools/hoststub.py
#                   and the part drivers against tools/radiotest.py

MPY_DIR ?= ../circuitpython
MPY_CROSS ?= $(MPY_DIR)/mpy-cross/build/mpy-cross

#: Sub-makes run in another directory, so a relative path to the tree or to an
#: interpreter has to be absolute by the time it gets there. This matters even
#: when nothing here sets them: a variable given on the command line propagates
#: to every sub-make, so `make MPY_DIR=../circuitpython` -- which is correct
#: here -- would otherwise reach meshtastic/natmod/ as a path two levels wrong.
#: A bare command name is left alone; it is resolved on PATH, and turning that
#: into a path would break it.
ABS_MPY_DIR = $(abspath $(MPY_DIR))
ABS_PYTHON = $(if $(findstring /,$(PYTHON)),$(abspath $(PYTHON)),$(PYTHON))
#: Passed as one blob so the three call sites cannot drift apart.
NATMOD_ARGS = MPY_DIR=$(ABS_MPY_DIR) PYTHON=$(ABS_PYTHON)
#: Drops asserts and source line numbers. Worth about 6% on the pure-bytecode
#: modules below. It buys nothing on meshtastic.mpy, because merging anything
#: with a natmod already discards the line info of every Python half it carries
#: -- which is why a traceback out of that file always says line 1, while one
#: out of meshphone.mpy names the real file and line. Measured both ways.
MPY_FLAGS ?= -O3
#: Merges a native .mpy and any number of bytecode ones into a single module.
MPY_TOOL ?= $(MPY_DIR)/tools/mpy-tool.py
PYTHON ?= $(CURDIR)/venv/bin/python
DIST ?= dist
PYC ?= build/pyc
#: Beside the Rust half it merges with, the way lr1121/python/ already sits
#: beside its own.
PYSRC ?= meshtastic/python

#: The radio drivers are separate code bases with their own repositories and
#: their own releases. They are built here while this tree still carries them,
#: and skipped when it does not, so that moving them out is a `git rm` rather
#: than a Makefile edit. What ships from here is the library; `meshradio` loads
#: whichever driver the board carries by name at import, from the drive.
LR1121_SRC := $(wildcard lr1121)
LR2021_SRC := $(wildcard lr2021)

#: Merged into meshlib.mpy, in dependency order. See the note above on why
#: the order is load-bearing. Both radio adapters ship: they are a few hundred
#: bytes each, the MESH_RADIO setting picks one, and a library that only runs
#: on the board it was built for is not what the meshradio seam is for.
LIBRARY = mesh_budget mesh_proto mesh_protocol keystore nodeinfo mesh_config \
          nodedb inbox mesh_tx meshradio radio_lr11xx radio_lr2021 \
          meshpower meshnode mesh

#: Merged into meshphone.mpy, which `mesh.phone()` imports the first time it is
#: called. Separate because a merged .mpy is all or nothing: loading one
#: allocates every module in it, so anything that shares a file with the node is
#: resident from boot whether or not a phone ever connects. These are 20 kB
#: that a node running on its own never touches. Both transports ship, the
#: MESH_PHONE setting picks one, and they share meshapi -- which is the whole
#: argument for keeping the two in one file rather than two.
PHONE = meshapi meshble meshtcp

#: Rendering a decoded payload as text, which only `listen()` does. Its own file
#: for the same reason as PHONE, and one module so it needs no merge.
TEXT = meshtext

#: Restoring the roster from NVM at boot, and writing it back every six hours.
#: `meshnode` imports it, uses it and drops it again, so those two kilobytes are
#: absent for the whole session in between. Optional like the two above, but it
#: costs more to leave out than they do: without it the node forgets every peer
#: and every message across a reboot. Leaving the file off the drive and setting
#: `MESH_CACHE = false` do the same thing; the setting says it was meant.
CACHE = meshcache

#: Names that used to be modules of their own, listed so that `dist` can say
#: what to delete. Every one is now inside meshlib.mpy, and a copy left on
#: the drive would be found first at the root and shadow the merged library, or
#: sit unused in lib/ while looking current.
#:
#: A copy FROZEN into an image is worse, and cannot be deleted at all: sys.path
#: is '', '/', '.frozen', '/lib', so it beats lib/ outright. A board carrying an
#: image from when this library was frozen has to be put back on a stock one.
#:
#: lr1121 and _lr1121 are absent: both are shipped names now, the Python half
#: and the native one.
RETIRED = _meshtastic meshtastic_link mesh_payload $(LIBRARY) $(PHONE) $(TEXT)

.PHONY: all test firmware firmware-clean w12 w12-probe

# dist/      -> the drive root: what you edit
# dist/lib/  -> the drive's lib/: the two libraries, one file each
#
# Each library is compiled a module at a time and then merged, native half
# first. The native half leads so that its names are bound before any Python
# body runs and looks one up.
#
# The tree's own mpy-cross, so the bytecode version cannot drift from the
# image meant to load it.
all: $(MPY_CROSS)
ifneq ($(LR1121_SRC),)
	$(MAKE) -C lr1121/natmod PYTHON=$(PYTHON)
endif
	$(MAKE) -C meshtastic/natmod $(NATMOD_ARGS)
	@rm -rf $(DIST)
	@mkdir -p $(DIST)/lib/meshtastic $(PYC)
	cp $(PYSRC)/code.py $(PYSRC)/repl.py $(PYSRC)/settings.toml $(DIST)/
ifneq ($(LR1121_SRC),)
	cp lr1121/natmod/lr1121.mpy $(DIST)/lib/_lr1121.mpy
	$(MPY_CROSS) -o $(DIST)/lib/lr1121.mpy lr1121/python/lr1121.py
endif
	@for m in $(LIBRARY); do \
		$(MPY_CROSS) -o $(PYC)/$$m.mpy $(PYSRC)/$$m.py || exit 1; \
	 done
	cp meshtastic/natmod/meshtastic.mpy $(DIST)/lib/meshtastic/__init__.mpy
	$(PYTHON) $(MPY_TOOL) --merge -o $(DIST)/lib/meshtastic/meshlib.mpy \
		$(addprefix $(PYC)/,$(addsuffix .mpy,$(LIBRARY)))
	@for m in $(PHONE); do \
		$(MPY_CROSS) $(MPY_FLAGS) -o $(PYC)/$$m.mpy $(PYSRC)/$$m.py || exit 1; \
	 done
	$(PYTHON) $(MPY_TOOL) --merge -o $(DIST)/lib/meshtastic/meshphone.mpy \
		$(addprefix $(PYC)/,$(addsuffix .mpy,$(PHONE)))
	$(MPY_CROSS) $(MPY_FLAGS) -o $(DIST)/lib/meshtastic/meshtext.mpy $(PYSRC)/meshtext.py
	$(MPY_CROSS) $(MPY_FLAGS) -o $(DIST)/lib/meshtastic/meshcache.mpy $(PYSRC)/meshcache.py
	@echo
	@ls -l $(DIST) $(DIST)/lib $(DIST)/lib/meshtastic
	@echo
	@echo "dist/ goes to the drive root, dist/lib/ to the drive's lib/."
	@echo "settings.toml is in dist/, not dist/lib/: copying only lib/ leaves"
	@echo "the board on its old budget and the new sizes have no effect."
	@echo "Everything of ours is lib/meshtastic/ now, one directory to copy."
	@echo "The radio driver stays outside it because it is a separate code base"
	@echo "and which one a board needs depends on which radio it carries."
	@echo "__init__.mpy is the Rust half, for a stock image. An image built by"
	@echo "'make firmware' has it built in, where it costs no heap -- but a"
	@echo "built-in is found before lib/ is looked at, so the package would then"
	@echo "be shadowed whole. Copy this one unless the image was built without it."
	@echo "_lr1121.mpy is the Rust radio driver, ignored by an image that has it"
	@echo "built in -- but lr1121.mpy is only the Python half now and cannot"
	@echo "drive the radio without one."
	@echo "meshphone.mpy is optional: copy it only to talk to the phone app."
	@echo "meshtext.mpy is optional too: it is what listen() prints payloads with."
	@echo "meshcache.mpy is optional as of MESH_CACHE: without it the node"
	@echo "forgets peers and messages on reboot, and frees ~2 kB while running."
	@echo "Keep your own code.py: the names and region are declared at the top of it."
	@echo "Delete any of these left on the drive,"
	@echo "at the root or in lib/, or they will shadow the library:"
	@echo "  $(RETIRED)"

$(MPY_CROSS):
	$(MAKE) -C $(MPY_DIR)/mpy-cross

#: Builds an image with the Rust half linked in rather than loaded. A natmod's
#: machine code is copied into the GC heap at import and stays there; built in,
#: it executes from flash and costs nothing. That is ~22 kB back on a board
#: whose whole free heap was 6 kB.
#:
#: meshtastic/micropython.mk is what USER_C_MODULES picks up. It compiles the
#: same meshtastic.c the natmod does and links the same libmeshtastic.a, so the
#: `all` above has to have run first.
#:
#: A built-in shadows the drive: `import meshtastic` finds this and never looks
#: in lib/. Whatever the drive is carrying under that name becomes dead weight.
BOARD ?= muzi_base_duo
PORT_DIR ?= $(MPY_DIR)/ports/nordic
#: The CircuitPython tree builds with its own venv, which has cascadetoml.
CPY_VENV ?= $(MPY_DIR)/.venv

.PHONY: firmware firmware-clean
firmware: all
	PATH=$(abspath $(CPY_VENV))/bin:$$PATH $(MAKE) -C $(PORT_DIR) \
		BOARD=$(BOARD) USER_C_MODULES=$(CURDIR) \
		-j$$(nproc) build-$(BOARD)/firmware.uf2
	@echo
	@ls -l $(PORT_DIR)/build-$(BOARD)/firmware.uf2

#: make does not track CFLAGS, so a build that has already seen one setting of
#: USER_C_MODULES will not rebuild itself when it changes.
firmware-clean:
	rm -rf $(PORT_DIR)/build-$(BOARD)

# ── Meshnology W12 ───────────────────────────────────────────────────────────
#
# The other board this tree builds for: ESP32-S3 rather than nRF52840, and an
# LR2021 rather than an LR1121. Its own target rather than a variable on `all`
# because almost nothing is shared: the natmods are Xtensa and will not load on
# the nRF board, nor its Thumb ones here, and the radio driver is a different
# part. The mesh library itself is pure bytecode and so is the same file on
# both, with MESH_RADIO = lr2021 choosing this board's adapter out of it.
#
# Everything stays on the drive as a .mpy, with no USER_C_MODULES build to put
# it in the image: that trade only pays on a board short of heap, and this one
# has 8 MB of PSRAM.
#
#   . ~/export-esp.sh && make w12
#
# The export line is not optional -- the Xtensa compiler and the `esp` rustc
# fork are both only on PATH after it, and upstream rustc has no Xtensa
# backend at all.
W12_DIST ?= dist-w12

.PHONY: w12
w12: $(MPY_CROSS)
ifneq ($(LR2021_SRC),)
	$(MAKE) -C lr2021/natmod PYTHON=$(PYTHON)
endif
	$(MAKE) -C meshtastic/natmod TARGET=xtensa $(NATMOD_ARGS)
	@rm -rf $(W12_DIST)
	@mkdir -p $(W12_DIST)/lib/meshtastic $(PYC)
	cp w12/code.py $(W12_DIST)/code.py
	#: The same file the nRF board gets: it names no pin and no part, so the
	#: prompt is identical on both and MESH_RADIO is all that differs.
	cp $(PYSRC)/repl.py $(W12_DIST)/repl.py
	cp w12/settings.toml $(W12_DIST)/settings.toml
ifneq ($(LR2021_SRC),)
	cp lr2021/natmod/lr2021.mpy $(W12_DIST)/lib/lr2021.mpy
endif
	#: Renamed on the way in twice over. The import name comes from the
	#: filename, the Xtensa build is only called that to keep it from
	#: overwriting the Thumb one in the tree, and landing it as a package's
	#: __init__ is what lets the two board files below share its name.
	#: CircuitPython stats the directory first, so `import meshtastic` gets
	#: this and `meshtastic.gps` gets the file beside it.
	cp meshtastic/natmod/meshtastic-xtensa.mpy \
		$(W12_DIST)/lib/meshtastic/__init__.mpy
	cp w12/oled.py $(W12_DIST)/lib/meshtastic/oled.py
	#: Boot code despite its history: MESH_GPS names it and `open_gps` imports
	#: it. It was called gpstest while it was only a bring-up tool, which is
	#: the reason the drive used to look like it was half test code.
	cp w12/gps.py $(W12_DIST)/lib/meshtastic/gps.py
	#: Bytecode, so the same merge as the nRF board's. Only the natmods above
	#: are architecture-specific.
	@for m in $(LIBRARY); do \
		$(MPY_CROSS) -o $(PYC)/$$m.mpy $(PYSRC)/$$m.py || exit 1; \
	 done
	$(PYTHON) $(MPY_TOOL) --merge -o $(W12_DIST)/lib/meshtastic/meshlib.mpy \
		$(addprefix $(PYC)/,$(addsuffix .mpy,$(LIBRARY)))
	$(MPY_CROSS) $(MPY_FLAGS) -o $(W12_DIST)/lib/meshtastic/meshtext.mpy $(PYSRC)/meshtext.py
	$(MPY_CROSS) $(MPY_FLAGS) -o $(W12_DIST)/lib/meshtastic/meshcache.mpy $(PYSRC)/meshcache.py
	#: Bytecode too, and `meshble` calls nothing but `_bleio`, which the S3
	#: builds with NimBLE behind it. The SoftDevice in its comments is where it
	#: was written, not what it needs.
	@for m in $(PHONE); do \
		$(MPY_CROSS) $(MPY_FLAGS) -o $(PYC)/$$m.mpy $(PYSRC)/$$m.py || exit 1; \
	 done
	$(PYTHON) $(MPY_TOOL) --merge -o $(W12_DIST)/lib/meshtastic/meshphone.mpy \
		$(addprefix $(PYC)/,$(addsuffix .mpy,$(PHONE)))
	@echo
	@ls -l $(W12_DIST) $(W12_DIST)/lib $(W12_DIST)/lib/meshtastic
	@echo
	@echo "$(W12_DIST)/ goes to the drive root, $(W12_DIST)/lib/ to the drive's lib/."
	@echo "Everything of ours is lib/meshtastic/ now, one directory to copy."
	@echo "lr2021.mpy stays outside it: the radio driver is a separate code base,"
	@echo "and which one a board needs depends on which radio it carries."
	@echo "Delete what was on the drive first rather than copying over it. A"
	@echo "drive that keeps the old root gps.py and lib/meshlib.mpy still boots"
	@echo "-- the package wins over a flat .mpy and nothing imports a bare"
	@echo "meshlib any more -- but then two copies of each file are on it and"
	@echo "only one of them is the one being edited."
	@echo "The image must be built with CIRCUITPY_ENABLE_MPY_NATIVE, which the"
	@echo "board's mpconfigboard.mk already sets, or both .mpy files fail to"
	@echo "import with 'native code in .mpy unsupported'."
	@echo "code.py is the node now, not the self-test. It waits for the console,"
	@echo "brings the radio up, announces and serves the phone over TCP."
	@echo "Only what the node boots is here. The bring-up tools -- selftest,"
	@echo "pintest, irqtest, loratest, gpsweb -- are 'make w12-tools', which"
	@echo "adds them to this same drive. Two of them key a transmitter, so"
	@echo "they are opt-in rather than always present."
	@echo "phone() and serve() import here but nothing has been seen"
	@echo "advertising from the S3: _bleio is in the image and meshble uses"
	@echo "only that, so the gap is below us. Revisit on the Zephyr port."

# The bring-up drive: everything `make w12` ships, plus the files that are run
# by hand from the prompt. Kept out of the default because none of them is
# imported by code.py and a node in the field has no use for any of them --
# and because two of them key a transmitter, which is not something that
# should be one typo away on a board that is just meant to be running.
#
# Source rather than bytecode on purpose: these are the files a bring-up
# session edits between resets, and the board recompiles any of them in
# milliseconds.
.PHONY: w12-tools
w12-tools: w12
	cp w12/selftest.py $(W12_DIST)/selftest.py
	cp w12/irqtest.py $(W12_DIST)/irqtest.py
	cp w12/pintest.py $(W12_DIST)/pintest.py
	cp w12/loratest.py $(W12_DIST)/loratest.py
	cp w12/gpsweb.py $(W12_DIST)/gpsweb.py
	#: selftest stage 12 imports this one.
ifneq ($(LR2021_SRC),)
	cp lr2021/examples/flrc_link/lr2021_flrc.py $(W12_DIST)/lr2021_flrc.py
endif
	@echo
	@ls -l $(W12_DIST)
	@echo
	@echo "selftest.py keys the 2.4 GHz transmitter in stage 7, into the W12's"
	@echo "internal flexible printed antenna. Clear TRANSMIT at the top of it"
	@echo "if that antenna is ever disconnected: keying into an open circuit"
	@echo "can destroy the PA."
	@echo "loratest.transmit() keys the sub-GHz PA the same way."

# A drive that answers one question and nothing else: why does meshtastic.mpy
# take the board down on import while lr2021.mpy loads?
#
# tools/sizeprobe builds a ladder of padding-only modules -- same trivial C at
# nine different sizes, no Rust, no libgcc, nothing that could misbehave. The
# ESP32-S3 loader copies a module's text into internal SRAM with
# heap_caps_malloc(MALLOC_CAP_EXEC), a pool of a few hundred kB shared with the
# IDF, so the ladder measures how big a module that pool will actually take.
# Whichever rung first fails answers the question: if it lands near
# meshtastic's 21,786 B then this is an executable-memory problem, and if the
# whole ladder loads then the size is a red herring and the fault is in the
# module's content.
W12_PROBE_DIST ?= dist-w12-probe

.PHONY: w12-probe
w12-probe:
ifneq ($(LR2021_SRC),)
	$(MAKE) -C lr2021/natmod PYTHON=$(PYTHON)
endif
	$(MAKE) -C meshtastic/natmod TARGET=xtensa $(NATMOD_ARGS)
	$(MAKE) -C tools/sizeprobe $(NATMOD_ARGS)
	@rm -rf $(W12_PROBE_DIST)
	@mkdir -p $(W12_PROBE_DIST)/lib
	#: The probe is the whole point of this drive, so it goes in as code.py
	#: and runs on its own at every boot -- including the boots that follow a
	#: crash, which is how it walks the ladder unattended.
	cp w12/sizeprobe.py $(W12_PROBE_DIST)/code.py
	cp tools/sizeprobe/sizeprobe*.mpy $(W12_PROBE_DIST)/lib/
ifneq ($(LR2021_SRC),)
	cp lr2021/natmod/lr2021.mpy $(W12_PROBE_DIST)/lib/lr2021.mpy
endif
	cp meshtastic/natmod/meshtastic-xtensa.mpy $(W12_PROBE_DIST)/lib/meshtastic.mpy
	@echo
	@ls -l $(W12_PROBE_DIST)/lib
	@echo
	@echo "$(W12_PROBE_DIST)/ goes to the drive root, $(W12_PROBE_DIST)/lib/ to lib/."
	@echo "Then just watch the serial console and reset when it asks. It tests"
	@echo "one module per boot and keeps its place in NVM, so a module that"
	@echo "crashes the board is skipped rather than retried on the next boot."

test:
	#: +nightly because the crate builds with panic-immediate-abort, and --lib
	#: because the doc and integration harnesses want unwinding, which is not
	#: available without std. A bare `cargo test` fails on both counts.
	cargo +nightly test --lib -p meshtastic
	$(PYTHON) -m py_compile $(PYSRC)/*.py
	$(PYTHON) tools/hosttest.py
	#: The part drivers, which hosttest does not reach: it fakes meshradio's
	#: contract, and a chip register written at the wrong moment is invisible
	#: from there. See the file's header for what that cost once.
	$(PYTHON) tools/radiotest.py
