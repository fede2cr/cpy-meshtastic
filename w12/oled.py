"""The 128x64 OLED on the Meshnology W12.

The panel is an SSD1315 at 0x3C on the board I2C bus. The SSD1315 is the
SSD1306's successor and takes the same command set, so this drives it as an
SSD1306 and nothing here is specific to the newer part.

No library from the Adafruit bundle is needed: `busdisplay` talks to the panel,
`terminalio.FONT` supplies the glyphs, and a `TileGrid` of those glyphs is the
text. That matters on a board whose remaining flash is measured in tens of
kilobytes -- and it means a fresh CIRCUITPY drive can draw a screen with
nothing on it but this file.

    from meshtastic.oled import Screen
    screen = Screen()
    screen.lines("W12", "no radio yet")

`Status` puts a running node on it instead, and is the reason this file exists:

    from meshtastic import meshlib as mesh, oled
    oled.Status(mesh.node()).run()

Anything else on this drive reaches the panel through the module itself rather
than by owning a `Screen`, because building a second one releases the first:

    oled.say("stage 3 passed")   # scrolls a line on
    oled.show("a", "b", "c")     # replaces all five rows
    oled.tee()                   # and now every print lands there too
    oled.hush()                  # give it back before Ctrl-D

`tee` is what puts a throwaway test on the screen without the test knowing
there is one, and is why the modules here mostly just print.

The panel is behind VEXT (GPIO45, active low), which is undriven at boot, so
the display is dark and absent from an I2C scan until something turns the rail
on. `Screen` does that itself and keeps the pin, because releasing it cuts the
power again.

One surprise worth knowing: when code.py finishes, CircuitPython hands every
display to the REPL and whatever was on screen is replaced by the serial
terminal. A screen meant to stay up has to outlive code.py -- end in a loop.
"""

import time

import board
import busdisplay
import digitalio
import displayio
import i2cdisplaybus
import terminalio

#: The panel's address. The SSD1315 can be strapped to 0x3D instead, but the
#: W12 wires it low.
ADDRESS = 0x3C

WIDTH = 128
HEIGHT = 64

#: The I2C pull-ups are fed from VEXT too, so the bus does not exist until the
#: rail has come up. Constructing `board.I2C()` any sooner fails its pull-up
#: check with "No pull up found on SDA or SCL", which reads like a wiring fault
#: and is really a timing one.
POWER_SETTLE_S = 0.1
#: How long `_await` keeps asking the bus for the panel before giving up.
PANEL_WAIT_S = 2.0

#: Held for the lifetime of the program: this is a power switch, not a signal,
#: and a deinit here blanks the display.
_vext = None

#: True when something else already holds the pin. The radio adapter claims the
#: same rail for its front end, so on a running node the screen is a guest on a
#: switch it must not touch: turning it off would take the radio down with it.
_shared = False


def power(on=True):
    """Switches the VEXT rail that feeds the panel, and waits for it. Active low."""
    global _vext, _shared
    if _shared:
        return
    if _vext is None:
        try:
            _vext = digitalio.DigitalInOut(board.VEXT_ENABLE)
        except ValueError:
            # The radio got here first, which also means the rail is already up
            # and settled. Nothing to switch and nothing to wait for.
            _shared = True
            return
        _vext.switch_to_output(value=not on)
    else:
        _vext.value = not on
    if on:
        time.sleep(POWER_SETTLE_S)


def _await(i2c, address, timeout_s=PANEL_WAIT_S):
    """Keeps asking until the panel answers on the bus. True if it did.

    A cold boot brings VEXT up from flat and the SSD1315 does not acknowledge
    for most of a second after that; a soft reboot never dropped the rail and
    answers on the first try. Waiting for the thing itself beats guessing at a
    settle time, and costs nothing in the case that already worked.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if i2c.try_lock():
            try:
                found = address in i2c.scan()
            finally:
                i2c.unlock()
            if found:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _init_sequence(height):
    """The SSD1306 power-on sequence, as (command, length, data...) triples.

    A length byte with 0x80 set means a delay in milliseconds follows the data.
    """
    return bytes((
        0xAE, 0x00,                 # display off while it is reconfigured
        0xD5, 0x01, 0x80,           # clock: divide by 1, oscillator mid-range
        0xA8, 0x01, height - 1,     # multiplex ratio is the row count
        0xD3, 0x01, 0x00,           # no display offset
        0x40, 0x00,                 # start at line 0
        0x8D, 0x01, 0x14,           # charge pump on -- nothing lights without this
        0x20, 0x01, 0x00,           # horizontal addressing, so displayio can stream
        0xA1, 0x00,                 # flip columns
        0xC8, 0x00,                 # flip rows; with the above, origin is top left
        0xDA, 0x01, 0x12 if height > 32 else 0x02,   # COM pin wiring
        0x81, 0x01, 0xCF,           # contrast
        0xD9, 0x01, 0xF1,           # precharge
        0xDB, 0x01, 0x40,           # VCOMH deselect level
        0xA4, 0x00,                 # follow RAM, not all-on
        0xA6, 0x00,                 # not inverted
        0xAF, 0x80, 0x64,           # display on, then wait 100 ms
    ))


class Screen:
    """A grid of fixed-width text on the OLED.

    The built-in font is 6x12, so the usable screen is 21 columns by 5 rows.
    Rows are addressed from the top; writing one repaints only that row.
    """

    def __init__(self, i2c=None, address=ADDRESS, rotation=0, brightness=1.0,
                 auto_refresh=False):
        power(True)

        # A soft reboot leaves the previous display registered and the bus
        # locked; without this the second run dies on "Too many display busses".
        displayio.release_displays()

        if i2c is None:
            i2c = board.I2C()
        _await(i2c, address)
        self.bus = i2cdisplaybus.I2CDisplayBus(i2c, device_address=address)
        self.display = busdisplay.BusDisplay(
            self.bus,
            _init_sequence(HEIGHT),
            width=WIDTH,
            height=HEIGHT,
            rotation=rotation,
            # One bit per pixel, eight pixels stacked vertically per byte:
            # that is the SSD1306's page layout, and saying so lets displayio
            # hand its buffer to the panel without repacking it.
            color_depth=1,
            grayscale=True,
            pixels_in_byte_share_row=False,
            # The panel has no data/command line; the first I2C byte says
            # which one follows, so every byte of the init sequence is a
            # command and the address window is one byte per bound.
            data_as_commands=True,
            single_byte_bounds=True,
            set_column_command=0x21,
            set_row_command=0x22,
            brightness_command=0x81,
            brightness=brightness,
            # Off by default. displayio refreshes from the same background task
            # that drains the USB console, and pushing a 1 kB frame over a
            # 100 kHz bus starves it badly enough to stall every print.
            auto_refresh=auto_refresh,
        )

        self.font = terminalio.FONT
        self.glyph_width, self.glyph_height = self.font.get_bounding_box()
        self.columns = WIDTH // self.glyph_width
        self.rows = HEIGHT // self.glyph_height

        palette = displayio.Palette(2)
        palette[0] = 0x000000
        palette[1] = 0xFFFFFF
        self.grid = displayio.TileGrid(
            self.font.bitmap,
            pixel_shader=palette,
            width=self.columns,
            height=self.rows,
            tile_width=self.glyph_width,
            tile_height=self.glyph_height,
        )
        self.group = displayio.Group()
        self.group.append(self.grid)
        self.display.root_group = self.group

        self._blank = self._tile(" ")
        self.clear()

    def _tile(self, character):
        """Maps a character to its index in the font strip."""
        glyph = self.font.get_glyph(ord(character))
        if glyph is None:
            glyph = self.font.get_glyph(ord("?"))
        return glyph.tile_index

    def clear(self):
        for y in range(self.rows):
            for x in range(self.columns):
                self.grid[x, y] = self._blank
        self.refresh()

    def _write(self, row, text):
        if not 0 <= row < self.rows:
            raise IndexError("row %d is outside 0..%d" % (row, self.rows - 1))
        for x in range(self.columns):
            self.grid[x, row] = self._tile(text[x]) if x < len(text) else self._blank

    def line(self, row, text):
        """Writes one row, padded with spaces so it replaces what was there."""
        self._write(row, text)
        self.refresh()

    def lines(self, *texts):
        """Writes from the top down and blanks the rows below."""
        for row in range(self.rows):
            self._write(row, texts[row] if row < len(texts) else "")
        self.refresh()

    def refresh(self):
        """Pushes a frame. A no-op while auto_refresh is on."""
        self.display.refresh()

    def deinit(self):
        # Gives the display back but leaves VEXT alone: the radio's front end is
        # on the same rail, and dropping it here would take a running node down.
        displayio.release_displays()


# ------------------------------------------------------------- the shared panel
#
# One screen for the whole board. Every module below and every line typed at
# the prompt draws on the same object, because `Screen()` calls
# `release_displays()` and a second one would quietly blank the first.

_panel = None

#: Set once a panel has failed to build, so the next caller does not spend
#: another second finding out. A board with no display, or one whose rail the
#: radio has taken, must not make every `print` cost a timeout.
_absent = False

#: What `say` has scrolled, newest last. Kept so `Status` can hand the panel
#: back afterwards without the log having been lost.
_log = []


def panel(force=False):
    """The board's screen, built on first use. None if there is not one.

    Never raises: a missing panel is a normal state on a bare drive, and the
    exploratory modules that draw on it have to keep working without one.
    """
    global _panel, _absent
    if _panel is None and (force or not _absent):
        try:
            _panel = Screen()
        except (ValueError, RuntimeError, OSError) as error:
            _absent = True
            print("# no screen: %s" % (error,))
    return _panel


def say(text):
    """Scrolls one line onto the panel. Returns the text, so it can wrap a print.

    A repaint is about a kilobyte over a 100 kHz bus, so this is for things
    that happen at human pace -- a stage finishing, a fix landing -- and not
    for a loop that runs faster than the eye.
    """
    screen = panel()
    if screen is None:
        return text
    _log.append(str(text))
    while len(_log) > screen.rows:
        del _log[0]
    screen.lines(*_log)
    return text


def show(*rows):
    """Replaces the whole panel, and forgets the scrolled log."""
    screen = panel()
    if screen is None:
        return
    del _log[:]
    screen.lines(*rows)


def hush():
    """Drops the panel and stops mirroring. Run this before Ctrl-D."""
    global _panel, _absent
    tee(False)
    del _log[:]
    if _panel is not None:
        _panel.deinit()
        _panel = None
    _absent = False


#: The real `print`, kept while `tee` has replaced it.
_printed = None


def tee(on=True):
    """Mirrors everything printed onto the panel as well as the console.

    This is what puts a throwaway test on the screen without the test knowing
    there is one. It costs a repaint per line, so it belongs around code that
    prints at the pace someone reads -- `listen()` dumping packets through it
    would stall the console it shares the USB with.
    """
    global _printed
    import builtins
    if not on:
        if _printed is not None:
            builtins.print = _printed
            _printed = None
        return
    if _printed is not None:
        return
    if panel() is None:
        return
    _printed = builtins.print

    def mirrored(*args, **kwargs):
        _printed(*args, **kwargs)
        try:
            # A blank line is a paragraph break for a console with a hundred
            # rows. Five of them cannot spare one, and a fragment printed with
            # end="" is not a line yet -- the call that finishes it will be.
            if kwargs.get("end", "\n") != "\n":
                return
            text = kwargs.get("sep", " ").join(str(a) for a in args).strip()
            if not text:
                return
            # The console marks its own lines with a leading '#'; the panel
            # has only five of them and no room to spend one on punctuation.
            say(text[2:] if text.startswith("# ") else text)
        except Exception as error:
            # A panel that has gone away must not take the console with it:
            # everything below this line is somebody's test reporting a
            # result, and losing that to a display is the wrong trade. Said
            # out loud, or a screen that silently stops looks like one that
            # was never wired up.
            tee(False)
            print("# screen dropped, printing only to USB: %s" % (error,))

    builtins.print = mirrored


class Status:
    """A mesh node on the panel: who we are, what we are tuned to, what we heard.

    Takes the node rather than importing one. `oled` stays a file that needs
    nothing else on the drive, and the mesh library stays unaware of a screen
    that only one of the two boards has.

        from meshtastic import meshlib as mesh, oled
        oled.Status(mesh.node()).run()

    Five rows, and every one of them says what it does not know rather than
    leaving the row off: a screen whose layout moves cannot be read at a glance.
    """

    def __init__(self, node, screen=None, session=None):
        self.node = node
        # The shared panel, not a new one: building a second `Screen` releases
        # the first, and the prompt would be drawing on a display nothing owns.
        self.screen = panel(force=True) if screen is None else screen
        #: A phone link to show a page for, if one is running.
        self.session = session
        #: (number, rssi, snr) of the last packet, for the bottom row.
        self.last = None
        self._previous = node.on_receive
        node.on_receive = self._heard
        #: Which of `PAGES` is showing. The panel holds five rows and the node
        #: has more than five things worth saying, so they take turns.
        self.page = 0
        self._turn_at = time.monotonic()
        self._paint_at = time.monotonic()
        self.update()

    def _heard(self, packet, frame, payload):
        """Records, and draws nothing.

        This runs from the interrupt-scheduled poll, where pushing a kilobyte
        over a 100 kHz bus would stall the console that is reporting the same
        packet. `run` repaints a moment later, out here.
        """
        if self._previous is not None:
            self._previous(packet, frame, payload)
        self.last = (packet.from_, packet.rssi, packet.snr)

    def detach(self):
        """Puts back whatever hook was there before. The screen keeps its last frame."""
        self.node.on_receive = self._previous

    def _names(self, num):
        try:
            from meshtastic import meshlib as nodeinfo
            return nodeinfo.names(num)
        except (ImportError, AttributeError):
            # Driving the panel by hand, with no mesh library on the drive.
            return "node %04x" % (num & 0xFFFF), "%04x" % (num & 0xFFFF)

    def _last_row(self):
        if self.last is None:
            return "last --"
        num, rssi, snr = self.last
        who = self.node.nodes.name(num)
        # A packet handed over by the phone or replayed from the cache has no
        # measurement behind it, and a zero there would be a claim.
        return "last %s %s %s" % (
            who,
            "--" if rssi is None else "%d" % rssi,
            "--" if snr is None else "%.1f" % snr)

    def rows(self):
        """The five lines, as text. Separate from drawing so it can be printed."""
        page = self.pages()[self.page]
        if page == "gps":
            return self.gps_rows()
        if page == "phone":
            return self.session.rows()
        return self.mesh_rows()

    def pages(self):
        """Which pages this node has. The GPS one only where there is a receiver."""
        names = ("mesh",)
        if getattr(self.node, "gps", None) is not None:
            names += ("gps",)
        # A transport with no layout of its own gets no page rather than a
        # guess at one: only `meshtcp` has an address worth five rows.
        if getattr(self.session, "rows", None) is not None:
            names += ("phone",)
        return names

    def turn(self):
        """Moves to the next page."""
        self.page = (self.page + 1) % len(self.pages())
        self.update()

    def gps_rows(self):
        """What the satellites are saying. The fix draws itself: the panel
        layout for a reading belongs with the reading."""
        return self.node.gps.fix.rows()

    def mesh_rows(self):
        this = self.node
        num = (this.tx.node_num if this.tx is not None
               else this.config.node_num or 0)
        _long, short_name = self._names(num)
        cfg = this.settings
        return (
            # The short name, not the long one: 21 columns is not enough for a
            # sentence, and this is the name the mesh's own listings use.
            "%-11s !%08x" % (short_name[:11], num),
            "%s %s %.1f" % (cfg.region_name, cfg.preset_name,
                            cfg.frequency_hz / 1e6),
            "%d nodes %d msg %d new" % (len(this.nodes), len(this.inbox),
                                        this.inbox.unread_count()),
            # The power is the radio's, and it only has one once it has been
            # configured, so an unstarted node says so instead of raising.
            "rx %d/%d drop %d %s" % (
                this.decoded, this.heard, this.dropped,
                "%+ddBm" % this.radio.power_dbm
                if getattr(this.radio, "power_dbm", None) is not None else "off"),
            self._last_row(),
        )

    def update(self):
        """Repaints. One frame over I2C, so not worth calling faster than a second."""
        if self.screen is None:
            return
        self.screen.lines(*self.rows())

    def tick(self, interval_s=1.0, page_s=5.0):
        """Turns the page and repaints, but only when either is due.

        For a caller that already owns a loop -- `mesh.serve()` turns its own
        fifty times a second, and a kilobyte over the panel's bus at that rate
        would be most of what the board did.
        """
        now = time.monotonic()
        if len(self.pages()) > 1 and now >= self._turn_at:
            self._turn_at = now + page_s
            self.page = (self.page + 1) % len(self.pages())
        if now < self._paint_at:
            return
        self._paint_at = now + interval_s
        self.update()

    def run(self, interval_s=1.0, page_s=5.0):
        """Polls the radio and repaints until Ctrl-C. This is what outlives code.py.

        The loop is here rather than in code.py because the screen is only up
        for as long as something is running: when the last line of code.py
        returns, CircuitPython hands the display to the REPL and the panel
        becomes the serial terminal.
        """
        try:
            while True:
                self.node.poll()
                self.node.service()
                self.tick(interval_s, page_s)
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("# screen stopped, node still up")
