"""Split extracted text into retrievable chunks.

The rule that matters is size. An earlier version emitted one chunk per
heading, which on a heading-dense marketing site produced 777 chunks averaging
171 characters. Short text embeds toward the middle of the corpus, so every
query scored between 0.55 and 0.77 and no threshold could separate a real
match from an unrelated one.

So sections are merged until they reach a useful size, and only split when a
single section is genuinely long. Chunks carry the page URL and the heading
they started under, so the agent can still say where an answer came from.
"""

from __future__ import annotations

from dataclasses import dataclass

TARGET_CHARS = 1100
# Merging stops once a chunk is at least this long, so a chunk is never one
# stray line even when the next heading arrives immediately.
MIN_CHUNK_CHARS = 450
OVERLAP_CHARS = 150
# A section shorter than this is a label, not content, and is merged forward
# rather than kept.
MIN_SECTION_CHARS = 40


@dataclass
class Chunk:
    url: str
    title: str
    heading: str
    text: str


def _sections(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in text.split("\n"):
        if line.startswith("## "):
            out.append((line[3:].strip(), []))
        else:
            out[-1][1].append(line)
    return [(h, "\n".join(l for l in body if l.strip()).strip()) for h, body in out]


def _window(text: str) -> list[str]:
    """Split one long section, breaking on a line or sentence boundary."""
    if len(text) <= TARGET_CHARS:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(start + TARGET_CHARS, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + MIN_CHUNK_CHARS, end)
            if cut == -1:
                cut = text.rfind(". ", start + MIN_CHUNK_CHARS, end)
                cut = cut + 1 if cut != -1 else -1
            if cut != -1:
                end = cut
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP_CHARS, start + 1)
    return [c for c in out if c]


def chunk_doc(url: str, title: str, text: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffer_heading = ""

    def flush() -> None:
        nonlocal buffer, buffer_heading
        body = "\n".join(buffer).strip()
        if len(body) >= MIN_SECTION_CHARS:
            chunks.append(
                Chunk(url=url, title=title, heading=buffer_heading or title, text=body)
            )
        buffer, buffer_heading = [], ""

    for heading, body in _sections(text):
        if not body and not heading:
            continue
        # A long section stands alone. Flush whatever was accumulating first so
        # unrelated text does not get glued to the front of it.
        if len(body) > TARGET_CHARS:
            flush()
            for piece in _window(body):
                chunks.append(
                    Chunk(url=url, title=title, heading=heading or title, text=piece)
                )
            continue

        if not buffer:
            buffer_heading = heading
        piece = f"{heading}\n{body}".strip() if heading else body
        if piece:
            buffer.append(piece)
        if sum(len(b) for b in buffer) >= TARGET_CHARS:
            flush()

    flush()
    return chunks
