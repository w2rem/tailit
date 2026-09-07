# tailit

Minimal Tailscale connector for Streamlit Cloud and any host. Downloads `tailscaled`, registers the machine and exposes `tailscale serve`.

## How it works

1. Resolves version (`latest` → `https://pkgs.tailscale.com/stable/`), downloads `tailscale_<ver>_<arch>.tgz` to `/tmp/tailit`.
2. Starts `tailscaled --socket /tmp/tailit/tailscaled.sock --state /tmp/tailit/tailscaled.state --port 41641 --tun userspace-networking` (root → without `--tun`).
3. `tailscale up --auth-key $TS_KEY --hostname $TS_HOST --accept-dns=false` (optional tags/routes/ephemeral/exit-node).
4. `tailscale serve --bg $TS_PORT` (HTTP proxy to `http://127.0.0.1:$TS_PORT`).

As a Streamlit app (`Main file path = tailit/tailscale.py`) it auto-starts on page load and shows live status.

## Requirements

- Python 3.10+
- `streamlit>=1.32` (only for Streamlit mode)
- Outbound 443 to `pkgs.tailscale.com` and `controlplane.tailscale.com`

## Secrets / Environment

| Variable | Required | Default | Description | Recommended |
|---|---|---|---|---|
| `TS_KEY` | **yes** | — | Auth key `tskey-auth-...` (reusable + ephemeral). | Generate in Admin → Settings → Keys, reusable, tags `tag:worker`. |
| `TS_HOST` | no | OS hostname | Hostname in tailnet (`[a-z0-9-]{1,63}`). | `worker-1`, `worker-2` — one per instance. |
| `TS_PORT` | no | `8501` | Port for `tailscale serve`. | `8501` (Streamlit) or `9988` etc. |
| `TS_TAGS` | no | — | `--advertise-tags`, e.g. `tag:worker` or `tag:a,tag:b`. | `tag:worker` if key is tagged. |
| `TS_VERSION` | no | `latest` | `latest` or `1.102.3`. Latest is resolved from `pkgs.tailscale.com`. | `latest` to avoid `update available`. |
| `TS_EPHEMERAL` | no | `0` | `1` → `--ephemeral`. | `1` on ephemeral hosts (Streamlit). |
| `TS_ADVERTISE_EXIT_NODE` | no | `0` | `1` → `--advertise-exit-node`. | `0` on Streamlit (userspace can't be exit node). `1` only on hosts with TUN. |
| `TS_ROUTES` | no | — | `--advertise-routes`, e.g. `10.0.0.0/24`. | — |
| `TS_EXTRA_ARGS` | no | — | Extra args appended to `tailscale up`. | — |
| `TS_CLIENT_ID` / `TS_CLIENT_SECRET` | no | — | OAuth client for stale-node cleanup (`Devices:Write`). Deletes `hostname`/`hostname-N` before `up` to avoid `-1` suffix. | Set if you reuse hostnames. |
| `TS_TAILNET` | no | `-` | Tailnet for OAuth API. | `-` (auto). |
| `TS_OAUTH_SCOPE` | no | `devices:write` | OAuth scope, fallback `all:write`. | `devices:write`. |
| `TS_PEERS` | no | — | Comma-separated peer Streamlit URLs for keepalive, e.g. `https://worker-2.streamlit.app`. Probes `/_stcore/health` then `/`; treats 3xx (private redirect) as success. | Pair peers to reduce sleep. |
| `TS_PEER_INTERVAL` | no | `300` | Peer ping interval sec (min 60). | `300`. |
| `TAILIT_GO` | no | `1` | `1` → download Go to `/tmp/go`, build `tailit/app` with 1GiB, run it after serve. `0` → skip (logged). | `1` if the Go API is needed, `0` for tailscale-only. |
| `GO_VERSION` | no | `1.22.8` | Go version to download to `/tmp/go`. | `1.22.8` or `1.21.13`. |
| `GO_MEM_LIMIT` | no | `2GiB` | `GOMEMLIMIT` for `go build` (`-p 2`, `GOMAXPROCS=2`). | `2GiB` (Streamlit allows up to ~2.7GB). |
| `GO_PORT` | no | `TS_PORT+1` | Port of the Go sidecar (overrides `TS_PORT+1`). | Leave unset. |
| `DATABASE_URL` | no | — | Postgres DSN for Go app (`postgres://...`). | From Secrets if Go app is enabled. |
| `VALKEY_URL` / `VALKEY_DB` | no | — | Valkey/Redis for Go app cache. | `redis://...` / `0`. |

No other `TS_*` is read. Legacy aliases (`TS_IP`, `TS_HOSTNAME`, `TS_SERVE_PORT`, `PORT`, `TS_AUTHKEY`…) are removed.

## Go App

`tailit/app/` is a standalone Go module (stdlib only by default). It is **not** bundled — it is built on demand.

```bash
# manual
python3 -c "import tailit.tailscale; print(tailit.tailscale.ensure_go())"
python3 -c "import tailit.tailscale; print(tailit.tailscale.build_go_app())"
# → /tmp/tailit-app (~9MB, -trimpath -ldflags="-s -w", GOMEMLIMIT=2GiB, GOMAXPROCS=2, -p 2)

# run (reads DATABASE_URL, VALKEY_URL, TS_PORT → listens TS_PORT+1)
DATABASE_URL=postgres://... VALKEY_URL=redis://... TS_PORT=9978 /tmp/tailit-app &
# → listens :9979
curl http://127.0.0.1:9979/health  # {"status":"ok","time":"..."}
curl http://127.0.0.1:9979/ready   # {"ready":true} (200) or 503 before first sweep
curl http://127.0.0.1:9979/peers   # {"peers":[...],"interval_sec":300}
```

Build details:
- Go is downloaded to `/tmp/go` (`https://go.dev/dl/go<ver>.linux-<arch>.tar.gz`) only if not cached; system Go (`/usr/local/go/bin/go`) is reused on dev hosts to avoid 100MB download.
- Compile is limited to 2GiB: `GOMEMLIMIT=2GiB GOMAXPROCS=2 go build -p 2 -trimpath -ldflags="-s -w"`.
- `tailit/app/go.mod` is stdlib-only; add `sing-box`/`xray-core` when you need proxy checks (sing-box covers most protocols; keep xray only for `reality`/`xtls-rprx-vision`).

`TAILIT_GO=1` (default) auto-builds and runs the sidecar in `main()` after `tailscale serve`; `0` skips (logged). Python UI polls `127.0.0.1:TS_PORT+1/peers` every 60s for display.

## Peer Keepalive (Go sidecar)

Pinging is owned by `tailit/app` (`TS_PORT+1`), not Python. Round-robin: one peer per `TS_PEER_INTERVAL` tick (single peer → every tick).

Each check reads the **full response body** (waits for the server to finish) and classifies state:
- `awake` — 200 with app content, or 3xx (private `/-/auth` redirect = front proxy alive).
- `starting` — body contains `Waking up` / `in the oven` / `Please wait`.
- `asleep` — body contains `get this app back up` (needs manual wake, ping can't fix it).
- `down` — network error or other HTTP status.

Verified live: `{"url":"https://worker-2.streamlit.app","status":303,"latency_ms":759,"ok":true,"state":"awake"}`.

Note: HTTP GET is the maximum "wait for scripts" possible over plain HTTP — a true
wait-for-script-execution would need a `/_stcore/stream` websocket session; the
full-body read is the closest stateless equivalent.

## Logs

- Format: `[19:53:46] message event=... key=value` — timestamp colored by level
  (`info` cyan, `ok` green, `warn` yellow, `error` red, `debug` dim), no date/level text dupes.
- Everything `main()` prints goes **both** to the Streamlit Cloud logs (live, via stdout tee)
  **and** to the UI log tail + `/tmp/tailit/tailit.log` (plain).


## Run

```bash
# CLI
TS_KEY=tskey-auth-... TS_HOST=worker-1 TS_PORT=8501 python -m tailit.tailscale
TS_KEY=... python tailit/tailscale.py --serve 9988 --hostname worker-1

# Streamlit Cloud
# Main file path = tailit/tailscale.py
# Secrets: TS_KEY, TS_HOST, TS_PORT (+ optional above)
```

## Streamlit UI

No inputs, only status. Auto-starts once per session with file-lock to avoid double `up` on parallel exec.

- Live `CPU` / `RAM` (`/proc/stat` delta 0.4s, `/proc/meminfo`) — refresh every 2s.
- Live `Public IP` / `Country` / `ISP` / `ASN` from `https://api.ip.sb/geoip` — refresh every 60s.
- Peer keepalive status if `TS_PEERS` set.
- `status` / `version` (`update available: X` if behind) / `hostname` / `ip` (`tailscale ip -4`) / `serve port` + tail of startup log.

## Notes

- State and binaries are in `/tmp/tailit` (ephemeral). Each reboot re-downloads and re-registers — use a reusable key.
- `--accept-dns=false` is always set to suppress `health(check): OS base config` on Streamlit.
- Serve races (`etag mismatch`) are retried 5× with 3s delay.
- Auth key is never logged (`--auth-key ***`).
