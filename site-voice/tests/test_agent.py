"""Prompt assembly and tool dispatch."""
import asyncio

import pytest

from app import agent as agent_mod
from app.retrieval import Index
from app.store import connect, pack


def test_prompt_states_the_crawl_date_and_the_current_time():
    """Without the current date the model guesses what day it is and answers
    'are you open tomorrow' with confidence."""
    text = agent_mod.build(name="Acme", brief="Acme roofs things.",
                           crawled_at="9 August 2026", tz="America/Chicago")
    assert "Acme" in text
    assert "9 August 2026" in text
    assert "America/Chicago" in text
    assert "Acme roofs things." in text


def test_prompt_says_what_to_do_with_an_empty_brief():
    text = agent_mod.build(name="", brief="", crawled_at="today")
    assert "lookup_site for everything" in text


def test_greeting_is_a_stage_direction_not_dialogue():
    """The model reads dialogue out verbatim, brackets and all."""
    assert agent_mod.greeting("Acme").startswith("(")


def test_tool_schemas_require_a_self_contained_question():
    lookup = next(t for t in agent_mod.TOOL_SCHEMAS if t["name"] == "lookup_site")
    assert lookup["parameters"]["required"] == ["question"]
    assert "pronouns" in lookup["parameters"]["properties"]["question"]["description"]


class Settings:
    gemini_api_key = "x"
    embedding_model = "m"
    embedding_dims = 3
    top_k = 4
    min_score = 0.5
    min_z = 0.0  # tiny fixture corpus, the distribution gate is not the subject


@pytest.fixture()
def index(tmp_path):
    conn = connect(tmp_path / "s.db")
    conn.execute("INSERT INTO pages (url,title) VALUES ('https://x/pricing','P')")
    conn.execute(
        "INSERT INTO chunks (id,url,title,heading,text,embedding) VALUES (1,?,?,?,?,?)",
        ("https://x/pricing", "P", "Pricing", "forty dollars a month" * 8, pack([1.0, 0, 0])),
    )
    conn.commit()
    return Index(conn, 3)


@pytest.mark.asyncio
async def test_a_failed_embed_never_becomes_an_invented_answer(index, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("network")

    monkeypatch.setattr("app.embed.embed_query", boom)
    d = agent_mod.ToolDispatcher(index=index, settings=Settings())
    out = await d.dispatch("lookup_site", {"question": "how much"})
    assert out["found"] is False
    assert "message" in out["hint"]


@pytest.mark.asyncio
async def test_a_hit_reports_its_source(index, monkeypatch):
    async def vec(*a, **k):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr("app.embed.embed_query", vec)
    seen = []
    d = agent_mod.ToolDispatcher(index=index, settings=Settings(), on_sources=seen.extend)
    out = await d.dispatch("lookup_site", {"question": "how much"})
    assert out["found"] is True
    assert out["passages"][0]["source"] == "https://x/pricing"
    assert seen == ["https://x/pricing"]


@pytest.mark.asyncio
async def test_an_empty_question_is_a_miss_not_a_crash(index):
    d = agent_mod.ToolDispatcher(index=index, settings=Settings())
    assert (await d.dispatch("lookup_site", {}))["found"] is False


@pytest.mark.asyncio
async def test_take_message_is_recorded(index):
    d = agent_mod.ToolDispatcher(index=index, settings=Settings())
    out = await d.dispatch("take_message", {"caller_name": "Sam", "message": "call back"})
    assert out["saved"] is True
    assert d.messages[0]["caller_name"] == "Sam"


def test_provided_facts_are_labelled_as_not_from_the_website():
    """The agent must never claim a supplied fact is 'on their pricing page'."""
    text = agent_mod.build(
        name="Acme", brief="Acme roofs things.", crawled_at="today",
        facts="## Hours\nSaturdays 10am.",
    )
    assert "Saturdays 10am." in text
    assert "not on the website" in text
    assert "never say they came from a web page" in text


def test_no_facts_means_no_dangling_header():
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today", facts="")
    assert "FACTS THE BUSINESS PROVIDED" not in text


@pytest.mark.asyncio
async def test_a_slow_lookup_is_never_reported_as_not_on_the_site():
    """A timeout and a genuine miss are different statements to a caller.

    On a real call every lookup timed out at exactly 2500ms and the agent
    told the caller the answer was not on the site. It was; the search never
    finished.
    """
    from app.providers.base import ProviderEvent
    from app.providers.mock import MockProvider
    from app.telephony.bridge import MediaBridge
    from tests.test_bridge import STOP, FakeTwilioWS, media_msg, quiet, start_msg

    ws = FakeTwilioWS([start_msg()] + [media_msg(quiet()) for _ in range(40)] + [STOP])
    provider = MockProvider(
        [ProviderEvent(kind="tool_call", tool_call_id="fc1",
                       tool_name="lookup_site", tool_args={"question": "price"})]
    )

    async def never(name, args):
        await asyncio.sleep(30)

    bridge = MediaBridge(ws, provider, dispatch_tool=never, stall_after_ms=40,
                         tool_timeout_ms=120)
    await asyncio.wait_for(bridge.run(), timeout=5)

    result = provider.tool_results[0]["result"]
    assert result["error"] == "timeout"
    assert "found" not in result, "a timeout must not look like a miss"
    assert "not on the site" in result["hint"]


def test_a_query_embed_cannot_sleep_longer_than_the_tool_budget():
    """Ingest can afford exponential backoff. A caller on the line cannot."""
    import inspect

    from app import embed

    src = inspect.getsource(embed.embed_query)
    assert "attempts=2" in src
    assert "backoff_base=0.25" in src


def test_the_embedding_client_is_reused():
    """Building a genai.Client does a TLS handshake. Doing that per lookup
    spent most of the tool budget before the request was sent."""
    import inspect

    from app import embed

    assert "_CLIENTS" in inspect.getsource(embed)
    assert "genai.Client" not in inspect.getsource(embed.embed_texts)
