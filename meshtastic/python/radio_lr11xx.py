"""The Semtech LR1110/LR1120/LR1121 family, as the node wants to see it.

Everything chip-specific about running Meshtastic on an LR11xx is in this file:
the register code tables, the RF switch wiring, the TCXO, and which band a
frequency falls in. The node above it never learns any of that, which is the
point -- an SX1262 board writes `radio_sx126x.py` and changes nothing else.

The `lr1121` driver this wraps is a general-purpose library and knows nothing
about Meshtastic. That direction of ignorance is deliberate and worth keeping:
the driver is useful to people who have never heard of a mesh.
"""

import board

from meshtastic import meshlib as mt

# The driver is imported by `open_lr11xx`, not here. This module is merged into
# meshtastic.mpy, so importing lr1121.mpy from a module body nests a 20 kB load
# inside a 74 kB one and holds both sets of loader allocations at once, which is
# where the heap runs out. Sequential fits where nested does not.
_lr11xx = None

#: Which DIO lines the antenna switch is on, and what it should be for each
#: mode. This is board wiring rather than chip design -- muzi Base Duo puts the
#: switch on DIO5 and DIO6 and leaves DIO7 and DIO8 unconnected. Wrong here,
#: receiving looks exactly like an empty band and transmitting goes into a
#: disconnected port.
DIO5, DIO6 = 0x01, 0x02
RF_SWITCH = bytes((
    DIO5 | DIO6,  # which DIOs the switch owns
    0x00,         # standby
    DIO5,         # rx
    DIO6,         # tx (low power PA)
    DIO6,         # tx high power
    0x00,         # tx high frequency
    0x00,         # gnss
    0x00,         # wifi
))

#: Bandwidth in Hz to the register code, filled in once the driver is loaded.
#: 1625 kHz, which the 2.4 GHz turbo presets want, has no constant in the driver
#: yet and is left out deliberately so the lookup fails with a clear message
#: instead of a guessed value.
_BW_CODES = {}


def _band(frequency_hz):
    for band, (low, high) in _lr11xx.BAND_LIMITS_HZ.items():
        if low <= frequency_hz <= high:
            return band
    raise ValueError("%d Hz is outside every LR11xx band" % frequency_hz)


class LR11xx:
    """One LR11xx, speaking the vocabulary in `meshradio.Radio`."""

    def __init__(self, radio):
        self.chip = radio
        self.description = "LR11xx"
        self.hw_model = mt.HW_MUZI_BASE

    def open(self):
        self.chip.begin(tcxo_voltage=_lr11xx.TCXO_3_0V, tcxo_delay_us=5000,
                        rf_switch=RF_SWITCH)
        hw, device, major, minor = self.chip.version()
        if device != _lr11xx.DEVICE_LR1121:
            raise RuntimeError("unexpected device byte 0x%02X" % device)
        self.description = "LR1121 fw %d.%d, hw 0x%02X" % (major, minor, hw)
        # After a packet the part returns here rather than to sleep, so the
        # next receive does not pay for a cold start.
        self.chip.set_fallback_mode(_lr11xx.FALLBACK_STBY_RC)
        self.chip.reset_stats()
        return self

    def close(self):
        self.chip.deinit()

    # ----------------------------------------------------------------- tuning

    def power_ceiling(self, frequency_hz):
        return _lr11xx.MAX_POWER_DBM[_band(frequency_hz)]

    def tune(self, *, frequency_hz, sf, bw_hz, cr, preamble, sync_word,
             power_dbm, rx_boosted=True):
        try:
            bandwidth = _BW_CODES[bw_hz]
        except KeyError:
            raise ValueError("no LR11xx bandwidth code for %d Hz" % bw_hz)
        if not 5 <= cr <= 8:
            raise ValueError("coding rate must be 5..8, got %r" % (cr,))
        self.chip.configure_lora(
            frequency=frequency_hz,
            band=_band(frequency_hz),
            sf=sf,
            bandwidth=bandwidth,
            # The short-interleaver encoding. The part also has long-interleaver
            # variants at 0x05..0x07, which are a different waveform and not
            # interoperable with an SX126x node.
            coding_rate=cr - 4,
            preamble_length=preamble,
            sync_word=sync_word,
            crc=True,
            invert_iq=False,
            power_dbm=power_dbm,
        )
        # Only settable once configure_lora has put the part in LoRa mode, and
        # the chip has no usable default: CAD without it reports nonsense.
        self.chip.set_cad_params()
        self.chip.set_rx_boosted(rx_boosted)

    @property
    def power_dbm(self):
        return self.chip.output_power

    # ---------------------------------------------------------------- traffic

    def listen(self):
        self.chip.start_receive(_lr11xx.RX_CONTINUOUS)

    @property
    def listening(self):
        return self.chip.receiving == _lr11xx.RX_CONTINUOUS

    def standby(self):
        self.chip.standby(_lr11xx.STANDBY_RC)

    def send(self, data):
        return bool(self.chip.transmit(data))

    def collect(self):
        return self.chip.poll_receive()

    def quality(self):
        return self.chip.packet_status()

    def noise(self):
        return self.chip.rssi()

    def busy(self):
        return self.chip.channel_activity_detected()

    # ------------------------------------------------------------------- odds

    def entropy(self):
        return self.chip.random()

    def state(self):
        return self.chip.chip_mode_name()

    def counters(self):
        return self.chip.stats()

    def reset_counters(self):
        self.chip.reset_stats()

    def watch(self, handler):
        # LR1121 DIO9, which the Base Duo brings to the header as LORA_IRQ.
        self.chip.watch(board.LORA_IRQ, handler)

    def unwatch(self):
        self.chip.unwatch(board.LORA_IRQ)


def open_lr11xx():
    """The LR11xx as it is wired on this board."""
    global _lr11xx
    if _lr11xx is None:
        import lr1121
        _lr11xx = lr1121
        _BW_CODES.update({
            62_500: lr1121.LORA_BW_62_5,
            125_000: lr1121.LORA_BW_125_0,
            203_125: lr1121.LORA_BW_203_125,
            250_000: lr1121.LORA_BW_250_0,
            406_250: lr1121.LORA_BW_406_25,
            500_000: lr1121.LORA_BW_500_0,
            812_500: lr1121.LORA_BW_812_50,
        })
    return LR11xx(_lr11xx.LR1121(board.LORA_SPI(), board.LORA_CS,
                                 board.LORA_BUSY, board.LORA_RESET))
