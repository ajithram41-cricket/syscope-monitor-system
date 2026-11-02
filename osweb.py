#!/usr/bin/env python3
"""
SysScope Web: System Analysis & Live Performance Monitor (single-file, responsive)

Everything (Flask backend + HTML/CSS/JS frontend) lives in this one file.

Setup:
    pip install flask psutil

Run:
    python sysscope_web.py

Open:
    PC:     http://localhost:5000
    Phone:  http://<PC-LAN-IP>:5000   (same Wi-Fi)

Optional environment overrides:
    SYSSCOPE_HOST=127.0.0.1   # only allow this PC
    SYSSCOPE_PORT=8080
"""

import os
import platform
import socket
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import psutil
from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

HOST = os.environ.get("SYSSCOPE_HOST", "0.0.0.0")
PORT = int(os.environ.get("SYSSCOPE_PORT", "5000"))
REPORT_DIR = Path("reports")

LINK_AF = getattr(psutil, "AF_LINK", -1)
_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
#  ERROR HANDLING
# ══════════════════════════════════════════════════════════════════════════════

@app.errorhandler(HTTPException)
def handle_http_error(e):
    # Keep normal HTTP errors (404, 405, ...) as they are
    return jsonify({"error": e.description, "status": e.code}), e.code


@app.errorhandler(Exception)
def handle_error(e):
    traceback.print_exc()  # full traceback appears in the terminal
    return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def safe(fn, default=None):
    """Call fn and return default on any error (some metrics are OS-dependent)."""
    try:
        return fn()
    except Exception:
        return default


def fmt_bytes(b, precision=1):
    if b is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(b) < 1024:
            return f"{b:.{precision}f} {unit}"
        b /= 1024
    return f"{b:.{precision}f} EB"


def fmt_mhz(v):
    if not v:
        return "—"
    return f"{v / 1000:.2f} GHz" if v >= 1000 else f"{v:.0f} MHz"


def fmt_uptime(seconds):
    td = timedelta(seconds=int(seconds))
    days, hours = td.days, td.seconds // 3600
    minutes, secs = (td.seconds % 3600) // 60, td.seconds % 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP connect sends no packets; it only picks a route
        return s.getsockname()[0]
    except Exception:
        return "N/A"
    finally:
        s.close()


def temperatures():
    fn = getattr(psutil, "sensors_temperatures", None)
    data = safe(fn, {}) if fn else {}
    out = []
    for sensor, entries in (data or {}).items():
        for e in entries[:4]:
            out.append({"sensor": sensor, "label": e.label or "core",
                        "current": round(e.current, 1)})
    return out


def disk_partitions():
    out = []
    for p in psutil.disk_partitions(all=False):
        base = {"device": p.device, "mount": p.mountpoint, "fstype": p.fstype}
        try:
            u = psutil.disk_usage(p.mountpoint)
            out.append({**base, "total": u.total, "used": u.used, "free": u.free,
                        "percent": u.percent, "error": None})
        except OSError:
            out.append({**base, "total": 0, "used": 0, "free": 0, "percent": 0,
                        "error": "Not ready / permission denied"})
    return out


# Shared counters for network / disk throughput (rates are computed between polls)
_prev = {
    "t": time.time(),
    "net": psutil.net_io_counters(),
    "disk": safe(psutil.disk_io_counters),
}


# ══════════════════════════════════════════════════════════════════════════════
#  DATA COLLECTORS (each returns plain dicts → JSON)
# ══════════════════════════════════════════════════════════════════════════════

def get_overview():
    u = platform.uname()
    boot = psutil.boot_time()
    freq = safe(psutil.cpu_freq)
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "system": {
            "os": f"{u.system} {u.release}",
            "version": u.version[:90],
            "machine": u.machine,
            "hostname": u.node,
            "processor": (u.processor or platform.processor() or "N/A")[:90],
            "python": sys.version.split()[0],
            "boot_time": datetime.fromtimestamp(boot).strftime("%Y-%m-%d %H:%M:%S"),
            "uptime": fmt_uptime(time.time() - boot),
            "local_ip": get_local_ip(),
        },
        "cpu": {
            "physical": psutil.cpu_count(logical=False) or 0,
            "logical": psutil.cpu_count(logical=True) or 0,
            "freq_max": freq.max if freq else None,
        },
        "memory": {"total": vm.total, "swap_total": sw.total},
    }


def get_live():
    """Lightweight snapshot polled every ~2s by the dashboard."""
    now = time.time()
    net = psutil.net_io_counters()
    dsk = safe(psutil.disk_io_counters)

    with _lock:
        dt = max(now - _prev["t"], 0.001)
        pn, pd = _prev["net"], _prev["disk"]
        up = (net.bytes_sent - pn.bytes_sent) / dt
        down = (net.bytes_recv - pn.bytes_recv) / dt
        dr = (dsk.read_bytes - pd.read_bytes) / dt if (dsk and pd) else 0.0
        dw = (dsk.write_bytes - pd.write_bytes) / dt if (dsk and pd) else 0.0
        _prev.update(net=net, disk=dsk, t=now)

    per = psutil.cpu_percent(percpu=True, interval=None)
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()

    return {
        "uptime": fmt_uptime(now - psutil.boot_time()),
        "cpu": {"overall": round(sum(per) / len(per), 1) if per else 0.0,
                "logical": psutil.cpu_count() or 0},
        "memory": {"percent": vm.percent, "used": vm.used,
                   "total": vm.total, "available": vm.available},
        "swap": {"percent": sw.percent, "used": sw.used, "total": sw.total},
        "disks": disk_partitions()[:4],
        "net": {"up": max(up, 0), "down": max(down, 0)},
        "disk_io": {"read": max(dr, 0), "write": max(dw, 0)},
    }


def get_cpu():
    per = psutil.cpu_percent(interval=0.5, percpu=True)
    freqs = safe(lambda: psutil.cpu_freq(percpu=True)) or []
    freq = safe(psutil.cpu_freq)
    times = safe(lambda: psutil.cpu_times_percent(interval=None))
    stats = psutil.cpu_stats()

    tdict = {}
    if times:
        for k in ("user", "system", "idle", "iowait", "irq"):
            if hasattr(times, k):
                tdict[k] = round(getattr(times, k), 1)

    return {
        "overall": round(sum(per) / len(per), 1) if per else 0.0,
        "physical": psutil.cpu_count(logical=False) or 0,
        "logical": psutil.cpu_count(logical=True) or 0,
        "cores": [
            {"core": i, "usage": round(p, 1),
             "freq": freqs[i].current if i < len(freqs) and freqs[i] else None}
            for i, p in enumerate(per)
        ],
        "freq": {"current": freq.current if freq else None,
                 "max": freq.max if freq else None},
        "times": tdict,
        "stats": {"ctx_switches": stats.ctx_switches,
                  "interrupts": stats.interrupts,
                  "soft_interrupts": stats.soft_interrupts},
        "temps": temperatures(),
    }


def get_memory():
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    ram = {k: getattr(vm, k, None) for k in
           ("total", "available", "used", "free", "percent", "cached", "buffers", "shared")}
    swap = {"total": sw.total, "used": sw.used, "free": sw.free,
            "percent": sw.percent, "sin": sw.sin, "sout": sw.sout}
    return {"ram": ram, "swap": swap, "top": list_processes("rss", 15)["rows"]}


def get_disk():
    dio = safe(psutil.disk_io_counters)
    per = safe(lambda: psutil.disk_io_counters(perdisk=True)) or {}
    io = None
    if dio:
        io = {
            "read_count": dio.read_count, "write_count": dio.write_count,
            "read_bytes": dio.read_bytes, "write_bytes": dio.write_bytes,
            "read_time": getattr(dio, "read_time", None),
            "write_time": getattr(dio, "write_time", None),
        }
    return {
        "partitions": disk_partitions(),
        "io": io,
        "per_disk": [
            {"name": k, "reads": v.read_count, "writes": v.write_count,
             "read_bytes": v.read_bytes, "write_bytes": v.write_bytes}
            for k, v in list(per.items())[:8]
        ],
    }


def get_network():
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    ifaces = []
    for name, al in addrs.items():
        st = stats.get(name)
        ifaces.append({
            "name": name,
            "ipv4": next((a.address for a in al if a.family == socket.AF_INET), "—"),
            "ipv6": next((a.address.split("%")[0] for a in al if a.family == socket.AF_INET6), "—"),
            "mac": next((a.address for a in al if a.family == LINK_AF), "—"),
            "up": bool(st and st.isup),
            "speed": st.speed if st else 0,
            "mtu": st.mtu if st else None,
        })

    n = psutil.net_io_counters()
    io = {k: getattr(n, k, 0) for k in
          ("bytes_sent", "bytes_recv", "packets_sent", "packets_recv",
           "errin", "errout", "dropin", "dropout")}

    conns, note = [], None
    try:
        raw = sorted(psutil.net_connections(kind="inet"), key=lambda c: c.status or "")
        for c in raw[:30]:
            conns.append({
                "proto": "TCP" if c.type == socket.SOCK_STREAM else "UDP",
                "local": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "—",
                "remote": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "—",
                "status": c.status or "NONE",
                "pid": c.pid,
            })
    except (psutil.AccessDenied, OSError):
        note = "The connection list needs administrator/root privileges on this system."

    return {"interfaces": ifaces, "io": io, "connections": conns, "note": note}


ATTRS = ["pid", "name", "username", "status", "num_threads",
         "cpu_percent", "memory_percent", "memory_info"]
SORTS = {
    "cpu":  (lambda r: r["cpu"], True),
    "ram":  (lambda r: r["ram"], True),
    "rss":  (lambda r: r["rss"], True),
    "pid":  (lambda r: r["pid"], False),
    "name": (lambda r: r["name"].lower(), False),
}


def list_processes(sort="cpu", limit=30, query=""):
    q = query.lower()
    rows = []
    for p in psutil.process_iter(ATTRS):
        i = p.info
        name = i.get("name") or "?"
        if q and q not in name.lower() and q not in str(i.get("pid", "")):
            continue
        mi = i.get("memory_info")
        rows.append({
            "pid": i["pid"],
            "name": name,
            "user": i.get("username") or "?",
            "status": (i.get("status") or "unknown").lower(),
            "threads": i.get("num_threads") or 0,
            "cpu": round(i.get("cpu_percent") or 0.0, 1),
            "ram": round(i.get("memory_percent") or 0.0, 2),
            "rss": mi.rss if mi else 0,
        })

    counts = {"running": 0, "sleeping": 0, "zombie": 0}
    for r in rows:
        if r["status"] in counts:
            counts[r["status"]] += 1

    key, reverse = SORTS.get(sort, SORTS["cpu"])
    rows.sort(key=key, reverse=reverse)
    return {
        "total": len(rows),
        "threads": sum(r["threads"] for r in rows),
        "counts": counts,
        "rows": rows[:limit],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  TEXT REPORT
# ══════════════════════════════════════════════════════════════════════════════

def build_report_text():
    ov = get_overview()
    cpu = get_cpu()
    mem = get_memory()
    disk = get_disk()
    net = get_network()
    procs = list_processes("cpu", 20)

    L = []

    def head(title):
        L.extend(["", "=" * 72, f"  {title}", "=" * 72])

    def row(k, v):
        L.append(f"  {k:<24} {v}")

    L += ["=" * 72, "  SYSSCOPE: FULL SYSTEM REPORT",
          f"  Generated: {datetime.now():%Y-%m-%d %H:%M:%S}", "=" * 72]

    s = ov["system"]
    head("1. OPERATING SYSTEM")
    for key, label in [("os", "System"), ("version", "Version"), ("machine", "Machine"),
                       ("hostname", "Hostname"), ("processor", "Processor"),
                       ("python", "Python"), ("boot_time", "Boot time"),
                       ("uptime", "Uptime"), ("local_ip", "Local IP")]:
        row(label, s[key])

    head("2. CPU")
    row("Physical cores", cpu["physical"])
    row("Logical cores", cpu["logical"])
    row("Overall usage", f"{cpu['overall']:.1f} %")
    row("Current frequency", fmt_mhz(cpu["freq"]["current"]))
    for c in cpu["cores"]:
        row(f"Core {c['core']}", f"{c['usage']:.1f} %")

    head("3. MEMORY")
    r, sw = mem["ram"], mem["swap"]
    row("RAM total", fmt_bytes(r["total"]))
    row("RAM used", fmt_bytes(r["used"]))
    row("RAM available", fmt_bytes(r["available"]))
    row("RAM usage", f"{r['percent']:.1f} %")
    row("Swap total", fmt_bytes(sw["total"]))
    row("Swap used", fmt_bytes(sw["used"]))
    row("Swap usage", f"{sw['percent']:.1f} %")

    head("4. DISK PARTITIONS")
    for p in disk["partitions"]:
        row(p["mount"], f"Total {fmt_bytes(p['total'])}  Used {fmt_bytes(p['used'])}  "
                        f"Free {fmt_bytes(p['free'])}  ({p['percent']:.1f}%)")

    head("5. NETWORK")
    for i in net["interfaces"]:
        row(i["name"], f"IPv4 {i['ipv4']}  {'UP' if i['up'] else 'DOWN'}")
    row("Total sent", fmt_bytes(net["io"]["bytes_sent"]))
    row("Total received", fmt_bytes(net["io"]["bytes_recv"]))

    head("6. TOP PROCESSES (by CPU)")
    row("Total processes", procs["total"])
    for p in procs["rows"]:
        row(f"PID {p['pid']} {p['name'][:20]}",
            f"CPU={p['cpu']:.1f}%  RAM={p['ram']:.1f}%  Status={p['status']}")

    L += ["", "=" * 72, "  END OF REPORT", "=" * 72, ""]
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════════════
#  API ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")


@app.route("/api/overview")
def api_overview():
    return jsonify(get_overview())


@app.route("/api/live")
def api_live():
    return jsonify(get_live())


@app.route("/api/cpu")
def api_cpu():
    return jsonify(get_cpu())


@app.route("/api/memory")
def api_memory():
    return jsonify(get_memory())


@app.route("/api/disk")
def api_disk():
    return jsonify(get_disk())


@app.route("/api/network")
def api_network():
    return jsonify(get_network())


@app.route("/api/processes")
def api_processes():
    sort = request.args.get("sort", "cpu")
    query = request.args.get("q", "")[:64]
    limit = min(max(request.args.get("limit", 40, type=int), 5), 200)
    return jsonify(list_processes(sort, limit, query))


@app.route("/api/report")
def api_report():
    text = build_report_text()

    if request.args.get("preview"):
        return Response(text, mimetype="text/plain; charset=utf-8")

    REPORT_DIR.mkdir(exist_ok=True)
    fname = f"sysscope_report_{datetime.now():%Y%m%d_%H%M%S}.txt"
    (REPORT_DIR / fname).write_text(text, encoding="utf-8")
    return Response(
        text,
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
#  FRONTEND (HTML + CSS + JS embedded)
# ══════════════════════════════════════════════════════════════════════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="theme-color" content="#0b1020" />
<meta name="color-scheme" content="dark" />
<title>SysScope · System Monitor</title>
<style>
:root {
  --bg: #0b1020; --panel: #121a2e; --panel-2: #182340; --border: #243152;
  --text: #e6edf7; --dim: #8b98b5; --accent: #22d3ee;
  --good: #22c55e; --warn: #facc15; --crit: #ef4444;
  --mem: #c084fc; --disk: #fbbf24; --net: #60a5fa;
  --radius: 14px;
}
*{box-sizing:border-box;margin:0;padding:0}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{
  background:radial-gradient(1200px 600px at 10% -10%,#16234a 0,transparent 60%),var(--bg);
  color:var(--text);min-height:100vh;min-height:100dvh;
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-tap-highlight-color:transparent;
}
button,input{font:inherit;color:inherit}
h3{font-size:15px;font-weight:600;margin-bottom:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
h4{font-size:13px;color:var(--dim);margin:16px 0 8px;font-weight:600}
code{background:#0a0f1d;padding:1px 6px;border-radius:6px;font-size:12px}
.muted{color:var(--dim)}

/* Layout */
.app{display:grid;grid-template-columns:240px minmax(0,1fr);min-height:100dvh}
aside{
  background:var(--panel);border-right:1px solid var(--border);
  padding:22px 14px;position:sticky;top:0;height:100dvh;
  display:flex;flex-direction:column;gap:4px;z-index:60;
}
.brand{font-size:21px;font-weight:800;padding:4px 12px 20px;letter-spacing:.3px}
.brand b{color:var(--accent)}
.nav-btn{
  display:flex;align-items:center;gap:12px;width:100%;padding:12px 14px;
  border:0;border-radius:11px;background:none;color:var(--dim);
  cursor:pointer;text-align:left;transition:background .15s,color .15s;
}
.nav-btn:hover{background:var(--panel-2);color:var(--text)}
.nav-btn.active{background:var(--panel-2);color:var(--text);box-shadow:inset 3px 0 0 var(--accent)}
.nav-btn .ic{width:22px;text-align:center}
.aside-foot{margin-top:auto;color:var(--dim);font-size:12px;padding:12px}
main{padding:22px;min-width:0}
.topbar{display:flex;align-items:center;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.title{font-size:22px;font-weight:700;flex:1;min-width:0}
.pill{
  display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:99px;
  background:var(--panel);border:1px solid var(--border);font-size:12px;color:var(--dim);
  white-space:nowrap;
}
.dot-status{width:8px;height:8px;border-radius:50%;background:var(--good);box-shadow:0 0 10px var(--good)}
.pill.offline .dot-status{background:var(--crit);box-shadow:0 0 10px var(--crit)}
.menu-btn{display:none}
.overlay{display:none}

/* Cards & grids */
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));margin-bottom:16px}
.grid.two{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card{
  background:linear-gradient(180deg,var(--panel-2),var(--panel));
  border:1px solid var(--border);border-radius:var(--radius);
  padding:18px;min-width:0;margin-bottom:16px;
}
.grid > .card{margin-bottom:0}
.stat{display:flex;flex-direction:column;gap:10px}
.stat .label{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.stat .big{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;word-break:break-word}
.big.net-up{font-size:18px;color:var(--good)}
.big.net-down{font-size:18px;color:var(--net)}
.stat .row{display:flex;align-items:center;gap:14px}
.stat .sub{color:var(--dim);font-size:12px}

/* Rings */
.ring{
  position:relative;width:84px;height:84px;border-radius:50%;flex:0 0 auto;
  background:conic-gradient(var(--c) calc(var(--p) * 1%),#1e2a47 0);
  display:grid;place-items:center;--c:var(--good);
}
.ring.warn{--c:var(--warn)}
.ring.crit{--c:var(--crit)}
.ring::after{content:"";position:absolute;inset:8px;border-radius:50%;background:var(--panel)}
.ring span{position:relative;z-index:1;font-weight:700;font-size:15px}

/* Bars */
.bar-wrap{display:flex;align-items:center;gap:10px;min-width:150px}
.bar{flex:1;height:8px;background:#1e2a47;border-radius:99px;overflow:hidden}
.bar > i{display:block;height:100%;border-radius:99px;transition:width .5s ease}
.bar > i.good{background:linear-gradient(90deg,#15803d,var(--good))}
.bar > i.warn{background:linear-gradient(90deg,#a16207,var(--warn))}
.bar > i.crit{background:linear-gradient(90deg,#b91c1c,var(--crit))}
.pct{font-variant-numeric:tabular-nums;font-size:12px;min-width:52px;text-align:right}
.good{color:var(--good)} .warn{color:var(--warn)} .crit{color:var(--crit)}
.trow{display:flex;align-items:center;gap:12px;justify-content:space-between;margin-bottom:12px;font-size:13px}
.trow > span:first-child{min-width:110px;color:var(--dim)}
.trow .bar-wrap{flex:1;min-width:0}

/* Cores */
.cores{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
.core{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:8px;min-width:0}
.core .meta{display:flex;justify-content:space-between;color:var(--dim);font-size:12px}
.core .bar-wrap{min-width:0}

/* Tables */
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:10px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.name{font-weight:600;max-width:240px;overflow:hidden;text-overflow:ellipsis}
tbody tr:hover td{background:rgba(255,255,255,.025)}
.tag{
  display:inline-block;padding:2px 9px;border-radius:99px;font-size:11px;
  background:#1e2a47;color:var(--dim);
}
.tag.running,.tag.ESTABLISHED,.tag.UP{color:var(--good);background:rgba(34,197,94,.12)}
.tag.listen{color:var(--accent);background:rgba(34,211,238,.12)}
.tag.stopped{color:var(--warn);background:rgba(250,204,21,.12)}
.tag.zombie{color:var(--crit);background:rgba(239,68,68,.12)}

/* Key/value lists */
.kv{display:grid;grid-template-columns:minmax(110px,auto) 1fr;gap:10px 16px;font-size:13px}
.kv dt{color:var(--dim)}
.kv dd{text-align:right;word-break:break-word;font-variant-numeric:tabular-nums}

/* Controls */
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
.input{
  flex:1;min-width:180px;padding:11px 14px;border-radius:10px;
  border:1px solid var(--border);background:var(--panel);outline:none;
}
.input:focus{border-color:var(--accent)}
.seg{display:flex;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden;flex-wrap:wrap}
.seg button{padding:10px 14px;border:0;background:none;color:var(--dim);cursor:pointer}
.seg button.on{background:var(--accent);color:#04121a;font-weight:600}
.btn{
  padding:11px 18px;border-radius:10px;border:0;cursor:pointer;
  background:linear-gradient(135deg,#06b6d4,#3b82f6);color:#fff;font-weight:600;
  text-decoration:none;display:inline-flex;align-items:center;gap:8px;
}
.btn.ghost{background:var(--panel);border:1px solid var(--border);color:var(--text)}
.menu-btn{padding:9px 13px;font-size:18px;line-height:1}

/* Alerts, charts, misc */
.alert{padding:10px 12px;border-radius:10px;margin-bottom:8px;font-size:13px;border:1px solid}
.alert.crit{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.35);color:#fecaca}
.alert.warn{background:rgba(250,204,21,.08);border-color:rgba(250,204,21,.3);color:#fef08a}
.alert.ok{background:rgba(34,197,94,.08);border-color:rgba(34,197,94,.3);color:#bbf7d0}
.chart{width:100%;height:180px;display:block}
.legend{display:flex;gap:16px;font-size:12px;color:var(--dim);margin-top:10px;flex-wrap:wrap}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;background:var(--c);margin-right:6px;vertical-align:middle}
.empty{color:var(--dim);padding:12px 0;font-size:13px}
pre{
  background:#0a0f1d;border:1px solid var(--border);border-radius:12px;padding:14px;
  overflow:auto;max-height:60vh;white-space:pre;color:#cbd5e1;
  font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;margin-top:16px;
}

/* Tablets & phones: sidebar becomes a drawer */
@media (max-width: 900px){
  .app{grid-template-columns:1fr}
  aside{
    position:fixed;left:0;top:0;bottom:0;width:272px;height:100dvh;
    transform:translateX(-102%);transition:transform .25s ease;
    box-shadow:0 0 40px rgba(0,0,0,.5);
    padding-top:calc(22px + env(safe-area-inset-top));
    padding-left:calc(14px + env(safe-area-inset-left));
  }
  aside.open{transform:none}
  .overlay.show{display:block;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:55}
  .menu-btn{display:inline-flex}
  main{
    padding:14px 14px calc(24px + env(safe-area-inset-bottom));
    padding-top:calc(14px + env(safe-area-inset-top));
    padding-left:calc(14px + env(safe-area-inset-left));
    padding-right:calc(14px + env(safe-area-inset-right));
  }
  .title{font-size:18px}
  .grid.two{grid-template-columns:1fr}
}

/* Phones */
@media (max-width: 600px){
  body{font-size:13px}
  .hide-sm{display:none!important}
  .grid{gap:12px}
  .grid.stats{grid-template-columns:1fr 1fr;gap:10px}
  .stat .big{font-size:20px}
  .ring{width:66px;height:66px}
  .ring::after{inset:7px}
  .ring span{font-size:13px}
  .card{padding:14px}
  .bar-wrap{min-width:110px}
  th,td{padding:8px 7px}
  .kv{grid-template-columns:1fr;gap:4px 16px}
  .kv dt{margin-top:8px}
  .kv dd{text-align:left}
  .cores{grid-template-columns:1fr 1fr}
  .trow > span:first-child{min-width:80px}
  .pill{font-size:11px;padding:5px 10px}
  .chart{height:150px}
  .toolbar .input{min-width:100%}
  .btn{padding:10px 14px}
}

/* Very small phones */
@media (max-width: 360px){
  .grid.stats{grid-template-columns:1fr}
  .cores{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="app">

  <aside id="sidebar">
    <div class="brand">⚡ Sys<b>Scope</b></div>
    <button class="nav-btn active" data-view="overview"><span class="ic">🏠</span>Overview</button>
    <button class="nav-btn" data-view="cpu"><span class="ic">⚡</span>CPU</button>
    <button class="nav-btn" data-view="memory"><span class="ic">🧠</span>Memory</button>
    <button class="nav-btn" data-view="disk"><span class="ic">💾</span>Disks</button>
    <button class="nav-btn" data-view="network"><span class="ic">🌐</span>Network</button>
    <button class="nav-btn" data-view="processes"><span class="ic">🔢</span>Processes</button>
    <button class="nav-btn" data-view="report"><span class="ic">📄</span>Report</button>
    <div class="aside-foot">Auto-refresh every 2 s</div>
  </aside>
  <div class="overlay" id="overlay"></div>

  <main>
    <div class="topbar">
      <button class="btn ghost menu-btn" id="menuBtn" aria-label="Open menu">☰</button>
      <h1 class="title" id="title">Overview</h1>
      <span class="pill" id="status"><span class="dot-status"></span>
        <span id="statusText">Live</span> · <span id="clock">--:--:--</span></span>
    </div>
    <div id="view"></div>
  </main>
</div>

<script>
(() => {
'use strict';

/* ══════════════ CONFIG & STATE ══════════════ */
const HIST = 60;
const REFRESH_MS = 2000;
const S = { view: 'overview', sort: 'cpu', q: '', info: null };
const hist = { cpu: [], ram: [], up: [], down: [], dr: [], dw: [] };
let timer = null, busy = false;

/* ══════════════ HELPERS ══════════════ */
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = v => Math.max(0, Math.min(100, Number(v) || 0));
const cls = p => p >= 85 ? 'crit' : p >= 60 ? 'warn' : 'good';
const fmtBytes = (b, d = 1) => {
  if (b == null || isNaN(b)) return '—';
  let n = Number(b), i = 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? d : 0)} ${u[i]}`;
};
const fmtRate = b => fmtBytes(b) + '/s';
const fmtMHz = v => !v ? '—' : v >= 1000 ? `${(v / 1000).toFixed(2)} GHz` : `${Math.round(v)} MHz`;
const num = v => v == null ? '—' : Number(v).toLocaleString();
const setHTML = (sel, html) => { const el = $(sel); if (el) el.innerHTML = html; };
const get = async url => {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
};
const kv = rows => rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('');
const bar = p => {
  p = clamp(p);
  const c = cls(p);
  return `<div class="bar-wrap"><div class="bar"><i class="${c}" style="width:${p}%"></i></div>
          <span class="pct ${c}">${p.toFixed(1)}%</span></div>`;
};
const ring = p => {
  p = clamp(p);
  return `<div class="ring ${cls(p)}" style="--p:${p}"><span>${Math.round(p)}%</span></div>`;
};
const tag = (s, c = '') => `<span class="tag ${c || esc(s)}">${esc(s)}</span>`;
const table = (head, body) => `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
const empty = t => `<div class="empty">${t}</div>`;
const pushHist = (k, v) => { const a = hist[k]; a.push(v); if (a.length > HIST) a.shift(); };

/* ══════════════ CHARTS (canvas, DPI-aware) ══════════════ */
function chart(id, series, max) {
  const cv = document.getElementById(id);
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const pad = 10;
  const all = series.flatMap(s => s.data);
  const top = max ?? Math.max(1, ...all) * 1.15;

  g.strokeStyle = 'rgba(139,152,181,.15)';
  g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad + (h - 2 * pad) * i / 4;
    g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
  }

  for (const s of series) {
    const d = s.data, n = d.length;
    if (n < 2) continue;
    const step = w / (HIST - 1);
    g.beginPath();
    d.forEach((v, i) => {
      const x = w - (n - 1 - i) * step;
      const y = h - pad - (Math.min(v, top) / top) * (h - 2 * pad);
      i ? g.lineTo(x, y) : g.moveTo(x, y);
    });
    g.strokeStyle = s.color;
    g.lineWidth = 2;
    g.lineJoin = 'round';
    g.stroke();
    g.lineTo(w, h - pad);
    g.lineTo(w - (n - 1) * step, h - pad);
    g.closePath();
    g.fillStyle = s.color + '22';
    g.fill();
  }
}

function drawCharts() {
  chart('c-cpu', [{ data: hist.cpu, color: '#22d3ee' }, { data: hist.ram, color: '#c084fc' }], 100);
  chart('c-mem', [{ data: hist.ram, color: '#c084fc' }], 100);
  chart('c-net', [{ data: hist.up, color: '#22c55e' }, { data: hist.down, color: '#60a5fa' }]);
  chart('c-disk', [{ data: hist.dr, color: '#fbbf24' }, { data: hist.dw, color: '#f97316' }]);
}

/* ══════════════ SHARED RENDERERS ══════════════ */
function alertsFor(L) {
  const a = [];
  const c = L.cpu.overall, r = L.memory.percent;
  if (c >= 85) a.push(['crit', `CPU critical: ${c.toFixed(1)}%`]);
  else if (c >= 60) a.push(['warn', `CPU high: ${c.toFixed(1)}%`]);
  if (r >= 85) a.push(['crit', `RAM critical: ${r.toFixed(1)}%`]);
  else if (r >= 70) a.push(['warn', `RAM high: ${r.toFixed(1)}%`]);
  if (L.swap.total && L.swap.percent >= 50) a.push(['warn', `Swap in use: ${L.swap.percent.toFixed(1)}%`]);
  for (const d of L.disks) {
    if (d.percent >= 90) a.push(['crit', `Disk ${d.mount} critical: ${d.percent.toFixed(1)}%`]);
    else if (d.percent >= 75) a.push(['warn', `Disk ${d.mount} high: ${d.percent.toFixed(1)}%`]);
  }
  return a.length
    ? a.map(([k, t]) => `<div class="alert ${k}">${esc(t)}</div>`).join('')
    : `<div class="alert ok">✅ All systems normal</div>`;
}

function procTable(rows, compact) {
  if (!rows.length) return empty('No processes to show.');
  const head = compact
    ? `<th>PID</th><th>Name</th><th class="num">CPU</th><th class="num">RAM</th>`
    : `<th>PID</th><th>Name</th><th class="hide-sm">User</th><th>Status</th>
       <th>CPU</th><th>RAM</th><th class="num hide-sm">RSS</th><th class="num hide-sm">Threads</th>`;
  const body = rows.map(r => compact
    ? `<tr><td>${r.pid}</td><td class="name" title="${esc(r.name)}">${esc(r.name)}</td>
         <td class="num ${cls(r.cpu)}">${r.cpu.toFixed(1)}%</td>
         <td class="num ${cls(r.ram)}">${r.ram.toFixed(1)}%</td></tr>`
    : `<tr><td>${r.pid}</td><td class="name" title="${esc(r.name)}">${esc(r.name)}</td>
         <td class="hide-sm">${esc(r.user)}</td><td>${tag(r.status)}</td>
         <td>${bar(r.cpu)}</td><td>${bar(r.ram)}</td>
         <td class="num hide-sm">${fmtBytes(r.rss)}</td><td class="num hide-sm">${r.threads}</td></tr>`
  ).join('');
  return table(head, body);
}

const STATUS_CLS = { ESTABLISHED: 'running', LISTEN: 'listen', TIME_WAIT: 'stopped',
                     CLOSE_WAIT: 'stopped', SYN_SENT: 'stopped', SYN_RECV: 'stopped' };

/* ══════════════ VIEWS ══════════════ */
const VIEWS = {

  overview: {
    title: 'Overview',
    html: () => `
      <div class="grid stats" id="ov-stats"></div>
      <div class="grid two">
        <div class="card"><h3>CPU &amp; memory history</h3>
          <canvas class="chart" id="c-cpu"></canvas>
          <div class="legend"><span><i style="--c:#22d3ee"></i>CPU</span><span><i style="--c:#c084fc"></i>RAM</span></div>
        </div>
        <div class="card"><h3>Network throughput</h3>
          <canvas class="chart" id="c-net"></canvas>
          <div class="legend"><span><i style="--c:#22c55e"></i>Upload</span><span><i style="--c:#60a5fa"></i>Download</span></div>
        </div>
      </div>
      <div class="grid two">
        <div class="card"><h3>⚠ Alerts</h3><div id="ov-alerts"></div></div>
        <div class="card"><h3>🖥 Machine</h3><dl class="kv" id="ov-sys"></dl></div>
      </div>
      <div class="card"><h3>⚡ Top processes by CPU</h3><div class="table-wrap" id="ov-procs"></div></div>`,
    draw: async L => {
      if (!S.info) S.info = await get('/api/overview');
      const s = S.info.system, c = S.info.cpu;
      const d = L.disks[0] || { mount: '—', percent: 0, free: 0 };

      setHTML('#ov-stats', `
        <div class="card stat"><div class="label">CPU</div>
          <div class="row">${ring(L.cpu.overall)}
            <div><div class="big">${L.cpu.overall.toFixed(1)}%</div><div class="sub">${L.cpu.logical} logical cores</div></div>
          </div></div>
        <div class="card stat"><div class="label">Memory</div>
          <div class="row">${ring(L.memory.percent)}
            <div><div class="big">${fmtBytes(L.memory.used)}</div><div class="sub">of ${fmtBytes(L.memory.total)}</div></div>
          </div></div>
        <div class="card stat"><div class="label">Disk · ${esc(d.mount)}</div>
          <div class="row">${ring(d.percent)}
            <div><div class="big">${fmtBytes(d.free)}</div><div class="sub">free space</div></div>
          </div></div>
        <div class="card stat"><div class="label">Network</div>
          <div class="big net-up">↑ ${fmtRate(L.net.up)}</div>
          <div class="big net-down">↓ ${fmtRate(L.net.down)}</div>
          <div class="sub">Uptime ${esc(L.uptime)}</div>
        </div>`);

      setHTML('#ov-alerts', alertsFor(L));
      setHTML('#ov-sys', kv([
        ['OS', esc(s.os)], ['Hostname', esc(s.hostname)], ['Machine', esc(s.machine)],
        ['Processor', esc(s.processor)], ['Local IP', esc(s.local_ip)],
        ['Boot time', esc(s.boot_time)], ['Cores', `${c.physical} physical · ${c.logical} logical`],
      ]));

      const p = await get('/api/processes?sort=cpu&limit=8');
      setHTML('#ov-procs', procTable(p.rows, true));
    },
  },

  cpu: {
    title: 'CPU Analysis',
    html: () => `
      <div class="grid stats" id="cpu-stats"></div>
      <div class="card"><h3>Per-core usage</h3><div class="cores" id="cpu-cores"></div></div>
      <div class="grid two">
        <div class="card"><h3>Time distribution</h3><div id="cpu-times"></div></div>
        <div class="card"><h3>Details</h3>
          <dl class="kv" id="cpu-kv"></dl>
          <h4>Temperatures</h4><div id="cpu-temps"></div>
        </div>
      </div>`,
    draw: async () => {
      const d = await get('/api/cpu');
      setHTML('#cpu-stats', `
        <div class="card stat"><div class="label">Overall</div>
          <div class="row">${ring(d.overall)}<div><div class="big">${d.overall.toFixed(1)}%</div>
          <div class="sub">${d.logical} threads</div></div></div></div>
        <div class="card stat"><div class="label">Frequency</div>
          <div class="big">${fmtMHz(d.freq.current)}</div><div class="sub">max ${fmtMHz(d.freq.max)}</div></div>
        <div class="card stat"><div class="label">Context switches</div>
          <div class="big">${num(d.stats.ctx_switches)}</div><div class="sub">since boot</div></div>
        <div class="card stat"><div class="label">Interrupts</div>
          <div class="big">${num(d.stats.interrupts)}</div><div class="sub">since boot</div></div>`);

      setHTML('#cpu-cores', d.cores.map(c => `
        <div class="core"><div class="meta"><span>Core ${c.core}</span><span>${fmtMHz(c.freq)}</span></div>
        ${bar(c.usage)}</div>`).join(''));

      const LABEL = { user: 'User', system: 'System', iowait: 'I/O wait', irq: 'IRQ' };
      const times = Object.keys(LABEL).filter(k => k in d.times)
        .map(k => `<div class="trow"><span>${LABEL[k]}</span>${bar(d.times[k])}</div>`).join('');
      setHTML('#cpu-times', times || empty('Not available on this OS'));

      setHTML('#cpu-kv', kv([
        ['Physical cores', d.physical], ['Logical cores', d.logical],
        ['Idle', `${(d.times.idle ?? 0).toFixed(1)}%`], ['Max frequency', fmtMHz(d.freq.max)],
      ]));

      setHTML('#cpu-temps', d.temps.length
        ? d.temps.map(t => `<div class="trow"><span>${esc(t.sensor)} · ${esc(t.label)}</span>
            <span class="${t.current >= 85 ? 'crit' : t.current >= 70 ? 'warn' : 'good'}">${t.current.toFixed(1)} °C</span></div>`).join('')
        : empty('No temperature sensors exposed on this OS (common on Windows).'));
    },
  },

  memory: {
    title: 'Memory Analysis',
    html: () => `
      <div class="grid stats" id="m-stats"></div>
      <div class="grid two">
        <div class="card"><h3>RAM breakdown</h3><dl class="kv" id="m-kv"></dl></div>
        <div class="card"><h3>Usage history</h3>
          <canvas class="chart" id="c-mem"></canvas>
          <div class="legend"><span><i style="--c:#c084fc"></i>RAM %</span></div>
        </div>
      </div>
      <div class="card"><h3>🧠 Top memory consumers</h3><div class="table-wrap" id="m-procs"></div></div>`,
    draw: async () => {
      const d = await get('/api/memory');
      const r = d.ram, s = d.swap;
      setHTML('#m-stats', `
        <div class="card stat"><div class="label">RAM</div>
          <div class="row">${ring(r.percent)}<div><div class="big">${fmtBytes(r.used)}</div>
          <div class="sub">of ${fmtBytes(r.total)} · ${fmtBytes(r.available)} free</div></div></div></div>
        <div class="card stat"><div class="label">Swap</div>
          ${s.total
            ? `<div class="row">${ring(s.percent)}<div><div class="big">${fmtBytes(s.used)}</div>
               <div class="sub">of ${fmtBytes(s.total)}</div></div></div>`
            : `<div class="sub">No swap configured</div>`}
        </div>`);
      setHTML('#m-kv', kv([
        ['Total', fmtBytes(r.total)], ['Used', fmtBytes(r.used)], ['Available', fmtBytes(r.available)],
        ['Free', fmtBytes(r.free)], ['Cached', fmtBytes(r.cached)], ['Buffers', fmtBytes(r.buffers)],
      ]));
      setHTML('#m-procs', procTable(d.top, false));
    },
  },

  disk: {
    title: 'Disk Analysis',
    html: () => `
      <div class="card"><h3>💾 Partitions</h3><div class="table-wrap" id="d-parts"></div></div>
      <div class="grid two">
        <div class="card"><h3>I/O totals</h3><dl class="kv" id="d-io"></dl></div>
        <div class="card"><h3>Live throughput</h3>
          <canvas class="chart" id="c-disk"></canvas>
          <div class="legend"><span><i style="--c:#fbbf24"></i>Read</span><span><i style="--c:#f97316"></i>Write</span></div>
        </div>
      </div>
      <div class="card"><h3>Per-disk I/O</h3><div class="table-wrap" id="d-per"></div></div>`,
    draw: async () => {
      const d = await get('/api/disk');
      setHTML('#d-parts', table(
        `<th>Mount</th><th class="hide-sm">Device</th><th class="hide-sm">FS</th>
         <th class="num">Total</th><th class="num">Used</th><th class="num">Free</th><th>Usage</th>`,
        d.partitions.map(p => `<tr>
          <td class="name">${esc(p.mount)}</td><td class="hide-sm">${esc(p.device)}</td>
          <td class="hide-sm">${esc(p.fstype)}</td><td class="num">${fmtBytes(p.total)}</td>
          <td class="num">${fmtBytes(p.used)}</td><td class="num">${fmtBytes(p.free)}</td>
          <td>${p.error ? `<span class="warn">${esc(p.error)}</span>` : bar(p.percent)}</td></tr>`).join('')));

      const io = d.io;
      setHTML('#d-io', io
        ? kv([['Reads', num(io.read_count)], ['Writes', num(io.write_count)],
              ['Bytes read', fmtBytes(io.read_bytes)], ['Bytes written', fmtBytes(io.write_bytes)],
              ['Read time', `${num(io.read_time)} ms`], ['Write time', `${num(io.write_time)} ms`]])
        : empty('I/O counters unavailable on this system'));

      setHTML('#d-per', d.per_disk.length
        ? table(`<th>Disk</th><th class="num">Reads</th><th class="num">Writes</th>
                 <th class="num">Read</th><th class="num">Written</th>`,
            d.per_disk.map(p => `<tr><td class="name">${esc(p.name)}</td><td class="num">${num(p.reads)}</td>
              <td class="num">${num(p.writes)}</td><td class="num">${fmtBytes(p.read_bytes)}</td>
              <td class="num">${fmtBytes(p.write_bytes)}</td></tr>`).join(''))
        : empty('No per-disk data'));
    },
  },

  network: {
    title: 'Network Analysis',
    html: () => `
      <div class="card"><h3>🌐 Interfaces</h3><div class="table-wrap" id="n-if"></div></div>
      <div class="grid two">
        <div class="card"><h3>Traffic totals</h3><dl class="kv" id="n-io"></dl></div>
        <div class="card"><h3>Live traffic</h3>
          <canvas class="chart" id="c-net"></canvas>
          <div class="legend"><span><i style="--c:#22c55e"></i>Upload</span><span><i style="--c:#60a5fa"></i>Download</span></div>
        </div>
      </div>
      <div class="card">
        <h3>Active connections <span class="tag" id="n-count"></span></h3>
        <div id="n-note"></div>
        <div class="table-wrap" id="n-conn"></div>
      </div>`,
    draw: async () => {
      const d = await get('/api/network');
      setHTML('#n-if', table(
        `<th>Interface</th><th>IPv4</th><th class="hide-sm">IPv6</th><th class="hide-sm">MAC</th>
         <th>Status</th><th class="num">Speed</th><th class="num hide-sm">MTU</th>`,
        d.interfaces.map(i => `<tr>
          <td class="name">${esc(i.name)}</td><td>${esc(i.ipv4)}</td>
          <td class="hide-sm">${esc(i.ipv6)}</td><td class="hide-sm">${esc(i.mac)}</td>
          <td>${i.up ? tag('UP', 'running') : tag('DOWN', 'zombie')}</td>
          <td class="num">${i.speed ? i.speed + ' Mbps' : '—'}</td>
          <td class="num hide-sm">${i.mtu ?? '—'}</td></tr>`).join('')));

      const io = d.io;
      setHTML('#n-io', kv([
        ['Sent', fmtBytes(io.bytes_sent)], ['Received', fmtBytes(io.bytes_recv)],
        ['Packets sent', num(io.packets_sent)], ['Packets received', num(io.packets_recv)],
        ['Errors in / out', `${io.errin} / ${io.errout}`], ['Drops in / out', `${io.dropin} / ${io.dropout}`],
      ]));

      setHTML('#n-count', `${d.connections.length} shown`);
      setHTML('#n-note', d.note ? empty(esc(d.note)) : '');
      setHTML('#n-conn', d.connections.length
        ? table(`<th>Proto</th><th>Local</th><th class="hide-sm">Remote</th><th>Status</th><th class="num">PID</th>`,
            d.connections.map(c => `<tr><td>${c.proto}</td><td>${esc(c.local)}</td>
              <td class="hide-sm">${esc(c.remote)}</td>
              <td>${tag(c.status, STATUS_CLS[c.status] || '')}</td>
              <td class="num">${c.pid ?? '—'}</td></tr>`).join(''))
        : '');
    },
  },

  processes: {
    title: 'Process Manager',
    html: () => `
      <div class="toolbar">
        <input id="p-q" class="input" type="search" placeholder="Search by name or PID…" autocomplete="off" />
        <div class="seg" id="p-sort">
          <button data-s="cpu">CPU</button><button data-s="ram">RAM</button>
          <button data-s="name">Name</button><button data-s="pid">PID</button>
        </div>
      </div>
      <div class="grid stats" id="p-sum"></div>
      <div class="card"><h3 id="p-title">Processes</h3><div class="table-wrap" id="p-tbl"></div></div>`,
    bind() {
      const q = $('#p-q');
      q.value = S.q;
      let t;
      q.addEventListener('input', () => {
        clearTimeout(t);
        t = setTimeout(() => { S.q = q.value.trim(); tick(); }, 300);
      });
      const sync = () => document.querySelectorAll('#p-sort button')
        .forEach(b => b.classList.toggle('on', b.dataset.s === S.sort));
      document.querySelectorAll('#p-sort button').forEach(b => b.onclick = () => {
        S.sort = b.dataset.s; sync(); tick();
      });
      sync();
    },
    draw: async () => {
      const d = await get(`/api/processes?sort=${S.sort}&q=${encodeURIComponent(S.q)}&limit=40`);
      const c = d.counts;
      setHTML('#p-sum', [
        ['Total', d.total, ''], ['Running', c.running, 'good'],
        ['Sleeping', c.sleeping, ''], ['Zombie', c.zombie, c.zombie ? 'crit' : ''],
        ['Threads', num(d.threads), ''],
      ].map(([l, v, k]) => `<div class="card stat"><div class="label">${l}</div><div class="big ${k}">${v}</div></div>`).join(''));
      setHTML('#p-title', `Processes · sorted by ${S.sort.toUpperCase()}${S.q ? ` · "${esc(S.q)}"` : ''}`);
      setHTML('#p-tbl', procTable(d.rows, false));
    },
  },

  report: {
    title: 'Full System Report',
    html: () => `
      <div class="card">
        <h3>📄 Full system report</h3>
        <p class="muted">A complete snapshot of OS, CPU, memory, disks, network and top processes.
          Downloads are also saved in the <code>reports/</code> folder on the machine running SysScope.</p>
        <div class="toolbar" style="margin-top:16px">
          <button class="btn ghost" id="rep-prev">👁 Preview</button>
          <a class="btn" href="/api/report" download>⬇ Download report</a>
        </div>
        <pre id="rep-out" hidden></pre>
      </div>`,
    bind() {
      $('#rep-prev').onclick = async () => {
        const btn = $('#rep-prev');
        btn.disabled = true; btn.textContent = 'Generating…';
        try {
          const r = await fetch('/api/report?preview=1', { cache: 'no-store' });
          const pre = $('#rep-out');
          pre.textContent = await r.text();
          pre.hidden = false;
        } finally {
          btn.disabled = false; btn.textContent = '👁 Preview';
        }
      };
    },
  },
};

/* ══════════════ NAVIGATION ══════════════ */
function openMenu() {
  $('#sidebar').classList.add('open');
  $('#overlay').classList.add('show');
}
function closeMenu() {
  $('#sidebar').classList.remove('open');
  $('#overlay').classList.remove('show');
}

function go(name) {
  if (!VIEWS[name]) name = 'overview';
  S.view = name;
  if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  $('#title').textContent = VIEWS[name].title;
  $('#view').innerHTML = VIEWS[name].html();
  VIEWS[name].bind?.();
  closeMenu();
  tick();
}

/* ══════════════ REFRESH LOOP ══════════════ */
function setStatus(ok, msg = '') {
  const p = $('#status');
  p.classList.toggle('offline', !ok);
  p.title = msg;
  $('#statusText').textContent = ok ? 'Live' : 'Offline';
}

async function tick() {
  if (busy) return;
  busy = true;
  try {
    const L = await get('/api/live');
    pushHist('cpu', L.cpu.overall);
    pushHist('ram', L.memory.percent);
    pushHist('up', L.net.up);
    pushHist('down', L.net.down);
    pushHist('dr', L.disk_io.read);
    pushHist('dw', L.disk_io.write);
    $('#clock').textContent = new Date().toLocaleTimeString();
    setStatus(true);

    try {
      await VIEWS[S.view].draw?.(L);
    } catch (e) {
      console.warn('View update failed:', e);
    }
    drawCharts();
  } catch (e) {
    setStatus(false, e.message);
  } finally {
    busy = false;
    clearTimeout(timer);
    timer = setTimeout(tick, REFRESH_MS);
  }
}

/* ══════════════ EVENTS & INIT ══════════════ */
document.querySelectorAll('.nav-btn').forEach(b =>
  b.addEventListener('click', () => go(b.dataset.view)));
$('#menuBtn').addEventListener('click', openMenu);
$('#overlay').addEventListener('click', closeMenu);
window.addEventListener('hashchange', () => {
  const v = location.hash.slice(1);
  if (v !== S.view) go(v);
});
window.addEventListener('resize', drawCharts);

go(location.hash.slice(1) || 'overview');
})();
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    psutil.cpu_percent(percpu=True, interval=None)  # prime counters
    list_processes(limit=1)
    REPORT_DIR.mkdir(exist_ok=True)

    print("\n  ⚡ SysScope Web is running")
    print(f"     PC:     http://localhost:{PORT}")
    if HOST == "0.0.0.0":
        print(f"     Phone:  http://{get_local_ip()}:{PORT}  (same Wi-Fi network)")
        print("     Note: anyone on your network can open this page. "
              "Use SYSSCOPE_HOST=127.0.0.1 to restrict it.\n")

    app.run(host=HOST, port=PORT, threaded=True, debug=False)