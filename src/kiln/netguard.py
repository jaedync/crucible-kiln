"""Request checks that the gateway and the dashboard share.

- HostGuard: a Host allowlist against DNS rebinding. A page at rebind.attacker.example can make its name
  resolve to this machine; the browser then sends Host and Origin with the attacker's name, so a same-origin
  check alone passes. Only names that this machine owns are accepted.
- OriginGuard: refuses a request from a web page. A page on any site can POST text/plain to
  127.0.0.1:8000 from the owner's browser with no CORS preflight; the browser sends its Origin. API clients
  (the OpenAI SDK, curl) send no Origin.
- proxied(): a request that a reverse proxy relayed. Through `tailscale serve`, Caddy or a container port
  mapping, every client arrives from loopback, so loopback must not mean "the owner" then.
- node_allowed(): action rights by full MagicDNS name, because the first label is not unique across
  tailnets (node sharing).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from typing import Iterable

from aiohttp import web

# Headers that reverse proxies add. Any of them on a request means the TCP peer is not the client.
PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP", "Tailscale-User-Login", "CF-Connecting-IP")
# Names that container runtimes give the host. Public DNS cannot give an attacker these names, so they are
# safe to accept, and a client in a container (for example a chat UI) reaches the gateway by them.
CONTAINER_HOST_NAMES = ("host.docker.internal", "host.containers.internal")
ANY = "*"
_NAME = re.compile(r"^[a-z0-9_]([a-z0-9_.-]{0,252})$")
STATUS_TIMEOUT_S = 3.0


def host_name(header: str | None) -> str | None:
    """The name or address in a Host header, without port, brackets or trailing dot, in lower case."""
    if not header:
        return None
    h = header.strip().lower()
    if h.startswith("["):  # an IPv6 literal, the only form that may carry a zone
        end = h.find("]")
        inner = h[1:end] if end > 0 else ""
        return inner if _ip(inner.split("%", 1)[0]) else None
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    h = h.rstrip(".")
    if _ip(h):
        return h
    return h if _NAME.match(h) else None


def _ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s.split("%", 1)[0] if ":" in s else s)  # a zone only on IPv6
        return True
    except ValueError:
        return False


def is_mdns_name(header: str | None) -> bool:
    """A .local name: any peer on the link can answer mDNS for it, so it can be rebound to this machine's
    tailnet or loopback address. Such a request may read but never act."""
    h = host_name(header)
    return h is not None and h.endswith(".local")


def default_names(host: str, extra: Iterable[str] = (), hostname: str | None = None) -> set[str]:
    """localhost, the configured host name and <host>.local, the machine's own name, container host names,
    and the names from the config. Address literals need no entry (HostGuard accepts all of them)."""
    hostname = socket.gethostname() if hostname is None else hostname
    names = {"localhost", host, f"{host}.local", *CONTAINER_HOST_NAMES}
    if hostname:
        short = hostname.split(".")[0]
        names |= {hostname, short, f"{short}.local"}
    return {n.lower().rstrip(".") for n in (*names, *extra) if n}


class HostGuard:
    def __init__(self, names: Iterable[str]):
        self.names = {n.lower().rstrip(".") for n in names}

    def add(self, name: str | None) -> None:
        if name:
            self.names.add(name.lower().rstrip("."))

    def allows(self, header: str | None) -> bool:
        if ANY in self.names:
            return True
        h = host_name(header)
        if h is None:
            return False
        # DNS rebinding needs a name that the attacker controls. A browser that sends an address literal
        # connected to that address, so the request reached this machine by its own address: accept every
        # literal, also a DHCP or tailnet address that changed since the start.
        return _ip(h) or h in self.names


def middleware(guard: HostGuard, setting: str):
    """Refuse a request (also a WebSocket handshake) whose Host is not one of this machine's names."""
    @web.middleware
    async def check_host(request: web.Request, handler):
        if not guard.allows(request.headers.get("Host") or request.host):
            return web.Response(status=403, text=f"403 Host {request.host!r} is not allowed here; add the name"
                                f" to {setting} in kiln.toml if this machine really has it\n")
        return await handler(request)
    return check_host


def origin_key(origin: str) -> str:
    """An origin as compared: lower case, without a trailing slash."""
    return origin.strip().lower().rstrip("/")


class OriginGuard:
    def __init__(self, allowed: Iterable[str]):
        self.allowed = {origin_key(o) for o in allowed}

    def allows(self, origin: str | None) -> bool:
        return origin is None or ANY in self.allowed or origin_key(origin) in self.allowed


CORS_METHODS = "GET, POST, OPTIONS"
CORS_MAX_AGE = "600"
_HEADER_TOKEN = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
CORS_MAX_HEADERS = 32


def cors_request_headers(value: str | None) -> str:
    """Access-Control-Request-Headers as the preflight answer echoes it: header names only, at most 32."""
    names = [n.strip() for n in (value or "").split(",")]
    return ", ".join([n for n in names if _HEADER_TOKEN.fullmatch(n)][:CORS_MAX_HEADERS])


def is_preflight(request: web.Request) -> bool:
    return request.method == "OPTIONS" and "Access-Control-Request-Method" in request.headers


def origin_middleware(guard: OriginGuard, setting: str, cors: bool = False):
    """Refuse a request that carries an Origin header unless the origin is allowed: a web page sent it.
    cors: answer a preflight from an allowed origin here (it never reaches a handler). add_cors() then gives
    every response to an allowed origin its CORS headers."""
    @web.middleware
    async def check_origin(request: web.Request, handler):
        origin = request.headers.get("Origin")
        if not guard.allows(origin):
            return web.Response(status=403, text=f"403 Origin {origin[:200]!r} is not allowed here; add it to"
                                f" {setting} in kiln.toml if a browser app of yours calls the gateway\n")
        if cors and origin is not None and is_preflight(request):
            headers = {"Access-Control-Allow-Methods": CORS_METHODS, "Access-Control-Max-Age": CORS_MAX_AGE}
            if allow := cors_request_headers(request.headers.get("Access-Control-Request-Headers")):
                headers["Access-Control-Allow-Headers"] = allow
            return web.Response(status=204, headers=headers)
        return await handler(request)
    return check_origin


def cors_signal(guard: OriginGuard):
    """An on_response_prepare handler: CORS headers on every response to an allowed origin, a stream too
    (its headers go out before the handler returns). Never Allow-Credentials: an engine's own CORS headers
    are dropped first."""
    async def add_cors(request: web.Request, response: web.StreamResponse) -> None:
        origin = request.headers.get("Origin")
        if origin is None or not guard.allows(origin):
            return
        if not is_preflight(request):  # the engine's own CORS headers (an allowed credential, say) go
            for key in {k for k in response.headers if k.lower().startswith("access-control-")}:
                response.headers.popall(key, None)
        response.headers["Access-Control-Allow-Origin"] = origin
        vary = response.headers.get("Vary")
        if not vary:
            response.headers["Vary"] = "Origin"
        elif "origin" not in vary.lower():
            response.headers["Vary"] = f"{vary}, Origin"
    return add_cors


def proxied(request) -> bool:
    return any(h in request.headers for h in PROXY_HEADERS)


def node_allowed(fqdn: str | None, allow: Iterable[str], tailnet: str | None) -> bool:
    """Whether a device (full MagicDNS name) may act. An entry with a dot is a full name. An entry without
    a dot is a device of this host's own tailnet: a shared-in device of another tailnet can have the same
    first label, so with the own tailnet unknown such an entry matches nothing."""
    if not fqdn:
        return False
    fqdn = fqdn.lower().rstrip(".")
    for entry in allow:
        e = entry.lower().rstrip(".")
        if "." in e:
            if fqdn == e:
                return True
        elif tailnet and fqdn == f"{e}.{tailnet.lower().rstrip('.')}":
            return True
    return False


def parse_status(st) -> dict | None:
    """This node's MagicDNS name and tailnet suffix from `tailscale status --json`."""
    if not isinstance(st, dict) or not isinstance(st.get("Self"), dict):
        return None
    name = str(st["Self"].get("DNSName") or "").lower().rstrip(".")
    if "." not in name:
        return None
    suffix = st.get("MagicDNSSuffix")
    tailnet = suffix.lower().rstrip(".") if isinstance(suffix, str) and suffix else name.split(".", 1)[1]
    return {"dns_name": name, "tailnet": tailnet}


async def tailscale_self() -> dict | None:
    """parse_status of this machine, or None without Tailscale."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json", "--peers=false",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), STATUS_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    try:
        return parse_status(json.loads(out))
    except ValueError:
        return None
