"""Meshnology W12 bring-up self-test.

Run it with `import selftest` at the prompt. It used to be code.py and ran at
every boot, which was right while the board was unknown and is wrong now that
it runs a node: stage 7 keys a transmitter, and that is not a thing to do on
the way to a prompt.

Prints a PASS/FAIL line per check to the USB REPL. Nothing here is a demo:
every stage exists to answer one question about a board that has never run this
firmware before, in an order chosen so that a failure early on explains the
failures after it.

    1  firmware      is this the image we think it is, and is PSRAM present
    2  natmods       do the two .mpy files load at all
    3  codec         does the Rust in meshtastic.mpy still compute
    4  flrc tables   does the Rust in lr2021.mpy still compute
    5  board         I2C, the battery divider, the LED, the button
    6  radio         SPI, reset, BUSY, and the first words out of the LR2021
    7  on air        FLRC configured and one packet transmitted
    8  oled          the panel lights and holds text

Stages 1-4 need no hardware beyond the chip and will pass on a bare board with
nothing soldered to the radio. Stage 6 is the first that can fail because of a
wire. Stage 7 keys the transmitter and can be turned off below.

Every stage is guarded: one failing stage does not stop the ones after it, so a
single run produces the whole picture rather than the first problem.

There are almost no golden constants in the codec checks. The values that would
serve as one live in the Rust, so asserting them here would only restate the
thing under test; what the checks do instead is round-trip, bound and
cross-check against an independent line or two of Python, which catches a
miscompilation or a broken relocation -- the failures a fresh architecture
actually produces.
"""

import gc
import os
import sys
import time

import board
import microcontroller

# ─── what to run ─────────────────────────────────────────────────────────────

#: Stage 7 keys the 2.4 GHz transmitter. The W12 carries a flexible printed
#: antenna inside the case, so the PA is loaded and this is safe to leave on.
#: Set it False if the board is ever opened and that antenna disconnected:
#: transmitting into an open circuit reflects the whole output back into the PA
#: and can destroy it. Nothing else in this file transmits.
TRANSMIT = True

#: Stage 7 settings, used only when TRANSMIT is on. FLRC is a 2.4 GHz mode --
#: its narrowest bandwidth is 444 kHz -- so there is no sub-GHz channel to
#: pick. 0 dBm is far below the +12 dBm the PA can do and is plenty for a
#: bench.
TX_FREQUENCY_HZ = 2_450_000_000
TX_POWER_DBM = 0
TX_PACKETS = 20


# ─── harness ─────────────────────────────────────────────────────────────────

_passed = 0
_failed = 0
_skipped = 0


def section(title):
    print()
    print("== %s " % title + "=" * max(0, 60 - len(title)))


def ok(name, condition, detail=""):
    """Records one check. `condition` is already evaluated; this only reports."""
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS  %-34s %s" % (name, detail))
    else:
        _failed += 1
        print("  FAIL  %-34s %s" % (name, detail))
    return bool(condition)


def eq(name, got, want):
    return ok(name, got == want, "got %r, want %r" % (got, want))


def skip(name, why):
    global _skipped
    _skipped += 1
    print("  SKIP  %-34s %s" % (name, why))


def info(name, value):
    print("  ----  %-34s %s" % (name, value))


def stage(title, fn):
    """Runs one stage, turning anything it raises into a single FAIL.

    A stage that dies part way through has already printed whatever it managed
    to check, so the traceback is extra information rather than the only
    information.
    """
    section(title)
    started = time.monotonic()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - a bring-up test wants them all
        global _failed
        _failed += 1
        print("  FAIL  %-34s %s: %s" % ("stage raised", type(exc).__name__, exc))
        sys.print_exception(exc)
    # A stall is otherwise invisible: the board is quiet either way, so there
    # is nothing to tell a slow check from a slow terminal.
    info("stage took", "%d ms" % int((time.monotonic() - started) * 1000))


# ─── 1. firmware ─────────────────────────────────────────────────────────────

def stage_firmware():
    uname = os.uname()
    info("version", uname.version)
    info("machine", uname.machine)
    ok("board id", board.board_id == "meshnology_w12", board.board_id)

    uid = microcontroller.cpu.uid
    info("cpu uid", "".join("%02x" % b for b in uid))
    info("cpu frequency", "%d MHz" % (microcontroller.cpu.frequency // 1_000_000))
    info("cpu temperature", "%.1f C" % microcontroller.cpu.temperature)

    # PSRAM is the whole reason this board can carry natmods on the heap
    # instead of in the image. Without it the free heap is a couple of hundred
    # kilobytes, so the number below is the check: it cannot be reached from
    # internal SRAM alone.
    gc.collect()
    free = gc.mem_free()
    info("free heap", "%d bytes (%.1f MB)" % (free, free / 1048576))
    ok("psram in the heap", free > 2_000_000, "%d bytes free" % free)

    pins = [n for n in dir(board) if n.isupper() or n.startswith("LORA")]
    info("board pins", "%d names" % len(pins))
    for name in ("LORA_CS", "LORA_SCK", "LORA_MOSI", "LORA_MISO", "LORA_RESET",
                 "LORA_BUSY", "LORA_DIO1", "PA_EN_2G4", "PA_EN_SUBGHZ",
                 "VEXT_ENABLE", "BATTERY", "ADC_CTRL", "SDA", "SCL"):
        ok("board.%s" % name, hasattr(board, name))


# ─── 2. native modules ───────────────────────────────────────────────────────

# Filled in by stage 2 and used by 3, 4, 6 and 7. A stage that needs one it
# never got says so and skips rather than raising.
lr2021 = None
mt = None


def stage_natmods():
    global lr2021, mt

    gc.collect()
    before = gc.mem_free()
    try:
        import lr2021 as _lr2021
        lr2021 = _lr2021
        ok("import lr2021", True, "")
    except Exception as exc:  # noqa: BLE001
        ok("import lr2021", False, "%s: %s" % (type(exc).__name__, exc))
    gc.collect()
    # A natmod's code is copied into the GC heap at import and stays there, so
    # the drop is the module's real cost. It should be within a few hundred
    # bytes of the .mpy on the drive.
    info("lr2021 heap cost", "%d bytes" % (before - gc.mem_free()))

    gc.collect()
    before = gc.mem_free()
    try:
        import meshtastic as _mt
        mt = _mt
        ok("import meshtastic", True, "")
    except Exception as exc:  # noqa: BLE001
        ok("import meshtastic", False, "%s: %s" % (type(exc).__name__, exc))
    gc.collect()
    info("meshtastic heap cost", "%d bytes" % (before - gc.mem_free()))

    if lr2021 is not None:
        for name in ("flrc_bitrate_kbps", "flrc_bandwidth_khz",
                     "flrc_modulation_params", "flrc_goodput_bps",
                     "flrc_per_permille"):
            ok("lr2021.%s" % name, hasattr(lr2021, name))
    if mt is not None:
        for name in ("parse_header", "write_header", "build_flags", "djb2",
                     "channel_hash", "preset_params", "region_info",
                     "airtime_us", "encode_data", "parse_data"):
            ok("meshtastic.%s" % name, hasattr(mt, name))


# ─── 3. meshtastic codec ─────────────────────────────────────────────────────

def _djb2_reference(data):
    """The same hash in four lines of Python, as a second opinion.

    Worth having because this is the first Xtensa build of that crate: a
    32-bit multiply that wraps wrongly, or a relocation that points a constant
    somewhere else, shows up here and nowhere in a round-trip test.
    """
    h = 5381
    for b in data:
        h = (h * 33 + b) & 0xFFFFFFFF
    return h


def _xor_reference(data):
    h = 0
    for b in data:
        h ^= b
    return h


def stage_codec():
    if mt is None:
        skip("meshtastic codec", "module did not import")
        return

    for probe in (b"", b"a", b"LongFast", b"\x00\xff" * 64,
                  bytes(range(256))):
        eq("djb2(%d bytes)" % len(probe), mt.djb2(probe), _djb2_reference(probe))

    name, psk = b"LongFast", bytes(range(16))
    eq("channel_hash", mt.channel_hash(name, psk),
       _xor_reference(name) ^ _xor_reference(psk))

    # build_flags packs five things into one byte, and parse_header takes them
    # apart again. Running one into the other covers both without either
    # having to be written down here.
    for hop_limit, want_ack, via_mqtt, hop_start in (
            (3, False, False, 3), (7, True, True, 7), (0, True, False, 5)):
        flags = mt.build_flags(hop_limit, want_ack, via_mqtt, hop_start)
        frame = mt.write_header(0xFFFFFFFF, 0xDEADBEEF, 0xA7C16FC0,
                                flags, 0x08, 0, 0)
        h = mt.parse_header(frame)
        to, frm, pid = h[0], h[1], h[2]
        ok("header to", to == 0xFFFFFFFF, "0x%08X" % to)
        ok("header from", frm == 0xDEADBEEF, "0x%08X" % frm)
        ok("header id", pid == 0xA7C16FC0, "0x%08X" % pid)
        eq("header channel", h[4], 0x08)
        eq("header hop_limit", h[7], hop_limit)
        eq("header hop_start", h[8], hop_start)
        eq("header want_ack", h[9], want_ack)
        eq("header via_mqtt", h[10], via_mqtt)

    # A 32-bit node number arriving as -1 is the classic sign that a value came
    # back through a signed small int somewhere. It is checked above as an
    # equality; this states the intent.
    ok("node numbers are unsigned",
       mt.parse_header(mt.write_header(0xFFFFFFFF, 0xFFFFFFFF, 1,
                                       0, 0, 0, 0))[1] > 0)

    # Data frames: encode then parse, which exercises the protobuf writer and
    # reader against each other.
    payload = b"hello from the W12"
    frame = mt.encode_data(1, payload, True, None)
    d = mt.parse_data(frame)
    eq("data portnum", d[0], 1)
    eq("data payload_len", d[2], len(payload))
    eq("data payload", bytes(frame[d[1]:d[1] + d[2]]), payload)
    eq("data want_response", d[8], True)
    eq("data bitfield absent", d[9], None)
    d = mt.parse_data(mt.encode_data(1, payload, False, 1))
    eq("data bitfield present", d[9], 1)

    # Presets and regions are tables. Checking that they are monotonic and in
    # range catches a table that got relocated to the wrong address, which is
    # what a broken GOT entry looks like from Python.
    for preset in range(8):
        try:
            sf, bw, cr = mt.preset_params(preset, False)
        except (ValueError, RuntimeError):
            # The table is shorter than the loop; anything past the end says
            # so rather than returning its last row again.
            continue
        ok("preset %d" % preset, 5 <= sf <= 12 and 7000 <= bw <= 500000
           and 5 <= cr <= 8, "sf%d bw%d cr4/%d" % (sf, bw, cr))

    start, end, spacing, duty, power, wide = mt.region_info(1)
    info("region 1", "%d..%d Hz, %d Hz slots, %d%% duty, %d dBm, wide=%s"
         % (start, end, spacing, duty, power, wide))
    ok("region span is sane", end > start > 100_000_000)

    # Airtime is a float-free calculation over 64-bit intermediates, which is
    # exactly the arithmetic that needed libgcc linked in.
    us = mt.airtime_us(64, 11, 250000, 5)
    info("airtime sf11/250k/64B", "%d ms" % (us // 1000))
    ok("airtime is plausible", 100_000 < us < 5_000_000, "%d us" % us)
    ok("airtime grows with payload",
       mt.airtime_us(128, 11, 250000, 5) > us)
    ok("airtime shrinks with bandwidth",
       mt.airtime_us(64, 11, 500000, 5) < us)

    for mv, lo, hi in ((3000, 0, 10), (4200, 95, 100)):
        pct = mt.battery_percent(mv)
        ok("battery_percent(%d mV)" % mv, lo <= pct <= hi, "%d%%" % pct)


# ─── 4. LR2021 FLRC tables ───────────────────────────────────────────────────

#: The combined bitrate/bandwidth enum, fastest first. One knob, eight values;
#: the bandwidth is not separately settable. Written down here so that the
#: check is against the datasheet rather than against the Rust restating
#: itself.
FLRC_RATES = (
    (2600, 2666), (2080, 2222), (1300, 1333), (1040, 1333),
    (650, 888), (520, 769), (325, 444), (260, 444),
)

#: Not in rate order, which is the trap: picking a coding rate by arithmetic
#: instead of by name gets 1/2 where 2/3 was meant.
CR_1_2, CR_3_4, CR_1_1, CR_2_3 = 0x00, 0x01, 0x02, 0x03


def stage_flrc_tables():
    if lr2021 is None:
        skip("flrc tables", "module did not import")
        return

    for code, (kbps, khz) in enumerate(FLRC_RATES):
        eq("rate %d bitrate" % code, lr2021.flrc_bitrate_kbps(code), kbps)
        eq("rate %d bandwidth" % code, lr2021.flrc_bandwidth_khz(code), khz)

    ok("rates are fastest first",
       all(lr2021.flrc_bitrate_kbps(i) > lr2021.flrc_bitrate_kbps(i + 1)
           for i in range(7)))

    # Off the end of the table has to raise rather than return a neighbour: a
    # silently clamped rate code configures a radio the other end cannot hear.
    for bad in (8, 255, -1):
        try:
            lr2021.flrc_bitrate_kbps(bad)
            ok("rate %d rejected" % bad, False, "returned a value")
        except RuntimeError:
            ok("rate %d rejected" % bad, True)

    # Two bytes, packed the way SetFlrcModulationParams wants them.
    packed = lr2021.flrc_modulation_params(0, CR_2_3, 0x05)
    eq("modulation params brBw", (packed >> 8) & 0xFF, 0x00)
    eq("modulation params cr/shaping", packed & 0xFF, (CR_2_3 << 4) | 0x05)

    # Goodput and PER are the numbers a link report is made of, and both are
    # integer arithmetic over values that overflow 32 bits half way through.
    eq("goodput of nothing", lr2021.flrc_goodput_bps(0, 0), 0)
    bps = lr2021.flrc_goodput_bps(500 * 255, 1000)
    ok("goodput of a full burst", 1_000_000 < bps < 2_000_000, "%d bps" % bps)
    eq("per of a clean link", lr2021.flrc_per_permille(500, 500), 0)
    eq("per of a dead link", lr2021.flrc_per_permille(500, 0), 1000)
    eq("per of half a link", lr2021.flrc_per_permille(500, 250), 500)
    eq("per clamps a surplus", lr2021.flrc_per_permille(500, 600), 0)


# ─── 5. board peripherals ────────────────────────────────────────────────────

def stage_board():
    import analogio
    import digitalio

    # The OLED sits behind VEXT, which is active low and undriven at boot, so
    # an I2C scan before this finds an empty bus and says nothing.
    vext = digitalio.DigitalInOut(board.VEXT_ENABLE)
    vext.switch_to_output(value=False)
    time.sleep(0.1)
    # board.I2C() is a singleton the rest of the image shares, so it is
    # unlocked when this stage is done with it and never deinitialised.
    i2c = board.I2C()
    while not i2c.try_lock():
        pass
    try:
        found = i2c.scan()
    finally:
        i2c.unlock()
    info("i2c devices", ", ".join("0x%02X" % a for a in found) or "none")
    ok("ssd1315 oled at 0x3c", 0x3C in found)

    # The divider is 390K/100K and ADC_CTRL gates its bottom leg. Undriven, the
    # pin reads a hard zero rather than something floating, so a zero here is
    # "the gate is off", not "the battery is flat".
    ctrl = digitalio.DigitalInOut(board.ADC_CTRL)
    ctrl.switch_to_output(value=True)
    time.sleep(0.05)
    adc = analogio.AnalogIn(board.BATTERY)
    raw = sum(adc.value for _ in range(16)) // 16
    at_pin_mv = raw * adc.reference_voltage * 1000 / 65535
    battery_mv = at_pin_mv * (390 + 100) / 100
    adc.deinit()
    ctrl.value = False
    ctrl.deinit()
    info("battery adc", "raw %d, %d mV at the pin" % (raw, at_pin_mv))
    info("battery", "%d mV" % battery_mv)
    # USB-powered with no cell attached reads near zero, which is not a fault.
    if battery_mv < 500:
        skip("battery voltage", "no cell attached (%d mV)" % battery_mv)
    else:
        ok("battery voltage", 3000 < battery_mv < 4400, "%d mV" % battery_mv)

    # Both PA rails have NC pull-ups, so nothing but firmware can turn them on.
    # Driving them is the test: if the pin is wrong the radio stage after this
    # hears nothing and the reason is here.
    for name in ("PA_EN_2G4", "PA_EN_SUBGHZ"):
        pin = digitalio.DigitalInOut(getattr(board, name))
        pin.switch_to_output(value=True)
        ok("%s drives high" % name, pin.value)
        pin.value = False
        pin.deinit()

    try:
        import neopixel_write
        led = digitalio.DigitalInOut(board.NEOPIXEL)
        led.switch_to_output()
        for colour in ((16, 0, 0), (0, 16, 0), (0, 0, 16), (0, 0, 0)):
            neopixel_write.neopixel_write(led, bytearray(colour))
            time.sleep(0.15)
        led.deinit()
        ok("rgb led", True, "red, green, blue, off")
    except ImportError:
        skip("rgb led", "no neopixel_write in this build")

    btn = digitalio.DigitalInOut(board.BUTTON)
    btn.switch_to_input(pull=digitalio.Pull.UP)
    time.sleep(0.01)
    # BOOT is also the strapping pin, so it has to read high here; low would
    # mean it is stuck, which would put the chip in the ROM loader next boot.
    ok("boot button released", btn.value, "reads %s" % btn.value)
    btn.deinit()

    vext.deinit()


# ─── 6. LR2021 over SPI ──────────────────────────────────────────────────────

CMD_GET_STATUS = 0x0100
CMD_GET_VERSION = 0x0101
CMD_ACTIVATE_PRAM = 0x012D
CMD_SET_STANDBY = 0x0128

#: Bytes of status in front of a reply on this part, against the LR1121's one.
STATUS_LEN = 2


class Probe:
    """The smallest thing that can put two bytes on the bus and read the reply.

    Deliberately not the driver in `lr2021_flrc.py`: this one reads raw and
    prints raw, so the status bytes and the payload can be looked at without
    the split between them being assumed first.
    """

    def __init__(self, baudrate=2_000_000):
        import busio
        import digitalio
        self._spi = busio.SPI(board.LORA_SCK, board.LORA_MOSI, board.LORA_MISO)
        while not self._spi.try_lock():
            pass
        # Slow on purpose. 2 MHz is well inside anything the part or the
        # traces can be blamed for, so a bad read here is a wiring fault and
        # not a timing one.
        self._spi.configure(baudrate=baudrate, polarity=0, phase=0)
        self.cs = digitalio.DigitalInOut(board.LORA_CS)
        self.cs.switch_to_output(value=True)
        self.busy = digitalio.DigitalInOut(board.LORA_BUSY)
        self.busy.switch_to_input()
        self.irq = digitalio.DigitalInOut(board.LORA_DIO1)
        self.irq.switch_to_input()
        self.reset_pin = digitalio.DigitalInOut(board.LORA_RESET)
        self.reset_pin.switch_to_output(value=True)
        self.pa = digitalio.DigitalInOut(board.PA_EN_2G4)
        self.pa.switch_to_output(value=True)

    def deinit(self):
        for pin in (self.cs, self.busy, self.irq, self.reset_pin, self.pa):
            pin.deinit()
        self._spi.unlock()
        self._spi.deinit()

    def wait_busy(self, timeout_ms=500):
        deadline = time.monotonic_ns() + timeout_ms * 1_000_000
        while self.busy.value:
            if time.monotonic_ns() > deadline:
                return False
        return True

    def reset(self):
        self.reset_pin.value = False
        time.sleep(0.005)
        self.reset_pin.value = True
        time.sleep(0.020)
        return self.wait_busy()

    def command(self, opcode, args=b""):
        self.wait_busy()
        self.cs.value = False
        self._spi.write(bytes((opcode >> 8, opcode & 0xFF)) + bytes(args))
        self.cs.value = True

    def reply(self, opcode, length, args=b""):
        """Sends a command and returns the whole second transaction, raw."""
        self.command(opcode, args)
        self.wait_busy()
        buf = bytearray(length)
        self.cs.value = False
        self._spi.readinto(buf)
        self.cs.value = True
        return bytes(buf)


def _hex(data):
    return " ".join("%02x" % b for b in data)


def stage_radio():
    probe = Probe()
    try:
        # BUSY is the first thing that says the part is alive and powered. It
        # is high while the radio is working and low when it is idle; a line
        # that never moves is either not connected or the chip is dead.
        ok("busy line settles low", probe.reset(),
           "busy=%s after reset" % probe.busy.value)
        info("dio8/irq at rest", probe.irq.value)

        # Read more than any reply needs, so the status bytes and the payload
        # can both be looked at without deciding in advance where the boundary
        # is.
        raw = probe.reply(CMD_GET_VERSION, 8)
        info("GetVersion raw", _hex(raw))

        alive = not (all(b == 0x00 for b in raw) or all(b == 0xFF for b in raw))
        ok("miso is not stuck", alive,
           "all-zero or all-ones means CS, MISO or power" if not alive else "")

        if alive:
            # Settled on hardware in favour of two: with one status byte the
            # version body moves when the status changes across the PRAM
            # activate below, and with two it is identical both times. Both
            # readings are still printed so a different part shows itself.
            #
            # A version reply is hardware, use case, firmware major, firmware
            # minor. The hardware byte is the tell: it is a small non-zero
            # number, and firmware major is small too.
            for status_len in (1, 2):
                body = raw[status_len:status_len + 4]
                plausible = body[0] not in (0x00, 0xFF) and body[2] < 0x20
                print("  ----  %-34s status=%s version=%s %s"
                      % ("reading with %d status bytes" % status_len,
                         _hex(raw[:status_len]), _hex(body),
                         "<- plausible" if plausible else ""))

        status = probe.reply(CMD_GET_STATUS, 6)
        info("GetStatus raw", _hex(status))

        # Patch RAM before anything else touches a radio block. On this part
        # the blocks are not necessarily complete without it, and skipping it
        # produces a radio that configures cleanly and then never transmits --
        # a failure with no symptom at the point where it is caused.
        probe.command(CMD_ACTIVATE_PRAM, b"\x00")
        ok("pram activate returns", probe.wait_busy(1000),
           "busy stayed high" if probe.busy.value else "")
        after = probe.reply(CMD_GET_VERSION, 8)
        info("GetVersion after pram", _hex(after))
        # Only the version body has to be stable. The status bytes describe
        # whatever command ran last and legitimately differ between the two
        # reads, so comparing the whole reply would fail on a healthy part.
        body = raw[STATUS_LEN:STATUS_LEN + 4]
        after_body = after[STATUS_LEN:STATUS_LEN + 4]
        ok("version is stable across pram", after_body == body,
           "" if after_body == body
           else "%s -> %s" % (_hex(body), _hex(after_body)))

        probe.command(CMD_SET_STANDBY, b"\x00")
        ok("standby accepted", probe.wait_busy())
    finally:
        probe.deinit()


# ─── 7. on air ───────────────────────────────────────────────────────────────

def stage_on_air():
    if not TRANSMIT:
        skip("flrc transmit", "TRANSMIT is False")
        return
    if lr2021 is None:
        skip("flrc transmit", "lr2021 module did not import")
        return

    from lr2021_flrc import Flrc

    with Flrc() as radio:
        radio.reset()
        info("version", _hex(radio.version()))
        # Rate 0 is 2600 kbps in 2666 kHz, and CR_2_3 is what RadioLib's
        # beginFLRC defaults to. Shaping 0x05 is Gaussian BT 0.5.
        radio.begin(TX_FREQUENCY_HZ, 0, CR_2_3, 0x05, 0x12345678,
                    TX_POWER_DBM, 64)
        info("channel", "%d kbps / %d kHz at %d MHz"
             % (lr2021.flrc_bitrate_kbps(0), lr2021.flrc_bandwidth_khz(0),
                TX_FREQUENCY_HZ // 1_000_000))

        started = time.monotonic()
        sent = 0
        for seq in range(TX_PACKETS):
            body = b"W12" + bytes((seq & 0xFF,))
            if radio.transmit(body + bytes(64 - len(body))):
                sent += 1
        elapsed_ms = int((time.monotonic() - started) * 1000)

        # Every packet has to come back with TxDone. A radio that accepts the
        # configuration and then never finishes a transmission is what a
        # missing RF-switch setup looks like -- DIO5 and DIO6 are the
        # RFX2402E's TXEN and RXEN, and until the driver hands them over the
        # front end is shut down in both directions.
        ok("txdone for every packet", sent == TX_PACKETS,
           "%d/%d in %d ms" % (sent, TX_PACKETS, elapsed_ms))
        offered = lr2021.flrc_goodput_bps(sent * 64, elapsed_ms)
        info("offered rate", "%d kbps" % (offered // 1000))
        info("note", "this is a floor: the per-packet loop is Python")


# ─── run ─────────────────────────────────────────────────────────────────────

#: Set by stage 8 so the summary can be mirrored to the panel.
screen = None


def stage_oled():
    global screen
    from meshtastic import oled

    # The module's shared panel, not a fresh `Screen`: a second one calls
    # release_displays(), and whatever drew on the first is then drawing on a
    # display nothing owns.
    screen = oled.panel(force=True)
    info("panel", "%dx%d, %d columns by %d rows of %dx%d text"
         % (oled.WIDTH, oled.HEIGHT, screen.columns, screen.rows,
            screen.glyph_width, screen.glyph_height))
    ok("text fits the panel", screen.columns >= 16 and screen.rows >= 4)

    # A panel that is powered but unconfigured shows noise, and one that is
    # configured but unwritten shows nothing; neither is distinguishable from
    # a dead panel over I2C, because the SSD1315 never answers back. Walking
    # the rows is what makes a wrong rotation or a wrong COM wiring visible.
    for row in range(screen.rows):
        screen.line(row, ("%d" % row) + "." * (screen.columns - 1))
        time.sleep(0.12)
    screen.clear()

    screen.lines("Meshnology W12", "", "self-test running")
    ok("panel accepts text", True, "look at the screen")
    # Proven, so the summary and everything after it can be read off the panel
    # as well. Not before: a stage cannot report through the thing it is testing.
    oled.tee()


def main():
    # A display registered by a previous run survives a soft reload and keeps
    # refreshing over I2C from the task that also drains USB, which stalls
    # every print below until the next hard reset. Stage 8 is far too late.
    import displayio
    displayio.release_displays()

    print()
    print("Meshnology W12 self-test")
    started = time.monotonic()

    stage("1. firmware", stage_firmware)
    stage("2. native modules", stage_natmods)
    stage("3. meshtastic codec", stage_codec)
    stage("4. lr2021 flrc tables", stage_flrc_tables)
    stage("5. board peripherals", stage_board)
    stage("6. lr2021 over spi", stage_radio)
    stage("7. on air", stage_on_air)
    stage("8. oled", stage_oled)

    section("summary")
    print("  %d passed, %d failed, %d skipped in %.1f s"
          % (_passed, _failed, _skipped, time.monotonic() - started))
    gc.collect()
    print("  %d bytes free" % gc.mem_free())
    if _failed:
        print()
        print("  Read the first failure, not the last: stage 6 cannot pass if")
        print("  stage 2 did not, and stage 7 cannot pass if stage 6 did not.")

    if screen is not None:
        from meshtastic import oled

        # Stop mirroring first: the lines below would otherwise scroll straight
        # over the summary they are introducing.
        oled.tee(False)
        screen.lines(
            "Meshnology W12",
            "%d pass  %d fail" % (_passed, _failed),
            "%d skipped" % _skipped,
            "FAILED -- see USB" if _failed else "all checks passed",
            "",
        )
        # CircuitPython gives every display back to the REPL the moment this
        # file returns, so the summary would flash past. Holding here keeps it
        # up; Ctrl-C at the REPL ends it.
        print()
        print("  Holding so the panel keeps the summary. This is not a hang.")
        print("  Ctrl-C stops it, Ctrl-D runs the whole thing again.")
        # A silent loop and a crashed board look identical over USB, so the
        # last row is left blank above for this to tick in.
        ticks = 0
        try:
            while True:
                time.sleep(1)
                ticks += 1
                screen.line(screen.rows - 1, "holding %d s" % ticks)
        except KeyboardInterrupt:
            # Otherwise the REPL inherits the panel and refreshes it forever.
            oled.hush()
            raise


main()
