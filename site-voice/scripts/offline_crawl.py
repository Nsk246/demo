#!/usr/bin/env python3
"""Crawl a site using nothing but the Python standard library.

For the machine that can reach the site but has none of this project set up.
No pip install, no virtualenv, no git, no API key. Copy this one file across,
run it, and hand the resulting folder back.

    python3 offline_crawl.py https://www.rxiedu.com/

Writes ./pages/ with one HTML file per page plus manifest.json, which is
exactly what `python -m app.ingest --from-dir` expects.

Deliberately kept to the stdlib and to one file. Anything that needs
installing is one more thing to go wrong on a machine you are not sitting at.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
SKIP_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|svg|webp|ico|css|js|mjs|woff2?|ttf|eot|zip|gz|tar|"
    r"mp4|mp3|wav|avi|mov|pdf|doc|docx|xls|xlsx|ppt|pptx)$",
    re.I,
)
SKIP_PATH = re.compile(
    r"/(wp-json|wp-admin|cdn-cgi|feed|rss|tag|author|cart|checkout|login|"
    r"signin|signup|account)(/|$)",
    re.I,
)
SKIP_QUERY = re.compile(r"(^|&)(tag|author|page|paged|s|q|sort|filter)=", re.I)
LINK_RE = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"'>]+)["']""", re.I)
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
SAFE = re.compile(r"[^a-z0-9._-]+")

MAX_PAGES = 120
MAX_DEPTH = 3
DELAY_S = 0.3


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def normalise(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower()
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = "&".join(
        sorted(
            q
            for q in p.query.split("&")
            if q and not q.split("=")[0].lower().startswith(("utm_", "fbclid", "gclid"))
        )
    )
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")


def key(url: str) -> str:
    p = urlparse(normalise(url))
    return strip_www(p.netloc) + p.path + (f"?{p.query}" if p.query else "")


def same_site(url: str, root: str) -> bool:
    return strip_www(urlparse(normalise(url)).netloc) == strip_www(
        urlparse(normalise(root)).netloc
    )


def crawlable(url: str) -> bool:
    p = urlparse(url)
    return (
        p.scheme in ("http", "https")
        and not SKIP_EXT.search(p.path)
        and not SKIP_PATH.search(p.path)
        and not (p.query and SKIP_QUERY.search(p.query))
    )


def get(url: str, timeout: float = 20.0):
    """Return (final_url, content_type, text) or raise."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "text/html"}
    )
    # Some small hosts have a partial certificate chain. A crawl that dies on
    # that is more annoying than the risk it avoids on a public marketing site.
    ctx = ssl.create_default_context()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except ssl.SSLError:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("  note: certificate could not be verified, continuing anyway")
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    charset = resp.headers.get_content_charset() or "utf-8"
    return resp.geturl(), resp.headers.get("Content-Type", ""), raw.decode(
        charset, errors="replace"
    )


def filename(url: str) -> str:
    slug = SAFE.sub("-", url.split("://", 1)[-1].lower()).strip("-")[:80] or "page"
    return f"{slug}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.html"


def sitemap_seeds(root: str, limit: int) -> list[str]:
    found, seen_maps = [], set()
    queue = [urljoin(root, "/sitemap.xml"), urljoin(root, "/sitemap_index.xml")]
    while queue and len(found) < limit:
        target = queue.pop(0)
        if target in seen_maps:
            continue
        seen_maps.add(target)
        try:
            _, ctype, text = get(target)
        except Exception:
            continue
        if "<loc" not in text.lower():
            continue
        for loc in (normalise(u) for u in LOC_RE.findall(text)):
            if loc.endswith(".xml") and len(seen_maps) < 12:
                queue.append(loc)
            elif same_site(loc, root) and crawlable(loc):
                found.append(loc)
    ordered, seen = [], set()
    for u in found:
        if key(u) not in seen:
            seen.add(key(u))
            ordered.append(u)
    return ordered[:limit]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    root = normalise(sys.argv[1])
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "pages")

    print(f"crawling {root}")
    try:
        final, ctype, html = get(root)
    except urllib.error.HTTPError as exc:
        print(f"the root returned HTTP {exc.code}. Nothing to crawl.")
        return 1
    except Exception as exc:
        print(f"could not reach the root: {type(exc).__name__}: {exc}")
        print("If this machine also cannot reach it, try a different network.")
        return 1
    if "html" not in ctype.lower():
        print(f"the root is {ctype}, not HTML.")
        return 1

    root = normalise(final)
    pages = [(root, html)]
    seen = {key(root)}
    frontier = [(u, 1) for u in sitemap_seeds(root, MAX_PAGES)]
    frontier += [
        (normalise(urljoin(root, h)), 1)
        for h in LINK_RE.findall(html)
        if not h.startswith(("mailto:", "tel:", "javascript:", "#"))
    ]
    print(f"  [  1] {root}")

    while frontier and len(pages) < MAX_PAGES:
        url, depth = frontier.pop(0)
        if key(url) in seen or not same_site(url, root) or not crawlable(url):
            continue
        seen.add(key(url))
        time.sleep(DELAY_S)
        try:
            final, ctype, body = get(url)
        except Exception as exc:
            print(f"       skip {url}  ({type(exc).__name__})")
            continue
        if "html" not in ctype.lower():
            continue
        # A link can redirect off-site: a payment link lands on the provider's
        # domain. Archiving that makes the agent answer for the wrong company.
        if not same_site(normalise(final), root):
            print(f"       skip {url}  (redirects off-site)")
            continue
        pages.append((normalise(final), body))
        print(f"  [{len(pages):3d}] {url}")
        if depth < MAX_DEPTH:
            for h in LINK_RE.findall(body):
                if h.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                link = normalise(urljoin(url, h))
                if key(link) not in seen and same_site(link, root):
                    frontier.append((link, depth + 1))

    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.html"):
        old.unlink()
    manifest = []
    for url, body in pages:
        name = filename(url)
        (out / name).write_text(body, encoding="utf-8")
        manifest.append({"url": url, "file": name, "status": 200})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    bundle = out.with_suffix(".zip")
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.iterdir()):
            z.write(f, f"{out.name}/{f.name}")

    print(f"\n{len(pages)} pages saved to {out.resolve()}")
    print(f"zipped to {bundle.resolve()}")
    print("\nDrag that zip into the Codespaces file explorer, then run:")
    print(f"  unzip -o {bundle.name} -d data/ && python -m app.ingest --from-dir data/{out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
