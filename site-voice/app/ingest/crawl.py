"""Same-origin crawler.

Order of preference: sitemap.xml, then breadth-first link following. Sitemaps
give better coverage in fewer requests and put the important pages first, which
matters because the page budget is small.

robots.txt is honoured. A demo that ignores it is a demo you cannot show to the
company whose site you crawled.

Two rules earned by failing on a real site:

The URL you fetch is not the URL you compare. Stripping `www.` is right for
deciding whether two links are the same page and wrong for deciding what to
request, because plenty of hosts serve only one of the two. Fetching uses the
host as given, then adopts whatever the root redirected to.

A crawl that fetches nothing must say why. "Wrong URL, or the site blocks
crawlers" is a guess, and a guess sends you looking in the wrong place.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

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
# Listing pages built from query parameters. Each is a reshuffle of posts that
# are already indexed individually, so they add near-duplicate chunks and no
# new facts. `?tag=`, `?author=`, `?page=` are the usual three.
SKIP_QUERY = re.compile(r"(^|&)(tag|author|page|paged|s|q|sort|filter)=", re.I)

BOT_UA = "SiteVoiceDemoBot/0.1 (+contact via the site owner)"
# Used only after the polite identifier is refused. Many small-business sites
# sit behind a WAF that rejects anything it does not recognise, including
# well-behaved crawlers, so the choice is this or no demo.
FALLBACK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
BLOCKED_STATUS = {401, 403, 406, 429, 503}
# Connect-level failures worth retrying on a forced IPv4 socket. httpx has no
# Happy Eyeballs: given an AAAA record and a container whose IPv6 route goes
# nowhere, it picks v6 and hangs until timeout. curl falls back in
# milliseconds, which is why the site looks reachable from the same shell.
CONNECT_FAILURES = (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout)


@dataclass
class Page:
    url: str
    status: int
    html: str


@dataclass
class CrawlReport:
    """Everything needed to tell a person what actually happened."""

    root: str = ""
    final_root: str = ""
    robots_status: str = ""
    root_status: str = ""
    content_type: str = ""
    user_agent: str = BOT_UA
    ua_fallback_used: bool = False
    ipv4_forced: bool = False
    sitemap_urls: int = 0
    fetched: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def fail(self, reason: str) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1

    def render(self) -> str:
        lines = [
            f"  root requested   {self.root}",
            f"  root resolved    {self.final_root or '(never resolved)'}",
            f"  root response    {self.root_status or '(no response)'}",
            f"  content type     {self.content_type or '-'}",
            f"  robots.txt       {self.robots_status or '-'}",
            f"  user agent       {'browser fallback' if self.ua_fallback_used else 'SiteVoiceDemoBot'}",
            f"  socket           {'forced IPv4' if self.ipv4_forced else 'default'}",
            f"  sitemap urls     {self.sitemap_urls}",
            f"  pages fetched    {self.fetched}",
        ]
        if self.failures:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.failures.items()))
            lines.append(f"  failures         {detail}")
        lines.extend(f"  note             {n}" for n in self.notes)
        return "\n".join(lines)


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def normalise(url: str, *, for_fetch: bool = True) -> str:
    """Canonicalise a URL.

    `for_fetch=True` keeps the host exactly as written, because that is what
    has to go on the wire. `for_fetch=False` drops `www.` and gives a key for
    comparing and de-duplicating.
    """
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    scheme = (parsed.scheme or "https").lower()
    host = parsed.netloc.lower()
    if not for_fetch:
        host = strip_www(host)
    if (scheme == "https" and host.endswith(":443")) or (
        scheme == "http" and host.endswith(":80")
    ):
        host = host.rsplit(":", 1)[0]
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = parsed.query
    if query:
        keep = [
            p
            for p in query.split("&")
            if p and not p.split("=")[0].lower().startswith(("utm_", "fbclid", "gclid"))
        ]
        query = "&".join(sorted(keep))
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")


def key(url: str) -> str:
    """Identity of a page, ignoring www and scheme."""
    return normalise(url, for_fetch=False).split("://", 1)[-1]


def same_site(url: str, root: str) -> bool:
    return strip_www(urlparse(normalise(url)).netloc) == strip_www(
        urlparse(normalise(root)).netloc
    )


def crawlable(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if SKIP_EXT.search(parsed.path) or SKIP_PATH.search(parsed.path):
        return False
    if parsed.query and SKIP_QUERY.search(parsed.query):
        return False
    return True


LINK_RE = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"'>]+)["']""", re.I)
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def extract_links(html: str, base: str) -> list[str]:
    out = []
    for href in LINK_RE.findall(html):
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append(normalise(urljoin(base, href)))
    return out


def open_client(timeout_s: float, concurrency: int, *, force_ipv4: bool = False):
    """One place that builds the HTTP client, so the IPv4 fallback is a flag."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(
            local_address="0.0.0.0" if force_ipv4 else None,
            limits=httpx.Limits(max_connections=concurrency),
            retries=1,
        ),
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 8.0)),
        headers={"Accept": "text/html,application/xhtml+xml"},
    )


async def _probe_root(client: httpx.AsyncClient, root: str, report: CrawlReport):
    """Fetch the root once, learn the real host, and detect a UA block.

    Doing this before anything else means a wrong hostname, a redirect to a
    different domain, or a WAF rejection is reported as itself rather than as
    an empty crawl.
    """
    for attempt, ua in enumerate((BOT_UA, FALLBACK_UA)):
        try:
            resp = await client.get(root, headers={"User-Agent": ua})
        except CONNECT_FAILURES as exc:
            report.root_status = f"{type(exc).__name__}: {exc}"
            return None, ua
        except Exception as exc:
            report.root_status = f"{type(exc).__name__}: {exc}"
            report.note(
                "the root URL could not be reached at all. Check the spelling "
                "and that the host resolves from this machine."
            )
            return None, ua
        report.root_status = str(resp.status_code)
        report.content_type = resp.headers.get("content-type", "")
        report.final_root = normalise(str(resp.url))
        if resp.status_code in BLOCKED_STATUS and attempt == 0:
            report.note(
                f"the site answered {resp.status_code} to a declared crawler. "
                f"Retrying with a browser user agent."
            )
            continue
        if resp.status_code != 200:
            report.note(
                f"the root returned {resp.status_code}, so there is nothing to "
                f"crawl. Open the URL in a browser and confirm it loads."
            )
            return None, ua
        if "html" not in report.content_type.lower():
            report.note(
                f"the root is {report.content_type or 'an unknown type'}, not "
                f"HTML. This crawler only reads HTML pages."
            )
            return None, ua
        report.user_agent = ua
        report.ua_fallback_used = ua is FALLBACK_UA
        return Page(url=report.final_root, status=200, html=resp.text), ua
    return None, FALLBACK_UA


async def _robots(client: httpx.AsyncClient, root: str, ua: str, report: CrawlReport):
    rp = RobotFileParser()
    target = urljoin(root, "/robots.txt")
    rp.set_url(target)
    try:
        resp = await client.get(target, headers={"User-Agent": ua})
    except Exception as exc:
        report.robots_status = f"unreachable ({type(exc).__name__})"
        rp.parse([])
        return rp
    if resp.status_code == 200 and "html" not in resp.headers.get(
        "content-type", ""
    ).lower():
        report.robots_status = "200, honoured"
        rp.parse(resp.text.splitlines())
    else:
        report.robots_status = f"{resp.status_code}, no rules"
        rp.parse([])
    return rp


async def _sitemap_urls(client, root: str, ua: str, limit: int) -> list[str]:
    found: list[str] = []
    queue = [urljoin(root, "/sitemap.xml"), urljoin(root, "/sitemap_index.xml")]
    seen_maps: set[str] = set()
    while queue and len(found) < limit:
        target = queue.pop(0)
        if target in seen_maps:
            continue
        seen_maps.add(target)
        try:
            resp = await client.get(target, headers={"User-Agent": ua})
        except Exception:
            continue
        if resp.status_code != 200 or "<loc" not in resp.text.lower():
            continue
        for loc in (normalise(u) for u in LOC_RE.findall(resp.text)):
            if loc.endswith(".xml") and len(seen_maps) < 12:
                queue.append(loc)
            elif same_site(loc, root) and crawlable(loc):
                found.append(loc)
    ordered, seen = [], set()
    for url in found:
        if key(url) not in seen:
            seen.add(key(url))
            ordered.append(url)
    return ordered[:limit]


async def crawl(
    root: str,
    *,
    max_pages: int = 120,
    max_depth: int = 3,
    timeout_s: float = 15.0,
    concurrency: int = 6,
    on_page=None,
) -> tuple[list[Page], CrawlReport]:
    report = CrawlReport(root=normalise(root))

    # Try the default socket, then a forced-IPv4 one. Anything that survives
    # both is a real network problem rather than an address-family accident.
    for force_ipv4 in (False, True):
        client = open_client(timeout_s, concurrency, force_ipv4=force_ipv4)
        first, ua = await _probe_root(client, report.root, report)
        if first is not None:
            report.ipv4_forced = force_ipv4
            break
        await client.aclose()
        if force_ipv4 or "Connect" not in report.root_status:
            break
        report.note(
            "the connection timed out on the default socket. Retrying on IPv4 "
            "only, which is the usual fix inside a container with no working "
            "IPv6 route."
        )

    if first is None:
        if "Connect" in report.root_status:
            report.note(
                "IPv4 timed out too. Either this network cannot reach the site "
                "or the site drops traffic from it. Run scripts/netcheck.py to "
                "tell those apart."
            )
        return [], report

    # Not `async with`: the client is already open from the probe above, and
    # httpx refuses to re-enter one. Close it in the finally instead.
    try:

        # Follow the root's own redirect rather than arguing with it: a site
        # that sends the apex to www knows better than we do.
        root = report.final_root
        rp = await _robots(client, root, ua, report)

        def allowed(url: str) -> bool:
            try:
                return rp.can_fetch(ua, url)
            except Exception:
                return True

        seeds = await _sitemap_urls(client, root, ua, max_pages)
        report.sitemap_urls = len(seeds)
        if not seeds:
            report.note("no sitemap found, falling back to following links")

        pages: list[Page] = [first]
        seen: set[str] = {key(first.url)}
        if on_page:
            on_page(first, 1)
        frontier: list[tuple[str, int]] = [(u, 1) for u in seeds]
        frontier += [(u, 1) for u in extract_links(first.html, first.url)]
        sem = asyncio.Semaphore(concurrency)

        async def fetch(url: str) -> Page | None:
            async with sem:
                try:
                    resp = await client.get(url, headers={"User-Agent": ua})
                except Exception as exc:
                    report.fail(type(exc).__name__)
                    return None
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200:
                report.fail(f"http {resp.status_code}")
                return None
            if "html" not in ctype.lower():
                report.fail("not html")
                return None
            final = normalise(str(resp.url))
            # A link can redirect off-site. Checking only the requested URL
            # lets a payment or booking provider's pages into the archive,
            # and the agent then answers questions about the wrong company.
            if not same_site(final, root):
                report.fail("redirected off-site")
                return None
            return Page(url=final, status=200, html=resp.text)

        while frontier and len(pages) < max_pages:
            batch: list[tuple[str, int]] = []
            while (
                frontier
                and len(batch) < concurrency
                and len(pages) + len(batch) < max_pages
            ):
                url, depth = frontier.pop(0)
                if key(url) in seen or not same_site(url, root):
                    continue
                if not crawlable(url):
                    continue
                if not allowed(url):
                    report.fail("robots disallow")
                    continue
                seen.add(key(url))
                batch.append((url, depth))
            if not batch:
                continue
            for (url, depth), page in zip(
                batch, await asyncio.gather(*(fetch(u) for u, _ in batch))
            ):
                if page is None:
                    continue
                pages.append(page)
                if on_page:
                    on_page(page, len(pages))
                if depth >= max_depth:
                    continue
                for link in extract_links(page.html, page.url):
                    if key(link) not in seen and same_site(link, root):
                        frontier.append((link, depth + 1))

        report.fetched = len(pages)
        return pages, report
    finally:
        await client.aclose()
