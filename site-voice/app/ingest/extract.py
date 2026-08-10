"""Boilerplate removal.

Nav, footer, and cookie banners repeat on every page. Left in, they dominate
the embedding space and every query retrieves the header. Stripped, a small
site fits comfortably in a few thousand tokens.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

DROP = (
    "script", "style", "noscript", "svg", "iframe", "nav", "footer", "header",
    "aside", "form", "template", "picture", "video", "audio",
)
DROP_HINT = re.compile(
    r"(nav|menu|footer|header|cookie|consent|banner|sidebar|breadcrumb|social|"
    r"share|subscribe|newsletter|popup|modal|skip-link)",
    re.I,
)
WS = re.compile(r"[ \t\r\f\v]+")
BLANKS = re.compile(r"\n{3,}")


@dataclass
class Doc:
    url: str
    title: str
    description: str = ""
    text: str = ""
    headings: list[str] = field(default_factory=list)

    @property
    def words(self) -> int:
        return len(self.text.split())


def _clean(text: str) -> str:
    text = WS.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return BLANKS.sub("\n\n", "\n".join(l for l in lines if l))


def extract(url: str, html: str) -> Doc:
    tree = HTMLParser(html)

    title = ""
    if tree.css_first("title"):
        title = tree.css_first("title").text(strip=True)
    description = ""
    meta = tree.css_first('meta[name="description"]') or tree.css_first(
        'meta[property="og:description"]'
    )
    if meta is not None:
        description = (meta.attributes.get("content") or "").strip()

    for tag in DROP:
        for node in tree.css(tag):
            node.decompose()
    for node in tree.css("[class],[id],[role]"):
        attrs = " ".join(
            filter(None, [node.attributes.get("class"), node.attributes.get("id"),
                          node.attributes.get("role")])
        )
        if attrs and DROP_HINT.search(attrs):
            node.decompose()

    main = (
        tree.css_first("main")
        or tree.css_first("article")
        or tree.css_first('[role="main"]')
        or tree.body
        or tree.root
    )
    if main is None:
        return Doc(url=url, title=title, description=description)

    headings = [h.text(strip=True) for h in main.css("h1,h2,h3") if h.text(strip=True)]

    parts: list[str] = []
    for node in main.css("h1,h2,h3,h4,p,li,td,th,dd,dt,blockquote,figcaption"):
        chunk = node.text(separator=" ", strip=True)
        if not chunk:
            continue
        tag = node.tag
        if tag in ("h1", "h2", "h3", "h4"):
            parts.append(f"\n## {chunk}\n")
        elif tag in ("li", "dd", "dt"):
            parts.append(f"- {chunk}")
        else:
            parts.append(chunk)

    text = _clean("\n".join(parts))

    # A page whose every line repeats elsewhere is navigation we failed to kill.
    deduped, seen = [], set()
    for line in text.split("\n"):
        key = line.strip().lower()
        if key and key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return Doc(url=url, title=title, description=description,
               text=_clean("\n".join(deduped)), headings=headings[:20])
