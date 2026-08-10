"""Point the Twilio number at this demo, and put it back afterwards.

The number is shared with the restaurant build, so every swap is recorded to
.webhook-backup.json before it happens. `restore` puts back whatever was there
the first time you ran `point`, so the restaurant demo is always one command
from working again.

    python scripts/webhook.py status
    python scripts/webhook.py point https://site-voice.up.railway.app
    python scripts/webhook.py restore
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twilio.rest import Client  # noqa: E402

from app.config import get_settings, log_config_source  # noqa: E402

BACKUP = Path(__file__).resolve().parent.parent / ".webhook-backup.json"


def client() -> Client:
    """Read credentials the same way the service does.

    Reading os.environ directly meant this script worked only in the one shell
    where the values had been exported, and silently disagreed with the app
    about which account it was talking to.
    """
    s = get_settings()
    if not s.twilio_account_sid or not s.twilio_auth_token:
        sys.exit(
            f"TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are empty. Config came "
            f"from {log_config_source()}."
        )
    return Client(s.twilio_account_sid, s.twilio_auth_token)


def number(cli: Client):
    target = get_settings().twilio_number
    if not target or target == "+1":
        sys.exit("set TWILIO_NUMBER in .env, for example +16155551234")
    found = cli.incoming_phone_numbers.list(phone_number=target, limit=1)
    if not found:
        sys.exit(f"{target} is not on this Twilio account")
    return found[0]


def status() -> None:
    num = number(client())
    print(f"number      {num.phone_number}")
    print(f"voice url   {num.voice_url or '(none)'}")
    print(f"method      {num.voice_method}")
    if BACKUP.exists():
        saved = json.loads(BACKUP.read_text())
        print(f"backup      {saved['voice_url'] or '(none)'}  saved {saved['saved_at']}")
    else:
        print("backup      none recorded yet")


def point(base: str) -> None:
    from datetime import datetime, timezone

    cli = client()
    num = number(cli)
    if not BACKUP.exists():
        BACKUP.write_text(
            json.dumps(
                {
                    "phone_number": num.phone_number,
                    "voice_url": num.voice_url,
                    "voice_method": num.voice_method,
                    "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
                indent=2,
            )
        )
        print(f"backed up {num.voice_url or '(none)'} -> {BACKUP.name}")
    else:
        print(f"backup already exists, leaving it alone ({BACKUP.name})")

    url = base.rstrip("/") + "/twilio/voice"
    num.update(voice_url=url, voice_method="POST")
    print(f"pointed {num.phone_number} at {url}")


def restore() -> None:
    if not BACKUP.exists():
        sys.exit("no backup recorded. nothing to restore.")
    saved = json.loads(BACKUP.read_text())
    num = number(client())
    num.update(voice_url=saved["voice_url"], voice_method=saved["voice_method"] or "POST")
    print(f"restored {num.phone_number} to {saved['voice_url']}")
    print("delete .webhook-backup.json if you want the next `point` to re-record.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "status":
        status()
    elif cmd == "point":
        if len(sys.argv) < 3:
            sys.exit("usage: webhook.py point https://your-host")
        point(sys.argv[2])
    elif cmd == "restore":
        restore()
    else:
        sys.exit(__doc__)
