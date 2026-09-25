"""A Meshtastic node: bring the radio up, say something, then listen.

The application layer, as a module. Everything below this is about the wire
format or the chip and has no opinion about what a session looks like; this is
where those become a node that provisions itself, announces once and then keeps
a running commentary on what it hears. `code.py` is four lines because all of it
is here.

Printing is deliberately part of the module rather than left to the caller. The
console transcript is the product for this project -- it is the only evidence a
board without a display can offer -- so its format is something to keep stable,
not something for each script to reinvent.

Transmitting is what makes a node a nuisance rather than merely wrong, so two
guards are on by default. The duty-cycle budget refuses a send once the last
hour is fuller than the region allows. Listen-before-talk checks for channel
activity first, which is what stops this stepping on a packet already in the
air.

Staying in receive afterwards is the point rather than a convenience. The useful
confirmation is not that the radio reported success -- it always does -- but
that a neighbouring node rebroadcast the message back. That echo is the proof
the frame was well formed, because a relay only forwards what it could parse.
Such a line is marked ECHO.
"""

import gc
import sys
import time

import board
import microcontroller
import rtc
import supervisor

from meshtastic import meshlib as keystore
from meshtastic import meshlib as inbox
from meshtastic import meshlib as mesh_budget
from meshtastic import meshlib as mesh_config
from meshtastic import meshlib as mesh_tx
from meshtastic import meshlib as meshradio
from meshtastic import meshlib as meshpower
from meshtastic import meshlib as mt
from meshtastic import meshlib as nodedb
from meshtastic import meshlib as nodeinfo

#: Whether anything survives a reset. See `_cache`. Named at length because the
#: library merges into one globals dict beside `keystore`'s `CACHE_BYTES`.
CACHE_ENABLED = mesh_budget.budget_setting("MESH_CACHE", True)


def _cache():
    """The cache module, loaded for as long as it takes to use it, or None.

    It ships as its own meshcache.mpy rather than in the library, because it
    only runs at boot and once every six hours, and two kilobytes resident for
    that is two kilobytes the phone session does not have.

    None is a node that forgets every peer and every message across a reboot,
    which is a real loss and is still the right trade on a board with no room
    for the file. It is asked for two ways, and neither is an error: `MESH_CACHE
    = false` in settings.toml says so on purpose, and a drive with no
    meshcache.mpy on it says the same thing by leaving it off.
    """
    if not CACHE_ENABLED:
        return None
    try:
        from meshtastic import meshcache
    except ImportError:
        return None
    return meshcache


def _drop_cache():
    """Lets go of it again. Harmless if it was never loaded."""
    # Dotted: it is registered under its name inside the package, and popping
    # the bare name frees nothing.
    sys.modules.pop("meshtastic.meshcache", None)
    gc.collect()


#: Telemetry is the only payload allowed to seed the clock. See `_seed_clock`.
_PORT_TELEMETRY = 67

#: Where a clock came from, ranked, so a worse source cannot overwrite a better
#: one. The board's own satellites beat a client's clock, which beats a stamp
#: off the air -- that last one is a stranger's guess, delayed by its own hops.
CLOCK_NONE = 0
CLOCK_MESH = 1
CLOCK_PHONE = 2
CLOCK_GPS = 3
CLOCK_NAMES = {CLOCK_MESH: "the mesh", CLOCK_PHONE: "the phone",
               CLOCK_GPS: "GPS"}

#: How often the GNSS is allowed to correct a clock it already set. The RTC
#: drifts and the satellites do not, but re-announcing it every second would
#: bury everything else.
GPS_RESYNC_S = 600

#: Portnum 3, `POSITION_APP`. Where a node says where it is.
PORT_POSITION = 3

#: How often to broadcast a position, once there is one to broadcast. The
#: firmware's own default is fifteen minutes, and a position is a whole packet
#: on a shared band: sending it faster buys precision nobody asked for out of
#: airtime everyone shares.
POSITION_S = 900

#: How many of the 32 bits of each coordinate to state. 32 is the full reading;
#: 13 is roughly a kilometre, 16 roughly a hundred metres. A privacy setting,
#: not an accuracy one, which is why it goes out on the wire beside the number.
POSITION_PRECISION = 32

#: Enough for a `Position` with every field this node can fill.
POSITION_BYTES = 64

#: How often `listen` prints the noise floor while nothing is arriving.
FLOOR_S = 30

#: Whether every reported packet is followed by its raw bytes. Off because the
#: node's log is read for what happened, not for what was on the air; set it
#: from the REPL when a frame needs picking apart.
RAW_HEX = False


def open_gps(name=None):
    """The GNSS this board carries, or None if it has none.

    `MESH_GPS` names the module supplying a `Reader`, the way `MESH_RADIO`
    names the radio driver. A board without the setting keeps a clock it was
    given rather than one it read, which is what every board did until now.
    """
    if name is None:
        name = supervisor.get_setting("MESH_GPS", "")
    if not name:
        return None
    try:
        module = __import__(name)
        #: A dotted name comes back as the top package, not the submodule.
        for part in name.split(".")[1:]:
            module = getattr(module, part)
        return module.Reader()
    except (ImportError, AttributeError):
        # Not fatal: a node with no clock of its own still carries traffic.
        print("# no GNSS module %r; the clock will come from elsewhere" % name)
        return None


class Node:
    """One board on one channel: provisioned, configured and ready to talk."""

    def __init__(self, radio=None, config=None, declared=None):
        mt.self_check()
        self.provisioned = mesh_config.config_provision(**(declared or {}))
        # Read after provisioning, because that is where the key and identity
        # are.
        self.config = mesh_config.Config() if config is None else config
        self.settings = self.config.settings()
        # Before the radio and the roster, because this is the largest block
        # the node asks for and the heap will not have a better one later. An
        # unnamed primary channel is called after its preset, which is the rule
        # that also decides the channel hash.
        self.inbox = inbox.Inbox(
            self.config.node_num,
            self.config.channel_name or self.settings.preset_name)
        self.radio = open_radio() if radio is None else radio
        # The adapter is the only module that knows which board this is, and
        # the nodeinfo and the phone's DeviceMetadata must not disagree.
        mt.HW_MODEL = getattr(self.radio, "hw_model", mt.HW_MODEL)
        #: The battery and its charger, or None on a board that cannot measure
        #: itself. Opened here rather than on demand because it claims pins, and
        #: a board that cannot give them up should say so at start-up.
        self.power = meshpower.open_power()
        self.tx = None
        self.sent_id = None
        self.sent = 0
        self.heard = 0
        self.dropped = 0
        #: Frames whose payload came back from `decode`. Heard-but-never-decoded
        #: is the signature of a wrong channel key, and looks nothing like a
        #: quiet mesh once the two numbers are side by side.
        self.decoded = 0
        self.nodes = nodedb.NodeDB()
        #: (peers, messages) taken back out of NVM. Read before anything is
        #: heard, so the two tables start the session where they left off.
        cache = _cache()
        #: False when nothing this node learns outlives a reset.
        self.caching = cache is not None
        if cache is None:
            self.restored = (0, 0)
            self._autosave_s = self._settle_s = 0
        else:
            self.restored = cache.cache_load(self)
            # Kept as numbers so the deadlines below never need the module back.
            self._autosave_s = cache.AUTOSAVE_S
            self._settle_s = cache.SETTLE_S
            _drop_cache()
        #: When the cache is next written back. None until `start`, so building
        #: a node for a test never spends an erase cycle.
        self._next_save = None
        # No battery-backed RTC and no network, so the time comes from whatever
        # the board can reach: its own satellites if it has a receiver, else a
        # connected client, else whatever the mesh happens to say.
        self.clock_set = False
        #: Which source set it, so the mesh cannot overrule the GPS. See the
        #: CLOCK_* ranks above.
        self.clock_rank = CLOCK_NONE
        #: A GNSS reader, on a board that has one: anything with `poll()` and a
        #: `fix`. Named by MESH_GPS; None on a board without the setting.
        self.gps = open_gps()
        self._next_gps = None
        #: When a position is next due, or None while there is no GNSS. Set on
        #: the first confirmed fix rather than at boot: a node with a receiver
        #: that never gets indoors should not spend a slot saying so.
        self._next_pos = None
        #: Both assignable from the REPL. Neither is in NVM, because a stored
        #: position interval is a setting you cannot see and would not think to
        #: look for after moving the board somewhere it should be quieter.
        self.position_s = POSITION_S
        self.position_precision = POSITION_PRECISION
        self._pos_buf = None
        #: True while a client is connected. Set by the phone session.
        self.attached = False
        #: When nodeinfo is next due. None until the radio is up.
        self._next_info = None
        #: Cleared by the first nodeinfo, the only one that asks to be answered.
        self._ask_names = True
        #: Called with (packet, frame, payload) once every frame has been filed.
        #: One hook rather than a list: the only thing that wants it is a phone,
        #: and there is one of those. Exceptions are not caught, because a
        #: listener that is broken should say so rather than quietly stop.
        self.on_receive = None

    # --------------------------------------------------------------- bring-up

    def start(self, *, quiet=False):
        """Brings the radio up on the configured channel."""
        cfg = self.config
        self.radio.open()

        mt.protocol_configure(self.radio, self.settings, power_dbm=cfg.tx_power_dbm)
        # Every Meshtastic node runs its receiver boosted, so a node that does
        # not is a couple of dB deafer than the peers judging its range.
        self.radio.reset_counters()

        duty = mesh_tx.DutyCycle(cfg.duty_cycle_pct
                                 or self.settings.duty_cycle_pct)
        self.tx = mesh_tx.Transmitter(self.radio, self.settings,
                                      cfg.channel_key, cfg.node_num,
                                      duty_cycle=duty)
        # Not due immediately: the first nodeinfo is a decision for whoever
        # brought the node up, since entering the REPL should not transmit.
        self._next_info = nodedb.seconds() + cfg.nodeinfo_s
        self._next_save = (nodedb.seconds() + self._autosave_s
                           if self.caching else None)
        #: When the last unsaved change should reach flash. See `service`.
        self._settle_at = None
        if not quiet:
            print(self.radio.description)
            self.describe()
        return self

    def describe(self):
        """The header. Commented so a redirected session is self-describing."""
        if self.provisioned:
            print("# provisioned NVM")
        print("# meshtastic node, firmware %s" % mt.PINNED_FIRMWARE)
        print("# %s" % self.settings)
        print("# node 0x%08x relay 0x%02x channel 0x%02x, key from %s"
              % (self.tx.node_num, self.tx.relay_node, self.tx.channel,
                 self.config.key_source))
        print("# %+d dBm, %s" % (self.radio.power_dbm, self.tx.duty_cycle))
        print("# %s" % keystore.keystore_describe())
        if self.restored[0] or self.restored[1]:
            print("# cache: %d nodes, %d messages restored" % self.restored)
        elif not self.caching:
            print("# cache: off, nothing kept across a reset")

    # --------------------------------------------------------------- transmit

    def wait_for_clear(self, tries=8):
        """Listen before talk. True once the channel is quiet."""
        for _ in range(tries):
            try:
                if not self.radio.busy():
                    return True
            except RuntimeError as err:
                # A CAD that never completes says nothing about the channel, so
                # it cannot be read as permission to transmit.
                print("# cad: %s" % err)
                return False
            # A LongFast packet is most of a second, so back off by about that
            # much rather than spinning; being clear of the packet already in
            # the air is what matters, not the exact interval.
            time.sleep(0.5 + self.radio.entropy() % 500 / 1000.0)
        return False

    def send(self, payload, portnum, **kwargs):
        """Sends one payload past both guards, or says why it did not.

        Returns the packet id, which is what a later echo is recognised by.
        """
        frame = self.tx.frame(payload, portnum, **kwargs)
        us = self.tx.airtime_us(len(frame))
        print("# sending %d bytes, %d ms on air" % (len(frame), us // 1000))
        if not self.tx.duty_cycle.allows(us):
            print("# refused: %s" % self.tx.duty_cycle)
            return None
        if not self.wait_for_clear():
            print("# refused: channel busy")
            return None
        left = self.radio.send(frame)
        self.tx.duty_cycle.record(us)
        if not left:
            # The chip's own transmit timeout. Airtime is recorded anyway: the
            # duty cycle is about what the antenna was asked to do.
            print("# transmit timed out, packet did not leave")
            self.radio.listen()
            return None
        self.sent += 1
        # Receiving is the resting state: transmit leaves the chip in standby,
        # and anything replying to this is already on its way.
        self.radio.listen()
        packet = mt.Packet(frame)
        print("SENT  %s" % packet.describe(mt.decode(frame, self.tx.key)))
        self.sent_id = packet.id
        return packet.id

    def announce(self, message=None):
        """Broadcasts the given text. None if there was nothing to say."""
        if not message:
            print("# nothing to send, call send() with a message")
            return None
        return self.send(message.encode("utf-8"), mt.PORT_TEXT_MESSAGE)

    def introduce(self):
        """Broadcasts who this node is, so it stops being a bare number.

        The first one asks to be answered. Names reach the air in that one
        payload and nothing else, so a roster left to collect unprompted
        broadcasts stays a list of numbers for hours. Later ones do not ask:
        the reply is the whole mesh at once, and once is enough.
        """
        long_name, short_name = nodeinfo.names(self.tx.node_num)
        ask = self._ask_names
        self._ask_names = False
        print("# nodeinfo: %s (%s)%s"
              % (long_name, short_name, ", asking for replies" if ask else ""))
        self._next_info = nodedb.seconds() + self.config.nodeinfo_s
        return self.send(
            nodeinfo.user(self.tx.node_num, long_name, short_name),
            nodeinfo.PORT_NODEINFO, want_response=ask)

    def due_in(self):
        """Seconds until the next nodeinfo, or 0 if it is already due."""
        if self._next_info is None:
            return 0
        return max(0, self._next_info - nodedb.seconds())

    def locate(self, precision=None):
        """Broadcasts where this node is. None if it has nothing to say.

        Refuses anything the receiver has not confirmed, rather than sending
        zeroes. A position is the one broadcast that every node which hears it
        writes down, so a wrong one outlives the mistake by hours and travels
        further than the node that made it.
        """
        if self.gps is None:
            return None
        if precision is None:
            precision = self.position_precision
        if self._pos_buf is None:
            # Kept, not made per send: this runs on a schedule for the life of
            # the board, and a fresh buffer every quarter hour is fragmentation
            # bought for nothing.
            self._pos_buf = bytearray(POSITION_BYTES)
        try:
            used = mt.position_encode(self._pos_buf, self.gps.fix.state,
                                      precision)
        except ValueError:
            # No confirmed fix. Not an error here, just nothing to send yet.
            return None
        return self.send(bytes(self._pos_buf[:used]), PORT_POSITION)

    def save(self, force=False):
        """Writes the roster and the inbox to NVM. True if flash was written.

        Re-arms the automatic save either way, including when there was nothing
        to write: the deadline is there to bound how often flash is touched, and
        a check that found nothing has done its job.
        """
        if not self.caching:
            return False
        self._next_save = nodedb.seconds() + self._autosave_s
        self._settle_at = None
        if not (force or self.nodes.dirty or self.inbox.dirty):
            return False
        try:
            return _cache().cache_save(self)
        except MemoryError:
            # Loading it needs two contiguous kilobytes, and this runs from the
            # poll loop where a phone may be holding most of the heap. The
            # deadline above is already re-armed and the dirty flags are
            # untouched, so the write is postponed rather than lost.
            print("# cache: no room to save, will retry")
            return False
        finally:
            _drop_cache()

    def service(self):
        """The upkeep a node owes the mesh. True if it transmitted.

        Only nodeinfo goes on the air; saving the cache is here too because this
        is the one thing that runs whenever anything is running. Called from
        `poll`, so there is no timer, just deadlines that get checked on the way
        past.
        """
        now = nodedb.seconds()
        if self.gps is not None:
            self._service_gps()
            if self._next_pos is None and self.gps.fix.valid:
                # Armed by the first confirmed fix and not at boot: time comes
                # off one satellite long before there is a position worth
                # stating, and a receiver that never gets a fix should not
                # spend a slot saying so. Due at once, so the mesh hears where
                # this is as soon as it knows.
                self._next_pos = now
        if self.caching and (self.nodes.dirty or self.inbox.dirty):
            # Dated from the first change, not the last, so a mesh that keeps
            # producing news cannot postpone the write indefinitely.
            if self._settle_at is None:
                self._settle_at = now + self._settle_s
        else:
            self._settle_at = None
        if ((self._next_save is not None and now >= self._next_save)
                or (self._settle_at is not None and now >= self._settle_at)):
            self.save()
        if (self._next_pos is not None and now >= self._next_pos
                and self.position_s):
            # Re-armed whether or not it goes, for the same reason nodeinfo is:
            # a refusal should wait out the interval rather than retry against
            # a full duty cycle on every poll.
            self._next_pos = now + self.position_s
            if self.locate() is not None:
                return True
        if self._next_info is None or nodedb.seconds() < self._next_info:
            return False
        # Re-armed before sending, so a refusal waits out the interval rather
        # than retrying against a full duty cycle on every poll.
        self._next_info = nodedb.seconds() + self.config.nodeinfo_s
        return self.introduce() is not None

    # ---------------------------------------------------------------- receive

    def reload(self):
        """Re-reads stored settings, after something else has written them.

        Reporting only. The radio was tuned in `start` and the transmitter kept
        the settings it was given, so what is on the air is unchanged until a
        reset; what this node says about itself is not, because a client that
        reads back the value it just replaced takes the write for lost.
        """
        self.config = mesh_config.Config()
        self.settings = self.config.settings()

    def factory_reset(self, full=False):
        """Throws the settings away. `full` takes what was heard with them.

        Nothing re-reads the store without a restart, so the caller owes one:
        until then this node is still running on settings that no longer exist.
        The cache is erased rather than left to be overwritten, because the text
        in it is the one thing here that came from somebody else.
        """
        keystore.keystore_erase()
        if full:
            self.nodes.forget_all()
            self.inbox.forget_all()
            cache = _cache()
            if cache is not None:
                cache.cache_erase()
                _drop_cache()
            # Nothing to write back, and the deadline would only put the empty
            # tables back over an already-blank region.
            self.nodes.dirty = self.inbox.dirty = False
        print("# factory reset: %s erased, restart to take effect"
              % ("settings, nodes and messages" if full else "settings"))

    def restart(self):
        """Reboots the board. Does not return."""
        # An ordered reboot is the one reset that can be seen coming, so it is
        # the one that has no excuse for losing anything.
        self.save()
        print("# restarting")
        microcontroller.reset()

    def set_clock(self, when, source=CLOCK_PHONE):
        """Takes the time from a client or this board's GPS. False if refused."""
        if source < self.clock_rank:
            # A phone reconnecting must not pull the clock back off the
            # satellites, and nothing may undo either with a mesh stamp.
            return False
        rtc.RTC().datetime = time.localtime(when)
        self.clock_set = True
        self.clock_rank = source
        print("# clock set from %s: %s UTC"
              % (CLOCK_NAMES.get(source, source), mt.stamp(when)))
        return True

    @property
    def clock_from_phone(self):
        """True once something better than the mesh has set the clock."""
        return self.clock_rank >= CLOCK_PHONE

    def _service_gps(self):
        """Reads the GNSS, and takes its time. The only clock here that is not
        somebody else's word for it."""
        self.gps.poll()
        when = self.gps.fix.when
        if when is None:
            return
        now = nodedb.seconds()
        if self.clock_rank < CLOCK_GPS:
            self.set_clock(when, CLOCK_GPS)
            self._next_gps = now + GPS_RESYNC_S
        elif self._next_gps is None or now >= self._next_gps:
            # Quietly this time: the announcement was made when it first landed.
            rtc.RTC().datetime = time.localtime(when)
            self._next_gps = now + GPS_RESYNC_S

    def _seed_clock(self, packet, payload):
        """Takes the time from the mesh, forwards only and never far."""
        # A stamp off the air is a stranger's guess. A connected client has a
        # real clock and offers it unasked, so while one is here the mesh is not
        # consulted, and once one has been believed it is not overruled.
        if self.clock_rank >= CLOCK_PHONE or self.attached:
            return
        if not self.clock_set:
            # Only telemetry may seed. Position.time is when the fix was taken,
            # and a node with a manually set location keeps sending the day it
            # was set -- one here is seven weeks stale. Seeding from that would
            # strand the clock in the past, because the ratchet's one-hour cap
            # would then reject every correct stamp that followed.
            if payload.portnum == _PORT_TELEMETRY:
                rtc.RTC().datetime = time.localtime(payload.time)
                self.clock_set = True
                self.clock_rank = CLOCK_MESH
                print("# clock set from 0x%08x: %s UTC"
                      % (packet.from_, mt.stamp(payload.time)))
            return
        # Each stamp is a lower bound on the true time: it was written before
        # the packet spent airtime and hops getting here. So the newest one seen
        # is the least wrong, and the clock only ever ratchets forward. The cap
        # is because nothing on an unauthenticated broadcast has earned an hour
        # of trust.
        ahead = payload.time - time.time()
        if 0 < ahead < 3600:
            rtc.RTC().datetime = time.localtime(payload.time)

    def _receive(self, frame, rssi, snr, signal):
        """Files one frame away. Returns (packet, payload, peer, message).

        Separate from printing because what is kept must not depend on whether
        anyone was watching: `poll` is called from the REPL, where displaying it
        is a later and separate decision.
        """
        if len(frame) < mt.HEADER_LEN:
            return None, None, None, None
        packet = mt.Packet(frame, rssi, snr, signal)
        if packet.from_ == self.config.node_num:
            # Ours, coming back through a neighbour that rebroadcast it. It is
            # the only evidence this node gets that a packet reached anyone, so
            # it is worth reporting, but filing it would put this node in its
            # own roster and hand the phone back what the phone just sent.
            return packet, None, None, None
        payload = mt.decode(frame, self.config.channel_key)
        peer = self.nodes.heard(packet)
        message = None
        if payload is not None:
            self.decoded += 1
            if payload.portnum == nodeinfo.PORT_NODEINFO:
                before = (peer.long, peer.short, peer.hw)
                self.nodes.learn(packet.from_, payload.body)
                if (peer.long, peer.short, peer.hw) != before:
                    # The only moment a node stops being a bare number. Worth a
                    # line because it is what the phone's list is made of.
                    print("# learned 0x%08x is %s (%s), hw %d"
                          % (peer.num, peer.long, peer.short, peer.hw))
            else:
                message = self.inbox.add(packet, payload)
            if payload.time:
                self._seed_clock(packet, payload)
        if self.on_receive is not None:
            # After filing, so that anything the hook goes on to ask the node
            # about already knows this packet happened.
            self.on_receive(packet, frame, payload)
        return packet, payload, peer, message

    def _report(self, frame, rssi, snr, signal, ticks):
        packet, payload, peer, _message = self._receive(frame, rssi, snr, signal)
        if packet is None:
            # Correct CRC but too short to be a Meshtastic frame: almost
            # certainly another protocol sharing the band.
            print("%5d  runt, %d bytes, %.1f dBm"
                  % (self.heard, len(frame), rssi))
        else:
            at = ""
            if self.clock_set:
                now = time.localtime()
                at = "%02d:%02d:%02d " % (now[3], now[4], now[5])
            if packet.from_ == self.config.node_num:
                # Matched on the sender rather than on the last id sent: by the
                # time a relay hands one back, this node has usually sent
                # another, and the id it was compared against has moved on.
                print("%5d %s ECHO %s"
                      % (self.heard, at, packet.describe(None)))
            else:
                print("%5d %s %s" % (self.heard, at, packet.describe(payload)))
                who = "node=%s" % peer.name
                if payload is not None:
                    print("       %s %s" % (who, payload.describe()))
                else:
                    # Foreign channel or wrong key: the name still stands, and
                    # knowing who transmits there is most of what is knowable.
                    print("       %s" % who)
        if RAW_HEX:
            print("       %d %.1f %.2f %s"
                  % (ticks, rssi, snr, "".join("%02x" % b for b in frame)))

    def _dropped(self, err):
        """Prints whatever survived a reception the modem threw away."""
        frame = getattr(err, "frame", None)
        if frame is None:
            # Nothing to show: a header error means the PHY header failed its
            # own CRC, so the modem never learnt how long the packet was and
            # gave up before writing any of it. The flags say how far it got.
            print("dropped: %s irq=0x%08x" % (err, getattr(err, "flags", 0)))
            return
        print("dropped: %s" % err)
        if len(frame) >= mt.HEADER_LEN:
            # The CRC covers the whole packet, so one wrong byte anywhere fails
            # it while the header in front is very often still readable.
            print("       %s" % mt.Packet(frame, *err.status).describe(None))
        else:
            print("       %s" % "".join("%02x" % b for b in frame))

    def _rearm(self):
        """Puts the radio back into receive, but only if it actually left.

        `listen` goes through standby, which throws away whatever is
        part-way in, and after handling one packet there usually is something:
        neighbours rebroadcast the same packet within a second of each other,
        and a reply to a request arrives in amongst them.
        """
        if not self.radio.listening:
            self.radio.listen()

    def poll(self, *, report=True, limit=8):
        """Collects whatever the radio already has, without waiting.

        Returns how many frames were taken. `limit` caps one call, so a radio
        failing the same way every time cannot hold the interpreter in here.
        """
        taken = 0
        self.service()
        while taken < limit:
            try:
                frame = self.radio.collect()
            except RuntimeError as err:
                # A corrupt packet is still evidence: something is transmitting
                # on this frequency, which is more than silence tells us. Count
                # it and carry on.
                self.dropped += 1
                if report:
                    self._dropped(err)
                self._rearm()
                taken += 1
                continue
            if frame is None:
                return taken

            ticks = supervisor.ticks_ms()
            rssi, snr, signal = self.radio.quality()
            self.heard += 1
            taken += 1
            if report:
                self._report(frame, rssi, snr, signal, ticks)
            else:
                self._receive(frame, rssi, snr, signal)

            self._rearm()
        return taken

    def missed(self):
        """Packets the chip counted that this code never collected.

        The radio holds one: `get_rx_buffer_status` reports the most recent
        packet, so anything landing while nothing polls is overwritten. The gap
        between the chip's counter and ours is what that cost.
        """
        rx = self.radio.counters()[0]
        return rx - self.heard if rx > self.heard else 0

    def listen(self, seconds=None):
        """Prints every packet heard until the deadline or a ctrl-C.

        None means until interrupted, which is what the REPL wants.
        """
        # Asking to be shown packets is what makes the text layer worth its
        # five kilobytes; serving the phone app never gets here.
        mt.WANT_TEXT = True
        started = supervisor.ticks_ms()
        next_floor = started
        quietest = 0.0
        deadline = (None if not seconds else started + seconds * 1000)
        print("listening, ctrl-C to stop")
        print()

        try:
            self.radio.listen()
            while deadline is None or meshradio.ticks_diff(
                    deadline, supervisor.ticks_ms()) > 0:
                if self.poll():
                    continue
                # The quietest reading of the window rather than the latest: a
                # single sample lands on a packet as often as not, and then
                # reports a distant sender's strength as if it were the noise.
                rssi = self.radio.noise()
                if rssi < quietest:
                    quietest = rssi
                if meshradio.ticks_diff(supervisor.ticks_ms(),
                                        next_floor) >= 0:
                    next_floor = supervisor.ticks_ms() + FLOOR_S * 1000
                    print("# floor %.1f dBm, %d heard, %d decoded, %d dropped"
                          % (quietest, self.heard, self.decoded, self.dropped))
                    quietest = 0.0
        except KeyboardInterrupt:
            print()
        finally:
            # Left receiving, not in standby: the chip holds the most recent
            # packet either way, and standby would hold nothing at all.
            self.radio.listen()

        elapsed = meshradio.ticks_diff(supervisor.ticks_ms(), started) / 1000.0
        rx, crc_err, hdr_err, false_sync = self.radio.counters()
        print("%d packets in %.0f s, %d decoded, %d dropped"
              % (self.heard, elapsed, self.decoded, self.dropped))
        print("chip counters: rx %d crc_err %d hdr_err %d false_sync %d"
              % (rx, crc_err, hdr_err, false_sync))
        self.report_nodes()
        self.report_messages()

    def report_nodes(self):
        """The roster, which is the part of a capture worth keeping."""
        if not len(self.nodes):
            return
        print()
        print("# %d nodes heard%s"
              % (len(self.nodes),
                 "" if not self.nodes.forgotten
                 else ", %d forgotten" % self.nodes.forgotten))
        print("# number     short   hops  pkts    snr   last  long")
        for peer in self.nodes.roster():
            print("# %s" % peer.line())

    def report_messages(self, where=None, mark=True):
        """The text, which is the other half of what a capture is for."""
        found = self.inbox.read(where, mark=mark)
        if not found:
            return
        print()
        print("# %d messages held%s"
              % (len(self.inbox),
                 "" if not self.inbox.dropped
                 else ", %d dropped" % self.inbox.dropped))
        for message in found:
            print("# %s" % message.line(self.nodes.name(message.from_)))
