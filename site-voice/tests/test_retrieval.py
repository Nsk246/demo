import numpy as np
import pytest

from app.retrieval import Index, render
from app.store import connect, pack


@pytest.fixture()
def index(tmp_path):
    conn = connect(tmp_path / "s.db")
    rows = [
        ("https://x/pricing", [1.0, 0.0, 0.0]),
        ("https://x/pricing", [0.98, 0.02, 0.0]),
        ("https://x/about", [0.0, 1.0, 0.0]),
    ]
    for url, _ in rows:
        conn.execute("INSERT OR IGNORE INTO pages (url,title) VALUES (?,?)", (url, "T"))
    for i, (url, vec) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO chunks (id,url,title,heading,text,embedding) VALUES (?,?,?,?,?,?)",
            (i, url, "T", f"H{i}", "x" * 200, pack(vec)),
        )
    conn.commit()
    return Index(conn, 3)


def test_returns_one_chunk_per_page(index):
    hits = index.search([1.0, 0.05, 0.0], top_k=3, min_score=0.2)
    assert [h.url for h in hits] == ["https://x/pricing"]


def test_threshold_excludes_weak_matches(index):
    assert index.search([0.0, 0.0, 1.0], min_score=0.5) == []


def test_render_tells_the_model_what_to_do_when_empty():
    out = render([])
    assert out["found"] is False
    assert "message" in out["hint"].lower()


def test_render_keeps_source_per_passage(index):
    out = render(index.search([1.0, 0.0, 0.0], min_score=0.2))
    assert out["found"] is True
    assert out["passages"][0]["source"] == "https://x/pricing"


def test_empty_index_is_not_an_error(tmp_path):
    idx = Index(connect(tmp_path / "e.db"), 3)
    assert idx.size == 0
    assert idx.search(np.zeros(3)) == []
