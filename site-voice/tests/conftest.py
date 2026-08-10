"""Test environment.

Settings are read once and cached, so these must be set before anything
imports `app.config`. Putting them here rather than in a test module keeps
import order out of the tests themselves.

The temp database matters as much as the rest. Pointing the suite at the real
`data/site.db` makes /health report whatever happens to be ingested on that
machine, so the same commit passes for one person and fails for another. That
failure mode has already cost this project an afternoon once.
"""

import os
import tempfile
import warnings
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="site-voice-tests-"))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("REALTIME_PROVIDER", "mock")
os.environ.setdefault("PUBLIC_BASE_URL", "https://demo.test")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")
# Never the real one, whatever the developer has ingested locally.
os.environ["SITE_DB"] = str(_SANDBOX / "site.db")
os.environ["FACTS_FILE"] = str(_SANDBOX / "facts.md")
# No real model calls from the suite: slow, costs money per run, and the
# result depends on what the model felt like returning.
os.environ["GEMINI_API_KEY"] = ""

warnings.filterwarnings("ignore", category=DeprecationWarning)
