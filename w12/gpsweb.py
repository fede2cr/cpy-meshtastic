"""The GNSS readout over WiFi, because a serial cable does not reach the garden.

    import gpsweb
    gpsweb.serve()

It prints its address in a banner and then says nothing more, so the USB cable
can be pulled and the board carried outside on battery without a console to
block on. Point anything at the address:

    /        a page that refreshes itself, readable on a phone
    /json    the same numbers, for a script or a curl loop
    /nmea    the raw sentences, the equivalent of `gps.dump`

Credentials come from settings.toml, the same `CIRCUITPY_WIFI_SSID` and
`CIRCUITPY_WIFI_PASSWORD` the supervisor reads, so the board is usually already
on the network before this runs. `ap=True` makes the board its own network
instead, for a field with no WiFi -- but then only something joined to that
network can read it, which rules out anyone watching from elsewhere.

Nothing here asks who is connecting. It is an unauthenticated readout of where
this board is, so put it on a network you trust and stop it when you are done.
"""

import os
import time

import rtc
import socketpool
import wifi

from meshtastic import gps

try:
    import mdns
except ImportError:
    mdns = None

#: The supervisor's own web workflow would take this, but only when
#: `CIRCUITPY_WEB_API_PASSWORD` is set; leave that unset and port 80 is ours.
PORT = 80
HOSTNAME = "w12"
AP_SSID = "w12-gps"

#: WPA2 will not accept fewer than eight characters. Change it if the network
#: it makes will be up anywhere public.
AP_PASSWORD = "meshtastic"

#: Sentences kept for `/nmea`. About 70 bytes each.
KEEP = 24

#: How often to check the link is still up. esp-idf retries a disconnect a fixed
#: number of times and then stops for good, so carrying the board out of range
#: and back is not something it recovers from on its own.
LINK_CHECK_S = 5

#: How often to repaint the OLED. A frame is a kilobyte over the same 100 kHz
#: bus the panel shares with nothing, but the loop it steals from is serving.
PANEL_S = 2.0

#: Talker ids, for naming the constellations on the page.
TALKERS = {"GP": "GPS", "GL": "GLONASS", "GA": "Galileo", "GB": "BeiDou",
           "BD": "BeiDou", "GQ": "QZSS", "GI": "NavIC", "GN": "combined"}


def _oled_show(rows):
    """Draws on the panel if this drive has one. Never the reason serving stops."""
    try:
        from meshtastic import oled
    except ImportError:
        return
    try:
        oled.show(*rows)
    except Exception as error:
        print("# screen: %s" % (error,))


_sock = None
_ap = False
_started = 0

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>W12 GNSS</title><style>
body{background:#101214;color:#e6e6e6;font:16px/1.45 system-ui,sans-serif;
margin:0 auto;padding:1rem;max-width:34rem}
h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:#7b8a99;
margin:1.4rem 0 .4rem;font-weight:600}
#state{font:600 1.5rem/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;
margin:.1rem 0 .3rem}
#where{font:1.35rem/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;color:#fff;
word-break:break-all}
.row{display:flex;justify-content:space-between;gap:1rem;
border-bottom:1px solid #1e2226;padding:.4rem 0}
.k{color:#7b8a99}.v{font-family:ui-monospace,Menlo,monospace;text-align:right}
.ok{color:#4ade80}.soft{color:#fbbf24}.no{color:#f87171}
pre{background:#000;border:1px solid #1e2226;border-radius:4px;padding:.5rem;
overflow:auto;font-size:.72rem;line-height:1.35;max-height:38vh;color:#9fb}
a{color:#60a5fa}
</style></head><body>
<h2>Meshnology W12 &middot; L76K</h2>
<div id="state" class="no">connecting</div>
<div id="where"></div>
<div id="map"></div>
<h2>satellites</h2><div id="sats"></div>
<h2>receiver</h2><div id="misc"></div>
<h2>raw nmea</h2><pre id="nmea">...</pre>
<script>
var NAMES={GP:"GPS",GL:"GLONASS",GA:"Galileo",GB:"BeiDou",BD:"BeiDou",
QZ:"QZSS",GQ:"QZSS",GI:"NavIC",GN:"combined"};
function rows(el,pairs){
  el.innerHTML=pairs.map(function(p){
    return '<div class="row"><span class="k">'+p[0]+
           '</span><span class="v">'+p[1]+'</span></div>';}).join('');
}
function num(v,digits,unit){
  if(v===null||v===undefined)return '--';
  return (digits===null?v:Number(v).toFixed(digits))+(unit||'');
}
async function tick(){
  var state=document.getElementById('state');
  try{
    var r=await fetch('/json',{cache:'no-store'});
    var d=await r.json();
    if(d.valid){state.className='ok';state.textContent='FIX \\u2014 '+d.quality_name;}
    else if(d.latitude!==null){state.className='soft';
      state.textContent='no fix \\u2014 '+d.quality_name+' only';}
    else{state.className='no';state.textContent='searching';}
    document.getElementById('where').textContent =
      d.latitude===null?'no position':
      num(d.latitude,5)+', '+num(d.longitude,5);
    document.getElementById('map').innerHTML = d.latitude===null?'':
      '<a target="_blank" rel="noreferrer" href="https://www.openstreetmap.org/?mlat='+
      d.latitude+'&mlon='+d.longitude+'#map=17/'+d.latitude+'/'+d.longitude+
      '">open in map</a>';
    var sats=[['used in solution',num(d.satellites,null)],
              ['in view',num(d.in_view,null)],
              ['HDOP',num(d.hdop,1)]];
    for(var k in d.talkers){sats.push([NAMES[k]||k,d.talkers[k]+' in view']);}
    rows(document.getElementById('sats'),sats);
    rows(document.getElementById('misc'),[
      ['altitude',num(d.altitude_m,0,' m')],
      ['UTC from GPS',d.utc||'--'],
      ['GGA quality',d.quality+' ('+d.quality_name+')'],
      ['RMC status',(d.status||'--')+(d.mode?'/'+d.mode:'')],
      ['sentences',d.sentences+' good, '+d.unreadable+' bad'],
      ['server uptime',d.uptime_s+' s']]);
    var n=await fetch('/nmea',{cache:'no-store'});
    document.getElementById('nmea').textContent=await n.text();
  }catch(e){state.className='no';state.textContent='board unreachable';}
  setTimeout(tick,1000);
}
tick();
</script></body></html>"""


def connect(ssid=None, password=None, hostname=HOSTNAME, timeout=20):
    """Joins the network from settings.toml. Returns the address as text."""
    if ssid is None:
        ssid = os.getenv("CIRCUITPY_WIFI_SSID")
    if password is None:
        password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
    wifi.radio.enabled = True
    if wifi.radio.connected:
        # The supervisor connects at boot from those same two keys, so this is
        # usually already done and reconnecting would only drop the link.
        return str(wifi.radio.ipv4_address)
    if not ssid:
        raise ValueError("no CIRCUITPY_WIFI_SSID in settings.toml; "
                         "pass ssid= and password=, or ap=True")
    try:
        wifi.radio.hostname = hostname
    except (AttributeError, ValueError):
        pass
    wifi.radio.connect(ssid, password or "", timeout=timeout)
    return str(wifi.radio.ipv4_address)


def access_point(ssid=AP_SSID, password=AP_PASSWORD):
    """Makes the board its own network. Returns the address as text."""
    global _ap
    wifi.radio.enabled = True
    wifi.radio.start_ap(ssid, password)
    _ap = True
    return str(wifi.radio.ipv4_address_ap)


def _advertise(hostname, port):
    """Publishes `hostname.local` so the address need not be typed."""
    if mdns is None:
        return None
    try:
        server = mdns.Server(wifi.radio)
        server.hostname = hostname
        server.advertise_service(service_type="_http", protocol="_tcp",
                                 port=port)
        return hostname
    except (RuntimeError, OSError, ValueError):
        # Already claimed, usually by the supervisor's own workflow. The
        # numeric address in the banner works either way.
        return None


def _data(reader):
    fix = reader.fix
    return {
        "valid": fix.valid,
        "timed": fix.timed,
        "latitude": fix.latitude,
        "longitude": fix.longitude,
        "altitude_m": fix.altitude_m,
        "satellites": fix.satellites,
        "in_view": fix.in_view,
        "talkers": fix.talkers,
        "hdop": fix.hdop,
        "quality": fix.quality,
        "quality_name": gps.QUALITY.get(fix.quality, "?"),
        "status": fix.status,
        "mode": fix.mode,
        "nav": fix.nav,
        "utc": fix.utc,
        "epoch": fix.when,
        "sentences": reader.good,
        "unreadable": reader.bad,
        "uptime_s": int(time.monotonic() - _started),
    }


def _json(data):
    """Serialises the flat dict above. Every value is a number, a short ASCII
    string from the receiver, or a dict of those."""
    out = []
    for key in sorted(data):
        out.append('"%s":%s' % (key, _value(data[key])))
    return "{" + ",".join(out) + "}"


def _value(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, dict):
        return "{" + ",".join('"%s":%s' % (k, _value(v))
                              for k, v in value.items()) + "}"
    if isinstance(value, str):
        return '"%s"' % value.replace("\\", "").replace('"', "")
    return str(value)


def _send(conn, body, kind="text/html; charset=utf-8", status="200 OK"):
    if isinstance(body, str):
        body = body.encode("utf-8")
    head = ("HTTP/1.1 %s\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %d\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n" % (status, kind, len(body)))
    _send_all(conn, head.encode("ascii"))
    _send_all(conn, body)


def _send_all(conn, data):
    at = 0
    view = memoryview(data)
    while at < len(data):
        sent = conn.send(view[at:])
        if not sent:
            break
        at += sent


def _path(conn, buf):
    """The requested path, from the first line. None if nothing readable came."""
    conn.settimeout(2)
    try:
        count = conn.recv_into(buf)
    except OSError:
        return None
    if not count:
        return None
    try:
        line = str(buf[:count], "utf-8").split("\r\n", 1)[0]
    except (UnicodeError, ValueError):
        return None
    parts = line.split(" ")
    if len(parts) < 2:
        return None
    return parts[1].split("?", 1)[0]


def _handle(conn, reader, buf):
    path = _path(conn, buf)
    if path is None:
        return
    if path == "/json":
        _send(conn, _json(_data(reader)), "application/json")
    elif path == "/nmea":
        _send(conn, "\n".join(reader.recent) or "nothing yet",
              "text/plain; charset=utf-8")
    elif path == "/":
        _send(conn, PAGE)
    else:
        _send(conn, "no such path", "text/plain; charset=utf-8",
              "404 Not Found")


def _listen(port):
    """A bound, listening, non-blocking socket."""
    pool = socketpool.SocketPool(wifi.radio)
    sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
    sock.setsockopt(pool.SOL_SOCKET, pool.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError:
        sock.close()
        raise OSError(
            "port %d is taken -- unset CIRCUITPY_WEB_API_PASSWORD in "
            "settings.toml, or call serve(port=8080)" % port)
    sock.listen(2)
    sock.setblocking(False)
    return sock


def _relink(sock, port, ssid, password, hostname):
    """Rejoins the network and rebuilds the socket on it. None if it failed."""
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    try:
        connect(ssid, password, hostname, timeout=10)
        return _listen(port)
    except (OSError, ValueError, RuntimeError):
        return None


def serve(port=PORT, ap=False, ssid=None, password=None, keep=KEEP,
          set_clock=True, hostname=HOSTNAME):
    """Serves the GNSS over WiFi until interrupted. Prints where to find it.

    Blocks forever by design: the loop is what keeps reading the receiver, and
    a GNSS left unread loses sentences to the UART buffer.
    """
    global _sock, _started
    stop()
    _started = time.monotonic()
    reader = gps.Reader(keep=keep)
    reader.poll()

    address = access_point(ssid or AP_SSID, password or AP_PASSWORD) if ap \
        else connect(ssid, password, hostname)
    name = _advertise(hostname, port)

    sock = _listen(port)
    _sock = sock

    bar = "=" * 52
    print("\n" + bar)
    if ap:
        print("  network:  %s  (password %s)" % (ssid or AP_SSID,
                                                 password or AP_PASSWORD))
    print("  GNSS web server:  http://%s%s"
          % (address, "" if port == 80 else ":%d" % port))
    if name:
        print("                    http://%s.local%s"
              % (name, "" if port == 80 else ":%d" % port))
    print("  /json for data, /nmea for raw sentences")
    print("  Safe to unplug now; it keeps serving on battery.")
    print(bar + "\n")

    # Unplugged and on battery there is no console, and the address is the one
    # thing a reader needs before it can ask for anything else.
    where = "%s%s" % (address, "" if port == 80 else ":%d" % port)
    _oled_show(("GNSS web server", where,
                "%s.local" % name if name else "",
                "/json  /nmea", "starting"))

    buf = bytearray(512)
    clock_set = False
    link_at = time.monotonic() + LINK_CHECK_S
    panel_at = time.monotonic() + PANEL_S
    try:
        while True:
            reader.poll()
            if time.monotonic() >= panel_at:
                panel_at = time.monotonic() + PANEL_S
                _oled_show((where,) + reader.fix.rows()[1:])
            if set_clock and not clock_set and reader.fix.timed:
                rtc.RTC().datetime = time.localtime(reader.fix.when)
                clock_set = True
            if not ap and time.monotonic() >= link_at:
                link_at = time.monotonic() + LINK_CHECK_S
                if sock is None or not wifi.radio.connected:
                    sock = _relink(sock, port, ssid, password, hostname)
                    _sock = sock
            if sock is None:
                time.sleep(0.1)
                continue
            try:
                conn, _addr = sock.accept()
            except OSError:
                # Nothing waiting. The GNSS still is, so this is the whole
                # reason the socket is non-blocking.
                time.sleep(0.02)
                continue
            try:
                _handle(conn, reader, buf)
            except OSError:
                pass
            finally:
                conn.close()
    except KeyboardInterrupt:
        print("# stopped")
        _oled_show((where, "", "server stopped", "", ""))
        return reader.fix
    finally:
        stop()


def stop():
    """Closes the socket, and the network if this made one."""
    global _sock, _ap
    if _sock is not None:
        try:
            _sock.close()
        except OSError:
            pass
        _sock = None
    if _ap:
        wifi.radio.stop_ap()
        _ap = False
