"""What the board can say about its own power.

Three signals: one divided analogue rail carrying the pack voltage, and the two
open-drain status pins of the BQ25185 charger. Between them they answer the only
power questions a node can ask about itself -- how much is left, and whether
anything is going back in -- which are two of the five fields the phone app
draws as a node's vital signs and which have until now been left out.

Percent does not come from the voltage directly. See `mt.battery_percent`: a
lithium cell's voltage is a bad linear proxy for its charge, and matching the
firmware's curve is what makes this node and a stock one agree about the same
pack.

The status pins are read as the datasheet's Table 6-2 defines them, not as the
red LED suggests. Both are active low, and STAT1 low means a fault, which is
worth distinguishing from the ordinary states rather than folding into "not
charging".
"""

import analogio
import board
import digitalio

import meshtastic as _mt

#: Both status pins released: charge complete, charger asleep, or charging
#: disabled. The resting state on USB with a full pack, and also on battery
#: alone, so this does not distinguish those.
IDLE = 0
#: STAT2 low. Normal charging in progress, including automatic recharge. This is
#: what lights the red LED, which shares the net.
CHARGING = 1
#: STAT1 low, STAT2 high. Recoverable: input overvoltage, thermistor hot or
#: cold, thermal shutdown, system short. Clears itself when the cause goes away.
FAULT = 2
#: Both low. Latched: ILIM/ISET pin short, battery overcurrent, or the safety
#: timer expiring. Needs the input power cycled.
FAULT_LATCHED = 3

STATE_NAMES = ("idle", "charging", "fault", "fault latched")

#: The divider on the schematic, R25 806k over R26 1.5M, as a fraction to
#: multiply the pin voltage by. Whole kilohms because they are 1% parts and the
#: ADC is nowhere near that good.
_DIVIDER_TOP = 806
_DIVIDER_BOTTOM = 1500

#: The nRF52840's ADC reference in millivolts, with the 1/6 gain and the internal
#: 0.6 V band gap that CircuitPython configures for a 0-3.3 V full scale.
_FULL_SCALE_MV = 3300

#: Readings averaged per call. The rail moves several tens of millivolts when the
#: PA keys up, and one sample caught there reads as a flat pack.
_SAMPLES = 4


class Power:
    """The charger and the pack behind it."""

    def __init__(self, monitor, stat1=None, stat2=None):
        self._adc = analogio.AnalogIn(monitor)
        self._stat1 = self._sense(stat1, pull=True)
        # STAT2 has R21, a 100k pull-up to 3V3, so it needs no internal one. The
        # red LED sits on the same net and cannot pull it up on its own: through
        # the LED the pin would only ever reach a diode drop below the rail,
        # which is not a logic high.
        self._stat2 = self._sense(stat2, pull=False)

    @staticmethod
    def _sense(pin, pull):
        if pin is None:
            return None
        line = digitalio.DigitalInOut(pin)
        line.switch_to_input(digitalio.Pull.UP if pull else None)
        return line

    def deinit(self):
        self._adc.deinit()
        for line in (self._stat1, self._stat2):
            if line is not None:
                line.deinit()

    # ------------------------------------------------------------- the pack

    def millivolts(self):
        """Pack voltage, averaged over a few samples."""
        total = 0
        for _ in range(_SAMPLES):
            total += self._adc.value
        pin_mv = (total // _SAMPLES) * _FULL_SCALE_MV // 65535
        return pin_mv * (_DIVIDER_TOP + _DIVIDER_BOTTOM) // _DIVIDER_BOTTOM

    def present(self):
        """False when the board is running on the charger's power path alone."""
        return self.millivolts() >= _mt.NO_BATTERY_MV

    def report(self):
        """(percent, millivolts) from a single measurement.

        Percent is None with no pack fitted, and held at 99 while charging: the
        charger does not release STAT2 until the current has tapered, and a node
        claiming 100% for the last half hour of a charge is the one thing this
        reading can get visibly wrong.
        """
        millivolts = self.millivolts()
        if millivolts < _mt.NO_BATTERY_MV:
            return None, millivolts
        level = _mt.battery_percent(millivolts)
        if level > 99 and self.state() == CHARGING:
            level = 99
        return level, millivolts

    def percent(self):
        """0-100, or None with no pack fitted."""
        return self.report()[0]

    # ---------------------------------------------------------- the charger

    def state(self):
        """One of IDLE, CHARGING, FAULT, FAULT_LATCHED, or None if unwired."""
        if self._stat1 is None or self._stat2 is None:
            return None
        low1 = not self._stat1.value
        low2 = not self._stat2.value
        if low1:
            return FAULT_LATCHED if low2 else FAULT
        return CHARGING if low2 else IDLE

    def charging(self):
        return self.state() == CHARGING

    def describe(self):
        level, millivolts = self.report()
        state = self.state()
        name = "unwired" if state is None else STATE_NAMES[state]
        if level is None:
            return "no battery, %s" % name
        return "%d%% at %.2f V, %s" % (level, millivolts / 1000.0, name)


def open_power():
    """This board's power monitor, or None if it has no way to measure itself.

    The charger pins are optional separately from the divider: a board that
    brings out one and not the other still gets the half it has.
    """
    monitor = getattr(board, "VOLTAGE_MONITOR", None)
    if monitor is None:
        return None
    return Power(monitor,
                 getattr(board, "CHARGE_STAT1", None),
                 getattr(board, "CHARGE_STATUS", None))
