#!/usr/bin/env python3
"""
pve-glance — a zero-dependency, single-file live dashboard for Proxmox VE.

Run it on a Proxmox VE node. It serves a web page showing every node and guest
(VM + LXC) as live CPU / RAM / disk meters, refreshed in the browser. Data comes
from `pvesh get /cluster/resources`, so it needs no API token — it uses the local
root permissions of the node it runs on.

    python3 pve-glance.py         # then open http://<node-ip>:8008

Or run it on any machine and point it at one or more REMOTE PVE clusters over the
HTTP API with read-only tokens (see PVE_GLANCE_API_URL / PVE_GLANCE_TARGETS_FILE).

Created by MONTYLABS — https://github.com/antonflor/pve-glance

Configuration (all optional, via environment variables):
    PVE_GLANCE_BIND       interface to bind      (default 0.0.0.0)
    PVE_GLANCE_PORT       port to listen on      (default 8008)
    PVE_GLANCE_AUTH       "user:pass" to require HTTP basic auth (default: none)
    PVE_GLANCE_CACHE      seconds to cache output across viewers (default 3)
    PVE_GLANCE_PVESH      path to the pvesh binary (default: pvesh on PATH)
    PVE_GLANCE_VERBOSE    set to log every request
  Remote mode (talk to a remote PVE instead of local pvesh):
    PVE_GLANCE_API_URL    PVE base URL, e.g. https://192.168.1.30:8006
    PVE_GLANCE_API_TOKEN  API token: "USER@REALM!TOKENID=SECRET" (read-only is enough)
    PVE_GLANCE_API_VERIFY set to verify TLS (default off — PVE ships a self-signed cert)
    PVE_GLANCE_API_TIMEOUT  per-target HTTP timeout in seconds (default 15)
  Multiple clusters (aggregate several PVE clusters into one page):
    PVE_GLANCE_TARGETS_FILE path to JSON: [{"name","url","token","verify"}, ...]
    PVE_GLANCE_TARGETS      the same JSON inline (alternative to the file)
    PVE_GLANCE_LOCAL_NAME   label for the local cluster in local mode (default: hostname)

MIT licensed. No external dependencies — Python 3 standard library only.
"""
from concurrent.futures import ThreadPoolExecutor
import base64
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("PVE_GLANCE_BIND", "0.0.0.0")
PORT = int(os.environ.get("PVE_GLANCE_PORT", "8008"))
AUTH = os.environ.get("PVE_GLANCE_AUTH", "")
CACHE_TTL = float(os.environ.get("PVE_GLANCE_CACHE", "3"))
PVESH = os.environ.get("PVE_GLANCE_PVESH", "pvesh")
VERBOSE = bool(os.environ.get("PVE_GLANCE_VERBOSE"))

# Remote mode: if PVE_GLANCE_API_URL is set, talk to a remote PVE over the HTTP
# API with a token instead of the local pvesh binary.
API_URL = os.environ.get("PVE_GLANCE_API_URL", "").rstrip("/")
API_TOKEN = os.environ.get("PVE_GLANCE_API_TOKEN", "")
API_VERIFY = os.environ.get("PVE_GLANCE_API_VERIFY", "") not in ("", "0", "false", "no", "off")
API_TIMEOUT = float(os.environ.get("PVE_GLANCE_API_TIMEOUT", "15"))

# Multi-cluster mode: point at several PVE clusters at once. Either a JSON file
# (PVE_GLANCE_TARGETS_FILE) or inline JSON (PVE_GLANCE_TARGETS), each a list of
# {"name","url","token","verify"} objects. Falls back to the single API_URL above,
# then to local pvesh.
TARGETS_FILE = os.environ.get("PVE_GLANCE_TARGETS_FILE", "")
TARGETS_JSON = os.environ.get("PVE_GLANCE_TARGETS", "")
LOCAL_NAME = os.environ.get("PVE_GLANCE_LOCAL_NAME") or socket.gethostname() or "local"

_cache = {"t": 0.0, "data": None}
_lock = threading.Lock()


def reshape(items):
    """Turn a /cluster/resources array into {updated, nodes[], guests[]}."""
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


def _pvesh_get(path, timeout=25):
    """Local mode: read /cluster/resources via the pvesh CLI (runs on a PVE node)."""
    proc = subprocess.run(
        [PVESH, "get", path, "--output-format", "json"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "pvesh exited non-zero")
    return json.loads(proc.stdout or "[]")


def _http_get(target, path, timeout=API_TIMEOUT):
    """Remote mode: read /cluster/resources from one PVE target over the HTTP API."""
    token = target["token"]
    if not token:
        raise RuntimeError("no API token for target '%s'" % target["name"])
    if token.startswith("PVEAPIToken="):
        token = token[len("PVEAPIToken="):]
    ctx = ssl.create_default_context()
    if not target["verify"]:                 # self-signed certs are the PVE default
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        target["url"] + "/api2/json" + path,
        headers={"Authorization": "PVEAPIToken=" + token})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8")).get("data", [])


# Agent calls are automatic, bounded, and cached separately from CPU/RAM.
# Cache entries are scoped to the target (including its credentials) and guest.
_disk_cache = {}
DISK_TTL = 30
AGENT_TIMEOUT = 5


def filesystems(payload):
    """Keep actual filesystems with usable usage counters, including empty disks."""
    rows = payload.get("result", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    out = []
    seen = set()
    for fs in rows:
        if not isinstance(fs, dict):
            continue
        mount = fs.get("mountpoint")
        used, total = fs.get("used-bytes"), fs.get("total-bytes")
        if (not isinstance(mount, str) or not mount or mount in seen
                or fs.get("type", "").lower() in
                {"tmpfs", "devtmpfs", "squashfs", "iso9660", "overlay", "nfs", "nfs4", "cifs"}
                or ("disk" in fs and not fs["disk"])
                or type(used) is not int or type(total) is not int
                or total <= 0 or used < 0 or used > total):
            continue
        seen.add(mount)
        out.append({"mountpoint": mount, "type": fs.get("type", ""),
                    "disk": used, "maxdisk": total})
    return sorted(out, key=lambda f: (f["mountpoint"] not in ("/", "C:\\"), f["mountpoint"]))


def guest_disks(cluster, target=None):
    """Enrich running VMs; an unavailable agent never fails the cluster."""
    now = time.monotonic()
    scope = (target["url"], target["token"], target["verify"]) if target else ("local",)
    active = {(scope, g["node"], g["vmid"]) for g in cluster["guests"]
              if g["type"] == "qemu" and g["status"] == "running"}
    for key in list(_disk_cache):
        if key[0] == scope and key not in active:
            del _disk_cache[key]

    def work(g):
        if g["type"] != "qemu":
            return
        g.update(filesystems=[], disk_status="stopped", disk=0, maxdisk=0)
        if g["status"] != "running":
            return
        key = (scope, g["node"], g["vmid"])
        cached = _disk_cache.get(key)
        if cached and now - cached[0] < DISK_TTL:
            g.update(cached[1])
            return
        path = "/nodes/%s/qemu/%s/agent/get-fsinfo" % (
            quote(str(g["node"]), safe=""), quote(str(g["vmid"]), safe=""))
        result = {"filesystems": [], "disk_status": "agent unavailable"}
        try:
            payload = (_http_get(target, path, AGENT_TIMEOUT) if target
                       else _pvesh_get(path, AGENT_TIMEOUT))
            rows = filesystems(payload)
            result = {"filesystems": rows,
                      "disk_status": "ok" if rows else "no filesystem data"}
            if rows:
                result.update(disk=rows[0]["disk"], maxdisk=rows[0]["maxdisk"])
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                result["disk_status"] = "permission denied"
        except (subprocess.TimeoutExpired, TimeoutError):
            result["disk_status"] = "agent timed out"
        except Exception:
            pass  # Do not expose remote tokens, URLs or raw guest-agent errors.
        _disk_cache[key] = (time.monotonic(), result)
        g.update(result)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, cluster["guests"]))
    return cluster


def _host_of(url):
    return url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0] or "remote"


def targets():
    """Resolve remote targets, or [] to mean local pvesh mode."""
    if TARGETS_FILE:
        with open(TARGETS_FILE) as fh:
            data = json.load(fh)
    elif TARGETS_JSON:
        data = json.loads(TARGETS_JSON)
    elif API_URL:
        data = [{"url": API_URL, "token": API_TOKEN, "verify": API_VERIFY}]
    else:
        return []
    out = []
    for t in data:
        url = (t.get("url") or "").rstrip("/")
        out.append({
            "name": t.get("name") or _host_of(url),
            "url": url,
            "token": t.get("token", ""),
            "verify": bool(t.get("verify", API_VERIFY)),
        })
    return out


def _cluster(name, items):
    r = reshape(items)
    return {"name": name, "ok": True, "nodes": r["nodes"], "guests": r["guests"]}


def collect():
    """Aggregate one or more clusters into {updated, clusters[]}.

    Each cluster is collected independently, so one unreachable target is shown as
    an error without breaking the rest. Remote targets are fetched in parallel."""
    tgts = targets()
    if not tgts:                             # local pvesh — a single cluster
        try:
            clusters = [guest_disks(_cluster(LOCAL_NAME, _pvesh_get("/cluster/resources")))]
        except Exception as exc:
            clusters = [{"name": LOCAL_NAME, "ok": False, "error": str(exc),
                         "nodes": [], "guests": []}]
        return {"updated": int(time.time()), "clusters": clusters}

    clusters = [None] * len(tgts)

    def work(i, t):
        try:
            clusters[i] = guest_disks(_cluster(t["name"], _http_get(t, "/cluster/resources")), t)
        except Exception as exc:
            clusters[i] = {"name": t["name"], "ok": False, "error": str(exc),
                           "nodes": [], "guests": []}

    threads = [threading.Thread(target=work, args=(i, t)) for i, t in enumerate(tgts)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return {"updated": int(time.time()), "clusters": clusters}


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
<script>try{document.documentElement.setAttribute('data-theme',['light','dark','monty','stars','tricolor'].includes(localStorage.getItem('pgtheme'))?localStorage.getItem('pgtheme'):'dark')}catch(e){}</script>
<style>
/* ---- THEMES ---- switch in Settings; add your own by copying a block.
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
:root[data-theme="stars"]{
  --bg:#0b152a;--panel:#13223d;--fg:#f7f9ff;--fg-dim:#bdcbe4;--fg-mute:#9bacc9;
  --accent:#91bdff;--vm:#c6b5ff;--ct:#91bdff;
  --good:#6fe0a0;--warn:#ffd074;--crit:#ff8e98;
  --line:rgba(247,249,255,.14);--line-2:rgba(247,249,255,.25);
  --track:rgba(255,255,255,.09);--hatch:rgba(255,255,255,.08);--shadow:0 4px 24px #040c1b44;
  --glow:radial-gradient(ellipse at 10% 0%,#416fba22,transparent 55%);
}
:root[data-theme="tricolor"]{
  --bg:#f4f4ee;--panel:#ffffff;--fg:#172e27;--fg-dim:#405f52;--fg-mute:#587163;
  --accent:#076b49;--vm:#70408e;--ct:#076b70;
  --good:#167448;--warn:#8b5900;--crit:#bc303b;
  --line:rgba(23,46,39,.14);--line-2:rgba(23,46,39,.25);
  --track:rgba(23,46,39,.09);--hatch:rgba(23,46,39,.09);--shadow:0 2px 14px #173f2910;
  --glow:radial-gradient(ellipse at 90% 0%,#b62b3710,transparent 55%);
}
:root[data-theme="stars"] .hostpanel::before{background:linear-gradient(#e87987 0 33%,#f7f9ff 33% 66%,#91bdff 66%)}
:root[data-theme="tricolor"] .hostpanel::before{background:linear-gradient(#076b49 0 33%,#d9dfd9 33% 66%,#bc303b 66%)}
:root[data-theme="stars"] .hostpanel.off::before,:root[data-theme="tricolor"] .hostpanel.off::before{background:var(--crit)}
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
button{font:inherit;cursor:pointer}
.settings-button,.settings-close,.settings-reset{border:1px solid var(--line-2);border-radius:7px;
  background:var(--panel);color:var(--fg);padding:9px 13px;font-size:12px}
button:hover{border-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
/* Settings opens as a panel anchored under the Settings button, with no dimmed
   backdrop, so the dashboard stays visible and theme changes preview live. */
.settings{color:var(--fg);background:var(--panel);border:1px solid var(--line-2);
  border-radius:12px;padding:20px;width:min(340px,calc(100% - 32px));max-height:calc(100dvh - 116px);
  margin:92px max(24px,calc((100vw - 1072px) / 2)) auto auto;
  box-shadow:0 18px 60px #0008,0 0 0 1px #0003;overflow:auto}
.settings::backdrop{background:transparent}
.settings-head{display:flex;justify-content:space-between;align-items:center;gap:16px}
.settings h2{font-family:var(--ff-d);font-size:18px;margin:0}
.settings p{color:var(--fg-dim);font-size:12px;line-height:1.6;margin:8px 0 0}
.settings fieldset{border:0;padding:0;margin:18px 0}
.settings legend{font-size:11px;text-transform:uppercase;letter-spacing:.12em;margin-bottom:12px;color:var(--fg-dim)}
.theme-options{display:grid;grid-template-columns:1fr;gap:6px}
.theme-option{position:relative;display:flex;align-items:center;gap:10px;border:1px solid var(--line-2);
  border-radius:8px;cursor:pointer;font-size:12px;min-height:44px;padding:8px 12px}
.theme-option:has(input:checked){border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.theme-option input{margin:0;accent-color:var(--accent);width:16px;height:16px;flex-shrink:0}
.theme-swatch{width:22px;height:22px;border:1px solid #8993a777;border-radius:50%;flex-shrink:0}
.settings-footer{border-top:1px solid var(--line);padding-top:14px;display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.settings-footer p{flex:1;min-width:120px;margin:0;font-size:11px}
@media(max-width:400px){.settings{margin:88px 16px auto auto;padding:16px}}
@media(prefers-reduced-motion:reduce){.cursor{animation:none}}
.meta{font-size:11px;color:var(--fg-dim);text-align:right;line-height:1.8;letter-spacing:.03em}
.meta .k{color:var(--fg-mute)} .meta .on{color:var(--good)}

section{margin-top:16px}
.clusterbar{margin:38px 0 12px;padding:12px 16px;background:var(--panel);border:1px solid var(--line-2);
  border-left:3px solid var(--accent);border-radius:8px;box-shadow:var(--shadow);
  display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:10px}
.clusterbar.off{border-left-color:var(--crit)}
.cname{font-family:var(--ff-d);font-weight:700;font-size:16px;letter-spacing:.02em}
.cname .cstate{font-size:10px;font-weight:400;text-transform:uppercase;letter-spacing:.12em;color:var(--good);margin-left:9px}
.clusterbar.off .cstate{color:var(--crit)}
.csub{color:var(--fg-mute);font-size:11px}
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
      <button class="settings-button" id="settings-open" type="button" aria-haspopup="dialog" aria-controls="settings">Settings</button>
      <div class="meta">
        <div><span id="m-cl"></span><span class="k">NODES</span> <span id="m-nodes">—</span> · <span class="k">GUESTS</span> <span id="m-guests" class="on">— / —</span> up</div>
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
    <a href="/api/resources">/api/resources</a> ·
    built by <a href="https://github.com/antonflor/pve-glance">MONTYLABS</a>
  </div>
</div>


<dialog class="settings" id="settings" aria-labelledby="settings-title" aria-describedby="settings-description">
  <div class="settings-head">
    <h2 id="settings-title">Settings</h2>
    <button class="settings-close" id="settings-close" type="button">Close</button>
  </div>
  <p id="settings-description">Changes apply instantly.</p>
  <fieldset>
    <legend>Appearance</legend>
    <div class="theme-options">
      <label class="theme-option"><input type="radio" name="theme" value="light"><span class="theme-swatch" aria-hidden="true" style="background:linear-gradient(135deg,#f5f7fa 50%,#0d9488 50%)"></span>Light</label>
      <label class="theme-option"><input type="radio" name="theme" value="dark"><span class="theme-swatch" aria-hidden="true" style="background:linear-gradient(135deg,#11151c 50%,#2dd4bf 50%)"></span>Dark</label>
      <label class="theme-option"><input type="radio" name="theme" value="monty"><span class="theme-swatch" aria-hidden="true" style="background:linear-gradient(135deg,#101013 50%,#d1ff48 50%)"></span>Monty</label>
      <label class="theme-option"><input type="radio" name="theme" value="stars"><span class="theme-swatch" aria-hidden="true" style="background:linear-gradient(135deg,#b73548 33%,#f7f9ff 33% 66%,#234e8f 66%)"></span>Stars</label>
      <label class="theme-option"><input type="radio" name="theme" value="tricolor"><span class="theme-swatch" aria-hidden="true" style="background:linear-gradient(135deg,#076b49 33%,#fff 33% 66%,#bc303b 66%)"></span>Tricolor</label>
    </div>
  </fieldset>
  <div class="settings-footer">
    <p id="settings-status" role="status" aria-live="polite">Saved in this browser.</p>
    <button class="settings-reset" id="settings-reset" type="button">Reset settings</button>
  </div>
</dialog>

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
  const diskNA = !g.maxdisk;
  const diskMeters = isVM
    ? ((g.filesystems||[]).map(f => meter('DISK '+esc(f.mountpoint), f.disk/f.maxdisk*100,
        fmtG(f.disk)+' / '+fmtG(f.maxdisk), ' '+unit(f.maxdisk), false)).join('')
       || meter('DISK', 0, '', ' · '+esc(g.disk_status||'agent unavailable'), true))
    : meter('DISK', dsk, diskNA?'':fmtG(g.disk)+' / '+fmtG(g.maxdisk), diskNA?'':' '+unit(g.maxdisk), diskNA);
  return '<div class="card'+(on?'':' stopped')+'">'
    + '<span class="dot '+(on?'up':'down')+'"></span>'
    + '<div class="crow"><span class="badge '+(isVM?'vm':'ct')+'">'+(isVM?'VM':'CT')+'</span>'
    + '<span class="ct">'+esc(g.name)+'</span><span class="vmid">#'+g.vmid+'</span></div>'
    + (on ? (
        meter('CPU', cpu, cpu.toFixed(cpu<10?1:0)+'%', ' · '+g.maxcpu+'c', false)
      + meter('RAM', mem, fmtG(g.mem)+' / '+fmtG(g.maxmem), ' '+unit(g.maxmem), false)
      + diskMeters
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

function clusterHeader(c, up, total, nUp, nTot){
  const ok = c.ok;
  const sub = ok ? (nUp+' / '+nTot+' nodes · '+up+' / '+total+' guests up')
                 : esc(c.error || 'unreachable');
  return '<div class="clusterbar'+(ok?'':' off')+'">'
    + '<div class="cname">'+esc(c.name)+' <span class="cstate">'+(ok?'online':'unreachable')+'</span></div>'
    + '<div class="csub">'+sub+'</div></div>';
}

function prepNodes(c){
  const nodes = (c.nodes||[]).slice(), guests = c.guests||[];
  const byNode = {};
  guests.forEach(g => { (byNode[g.node] = byNode[g.node] || []).push(g); });
  const names = nodes.map(n => n.name);
  // any guest on a node not returned in nodes[] still gets a section
  Object.keys(byNode).forEach(n => { if(!names.includes(n)) nodes.push({name:n,status:'',maxcpu:0,uptime:0,cpu:0,mem:0,maxmem:0,disk:0,maxdisk:0}); });
  return {nodes, byNode, guests};
}

async function refresh(){
  let doc;
  try{
    const r = await fetch('/api/resources', {cache:'no-store'});
    doc = await r.json();
  }catch(e){ showErr('could not reach /api/resources — is pve-glance running?'); return; }
  if(doc.error){ showErr('error: '+doc.error); return; }

  const clusters = doc.clusters || [];
  const multi = clusters.length > 1;
  let html = '', tNodes = 0, tGuests = 0, tRunning = 0, cOk = 0;

  clusters.forEach(c => {
    const {nodes, byNode, guests} = prepNodes(c);
    const running = guests.filter(g => g.status === 'running').length;
    const nUp = nodes.filter(n => n.status === 'online').length;
    tNodes += nodes.length; tGuests += guests.length; tRunning += running;
    if(c.ok) cOk++;
    if(multi) html += clusterHeader(c, running, guests.length, nUp, nodes.length);
    if(c.ok) html += nodes.map(n => sectionFor(n, byNode[n.name] || [])).join('');
  });

  const singleFail = !multi && clusters.length === 1 && !clusters[0].ok;
  if(singleFail){
    showErr('error: '+(clusters[0].error || 'unreachable'));
    document.getElementById('sections').innerHTML = '';
  }else{
    document.getElementById('err').style.display = 'none';
    document.getElementById('sections').innerHTML = html ||
      '<div class="err" style="display:block">no data returned</div>';
  }

  document.getElementById('m-cl').innerHTML = multi
    ? '<span class="k">CLUSTERS</span> '+cOk+' / '+clusters.length+' · ' : '';
  document.getElementById('m-nodes').textContent = tNodes;
  document.getElementById('m-guests').textContent = tRunning+' / '+tGuests;
  document.getElementById('m-upd').textContent = new Date((doc.updated||0)*1000).toLocaleTimeString();
}

// ---------- theme switcher ----------
const THEMES = ['light','dark','monty','stars','tricolor'];
const BRANDS = {
  monty: ['<b>MONTY</b>LABS<span class="cursor"></span>', '// live infrastructure at a glance'],
  def:   ['<b>pve</b><span class="dotc">·</span>glance', 'live at-a-glance view of every node &amp; guest'],
};
function applyTheme(t, persist = true){
  if(!THEMES.includes(t)) t = 'dark';
  document.documentElement.setAttribute('data-theme', t);
  if(persist){
    try{
      localStorage.setItem('pgtheme', t);
      document.getElementById('settings-status').textContent = 'Saved in this browser.';
    }catch(e){
      document.getElementById('settings-status').textContent = 'Applied for this visit. Browser storage is unavailable.';
    }
  }
  document.querySelectorAll('input[name="theme"]').forEach(input => { input.checked = input.value === t; });
  const [brand, tag] = BRANDS[t === 'monty' ? 'monty' : 'def'];
  document.getElementById('brand').innerHTML = brand;
  document.getElementById('tag').innerHTML = tag;
}
document.querySelectorAll('input[name="theme"]').forEach(input =>
  input.addEventListener('change', () => { if(input.checked) applyTheme(input.value); }));
const settings = document.getElementById('settings');
document.getElementById('settings-open').addEventListener('click', () => {
  settings.showModal();
  // Start on the current choice, not the Close button, so the focus ring marks what's selected.
  const current = [...document.querySelectorAll('input[name="theme"]')].find(input => input.checked);
  if(current) current.focus();
});
document.getElementById('settings-close').addEventListener('click', () => settings.close());
settings.addEventListener('click', event => {
  const rect = settings.getBoundingClientRect();
  if(event.target === settings && (event.clientX < rect.left || event.clientX > rect.right
      || event.clientY < rect.top || event.clientY > rect.bottom)) settings.close();
});
document.getElementById('settings-reset').addEventListener('click', () => applyTheme('dark'));
let saved = 'dark';
try{ saved = localStorage.getItem('pgtheme') || 'dark'; }catch(e){
  document.getElementById('settings-status').textContent = 'Applied for this visit. Browser storage is unavailable.';
}
applyTheme(saved, false);

function clock(){ document.getElementById('m-clock').textContent = new Date().toLocaleTimeString(); }
setInterval(clock, 1000); clock();
refresh(); setInterval(refresh, 10000);
</script>
</body>
</html>
"""


def main():
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    try:
        tg = targets()
        source = ("local pvesh" if not tg
                  else "remote " + tg[0]["url"] if len(tg) == 1
                  else "%d remote clusters" % len(tg))
    except Exception as exc:
        source = "CONFIG ERROR: %s" % exc
    print("pve-glance serving on http://%s:%d  (source: %s, auth: %s)"
          % (BIND, PORT, source, "on" if AUTH else "off"), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
