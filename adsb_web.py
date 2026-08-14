"""Read-only web dashboard for the live ADS-B picture.

A single page plus three JSON endpoints, served from the poller's in-memory
track store by the standard library's HTTP server. No new dependencies: this
project runs on requests and stdlib, and adding a web framework to draw one map
would be out of proportion.

Security posture, because this is the one component that listens on a socket:

  * GET and HEAD only. There is no route that changes anything.
  * No filesystem serving at all — the page is a module-level string, so there
    is no path-traversal surface to get wrong.
  * Unknown paths 404 rather than falling through to anything.
  * Optional shared-secret token via ADSB_WEB_TOKEN.
  * Bound to 127.0.0.1 by default.

On ADSB_WEB_BIND: setting it to 0.0.0.0 makes the page reachable over
Tailscale, which is the usual reason to want it — but it also exposes it to
every other interface on the host. Binding to the machine's own 100.x.y.z
Tailscale address, or leaving it on loopback behind `tailscale serve`, gets the
same reachability without the extra exposure.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gold Coast air picture</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#10141a; color:#e6edf3; }
  #map { position:absolute; inset:0 340px 0 0; background:#0b0e13; }
  #side { position:absolute; top:0; right:0; bottom:0; width:340px; overflow-y:auto;
          border-left:1px solid #232a34; padding:14px; }
  h1 { font-size:15px; margin:0 0 2px; letter-spacing:.2px; }
  .sub { color:#8b98a8; font-size:12px; margin-bottom:14px; }
  h2 { font-size:11px; text-transform:uppercase; letter-spacing:.08em;
       color:#8b98a8; margin:18px 0 6px; }
  .ac { border:1px solid #232a34; border-left-width:3px; border-radius:6px;
        padding:7px 9px; margin-bottom:6px; background:#161b23; }
  .ac.mil { border-left-color:#e67e22; }
  .ac.prob { border-left-color:#8b98a8; }
  .ac.box { border-left-color:#e74c3c; background:#1d1618; }
  .ac .cs { font-weight:600; }
  .ac .meta { color:#8b98a8; font-size:12px; }
  .ac .why { color:#6f7d8d; font-size:11px; margin-top:3px; }
  .empty { color:#6f7d8d; font-style:italic; font-size:13px; }
  a { color:#58a6ff; }
  footer { margin-top:22px; padding-top:12px; border-top:1px solid #232a34;
           color:#6f7d8d; font-size:11px; }
  .leaflet-container { background:#0b0e13; }
  .plane { font-size:19px; line-height:19px; text-shadow:0 0 4px #000; }
  @media (max-width: 720px) {
    #map { inset:0 0 45% 0; } #side { top:55%; width:100%; border-left:none;
    border-top:1px solid #232a34; }
  }
</style>
</head>
<body>
<div id="map"></div>
<div id="side">
  <h1>Gold Coast air picture</h1>
  <div class="sub" id="status">connecting…</div>
  <h2>Military &amp; display</h2><div id="mil"></div>
  <h2>Display box</h2><div id="box"></div>
  <h2>All airborne</h2><div id="all"></div>
  <footer>
    Data from <a href="https://adsb.fi" target="_blank" rel="noopener">adsb.fi</a>,
    used under their personal non-commercial terms.<br>
    Aircraft detail on <a href="https://globe.adsbexchange.com/" target="_blank"
    rel="noopener">globe.adsbexchange.com</a>.
  </footer>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const q = p => p + (TOKEN ? '?token=' + encodeURIComponent(TOKEN) : '');

const map = L.map('map').setView([__LAT__, __LON__], 10);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap, &copy; CARTO', maxZoom: 18
}).addTo(map);

// The airshow display box.
const BOX = __BOX__;
L.polygon(BOX, {color:'#e74c3c', weight:1.5, fillOpacity:0.07,
  dashArray:'5,5'}).addTo(map).bindTooltip('Airshow display box');
L.circleMarker([__LAT__, __LON__], {radius:5, color:'#58a6ff', fillOpacity:1})
  .addTo(map).bindTooltip('__ICAO__');

// Aircraft silhouettes, so a C-17 looks like a C-17. Shapes come from
// tar1090 (the same set globe.adsbexchange.com draws) and are fetched once.
// The rendering below mirrors tar1090's own svgShapeToSVG: stroke width is
// scaled per shape, paths are drawn with paint-order:stroke so the outline
// sits behind the fill, and accent paths are drawn thinner on top.
let MARKERS = null;
fetch(q('/api/markers') + (q('/api/markers').includes('?') ? '&' : '?') + 'v=__MARKERV__')
  .then(r => r.json()).then(m => { MARKERS = m; refreshIcons(); })
  .catch(() => {});

function shapeFor(a) {
  if (!MARKERS) return null;
  let entry = (a.type && MARKERS.typeDesignators[a.type])
           || (a.category && MARKERS.categories[a.category])
           || MARKERS.default;
  const shape = MARKERS.shapes[entry[0]];
  return shape ? {shape, scale: entry[1] || 1} : null;
}
function svgIcon(a, colour) {
  const s = shapeFor(a);
  if (!s) return null;
  const sh = s.shape, scale = s.scale;
  if (sh.svg) return null;                 // a few shapes ship raw markup; skip those
  const sw = 1.2 * (sh.strokeScale || 1);
  const w = sh.w * scale, h = sh.h * scale;
  let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${sh.viewBox}" `
          + `width="${w}" height="${h}">`
          + (sh.transform ? `<g transform="${sh.transform}">` : '<g>');
  for (const d of [].concat(sh.path || []))
    svg += `<path paint-order="stroke" fill="${colour}" stroke="#0b0e13" `
         + `stroke-width="${2 * sw}" d="${d}"/>`;
  for (const d of [].concat(sh.accent || []))
    svg += `<path fill="none" stroke="#0b0e13" stroke-opacity="0.55" `
         + `stroke-width="${0.6 * (sh.accentMult || 1) * sw}" d="${d}"/>`;
  return {html: svg + '</g></svg>', w, h, noRotate: !!sh.noRotate};
}

let markers = {};
function colourFor(a) {
  return a.in_box ? '#e74c3c' : (a.military ? '#e67e22'
       : (a.probable ? '#c9d1d9' : '#4d94d6'));
}
function icon(a) {
  const colour = colourFor(a);
  const rot = a.track_deg || 0;
  const built = svgIcon(a, colour);
  if (built) {
    // Rotate a wrapper rather than the SVG so the icon anchor stays centred.
    const box = Math.ceil(Math.max(built.w, built.h));
    return L.divIcon({className:'', iconSize:[box, box], iconAnchor:[box/2, box/2],
      html:`<div style="width:${box}px;height:${box}px;display:flex;`
         + `align-items:center;justify-content:center;`
         + `transform:rotate(${built.noRotate ? 0 : rot}deg)">${built.html}</div>`});
  }
  return L.divIcon({className:'', iconSize:[19,19], iconAnchor:[9,9],
    html:`<div class="plane" style="color:${colour};transform:rotate(${rot}deg)">&#9992;</div>`});
}
// Shapes arrive after the first poll, so redraw once they land.
let lastAircraft = [];
function refreshIcons() {
  for (const a of lastAircraft) if (markers[a.hex]) markers[a.hex].setIcon(icon(a));
}
function label(a) {
  const alt = a.on_ground ? 'on the ground'
            : (a.alt_ft != null ? a.alt_ft.toLocaleString() + ' ft' : '? ft');
  return `<b>${a.ident || a.reg || a.hex.toUpperCase()}</b><br>`
       + `${a.desc || a.type || 'unidentified'}<br>${alt}`
       + (a.gs_kt ? ` &middot; ${Math.round(a.gs_kt)} kt` : '')
       + (a.reasons ? `<br><i>${a.reasons}</i>` : '')
       + `<br><a href="${a.url}" target="_blank" rel="noopener">globe.adsbexchange.com</a>`;
}
function card(a) {
  const cls = a.in_box ? 'box' : (a.military ? 'mil' : (a.probable ? 'prob' : ''));
  const alt = a.on_ground ? 'ground'
            : (a.alt_ft != null ? a.alt_ft.toLocaleString() + ' ft' : '?');
  const dist = a.dist_nm != null ? `${a.dist_nm.toFixed(0)} nm` : '';
  return `<div class="ac ${cls}"><span class="cs">${a.ident || a.reg || a.hex.toUpperCase()}</span>
    <span class="meta"> ${a.type || '?'} &middot; ${alt}
    ${a.gs_kt ? '&middot; ' + Math.round(a.gs_kt) + ' kt' : ''} ${dist ? '&middot; ' + dist : ''}</span>
    ${a.reasons ? `<div class="why">${a.reasons}</div>` : ''}</div>`;
}
function fill(id, rows, empty) {
  document.getElementById(id).innerHTML = rows.length
    ? rows.map(card).join('') : `<div class="empty">${empty}</div>`;
}
async function tick() {
  try {
    const r = await fetch(q('/api/aircraft'));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    lastAircraft = d.all;
    const seen = new Set();
    for (const a of d.all) {
      if (a.lat == null) continue;
      seen.add(a.hex);
      if (markers[a.hex]) {
        markers[a.hex].setLatLng([a.lat, a.lon]).setIcon(icon(a))
          .setPopupContent(label(a));
      } else {
        markers[a.hex] = L.marker([a.lat, a.lon], {icon: icon(a)})
          .addTo(map).bindPopup(label(a));
      }
    }
    for (const hex of Object.keys(markers)) {
      if (!seen.has(hex)) { map.removeLayer(markers[hex]); delete markers[hex]; }
    }
    fill('mil', d.military, 'Nothing military or unusual in range.');
    fill('box', d.box, 'The display box is empty.');
    fill('all', d.all, 'Nothing airborne in range.');
    document.getElementById('status').textContent =
      `${d.tracks} aircraft in range · ${d.military.length} military · `
      + `${d.box.length} in the box · ${d.source} · `
      + new Date().toLocaleTimeString('en-AU');
  } catch (e) {
    document.getElementById('status').textContent = 'lost contact with the tracker: ' + e.message;
  }
}
tick(); setInterval(tick, 5000);
</script>
</body>
</html>
"""


def _render_page() -> bytes:
    if config.ADSB_BOX_POLY:
        poly = list(config.ADSB_BOX_POLY)
    else:
        lat_min, lat_max, lon_min, lon_max = config.ADSB_BOX
        poly = [
            (lat_min, lon_min), (lat_min, lon_max),
            (lat_max, lon_max), (lat_max, lon_min),
        ]
    return (
        _PAGE.replace("__LAT__", f"{config.ADSB_HOME_LAT}")
        .replace("__LON__", f"{config.ADSB_HOME_LON}")
        .replace("__ICAO__", config.ADSB_HOME_ICAO)
        .replace("__BOX__", json.dumps([[a, b] for a, b in poly]))
        .replace("__MARKERV__", _marker_version())
    ).encode("utf-8")


_marker_cache: dict = {}


def _marker_shapes() -> bytes:
    """The aircraft silhouette table, read once and held.

    Shapes are tar1090's (GPL-2.0-or-later) so the map draws the same
    silhouettes as globe.adsbexchange.com — see the notice inside the file.
    A missing file is not fatal: the page falls back to a generic glyph.
    """
    if "body" not in _marker_cache:
        try:
            with open(config.ADSB_MARKER_SHAPES_FILE, "rb") as fh:
                _marker_cache["body"] = fh.read()
        except OSError:
            _marker_cache["body"] = (
                b'{"shapes":{},"typeDesignators":{},"categories":{},"default":["unknown",1]}'
            )
    return _marker_cache["body"]


def _marker_version() -> str:
    """Short fingerprint of the shape table, used to bust the browser cache.

    The table is ~110 KB and never changes while the process runs, so it is
    worth caching hard in the browser. But editing the file and getting the old
    copy back for the next 24 hours is a genuinely confusing failure, so the
    page requests it with ?v=<fingerprint> and a changed file changes the URL.
    """
    if "version" not in _marker_cache:
        _marker_cache["version"] = hashlib.sha1(_marker_shapes()).hexdigest()[:12]
    return _marker_cache["version"]


class _Handler(BaseHTTPRequestHandler):
    poller = None
    server_version = "atc-tracker"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        pass  # the TUI owns the terminal; access logs would trash it

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _authorised(self, query) -> bool:
        if not config.ADSB_WEB_TOKEN:
            return True
        return (query.get("token") or [""])[0] == config.ADSB_WEB_TOKEN

    # -- routes -----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            self._send(403, b"Forbidden\n", "text/plain; charset=utf-8")
            return

        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._send(200, _render_page(), "text/html; charset=utf-8")
        elif path == "/api/aircraft":
            self._json(self._board())
        elif path == "/api/markers":
            body = _marker_shapes()
            # Static for the process lifetime and ~100 KB, so let the browser
            # keep it rather than re-sending it on every reload.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        elif path == "/api/events":
            self._json(self._events())
        elif path == "/api/health":
            self._json(self.poller.health() if self.poller else {"error": "not running"})
        else:
            self._send(404, b"Not found\n", "text/plain; charset=utf-8")

    # Anything that could change state is simply not implemented. Spelling
    # that out beats relying on BaseHTTPRequestHandler's default 501.
    def _reject(self):
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _reject

    # -- data -------------------------------------------------------------

    def _board(self) -> dict:
        if self.poller is None:
            return {"all": [], "military": [], "box": [], "tracks": 0, "source": "-"}
        board = self.poller.snapshot_board()
        return {
            "at": board["at"],
            "all": board["all"],
            "military": board["military"],
            "box": board["box"],
            "tracks": board["tracks"],
            "source": board["source"],
        }

    def _events(self) -> list:
        if self.poller is None or self.poller.store is None:
            return []
        return self.poller.store.recent_events(50)


def serve(poller, stop_event=None, on_log=None) -> None:
    """Run the dashboard until stop_event is set. Intended as a thread body."""
    handler = type("_BoundHandler", (_Handler,), {"poller": poller})
    try:
        httpd = ThreadingHTTPServer((config.ADSB_WEB_BIND, config.ADSB_WEB_PORT), handler)
    except OSError as exc:
        if on_log:
            on_log(f"ADS-B map could not start on port {config.ADSB_WEB_PORT}: {exc}", True)
        return
    httpd.daemon_threads = True

    where = config.ADSB_WEB_BIND
    if on_log:
        note = "" if config.ADSB_WEB_TOKEN else " (no token set)"
        on_log(f"ADS-B map on http://{where}:{config.ADSB_WEB_PORT}/{note}")

    if stop_event is not None:
        threading.Thread(
            target=lambda: (stop_event.wait(), httpd.shutdown()),
            daemon=True,
            name="adsb-web-stop",
        ).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
