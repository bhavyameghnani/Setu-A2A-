"""Outbound URL guard. Setu fetches URLs that users (and agent cards) provide, so it must not
become a way to reach internal services (cloud metadata, databases, localhost admin ports)."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    pass


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_global and not ip.is_multicast


async def validate_agent_url(url: str, *, allow_private: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeUrlError("agent URLs must be http(s) with a host")
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials in URLs are not allowed")
    if allow_private:  # local development: loopback and LAN agents are the point
        return
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeUrlError(f"cannot resolve {host}") from exc
        addresses = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not addresses or not all(_is_public(a) for a in addresses):
        raise UnsafeUrlError(f"{host} resolves to a non-public address")
