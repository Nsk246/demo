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
            (i, url, "T", f"H{i}", f"body of chunk {i} " * 20, pack(vec)),
        )
    conn.commit()
    return Index(conn, 3)


def test_caps_chunks_per_page_but_does_not_starve_the_answer(index):
    """One chunk per page hid the detail chunk on the page the query had just
    identified as relevant: "tell me more about X" scored highest on that
    page's marketing blurb and the curriculum never reached the model."""
    hits = index.search([1.0, 0.05, 0.0], top_k=3, min_score=0.2)
    assert {h.url for h in hits} == {"https://x/pricing"}
    assert len(hits) == 2, "both chunks of the winning page should come through"


def test_the_winning_page_is_expanded_below_the_floor(index):
    """Once a page has scored well the question is what it says, not whether
    each paragraph independently clears a threshold."""
    strict = index.search([1.0, 0.0, 0.0], top_k=1, min_score=0.9,
                          expand_top_page=False)
    expanded = index.search([1.0, 0.0, 0.0], top_k=1, min_score=0.9)
    assert len(expanded) > len(strict)
    assert {h.url for h in expanded} == {"https://x/pricing"}


def test_expansion_never_crosses_to_another_page(index):
    hits = index.search([1.0, 0.0, 0.0], top_k=4, min_score=0.2)
    assert all(h.url == hits[0].url for h in hits[1:]) or len(
        {h.url for h in hits}
    ) <= 2


def test_threshold_excludes_weak_matches(index):
    assert index.search([0.0, 0.0, 1.0], min_score=0.5) == []


def test_render_tells_the_model_what_to_do_when_empty():
    out = render([])
    assert out["found"] is False
    hint = out["hint"].lower()
    assert "details" in hint, "the model must be told to offer a callback"
    # The hint steers phrasing, so it must not put "website" in the agent's
    # mouth. A receptionist does not narrate where they read something.
    assert "website" not in hint.replace("without mentioning a website", "")


def test_render_hides_urls_from_the_model(index):
    """Three prompt rules failed to stop the agent mentioning the website,
    because every tool result handed it a URL. It cannot talk about a source
    it never sees; the screen gets citations by a separate path."""
    out = render(index.search([1.0, 0.0, 0.0], min_score=0.2))
    assert out["found"] is True
    passage = out["passages"][0]
    assert "source" not in passage
    assert "section" in passage and "text" in passage
    assert "http" not in str(out), "no URL may reach the model"


def test_empty_index_is_not_an_error(tmp_path):
    idx = Index(connect(tmp_path / "e.db"), 3)
    assert idx.size == 0
    assert idx.search(np.zeros(3)) == []


def _page(tmp_path, vectors):
    conn = connect(tmp_path / "p.db")
    for i, (url, vec) in enumerate(vectors, 1):
        conn.execute("INSERT OR IGNORE INTO pages (url,title) VALUES (?,?)", (url, "T"))
        conn.execute(
            "INSERT INTO chunks (id,url,title,heading,text,embedding) VALUES (?,?,?,?,?,?)",
            (i, url, "T", f"H{i}", f"body {i} " * 30, pack(vec)),
        )
    conn.commit()
    return Index(conn, 3)


def test_a_marginal_top_hit_is_not_expanded(tmp_path):
    """On a real corpus "what happens if we miss a class" scraped the floor at
    0.58 and expansion returned six chunks of the summer camp page, none of
    which answered it. Expansion must enrich a confident match, never inflate
    a marginal one."""
    idx = _page(tmp_path, [("https://x/camp", [1.0, 0.0, 0.0])] * 0 + [
        ("https://x/camp", [1.0, 0.0, 0.0]),
        ("https://x/camp", [0.999, 0.01, 0.0]),
        ("https://x/camp", [0.998, 0.02, 0.0]),
    ])
    # Floor just under the top score: the match only barely clears it.
    marginal = idx.search([1.0, 0.0, 0.0], top_k=4, min_score=0.99)
    assert len(marginal) == 2, "capped at max_per_page, no expansion"
    # Floor well under the top score: the match is confident.
    confident = idx.search([1.0, 0.0, 0.0], top_k=4, min_score=0.5)
    assert len(confident) == 3, "the rest of the winning page comes through"


def test_expansion_adds_at_most_two(tmp_path):
    idx = _page(tmp_path, [("https://x/a", [1.0, 0.0, 0.0])] + [
        ("https://x/a", [1.0 - i * 0.001, i * 0.01, 0.0]) for i in range(1, 8)
    ])
    hits = idx.search([1.0, 0.0, 0.0], top_k=4, min_score=0.5)
    assert len(hits) <= 4, "two from the main loop plus two expanded"


def test_no_tool_hint_tells_the_agent_to_mention_a_website():
    """Three prompt rules banned it and it leaked anyway, because the hint
    attached to every result said "say it is not on the site". The
    instruction and the ban were contradicting each other.

    The word may still appear inside the ban itself, which is why it is
    stripped before checking.
    """
    from app.retrieval import Retrieved, render

    hit = Retrieved(url="https://x/a", title="T", heading="H", text="body", score=0.9)
    for out in (render([]), render([hit])):
        blob = str(out).lower()
        blob = blob.replace("without mentioning a website or a search", "")
        blob = blob.replace("never mention a website, a page, or a search.", "")
        for phrase in ("on the site", "on our site", "website", "http"):
            assert phrase not in blob, f"{phrase!r} still reaches the model: {out}"
