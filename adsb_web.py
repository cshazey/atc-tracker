"""Read-only web dashboard for the live ADS-B picture.

A single page plus a handful of JSON endpoints, served from the poller's
in-memory track store by the standard library's HTTP server. No new dependencies: this
project runs on requests and stdlib, and adding a web framework to draw one map
would be out of proportion.

Security posture, because this is the one component that listens on a socket:

  * GET and HEAD only. There is no route that changes anything.
  * No filesystem serving at all — the page is a module-level string, so there
    is no path-traversal surface to get wrong.
  * Unknown paths 404 rather than falling through to anything.
  * Optional shared-secret token via ADSB_WEB_TOKEN.
  * Bound to 127.0.0.1 by default.

ADSB_WEB_BIND takes a literal address, or the word "tailscale". The latter is
the setting worth using: it listens on this host's 100.x Tailnet address AND on
loopback, and on nothing else — reachable from your other devices, invisible to
whatever network the machine happens to be plugged into. 0.0.0.0 still works if
you want it, with a warning at startup.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
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
  .ac.spec { border-left-color:#3498db; }
  .ac.watch { border-left-color:#ff2d95; }
  .ac.emg { border-left-color:#ff3b30; background:#1d1618; }
  .ac.zone { background:#1d1618; }
  .ac { cursor:pointer; }
  .ac .cs { font-weight:600; }
  .ac .meta { color:#8b98a8; font-size:12px; }
  .ac .why { color:#6f7d8d; font-size:11px; margin-top:3px; }
  .ac .heard { color:#9fd0ff; font-size:11px; margin-top:3px; }
  .zn { border:1px solid #232a34; border-radius:6px; padding:6px 9px; margin-bottom:6px;
        background:#141a22; border-left:3px solid var(--c); }
  .zn .nm { font-weight:600; } .zn .al { color:#6f7d8d; font-size:11px; float:right; }
  .zn .oc { font-size:12px; color:#dbe5ef; margin-top:3px; }
  .zn .in { font-size:12px; color:#f1c40f; }
  .zn .clr { font-size:12px; color:#6f7d8d; font-style:italic; }
  .tag { display:inline-block; font-size:10px; padding:0 4px; border-radius:3px;
         background:#2a3340; color:#cdd9e5; margin-left:3px; }
  .chip { display:inline-block; font-size:11px; padding:0 5px; border-radius:3px;
          background:#1f3a55; color:#9fd0ff; margin:2px 3px 0 0; cursor:default; }
  .chip.ln { background:#3a2a55; color:#d5b8ff; cursor:pointer; }
  .empty { color:#6f7d8d; font-style:italic; font-size:13px; }
  a { color:#58a6ff; }
  footer { margin-top:22px; padding-top:12px; border-top:1px solid #232a34;
           color:#6f7d8d; font-size:11px; }
  .leaflet-container { background:#0b0e13; }
  .plane { font-size:19px; line-height:19px; text-shadow:0 0 4px #000; }
  .note { border-left:3px solid #4d94d6; background:#141a22; border-radius:5px;
          padding:5px 8px; margin-bottom:5px; font-size:12px; }
  .note.int { border-left-color:#e67e22; }
  .note.emg { border-left-color:#e74c3c; background:#1d1618; }
  .note .st { font-weight:600; color:#9fb3c8; }
  .note .tm { color:#6f7d8d; float:right; font-size:11px; }
  .note .mil { color:#e67e22; font-size:11px; margin-left:4px; }
  .note .tx { display:block; margin-top:2px; color:#dbe5ef; }
  .ev { font-size:12px; color:#aab7c6; padding:3px 1px; border-bottom:1px solid #1b212b; }
  .ev .tm { color:#6f7d8d; }
  .ev b { color:#dbe5ef; }
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
  <h2>Airspace watch</h2><div id="zones"><div class="empty">Loading zones…</div></div>
  <h2>Watch feed</h2><div id="watch"><div class="empty">Nothing flagged yet.</div></div>
  <h2>Latest ATC</h2><div id="notes"><div class="empty">Listening…</div></div>
  <h2>Military &amp; notable</h2><div id="mil"></div>
  <h2>All airborne</h2><div id="all"></div>
  <h2>Recent activity</h2><div id="events"><div class="empty">Nothing yet.</div></div>
  <footer>
    Zone shapes are approximate and not for navigation.<br>
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

// Watched airspace zones, redrawn when one is toggled on or off.
const zoneLayer = L.layerGroup().addTo(map);
let zoneSig = '';
async function loadZones() {
  try {
    const r = await fetch(q('/api/zones'));
    if (!r.ok) return;
    const zones = await r.json();
    const sig = JSON.stringify(zones.map(z => [z.id, z.enabled]));
    if (sig === zoneSig) return;
    zoneSig = sig;
    zoneLayer.clearLayers();
    for (const z of zones) {
      if (!z.enabled) continue;
      L.polygon(z.outline, {color: z.color, weight: 1.5, fillOpacity: 0.06,
        dashArray: z.kind === 'ring' ? '2,6' : '5,5'})
        .addTo(zoneLayer).bindTooltip(`${esc(z.name)} · ${esc(z.alt)}`);
    }
  } catch (e) {}
}
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
let trails = {};
let vectors = {};
const ROLE_COLOUR = {emergency:'#ff3b30', watch:'#ff2d95', military:'#e67e22',
  special:'#3498db', probable:'#c9d1d9', notable:'#b48ead', anon_fast:'#f1c40f'};
const ROLE_ORDER = ['emergency','watch','military','special','probable','notable','anon_fast'];
function topRole(a) {
  const roles = a.roles || [];
  return ROLE_ORDER.find(r => roles.includes(r)) || '';
}
function colourFor(a) {
  return ROLE_COLOUR[topRole(a)] || '#4d94d6';
}
function trailStyle(a) {
  const flagged = !!topRole(a);
  return {color: colourFor(a),
          weight: flagged ? (a.in_zone ? 3 : 2.5) : 1.5,
          opacity: flagged ? (a.in_zone ? 0.9 : 0.6) : 0.3};
}
// Dead-reckoned 3-minute vector for flagged aircraft — the same straight-line
// projection the tracker uses for its predicted-entry alerts.
function vectorFor(a) {
  if (!topRole(a) || !a.gs_kt || a.gs_kt < 60 || a.track_deg == null || a.on_ground) return null;
  const d = a.gs_kt * 3 / 60, t = a.track_deg * Math.PI / 180;
  return [[a.lat, a.lon], [a.lat + d * Math.cos(t) / 60,
          a.lon + d * Math.sin(t) / (60 * Math.cos(a.lat * Math.PI / 180))]];
}
function esc(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"]/g,
    m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
}
function fmtTime(epoch) {
  try { return new Date(epoch * 1000)
    .toLocaleTimeString('en-AU', {hour:'2-digit', minute:'2-digit'}); }
  catch (e) { return ''; }
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
function name(a) { return a.ident || a.reg || a.hex.toUpperCase(); }
function label(a) {
  const alt = a.on_ground ? 'on the ground'
            : (a.alt_ft != null ? a.alt_ft.toLocaleString() + ' ft' : '? ft');
  const h = a.heard;
  return `<b>${esc(name(a))}</b><br>`
       + `${esc(a.desc || a.type || 'unidentified')}<br>${alt}`
       + (a.gs_kt ? ` &middot; ${Math.round(a.gs_kt)} kt` : '')
       + ((a.zone_names || []).length ? `<br>🛡 In ${esc(a.zone_names.join(', '))}` : '')
       + ((a.roles || []).length ? `<br>${esc(a.roles.join(' · '))}` : '')
       + (a.reasons ? `<br><i>${esc(a.reasons)}</i>` : '')
       + (h ? `<br>📻 ${esc(h.icao)} ${fmtTime(h.at)}: “${esc(h.text)}”` : '')
       + `<br><a href="${esc(a.url)}" target="_blank" rel="noopener">globe.adsbexchange.com</a>`;
}
function card(a) {
  const role = topRole(a);
  const cls = {emergency:'emg', watch:'watch', military:'mil', special:'spec',
               probable:'prob'}[role] || '';
  const alt = a.on_ground ? 'ground'
            : (a.alt_ft != null ? a.alt_ft.toLocaleString() + ' ft' : '?');
  const dist = a.dist_nm != null ? `${a.dist_nm.toFixed(0)} nm` : '';
  const zones = (a.zone_names || []).map(z => `<span class="tag">${esc(z)}</span>`).join('');
  const h = a.heard;
  return `<div class="ac ${cls} ${a.in_zone && role ? 'zone' : ''}" data-hex="${esc(a.hex)}">`
    + `<span class="cs">${esc(name(a))}</span>${zones}
    <span class="meta"> ${esc(a.type || '?')} &middot; ${alt}
    ${a.gs_kt ? '&middot; ' + Math.round(a.gs_kt) + ' kt' : ''} ${dist ? '&middot; ' + dist : ''}</span>
    ${a.reasons ? `<div class="why">${esc(a.reasons)}</div>` : ''}
    ${h ? `<div class="heard">📻 ${esc(h.icao)} ${fmtTime(h.at)}: ${esc(h.text)}</div>` : ''}</div>`;
}
function focus(hex) {
  const m = markers[hex];
  if (!m) return;
  map.setView(m.getLatLng(), Math.max(map.getZoom(), 11));
  m.openPopup();
}
document.addEventListener('click', e => {
  const el = e.target.closest('[data-hex]');
  if (el && el.dataset.hex) focus(el.dataset.hex);
});
function fill(id, rows, empty) {
  document.getElementById(id).innerHTML = rows.length
    ? rows.map(card).join('') : `<div class="empty">${empty}</div>`;
}
function fillZones(zones) {
  const el = document.getElementById('zones');
  if (!zones.length) { el.innerHTML = '<div class="empty">No zones enabled.</div>'; return; }
  el.innerHTML = zones.map(z => {
    const occ = z.occupants.map(a =>
      `<span class="chip ln" data-hex="${esc(a.hex)}">${esc(name(a))}`
      + ` <small>${esc(topRole(a))}</small></span>`).join('');
    const inc = z.incoming.map(i =>
      `<div class="in" data-hex="${esc(i.hex)}">↘ ${esc(i.label)} inbound ~`
      + `${Math.max(1, Math.round((i.eta_sec || 0) / 60))} min</div>`).join('');
    return `<div class="zn" style="--c:${esc(z.color)}"><span class="al">${esc(z.alt)}`
      + ` · ${z.count} ac</span><span class="nm">${esc(z.name)}</span>`
      + (occ || inc ? `<div class="oc">${occ}</div>${inc}` : '<div class="clr">clear</div>')
      + `</div>`;
  }).join('');
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
      // Breadcrumb flight path. Drawn under the markers so the aircraft glyph
      // always sits on top of its own trail.
      const tr = a.trail || [];
      if (tr.length >= 2) {
        if (trails[a.hex]) trails[a.hex].setLatLngs(tr).setStyle(trailStyle(a));
        else trails[a.hex] = L.polyline(tr, trailStyle(a)).addTo(map);
      } else if (trails[a.hex]) {
        map.removeLayer(trails[a.hex]); delete trails[a.hex];
      }
      const vec = vectorFor(a);
      if (vec) {
        const st = {color: colourFor(a), weight: 1.5, opacity: 0.8, dashArray: '3,5'};
        if (vectors[a.hex]) vectors[a.hex].setLatLngs(vec).setStyle(st);
        else vectors[a.hex] = L.polyline(vec, st).addTo(map);
      } else if (vectors[a.hex]) {
        map.removeLayer(vectors[a.hex]); delete vectors[a.hex];
      }
    }
    for (const hex of Object.keys(markers)) {
      if (!seen.has(hex)) {
        map.removeLayer(markers[hex]); delete markers[hex];
        if (trails[hex]) { map.removeLayer(trails[hex]); delete trails[hex]; }
        if (vectors[hex]) { map.removeLayer(vectors[hex]); delete vectors[hex]; }
      }
    }
    fill('mil', d.military, 'Nothing military or unusual in range.');
    fillZones(d.zones || []);
    fill('all', d.all, 'Nothing airborne in range.');
    document.getElementById('status').textContent =
      `${d.tracks} aircraft in range · ${d.military.length} military · `
      + `${(d.zoned || []).length} flagged in zones · ${d.source} · `
      + new Date().toLocaleTimeString('en-AU');
  } catch (e) {
    document.getElementById('status').textContent = 'lost contact with the tracker: ' + e.message;
  }
}
const EV_ICON = {appeared:'📡', disappeared:'🔇', zone_enter:'🎯', zone_exit:'↗️',
  zone_predict:'⏱️', departure:'🛫', inbound:'🛬', landed:'🛬', emergency:'🚨',
  position:'🎯', radio:'📻'};

async function tickNotes() {
  try {
    const r = await fetch(q('/api/notes'));
    if (!r.ok) return;
    const notes = await r.json();
    const el = document.getElementById('notes');
    if (!notes.length) { el.innerHTML = '<div class="empty">No radio calls yet.</div>'; return; }
    el.innerHTML = notes.map(n => {
      const cls = n.tier >= 2 ? 'emg' : (n.tier >= 1 ? 'int' : '');
      const mil = (n.military && n.military.length)
        ? `<span class="mil">🎖 ${esc(n.military.join(', '))}</span>` : '';
      const chips = (n.callsigns || []).map(c => c.hex
        ? `<span class="chip ln" data-hex="${esc(c.hex)}" title="show on map">${esc(c.callsign)} → ✈</span>`
        : `<span class="chip">${esc(c.callsign)}</span>`).join('');
      return `<div class="note ${cls}"><span class="tm">${fmtTime(n.at)}</span>`
        + `<span class="st">${esc(n.icao)}</span>${mil}`
        + `<span class="tx">${esc(n.text)}</span>${chips}</div>`;
    }).join('');
  } catch (e) {}
}

async function tickWatch() {
  try {
    const r = await fetch(q('/api/watch'));
    if (!r.ok) return;
    const items = await r.json();
    const el = document.getElementById('watch');
    if (!items.length) { el.innerHTML = '<div class="empty">Nothing flagged yet.</div>'; return; }
    el.innerHTML = items.slice(0, 20).map(e =>
      `<div class="ev" ${e.hex ? `data-hex="${esc(e.hex)}" style="cursor:pointer"` : ''}>`
      + `<span class="tm">${fmtTime(e.at)}</span> ${EV_ICON[e.kind] || '✈️'} `
      + `<b>${esc(e.title)}</b> ${esc(e.detail)}</div>`
    ).join('');
  } catch (e) {}
}

async function tickEvents() {
  try {
    const r = await fetch(q('/api/events'));
    if (!r.ok) return;
    const evs = await r.json();
    const el = document.getElementById('events');
    if (!evs.length) { el.innerHTML = '<div class="empty">Nothing yet.</div>'; return; }
    el.innerHTML = evs.slice(0, 15).map(e =>
      `<div class="ev"><span class="tm">${fmtTime(e.at)}</span> ${EV_ICON[e.kind] || '✈️'} `
      + `<b>${esc(e.ident || e.hex.toUpperCase())}</b> ${esc(e.detail || e.kind)}</div>`
    ).join('');
  } catch (e) {}
}

tick(); setInterval(tick, 5000);
loadZones(); setInterval(loadZones, 60000);
tickWatch(); setInterval(tickWatch, 8000);
tickNotes(); setInterval(tickNotes, 7000);
tickEvents(); setInterval(tickEvents, 9000);
</script>
</body>
</html>
"""


def _render_page() -> bytes:
    return (
        _PAGE.replace("__LAT__", f"{config.ADSB_HOME_LAT}")
        .replace("__LON__", f"{config.ADSB_HOME_LON}")
        .replace("__ICAO__", config.ADSB_HOME_ICAO)
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
    notes_provider = None
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
        elif path == "/api/zones":
            self._json(self._zones())
        elif path == "/api/watch":
            self._json(self._watch())
        elif path == "/api/notes":
            self._json(self._notes())
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
            return {"all": [], "military": [], "zones": [], "zoned": [], "tracks": 0, "source": "-"}
        board = self.poller.snapshot_board()
        return {
            "at": board["at"],
            "all": board["all"],
            "military": board["military"],
            "zones": board["zones"],
            "zoned": board["zoned"],
            "tracks": board["tracks"],
            "source": board["source"],
        }

    def _zones(self) -> list:
        if self.poller is None:
            return []
        zs = self.poller.zones
        return [dict(z.as_dict(), enabled=zs.is_enabled(z)) for z in zs.all()]

    def _watch(self) -> list:
        """Zone events, emergencies and flagged radio callsigns, newest first."""
        if self.poller is None or self.poller.store is None:
            return []
        store = self.poller.store
        names = self.poller.zone_names()
        items = [
            {
                "at": e["at"], "kind": e["kind"], "hex": e["hex"],
                "title": f"{e['ident'] or e['hex'].upper()}"
                + (f" · {names.get(e['zone'], e['zone'])}" if e.get("zone") else ""),
                "detail": e["detail"],
            }
            for e in store.recent_events(80)
            if e["kind"] in ("zone_enter", "zone_exit", "zone_predict", "emergency")
        ]
        items += [
            {
                "at": m["at"], "kind": "radio", "hex": m["hex"],
                "title": f"{m['callsign']} on {m['icao']}"
                + (f" → {m['ident']}" if m["ident"] else ""),
                "detail": m["text"][:160],
            }
            for m in store.recent_mentions(80)
            if m["flagged"]
        ]
        items.sort(key=lambda x: -x["at"])
        return items[:40]

    def _events(self) -> list:
        if self.poller is None or self.poller.store is None:
            return []
        return self.poller.store.recent_events(50)

    def _notes(self) -> list:
        """Recent ATC transcripts, so the map sits beside the voice picture."""
        if self.notes_provider is None:
            return []
        try:
            return self.notes_provider(config.ADSB_WEB_NOTES)
        except Exception:
            return []


def tailscale_ip() -> str:
    """This host's Tailscale address, or "" if it has none.

    Tailscale hands out addresses from the 100.64.0.0/10 CGNAT range, so
    scanning the interface list is more reliable than shelling out to the
    `tailscale` binary — which lives in three different places on macOS
    depending on whether it came from the App Store, Homebrew, or the
    standalone package.
    """
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return ""
    for m in re.finditer(r"inet (100\.(\d+)\.\d+\.\d+)", out):
        second = int(m.group(2))
        if 64 <= second <= 127:          # 100.64/10, not 100.0/8 generally
            return m.group(1)
    return ""


def resolve_bind() -> tuple:
    """Turn ADSB_WEB_BIND into (address, note).

    Supports the literal "tailscale", which resolves to this host's Tailscale
    address. That is the setting worth wanting: reachable from your other
    devices, invisible to the coffee-shop wifi. See bind_addresses(), which
    pairs it with loopback. Falls back to loopback rather than to 0.0.0.0 if
    Tailscale is not up, because silently binding to every interface is not a
    reasonable thing to do on someone's behalf.
    """
    want = (config.ADSB_WEB_BIND or "").strip()
    if want.lower() not in ("tailscale", "ts"):
        return want or "127.0.0.1", ""
    ip = tailscale_ip()
    if ip:
        return ip, "reachable on your Tailnet and on this machine — not on the local network"
    return "127.0.0.1", "Tailscale not detected — bound to loopback instead"


def access_urls(bound: str) -> list:
    """Every URL this dashboard can actually be reached on."""
    port = config.ADSB_WEB_PORT
    token = f"?token={config.ADSB_WEB_TOKEN}" if config.ADSB_WEB_TOKEN else ""
    urls = []
    if bound in ("0.0.0.0", "::", ""):
        urls.append(f"http://localhost:{port}/{token}")
        ts = tailscale_ip()
        if ts:
            urls.append(f"http://{ts}:{port}/{token}   (Tailscale)")
        try:
            lan = socket.gethostbyname(socket.gethostname())
            if lan and not lan.startswith("127."):
                urls.append(f"http://{lan}:{port}/{token}   (LAN)")
        except Exception:
            pass
    elif bound.startswith("100."):
        urls.append(f"http://{bound}:{port}/{token}   (Tailscale)")
    elif bound.startswith("127."):
        urls.append(f"http://localhost:{port}/{token}")
    else:
        urls.append(f"http://{bound}:{port}/{token}")
    return urls


def bind_addresses() -> tuple:
    """Every address to listen on, plus a note. Usually one; two for Tailscale.

    Binding to the Tailscale address alone is the safe choice, but it also
    means the machine running the tracker cannot open the map on localhost —
    which is exactly where you are when you are looking at the terminal. So
    "tailscale" listens on both the Tailnet address and loopback, and on
    nothing else. 0.0.0.0 remains available for anyone who wants it.
    """
    bind, note = resolve_bind()
    if bind.startswith("100.") and 64 <= int(bind.split(".")[1]) <= 127:
        return ("127.0.0.1", bind), note
    return (bind,), note


def serve(poller, stop_event=None, on_log=None, notes_provider=None) -> None:
    """Run the dashboard until stop_event is set. Intended as a thread body."""
    attrs = {"poller": poller}
    if notes_provider is not None:
        # staticmethod so the attribute stays a plain callable rather than
        # binding `self` when accessed through the handler instance.
        attrs["notes_provider"] = staticmethod(notes_provider)
    handler = type("_BoundHandler", (_Handler,), attrs)
    addresses, note = bind_addresses()

    servers = []
    for addr in addresses:
        try:
            httpd = ThreadingHTTPServer((addr, config.ADSB_WEB_PORT), handler)
            httpd.daemon_threads = True
            servers.append((addr, httpd))
        except OSError as exc:
            if on_log:
                on_log(
                    f"ADS-B map could not listen on {addr}:{config.ADSB_WEB_PORT}: {exc}",
                    True,
                )
    if not servers:
        return

    if on_log:
        for addr, _ in servers:
            for url in access_urls(addr):
                on_log(f"ADS-B map  →  {url}")
        if note:
            on_log(f"ADS-B map  {note}")
        if any(a in ("0.0.0.0", "::") for a, _ in servers):
            on_log(
                "ADS-B map is bound to every interface. Set ADSB_WEB_BIND=tailscale "
                "to restrict it to your Tailnet plus loopback.",
                True,
            )
        if not config.ADSB_WEB_TOKEN and any(
            not a.startswith("127.") for a, _ in servers
        ):
            on_log("ADS-B map has no ADSB_WEB_TOKEN set — anyone who can reach "
                   "the port can view it.", True)

    if stop_event is not None:
        threading.Thread(
            target=lambda: (
                stop_event.wait(),
                [s.shutdown() for _, s in servers],
            ),
            daemon=True,
            name="adsb-web-stop",
        ).start()

    # Each listener needs its own serve_forever; run all but the last on their
    # own threads and keep this one for the last so serve() still blocks.
    for addr, httpd in servers[:-1]:
        threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
            name=f"adsb-web-{addr}",
        ).start()
    try:
        servers[-1][1].serve_forever(poll_interval=0.5)
    finally:
        for _, httpd in servers:
            httpd.server_close()
