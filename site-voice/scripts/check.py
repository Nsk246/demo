#!/usr/bin/env python3
"""Pre-package checks: the mistakes that have actually cost us hours.

Parses rather than greps, so a comment explaining a bug is not mistaken for
the bug. Carried over from the restaurant build, plus two checks specific to
this demo.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
TESTS = ROOT / "tests"

failures: list[str] = []


def say(label: str, ok: bool, detail: str = "") -> None:
    print(f"{label:<54} {'ok' if ok else 'FAIL'}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def fixed_depth_parents() -> list[str]:
    """`parents[3]` on /srv/app/main.py raises IndexError at import and the
    container dies before serving anything. Walk ancestors instead."""
    hits = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)
            ):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return hits


def paced_sleep_in_bridge() -> list[str]:
    """Sending one frame then sleeping 20ms looks like correct real-time
    pacing and is not: asyncio.sleep overshoots every iteration, so a long
    reply drifts hundreds of milliseconds late and the backlog compounds.
    Twilio buffers inbound media itself. Any nonzero sleep in the outbound
    pump is this bug coming back."""
    src = (APP / "telephony" / "bridge.py").read_text()
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name == "_pace_outbound"):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "sleep"
                and inner.args
                and not (
                    isinstance(inner.args[0], ast.Constant) and inner.args[0].value == 0
                )
            ):
                hits.append(f"bridge.py:{inner.lineno}")
    return hits


def audio_read_from_response_data() -> bool:
    """Gemini Live puts audio on `response.data`. Walking
    server_content.model_turn.parts[].inline_data instead yields a session
    that transcribes correctly and plays nothing."""
    return "response, \"data\"" in (APP / "providers" / "gemini.py").read_text().replace(
        "'", '"'
    )


def cli_signature_matches() -> list[str]:
    """`main()` calling `run()` with an argument it does not accept parses
    fine, passes --help, and dies only when someone runs the command. A rename
    that made one patch miss silently is how that shipped once already."""
    tree = ast.parse((APP / "ingest" / "__main__.py").read_text())
    accepted: set[str] = set()
    passed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            args = node.args
            accepted = {a.arg for a in args.args + args.kwonlyargs}
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run"
        ):
            passed = {kw.arg for kw in node.keywords if kw.arg}
    return sorted(passed - accepted)


def env_dependent_tests() -> list[str]:
    """Tests must not reload the settings module.

    Settings read the developer's own .env, so such a test passes on a machine
    without one and fails on a machine with one. The same class of bug already
    shipped here once as a test that read the real data/site.db.
    """
    hits = []
    for path in TESTS.rglob("*.py"):
        source = path.read_text()
        if "importlib.reload" in source and "config" in source:
            for i, line in enumerate(source.splitlines()):
                if "importlib.reload" in line:
                    hits.append(f"{path.relative_to(ROOT)}:{i + 1}")
    return hits


def tests_use_a_sandbox_db() -> bool:
    """The suite must point at a temporary database, never data/site.db."""
    return "site-voice-tests-" in (TESTS / "conftest.py").read_text()


def no_live_model_in_tests() -> bool:
    """The suite must never call a live model: slow, costs money per run, and
    the result depends on what the model felt like returning."""
    return 'os.environ["GEMINI_API_KEY"] = ""' in (TESTS / "conftest.py").read_text()


def db_not_committed() -> list[str]:
    """The SQLite file ships inside the image, so a stale one silently
    deploys yesterday's crawl. It must be built by ingest, not by git."""
    ignore = (ROOT / ".gitignore").read_text()
    return [] if "data/*.db" in ignore else ["data/*.db is not gitignored"]


if __name__ == "__main__":
    hits = fixed_depth_parents()
    say("no fixed-depth parents[N]", not hits, " ".join(hits))
    hits = paced_sleep_in_bridge()
    say("no reintroduced pacing sleep", not hits, " ".join(hits))
    say("gemini audio read from response.data", audio_read_from_response_data())
    hits = cli_signature_matches()
    say("ingest CLI signature matches its caller", not hits, " ".join(hits))
    say("tests never reach a live model", no_live_model_in_tests())
    say("tests use a sandbox database", tests_use_a_sandbox_db())
    hits = env_dependent_tests()
    say("no tests reloading the settings module", not hits, " ".join(hits))
    hits = db_not_committed()
    say("crawled db is gitignored", not hits, " ".join(hits))
    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        sys.exit(1)
    print("all checks passed")
