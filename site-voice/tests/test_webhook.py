"""Webhook surface: signature URL reconstruction and the TwiML Twilio gets."""
import pytest
from fastapi.testclient import TestClient

from app.config import resolve_base_url
from app.telephony.twilio_webhook import connect_stream_twiml, public_url


class FakeURL:
    def __init__(self, url, scheme, netloc):
        self._url, self.scheme, self.netloc = url, scheme, netloc

    def __str__(self):
        return self._url


class FakeRequest:
    def __init__(self, url, scheme, netloc, headers):
        self.url = FakeURL(url, scheme, netloc)
        self.headers = headers


def test_public_url_rebuilds_what_twilio_actually_signed():
    """Behind a proxy the app sees http and an internal host. Validating
    against that fails every time and surfaces as an unexplained 403."""
    req = FakeRequest(
        "http://10.0.0.4:8000/twilio/voice", "http", "10.0.0.4:8000",
        {"x-forwarded-proto": "https", "x-forwarded-host": "demo.up.railway.app"},
    )
    assert public_url(req) == "https://demo.up.railway.app/twilio/voice"


def test_public_url_is_unchanged_without_proxy_headers():
    req = FakeRequest("https://demo.test/twilio/voice", "https", "demo.test", {})
    assert public_url(req) == "https://demo.test/twilio/voice"


def test_twiml_uses_connect_not_start():
    """<Start><Stream> is listen-only: the caller hears nothing and the logs
    look completely healthy."""
    xml = connect_stream_twiml("wss://demo.test/ws/twilio/CA1")
    assert "<Connect>" in xml and "<Start>" not in xml
    assert 'url="wss://demo.test/ws/twilio/CA1"' in xml


@pytest.mark.parametrize(
    "configured,env,expected",
    [
        ("https://demo.test/", {}, "demo.test"),
        ("", {"RAILWAY_PUBLIC_DOMAIN": "site-voice.up.railway.app"},
         "site-voice.up.railway.app"),
        ("", {"FLY_APP_NAME": "site-voice"}, "site-voice.fly.dev"),
        ("", {}, ""),
    ],
)
def test_base_url_falls_back_to_the_platform(configured, env, expected):
    assert resolve_base_url(configured, env) == expected


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_the_suite_never_touches_the_real_database():
    # A test that reads data/site.db passes for whoever has not ingested
    # yet and fails for everyone else.
    import os

    assert 'site-voice-tests-' in os.environ['SITE_DB'], os.environ['SITE_DB']


def test_health_says_no_site_before_ingest(client):
    body = client.get("/health").json()
    assert body["chunks"] == 0
    assert body["ok"] is False
    assert body["note"] == "no site ingested"


def test_health_admits_the_mock_fallback(client):
    """Asking for gemini with no key used to silently yield a mock while
    /health still said gemini."""
    body = client.get("/health").json()
    assert body["provider"] == "mock"


def test_inbound_call_returns_a_stream_url_with_the_call_sid(client):
    resp = client.post("/twilio/voice", data={"To": "+1615", "From": "+1901", "CallSid": "CA9"})
    assert resp.status_code == 200
    assert 'wss://demo.test/ws/twilio/CA9' in resp.text
