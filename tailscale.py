"""
tailit — minimal Tailscale connector.

- Downloads tailscale from pkgs.tailscale.com to /tmp/tailit
- Starts tailscaled (userspace-networking without root)
- Runs: tailscale up --auth-key $TS_KEY --hostname $TS_HOST
         tailscale serve --bg $TS_PORT

Streamlit mode (Main file path = tailit/tailscale.py): auto-starts once per
session and renders live status (CPU/RAM, public IP, tailscale status).

Environment:
  TS_KEY                  required, tskey-auth-...
  TS_HOST                 hostname, default OS hostname
  TS_PORT                 serve port, default 8501
  TS_TAGS                 advertise-tags, e.g. tag:worker
  TS_VERSION              latest or 1.102.3, default latest
  TS_EPHEMERAL            1 → --ephemeral
  TS_ADVERTISE_EXIT_NODE  1 → --advertise-exit-node
  TS_ROUTES               --advertise-routes, e.g. 10.0.0.0/24
  TS_EXTRA_ARGS           extra args for tailscale up
  TS_CLIENT_ID/TS_CLIENT_SECRET  OAuth for stale-node cleanup (Devices:Write)
  TS_PEERS                comma-separated peer URLs for keepalive (probes /_stcore/health, 3xx=ok)
  TS_PEER_INTERVAL        peer ping interval sec, default 300
  TAILIT_GO               1 → download Go, build and run tailit/app, default 1
  GO_VERSION              go version for /tmp/go, default 1.22.8
  GO_MEM_LIMIT            GOMEMLIMIT for go build, default 1GiB
  DATABASE_URL            postgres DSN for Go app
  VALKEY_URL / VALKEY_DB  valkey/redis for Go app cache
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Defaults — keep all user-visible knobs here
# ---------------------------------------------------------------------------

DEFAULTS = {
    "TS_PORT": "8501",
    "TS_VERSION": "latest",
    "TS_HOST_FALLBACK": "tailit",
    "TS_PEER_INTERVAL": "300",
    "TAILIT_GO": "1",
    "GO_VERSION": "1.22.8",
    "GO_MEM_LIMIT": "2GiB",
}

TS_BASE = "https://pkgs.tailscale.com/stable"
GO_TARBALL_BASE = "https://go.dev/dl"
GO_ROOT = os.path.join(tempfile.gettempdir(), "go")
GO_BIN = os.path.join(GO_ROOT, "bin", "go")
GO_APP_SRC = os.path.join(os.path.dirname(__file__), "app")
GO_APP_BIN = os.path.join(tempfile.gettempdir(), "tailit-app")
INSTALL_DIR = os.path.join(tempfile.gettempdir(), "tailit")
BIN_TAR = os.path.join(INSTALL_DIR, "tailscale.tgz")
TAILSCALED_SOCK = os.path.join(INSTALL_DIR, "tailscaled.sock")
TAILSCALED_STATE = os.path.join(INSTALL_DIR, "tailscaled.state")
TAILSCALED_LOG = os.path.join(INSTALL_DIR, "tailscaled.log")

_IS_STREAMLIT_RUN = (
    any(k.startswith("STREAMLIT") for k in os.environ)
    or "streamlit" in " ".join(sys.argv).lower()
    or os.path.exists("/mount/src")
)

# ---------------------------------------------------------------------------
# Pretty logging — structured, timestamped, leveled, colorful on TTY
# ---------------------------------------------------------------------------

LOG_FILE = os.path.join(INSTALL_DIR, "tailit.log")
LOG_LEVEL = os.environ.get("TAILIT_LOG_LEVEL", "info").lower()  # debug|info|warn|error
_LEVELS = {"debug": 10, "info": 20, "ok": 20, "warn": 30, "error": 40}


def _detect_tty() -> bool:
    try:
        out = getattr(sys, "stdout", None)
        isatty = getattr(out, "isatty", None)
        if not callable(isatty):
            return False
        if not isatty():
            return False
    except Exception:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    return os.environ.get("TERM") != "dumb"


_IS_TTY = _detect_tty()
_C = {
    "dim": "\033[2m" if _IS_TTY else "",
    "red": "\033[31m" if _IS_TTY else "",
    "grn": "\033[32m" if _IS_TTY else "",
    "ylw": "\033[33m" if _IS_TTY else "",
    "cyn": "\033[36m" if _IS_TTY else "",
    "bld": "\033[1m" if _IS_TTY else "",
    "rst": "\033[0m" if _IS_TTY else "",
}
_LEVEL_COLOR = {"debug": _C["dim"], "info": _C["cyn"], "ok": _C["grn"], "warn": _C["ylw"], "error": _C["red"]}


def _should_log(level: str) -> bool:
    return _LEVELS.get(level, 20) >= _LEVELS.get(LOG_LEVEL, 20)


def _write_log_file(line: str) -> None:
    try:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log(msg: str, level: str = "info", event: str | None = None, **fields: object) -> None:
    if not _should_log(level):
        return
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    color = _LEVEL_COLOR.get(level, "")
    rst = _C["rst"]
    # Drop key=value tokens from msg that also come as structured fields (no dupes).
    for k in list(fields.keys()) + (["event"] if event else []):
        msg = re.sub(rf"\b{re.escape(k)}=\S+", "", msg).strip()
        msg = re.sub(r"\s{2,}", " ", msg)
    extra = ""
    if event:
        extra += f" event={event}"
    if fields:
        extra += " " + " ".join(f"{k}={json.dumps(v) if isinstance(v, (dict, list)) else v}" for k, v in fields.items())
    # file: plain, stdout: colored [HH:MM:SS]
    file_line = f"[{ts}] {msg}{extra}"
    console_line = f"{color}[{ts}]{rst} {msg}{extra}"
    print(console_line, flush=True)
    _write_log_file(file_line)


def log_ok(msg: str, **kw: object) -> None:
    log(msg, level="ok", **kw)


def log_warn(msg: str, **kw: object) -> None:
    log(msg, level="warn", **kw)


def log_error(msg: str, **kw: object) -> None:
    log(msg, level="error", **kw)


def log_debug(msg: str, **kw: object) -> None:
    log(msg, level="debug", **kw)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def get_auth_key() -> str:
    v = os.environ.get("TS_KEY", "").strip()
    if v:
        log(f"auth key from TS_KEY (len={len(v)})")
    return v


def get_hostname() -> str:
    raw = os.environ.get("TS_HOST", "").strip() or platform.node().split(".")[0] or DEFAULTS["TS_HOST_FALLBACK"]
    return re.sub(r"[^a-z0-9-]", "-", raw.lower())[:63].strip("-") or DEFAULTS["TS_HOST_FALLBACK"]


def get_serve_port() -> int:
    v = os.environ.get("TS_PORT", "").strip()
    return int(v) if v.isdigit() else int(DEFAULTS["TS_PORT"])


def arch_suffix() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("armv7l", "arm"):
        return "arm"
    return "amd64"


def _try_download(url: str, dest: str, timeout: float = 60) -> None:
    log(f"download {url}", level="debug", event="download.start", url=url)
    req = urllib.request.Request(url, headers={"User-Agent": "tailit/1.0"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as out:
        shutil.copyfileobj(r, out)
    log(f"download done {url} in {time.monotonic() - t0:.1f}s", level="debug", event="download.done", url=url)


def fetch_latest_version(timeout: float = 10) -> str:
    try:
        req = urllib.request.Request(f"{TS_BASE}/", headers={"User-Agent": "tailit/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode(errors="replace")
        for m in re.finditer(r'<option value="([^"]+)"', html):
            v = m.group(1).strip()
            if re.match(r"^\d+\.\d+\.\d+$", v):
                log(f"latest version resolved: {v}", level="debug", event="version.resolve", version=v)
                return v
    except Exception as e:  # noqa: BLE001
        log(f"fetch_latest_version failed: {e}", level="warn", event="version.fetch_error", error=str(e)[:120])
    return ""


def _resolve_version(version: str) -> str:
    orig = (version or "").strip() or os.environ.get("TS_VERSION", "").strip() or DEFAULTS["TS_VERSION"]
    if orig.lower() == "latest":
        latest = fetch_latest_version()
        if latest:
            log(f"version latest -> {latest}", event="version.resolve", requested=orig, resolved=latest)
            return latest
        log(f"latest fetch failed, fallback to 1.86.2", level="warn", event="version.fallback")
        return "1.86.2"
    log(f"version pinned: {orig}", level="debug", event="version.pinned", version=orig)
    return orig


# ---------------------------------------------------------------------------
# Tailscale — download / daemon / up / serve
# ---------------------------------------------------------------------------


def ensure_binaries(version: str = DEFAULTS["TS_VERSION"]) -> tuple[str, str]:
    version = _resolve_version(version)
    os.makedirs(INSTALL_DIR, exist_ok=True)
    arch = arch_suffix()
    want = os.path.join(INSTALL_DIR, f"tailscale_{version}_{arch}", "tailscale")
    wantd = os.path.join(INSTALL_DIR, f"tailscale_{version}_{arch}", "tailscaled")
    if os.path.isfile(want) and os.path.isfile(wantd):
        log(f"binaries cached: {want}", event="binaries.cached", version=version, arch=arch)
        return wantd, want
    log(f"binaries not cached, need download", event="binaries.miss", version=version, arch=arch)

    fname = f"tailscale_{version}_{arch}.tgz"
    candidates = [f"{TS_BASE}/{fname}"]
    if version != "1.84.0":
        candidates.append(f"https://pkgs.tailscale.com/stable/tailscale_1.84.0_{arch}.tgz")
    if version != "1.82.0":
        candidates.append(f"https://pkgs.tailscale.com/stable/tailscale_1.82.0_{arch}.tgz")

    last_err: Exception | None = None
    for url in candidates:
        log(f"downloading {url}", event="binaries.download", url=url)
        try:
            os.remove(BIN_TAR)
        except OSError:
            pass
        try:
            _try_download(url, BIN_TAR)
            log_ok(f"downloaded {url}", event="binaries.downloaded", url=url)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            log_warn(f"download failed {url}: {e}", event="binaries.download_failed", url=url, error=str(e)[:150])
    else:
        log_error(f"all mirrors failed (last: {last_err})", event="binaries.failed")
        raise RuntimeError(f"download failed (last: {last_err})")

    log(f"extracting {BIN_TAR} -> {INSTALL_DIR}", event="binaries.extract")
    with tarfile.open(BIN_TAR, "r:gz") as tf:
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in m.name:
                raise RuntimeError(f"unsafe path in tar: {m.name}")
        tf.extractall(INSTALL_DIR)
    try:
        os.remove(BIN_TAR)
    except OSError:
        pass

    if not (os.path.isfile(want) and os.path.isfile(wantd)):
        import glob as _glob

        cand = _glob.glob(os.path.join(INSTALL_DIR, "tailscale_*", "tailscale"))
        if cand:
            want = cand[0]
            wantd = os.path.join(os.path.dirname(want), "tailscaled")

    for p in (want, wantd):
        if os.path.isfile(p):
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if not (os.path.isfile(want) and os.path.isfile(wantd)):
        log_error("extraction did not produce binaries", event="binaries.extract_failed")
        raise RuntimeError("extraction did not produce binaries")

    log_ok(f"binaries ready: {want}", event="binaries.ready", version=version)
    return wantd, want


def is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def wait_sock(path: str, timeout: float = 10) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if os.path.exists(path):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(path)
                    return True
            except OSError:
                pass
        time.sleep(0.2)
    return False


def _kill_daemon() -> None:
    log("killing old tailscaled", event="daemon.kill")
    for cmd in (["pkill", "-f", "tailscaled"], ["killall", "tailscaled"]):
        try:
            subprocess.run(cmd, timeout=3, capture_output=True)
        except Exception:
            pass
    try:
        import glob as _g

        for pid in _g.glob("/proc/[0-9]*/cmdline"):
            try:
                with open(pid, "rb") as f:
                    if b"tailscaled" in f.read():
                        os.kill(int(pid.split("/")[2]), 15)
            except Exception:
                pass
    except Exception:
        pass
    time.sleep(1.5)
    try:
        os.remove(TAILSCALED_SOCK)
    except OSError:
        pass
    log("old tailscaled killed", event="daemon.killed")


def ensure_daemon(tailscaled_bin: str) -> subprocess.Popen | None:
    if os.path.exists(TAILSCALED_SOCK) and wait_sock(TAILSCALED_SOCK, timeout=2):
        try:
            import glob as _g

            existing = _g.glob(os.path.join(INSTALL_DIR, "tailscale_*", "tailscaled"))
            if existing and os.path.abspath(existing[0]) != os.path.abspath(tailscaled_bin):
                log(f"binary changed, restarting daemon", event="daemon.restart", old=existing[0], new=tailscaled_bin)
                _kill_daemon()
            else:
                log(f"tailscaled already listening {TAILSCALED_SOCK}", event="daemon.reuse", sock=TAILSCALED_SOCK)
                return None
        except Exception:
            log(f"tailscaled already listening {TAILSCALED_SOCK}", event="daemon.reuse")
            return None

    os.makedirs(INSTALL_DIR, exist_ok=True)
    logf = open(TAILSCALED_LOG, "ab")
    cmd = [tailscaled_bin, "--socket", TAILSCALED_SOCK, "--state", TAILSCALED_STATE, "--port", "41641"]
    if not is_root():
        cmd += ["--tun", "userspace-networking"]
        log("starting without root: --tun userspace-networking", event="daemon.start", mode="userspace")
    else:
        log("starting as root", event="daemon.start", mode="root")

    try:
        os.remove(TAILSCALED_SOCK)
    except OSError:
        pass

    log(f"starting: {' '.join(cmd)}", event="daemon.exec", cmd=" ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    if not wait_sock(TAILSCALED_SOCK, timeout=15):
        try:
            with open(TAILSCALED_LOG, "rb") as f:
                print(f.read()[-2000:].decode(errors="replace"))
        except OSError:
            pass
        log_error("tailscaled did not start in 15s", event="daemon.start_failed", log=TAILSCALED_LOG)
        raise RuntimeError("tailscaled did not start in 15s — see " + TAILSCALED_LOG)
    log_ok(f"tailscaled started pid={proc.pid} sock={TAILSCALED_SOCK}", event="daemon.started", pid=proc.pid, sock=TAILSCALED_SOCK)
    return proc


def run_tailscale(tailscale_bin: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TAILSCALED_SOCKET"] = TAILSCALED_SOCK
    cmd = [tailscale_bin, "--socket", TAILSCALED_SOCK] + list(args)
    shown: list[str] = []
    hide_next = False
    for a in cmd:
        if hide_next:
            shown.append("***")
            hide_next = False
        elif a == "--auth-key":
            shown.append(a)
            hide_next = True
        else:
            shown.append(a)
    log(f"$ {' '.join(shown)}", level="debug", event="tailscale.exec", cmd=" ".join(shown))
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    lvl = "ok" if res.returncode == 0 else "error"
    log(f"tailscale {' '.join(args[:1])} exit={res.returncode}", level=lvl, event="tailscale.done", args=" ".join(args[:2]), exit=res.returncode)
    return res


def do_up(tailscale_bin: str, extra_args: list[str] | None = None) -> None:
    key = get_auth_key()
    if not key:
        log_error("TS_KEY is not set", event="up.missing_key")
        raise SystemExit("TS_KEY is not set")

    log(f"tailscale up hostname={get_hostname()}", event="up.start", hostname=get_hostname())
    args = ["up", "--auth-key", key, "--hostname", get_hostname()]

    tags = os.environ.get("TS_TAGS", "").strip()
    if tags:
        args += ["--advertise-tags", tags.replace(",", " ").strip()]
    if env_flag("TS_EPHEMERAL"):
        args += ["--ephemeral"]
    if env_flag("TS_ADVERTISE_EXIT_NODE"):
        args += ["--advertise-exit-node"]

    routes = os.environ.get("TS_ROUTES", "").strip()
    if routes:
        args += ["--advertise-routes", routes.replace(" ", ",").strip(",")]
    args += ["--accept-dns=false"]

    if extra_args:
        args += extra_args
    if os.environ.get("TS_EXTRA_ARGS"):
        args += os.environ["TS_EXTRA_ARGS"].split()

    res = run_tailscale(tailscale_bin, *args)
    out = (res.stdout or "") + (res.stderr or "")
    print(out)
    if res.returncode != 0:
        log_error(f"tailscale up failed (exit {res.returncode})", event="up.failed", exit=res.returncode)
        raise SystemExit(f"tailscale up failed (exit {res.returncode})")
    log_ok(f"tailscale up done hostname={get_hostname()}", event="up.done", hostname=get_hostname())


def do_serve(tailscale_bin: str, port: int) -> None:
    log(f"tailscale serve port={port}", event="serve.start", port=port)
    last_out = ""
    for attempt in range(5):
        res = run_tailscale(tailscale_bin, "serve", "--bg", str(port))
        out = (res.stdout or "") + (res.stderr or "")
        print(out)
        last_out = out
        if res.returncode == 0:
            log_ok(f"tailscale serve started on :{port}", event="serve.done", port=port)
            break
        if "etag mismatch" in out or "Another client" in out:
            log_warn(f"serve busy, retry {attempt + 2}/5 in 3s", event="serve.retry", attempt=attempt + 1)
            time.sleep(3)
            continue
        log_error(f"tailscale serve failed (exit {res.returncode})", event="serve.failed", exit=res.returncode)
        raise SystemExit(f"tailscale serve failed (exit {res.returncode})")
    else:
        log_error(f"tailscale serve failed: {last_out.strip()[-200:]}", event="serve.failed")
        raise SystemExit(f"tailscale serve failed: {last_out.strip()[-200:]}")

    res2 = run_tailscale(tailscale_bin, "serve", "status")
    print(res2.stdout or res2.stderr or "")


def main(argv: list[str] | None = None) -> None:
    t0 = time.monotonic()
    ap = argparse.ArgumentParser(description="tailit: tailscale up + serve")
    ap.add_argument("--serve", type=int, default=None, help="serve port (default: $TS_PORT|8501)")
    ap.add_argument("--hostname", default=None, help="hostname in tailnet (default: $TS_HOST)")
    ap.add_argument("--tags", default=None, help="advertise-tags (default: $TS_TAGS)")
    ap.add_argument("--no-up", action="store_true", help="skip tailscale up (daemon + serve only)")
    ap.add_argument("--version", default=None, help="tailscale version or latest (default: $TS_VERSION|latest)")
    ap.add_argument("extra_up_args", nargs=argparse.REMAINDER, help="extra args for tailscale up after --")
    args = ap.parse_args(argv)

    if args.hostname:
        os.environ["TS_HOST"] = args.hostname
    if args.tags:
        os.environ["TS_TAGS"] = args.tags

    port = args.serve or get_serve_port()
    version = args.version or os.environ.get("TS_VERSION", "").strip() or DEFAULTS["TS_VERSION"]
    log(f"main start port={port} version={version} hostname={get_hostname()}", event="main.start", port=port, version=version, hostname=get_hostname())

    tailscaled_bin, tailscale_bin = ensure_binaries(version)
    ensure_daemon(tailscaled_bin)

    if not args.no_up:
        cleanup_stale_host(get_hostname())
        do_up(tailscale_bin, extra_args=args.extra_up_args if args.extra_up_args else None)
        res = run_tailscale(tailscale_bin, "status", "--peers=false")
        print(res.stdout or res.stderr or "")

    do_serve(tailscale_bin, port)
    go_status = maybe_run_go_app()
    log_ok(f"serve listening on :{port} — tailnet: https://<hostname>.<tailnet>.ts.net/ in {time.monotonic() - t0:.1f}s", event="main.done", port=port, elapsed=round(time.monotonic() - t0, 1), go=go_status)


# ---------------------------------------------------------------------------
# System helpers — CPU / RAM / version / IP / geo
# ---------------------------------------------------------------------------


def _installed_version() -> str:
    import glob as _g

    cands = _g.glob(os.path.join(INSTALL_DIR, "tailscale_*", "tailscale"))
    if not cands:
        return ""
    m = re.search(r"tailscale_(\d+\.\d+\.\d+)_", cands[0])
    if m:
        return m.group(1)
    try:
        r = run_tailscale(cands[0], "version")
        mm = re.search(r"(\d+\.\d+\.\d+)", (r.stdout or "") + (r.stderr or ""))
        if mm:
            return mm.group(1)
    except Exception:
        pass
    return ""


def _tailscale_ip() -> str:
    import glob as _g

    cands = _g.glob(os.path.join(INSTALL_DIR, "tailscale_*", "tailscale"))
    if not cands:
        return ""
    try:
        r = run_tailscale(cands[0], "ip", "-4")
        lines = (r.stdout or "").strip().splitlines()
        return lines[0].strip() if lines and r.returncode == 0 else ""
    except Exception:
        return ""


def _read_mem_mb() -> tuple[int, int]:
    total = avail = 0
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            k = k.strip()
            if k == "MemTotal":
                total = int(v.strip().split()[0]) // 1024
            elif k == "MemAvailable":
                avail = int(v.strip().split()[0]) // 1024
            if total and avail:
                break
    return total, avail


def _read_cpu_times() -> tuple[int, int]:
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    nums = [int(x) for x in parts]
    return sum(nums), nums[3] + nums[4]


def _cpu_pct() -> float | None:
    try:
        t1, i1 = _read_cpu_times()
        time.sleep(0.4)
        t2, i2 = _read_cpu_times()
    except OSError:
        return None
    dt, di = t2 - t1, i2 - i1
    if dt <= 0:
        return None
    return round((dt - di) / dt * 100, 1)


def _fmt_mb(mb: int) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


# ---------------------------------------------------------------------------
# OAuth — stale-node cleanup
# ---------------------------------------------------------------------------


def _oauth_token() -> str:
    import base64
    import json

    cid = os.environ.get("TS_CLIENT_ID", "").strip()
    csec = os.environ.get("TS_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        return ""
    scope = os.environ.get("TS_OAUTH_SCOPE", "").strip() or "devices:write"
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    last_err = ""
    for sc in (scope, "all:write"):
        try:
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": sc}).encode()
            req = urllib.request.Request(
                "https://api.tailscale.com/api/v2/oauth/token",
                data=body,
                method="POST",
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                tok = json.loads(r.read().decode()).get("access_token", "")
                if tok:
                    log(f"oauth token acquired (scope={sc})")
                    return tok
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:150]
    log(f"oauth token failed: {last_err}")
    return ""


def cleanup_stale_host(hostname: str) -> None:
    import json

    if not os.environ.get("TS_CLIENT_ID", "").strip() or not os.environ.get("TS_CLIENT_SECRET", "").strip():
        log("oauth: TS_CLIENT_ID/TS_CLIENT_SECRET not set — skip stale cleanup")
        return
    token = _oauth_token()
    if not token:
        return

    tailnet = os.environ.get("TS_TAILNET", "").strip() or "-"
    base = f"https://api.tailscale.com/api/v2/tailnet/{tailnet}/devices"

    def _api(url: str, method: str = "GET") -> tuple[int, str]:
        req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")[:500]

    code, body = _api(base)
    if code != 200:
        log(f"devices list failed: HTTP {code}")
        return

    try:
        devices = json.loads(body).get("devices", [])
    except Exception:
        devices = []

    victims = [(d.get("nodeId", ""), d.get("hostname", "")) for d in devices if _is_stale_match(d.get("hostname", ""), hostname)]
    for node_id, h in victims:
        if not node_id:
            continue
        c, _ = _api(f"https://api.tailscale.com/api/v2/device/{node_id}", method="DELETE")
        log(f"deleted stale node {h} ({node_id}): HTTP {c}")

    if victims:
        log("waiting 5s for control plane to apply")
        time.sleep(5)


def _is_stale_match(h: str, base: str) -> bool:
    return h == base or (h.startswith(base + "-") and h[len(base) + 1 :].isdigit())


# ---------------------------------------------------------------------------
# Peer keepalive + geo
# ---------------------------------------------------------------------------


def _go_port() -> int:
    """Go sidecar port = TS_PORT+1 (GO_PORT overrides)."""
    base = get_serve_port()
    v = os.environ.get("GO_PORT", "").strip()
    if v.isdigit() and 0 < int(v) < 65535:
        return int(v)
    return base + 1 if base + 1 < 65535 else base


class _Tee:
    """Write to several streams at once (buffer + real stdout)."""

    def __init__(self, *streams: object) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for s in self.streams:
            try:
                s.write(data)  # type: ignore[attr-defined]
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for s in self.streams:
            try:
                s.flush()  # type: ignore[attr-defined]
            except Exception:
                pass


def _fetch_geoip(timeout: float = 8) -> dict:
    import json
    import ssl

    req = urllib.request.Request(
        "https://api.ip.sb/geoip",
        headers={"User-Agent": "tailit/1.0", "Accept": "application/json"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------------------
# Go — download to /tmp/go and build tailit/app with 1GiB limit
# ---------------------------------------------------------------------------


def _go_arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("armv7l", "arm", "armv6l"):
        return "armv6l"
    return "amd64"


def ensure_go(version: str | None = None) -> str:
    """Ensure Go is available at /tmp/go/bin/go. Returns path to `go` binary.
    On hosts with system Go (e.g. local dev), reuse it to avoid 100MB download;
    on Streamlit Cloud (no system Go) it downloads to /tmp/go as requested."""
    ver = (version or os.environ.get("GO_VERSION", "").strip() or DEFAULTS["GO_VERSION"]).lstrip("go")
    go_bin = GO_BIN
    if os.path.isfile(go_bin):
        try:
            r = subprocess.run([go_bin, "version"], capture_output=True, text=True, timeout=5)
            if ver in (r.stdout or ""):
                log(f"go cached: {r.stdout.strip()}", event="go.cached", version=ver)
                return go_bin
            log(f"go version mismatch, re-downloading", event="go.mismatch", have=r.stdout.strip(), want=ver)
        except Exception:
            pass
        try:
            shutil.rmtree(GO_ROOT)
        except OSError:
            pass
    else:
        # No /tmp/go yet — check system Go to avoid download on dev machines
        for sys_go in ["/usr/local/go/bin/go", "/opt/go/bin/go", shutil.which("go") or ""]:
            if sys_go and os.path.isfile(sys_go):
                try:
                    r = subprocess.run([sys_go, "version"], capture_output=True, text=True, timeout=5)
                    # Accept any 1.22+ for now; exact version check is best-effort
                    if r.returncode == 0 and "go1." in r.stdout:
                        log(f"using system go: {r.stdout.strip()} (requested {ver})", event="go.system", path=sys_go)
                        return sys_go
                except Exception:
                    pass

    arch = _go_arch()
    tarball = f"go{ver}.linux-{arch}.tar.gz"
    url = f"{GO_TARBALL_BASE}/{tarball}"
    dest = os.path.join(tempfile.gettempdir(), tarball)
    log(f"downloading go {ver} [{arch}] from {url}", event="go.download", version=ver, arch=arch, url=url)
    _try_download(url, dest, timeout=120)
    log(f"extracting go to {GO_ROOT}", event="go.extract")
    # Go tarball contains top-level `go/` dir
    if os.path.isdir(GO_ROOT):
        shutil.rmtree(GO_ROOT)
    with tarfile.open(dest, "r:gz") as tf:
        tf.extractall(tempfile.gettempdir())
    try:
        os.remove(dest)
    except OSError:
        pass
    if not os.path.isfile(go_bin):
        raise RuntimeError(f"go binary not found at {go_bin} after extract")
    os.chmod(go_bin, os.stat(go_bin).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    r = subprocess.run([go_bin, "version"], capture_output=True, text=True, timeout=5)
    log_ok(f"go ready: {r.stdout.strip()}", event="go.ready", version=ver)
    return go_bin


def build_go_app(src_dir: str | None = None, output: str | None = None, mem_limit: str | None = None) -> str:
    """Build Go module in src_dir to output with GOMEMLIMIT=1GiB. Returns output path."""
    src = src_dir or GO_APP_SRC
    out = output or GO_APP_BIN
    mem = mem_limit or os.environ.get("GO_MEM_LIMIT", "").strip() or DEFAULTS["GO_MEM_LIMIT"]
    # Resolve src: Streamlit Cloud clones to /mount/src/tailit/app
    candidates = [src, os.path.join(os.getcwd(), "tailit", "app"), "/mount/src/tailit/app", os.path.join(INSTALL_DIR, "..", "tailit", "app")]
    real_src = None
    for c in candidates:
        if os.path.isfile(os.path.join(c, "go.mod")):
            real_src = c
            break
    if not real_src:
        # Also try tailit/app relative to this file's parent
        alt = os.path.join(os.path.dirname(__file__), "app")
        if os.path.isfile(os.path.join(alt, "go.mod")):
            real_src = alt
    if not real_src or not os.path.isfile(os.path.join(real_src, "go.mod")):
        raise FileNotFoundError(f"Go module not found: tried {candidates + [src]}")

    go_bin = ensure_go()
    env = dict(os.environ)
    env["GOMEMLIMIT"] = mem
    env["GOTOOLCHAIN"] = "local"
    # Ensure /tmp/go/bin is in PATH for `go` toolchain lookup
    env["PATH"] = os.path.join(GO_ROOT, "bin") + ":" + env.get("PATH", "")
    # Limit parallelism to reduce memory spikes
    env["GOMAXPROCS"] = env.get("GOMAXPROCS", "2")

    log(f"building Go app {real_src} -> {out} (GOMEMLIMIT={mem})", event="go.build.start", src=real_src, out=out, mem=mem)
    # Tidy first (best effort)
    subprocess.run([go_bin, "mod", "tidy"], cwd=real_src, env=env, capture_output=True, timeout=60)
    t0 = time.monotonic()
    # Use -trimpath -ldflags="-s -w" for smaller binary
    cmd = [go_bin, "build", "-trimpath", "-ldflags", "-s -w", "-o", out, "."]
    # Alternative: limit via `go build -p 2` (parallelism)
    if env["GOMAXPROCS"].isdigit():
        cmd = [go_bin, "build", "-p", env["GOMAXPROCS"], "-trimpath", "-ldflags", "-s -w", "-o", out, "."]
    res = subprocess.run(cmd, cwd=real_src, env=env, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        log_error(f"go build failed", event="go.build.failed", stderr=(res.stderr or "")[-2000:], stdout=(res.stdout or "")[-500:])
        raise RuntimeError(f"go build failed: {res.stderr[:500]}")
    os.chmod(out, os.stat(out).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log_ok(f"Go app built in {time.monotonic() - t0:.1f}s: {out}", event="go.build.done", out=out, elapsed=round(time.monotonic() - t0, 1))
    return out


def run_go_app(bin_path: str | None = None, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    """Run the built Go app in background. Returns Popen. Env is forwarded from current."""
    p = bin_path or GO_APP_BIN
    if not os.path.isfile(p):
        p = build_go_app()
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    # Forward postgres/valkey config — Go reads DATABASE_URL / VALKEY_URL
    log(f"starting Go app: {p} (port={env.get('TS_PORT', env.get('PORT', '8080'))})", event="go.run", bin=p)
    proc = subprocess.Popen([p], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    # Give it a moment and check it didn't crash immediately
    time.sleep(1.5)
    if proc.poll() is not None:
        out = proc.stdout.read() if proc.stdout else ""
        log_error(f"Go app exited early code={proc.returncode}: {out[:1000]}", event="go.run.failed", code=proc.returncode)
        raise RuntimeError(f"Go app failed to start: {out[:500]}")
    log_ok(f"Go app running pid={proc.pid}", event="go.running", pid=proc.pid)
    return proc


def _go_enabled() -> bool:
    v = os.environ.get("TAILIT_GO", "").strip().lower() or DEFAULTS.get("TAILIT_GO", "0").lower()
    return v in ("1", "true", "yes", "on")


def _write_go_status(status: str) -> None:
    try:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        with open(os.path.join(INSTALL_DIR, "go.status"), "w") as f:
            f.write(status)
    except OSError:
        pass


def maybe_run_go_app() -> str:
    """Download Go, build tailit/app and run it when TAILIT_GO=1. Never fails main."""
    if not _go_enabled():
        status = "disabled (TAILIT_GO=0)"
        log(f"Go app {status}", event="go.disabled")
        _write_go_status(status)
        return status
    try:
        go_bin = ensure_go()
        app_bin = build_go_app()
        proc = run_go_app(app_bin)
        status = f"running pid={proc.pid} go={go_bin}"
        _write_go_status(status)
        return status
    except Exception as e:  # noqa: BLE001
        status = f"failed: {e}"[:200]
        log_error(f"Go app step failed: {e}", event="go.failed", error=str(e)[:200])
        _write_go_status(status)
        return status


# ---------------------------------------------------------------------------
# Streamlit app
# ---------------------------------------------------------------------------


def _run_as_streamlit_app() -> None:
    import streamlit as st

    st.set_page_config(page_title="tailit", layout="centered")
    st.title("tailit")

    # Live CPU / RAM
    try:
        auto_metrics = st.fragment(run_every=2)
    except TypeError:
        auto_metrics = st.fragment

    @auto_metrics
    def _live_metrics() -> None:
        try:
            total_mb, avail_mb = _read_mem_mb()
            used_mb = total_mb - avail_mb
            ram_pct = round(used_mb / total_mb * 100, 1) if total_mb else 0.0
            ram_line = f"{_fmt_mb(used_mb)} / {_fmt_mb(total_mb)} ({ram_pct}%) — free {_fmt_mb(avail_mb)}"
        except OSError:
            ram_line = "unknown"
        cpu = _cpu_pct()
        cpu_line = f"{cpu}%" if cpu is not None else "unknown"
        c1, c2 = st.columns(2)
        c1.metric("CPU", cpu_line)
        c2.metric("RAM", ram_line)

    _live_metrics()

    # Live public IP / geo
    try:
        auto_geo = st.fragment(run_every=60)
    except TypeError:
        auto_geo = st.fragment

    @auto_geo
    def _live_geo() -> None:
        now = time.monotonic()
        cached = st.session_state.get("tailit_geo") or {}
        ts = st.session_state.get("tailit_geo_ts", 0.0)
        if not cached or now - ts > 60:
            try:
                data = _fetch_geoip()
                st.session_state["tailit_geo"] = data
                st.session_state["tailit_geo_ts"] = now
            except Exception:  # noqa: BLE001
                data = cached
        else:
            data = cached
        if not data:
            st.metric("Public IP", "unknown")
            return
        country = f"{data.get('country', '')} {data.get('country_code', '')}".strip() or "unknown"
        isp = (data.get("isp", "") or data.get("organization", "")) or "unknown"
        asn = f"AS{data['asn']}" if data.get("asn") else "unknown"
        g1, g2 = st.columns(2)
        g1.metric("Public IP", str(data.get("ip", "unknown")))
        g2.metric("Country", country)
        g3, g4 = st.columns(2)
        g3.metric("ISP", str(isp)[:32])
        g4.metric("ASN", asn)
        tz = str(data.get("timezone", "") or "").strip()
        lat, lon = data.get("latitude", ""), data.get("longitude", "")
        extra = " / ".join(p for p in [tz, f"{lat}, {lon}" if lat != "" and lon != "" else ""] if p)
        if extra:
            st.caption(extra)

    _live_geo()

    # Peer keepalive — owned by the Go sidecar (TS_PORT+1); here we only display.
    try:
        auto_peers = st.fragment(run_every=60)
    except TypeError:
        auto_peers = st.fragment

    @auto_peers
    def _live_peers() -> None:
        import json as _json

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{_go_port()}/peers", headers={"User-Agent": "tailit/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = _json.loads(r.read().decode())
        except Exception:
            st.write("peers: go service not running (see log)")
            return
        rows = data.get("peers", []) if isinstance(data, dict) else []
        if not rows:
            st.write(f"peers: no TS_PEERS configured (interval {data.get('interval_sec', '?')}s)")
            return
        st.write(
            "peers: "
            + " | ".join(
                f"{p.get('url')} {p.get('status')} {p.get('state')} {p.get('latency_ms')}ms"
                for p in rows
            )
        )

    _live_peers()

    # Tailscale — auto start once per session with file lock
    if not get_auth_key():
        st.write("TS_KEY: missing")
        st.write("Set TS_KEY in Secrets.")
        return

    if "tailit_started" not in st.session_state:
        st.session_state["tailit_started"] = True
        import contextlib
        import io

        os.makedirs(INSTALL_DIR, exist_ok=True)
        lock_path = os.path.join(INSTALL_DIR, "started.lock")
        result_path = os.path.join(INSTALL_DIR, "started.result")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            is_leader = True
        except FileExistsError:
            is_leader = False

        buf = io.StringIO()
        if is_leader:
            # Tee: everything main() prints goes BOTH to the UI buffer and to
            # the real process stdout/stderr, so Streamlit Cloud logs show it live.
            real_out, real_err = sys.stdout, sys.stderr
            try:
                with contextlib.redirect_stdout(_Tee(buf, real_out)), contextlib.redirect_stderr(
                    _Tee(buf, real_err)
                ):
                    main([])
                rc = 0
            except SystemExit as e:
                buf.write(f"\nexit {e.code}")
                print(f"exit {e.code}", file=real_out, flush=True)
                rc = e.code if isinstance(e.code, int) else 1
            except Exception as e:  # noqa: BLE001
                buf.write(f"\nerror: {e}")
                print(f"error: {e}", file=real_out, flush=True)
                rc = 1
            try:
                with open(result_path, "w") as f:
                    f.write(f"{rc}\n{buf.getvalue()}")
            except OSError:
                pass
            st.session_state["tailit_log"] = buf.getvalue()
            st.session_state["tailit_rc"] = rc
        else:
            t0 = time.monotonic()
            data = ""
            while time.monotonic() - t0 < 150:
                try:
                    with open(result_path) as f:
                        data = f.read()
                    if data:
                        break
                except OSError:
                    pass
                time.sleep(1)
            if data:
                first, _, rest = data.partition("\n")
                st.session_state["tailit_rc"] = int(first.strip() or "1")
                st.session_state["tailit_log"] = rest
            else:
                st.session_state["tailit_log"] = "timeout waiting for leader"
                st.session_state["tailit_rc"] = 1

    rc = st.session_state.get("tailit_rc", 1)
    installed = _installed_version()
    latest = fetch_latest_version()
    py_version = platform.python_version()
    try:
        with open(os.path.join(INSTALL_DIR, "go.status")) as _gof:
            go_status = _gof.read().strip() or "unknown"
    except OSError:
        go_status = "unknown"

    s1, s2, s3 = st.columns(3)
    s1.metric("Python", py_version)
    if installed and latest and installed != latest:
        s2.metric("Tailscale", installed, "update available")
    else:
        s2.metric("Tailscale", installed or "unknown")
    s3.metric("Status", "running" if rc == 0 else "error")
    st.caption(f"{get_hostname()} · {_tailscale_ip() or 'unknown'} · :{get_serve_port()} · go: {go_status}")
    st.code((st.session_state.get("tailit_log") or "")[-4000:] or "(empty)")


if __name__ == "__main__":
    _is_tailit_main = os.path.basename(sys.argv[0]).endswith("tailscale.py")
    _has_cli_flag = any(
        a.startswith("--serve") or a.startswith("--hostname") or a.startswith("--tags") or a == "--no-up"
        for a in sys.argv[1:]
    )
    if _IS_STREAMLIT_RUN and _is_tailit_main and not _has_cli_flag:
        _run_as_streamlit_app()
    else:
        main()
