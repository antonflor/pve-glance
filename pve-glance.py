#!/usr/bin/env python3
"""
pve-glance — a zero-dependency, single-file live dashboard for Proxmox VE.

Run it on a Proxmox VE node. It serves a web page showing every node and guest
(VM + LXC) as live CPU / RAM / disk meters, refreshed in the browser. Data comes
from `pvesh get /cluster/resources`, so it needs no API token — it uses the local
root permissions of the node it runs on.

    python3 pve-glance.py         # then open http://<node-ip>:8008

Configuration (all optional, via environment variables):
    PVE_GLANCE_BIND     interface to bind      (default 0.0.0.0)
    PVE_GLANCE_PORT     port to listen on      (default 8008)
    PVE_GLANCE_AUTH     "user:pass" to require HTTP basic auth (default: none)
    PVE_GLANCE_CACHE    seconds to cache pvesh output across viewers (default 3)
    PVE_GLANCE_PVESH    path to the pvesh binary (default: pvesh on PATH)
    PVE_GLANCE_VERBOSE  set to log every request

MIT licensed. No external dependencies — Python 3 standard library only.
"""
import base64
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("PVE_GLANCE_BIND", "0.0.0.0")
PORT = int(os.environ.get("PVE_GLANCE_PORT", "8008"))
AUTH = os.environ.get("PVE_GLANCE_AUTH", "")
CACHE_TTL = float(os.environ.get("PVE_GLANCE_CACHE", "3"))
PVESH = os.environ.get("PVE_GLANCE_PVESH", "pvesh")
VERBOSE = bool(os.environ.get("PVE_GLANCE_VERBOSE"))

_cache = {"t": 0.0, "data": None}
_lock = threading.Lock()


def collect():
    """Query the PVE cluster and reshape into {updated, nodes[], guests[]}."""
    proc = subprocess.run(
        [PVESH, "get", "/cluster/resources", "--output-format", "json"],
        capture_output=True, text=True, timeout=25)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "pvesh exited non-zero")
    items = json.loads(proc.stdout or "[]")

    nodes, guests = [], []
    for r in items:
        kind = r.get("type")
        if kind == "node":
            nodes.append({
                "name": r.get("node", ""),
                "status": r.get("status", ""),
                "cpu": r.get("cpu", 0), "maxcpu": r.get("maxcpu", 0),
                "mem": r.get("mem", 0), "maxmem": r.get("maxmem", 0),
                "disk": r.get("disk", 0), "maxdisk": r.get("maxdisk", 0),
                "uptime": r.get("uptime", 0),
            })
        elif kind in ("qemu", "lxc"):
            guests.append({
                "vmid": r.get("vmid"), "name": r.get("name", ""),
                "type": kind, "node": r.get("node", ""),
                "status": r.get("status", ""),
                "cpu": r.get("cpu", 0), "maxcpu": r.get("maxcpu", 0),
                "mem": r.get("mem", 0), "maxmem": r.get("maxmem", 0),
                "disk": r.get("disk", 0), "maxdisk": r.get("maxdisk", 0),
                "uptime": r.get("uptime", 0),
            })

    nodes.sort(key=lambda n: n["name"])
    guests.sort(key=lambda g: (g["node"], g["status"] != "running", g["vmid"] or 0))
    return {"updated": int(time.time()), "nodes": nodes, "guests": guests}


def cached_collect():
    with _lock:
        now = time.time()
        if _cache["data"] is None or now - _cache["t"] > CACHE_TTL:
            _cache["data"] = collect()
            _cache["t"] = now
        return _cache["data"]


class Handler(BaseHTTPRequestHandler):
    server_version = "pve-glance"

    def _authed(self):
        if not AUTH:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                return base64.b64decode(hdr[6:]).decode("utf-8", "replace") == AUTH
            except Exception:
                return False
        return False

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="pve-glance"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/resources":
            try:
                data = cached_collect()
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}),
                                  "application/json")
            return self._send(200, json.dumps(data, separators=(",", ":")),
                              "application/json")
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        return self._send(404, "not found", "text/plain")

    do_HEAD = do_GET

    def log_message(self, *args):
        if VERBOSE:
            super().log_message(*args)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>pve-glance</title>
<script>try{document.documentElement.setAttribute('data-theme',localStorage.getItem('pgtheme')||'dark')}catch(e){}</script>
<style>
/* ---- THEMES ---- switch via the header buttons; add your own by copying a block.
   The active theme is set with data-theme on <html>. Default (no attribute) = dark. */
:root, :root[data-theme="dark"]{
  --bg:#0b0e13;--panel:#11151c;--fg:#e8ecf1;--fg-dim:#94a0b0;--fg-mute:#5b6672;
  --accent:#2dd4bf;--vm:#a78bfa;--ct:#38bdf8;
  --good:#4ade80;--warn:#fbbf24;--crit:#f87171;
  --line:rgba(232,236,241,.10);--line-2:rgba(232,236,241,.20);
  --track:rgba(255,255,255,.06);--hatch:rgba(255,255,255,.05);--shadow:none;
  --glow:radial-gradient(circle at 50% -8%,rgba(45,212,191,.06),transparent 55%);
  --ff:ui-monospace,SFMono-Regular,Menlo,Consolas,"JetBrains Mono",monospace;
  --ff-d:"Segoe UI",system-ui,-apple-system,sans-serif;
}
:root[data-theme="light"]{
  --bg:#f5f7fa;--panel:#ffffff;--fg:#16202e;--fg-dim:#556070;--fg-mute:#8a95a2;
  --accent:#0d9488;--vm:#7c3aed;--ct:#0369a1;
  --good:#15a34a;--warn:#c2740a;--crit:#dc2626;
  --line:rgba(20,30,45,.10);--line-2:rgba(20,30,45,.16);
  --track:rgba(20,30,45,.08);--hatch:rgba(20,30,45,.12);
  --shadow:0 1px 2px rgba(20,30,45,.06),0 1px 10px rgba(20,30,45,.04);
  --glow:radial-gradient(circle at 50% -8%,rgba(13,148,136,.07),transparent 55%);
  --ff:ui-monospace,SFMono-Regular,Menlo,Consolas,"JetBrains Mono",monospace;
  --ff-d:"Segoe UI",system-ui,-apple-system,sans-serif;
}
:root[data-theme="monty"]{
  --bg:#0a0a0b;--panel:#101013;--fg:#f1efe6;--fg-dim:#9a9a95;--fg-mute:#55554f;
  --accent:#d1ff48;--vm:#a78bfa;--ct:#48cdff;
  --good:#d1ff48;--warn:#ffb020;--crit:#ff5e3a;
  --line:rgba(241,239,230,.12);--line-2:rgba(241,239,230,.22);
  --track:rgba(255,255,255,.06);--hatch:rgba(255,255,255,.05);--shadow:none;
  --glow:radial-gradient(circle at 50% -5%,rgba(209,255,72,.06),transparent 55%),
    repeating-linear-gradient(0deg,rgba(255,255,255,.012) 0,rgba(255,255,255,.012) 1px,transparent 1px,transparent 3px);
  /* Space Grotesk / JetBrains Mono if the viewer has them installed; no external fonts are loaded. */
  --ff:"JetBrains Mono",ui-monospace,Menlo,monospace;
  --ff-d:"Space Grotesk",system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--ff);min-height:100%}
body{background:var(--glow),var(--bg)}
.wrap{max-width:1120px;margin:0 auto;padding:44px 24px 90px}
.head{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:16px}
.brand{font-family:var(--ff-d);font-weight:700;letter-spacing:.02em;font-size:22px}
.brand b{color:var(--accent)}
.brand .dotc{color:var(--fg-mute)}
.cursor{display:inline-block;width:8px;height:16px;background:var(--accent);margin-left:5px;vertical-align:-2px;animation:blink 1.1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.tag{color:var(--fg-dim);font-size:12px;letter-spacing:.04em;margin-top:7px}
.right{display:flex;flex-direction:column;align-items:flex-end;gap:9px}
.themesel{display:flex;gap:4px}
.themesel button{font-family:var(--ff);font-size:10px;letter-spacing:.07em;text-transform:uppercase;
  background:transparent;color:var(--fg-mute);border:1px solid var(--line-2);border-radius:5px;padding:4px 9px;cursor:pointer;transition:.14s}
.themesel button:hover{color:var(--fg-dim);border-color:var(--fg-mute)}
.themesel button.active{color:var(--accent);border-color:var(--accent)}
.meta{font-size:11px;color:var(--fg-dim);text-align:right;line-height:1.8;letter-spacing:.03em}
.meta .k{color:var(--fg-mute)} .meta .on{color:var(--good)}

section{margin-top:16px}
.hostpanel{margin:26px 0 6px;background:var(--panel);border:1px solid var(--line-2);border-radius:9px;
  padding:18px 20px;position:relative;overflow:hidden;box-shadow:var(--shadow)}
.hostpanel::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
.hostpanel.off{opacity:.55}.hostpanel.off::before{background:var(--crit)}
.hosttop{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:10px;margin-bottom:15px}
.hostname{font-family:var(--ff-d);font-weight:700;font-size:17px}
.hostname .tagn{color:var(--good);font-size:11px;font-weight:400;margin-left:9px;text-transform:uppercase;letter-spacing:.1em}
.hostpanel.off .tagn{color:var(--crit)}
.hostsub{color:var(--fg-mute);font-size:11px;letter-spacing:.04em}
.hostmeters{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px}

h3{font-family:var(--ff-d);font-size:11px;letter-spacing:.16em;text-transform:uppercase;font-weight:600;color:var(--fg-dim);
  margin:26px 0 13px;border-bottom:1px solid var(--line);padding-bottom:8px;display:flex;align-items:center;gap:10px}
h3 .cdot{width:8px;height:8px;border-radius:2px;background:var(--accent)}
h3 .cnt{color:var(--fg-mute);font-size:10px;letter-spacing:.06em;margin-left:auto;text-transform:none}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line-2);border-radius:7px;padding:14px 15px;position:relative;overflow:hidden;transition:.16s;box-shadow:var(--shadow)}
.card:hover{transform:translateY(-2px)}
.card.stopped{opacity:.5}
.crow{display:flex;align-items:center;gap:9px;margin-bottom:12px;padding-right:14px}
.badge{font-size:9px;letter-spacing:.09em;padding:2px 6px;border-radius:4px;border:1px solid var(--line-2);color:var(--fg-dim);text-transform:uppercase;flex-shrink:0}
.badge.vm{color:var(--vm);border-color:color-mix(in srgb,var(--vm) 45%,transparent)}
.badge.ct{color:var(--ct);border-color:color-mix(in srgb,var(--ct) 45%,transparent)}
.ct{font-family:var(--ff-d);font-weight:600;font-size:14.5px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vmid{color:var(--fg-mute);font-size:11px}
.dot{position:absolute;top:14px;right:13px;width:9px;height:9px;border-radius:50%;background:var(--fg-mute)}
.dot.up{background:var(--good);animation:pulse 2.2s infinite}
.dot.down{background:var(--crit);box-shadow:0 0 7px color-mix(in srgb,var(--crit) 70%,transparent)}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--good) 45%,transparent)}70%{box-shadow:0 0 0 6px transparent}100%{box-shadow:0 0 0 0 transparent}}

.meter{margin:8px 0}
.mlab{display:flex;justify-content:space-between;align-items:baseline;font-size:11px;margin-bottom:4px}
.mlab .k{color:var(--fg-mute);letter-spacing:.06em}
.mlab .v{color:var(--fg);font-variant-numeric:tabular-nums}
.mlab .v .u{color:var(--fg-mute);font-size:10px;margin-left:3px}
.track{height:6px;background:var(--track);border-radius:4px;overflow:hidden}
.fill{height:100%;border-radius:4px;transition:width .5s ease,background .3s;min-width:2px}
.fill.g{background:var(--good)}.fill.w{background:var(--warn)}.fill.c{background:var(--crit)}
.track.na{background:repeating-linear-gradient(45deg,var(--hatch) 0 5px,transparent 5px 10px)}
.mfoot{color:var(--fg-mute);font-size:10px;letter-spacing:.04em;margin-top:11px;border-top:1px solid var(--line);padding-top:8px;display:flex;justify-content:space-between}
.mfoot.stop{border:0;padding:0;color:var(--crit)}

.legend{display:flex;gap:15px;flex-wrap:wrap;margin:20px 0 0;color:var(--fg-mute);font-size:11px}
.legend .li{display:flex;align-items:center;gap:6px}
.legend .sw{width:10px;height:10px;border-radius:2px}
.foot{margin-top:44px;padding-top:15px;border-top:1px solid var(--line);color:var(--fg-mute);font-size:11px;letter-spacing:.04em}
.foot a{color:var(--fg-dim);text-decoration:none;border-bottom:1px solid var(--line)}
.err{color:var(--crit);font-size:12.5px;margin:26px 0;display:none}
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <div>
      <div class="brand" id="brand"><b>pve</b><span class="dotc">·</span>glance</div>
      <div class="tag" id="tag">live at-a-glance view of every node &amp; guest</div>
    </div>
    <div class="right">
      <div class="themesel">
        <button data-set="light">Light</button>
        <button data-set="dark">Dark</button>
        <button data-set="monty">Monty</button>
      </div>
      <div class="meta">
        <div><span class="k">NODES</span> <span id="m-nodes">—</span> · <span class="k">GUESTS</span> <span id="m-guests" class="on">— / —</span> up</div>
        <div><span class="k">UPDATED</span> <span id="m-upd">—</span></div>
        <div id="m-clock">--:--:--</div>
      </div>
    </div>
  </div>

  <div class="legend">
    <div class="li"><span class="sw" style="background:var(--good)"></span>&lt; 70%</div>
    <div class="li"><span class="sw" style="background:var(--warn)"></span>70–90%</div>
    <div class="li"><span class="sw" style="background:var(--crit)"></span>&ge; 90%</div>
    <div class="li"><span class="sw" style="background:var(--vm)"></span>VM (KVM)</div>
    <div class="li"><span class="sw" style="background:var(--ct)"></span>CT (LXC)</div>
  </div>

  <div id="sections"></div>
  <div class="err" id="err"></div>

  <div class="foot">
    pve-glance · zero-dependency single-file dashboard · auto-refreshes every 10s ·
    <a href="/api/resources">/api/resources</a>
  </div>
</div>

<script>
const GiB = 1073741824;
const fmtG = b => b >= GiB ? (b/GiB).toFixed(b >= 100*GiB ? 0 : 1) : (b/1048576).toFixed(0);
const unit = b => b >= GiB ? 'G' : 'M';
const cls  = p => p >= 90 ? 'c' : p >= 70 ? 'w' : 'g';
function up(s){
  if(!s) return '—';
  const d = Math.floor(s/86400), h = Math.floor(s%86400/3600);
  if(d > 0) return d+'d '+h+'h';
  return h+'h '+Math.floor(s%3600/60)+'m';
}
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function meter(label, pct, val, u, na){
  const p = Math.max(0, Math.min(100, pct||0));
  const track = na
    ? '<div class="track na"></div>'
    : '<div class="track"><div class="fill '+cls(p)+'" style="width:'+p+'%"></div></div>';
  return '<div class="meter"><div class="mlab"><span class="k">'+label+'</span>'
    + '<span class="v">'+(na?'n/a':val)+(u?'<span class="u">'+u+'</span>':'')+'</span></div>'
    + track + '</div>';
}
function nodePanel(n){
  const cpu=(n.cpu||0)*100, mem=n.maxmem?n.mem/n.maxmem*100:0, dsk=n.maxdisk?n.disk/n.maxdisk*100:0;
  const online = n.status === 'online';
  return '<div class="hostpanel'+(online?'':' off')+'">'
    + '<div class="hosttop"><div class="hostname">'+esc(n.name)+' <span class="tagn">'+(online?'online':esc(n.status||'offline'))+'</span></div>'
    + '<div class="hostsub">'+n.maxcpu+' cores · up '+up(n.uptime)+'</div></div>'
    + '<div class="hostmeters">'
    + meter('CPU', cpu, cpu.toFixed(0)+'%', ' · '+n.maxcpu+'c', false)
    + meter('RAM', mem, fmtG(n.mem)+' / '+fmtG(n.maxmem), ' '+unit(n.maxmem), false)
    + meter('ROOT DISK', dsk, fmtG(n.disk)+' / '+fmtG(n.maxdisk), ' '+unit(n.maxdisk), false)
    + '</div></div>';
}
function guestCard(g){
  const on = g.status === 'running', isVM = g.type === 'qemu';
  const cpu=(g.cpu||0)*100, mem=g.maxmem?g.mem/g.maxmem*100:0, dsk=g.maxdisk?g.disk/g.maxdisk*100:0;
  const diskNA = isVM || !g.disk;   // Proxmox reports no in-guest fs usage for VMs
  return '<div class="card'+(on?'':' stopped')+'">'
    + '<span class="dot '+(on?'up':'down')+'"></span>'
    + '<div class="crow"><span class="badge '+(isVM?'vm':'ct')+'">'+(isVM?'VM':'CT')+'</span>'
    + '<span class="ct">'+esc(g.name)+'</span><span class="vmid">#'+g.vmid+'</span></div>'
    + (on ? (
        meter('CPU', cpu, cpu.toFixed(cpu<10?1:0)+'%', ' · '+g.maxcpu+'c', false)
      + meter('RAM', mem, fmtG(g.mem)+' / '+fmtG(g.maxmem), ' '+unit(g.maxmem), false)
      + meter('DISK', dsk, diskNA?'':fmtG(g.disk)+' / '+fmtG(g.maxdisk), diskNA?'':' '+unit(g.maxdisk), diskNA)
      + '<div class="mfoot"><span>up '+up(g.uptime)+'</span><span>'+(isVM?'KVM':'LXC')+'</span></div>'
      ) : '<div class="mfoot stop">stopped</div>')
    + '</div>';
}
function sectionFor(node, guests){
  const running = guests.filter(g=>g.status==='running').length;
  const vm = guests.filter(g=>g.type==='qemu').length, ct = guests.length - vm;
  return '<section>' + nodePanel(node)
    + '<h3><span class="cdot"></span>Guests <span class="cnt">'+running+' / '+guests.length+' up · '+vm+' VM · '+ct+' CT</span></h3>'
    + '<div class="grid">' + (guests.map(guestCard).join('') || '<div class="mfoot">no guests</div>') + '</div>'
    + '</section>';
}

function showErr(msg){ const e=document.getElementById('err'); e.textContent='> '+msg; e.style.display='block'; }

async function refresh(){
  let doc;
  try{
    const r = await fetch('/api/resources', {cache:'no-store'});
    doc = await r.json();
  }catch(e){ showErr('could not reach /api/resources — is pve-glance running?'); return; }
  if(doc.error){ showErr('pvesh error: '+doc.error); return; }
  document.getElementById('err').style.display = 'none';

  const nodes = doc.nodes || [], guests = doc.guests || [];
  const byNode = {};
  guests.forEach(g => { (byNode[g.node] = byNode[g.node] || []).push(g); });

  // any guest on a node not returned in nodes[] still gets a section
  const names = nodes.map(n => n.name);
  Object.keys(byNode).forEach(n => { if(!names.includes(n)) nodes.push({name:n,status:'',maxcpu:0,uptime:0,cpu:0,mem:0,maxmem:0,disk:0,maxdisk:0}); });

  document.getElementById('sections').innerHTML =
    nodes.map(n => sectionFor(n, byNode[n.name] || [])).join('') || '<div class="err" style="display:block">no nodes returned</div>';

  const running = guests.filter(g=>g.status==='running').length;
  document.getElementById('m-nodes').textContent = nodes.length;
  document.getElementById('m-guests').textContent = running+' / '+guests.length;
  document.getElementById('m-upd').textContent = new Date((doc.updated||0)*1000).toLocaleTimeString();
}

// ---------- theme switcher ----------
const THEMES = ['light','dark','monty'];
const BRANDS = {
  monty: ['<b>MONTY</b>LABS<span class="cursor"></span>', '// live infrastructure at a glance'],
  def:   ['<b>pve</b><span class="dotc">·</span>glance', 'live at-a-glance view of every node &amp; guest'],
};
function applyTheme(t){
  if(!THEMES.includes(t)) t = 'dark';
  document.documentElement.setAttribute('data-theme', t);
  try{ localStorage.setItem('pgtheme', t); }catch(e){}
  document.querySelectorAll('.themesel button').forEach(b => b.classList.toggle('active', b.dataset.set === t));
  const [brand, tag] = BRANDS[t === 'monty' ? 'monty' : 'def'];
  document.getElementById('brand').innerHTML = brand;
  document.getElementById('tag').innerHTML = tag;
}
document.querySelectorAll('.themesel button').forEach(b =>
  b.addEventListener('click', () => applyTheme(b.dataset.set)));
let saved = 'dark';
try{ saved = localStorage.getItem('pgtheme') || 'dark'; }catch(e){}
applyTheme(saved);

function clock(){ document.getElementById('m-clock').textContent = new Date().toLocaleTimeString(); }
setInterval(clock, 1000); clock();
refresh(); setInterval(refresh, 10000);
</script>
</body>
</html>
"""


def main():
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print("pve-glance serving on http://%s:%d  (auth: %s)"
          % (BIND, PORT, "on" if AUTH else "off"), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
