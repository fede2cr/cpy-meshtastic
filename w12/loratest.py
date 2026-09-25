"""Brings the LR2021's LoRa side up a stage at a time, and says what it saw.

Everything about this radio below the SPI wires has been written against
RadioLib's source rather than a datasheet, and several things in it are honest
guesses. This file exists to turn those guesses into observations. Run it from
the REPL so a stage that fails leaves the radio open for poking at:

    >>> import loratest
    >>> loratest.run()

`run()` stops short of transmitting. Keying the sub-GHz amplifier into a port
with no antenna on it can destroy the amplifier, so that is a separate call:

    >>> loratest.transmit()

What each stage can tell you, and what it cannot:

* **identify** fails if nothing is driving MISO. That is the one clean
  hardware/no-hardware answer here. It does *not* check that the part is an
  LR2021: its version reply carries a firmware number and nothing else, so a
  different chip on the same pins would pass.
* **floor** is the stage that catches a dead receive path -- or a wrong opcode,
  which is what it caught the first time it was run. It judges the reading
  against thermal noise in the bandwidth actually in use, since kTB rises 3 dB
  per doubling and a fixed threshold is meaningless. It cannot tell you an
  antenna is attached: an open input reads much the same as a connected one on
  a quiet band.
* **cad** calls the driver under the adapter so it can tell a timeout apart
  from a clear channel. `Radio.busy()` reports both as busy, which is the right
  thing for a node and the wrong thing for a bring-up.
* **receive** is the only stage that proves the antenna and the board's
  antenna switch, because it is the only one that needs a signal to have
  arrived. A quiet channel proves nothing either way; one decoded packet
  proves the whole chain at once.

Stages after the first keep the same radio object, so they can be re-run
individually. `radio` and `settings` are left at module scope for that.
"""

import time
import math
from binascii import hexlify

import supervisor

import meshtastic as nat
from meshtastic import meshlib as mesh

radio = None
settings = None


#: Bit names from the chip's error register, LSB first.
_ERRORS = (
    "HF XOSC failed to start",
    "LF XOSC failed to start",
    "PLL failed to lock",
    "LF RC calibration failed",
    "HF RC calibration failed",
    "PLL calibration failed",
    "anti-aliasing filter calibration failed",
    "image rejection calibration failed",
    "Tx or Rx refused, chip busy",
    "no front-end calibration for this Rx frequency",
    "measurement unit ADC calibration failed",
    "PA offset calibration failed",
    "poly-phase filter calibration failed",
    "self-reception cancellation calibration failed",
    "RSSI saturated during SRC calibration",
    "self-reception cancellation out of tolerance",
)

#: What GetRssiInst returns when the receiver is not running. -174 dBm/Hz is
#: thermal noise power density at room temperature, the floor no receiver beats.
_NO_READING_DBM = -250.0
_KTB_DBM_PER_HZ = -174.0


def _title(text):
    print()
    print("-- %s" % text)


def build():
    """Constructs the adapter, without talking to the chip yet."""
    global radio
    _title("build")
    if radio is not None:
        radio.close()
        radio = None
    # repl.py has already started a node, and it holds these pins and the bus.
    mesh.release()
    chosen = supervisor.get_setting("MESH_RADIO", None)
    print("MESH_RADIO = %r" % chosen)
    if chosen != "lr2021":
        print("   settings.toml is missing or stale; forcing lr2021 for now")
    radio = mesh.open_radio("lr2021")
    print("built %s" % radio.description)
    return radio


def identify():
    """Resets the part and reads its version. First contact over SPI."""
    _title("identify")
    radio.open()
    print(radio.description)
    print("state %s" % radio.state())
    return radio.description


def configure(region=mesh.US, preset=mesh.LONG_FAST, power_dbm=None):
    """Tunes to a real Meshtastic channel, through the node's own path."""
    global settings
    _title("configure")
    settings = mesh.Settings(region=region, preset=preset)
    print(settings)
    mesh.protocol_configure(radio, settings, power_dbm=power_dbm)
    print("tuned, %+d dBm at the chip -- the board's amplifier adds an unknown"
          % radio.power_dbm)
    print("amount on top of that, so this is a floor and not the radiated power")
    return settings


def errors():
    """Reads and decodes the chip's own error register, then clears it.

    Every calibration this driver kicks off reports here rather than through a
    command status, so a calibration that quietly failed is invisible until
    something is read out of this register.
    """
    _title("errors")
    raw = radio_errors()
    if not raw:
        print("none (0x0000)")
        return 0
    print("0x%04X" % raw)
    for bit, name in enumerate(_ERRORS):
        if raw & (1 << bit):
            print("   bit %-2d %s" % (bit, name))
    return raw


def radio_errors(clear=True):
    import lr2021

    raw = lr2021.get_errors(radio._bus)
    if clear:
        lr2021.clear_errors(radio._bus)
    return raw


def floor(samples=10):
    """Reads the noise floor listening, then in standby, and judges it.

    The verdict is against thermal noise in the bandwidth actually in use, not
    against a fixed number: kTB is 3 dB higher every time the bandwidth
    doubles, so -116 dBm is a quiet receiver at 250 kHz and a deaf one at
    31 kHz. The level is what decides, not how much it moves -- readings land
    on half-decibel steps and a quiet band repeats the same one happily. What
    this can prove is that the receiver is running and its gain is sane. What
    it cannot prove is that an antenna is attached: an open input also delivers
    roughly thermal noise.
    """
    _title("floor")
    radio.listen()
    time.sleep(0.05)
    live = [radio.noise() for _ in range(samples)]
    radio.standby()
    idle = [radio.noise() for _ in range(samples)]
    print("listening: %s" % " ".join("%.1f" % v for v in live))
    if max(idle) <= _NO_READING_DBM:
        print("standby:   no reading, which is what a stopped receiver owes us")
    else:
        print("standby:   %s" % " ".join("%.1f" % v for v in idle))

    # log10 is behind MICROPY_PY_MATH_SPECIAL_FUNCTIONS and this board is built
    # without it.
    ktb = _KTB_DBM_PER_HZ + 10.0 * (math.log(settings.bw_hz) / math.log(10))
    best = max(live)
    implied_nf = best - ktb
    print("thermal floor at %.0f kHz is %.1f dBm, so this receiver is showing"
          % (settings.bw_hz / 1000.0, ktb))
    print("a noise figure of about %.1f dB" % implied_nf)

    if best <= _NO_READING_DBM or best == 0.0:
        print("That is the value a stopped receiver or an all-zero reply gives,")
        print("not a measurement. Suspect the driver: a wrong opcode or reply")
        print("length still decodes to a number that looks like dBm.")
        return False
    if implied_nf < 0:
        print("Below thermal, which is impossible. The decode is wrong.")
        return False
    if implied_nf > 15:
        print("Higher than a receiver should manage, so either something")
        print("nearby is transmitting or the gain mode is wrong.")
        return False
    if len(set(live)) == 1:
        # RSSI lands on half-decibel steps, so a quiet band repeating one value
        # ten times is ordinary. Only the level decides whether it is real.
        print("Healthy, though every sample read the same. At half-decibel")
        print("steps on a quiet channel that is unremarkable.")
    else:
        print("Healthy.")
    print("None of this says the antenna is attached: an open input reads much")
    print("the same. Only traffic or a link test settles that.")
    return True


def cad(rounds=5):
    """Runs channel activity detection, separating 'clear' from 'no answer'.

    Reaches past the adapter to the driver on purpose: `Radio.busy()` restores
    whatever receive state it found, and watching it do that is not the same as
    watching a bare CAD answer.
    """
    import lr2021

    _title("cad")
    bus = radio._bus
    peak = mesh.CAD_DET_PEAK[settings.sf - 5]
    print("SF%d, %d symbols, detection peak %d" % (settings.sf,
                                                   mesh.CAD_SYMBOLS, peak))
    outcomes = []
    for _ in range(rounds):
        # Stated rather than inherited from whatever stage ran last: a CAD
        # issued while the part is receiving never starts, and that read as a
        # busy channel is what this stage exists to catch.
        radio.standby()
        lr2021.get_and_clear_irq_status(bus)
        lr2021.lora_set_cad_params(bus, mesh.CAD_SYMBOLS, False,
                                   mesh.CAD_PNR_DELTA_STANDARD,
                                   mesh.CAD_EXIT_MODE_FALLBACK, 0, peak)
        lr2021.lora_set_cad(bus)
        until = supervisor.ticks_ms() + 500
        result = "no answer"
        while mesh.ticks_diff(supervisor.ticks_ms(), until) < 0:
            flags = lr2021.get_and_clear_irq_status(bus)
            if flags & mesh.IRQ_CAD_DETECTED:
                result = "activity"
                break
            if flags & mesh.IRQ_CAD_DONE:
                result = "clear"
                break
        outcomes.append(result)
        time.sleep(0.05)
    radio.standby()
    print(", ".join(outcomes))
    if all(r == "no answer" for r in outcomes):
        print("CAD never completed. The parameters here are RadioLib's and the")
        print("detection peaks are only valid for a 2-symbol scan, so this is")
        print("the stage most likely to be wrong rather than the hardware.")
    return outcomes


def receive(seconds=30):
    """Listens, printing anything that arrives and the counters either way."""
    _title("receive")
    before = radio.counters()
    radio.listen()
    print("listening %d s on %.4f MHz" % (seconds, settings.frequency_hz / 1e6))
    heard = 0
    until = supervisor.ticks_ms() + seconds * 1000
    while mesh.ticks_diff(supervisor.ticks_ms(), until) < 0:
        packet = radio.collect()
        if packet:
            heard += 1
            rssi, snr, signal = radio.quality()
            print("%3d bytes  %.1f dBm  SNR %.2f dB  signal %.1f dBm"
                  % (len(packet), rssi, snr, signal))
            if len(packet) >= mesh.HEADER_LEN:
                # The payload stays encrypted -- this is the cleartext header
                # every node needs to route, printed as the firmware logs it.
                print("     %s" % mesh.Packet(packet, rssi, snr,
                                              signal).describe())
            else:
                print("     runt: %s" % hexlify(packet).decode())
        time.sleep(0.01)
    radio.standby()
    after = radio.counters()
    names = ("received", "crc errors", "header crc errors", "false syncs")
    moved = False
    for name, was, now in zip(names, before, after):
        delta = now - was
        moved = moved or delta
        print("%-18s %d" % (name, delta))
    if not heard and not moved:
        print("nothing on any counter. That is weak evidence on its own -- a")
        print("correct receiver on a quiet channel can sit at zero for twenty")
        print("seconds -- so read it together with `floor` above rather than")
        print("on its own, and re-run it for longer before concluding.")
    return heard


def transmit(text="lr2021 bring-up", count=3, listen_after=10):
    """Keys the transmitter. Attach a sub-GHz antenna before calling this.

    Sends a bare payload rather than a Meshtastic frame: nothing here should be
    decodable by a real node, only detectable by a receiver you are watching.
    """
    _title("transmit")
    payload = text.encode("utf-8")
    expected = nat.airtime_us(len(payload), settings.sf, settings.bw_hz,
                              settings.cr) / 1000.0
    print("%d bytes, %.0f ms of airtime each by the node's own model"
          % (len(payload), expected))
    sent = 0
    for n in range(count):
        started = supervisor.ticks_ms()
        ok = radio.send(payload)
        took = mesh.ticks_diff(supervisor.ticks_ms(), started)
        sent += ok
        print("%d: %s after %d ms" % (n + 1, "TxDone" if ok else "NO TxDone",
                                      took))
        time.sleep(0.5)
    if not sent:
        print("The part never raised TxDone. That is a configuration failure")
        print("rather than an antenna one -- an open port still completes a")
        print("transmit, it just radiates nothing.")
    if listen_after:
        receive(listen_after)
    return sent


def run(seconds=20):
    """Every stage that cannot transmit, in order, stopping at the first fault."""
    _mirror()
    build()
    identify()
    configure()
    errors()
    floor()
    cad()
    receive(seconds)
    print()
    print("Done. `transmit()` is the remaining stage, and it keys the")
    print("amplifier -- attach a 915 MHz antenna to the sub-GHz port first.")
    return radio


def _mirror(on=True):
    """Echoes what this prints onto the OLED, if the drive has one."""
    try:
        from meshtastic import oled
    except ImportError:
        return
    oled.tee(on)
