"""Score retrieval without a phone.

Run this before dialling anything. It is the cheapest way to find out whether
the crawl actually covers the questions a caller will ask, and to set
MIN_SCORE. Tuning that threshold on live calls wastes an afternoon.

    python scripts/eval.py                      # uses questions.txt if present
    python scripts/eval.py "what are your hours" "do you ship to canada"
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import log_config_source  # noqa: E402
from app.embed import embed_query  # noqa: E402
from app.retrieval import Index  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.store import connect, site_row  # noqa: E402

DEFAULT = [
    "what does this company do",
    "where are you located",
    "what are your opening hours",
    "how much does it cost",
    "how do I contact a person",
    "who founded the company",
    "do you offer refunds",
]


async def main() -> int:
    settings = get_settings()
    if not settings.gemini_api_key:
        print(f"GEMINI_API_KEY is empty. Config came from {log_config_source()}.")
        print("If that says MISSING, run: cp .env.example .env, then fill in "
              "the key. Exporting it in one shell does not survive a new "
              "terminal or a Codespace restart, which is what .env is for.")
        return 1
    conn = connect(settings.site_db)
    site = site_row(conn)
    if not site:
        print("no site ingested. run: python -m app.ingest <url>")
        return 1

    questions = sys.argv[1:]
    qfile = Path("questions.txt")
    if not questions and qfile.exists():
        questions = [
            q.strip()
            for q in qfile.read_text().splitlines()
            if q.strip() and not q.strip().startswith("#")
        ]
    questions = questions or DEFAULT

    index = Index(conn, site["dims"])
    lengths = [
        r[0] for r in conn.execute("SELECT length(text) FROM chunks").fetchall()
    ]
    avg = sum(lengths) // max(len(lengths), 1)
    print(f"{site['name']}  ·  {site['page_count']} pages  ·  {index.size} chunks "
          f"·  {avg} chars average")
    if avg < 350:
        print("WARNING: chunks are small. Short text embeds toward the corpus "
              "average, which compresses every score into the same narrow band "
              "and makes the threshold useless.")
    print(f"min_score {settings.min_score}   min_z {settings.min_z}\n")

    misses = 0
    scored: list[tuple[str, float, float, bool]] = []
    for question in questions:
        vec = await embed_query(
            question,
            api_key=settings.gemini_api_key,
            model=settings.embedding_model,
            dims=settings.embedding_dims,
        )
        hits = index.search(
            vec, top_k=settings.top_k, min_score=settings.min_score,
            min_z=settings.min_z,
        )
        st = dict(index.last_stats)
        scored.append((question, st.get("top", 0.0), st.get("z", 0.0), bool(hits)))
        if not hits:
            misses += 1
            print(f"MISS  {question}   top {st.get('top', 0):.2f}  z {st.get('z', 0):.1f}")
        else:
            print(f"{hits[0].score:.2f}  z{st['z']:.1f}  {question}")
            for hit in hits:
                print(f"        {hit.score:.2f}  {hit.heading or hit.title}  {hit.url}")
            # A cluster of near-identical scores across different pages means
            # the same block was indexed on all of them, which is chrome the
            # crawl failed to strip. Same-page clusters are page expansion
            # working as intended, so only distinct URLs count.
            distinct = {h.url for h in hits}
            if len(distinct) > 2 and hits[0].score - hits[-1].score < 0.02:
                print("        ^ near-identical scores across pages: likely "
                      "boilerplate that survived the crawl")
        print()

    print(f"{len(questions) - misses}/{len(questions)} answered from the crawl\n")

    # The control questions at the end of questions.txt are the calibration.
    # Sweeping the threshold against them is the only way to set it that is
    # not guesswork, and it has already overturned one confident theory.
    controls = [r for r in scored if any(
        w in r[0].lower() for w in ("used cars", "hotel", "laptop"))]
    real = [r for r in scored if r not in controls]
    if not controls or not real:
        return 0

    print("threshold sweep, real questions kept against controls rejected:")
    best = None
    for t in [x / 100 for x in range(48, 76, 2)]:
        keeps = sum(1 for r in real if r[1] >= t)
        leaks = sum(1 for r in controls if r[1] >= t)
        clean = leaks == 0
        if clean and best is None:
            best = t
        mark = "  <- lowest clean split" if clean and best == t else ""
        print(f"  min_score >= {t:.2f}   {keeps:2d}/{len(real)} real   "
              f"{leaks}/{len(controls)} controls{mark}")

    if best is None:
        print("\nNo threshold separates them. The crawl is probably missing "
              "the pages these questions need, or the chunks are still too "
              "small for the scores to spread.")
    else:
        kept = sum(1 for r in real if r[1] >= best)
        print(f"\nSet MIN_SCORE={best:.2f} in .env. It keeps {kept}/{len(real)} "
              f"real questions and rejects every control.")
        print("This number is fitted to this site and will not transfer to "
              "another one. Re-run the sweep after any re-crawl.")

    zc = max((r[2] for r in controls), default=0)
    zr = min((r[2] for r in real if r[3]), default=0)
    if zc >= zr:
        print(f"\nz is not usable here: the worst control reached {zc:.1f} "
              f"while a real question sat at {zr:.1f}. Leave MIN_Z at 0.")

    if misses:
        print("\nFor each miss: is the answer actually on the site? If yes, "
              "lower MIN_SCORE. If no, that is correct and the agent will "
              "offer to take a message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
