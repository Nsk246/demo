"""Crawl on one machine, embed on another.

Needed because some hosts null-route cloud IP ranges, so the machine that can
reach the site is not the machine that runs the demo.
"""
import json

import pytest

from app.ingest import archive
from app.ingest.crawl import Page


def pages():
    return [
        Page(url="https://x.test/", status=200, html="<html>root</html>"),
        Page(url="https://x.test/about", status=200, html="<html>about</html>"),
    ]


def test_roundtrip_preserves_url_and_html(tmp_path):
    archive.save(pages(), tmp_path)
    back = archive.load(tmp_path)
    assert [(p.url, p.html) for p in back] == [(p.url, p.html) for p in pages()]


def test_filenames_are_readable_and_collision_proof():
    a = archive.filename("https://x.test/a/b")
    b = archive.filename("https://x.test/a-b")
    assert a != b, "two urls that slugify alike must not overwrite each other"
    assert "x.test" in a and a.endswith(".html")
    assert archive.filename("https://x.test/a/b") == a, "must be stable"


def test_a_stale_archive_is_replaced_not_merged(tmp_path):
    archive.save(pages(), tmp_path)
    archive.save(pages()[:1], tmp_path)
    assert len(archive.load(tmp_path)) == 1
    assert len(list(tmp_path.glob("*.html"))) == 1


def test_a_directory_without_a_manifest_says_so(tmp_path):
    (tmp_path / "loose.html").write_text("<html></html>")
    with pytest.raises(FileNotFoundError, match="manifest"):
        archive.load(tmp_path)


def test_a_missing_file_named_in_the_manifest_is_skipped(tmp_path):
    archive.save(pages(), tmp_path)
    entries = json.loads((tmp_path / archive.MANIFEST).read_text())
    (tmp_path / entries[0]["file"]).unlink()
    assert len(archive.load(tmp_path)) == 1


def test_the_standalone_crawler_needs_only_the_stdlib():
    """It runs on a laptop with nothing installed. A stray third-party import
    would only be discovered on the machine you are not sitting at."""
    import ast
    import pathlib
    import sys

    source = pathlib.Path("scripts/offline_crawl.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names, (
        f"non-stdlib imports: {sorted(imported - sys.stdlib_module_names)}"
    )


def test_both_crawlers_agree_on_filenames():
    """The two implementations must produce the same archive layout, or an
    archive from one will not load in the other."""
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "offline_crawl", pathlib.Path("scripts/offline_crawl.py")
    )
    offline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(offline)

    for url in (
        "https://www.rxiedu.com/",
        "https://www.rxiedu.com/Programs/lego-basic",
        "https://x.test/a/b",
    ):
        assert offline.filename(url) == archive.filename(url)
