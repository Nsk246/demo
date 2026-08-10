#!/usr/bin/env python3
"""Find out which layer is refusing a connection.

A ConnectTimeout from the crawler has three plausible causes and they need
different fixes:

1. The container has an IPv6 address but no working IPv6 route. httpx has no
   Happy Eyeballs, so it picks the AAAA record and hangs until timeout, while
   curl falls back to IPv4 in milliseconds and appears to work fine. This is
   the usual answer inside Codespaces.
2. The site's firewall silently drops traffic from cloud IP ranges. Packets
   disappear rather than being rejected, which also reads as a timeout.
3. Egress from this machine is filtered.

    python scripts/netcheck.py https://www.rxiedu.com/
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
from urllib.parse import urlparse


def resolve(host: str, family: int) -> list[str]:
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(host, 443, family)})
    except socket.gaierror:
        return []


def tcp(addr: str, family: int, timeout: float = 6.0) -> str:
    try:
        s = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        # Errno 97 here means the container has no stack for this family at
        # all, which is itself the answer.
        return f"unavailable ({exc.strerror})"
    s.settimeout(timeout)
    started = time.time()
    try:
        s.connect((addr, 443))
        return f"open in {int((time.time() - started) * 1000)}ms"
    except socket.timeout:
        return "TIMED OUT (packets dropped)"
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        s.close()


async def via_httpx(url: str, force_ipv4: bool) -> str:
    import httpx

    transport = httpx.AsyncHTTPTransport(
        local_address="0.0.0.0" if force_ipv4 else None
    )
    started = time.time()
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=10.0, follow_redirects=True
        ) as c:
            r = await c.get(url)
        return f"{r.status_code} in {int((time.time() - started) * 1000)}ms"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.rxiedu.com/"
    host = urlparse(url).hostname or url
    print(f"host: {host}\n")

    v4 = resolve(host, socket.AF_INET)
    v6 = resolve(host, socket.AF_INET6)
    print(f"  A     records    {', '.join(v4) or 'none'}")
    print(f"  AAAA  records    {', '.join(v6) or 'none'}")

    for addr in v4[:2]:
        print(f"  tcp v4 {addr:<18} {tcp(addr, socket.AF_INET)}")
    for addr in v6[:2]:
        print(f"  tcp v6 {addr:<18} {tcp(addr, socket.AF_INET6)}")

    print(f"\n  httpx default    {await via_httpx(url, force_ipv4=False)}")
    print(f"  httpx ipv4 only  {await via_httpx(url, force_ipv4=True)}")

    print("\nreading the result:")
    print("  ipv4 only works, default does not  -> broken IPv6 route. The")
    print("     crawler retries on IPv4 automatically; nothing to do.")
    print("  both fail, tcp v4 times out        -> the site drops traffic from")
    print("     this network. Crawl from your laptop and commit data/site.db.")
    print("  no A records at all                -> the hostname is wrong.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
