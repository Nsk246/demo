"""Save a crawl to disk and read it back.

Some hosts null-route datacenter IP ranges, so the machine that can reach the
site and the machine that runs the demo are not always the same one. Hostinger
does this and it is why rxiedu.com is unreachable from Codespaces.

Splitting crawl from ingest fixes that without a proxy. Crawl on a laptop,
which needs no API key at all, commit the archive, then embed anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

MANIFEST = "manifest.json"
SAFE = re.compile(r"[^a-z0-9._-]+")


def filename(url: str) -> str:
    """Readable, collision-proof, and stable across runs.

    The hash suffix matters: two different URLs can slugify to the same string
    and silently overwrite each other, which shows up much later as a page
    mysteriously missing from the index.
    """
    slug = SAFE.sub("-", url.split("://", 1)[-1].lower()).strip("-")[:80] or "page"
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{slug}-{digest}.html"


def save(pages, directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.html"):
        old.unlink()
    manifest = []
    for page in pages:
        name = filename(page.url)
        (directory / name).write_text(page.html, encoding="utf-8")
        manifest.append({"url": page.url, "file": name, "status": page.status})
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2))
    return directory


def load(directory: str | Path):
    """Read an archive back as the same Page objects crawl returns."""
    from .crawl import Page

    directory = Path(directory)
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"{manifest_path} not found. An archive directory needs the "
            f"manifest written by --save-html, not just loose HTML files."
        )
    pages = []
    for entry in json.loads(manifest_path.read_text()):
        target = directory / entry["file"]
        if not target.is_file():
            continue
        pages.append(
            Page(
                url=entry["url"],
                status=entry.get("status", 200),
                html=target.read_text(encoding="utf-8"),
            )
        )
    return pages
