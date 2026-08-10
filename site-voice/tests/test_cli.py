"""The ingest CLI end to end.

Written because a rename made a patch miss silently: `main()` started passing
arguments `run()` did not accept, `--help` still looked right, and the failure
only appeared on the machine actually running it. Testing the pieces is not
the same as testing the command.
"""
import json

import pytest

from app.ingest import __main__ as cli
from app.ingest import archive
from app.ingest.crawl import CrawlReport, Page

PAGE = (
    "<html><head><title>Acme Robotics</title></head><body><main>"
    "<h1>Programs</h1><p>" + "The LEGO Basic programme costs one hundred and fifty nine dollars. " * 6
    + "</p></main></body></html>"
)


def make_pages(n=8):
    return [
        Page(url=f"https://acme.test/p{i}", status=200, html=PAGE.replace("Programs", f"Page {i}"))
        for i in range(n)
    ]


@pytest.fixture()
def offline(monkeypatch, tmp_path):
    """No network, no model. Only the CLI's own wiring is under test."""
    async def fake_crawl(root, **kw):
        on_page = kw.get("on_page")
        pages = make_pages()
        for i, p in enumerate(pages, 1):
            if on_page:
                on_page(p, i)
        report = CrawlReport(root=root, final_root=root, fetched=len(pages))
        return pages, report

    async def fake_brief(docs, **kw):
        return {"name": "Acme Robotics", "brief": "Acme teaches robotics.",
                "greeting": "Acme Robotics, how can I help?"}

    async def fake_embed(texts, **kw):
        return [[float(len(t) % 7), 1.0, 0.0] for t in texts]

    monkeypatch.setattr(cli, "crawl", fake_crawl)
    monkeypatch.setattr(cli, "generate_brief", fake_brief)
    monkeypatch.setattr(cli, "embed_documents", fake_embed)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("EMBEDDING_DIMS", "3")
    monkeypatch.setenv("SITE_DB", str(tmp_path / "site.db"))
    monkeypatch.setenv("FACTS_FILE", str(tmp_path / "facts.md"))
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_crawl_only_archives_and_stops(offline):
    """The half that runs on a laptop. It must not need an API key."""
    out = offline / "pages"
    code = await cli.run(
        "https://acme.test", max_pages=20, max_depth=2, db=None,
        save_html=str(out), crawl_only=True,
    )
    assert code == 0
    assert (out / archive.MANIFEST).is_file()
    assert len(json.loads((out / archive.MANIFEST).read_text())) == 8
    assert not (offline / "site.db").exists(), "crawl-only must not build the index"


@pytest.mark.asyncio
async def test_crawl_only_works_without_an_api_key(offline, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()
    code = await cli.run(
        "https://acme.test", max_pages=20, max_depth=2, db=None,
        save_html=str(offline / "pages"), crawl_only=True,
    )
    assert code == 0


@pytest.mark.asyncio
async def test_from_dir_builds_the_index_without_crawling(offline, monkeypatch):
    out = offline / "pages"
    archive.save(make_pages(), out)

    async def explode(*a, **k):
        raise AssertionError("--from-dir must not touch the network")

    monkeypatch.setattr(cli, "crawl", explode)
    code = await cli.run(
        "", max_pages=20, max_depth=2, db=str(offline / "site.db"), from_dir=str(out)
    )
    assert code == 0

    from app.store import connect, site_row

    conn = connect(offline / "site.db")
    row = site_row(conn)
    assert row["name"] == "Acme Robotics"
    assert row["page_count"] == 8
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0


@pytest.mark.asyncio
async def test_an_empty_archive_directory_fails_loudly(offline):
    empty = offline / "nothing"
    empty.mkdir()
    (empty / archive.MANIFEST).write_text("[]")
    assert await cli.run("", max_pages=5, max_depth=1, db=None, from_dir=str(empty)) == 1


def test_main_passes_exactly_what_run_accepts():
    """The specific failure: main() grew arguments run() did not have."""
    import inspect

    accepted = set(inspect.signature(cli.run).parameters)
    for name in ("save_html", "from_dir", "crawl_only", "max_pages", "max_depth", "db"):
        assert name in accepted, f"run() is missing {name}"


def test_crawl_only_without_save_html_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ingest", "https://acme.test", "--crawl-only"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "--save-html" in capsys.readouterr().err
