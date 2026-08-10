"""Facts the business gave us that are not on their website.

Every marketing site omits something a caller asks about on the first turn.
For a school it is almost always class days and times. The agent is right to
refuse to guess, but a demo whose first answer is "that isn't on the site" is
a demo that sells nothing.

So there is one plain-text file the business can fill in. Its contents go into
the brief and into the index, labelled as coming from the business rather than
the website, so the agent never claims a fact is "on their pricing page" when
it came from here.

Format is markdown. Lines starting with `## ` become section headings, which
is the same shape the chunker already understands.
"""

from __future__ import annotations

from pathlib import Path

HEADER = (
    "FACTS THE BUSINESS PROVIDED DIRECTLY. These are not on the website. They "
    "are as reliable as anything below, but never say they came from a web "
    "page."
)

SOURCE = "business:facts"


def load(path: str | Path) -> str:
    path = Path(path)
    if not path.is_file():
        return ""
    text = path.read_text().strip()
    # A file of nothing but comments is an empty file.
    body = "\n".join(
        line for line in text.split("\n") if not line.strip().startswith("<!--")
    ).strip()
    return body


def for_prompt(body: str) -> str:
    return f"\n\n{HEADER}\n{body}\n" if body else ""


TEMPLATE = """<!-- Anything a caller asks that the website does not answer.
     Delete a section rather than leaving it blank: an empty heading tells
     the agent the topic exists and gives it nothing to say. -->

## Class days and times
Brentwood: Saturdays 10:00 AM and 11:30 AM, Sundays 2:00 PM.
Murfreesboro: Saturdays 1:00 PM.

## Opening hours for phone and walk-ins
Monday to Friday, 4:00 PM to 7:00 PM. Saturday 9:00 AM to 4:00 PM. Closed Sunday.

## Enrolment
Students can join any week. Billing runs in four-week blocks from the start date.

## Makeup classes
One makeup class per four-week block, subject to space at the same centre.
"""
