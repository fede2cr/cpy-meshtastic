"""The Semtech LR2021 on its sub-GHz path, as the node wants to see it.

Everything chip-specific about running Meshtastic on an LR2021 is in this file:
the register code tables, the front-end wiring, and which of the part's two
radios a frequency belongs to. The node above it never learns any of that.

The `lr2021` native module this wraps is a general-purpose driver and knows
nothing about Meshtastic. It is also a thinner layer than `lr1121` is: there is
no Python class between this file and the Rust, so the bus tuple and the reset
line are built here.

Three things this adapter cannot do honestly yet, all of them because the part
is documented only through RadioLib's source:

* **The external PA is not in the power budget.** `power_ceiling` reports what
  the LR2021's own low-band PA can produce. The W12 puts an amplifier after it
  on the sub-GHz port, and its gain is not in any reference available here, so
  what actually leaves the antenna is higher than what this file admits to by
  an unknown amount. Anyone taking this to a regulator rather than to a bench
  needs to measure it.
* **Semtech's DC-DC workaround is not applied.** RadioLib reruns it after every
  `SetRxPath` and `SetLoRaModulationParams`; it poke at the switcher registers
  through commands this driver does not implement. Its absence should cost
  efficiency and spectral cleanliness rather than the link itself, but that is
  reasoning rather than a measurement.
* **`rx_boosted` is accepted and ignored.** See `GAIN_MODE_LF`.

Nothing here configures the sub-GHz antenna switch, and nothing needs to. The
board's own Meshtastic variant states that the switch's three control lines are
hardwired to the radio's CTX and CPS outputs, and a received packet has since
settled it: the 2.4 GHz front end on the same part does need a DIO table, this
one does not. `lr2021.set_dio_function` and `lr2021.set_dio_rf_switch_config`
remain exposed against DIO11 (CSD), DIO10 (CPS) and DIO9 (CTX), for a board
that wires it differently.
"""

import time

import board
import busio
import digitalio
import supervisor

import meshtastic as _mt

from meshtastic import meshlib as mt

# Loaded by `open_lr2021`, not here. This module is merged into meshtastic.mpy,
# and importing lr2021.mpy from a module body nests one native load inside
# another and holds both sets of loader allocations at once.
_lr = None

# ─── register codes, mirroring lr2021/src/cmd.rs ─────────────────────────────
STANDBY_RC = 0x00
PACKET_TYPE_LORA = 0x00
CALIBRATE_ALL = 0x7F
RX_CONTINUOUS = 0x00FFFFFF
RX_PATH_LF = 0x00

#: The part takes a 0..7 gain-mode level rather than a boost flag, and RadioLib
#: leaves it at 0 for the low band with no documented alternative. So
#: `rx_boosted` cannot be honoured: guessing a level would change sensitivity by
#: an unknown amount in an unknown direction. Set `gain_mode` on the adapter if
#: a value is ever established.
GAIN_MODE_LF = 0x00

LORA_HEADER_EXPLICIT = 0x00
LORA_CRC_ON = 0x01
LORA_IQ_STANDARD = 0x00

#: The payload length to arm an explicit-header receive with.
#:
#: The chip has one length field for both directions: transmitting sets it to
#: the length going out, while receiving reads it as the largest payload the
#: modem will accept. A transmit length left behind there makes the part reject
#: every longer packet at header-decode time -- no interrupt, no CRC error, the
#: packet simply never arrives. 255 is the whole of what a LoRa header can
#: describe, so nothing real is excluded.
_RX_ANY_LENGTH = 0xFF

#: 48 us. Affects the spectral mask rather than whether the link closes.
PA_RAMP_48U = 0x05

#: The chip's own low-band amplifier, before the board's. See the module note.
MAX_POWER_DBM = 22

IRQ_RX_DONE = 1 << 18
IRQ_TX_DONE = 1 << 19
IRQ_CAD_DONE = 1 << 20
IRQ_TIMEOUT = 1 << 21
IRQ_CRC_ERROR = 1 << 22
IRQ_LEN_ERROR = 1 << 23
IRQ_CAD_DETECTED = 1 << 7
IRQ_LORA_HDR_CRC_ERROR = 1 << 9
#: Everything that ends a receive, good or bad.
IRQ_RX_ANY = (IRQ_RX_DONE | IRQ_TIMEOUT | IRQ_CRC_ERROR | IRQ_LEN_ERROR
              | IRQ_LORA_HDR_CRC_ERROR)

#: What the part may raise on `IRQ_DIO`. Only the flags this adapter acts on:
#: the FIFO-level ones re-assert the moment they are cleared, and one of those
#: on the pin is an edge per SPI poll rather than an edge per packet.
IRQ_DIO_MASK = IRQ_RX_ANY | IRQ_TX_DONE | IRQ_CAD_DONE | IRQ_CAD_DETECTED

#: The radio DIO the board wires to LORA_DIO1. RadioLib's default of 5 is wrong
#: here -- DIO5 drives the 2.4 GHz front end and never reaches a header pin.
IRQ_DIO = 8

CAD_PNR_DELTA_STANDARD = 0x00
#: Leave CAD for the fallback mode rather than dropping straight into Rx or Tx,
#: so the decision stays with the caller.
CAD_EXIT_MODE_FALLBACK = 0x00
#: Two symbols, the count RadioLib's datasheet figures are quoted for.
CAD_SYMBOLS = 2
#: Detection threshold per spreading factor, SF5 first. Semtech's numbers, and
#: only valid for a two-symbol CAD.
CAD_DET_PEAK = (56, 56, 56, 58, 58, 60, 64, 68)

#: Bandwidth in Hz to the register code. Only the integral bandwidths appear:
#: the part's fractional set belongs to its 2.4 GHz radio and the driver refuses
#: it below a gigahertz.
_BW_CODES_LR2021 = {
    31_250: 0x02,
    62_500: 0x03,
    125_000: 0x04,
    250_000: 0x05,
    500_000: 0x06,
    1_000_000: 0x07,
}

#: How long to wait for BUSY on an ordinary command, and across a reset.
_BUSY_TIMEOUT_MS = 100
_RESET_TIMEOUT_MS = 500
#: A CAD of two symbols is under 70 ms even at SF12/125 kHz.
_CAD_TIMEOUT_MS = 500
#: Added to the computed time on air before a transmit is called failed.
_TX_MARGIN_MS = 250


class LR2021:
    """One LR2021 on the sub-GHz port, speaking `meshradio.Radio`."""

    def __init__(self, baudrate=8_000_000):
        self.description = "LR2021"
        #: The W12 has no allocated HardwareModel, and announcing `muzi-base`
        #: told the mesh it was the other board. The adapter carries this
        #: because it is the only module that differs between the two.
        self.hw_model = mt.HW_PRIVATE
        #: What was last commanded, for `state`. The part's status bytes carry
        #: a mode field, but its layout is not in any reference here and the
        #: driver deliberately does not decode it, so reporting a remembered
        #: mode is the honest version of this.
        self._mode = "off"
        self._listening = False
        self._sf = 12
        self._bw_hz = 125_000
        self._cr = 5
        self._preamble = 16
        self._power_dbm = 0
        #: The Rx gain level actually in use. See `GAIN_MODE_LF`.
        self.gain_mode = GAIN_MODE_LF

        self._spi = busio.SPI(board.LORA_SCK, board.LORA_MOSI, board.LORA_MISO)
        while not self._spi.try_lock():
            pass
        self._spi.configure(baudrate=baudrate, polarity=0, phase=0)
        self._cs = digitalio.DigitalInOut(board.LORA_CS)
        self._cs.switch_to_output(value=True)
        self._busy = digitalio.DigitalInOut(board.LORA_BUSY)
        self._busy.switch_to_input()
        self._reset = digitalio.DigitalInOut(board.LORA_RESET)
        self._reset.switch_to_output(value=True)
        # Active low, and left enabled: the OLED shares this rail, and whichever
        # of the two gets here first drives it on and keeps it there. Already
        # taken means already up, which is the state this wanted.
        try:
            self._vext = digitalio.DigitalInOut(board.VEXT_ENABLE)
            self._vext.switch_to_output(value=False)
        except ValueError:
            self._vext = None
        # Gates the LDO feeding the sub-GHz amplifier. The board's pull-up is
        # unpopulated, so without this the transmitter drives a dead stage and
        # the link looks like a range problem.
        self._pa = digitalio.DigitalInOut(board.PA_EN_SUBGHZ)
        self._pa.switch_to_output(value=True)

        self._bus = (self._spi, self._cs, self._busy, supervisor.ticks_ms,
                     _BUSY_TIMEOUT_MS)

    def _bus_for(self, timeout_ms):
        return self._bus[:4] + (timeout_ms,)

    # --------------------------------------------------------------- bring-up

    def open(self):
        self._reset.value = False
        time.sleep(0.005)
        self._reset.value = True
        time.sleep(0.020)
        _lr.wait_ready(self._bus_for(_RESET_TIMEOUT_MS))
        # Raises if nothing is driving MISO, which is the cheapest proof that
        # there is a chip on the other end rather than a floating bus.
        _lr.get_status(self._bus)
        # Patch RAM before anything else: the radio blocks are not necessarily
        # complete without it, and a node that configures cleanly and then never
        # transmits is what skipping this looks like.
        _lr.activate_pram(self._bus)
        _lr.set_standby(self._bus, STANDBY_RC)
        _lr.set_packet_type(self._bus, PACKET_TYPE_LORA)
        major, minor = _lr.get_version(self._bus)
        # Nothing in the reply identifies the part -- unlike the LR1121, this
        # one answers with a firmware version and nothing else -- so reaching
        # here is the whole of the presence check.
        self.description = "LR2021 fw %d.%d" % (major, minor)
        self._mode = "standby"
        self.reset_counters()
        return self

    def close(self):
        for pin in (self._cs, self._busy, self._reset, self._vext, self._pa):
            if pin is not None:
                pin.deinit()
        self._spi.unlock()
        self._spi.deinit()
        self._mode = "off"

    # ----------------------------------------------------------------- tuning

    def power_ceiling(self, frequency_hz):
        return MAX_POWER_DBM

    def tune(self, *, frequency_hz, sf, bw_hz, cr, preamble, sync_word,
             power_dbm, rx_boosted=True):
        try:
            bandwidth = _BW_CODES_LR2021[bw_hz]
        except KeyError:
            raise ValueError("no LR2021 sub-GHz bandwidth code for %d Hz" % bw_hz)
        if not 5 <= cr <= 8:
            raise ValueError("coding rate must be 5..8, got %r" % (cr,))
        if not 5 <= sf <= 12:
            raise ValueError("spreading factor must be 5..12, got %r" % (sf,))

        ldro = _lr.lora_ldro_needed(sf, bandwidth)
        # The short-interleaver rates. The part also has long-interleaver
        # variants at 0x05..0x09, which are a different waveform and are not
        # interoperable with an SX126x node.
        _lr.lora_set_modulation_params(self._bus, sf, bandwidth, cr - 4, ldro,
                                       False)
        # Length is reapplied per transmit and per receive: with an explicit
        # header it is what is about to go out, but on the way in it is the
        # largest packet the modem will accept. `listen` re-arms it.
        _lr.lora_set_packet_params(self._bus, preamble, LORA_HEADER_EXPLICIT,
                                   _RX_ANY_LENGTH,
                                   LORA_CRC_ON, LORA_IQ_STANDARD)
        _lr.lora_set_sync_word(self._bus, sync_word)
        _lr.set_rf_frequency(self._bus, frequency_hz)
        _lr.calibrate(self._bus, CALIBRATE_ALL)
        _lr.set_rx_path(self._bus, RX_PATH_LF, self.gain_mode)
        _lr.set_power_dbm(self._bus, power_dbm, PA_RAMP_48U)
        _lr.set_dio_irq_config(self._bus, IRQ_DIO, IRQ_DIO_MASK)

        self._sf, self._bw_hz, self._cr = sf, bw_hz, cr
        self._preamble, self._power_dbm = preamble, power_dbm
        self._listening = False
        self._mode = "standby"

    @property
    def power_dbm(self):
        return self._power_dbm

    # ---------------------------------------------------------------- traffic

    def listen(self):
        # Undoes the ceiling the last `send` left in the length field, which on
        # the way in is the largest packet the modem will accept rather than a
        # description of one. Left alone it costs every packet longer than the
        # last one sent, silently: no interrupt, no CRC error, just absence.
        _lr.lora_set_packet_params(self._bus, self._preamble,
                                   LORA_HEADER_EXPLICIT, _RX_ANY_LENGTH,
                                   LORA_CRC_ON, LORA_IQ_STANDARD)
        _lr.clear_rx_fifo(self._bus)
        _lr.get_and_clear_irq_status(self._bus)
        _lr.set_rx(self._bus, RX_CONTINUOUS)
        self._listening = True
        self._mode = "rx"

    @property
    def listening(self):
        return self._listening

    def standby(self):
        _lr.set_standby(self._bus, STANDBY_RC)
        self._listening = False
        self._mode = "standby"

    def send(self, data):
        """True if the packet left, False if the part gave up on it."""
        if len(data) > 255:
            raise ValueError("%d bytes is past what a LoRa header can describe"
                             % len(data))
        _lr.lora_set_packet_params(self._bus, self._preamble,
                                   LORA_HEADER_EXPLICIT, len(data),
                                   LORA_CRC_ON, LORA_IQ_STANDARD)
        _lr.clear_tx_fifo(self._bus)
        _lr.get_and_clear_irq_status(self._bus)
        _lr.write_tx_fifo(self._bus, data)
        # No chip-side timeout: the argument is in 30.52 us ticks and that
        # scaling is not confirmed on this part, while the deadline below is
        # in units this code owns. Reusing the node's own airtime model keeps
        # one formula rather than two that can disagree.
        _lr.set_tx(self._bus, 0)
        self._listening = False
        self._mode = "tx"
        limit = (_mt.airtime_us(len(data), self._sf, self._bw_hz, self._cr)
                 // 1000) + _TX_MARGIN_MS
        until = supervisor.ticks_ms() + limit
        while mt.ticks_diff(supervisor.ticks_ms(), until) < 0:
            flags = _lr.get_and_clear_irq_status(self._bus)
            if flags & IRQ_TX_DONE:
                self._mode = "standby"
                return True
            if flags & IRQ_TIMEOUT:
                self._mode = "standby"
                return False
        self._mode = "standby"
        return False

    def collect(self):
        """One received packet, or None. Never blocks."""
        if not self._listening:
            return None
        flags = _lr.get_and_clear_irq_status(self._bus)
        if not flags & IRQ_RX_ANY:
            return None
        if not flags & IRQ_RX_DONE or flags & (IRQ_CRC_ERROR | IRQ_LEN_ERROR
                                               | IRQ_LORA_HDR_CRC_ERROR):
            # The counters record it; the payload is not worth handing up.
            _lr.clear_rx_fifo(self._bus)
            return None
        length = _lr.get_rx_fifo_level(self._bus)
        if not length:
            return None
        return _lr.read_rx_fifo(self._bus, length)

    def quality(self):
        """(rssi_dbm, snr_db, signal_rssi_dbm) for the last packet."""
        _, _, _, snr, rssi, signal, _ = _lr.lora_get_packet_status(self._bus)
        return (rssi / -2.0, snr / 4.0, signal / -2.0)

    def noise(self):
        return _lr.get_rssi_inst(self._bus) / -2.0

    def busy(self):
        """True if the part hears a LoRa preamble on the channel.

        Raises RuntimeError if the CAD never finishes. That says nothing about
        the channel, so it cannot be read as permission to transmit -- and it
        must not be read as a busy channel either, which is silent and looks
        like an ordinary refusal.
        """
        listening = self._listening
        # From standby, not from Rx: issued while the part is receiving, the
        # CAD does not start and no CAD_DONE ever arrives.
        self.standby()
        _lr.get_and_clear_irq_status(self._bus)
        _lr.lora_set_cad_params(self._bus, CAD_SYMBOLS, False,
                                CAD_PNR_DELTA_STANDARD, CAD_EXIT_MODE_FALLBACK,
                                0, CAD_DET_PEAK[self._sf - 5])
        _lr.lora_set_cad(self._bus)
        self._mode = "cad"
        until = supervisor.ticks_ms() + _CAD_TIMEOUT_MS
        try:
            while mt.ticks_diff(supervisor.ticks_ms(), until) < 0:
                flags = _lr.get_and_clear_irq_status(self._bus)
                if flags & IRQ_CAD_DETECTED:
                    return True
                if flags & IRQ_CAD_DONE:
                    return False
            raise RuntimeError("CAD did not finish within %d ms"
                               % _CAD_TIMEOUT_MS)
        finally:
            # CAD leaves for the fallback mode however it ends, so without this
            # the commonest caller -- listen-before-talk that then decides not
            # to talk -- turns a busy channel into a radio that never listens
            # again.
            if listening:
                self.listen()
            else:
                self.standby()

    # ------------------------------------------------------------------- odds

    def entropy(self):
        """A word from the part's hardware random number generator."""
        return _lr.get_random_number(self._bus)

    def state(self):
        return self._mode

    def counters(self):
        return _lr.lora_get_rx_stats(self._bus)

    def reset_counters(self):
        _lr.reset_rx_stats(self._bus)

    def watch(self, handler):
        # Radio DIO8, which the W12 brings to the header as LORA_DIO1. `tune`
        # points every interrupt the part can raise at it.
        _lr.attach_irq(board.LORA_DIO1, handler, _lr.IRQ_RISING)

    def unwatch(self):
        _lr.detach_irq(board.LORA_DIO1)


def open_lr2021():
    """The LR2021 as it is wired on this board."""
    global _lr
    if _lr is None:
        import lr2021
        _lr = lr2021
    return LR2021()
