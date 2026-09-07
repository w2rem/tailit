# tailit/app

Go sidecar for `tailit`. Built on demand to `/tmp/tailit-app` with `GOMEMLIMIT=2GiB`.

## What it does

- Listens on `TS_PORT+1` (`GO_PORT` overrides), e.g. `TS_PORT=9978` → `:9979`.
- Round-robin pings `TS_PEERS` every `TS_PEER_INTERVAL` (min 60s): one peer per tick.
- Each check reads the full body and classifies `awake | starting | asleep | down`.
- `/ready` is 503 until the first full sweep, then 200 — peers pinging you know you finished startup.

## Build

```bash
# via Python helper (downloads Go to /tmp/go if needed, respects 2GiB)
python3 -c "import tailit.tailscale; print(tailit.tailscale.ensure_go())"
python3 -c "import tailit.tailscale; print(tailit.tailscale.build_go_app())"
# → /tmp/tailit-app

# manual with system Go
GOMEMLIMIT=2GiB GOMAXPROCS=2 go build -p 2 -trimpath -ldflags="-s -w" -o /tmp/tailit-app .
```

- Go tarball: `https://go.dev/dl/go1.22.8.linux-amd64.tar.gz` → `/tmp/go`
- System Go at `/usr/local/go/bin/go` is reused if present (avoids download on dev).

## Run

```bash
TS_PORT=9978 TS_PEERS="https://worker-2.streamlit.app" TS_PEER_INTERVAL=300 \
DATABASE_URL=postgres://... VALKEY_URL=redis://... /tmp/tailit-app &
curl http://127.0.0.1:9979/health
# {"status":"ok","time":"2026-09-07T20:22:45Z"}
curl http://127.0.0.1:9979/peers
# {"interval_sec":60,"peers":[{"url":"...","status":303,"latency_ms":759,"ok":true,"state":"awake","at":"20:23:59"}]}
```

- Reads `DATABASE_URL`, `VALKEY_URL`, `TS_PORT`/`GO_PORT`, `TS_PEERS`, `TS_PEER_INTERVAL` from env (Streamlit Secrets).
- Endpoints: `GET /`, `GET /health`, `GET /ready`, `GET /peers`

## Adding proxy checks

`go.mod` is stdlib-only by default. When you need real checks, add:

```bash
cd tailit/app
go get github.com/sagernet/sing-box@latest
# or
go get github.com/xtls/xray-core@latest
```

- Use `sing-box` alone if protocols overlap (it covers vmess/vless/trojan/ss/hysteria/wg).
- Keep `xray` only for `reality`/`xtls-rprx-vision`.
