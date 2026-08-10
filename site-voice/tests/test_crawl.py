from app.ingest.chunk import chunk_doc
from app.ingest.crawl import crawlable, extract_links, key, normalise, same_site
from app.ingest.extract import extract


def test_the_same_page_written_four_ways_has_one_key():
    variants = [
        "https://www.Example.com/about/",
        "https://example.com/about",
        "https://example.com/about#team",
        "https://example.com/about?utm_source=x",
    ]
    assert len({key(v) for v in variants}) == 1


def test_fetching_keeps_www_because_some_hosts_only_serve_one():
    """Stripping www from the URL we request is how a crawl of a working site
    returns zero pages."""
    assert normalise("https://www.example.com/about/") == "https://www.example.com/about"
    assert key("https://www.example.com/about") == "example.com/about"


def test_normalise_keeps_meaningful_query():
    assert normalise("https://example.com/p?id=7&utm_medium=x") == "https://example.com/p?id=7"


def test_same_site_ignores_www():
    assert same_site("https://www.example.com/a", "https://example.com")
    assert not same_site("https://cdn.example.com/a", "https://example.com")


def test_assets_and_admin_paths_are_skipped():
    assert not crawlable("https://example.com/logo.PNG")
    assert not crawlable("https://example.com/wp-admin/x")
    assert not crawlable("https://example.com/checkout")
    assert crawlable("https://example.com/pricing")


def test_extract_links_resolves_relative_and_drops_schemes():
    html = '<a href="/a">x</a><a href="mailto:x@y.com">m</a><a href="tel:123">t</a>'
    assert extract_links(html, "https://example.com/dir/") == ["https://example.com/a"]


HTML = """
<html><head><title>Acme - Pricing</title>
<meta name="description" content="Plans and pricing"></head>
<body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<div class="cookie-banner">We use cookies</div>
<main>
<h1>Pricing</h1>
<p>The starter plan is forty dollars a month and includes two seats.</p>
<h2>Enterprise</h2>
<p>Enterprise pricing is custom. Contact the sales team for a quote today.</p>
<ul><li>Unlimited seats</li><li>Priority support</li></ul>
</main>
<footer>Copyright Acme</footer>
</body></html>
"""


def test_extract_strips_boilerplate_and_keeps_content():
    doc = extract("https://acme.com/pricing", HTML)
    assert doc.title == "Acme - Pricing"
    assert doc.description == "Plans and pricing"
    assert "forty dollars" in doc.text
    assert "cookies" not in doc.text
    assert "Copyright" not in doc.text
    assert "Home" not in doc.text
    assert doc.headings[:2] == ["Pricing", "Enterprise"]


def test_chunks_carry_their_heading():
    doc = extract("https://acme.com/pricing", HTML)
    chunks = chunk_doc(doc.url, doc.title, doc.text)
    assert chunks
    assert all(c.url == "https://acme.com/pricing" for c in chunks)
    assert all(c.heading for c in chunks), 'every chunk needs a label'
    # Sections merge now, so a heading may land in the body rather than
    # the label. What matters is that the text is not lost.
    assert any('Enterprise' in c.heading or 'Enterprise' in c.text
               for c in chunks)


def test_long_text_windows_with_overlap():
    body = "## Section\n" + ("Sentence about the product. " * 200)
    chunks = chunk_doc("https://x/y", "T", body)
    assert len(chunks) > 1
    assert all(len(c.text) <= 1200 for c in chunks)


def test_many_small_sections_merge_into_few_chunks():
    # One chunk per heading gave 777 chunks averaging 171 characters,
    # which compressed every similarity score into one narrow band.
    page = chr(10).join(
        f'## Heading {i}' + chr(10) + f'A short body line for section {i}.'
        for i in range(30)
    )
    chunks = chunk_doc('https://x/y', 'Programs', page)
    assert len(chunks) <= 4, f'{len(chunks)} chunks from 30 short sections'
    assert sum(len(c.text) for c in chunks) / len(chunks) > 400


def test_a_chunk_without_a_heading_falls_back_to_the_page_title():
    chunks = chunk_doc('https://x/y', 'Pricing', 'Body with no heading. ' * 10)
    assert chunks[0].heading == 'Pricing'


# --- crawl reporting ---------------------------------------------------------

import asyncio  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.ingest import crawl as crawl_mod  # noqa: E402
from app.ingest.crawl import FALLBACK_UA, crawl  # noqa: E402

PAGE = b"<html><head><title>T</title></head><body><main><p>" + b"word " * 60 + b"</p></main></body></html>"


def fake_client(monkeypatch, handler):
    """Route every crawler request through a handler.

    Patches the crawler's own client factory rather than httpx.AsyncClient,
    so the real transport plumbing (including the IPv4 fallback) stays out of
    the way and there is no chance of the replacement calling itself.
    """
    monkeypatch.setattr(
        crawl_mod,
        "open_client",
        lambda *a, **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
    )


@pytest.mark.asyncio
async def test_a_ua_block_is_retried_with_a_browser_agent(monkeypatch):
    """Small-business sites sit behind WAFs that reject declared crawlers."""
    seen = []

    def handler(request):
        ua = request.headers.get("user-agent", "")
        seen.append(ua)
        if "SiteVoiceDemoBot" in ua:
            return httpx.Response(403)
        if request.url.path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://blocked.test", max_pages=2)
    assert report.ua_fallback_used
    assert FALLBACK_UA in seen
    assert pages, "the fallback must actually recover the crawl"


@pytest.mark.asyncio
async def test_a_dead_root_reports_the_status_not_a_guess(monkeypatch):
    def handler(request):
        return httpx.Response(404)

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://gone.test", max_pages=2)
    assert pages == []
    assert report.root_status == "404"
    assert any("404" in n for n in report.notes)


@pytest.mark.asyncio
async def test_a_root_redirect_is_adopted_rather_than_argued_with(monkeypatch):
    def handler(request):
        if request.url.host == "apex.test":
            return httpx.Response(301, headers={"location": "https://www.apex.test/"})
        if request.url.path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://apex.test", max_pages=2)
    assert report.final_root == "https://www.apex.test/"
    assert pages


@pytest.mark.asyncio
async def test_a_non_html_root_says_so(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://api.test", max_pages=2)
    assert pages == []
    assert any("not" in n and "HTML" in n for n in report.notes)


@pytest.mark.asyncio
async def test_a_connect_timeout_is_retried_on_ipv4(monkeypatch):
    """httpx has no Happy Eyeballs. Given an AAAA record and a container with
    no IPv6 route it hangs, while curl in the same shell succeeds."""
    attempts = []

    def handler(request):
        if request.url.path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    def factory(*a, force_ipv4=False, **kw):
        attempts.append(force_ipv4)
        if not force_ipv4:
            def boom(request):
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.AsyncClient(transport=httpx.MockTransport(boom))
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        )

    monkeypatch.setattr(crawl_mod, "open_client", factory)
    pages, report = await crawl("https://v6trap.test", max_pages=2)
    assert attempts == [False, True]
    assert report.ipv4_forced
    assert pages, "the IPv4 retry must actually recover the crawl"


@pytest.mark.asyncio
async def test_failing_on_both_sockets_says_which_checks_to_run(monkeypatch):
    def factory(*a, **kw):
        def boom(request):
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.AsyncClient(transport=httpx.MockTransport(boom))

    monkeypatch.setattr(crawl_mod, "open_client", factory)
    pages, report = await crawl("https://dropped.test", max_pages=2)
    assert pages == []
    assert any("netcheck" in n for n in report.notes)


def test_query_listing_pages_are_skipped():
    """?tag= and ?author= reshuffle posts that are already indexed alone. They
    add near-duplicate chunks and no new facts."""
    assert not crawlable("https://x.test/Blogs?tag=Robotics")
    assert not crawlable("https://x.test/Blogs?author=Admin")
    assert not crawlable("https://x.test/Blogs?page=2")
    assert crawlable("https://x.test/Blogs/blog-1")
    assert crawlable("https://x.test/p?id=7")


@pytest.mark.asyncio
async def test_a_link_that_redirects_off_site_is_not_archived(monkeypatch):
    """A payment link lands on the provider's domain. Archived, the agent ends
    up answering questions about the wrong company."""
    def handler(request):
        if request.url.path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        if request.url.host == "acme.test" and request.url.path == "/":
            return httpx.Response(
                200,
                content=b'<html><body><main><a href="/pay">pay</a><p>'
                + b"word " * 60 + b"</p></main></body></html>",
                headers={"content-type": "text/html"},
            )
        if request.url.path == "/pay":
            return httpx.Response(302, headers={"location": "https://billing.other.test/x"})
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://acme.test", max_pages=10)
    assert all("acme.test" in p.url for p in pages), [p.url for p in pages]
    assert report.failures.get("redirected off-site") == 1
