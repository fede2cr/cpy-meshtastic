# Finds out why importing meshtastic.mpy hard-crashes the W12.
#
# Copy this over code.py, along with the sizeprobe*.mpy ladder in lib/, and let
# the board run. It tests exactly one native module per boot and remembers how
# far it got in NVM, so when a module takes the board down the reset comes back
# to the *next* one instead of looping on the one that crashed. Leave it alone
# and it will walk the whole ladder by itself in a handful of reboots.
#
# What the ladder is for: every module below is the same trivial C, padded to a
# different size, with no Rust, no libgcc and no rodata to speak of. On the
# ESP32-S3 the loader copies a module's text into internal SRAM with
# heap_caps_malloc(MALLOC_CAP_EXEC), and that pool is a few hundred kB shared
# with the IDF. meshtastic.mpy needs 21,786 B of it in one contiguous piece;
# lr2021.mpy needs 1.5 kB and loads fine. If sizeprobe3 (28 kB) dies and
# sizeprobe2 (19 kB) lives, the problem is the executable heap and has nothing
# to do with the Rust. If the whole ladder loads and only meshtastic dies, the
# problem is in that module's content and the size is a red herring.

import gc
import os
import sys
import time

import microcontroller

# Ascending, so the first one that crashes is the threshold. The two real
# modules bracket the ladder: lr2021 is the known-good control at the start,
# meshtastic is the subject at the end.
LADDER = (
    ("lr2021", 1553),
    ("sizeprobe1", 9694),
    ("sizeprobe2", 19294),
    ("meshtastic", 21786),
    ("sizeprobe3", 28894),
    ("sizeprobe4", 38494),
    ("sizeprobe5", 48094),
    ("sizeprobe6", 57694),
    ("sizeprobe8", 77046),
)

# Byte 0 of NVM holds the index of the next module to try; byte 1 is a magic
# number so a fresh or unrelated NVM doesn't look like a run in progress. Bytes
# 2.. hold one result per rung, so the run survives the reboots it causes and
# the last screen shows the whole ladder instead of just its final line.
MAGIC = 0xA7
RESULTS = 2

UNTRIED = 0
PASSED = 1
CRASHED = 2
RAISED = 3
LOADED = 4

# LOADED vs CRASHED is the distinction worth having: dying inside the import is
# the loader failing to find or copy the text, dying on the first call into the
# module is the text arriving with bad relocations.
VERDICT = {
    UNTRIED: "-",
    PASSED: "pass",
    CRASHED: "CRASH on load",
    RAISED: "raised",
    LOADED: "CRASH on call",
}


def say(*parts):
    # Native module loading can take the board down between one line and the
    # next, and an unflushed line is a lost clue. CircuitPython writes straight
    # through to USB CDC, but the host side still needs a moment to drain
    # before a reset tears the connection down.
    print(" ".join(str(p) for p in parts))
    time.sleep(0.02)


def nvm_get():
    nvm = microcontroller.nvm
    if nvm is None or len(nvm) < 2:
        return None, 0
    if nvm[1] != MAGIC:
        return nvm, 0
    return nvm, nvm[0]


def nvm_set(nvm, index):
    if nvm is None:
        return
    nvm[0:2] = bytes((index, MAGIC))


def result_get(nvm, index):
    if nvm is None or len(nvm) < RESULTS + len(LADDER):
        return UNTRIED
    return nvm[RESULTS + index]


def result_set(nvm, index, value):
    # A single byte, written before the import and rewritten after it. If the
    # board never comes back from the import, the CRASHED byte is what is left.
    if nvm is None or len(nvm) < RESULTS + len(LADDER):
        return
    nvm[RESULTS + index] = value


def results_clear(nvm):
    if nvm is None or len(nvm) < RESULTS + len(LADDER):
        return
    nvm[RESULTS:RESULTS + len(LADDER)] = bytes(len(LADDER))


def report(nvm):
    say("")
    say("   rung  module        text      result")
    for i, (name, size) in enumerate(LADDER):
        say("   %4d  %-12s  %6d    %s" % (i + 1, name, size, VERDICT[result_get(nvm, i)]))


def module_file(name):
    # The .mpy actually on the drive, not the size the ladder expects. Copying a
    # stale build over a fixed one looks exactly like the fix not working.
    for path in ("/lib/%s.mpy" % name, "/%s.mpy" % name):
        try:
            return path, os.stat(path)[6]
        except OSError:
            pass
    return None, -1


def heap():
    gc.collect()
    try:
        import espidf

        # These are hardcoded to MALLOC_CAP_8BIT, so they cover the IDF heap
        # rather than the executable pool specifically. Still worth printing:
        # the executable pool is a subset of internal SRAM, so when the largest
        # 8-bit block collapses the executable one has collapsed with it.
        return gc.mem_free(), espidf.heap_caps_get_free_size(), espidf.heap_caps_get_largest_free_block()
    except ImportError:
        return gc.mem_free(), -1, -1


def main():
    say("")
    say("== W12 native module size probe ==")
    say("version       ", sys.version)
    say("reset reason  ", microcontroller.cpu.reset_reason)

    nvm, index = nvm_get()
    if nvm is None:
        say("nvm           ", "unavailable - edit index by hand to advance")
    elif index == 0:
        results_clear(nvm)

    if index >= len(LADDER):
        say("")
        say("ladder complete.")
        report(nvm)
        say("")
        say("reset to run it again.")
        nvm_set(nvm, 0)
        return

    report(nvm)

    name, text_size = LADDER[index]

    # Advance first, and bank the pessimistic result. If the import never
    # returns, the reset lands on the next rung rather than looping here, and
    # the CRASHED byte survives to be printed.
    nvm_set(nvm, index + 1)
    result_set(nvm, index, CRASHED)

    free, idf_free, idf_block = heap()
    path, file_size = module_file(name)
    say("")
    say("rung          ", "%d of %d" % (index + 1, len(LADDER)))
    say("module        ", name)
    say("file          ", path or "NOT FOUND")
    say("file size     ", file_size)
    say("text size     ", "%d bytes" % text_size)
    say("gc free       ", free)
    say("idf free      ", idf_free)
    say("idf largest   ", idf_block)
    say("")
    say(">>> importing %s ... if this is the last line you see, it crashed here" % name)

    try:
        mod = __import__(name)
    except Exception as exc:  # noqa: BLE001 - the whole point is to see anything
        result_set(nvm, index, RAISED)
        say("!!! %s: %s" % (type(exc).__name__, exc))
        say("")
        say("that is a clean failure, not a crash. reset for the next rung.")
        return

    result_set(nvm, index, LOADED)
    say("<<< imported %s ok" % name)

    after, idf_after, block_after = heap()
    say("gc cost       ", free - after)
    say("idf cost      ", (idf_free - idf_after) if idf_free >= 0 else "?")
    say("idf largest   ", block_after)

    # Loading is only half of it. Executing proves the relocations landed,
    # which is the part that would be wrong if this were a linker problem
    # rather than a memory one.
    if name.startswith("sizeprobe"):
        say("blocks()      ", mod.blocks())
        say("ping(1)       ", mod.ping(1))
    elif name == "lr2021":
        say("flrc_bitrate  ", mod.flrc_bitrate_kbps(0))
    elif name == "meshtastic":
        say("djb2(b'abc')  ", mod.djb2(b"abc"))

    result_set(nvm, index, PASSED)
    say("")
    say("rung %d passed. reset for the next one." % (index + 1))


main()
