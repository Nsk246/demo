"""Cross-page boilerplate removal.

Per-page stripping handles nav and footer. It does not handle the blocks a
marketing site repeats inside `<main>` on every page: testimonial carousels,
a shared FAQ accordion, "similar programs" cards.

Those are poison for retrieval in two ways. They multiply into near-identical
chunks that crowd out the real answer, and because a card carries a price,
asking about one product retrieves the page of a different one.

The rule is frequency. A line appearing on most pages is chrome, whatever it
looks like. Lines unique to a page are its content.
"""

from __future__ import annotations

from collections import Counter

# A line on this fraction of pages or more is treated as chrome.
REPEAT_RATIO = 0.5
# Below this many pages the ratio is meaningless: on three pages, two hits
# looks like boilerplate and is usually just a shared sentence.
MIN_PAGES = 6
# Long lines are almost never chrome, and dropping one loses real content.
MAX_CHROME_CHARS = 400


def repeated_lines(docs, *, ratio: float = REPEAT_RATIO) -> set[str]:
    if len(docs) < MIN_PAGES:
        return set()
    counts: Counter[str] = Counter()
    for doc in docs:
        seen = set()
        for raw in doc.text.split("\n"):
            line = raw.strip()
            if line.startswith("## "):
                line = line[3:].strip()
            if line:
                seen.add(line)
        for line in seen:
            counts[line] += 1
    threshold = max(2, int(len(docs) * ratio))
    return {
        line
        for line, n in counts.items()
        if n >= threshold and len(line) <= MAX_CHROME_CHARS
    }


def strip_repeats(docs, *, ratio: float = REPEAT_RATIO):
    """Remove cross-page chrome in place and report what went.

    Repeated headings go too. An earlier version kept them so chunks would
    have a label, and the result was that "Have questions?", "Frequently Asked
    Questions", and a registration banner appeared on every page and started
    winning matches. The chunker falls back to the page title for a label,
    which is a better citation anyway.
    """
    chrome = repeated_lines(docs, ratio=ratio)
    if not chrome:
        return docs, 0
    removed = 0
    for doc in docs:
        kept = []
        for line in doc.text.split("\n"):
            stripped = line.strip()
            # Compare without the marker so a repeated heading is caught
            # whether or not the extractor labelled it as one.
            bare = stripped[3:].strip() if stripped.startswith("## ") else stripped
            if bare and (stripped in chrome or bare in chrome):
                removed += 1
                continue
            kept.append(line)
        doc.text = "\n".join(kept).strip()
    return docs, removed
