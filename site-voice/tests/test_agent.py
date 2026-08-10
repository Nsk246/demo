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


def test_prompt_grounds_facts_without_claiming_what_is_missing():
    """The rule must be "check you can point to it", not "you do not know
    hours". Asserting a category is absent is a claim about one site, and it
    was wrong: rxiedu publishes campus hours on its location pages. A prompt
    that denies them makes the agent refuse questions it can answer."""
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "check that you can point to it" in text
    assert "probably charges" in text
    assert "NOT in your summary" not in text
    assert "the answer is not available yet" in text


def test_prompt_never_refers_to_the_business_in_third_person():
    """It regressed to "they offer" on turns answered from the summary."""
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "'We teach ages four to fourteen'" in text
    assert "on their pricing page" not in text


def test_greeting_says_the_name_once():
    """It opened with "RobotiX Institute, this is RobotiX Institute"."""
    assert "Do not say the name twice" in agent_mod.greeting("Acme")


def test_prompt_governs_turn_taking():
    """On real calls the agent asked a follow-up question on nearly every
    turn, said "is there anything else" after almost every answer, and
    stacked two holding phrases in one turn."""
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "A turn ends once the answer is given" in text
    assert "still thinking, not that they have finished" in text
    assert "A lookup is never narrated" in text
    assert "do not say it again" in text


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


# --- session config -----------------------------------------------------------

def _provider(**kw):
    from app.providers.gemini import GeminiLiveProvider

    return GeminiLiveProvider(api_key="k", model="m", **kw)


def test_turn_taking_defaults_cannot_make_the_agent_deaf():
    """prefix_padding_ms is the speech duration required before start-of-speech
    commits, not padding around it. With LOW start sensitivity a caller
    answering "yes" in under a third of a second can be dropped, which trades
    an eager agent for a deaf one on the shortest turns."""
    detection = _provider().build_config("i", [])["realtime_input_config"][
        "automatic_activity_detection"
    ]
    assert "prefix_padding_ms" not in detection
    assert "start_of_speech_sensitivity" not in detection


def test_the_caller_can_always_interrupt():
    realtime = _provider().build_config("i", [])["realtime_input_config"]
    assert realtime["activity_handling"] == "START_OF_ACTIVITY_INTERRUPTS"


def test_endpointing_is_patient_by_default():
    """At 500ms the agent starts talking over anyone who pauses mid-sentence.

    Asserts the declared default, not `Settings()`. Instantiating reads the
    developer's own .env, so the test would pass for whoever has not set the
    value and fail for everyone else. That is the environment-dependent test
    that scripts/check.py exists to prevent, and it slipped in anyway.
    """
    from app.config import Settings

    default = Settings.model_fields["gemini_end_of_speech_ms"].default
    assert default >= 800, f"declared default is {default}"
    detection = _provider(end_of_speech_silence_ms=900).build_config("i", [])[
        "realtime_input_config"
    ]["automatic_activity_detection"]
    assert detection["silence_duration_ms"] == 900
    assert detection["end_of_speech_sensitivity"] == "END_SENSITIVITY_LOW"


def test_the_risky_settings_still_apply_when_asked_for():
    detection = _provider(
        start_of_speech_sensitivity="START_SENSITIVITY_LOW", prefix_padding_ms=200
    ).build_config("i", [])["realtime_input_config"]["automatic_activity_detection"]
    assert detection["start_of_speech_sensitivity"] == "START_SENSITIVITY_LOW"
    assert detection["prefix_padding_ms"] == 200


def test_a_bad_enum_fails_at_construction_not_on_a_call():
    """An unrecognised value fails the handshake, and a failed handshake is a
    silent line."""
    with pytest.raises(ValueError, match="END_SENSITIVITY"):
        _provider(end_of_speech_sensitivity="low")
    with pytest.raises(ValueError, match="START_SENSITIVITY"):
        _provider(start_of_speech_sensitivity="high")


def test_config_still_carries_prompt_tools_and_transcription():
    cfg = _provider().build_config("the instructions", [{"name": "lookup_site"}])
    assert cfg["system_instruction"] == "the instructions"
    assert cfg["tools"] == [{"function_declarations": [{"name": "lookup_site"}]}]
    assert cfg["response_modalities"] == ["AUDIO"]
    assert "input_audio_transcription" in cfg
    assert "output_audio_transcription" in cfg


def test_the_closing_question_is_forbidden_outright():
    """Conditional wording was obeyed about half the time: the agent still
    asked twice in a five-turn call. A flat prohibition is easier to follow
    and shorter to state."""
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "Never ask if there is anything else" in text
    assert text.count("anything else") == 1


def test_generic_questions_are_banned_but_earned_offers_are_not():
    """Banning every follow-up removed the repetition and the reason for the
    call along with it. A generic "which program interests you?" is filler; a
    free trial offered after describing a programme is why they rang."""
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "A generic question is never fine" in text
    assert "free trial class if you" in text


def test_offers_are_capped_and_never_repeated():
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "Two offers in an entire call is the ceiling" in text
    assert "never repeat one" in text
    assert "no further offers follow" in text


def test_offers_must_come_from_the_site():
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "Never invent an offer" in text


def test_factual_runs_do_not_get_offers():
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "Facts get facts" in text


def test_prices_must_carry_their_unit():
    """It said "one hundred fifty nine per week", dropping the currency."""
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "with the unit included" in text


def test_no_instruction_can_be_read_out_as_a_line():
    """The agent said "Wait." and "We spell that out" on a real call. Both
    were prompt instructions spoken verbatim instead of followed. Short
    second-person imperatives are the ones that leak, so the rules are worded
    as descriptions of behaviour."""
    import re

    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    for line in text.split("\n"):
        assert not re.search(r"(^|\. )(wait|stop|spell out)\b", line, re.I), line
    assert "None of their\nwording is ever spoken aloud" in text


def test_contact_details_are_spoken_character_by_character():
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "digit by digit" in text
    assert "one character at a time" in text


def test_a_correction_from_the_caller_permits_another_search():
    """The caller said "No, I mean Python coding" and the agent restated the
    same vague answer. A correction is new information, not a repeat."""
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today")
    assert "search once more with their exact words" in text
    assert "Repeating yourself is never the right response to being corrected" in text
