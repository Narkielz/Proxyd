#!/usr/bin/env python3

import argparse, logging, os, select, socket, ssl, struct, sys, threading
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.live import Live
    from rich.layout import Layout
    from rich import box
    from rich.text import Text
    from rich.logging import RichHandler
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "rich",
                    "--break-system-packages"], check=True)
    os.execv(sys.executable, [sys.executable] + sys.argv)

console = Console()

BANNER = """[bold red]
  ██████╗ ██████╗  ██████╗ ██╗  ██╗██╗   ██╗██████╗
  ██╔══██╗██╔══██╗██╔═══██╗╚██╗██╔╝╚██╗ ██╔╝██╔══██╗
  ██████╔╝██████╔╝██║   ██║ ╚███╔╝  ╚████╔╝ ██║  ██║
  ██╔═══╝ ██╔══██╗██║   ██║ ██╔██╗   ╚██╔╝  ██║  ██║
  ██║     ██║  ██║╚██████╔╝██╔╝ ██╗   ██║   ██████╔╝
  ╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═════╝ [/bold red]
  [dim]Multi-protocol proxy server[/dim]  [bold white]v1.0.0[/bold white]"""


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.total = self.active = self.errors = self.bytes_in = self.bytes_out = 0

    def connect(self):
        with self._lock: self.total += 1; self.active += 1

    def disconnect(self, bi=0, bo=0):
        with self._lock: self.active -= 1; self.bytes_in += bi; self.bytes_out += bo

    def error(self):
        with self._lock: self.errors += 1

    def snapshot(self):
        with self._lock:
            return dict(total=self.total, active=self.active,
                        errors=self.errors, bytes_in=self.bytes_in, bytes_out=self.bytes_out)

stats = Stats()


logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=True)]
)
log = logging.getLogger("proxyd")

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


BUFSZ = 65536

def relay(a: socket.socket, b: socket.socket):
    bi = bo = 0
    socks = [a, b]
    try:
        while True:
            r, _, _ = select.select(socks, [], socks, 60)
            if not r: break
            for s in r:
                dst = b if s is a else a
                try:
                    data = s.recv(BUFSZ)
                    if not data: return bi, bo
                    dst.sendall(data)
                    if s is a: bi += len(data)
                    else:      bo += len(data)
                except OSError:
                    return bi, bo
    except Exception:
        pass
    return bi, bo

def connect_upstream(host: str, port: int,
                     upstream: dict | None) -> socket.socket:
    if upstream:
        s = socket.create_connection((upstream["host"], upstream["port"]), timeout=15)
        # SOCKS5 handshake (no auth)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00":
            raise ConnectionError("Upstream SOCKS5 auth failed")
        host_b = host.encode()
        s.sendall(struct.pack("!BBBBB", 5, 1, 0, 3, len(host_b)) + host_b + struct.pack("!H", port))
        resp = s.recv(10)
        if resp[1] != 0:
            raise ConnectionError(f"Upstream SOCKS5 connect failed: {resp[1]}")
        return s
    return socket.create_connection((host, port), timeout=15)


def handle_http(conn: socket.socket, addr, upstream: dict | None, auth: tuple | None):
    bi = bo = 0
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk: return
            data += chunk

        first_line = data.split(b"\r\n")[0].decode(errors="ignore")
        parts = first_line.split()
        if len(parts) < 3: return
        method, target, _ = parts[0], parts[1], parts[2]

        if auth:
            import base64
            headers = data.split(b"\r\n\r\n")[0].decode(errors="ignore")
            creds = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            if f"Basic {creds}" not in headers:
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                             b"Proxy-Authenticate: Basic realm=\"proxyd\"\r\n\r\n")
                return

        if method == "CONNECT":
            host, _, port = target.partition(":")
            port = int(port or 443)
            log.info(f"[cyan]CONNECT[/cyan] {host}:{port}  [dim]{addr[0]}[/dim]")
            try:
                remote = connect_upstream(host, port, upstream)
            except Exception as e:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                stats.error(); return
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            bi, bo = relay(conn, remote)
            remote.close()
        else:
            if target.startswith("http://"):
                url = target[7:]
                host, _, path = url.partition("/")
                path = "/" + path
                host, _, port = host.partition(":")
                port = int(port or 80)
            else:
                return
            log.info(f"[green]HTTP[/green]    {method} {host}:{port}{path}  [dim]{addr[0]}[/dim]")
            try:
                remote = connect_upstream(host, port, upstream)
            except Exception:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                stats.error(); return
            new_req = data.replace(target.encode(), path.encode(), 1)
            remote.sendall(new_req)
            bi, bo = relay(conn, remote)
            remote.close()
    except Exception as e:
        stats.error()
    finally:
        stats.disconnect(bi, bo)
        conn.close()


def handle_socks4(conn: socket.socket, addr, upstream: dict | None):
    bi = bo = 0
    try:
        hdr = conn.recv(8)
        if len(hdr) < 8 or hdr[0] != 4 or hdr[1] != 1:
            return
        port = struct.unpack("!H", hdr[2:4])[0]
        ip   = socket.inet_ntoa(hdr[4:8])
        # consume user-id (null-terminated)
        while True:
            b = conn.recv(1)
            if not b or b == b"\x00": break
        # SOCKS4a: ip 0.0.0.x → read hostname
        if ip.startswith("0.0.0.") and ip != "0.0.0.0":
            host = b""
            while True:
                b = conn.recv(1)
                if not b or b == b"\x00": break
                host += b
            host = host.decode()
        else:
            host = ip

        log.info(f"[yellow]SOCKS4[/yellow]  {host}:{port}  [dim]{addr[0]}[/dim]")
        try:
            remote = connect_upstream(host, port, upstream)
        except Exception:
            conn.sendall(b"\x00\x5b" + b"\x00" * 6)
            stats.error(); return
        conn.sendall(b"\x00\x5a" + struct.pack("!H", port) + socket.inet_aton(ip if ip != host else "0.0.0.0"))
        bi, bo = relay(conn, remote)
        remote.close()
    except Exception:
        stats.error()
    finally:
        stats.disconnect(bi, bo)
        conn.close()


def handle_socks5(conn: socket.socket, addr, upstream: dict | None, auth: tuple | None):
    bi = bo = 0
    try:
        # greeting
        hdr = conn.recv(2)
        if len(hdr) < 2: return
        nmethods = hdr[1]
        methods  = conn.recv(nmethods)

        if auth:
            if b"\x02" not in methods:
                conn.sendall(b"\x05\xff"); return
            conn.sendall(b"\x05\x02")
            # username/password sub-negotiation
            sub = conn.recv(2)
            ulen = sub[1]; uname = conn.recv(ulen)
            plen = conn.recv(1)[0]; passwd = conn.recv(plen)
            if uname.decode() != auth[0] or passwd.decode() != auth[1]:
                conn.sendall(b"\x01\x01"); return
            conn.sendall(b"\x01\x00")
        else:
            conn.sendall(b"\x05\x00")

        # request
        req = conn.recv(4)
        if len(req) < 4 or req[1] != 1: return  # only CONNECT
        atyp = req[3]
        if atyp == 1:    # IPv4
            host = socket.inet_ntoa(conn.recv(4))
        elif atyp == 3:  # domain
            n    = conn.recv(1)[0]
            host = conn.recv(n).decode()
        elif atyp == 4:  # IPv6
            host = socket.inet_ntop(socket.AF_INET6, conn.recv(16))
        else:
            return
        port = struct.unpack("!H", conn.recv(2))[0]

        log.info(f"[magenta]SOCKS5[/magenta]  {host}:{port}  [dim]{addr[0]}[/dim]")
        try:
            remote = connect_upstream(host, port, upstream)
        except Exception:
            conn.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)
            stats.error(); return

        conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 4 + struct.pack("!H", port))
        bi, bo = relay(conn, remote)
        remote.close()
    except Exception:
        stats.error()
    finally:
        stats.disconnect(bi, bo)
        conn.close()


def detect_and_handle(conn: socket.socket, addr, upstream: dict | None, auth: tuple | None):
    stats.connect()
    try:
        first = conn.recv(1, socket.MSG_PEEK)
        if not first: return
        b = first[0]
        if b == 5:
            handle_socks5(conn, addr, upstream, auth)
        elif b == 4:
            handle_socks4(conn, addr, upstream)
        else:
            handle_http(conn, addr, upstream, auth)
    except Exception:
        stats.error()
        stats.disconnect()
        conn.close()


def serve(host: str, port: int, upstream: dict | None, auth: tuple | None):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(256)
    log.info(f"Listening on [bold cyan]{host}:{port}[/bold cyan]")
    while True:
        try:
            conn, addr = srv.accept()
            conn.settimeout(120)
            t = threading.Thread(target=detect_and_handle,
                                 args=(conn, addr, upstream, auth), daemon=True)
            t.start()
        except KeyboardInterrupt:
            break
        except Exception:
            pass


def dashboard(host: str, port: int, upstream: dict | None, auth: bool):
    from rich.live import Live
    import time

    def build():
        s = stats.snapshot()
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column(style="dim", no_wrap=True)
        t.add_column(style="bold white")
        t.add_row("Listen",    f"[cyan]{host}:{port}[/cyan]")
        t.add_row("Protocols", "[green]HTTP · HTTPS · SOCKS4 · SOCKS5[/green]")
        t.add_row("Upstream",  f"[yellow]{upstream['host']}:{upstream['port']}[/yellow]"
                               if upstream else "[dim]none[/dim]")
        t.add_row("Auth",      "[green]enabled[/green]" if auth else "[dim]disabled[/dim]")
        t.add_row("",          "")
        t.add_row("Active",    f"[bold cyan]{s['active']}[/bold cyan]")
        t.add_row("Total",     str(s["total"]))
        t.add_row("Errors",    f"[red]{s['errors']}[/red]" if s["errors"] else "0")
        t.add_row("↓ In",      _fmt_bytes(s["bytes_in"]))
        t.add_row("↑ Out",     _fmt_bytes(s["bytes_out"]))
        return Panel(t, title="[bold white]proxyd[/bold white]",
                     border_style="cyan", box=box.ROUNDED)

    with Live(build(), console=console, refresh_per_second=2, screen=False) as live:
        while True:
            time.sleep(0.5)
            live.update(build())


def print_help():
    t = Table(box=None, show_header=False, padding=(0, 1))
    t.add_column(style="bold cyan", no_wrap=True)
    t.add_column(style="dim")
    rows = [
        ("-b, --bind HOST",        f"Bind address  [default: 0.0.0.0]"),
        ("-p, --port PORT",        f"Listen port   [default: 1080]"),
        ("-U, --upstream HOST:PORT","Chain to upstream SOCKS5 proxy"),
        ("-u, --user USER:PASS",   "Enable authentication"),
        ("-D, --dashboard",        "Show live stats dashboard"),
        ("-v, --verbose",          "Debug logging"),
        ("-h, --help",             "Show this help"),
    ]
    for r in rows: t.add_row(*r)
    console.print(Panel(t,
        title="[bold white]proxyd — multi-protocol proxy[/bold white]",
        subtitle="[dim]HTTP · HTTPS · SOCKS4 · SOCKS5[/dim]",
        border_style="cyan", box=box.ROUNDED))

def main():
    console.print(BANNER)
    console.print()

    ap = argparse.ArgumentParser(prog="proxyd", add_help=False)
    ap.add_argument("-b", "--bind",      default="0.0.0.0")
    ap.add_argument("-p", "--port",      type=int, default=1080)
    ap.add_argument("-U", "--upstream",  metavar="HOST:PORT")
    ap.add_argument("-u", "--user",      metavar="USER:PASS")
    ap.add_argument("-D", "--dashboard", action="store_true")
    ap.add_argument("-v", "--verbose",   action="store_true")
    ap.add_argument("-h", "--help",      action="store_true")
    args = ap.parse_args()

    if args.help:
        print_help(); sys.exit(0)

    if args.verbose:
        logging.getLogger("proxyd").setLevel(logging.DEBUG)

    upstream = None
    if args.upstream:
        h, _, p = args.upstream.partition(":")
        upstream = {"host": h, "port": int(p or 1080)}

    auth = None
    if args.user:
        u, _, p = args.user.partition(":")
        auth = (u, p)

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t.add_column(style="dim"); t.add_column(style="bold white")
    t.add_row("Listen",    f"[cyan]{args.bind}:{args.port}[/cyan]")
    t.add_row("Protocols", "[green]HTTP · HTTPS · SOCKS4 · SOCKS5[/green]")
    t.add_row("Upstream",  f"[yellow]{args.upstream}[/yellow]" if upstream else "[dim]none[/dim]")
    t.add_row("Auth",      f"[green]{args.user.split(':')[0]}[/green]" if auth else "[dim]disabled[/dim]")
    console.print(Panel(t, border_style="cyan", box=box.ROUNDED))
    console.print()

    srv_thread = threading.Thread(
        target=serve, args=(args.bind, args.port, upstream, auth), daemon=True)
    srv_thread.start()

    if args.dashboard:
        try:
            dashboard(args.bind, args.port, upstream, bool(auth))
        except KeyboardInterrupt:
            pass
    else:
        log.info("[dim]Press Ctrl+C to stop[/dim]")
        try:
            srv_thread.join()
        except KeyboardInterrupt:
            pass

    console.print("\n[bold red]Stopped.[/bold red]")

if __name__ == "__main__":
    main()
