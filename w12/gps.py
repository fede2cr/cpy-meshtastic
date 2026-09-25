"""The GNSS on this W12: the driver the node reads from, and the bring-up tools
that worked out how to talk to it.

`MESH_GPS` in settings.toml names this module and `meshnode.open_gps` imports
it and asks for a `Reader`, so this is boot code, not a test. It was a test
first, which is why the two live together: `probe` and `watch` are how the
settings the driver hard-codes were arrived at, and the header they interrogate
is the part of this board most likely to differ on the next one.

The L76K sits on a header rather than being soldered down, and the vendor's own
variant.h calls that header unpopulated, so the first thing this ever answered
was whether a given board has one at all. Nothing here assumes it does.

Three things the header comment cannot settle are settled by trying them: which
way round the UART is, which way GPS_ENABLE is active, and what baud the module
came set to. `probe` walks all of them and prints what each produced, which is
why it takes a minute.

    from meshtastic import gps
    gps.probe()       # find a combination that yields NMEA
    gps.watch(60)     # then sit and report what it can see
    gps.close()       # then Ctrl-D, and check the board actually reboots

None of these pins belong to the radio, so this needs no `mesh.release()` and
can run beside a started node.
"""

import time

import board
import busio
import digitalio
import rtc

import meshtastic as _mt

#: The L76K leaves the factory at 9600. The others are where it would be if
#: someone had reconfigured it, and cost two seconds each to rule out.
BAUDS = (9600, 115200, 38400)

#: variant.h says CTRL is active low. It describes a footprint with nothing on
#: it, so it is the least load-bearing fact here and `probe` tries both.
ENABLE_ACTIVE_LOW = True

#: A cold module needs a moment after power before it says anything.
SETTLE_S = 0.6

#: Pins stay claimed between calls: dropping GPS_ENABLE would power the module
#: down and throw away the almanac it is part-way through downloading.
_pins = {}
_uart = None
_running = False

#: (enable active low, swapped, baud), measured on the board 2026-09-16: the
#: header is populated despite variant.h, and swapping the pair produced not one
#: byte on any baud. `probe` overwrites this if it is ever wrong again.
WIRING = (True, False, 9600)

#: What `probe` found, or the measured default so `watch` works without it.
found = WIRING


def _oled_show(rows):
    """Draws on the panel if this drive has one. Never the reason a probe fails."""
    try:
        from meshtastic import oled
    except ImportError:
        return
    try:
        oled.show(*rows)
    except Exception as error:
        print("# screen: %s" % (error,))


def _claim(name, pin, value):
    """Drives one pin. None if something else already owns it."""
    held = _pins.get(name)
    if held is None:
        try:
            held = digitalio.DigitalInOut(pin)
        except ValueError:
            return None
        held.switch_to_output(value=value)
        _pins[name] = held
    else:
        held.value = value
    return held


def power(on=True, active_low=ENABLE_ACTIVE_LOW, reset=None):
    """Powers the module, wakes it and lets go of reset.

    `reset` pulses the reset line, and by default only the first time: a running
    receiver loses its almanac to one and spends the next seconds re-acquiring.
    """
    global _running
    # The GNSS rail may sit behind VEXT like the panel and the radio front end
    # do; if the radio holds that pin it is already on and this is a no-op.
    _claim("vext", board.VEXT_ENABLE, False)
    _claim("enable", board.GPS_ENABLE, (not on) if active_low else on)
    _claim("wake", board.GPS_WAKE, True)
    pin = _claim("reset", board.GPS_RESET, True)
    if reset is None:
        reset = not _running
    if on and reset and pin is not None:
        pin.value = False
        time.sleep(0.05)
        pin.value = True
        time.sleep(SETTLE_S)
    _running = on


def open_uart(baud=9600, swap=False, timeout=0.5):
    """Opens the GNSS UART. `swap` exchanges tx and rx."""
    global _uart
    close_uart()
    tx, rx = ((board.GPS_RX, board.GPS_TX) if swap
              else (board.GPS_TX, board.GPS_RX))
    _uart = busio.UART(tx, rx, baudrate=baud, timeout=timeout,
                       receiver_buffer_size=1024)
    return _uart


def close_uart():
    global _uart
    if _uart is not None:
        _uart.deinit()
        _uart = None


def close():
    """Gives back every pin. Run this before Ctrl-D."""
    global found, _running
    close_uart()
    for held in _pins.values():
        held.deinit()
    _pins.clear()
    found = WIRING
    _running = False


def _sample(uart, seconds):
    """Reads for a while. Returns (bytes, dollars, first printable line)."""
    deadline = time.monotonic() + seconds
    total = 0
    dollars = 0
    line = ""
    while time.monotonic() < deadline:
        chunk = uart.read(256)
        if not chunk:
            continue
        total += len(chunk)
        dollars += chunk.count(b"$")
        if not line:
            try:
                text = chunk.decode("ascii")
            except (UnicodeError, ValueError):
                continue
            at = text.find("$")
            if at >= 0:
                end = text.find("\n", at)
                line = text[at:end if end > 0 else len(text)].strip()
    return total, dollars, line


def probe(seconds=2.0, exhaustive=False):
    """Tries wirings, polarities and bauds until one yields NMEA.

    Rows with bytes but no `$` are a module at the wrong baud. Nothing anywhere
    means the pair is wrong, the enable is wrong, or the header is empty -- and
    the first two are ruled out by the rest of the table. `exhaustive` finishes
    the table anyway, which takes about a minute.
    """
    global found
    best = None
    print("# enable  wiring  baud    bytes  $  sample")
    for active_low in (True, False):
        for swap in (False, True):
            for baud in BAUDS:
                power(True, active_low=active_low, reset=True)
                _oled_show(("GPS probe", "",
                            "%s enable" % ("low" if active_low else "high"),
                            "%s wiring" % ("swapped" if swap else "direct"),
                            "%d baud" % baud))
                total, dollars, line = _sample(open_uart(baud, swap), seconds)
                print("# %-7s %-7s %-6d  %5d %3d  %s"
                      % ("low" if active_low else "high",
                         "swapped" if swap else "direct", baud,
                         total, dollars, line[:32]))
                if dollars and (best is None or dollars > best[0]):
                    best = (dollars, active_low, swap, baud)
                    if not exhaustive:
                        break
            if best is not None and not exhaustive:
                break
        if best is not None and not exhaustive:
            break
    close_uart()
    if best is None:
        print("# nothing on any combination.")
        print("# Either the header is empty -- which is what the vendor's own")
        print("# variant.h says to expect -- or the module needs a rail this")
        print("# does not switch. Check the header is populated before")
        print("# reading anything else into this.")
        _oled_show(("GPS probe", "", "nothing on any", "combination",
                    "header empty?"))
        return None
    _, active_low, swap, baud = best
    found = (active_low, swap, baud)
    print("# NMEA on %s enable, %s wiring, %d baud"
          % ("low" if active_low else "high",
             "swapped" if swap else "direct", baud))
    _oled_show(("GPS probe", "", "NMEA found",
                "%s enable, %s" % ("low" if active_low else "high",
                                   "swapped" if swap else "direct"),
                "%d baud" % baud))
    return found


def _open_found():
    active_low, swap, baud = found
    # A second call must not reopen: powering pulses reset, and the receiver
    # would start from nothing every time it was asked a question.
    if _uart is not None and _running:
        return _uart
    power(True, active_low=active_low)
    return open_uart(baud, swap)


#: GGA fix quality. 6 is a position the receiver worked out without satellites
#: and does not stand behind; RMC calls the same moment invalid.
QUALITY = {0: "invalid", 1: "gps", 2: "dgps", 3: "pps", 4: "rtk",
           5: "rtk-float", 6: "estimated", 7: "manual", 8: "simulated"}


class Fix:
    """The last of everything the module has said.

    The sentences are parsed in Rust and the result lives in `state`, a plain
    buffer this owns. Every property below reads back out of it, so nothing is
    stored twice and a reading cannot go stale against the parser.
    """

    def __init__(self):
        self.state = bytearray(_mt.NMEA_STATE)
        _mt.nmea_reset(self.state)
        self._t = _mt.nmea_fix(self.state)

    def refresh(self):
        """Re-reads the parsed state. Called for you after anything lands."""
        self._t = _mt.nmea_fix(self.state)

    def sentence(self, line):
        """Takes one sentence. True if it was one this acts on."""
        if isinstance(line, str):
            line = line.encode("ascii")
        saw = _mt.nmea_sentence(self.state, line)
        self.refresh()
        return saw > 0

    @property
    def latitude(self):
        return None if self._t[0] is None else self._t[0] / 1e7

    @property
    def longitude(self):
        return None if self._t[1] is None else self._t[1] / 1e7

    @property
    def altitude_m(self):
        return None if self._t[2] is None else self._t[2] / 1000.0

    @property
    def when(self):
        return self._t[3]

    @property
    def satellites(self):
        return self._t[4]

    @property
    def in_view(self):
        return self._t[5]

    @property
    def quality(self):
        return self._t[6]

    @property
    def status(self):
        """RMC's own verdict: A valid, V not. The receiver disagreeing with its
        own GGA is worth seeing rather than averaging."""
        return None if self._t[7] is None else chr(self._t[7])

    @property
    def mode(self):
        return None if self._t[8] is None else chr(self._t[8])

    @property
    def nav(self):
        return None if self._t[9] is None else chr(self._t[9])

    @property
    def hdop(self):
        """Horizontal dilution of precision: how well spread the satellites are.
        Under 2 is good, over 5 means they are bunched and the fix is soft."""
        return None if self._t[10] is None else self._t[10] / 100.0

    @property
    def good(self):
        return self._t[11]

    @property
    def bad(self):
        return self._t[12]

    @property
    def valid(self):
        """Whether the receiver stands behind the position: RMC has to agree."""
        return self._t[13]

    @property
    def timed(self):
        """Whether the clock is real. True long before `valid` is, and indoors."""
        return self._t[3] is not None

    @property
    def talkers(self):
        """Satellites in view per constellation, by NMEA talker id."""
        seen = {}
        for i in range(_mt.NMEA_TALKERS):
            row = _mt.nmea_talker(self.state, i)
            if row is None:
                break
            seen[row[0].decode("ascii")] = row[1]
        return seen

    @property
    def utc(self):
        """The GPS clock as readable UTC.

        Formatted from the epoch through the calendar in the natmod, not
        `time.localtime`: that means the port's idea of local, and the whole
        point of this number is that it predates the board's own clock.
        """
        when = self._t[3]
        if when is None:
            return None
        c = _mt.nmea_civil(when)
        return "%04d-%02d-%02d %02d:%02d:%02dZ" % c

    def __str__(self):
        where = ("no position" if self.latitude is None else
                 "%.5f %.5f %sm"
                 % (self.latitude, self.longitude,
                    "--" if self.altitude_m is None
                    else "%.0f" % self.altitude_m))
        return ("%s, %s used, %s in view, %s, RMC %s%s, %s"
                % (where, self.satellites, self.in_view,
                   QUALITY.get(self.quality, self.quality),
                   self.status or "--",
                   "" if self.mode is None else "/" + self.mode,
                   self.utc or "no time"))

    def rows(self):
        """The same reading as five lines of 21 columns, for the OLED.

        Here rather than in `oled` so that the panel layout for a fix lives
        with the fix, and the screen module needs to know nothing about GNSS.
        """
        return (
            "GPS %s" % ("fix" if self.valid else
                        "time only" if self.timed else "searching"),
            "%s used  %s seen" % (
                "--" if self.satellites is None else self.satellites,
                "--" if self.in_view is None else self.in_view),
            ("no position" if self.latitude is None else
             "%.4f %.4f" % (self.latitude, self.longitude)),
            # HDOP is the one number that says whether a position is worth
            # sending: under 2 the satellites are spread, over 5 they are not.
            "%sm  hdop %s" % (
                "--" if self.altitude_m is None else "%.0f" % self.altitude_m,
                "--" if self.hdop is None else "%.1f" % self.hdop),
            self.utc or "no time yet",
        )

    #: The REPL prints the repr, and `<Fix object at 0x...>` answers nothing.
    __repr__ = __str__


def uart():
    """The GNSS UART, opened on the measured wiring if it is not open yet."""
    return _open_found()


class Reader:
    """Feeds a `Fix` from the UART without ever waiting.

    A server cannot use `watch`: a read that blocks is a page that hangs. This
    takes only what has already arrived, so it can be called from a loop that
    has other work. Call it more than once a second -- the UART buffer holds
    about a second of 9600-baud NMEA, and what overflows is lost.
    """

    def __init__(self, keep=0):
        self.fix = Fix()
        #: How many raw sentences to remember, for showing the stream itself.
        self.keep = keep
        self.recent = []
        #: Where the parser copies the sentences it accepted, when anyone is
        #: asking for them. None costs nothing on a node that only wants time.
        self._echo = bytearray(keep * 84) if keep else None

    @property
    def good(self):
        return self.fix.good

    @property
    def bad(self):
        return self.fix.bad

    def poll(self):
        """Reads whatever is waiting. Returns the number of good sentences."""
        port = uart()
        landed = 0
        used = 0
        while True:
            # Asking only for what has arrived: `read` would otherwise sit out
            # the UART's own timeout waiting for bytes that are not coming.
            waiting = port.in_waiting
            if not waiting:
                break
            chunk = port.read(waiting if waiting < 256 else 256)
            if not chunk:
                break
            got, used = _mt.nmea_feed(self.fix.state, chunk, self._echo, used)
            landed += got
            if self._echo is not None and used > len(self._echo) - 84:
                # No room for another sentence, so take what is there and start
                # the buffer over rather than silently dropping the rest.
                self._remember(used)
                used = 0
        if self._echo is not None and used:
            self._remember(used)
        if landed:
            self.fix.refresh()
        return landed

    def _remember(self, used):
        for line in bytes(self._echo[:used]).split(b"\n"):
            if line:
                self.recent.append(line.decode("ascii"))
        while len(self.recent) > self.keep:
            del self.recent[0]

    def close(self):
        """Powers the receiver down and gives every pin back."""
        # The module's `close`, not this method: a class body is not searched
        # from inside one of its methods.
        close()


def watch(seconds=60, every=5, stop_on_fix=False, stop_on_time=False):
    """Reads NMEA and reports for the whole time asked for.

    It does not stop at the first position by default: a cold receiver keeps
    finding satellites for minutes after it has something to show, and stopping
    early hides that. `stop_on_fix` ends at the first position the receiver
    stands behind -- an estimated one does not count -- and `stop_on_time` at
    the first real clock, which arrives far sooner.

    A cold module needs sky. Indoors it will report satellites in view and never
    use one.
    """
    reader = Reader()
    fix = reader.fix
    deadline = time.monotonic() + seconds
    report_at = time.monotonic()
    while time.monotonic() < deadline:
        reader.poll()
        if time.monotonic() >= report_at:
            report_at = time.monotonic() + every
            print("# %s" % fix)
            # The panel gets the laid-out reading rather than the printed
            # sentence: a fix does not fit on one 21-column row, and watching
            # satellites arrive is exactly what a screen is better at.
            _oled_show(fix.rows())
        if stop_on_fix and fix.valid:
            break
        if stop_on_time and fix.timed:
            break
        time.sleep(0.05)
    bad = reader.bad
    print("# %s, %d unreadable" % (fix, bad))
    _oled_show(fix.rows())
    return fix


def clock(seconds=90):
    """Sets the board's clock from GPS. Wants no position, so it works indoors.

    The mesh and the phone are the other two sources, and both are hearsay --
    this one is the satellites themselves.
    """
    fix = watch(seconds, stop_on_time=True)
    if not fix.timed:
        print("# no GPS time yet; the receiver has decoded no satellite")
        return None
    rtc.RTC().datetime = time.localtime(fix.when)
    print("# clock set from GPS: %s" % fix.utc)
    _oled_show(("clock set from GPS", "", fix.utc, "",
                "%s satellites seen" % (fix.in_view
                                        if fix.in_view is not None else "--")))
    return fix.when


def dump(seconds=5, only=None):
    """Prints the sentences themselves. `only` filters by type, e.g. "RMC".

    For when the parsed summary and the receiver disagree: this is what it
    actually said.
    """
    uart = _open_found()
    pending = ""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        chunk = uart.read(256)
        if not chunk:
            time.sleep(0.05)
            continue
        try:
            pending += chunk.decode("ascii")
        except (UnicodeError, ValueError):
            pending = ""
            continue
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.strip()
            if line.startswith("$") and (only is None or only in line[:7]):
                print(line)


def pps(seconds=10):
    """Counts edges on the PPS pin. The L76K pulses it only once it has a fix."""
    close_uart()
    held = _pins.pop("pps", None)
    if held is not None:
        held.deinit()
    pin = digitalio.DigitalInOut(board.GPS_PPS)
    pin.switch_to_input(digitalio.Pull.DOWN)
    try:
        edges = 0
        last = pin.value
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            now = pin.value
            if now and not last:
                edges += 1
            last = now
        print("# %d pulses in %ds" % (edges, seconds))
        return edges
    finally:
        pin.deinit()
