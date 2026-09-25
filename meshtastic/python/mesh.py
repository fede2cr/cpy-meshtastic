"""The node as one importable thing, for driving from the REPL.

The radio interrupts us. The adapter knows which pin its part raises an
interrupt on -- LR1121 DIO9 on one board, LR2021 DIO8 on the other -- and the
firmware schedules `_collect` on its rising edge; the scheduler runs it at the
next VM safe point, which includes the loop `mp_hal_stdin_rx_chr` sits in while
the prompt waits for a keystroke. So a packet arriving while the cursor blinks
is read out of the chip and filed before the next one can overwrite it. This
needs the custom firmware -- `CIRCUITPY_PIN_INTERRUPT` and
`MICROPY_ENABLE_SCHEDULER` are both CircuitPython patches -- and `start()` says
so if the module is absent.

`_collect` is scheduled, not run in interrupt context: it is ordinary Python,
on the ordinary heap, using the same SPI bus as everything else. Which is the
one thing to be careful about, because it can land *inside* a foreground call.
`_hold` is how that is avoided: every entry point below owns the radio while it
runs, `_collect` gives up immediately if it is not free, and `_hold` polls on
the way out so nothing deferred is left sitting in the chip.

It collects silently. Printing from a scheduled callback would arrive in the
middle of whatever line was being typed, so what arrives goes into the inbox and
`read()` is still how it is looked at.

`listen(30)` remains, and is still the right thing when watching a channel is
the whole activity: it prints as packets land rather than storing them.

The state is a module global because that is what survives at a prompt. Import
this from `repl.py` rather than `boot.py`: boot.py, code.py and the REPL are
three separate VM runs with the heap cleared and the buses released between
them, so nothing built in the first two is still alive by the third. `repl.py`
runs inside the REPL's own VM, and its top-level names land in the namespace you
are typing at.

    >>> read()
    >>> send("on my way")

`phone()` adds the Meshtastic BLE service and `serve()` runs the loop that
feeds it. That loop is separate from everything above because a phone writing to
the board is the one event nothing here can be interrupted by: the radio has a
pin and the REPL has a keyboard, but a GATT write only becomes visible when
someone looks. `_hold` looks on the way out of every command, which covers the
prompt; `serve` is for when nobody is at it.
"""

import gc
import time

import supervisor

from meshtastic import meshlib as keystore
from meshtastic import meshlib as mesh_config
from meshtastic import meshlib as pb
from meshtastic import meshlib as meshnode
from meshtastic import meshlib as nodeinfo

_node = None

#: The phone session, once `phone()` has been called. See `serve`.
_phone = None

#: True while foreground code owns the radio. See `_hold`.
_busy = False

#: How many packets the interrupt collected. `status()` prints it next to the
#: chip's own counter, which is what says whether any were still missed.
_collected = 0

#: How long `serve` goes without hearing anything before it says so. A quiet
#: mesh and a deaf radio look identical from the log otherwise.
QUIET_S = 300

#: Whether packets collected in the background are printed the way `listen`
#: prints them. Set False from the REPL when the traffic is drowning out
#: whatever else is being watched.
REPORT = True


def _collect(pin):
    """Scheduled by the DIO9 edge. Takes what the chip is holding."""
    global _collected
    if _busy or _node is None:
        # Foreground code is mid-transaction on this bus. It polls before it
        # lets go, so the packet is not lost by giving up here.
        return
    _collected += _hold(_node.poll, report=REPORT)


def _hold(fn, *args, **kwargs):
    """Runs fn with the radio held, then drains whatever arrived meanwhile."""
    global _busy
    outer = _busy
    _busy = True
    try:
        return fn(*args, **kwargs)
    finally:
        # Still held for this last poll, so it cannot be re-entered by the very
        # callback it exists to make unnecessary. The DIO line stays asserted
        # until the flags are cleared, so no further edge arrives while it is
        # high, and anything `_collect` declined to take is taken here instead.
        if not outer and _node is not None:
            _node.poll(report=REPORT)
            if _phone is not None:
                # Under the hold as well: answering the phone can transmit, and
                # that has to be as exclusive as anything else on this bus.
                _phone.pump()
        _busy = outer


def start(*, quiet=False, watch=True, **declared):
    """Brings the radio up once, and leaves it receiving and watched.

    Any setting named here is written to NVM at every start, so `code.py` is
    the statement of what this node is. Everything unnamed keeps its default
    and stays the board's own, changeable from the prompt with `configure()`.

    `watch=False` leaves the interrupt off, so traffic is only collected when
    something asks. It exists to take the interrupt out of the picture when the
    prompt itself is what is misbehaving.
    """
    global _node
    if _node is None:
        node = meshnode.Node(declared=declared).start(quiet=quiet)
        node.radio.listen()
        _node = node
        if watch:
            try:
                node.radio.watch(_collect)
            except (AttributeError, RuntimeError) as error:
                # AttributeError: stock firmware has no pin_interrupt in
                # mp_fun_table, so the module has no attach_irq. RuntimeError:
                # the module has it, but the port supplies no backend --
                # attach_irq is built from a tree-wide #define, so it exists
                # even where nothing implements it, and then blames pin
                # contention for a missing port. Everything else still works;
                # the prompt is just deaf between commands again.
                print("# no interrupt support in this firmware, use listen()")
                print("#   (%s)" % error)
    return _node


def node():
    return start(quiet=True)


def poll():
    """Takes whatever the radio is holding. Returns how many frames."""
    return _hold(node().poll, report=False)


def listen(seconds=30):
    """Blocks and prints, rather than storing silently."""
    _hold(node().listen, seconds)


def send(text=None):
    """Broadcasts a text message."""
    return _hold(node().announce, text)


def introduce():
    """Broadcasts this node's name now, and pushes the next one out.

    Rarely needed: `poll` re-sends on the firmware's own three-hour schedule.
    """
    return _hold(node().introduce)


def locate(precision=None):
    """Broadcasts where this node is now, and pushes the next one out.

    Returns None when the receiver has no fix it stands behind, which indoors
    is most of the time. `poll` sends these on its own schedule once there is
    one; this is for checking that the path works without waiting for it.
    """
    return _hold(node().locate, precision)


def unread():
    """Whether anything has arrived that has not been read."""
    return _hold(lambda: node().inbox.unread)


def read(where=None):
    """Prints the messages in one conversation, or in all of them."""
    _hold(node().report_messages, where)


def peek(where=None):
    """Prints them without marking them read."""
    _hold(node().report_messages, where, mark=False)


def nodes():
    """Prints every node heard."""
    _hold(node().report_nodes)


#: What can be asked for, under the names the app uses rather than by portnum.
#: A telemetry request names the kind wanted with an empty submessage, so the
#: field that submessage takes in `Telemetry` is all that separates the kinds --
#: they all go out as portnum 67 and are answered by different modules.
_TELEMETRY = 67
ASKABLE = {
    "telemetry": (_TELEMETRY, 2),
    "device": (_TELEMETRY, 2),
    "environment": (_TELEMETRY, 3),
    "air": (_TELEMETRY, 4),
    "power": (_TELEMETRY, 5),
    "energy": (_TELEMETRY, 5),
    "stats": (_TELEMETRY, 6),
    "health": (_TELEMETRY, 7),
    "nodeinfo": (nodeinfo.PORT_NODEINFO, None),
    "position": (3, None),
}


def ask(num, what="telemetry"):
    """Asks one node for something, and asks to be told the question arrived.

    The ack is the point. Silence after a request means either that the node
    never heard it or that it heard and declined to answer, and those want
    opposite fixes; a routing reply on portnum 5 tells them apart. The app
    leaves `want_ack` clear, so nothing it sends can distinguish the two.

    Only `device` and `stats` are answered by every node. `environment`, `air`,
    `power` and `health` need the far end to carry that hardware and have the
    module switched on, and one that does not stays silent rather than
    answering empty -- so no answer is a fact about its sensors, not a fault.

    Asking twice for a nodeinfo is worse than asking once: the firmware
    remembers who it last answered and then stays quiet for twelve hours, and
    the request it refuses still restarts that clock.
    """
    portnum, variant = ASKABLE.get(what, (what, None))
    this = node()
    peer = this.nodes.get(num)
    # Printed before asking: how far away it is and how well it was heard is
    # most of what decides whether an answer could get back at all.
    print("# %s" % (peer.line() if peer is not None
                    else "0x%08x has not been heard" % num))
    body = b""
    if portnum == nodeinfo.PORT_NODEINFO:
        # Firmware treats this one as a trade, and answers the requests that
        # arrive with the asker's own name inside them.
        long_name, short_name = nodeinfo.names(this.tx.node_num)
        body = nodeinfo.user(this.tx.node_num, long_name, short_name)
    elif variant is not None:
        # An empty body is answered by nobody. The far end decodes it as a
        # `Telemetry` and replies only for the variant the oneof selected, so
        # the question is an empty submessage rather than nothing at all.
        buf = pb.msg_buffer()
        body = pb.msg_take(buf, pb.blob(buf, 0, variant, b"", always=True))
    return _hold(this.send, body, portnum, to=num,
                 want_ack=True, want_response=True)


def save():
    """Writes the roster and the messages to NVM, so a reset keeps them.

    Rarely needed by hand: `stop()` and `serve()` both do it on the way out and
    `poll()` does it on a six-hour deadline. It is here because that deadline is
    long on purpose -- see `meshcache` -- and someone about to pull the power is
    better informed than any timer.
    """
    this = node()
    wrote = _hold(this.save, True)
    if not this.caching:
        print("# cache: off, nothing kept across a reset")
        return False
    print("# cache: %d nodes, %d messages%s"
          % (len(this.nodes), len(this.inbox),
             "" if wrote else " (no change)"))
    return wrote


def channels():
    """Prints the conversations, with how much is unread in each."""
    held = _hold(node().inbox.conversations)
    if not held:
        print("# nothing heard yet")
        return
    for where in sorted(held):
        total, new = held[where]
        print("# %-12s %3d held %3d unread" % (where, total, new))


def status():
    """Where the radio is and what it has taken in."""
    this = node()
    _hold(this.poll, report=False)
    print("# %s" % this.settings)
    print("# %d heard, %d dropped, %d still missed"
          % (this.heard, this.dropped, this.missed()))
    print("# %d collected by interrupt" % _collected)
    print("# %d nodes, %d messages, %d unread"
          % (len(this.nodes), len(this.inbox), this.inbox.unread_count()))
    due = this.due_in()
    print("# nodeinfo %s" % ("due" if not due else "in %dm" % (due // 60)))
    print("# %+d dBm, %s" % (this.radio.power_dbm, this.tx.duty_cycle))
    if _phone is not None:
        print("# %s" % _phone.describe())


def settings():
    """What is stored, which is not always what the radio is running."""
    print("# %s" % mesh_config.config_describe())
    print("# %s" % keystore.keystore_describe())


def configure(**values):
    """Changes a stored setting: configure(region="EU_868", preset="LongSlow").

    Names are `keystore.TAG_NAMES`. This is the whole reason the settings are in
    NVM rather than in a file: the board can write it, so a node can be retuned
    from the prompt it is answering at, or from anywhere else that reaches this.

    The radio is not retuned here. It was configured by `start()` and the
    channel it is on is baked into a running `Transmitter`, so the new value
    applies at the next reset rather than mid-conversation.
    """
    if not mesh_config.config_configure(**values):
        print("# unchanged")
        return
    print("# %s" % mesh_config.config_describe())
    if _node is not None:
        print("# reset to run it")


#: The transport `phone()` opens when `MESH_PHONE` says nothing. BLE is what
#: the app reaches for first and what the firmware ships, so it stays the
#: default; a board whose stack will not advertise sets `MESH_PHONE = "tcp"`
#: in settings.toml and gets the same two protobufs over a socket.
DEFAULT_PHONE = "ble"


def phone(over=None):
    """Starts advertising and serving the Meshtastic app. Idempotent.

    Not started by `start()`, because bringing up the SoftDevice costs RAM and
    takes the board's only advertising set away from the CIRCUITPY BLE workflow.
    That is a decision, so it is a call.

    `over` picks the transport: "ble" for the mesh service the app scans for,
    "tcp" for the Network entry in the app's connect screen. They carry the
    same messages through the same `PhoneAPI` and differ only in the envelope.
    """
    global _phone
    if _phone is None:
        if over is None:
            over = supervisor.get_setting("MESH_PHONE", DEFAULT_PHONE)
        this = start(quiet=True)
        # The SoftDevice and the read queue are the two largest things this
        # board ever asks for, and they are asked for here. Collecting first
        # costs nothing; printing what was left is the only warning there is
        # before a MemoryError with no context in front of it.
        gc.collect()
        print("# phone: %s starting, %d bytes free" % (over, gc.mem_free()))
        # After the collect, so the loader gets a compacted heap: meshphone.mpy
        # is the single largest allocation this board makes.
        from meshtastic import meshphone as transport
        if over == "tcp":
            session = transport.Network(this)
        elif over == "ble":
            session = transport.Phone(this)
        else:
            raise ValueError("MESH_PHONE is %r; expected 'ble' or 'tcp'" % over)
        where = session.advertise()
        # Loading the module and bringing the link up leave a lot behind.
        # Collect before reporting, so the number is real headroom rather than
        # uncollected garbage, and so the receive loop starts on a packed heap.
        gc.collect()
        print("# phone: %s on %s, %d bytes free" % (over, where, gc.mem_free()))
        _phone = session
    return _phone


def serve(seconds=None, each=None):
    """Runs the node with the phone attached, until ctrl-C or the time is up.

    Neither a GATT write nor a socket read interrupts anything, so someone has
    to keep asking. This is that someone. LoRa still arrives by interrupt
    underneath, and each turn of the loop is one `_hold`, so a packet and a
    phone command can never be halfway through the SPI bus at the same time.

    `each` is called once a turn, outside the hold: a screen is on a different
    bus and has no business blocking the radio's.
    """
    session = phone()
    this = node()
    deadline = None if seconds is None else time.monotonic() + seconds
    quiet_at = time.monotonic() + QUIET_S
    was_heard = this.heard
    try:
        while deadline is None or time.monotonic() < deadline:
            _hold(_turn, session, this)
            if each is not None:
                each()
            if time.monotonic() >= quiet_at:
                quiet_at = time.monotonic() + QUIET_S
                if this.heard == was_heard:
                    _hold(_silence, this)
                was_heard = this.heard
            # Long enough to be idle most of the time, short enough that the
            # app's own timeouts -- seconds, not milliseconds -- never notice.
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("# %s" % session.describe())
    # Whatever arrived during the loop, kept. A serve() that has just ended is
    # the likeliest moment for the board to be unplugged.
    _hold(this.save)
    return session


def _turn(session, this):
    session.pump()
    this.service()


def _silence(this):
    """Nothing heard for a while. The chip's own view says whose fault it is.

    The mode and the noise floor are the two that separate the cases. A chip
    not in RX is a driver fault; a floor pinned at some impossible value is the
    front end or the antenna; a plausible floor with every counter at zero is a
    receiver working perfectly on a frequency nobody is using.
    """
    rx, crc_err, hdr_err, false_sync = this.radio.counters()
    print("# quiet %ds on %.3f MHz %s: %d heard, %d dropped, chip rx %d crc %d "
          "hdr %d sync %d, %s, floor %.1f dBm"
          % (QUIET_S, this.settings.frequency_hz / 1e6, this.config.region,
             this.heard, this.dropped, rx, crc_err, hdr_err, false_sync,
             this.radio.state(), this.radio.noise()))


def stop():
    """Puts the radio in standby, unwatched, without forgetting anything."""
    if _node is not None:
        # Before the radio goes down rather than after: this is the last thing
        # that happens on purpose, and "without forgetting anything" now means
        # across the reset as well as across the call.
        _node.save()
        try:
            _node.radio.unwatch()
        except AttributeError:
            pass
        _node.radio.standby()
    if _phone is not None:
        _phone.stop()


def release():
    """Stops the node and hands the bus back, so another tool can take it.

    `stop()` keeps the radio claimed and the SPI lock held, which is right for
    a node that will be asked to carry on. It also means nothing else can build
    a radio on those pins until this is called: `repl.py` starts a node at
    every prompt, so `loratest` would otherwise meet "LORA_SCK in use".
    """
    global _node
    stop()
    if _node is not None:
        _node.radio.close()
        if _node.gps is not None:
            _node.gps.close()
        _node = None


def commands():
    print("# read() peek() unread() channels() nodes() listen(30)")
    print("# send(\"text\") introduce() poll() status() save() stop()")
    print("# ask(num) ask(num, \"environment\") node() release()")
    print("# phone() serve()")
    print("# settings() configure(region=\"EU_868\")")
