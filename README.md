# proxyd

> Multi-protocol proxy server — HTTP · HTTPS · SOCKS4 · SOCKS5

Single port, zero external dependencies (only `rich` for the UI). Auto-detects the protocol from the first byte of each connection.

```
  ██████╗ ██████╗  ██████╗ ██╗  ██╗██╗   ██╗██████╗
  ██╔══██╗██╔══██╗██╔═══██╗╚██╗██╔╝╚██╗ ██╔╝██╔══██╗
  ██████╔╝██████╔╝██║   ██║ ╚███╔╝  ╚████╔╝ ██║  ██║
  ██╔═══╝ ██╔══██╗██║   ██║ ██╔██╗   ╚██╔╝  ██║  ██║
  ██║     ██║  ██║╚██████╔╝██╔╝ ██╗   ██║   ██████╔╝
  ╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═════╝
```

---

## Features

- **Single port** serves all protocols simultaneously — no need to run multiple servers
- **HTTP forward proxy** — handles plain `GET http://...` requests
- **HTTPS tunnel** — `CONNECT` method for TLS passthrough
- **SOCKS4 / SOCKS4a** — IPv4 and hostname resolution
- **SOCKS5** — IPv4, IPv6, hostname + username/password authentication
- **Upstream chaining** — forward all traffic through another SOCKS5 proxy (e.g. Tor)
- **Authentication** — SOCKS5 user/pass and HTTP Proxy-Authorization Basic
- **Live dashboard** — real-time stats (active connections, bytes in/out, errors)
- **Threaded** — one thread per connection, non-blocking relay via `select`

---

## Requirements

- Python 3.9+
- `rich` (auto-installed on first run)

```sh
pip install rich
```

---

## Installation

```sh
# run directly
python3 proxyd.py -h

# or install globally
sudo cp proxyd.py /usr/bin/proxyd
sudo chmod +x /usr/bin/proxyd
proxyd -h
```

---

## Usage

```
proxyd [options]

Options:
  -b, --bind HOST        Bind address          [default: 0.0.0.0]
  -p, --port PORT        Listen port           [default: 1080]
  -U, --upstream HOST:PORT  Chain to upstream SOCKS5 proxy
  -u, --user USER:PASS   Enable authentication
  -D, --dashboard        Show live stats dashboard
  -v, --verbose          Debug logging
  -h, --help             Show help
```

---

## Examples

### Basic — all protocols on port 1080

```sh
python3 proxyd.py
```

### Custom port

```sh
python3 proxyd.py -p 8080
```

### Enable authentication

```sh
python3 proxyd.py -p 1080 -u admin:s3cr3t
```

All protocols enforce the same credentials:
- SOCKS5 → username/password sub-negotiation (RFC 1929)
- HTTP → `Proxy-Authorization: Basic` header

### Chain through Tor

Route all outbound connections through Tor's SOCKS5 listener:

```sh
python3 proxyd.py -p 1080 -U 127.0.0.1:9050
```

### Chain through another proxyd instance

```sh
# instance A (public-facing)
python3 proxyd.py -p 1080 -U 10.0.0.2:1080

# instance B (internal)
python3 proxyd.py -b 10.0.0.2 -p 1080
```

### Live dashboard

```sh
python3 proxyd.py -D
```

```
╭─────────────────────────────╮
│          proxyd             │
│  Listen    0.0.0.0:1080     │
│  Protocols HTTP·HTTPS·S4·S5 │
│  Upstream  none             │
│  Auth      disabled         │
│                             │
│  Active    3                │
│  Total     47               │
│  Errors    0                │
│  ↓ In      1.2 MB           │
│  ↑ Out     840.3 KB         │
╰─────────────────────────────╯
```

### Run in background (tmux)

```sh
tmux new-session -d -s proxy 'python3 proxyd.py -p 1080 -D'
```

---

## Integration with NucleiScanner

Start proxyd pointing to Tor, then pass it to `ns`:

```sh
# terminal 1 — start proxy chain → Tor
python3 proxyd.py -p 8080 -U 127.0.0.1:9050 -D

# terminal 2 — scan through it
ns -d example.com -P http://127.0.0.1:8080
```

Or use proxychains + proxyd together:

```sh
ns -d example.com -p -P http://127.0.0.1:8080
```

Configure your tools to use `proxyd` as their proxy:

| Tool | Config |
|---|---|
| curl | `curl -x socks5://127.0.0.1:1080 https://example.com` |
| wget | `wget -e "https_proxy=http://127.0.0.1:1080"` |
| Firefox | Settings → Network → Manual proxy → SOCKS5 `127.0.0.1:1080` |
| Burp Suite | User options → SOCKS proxy → `127.0.0.1:1080` |
| proxychains | `socks5 127.0.0.1 1080` in `/etc/proxychains.conf` |
| nuclei | `nuclei -proxy http://127.0.0.1:1080` |
| httpx | `httpx -http-proxy http://127.0.0.1:1080` |

---

## Protocol Detection

proxyd peeks at the first byte of each connection to route it:

| First byte | Protocol |
|---|---|
| `0x05` | SOCKS5 |
| `0x04` | SOCKS4 / SOCKS4a |
| anything else | HTTP (forward or CONNECT) |

---

## Architecture

```
client
  │
  ▼
[proxyd listener :1080]
  │
  ├── peek first byte
  │     ├── 0x05 → SOCKS5 handler
  │     ├── 0x04 → SOCKS4 handler
  │     └── other → HTTP handler
  │
  ├── (optional) upstream SOCKS5 chain
  │
  └── bidirectional relay (select-based, 64KB buffer)
```

Each connection runs in its own daemon thread. The relay loop uses `select()` with a 60-second idle timeout.

---

## Limitations

- SOCKS4 does not support authentication (protocol limitation)
- SOCKS5 UDP ASSOCIATE not supported (TCP only)
- No TLS termination — HTTPS is tunneled transparently via CONNECT
- Upstream chaining only supports SOCKS5 (not HTTP proxy)

---

## License

MIT
