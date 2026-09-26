# Meshtastic on CircuitPython

A Meshtastic node written in Rust and Python, running on CircuitPython. It talks
to other Meshtastic radios on the air and to the Meshtastic phone app over
Bluetooth or WiFi, and it gives you a Python prompt to drive it from.

This page is for using it. If you want to build it, read the `Makefile` — it
explains itself when you run `make help`.

---

## What is supported

### Boards

| Board | Chip | Radio | Phone link | Screen | GPS |
|---|---|---|---|---|---|
| Muzi Base Duo | nRF52840 | LR1121 | Bluetooth | — | — |
| Meshnology W12 | ESP32-S3 | LR2021 | WiFi | OLED | yes |

### Radios

| Chip | File you copy | Bands |
|---|---|---|
| LR1121 | `lr1121.mpy` and `_lr1121.mpy` | sub-GHz and 2.4 GHz |
| LR2021 | `lr2021.mpy` | sub-GHz and 2.4 GHz |

The radio driver is **not** part of this project — it is a separate code base
with its own releases. That is why it sits in `lib/` on its own instead of
inside `lib/meshtastic/`: which one your board needs depends on which chip is
soldered to it, and you only ever copy one.

Both boards are set up for you already. You only need to think about this if you
are putting the software on a board that is not in the table.

---

## Installing

**Attach an antenna before you power the board up.** Transmitting without one
can damage the amplifier.

### 1. Put CircuitPython on the board

Each release carries a CircuitPython image for each board:
`circuitpython-muzi_base_duo-*.uf2` and `circuitpython-meshnology_w12-*.uf2`.
They are the only images that will run this project: a stock CircuitPython
build does not know these boards and cannot load its `.mpy` files.

**Base Duo.** Press the reset button twice, quickly. A USB drive appears. Drag
the `.uf2` onto it.

**Meshnology W12.** The board is sold without a UF2 bootloader, so the first
time you have to install one over USB. Take `tinyuf2-meshnology_w12-*.zip` from
the same release, unzip it, hold BOOT, tap reset, let go of BOOT, and run:

```
esptool.py --chip esp32s3 write_flash 0x0 combined.bin
```

This erases whatever the board was running, and the board then comes up as
`W12BOOT` on its own every time it starts: TinyUF2 is all that is on it. Drag
the `.uf2` onto `W12BOOT` once and from then on it starts CircuitPython. To
update later: tap reset, and **while the LED is lit** press BOOT, and drag the
new `.uf2` onto `W12BOOT`. Pressing reset twice does nothing
on this board, and holding BOOT *through* reset gets you the chip's own
bootloader instead, which is the one `esptool` talks to.

Either way the board restarts by itself and a drive called `CIRCUITPY` appears.
You only do this once per board, or when you update the firmware.

### 2. Copy the node onto CIRCUITPY

Unzip the release and copy everything onto the `CIRCUITPY` drive, so it looks
like this:

```
CIRCUITPY/
    code.py
    repl.py
    settings.toml
    lib/
        meshtastic/          <- all of this project
            __init__.mpy
            meshlib.mpy
            meshphone.mpy
            meshtext.mpy
            meshcache.mpy
            gps.py           (W12)
            oled.py          (W12)
        lr2021.mpy           <- your radio driver, W12
        lr1121.mpy           <- your radio driver, Base Duo
        _lr1121.mpy
```

**Delete what was on the drive first** rather than copying over it. A leftover
file from an older version will still be imported, and it will look like a bug
in the new one.

### 3. Set your region

Open `code.py` on the drive in any text editor and change one line:

```python
REGION = "US"
```

Use the one for where you are: `US`, `EU_868`, `EU_433`, `ANZ`, `JP`, `IN`,
`BR_902`, and so on. Getting this wrong is not a preference — it is the
difference between being legal and not, and between hearing your neighbours and
hearing nothing.

That is the whole installation. Reset the board and it joins the mesh.

---

## Naming your node

By default your node names itself the way stock Meshtastic firmware does:
`Meshtastic da67` and `da67`, taken from the last four digits of its own ID. A
brand new board is indistinguishable from a brand new stock one.

Three places can set a name. The first one that has an answer wins:

1. **The phone app.** Rename it there and it sticks — it is saved on the board
   and survives a reset. This is the normal way.
2. **`settings.toml`.** Uncomment `MESH_LONG_NAME` and `MESH_SHORT_NAME` for a
   name baked in before anyone has touched the board.
3. **The derived name**, if neither of the above has anything to say.

Because the app wins, editing `settings.toml` does nothing once you have
renamed from the phone. To hand the file its say back, type this at the prompt
(see below):

```python
>>> configure(long_name=None, short_name=None)
```

---

## `code.py` and the prompt

The board runs two different things, and knowing which one you are in explains
most of what you will see.

### `code.py` — for the phone

This runs automatically when the board powers up or resets. It brings the radio
up, announces itself on the mesh, opens the link to the phone app, and then
**does not return** — it sits in a loop answering the app until you stop it.

This is the mode you leave the board in. It is what you want when the board is
in a backpack with a phone next to it.

Press **Ctrl-C** to stop it and drop to the prompt.

### `repl.py` — for testing and poking at things

When you land at the `>>>` prompt, CircuitPython runs `repl.py` first. That
brings the radio up again and puts the node's commands within reach, then gets
out of the way.

It is a real Python prompt: tab completion, history, and the freedom to write a
loop. Packets that arrive while you are thinking are collected in the
background, so nothing is lost between commands.

```
>>> read()                      # messages that came in
>>> peek()                      # ...without marking them read
>>> unread()                    # how many are waiting
>>> nodes()                     # who else is out there
>>> channels()                  # the channels this node is on
>>> listen(30)                  # print traffic for 30 seconds
>>> send("hello")               # broadcast a message
>>> introduce()                 # tell the mesh this node's name
>>> ask(num)                    # ask a node for its details
>>> status()                    # radio, queue, anything missed
>>> settings()                  # everything this node is configured to do
>>> configure(region="EU_868")  # change one, for now
>>> phone()                     # open the link to the app
>>> serve()                     # ...and answer it until Ctrl-C
>>> stop()                      # radio to standby
```

`configure()` changes a setting **for this session**. Anything `code.py`
declares goes back to what the file says at the next reset. That is the point:
the file is what the node *is*, the prompt is what it is *doing for now*.

Ctrl-D reboots the board and puts you back in `code.py`.

### How to connect

You need a serial terminal. On macOS or Linux:

```
tio /dev/tty.usbmodem*
```

Windows users can use PuTTY or the Mu editor.

---

## Connecting the phone app

Both links carry exactly the same thing — same protocol, same features. Which
one your board uses is `MESH_PHONE` in `settings.toml`.

### Bluetooth (Base Duo)

Bluetooth is the default, so there is nothing to set. If you want to say it out
loud, or you changed it once:

```toml
MESH_PHONE = "ble"
```

Reset the board. In the Meshtastic app, tap the **+** on the connect screen and
pick your node from the Bluetooth list.

> **Leave `CIRCUITPY_BLE_WORKFLOW` off.** The board can only advertise one
> thing at a time, so turning it on means the node and the workflow are never
> both discoverable.

### WiFi (Meshnology W12)

The W12 joins your network instead. Edit `settings.toml`:

```toml
MESH_PHONE = "tcp"
CIRCUITPY_WIFI_SSID = "your network"
CIRCUITPY_WIFI_PASSWORD = "your password"
```

Reset the board. It prints its address, and shows it on the screen. In the
Meshtastic app, tap **+** and choose **Network** rather than Bluetooth, then
enter that address.

> **Leave `CIRCUITPY_WEB_API_PASSWORD` unset.** Setting it starts
> CircuitPython's own web server, which takes the port the app needs.

The board and the phone must be on the same network. If the access point is not
there, the node still comes up and works as a radio — it just says why there is
no phone link.

### What the app can and cannot do

The app gets the mesh, the messages, and the ability to send. It can rename the
node. It **cannot** change the node's radio settings — region, preset, channel,
power. Those are the board's own, and only the board's prompt changes them.

---

## Saving space on the drive

If the `CIRCUITPY` drive is too small for all of it, these can be left off.
Nothing crashes — the node notices the file is missing and does without.

| File | Leave it off and you lose |
|---|---|
| `oled.py` | The screen. W12 only. |
| `gps.py` | Position and time from GPS. Also unset `MESH_GPS` in `settings.toml`. W12 only. |
| `meshcache.mpy` | Known nodes and messages are forgotten on every reset, and rebuilt from the air. Set `MESH_CACHE = false` in `settings.toml` to say so on purpose. |
| `meshtext.mpy` | `listen()` prints raw bytes instead of readable packets. Nothing else changes. |
| `meshphone.mpy` | The phone app entirely. On the W12 also set `PHONE = False` in `code.py`; on the Base Duo, delete the `serve()` line. |
| `repl.py` | The commands at the prompt. The node still runs. |

These are **required**:

| File | Why |
|---|---|
| `__init__.mpy` | The core of the node. Nothing works without it. |
| `meshlib.mpy` | Everything else in the package. |
| `lr1121.mpy` / `lr2021.mpy` | Your radio. |
| `code.py` | What runs at boot. |
| `settings.toml` | Holds the few things that must be read before any code runs. |

Delete whole files, not parts of them.

---

## When something is wrong

**Nothing on the serial port.** The board waits a few seconds for you to attach
before it starts, then prints what reset it — a plug, a Ctrl-D, a crash. If you
see nothing at all, check the cable: some USB cables are charge-only.

**"no GNSS module".** `MESH_GPS` in `settings.toml` names a file that is not on
the drive. Either copy `gps.py` into `lib/meshtastic/`, or comment the setting
out.

**No packets, ever.** Check `REGION` in `code.py` matches the other nodes, and
that the antenna is attached. `status()` at the prompt says whether the radio is
receiving.

**"no phone link".** WiFi boards only — the network was not there. Check the
SSID and password in `settings.toml`. The node works as a radio regardless.

**`ImportError`.** Almost always a half-copied drive, or an old file left next
to a new one. Delete everything and copy the release again.

**The app connects but shows nothing.** Give it a moment on first connect: it
asks for the whole node list and every stored message before it draws anything.

---

## Legal

Transmitting on these bands is regulated, and the rules differ by country. Set
`REGION` correctly. Use an antenna. That is your responsibility, not the
software's.
