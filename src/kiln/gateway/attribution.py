"""Who sent a request: an optional self-declared label plus the Tailscale device.

Tailscale identities come from WireGuard keys, so a tailnet address can't be
spoofed. Lookups run in the background and are cached; they never delay a
request.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time

CLIENT_HEADER = "X-Kiln-Client"
_LABEL = re.compile(r"^[A-Za-z0-9._-]{1,48}$")
TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
WHOIS_TTL_S = 600
WHOIS_TIMEOUT_S = 3.0


def normalize_ip(addr: str | None) -> str | None:
    if not addr:
        return None
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return addr
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def is_tailnet(addr: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    return any(ip in n for n in TAILNET_NETS)


def is_loopback(addr: str | None) -> bool:
    try:
        return ipaddress.ip_address(addr or "").is_loopback
    except ValueError:
        return False


def valid_label(value: str | None) -> str | None:
    return value if value and _LABEL.match(value) else None


def parse_whois(obj: dict, full: bool = False) -> dict | None:
    """Device identity. `node` is the first label of the MagicDNS name (device names and action auth).
    full=True adds `fqdn`, the whole name without its trailing dot: unlike the first label, it is
    unique, because a node shared in from another tailnet keeps its own tailnet's suffix."""
    node = obj.get("Node") if isinstance(obj.get("Node"), dict) else None
    if not node:
        return None
    hostinfo = node.get("Hostinfo") or {}
    user = obj.get("UserProfile") or {}
    out = {"node": (node.get("Name") or "").split(".")[0] or None,
           "name": hostinfo.get("Hostname") or node.get("ComputedName"),
           "os": hostinfo.get("OS"), "user": user.get("LoginName")}
    if full:
        out["fqdn"] = (node.get("Name") or "").rstrip(".").lower() or None
    return out


async def tailscale_whois(ip: str, full: bool = False) -> dict | None:
    proc = await asyncio.create_subprocess_exec(
        "tailscale", "whois", "--json", ip,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), WHOIS_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    try:
        return parse_whois(json.loads(out), full)
    except ValueError:
        return None


class Whois:
    """Cached, non-blocking device lookup."""

    def __init__(self, lookup=tailscale_whois, ttl: float = WHOIS_TTL_S, clock=time.monotonic):
        self._lookup, self._ttl, self._clock = lookup, ttl, clock
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def known(self, ip: str | None) -> dict | None:
        hit = self._cache.get(ip or "")
        return hit[1] if hit and hit[0] > self._clock() else None

    def prefetch(self, ip: str | None) -> None:
        if not is_tailnet(ip) or ip in self._tasks:
            return
        hit = self._cache.get(ip)
        if hit and hit[0] > self._clock():
            return
        self._tasks[ip] = asyncio.get_running_loop().create_task(self._run(ip))

    async def _run(self, ip: str) -> None:
        try:
            info = await self._lookup(ip)
        except Exception:  # a failed lookup only costs the device name
            info = None
        self._cache[ip] = (self._clock() + (self._ttl if info else 30), info)
        self._tasks.pop(ip, None)

    async def get(self, ip: str | None, wait: float = 0.0) -> dict | None:
        if (info := self.known(ip)) is not None or not ip:
            return info
        task = self._tasks.get(ip)
        if task and wait > 0:
            await asyncio.wait({task}, timeout=wait)
        return self.known(ip)
