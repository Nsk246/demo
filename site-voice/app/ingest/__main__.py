"""Ingest CLI.

    python -m app.ingest https://example.com

Crawls, extracts, chunks, embeds, writes the brief, and replaces the contents
of the SQLite file. Idempotent: run it again and the old site is gone.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import datetime, timezone

from ..embed import embed_documents
from ..config import get_settings
from ..store import connect, pack, save_site
from ..facts import SOURCE as FACTS_SOURCE
from ..facts import load as load_facts
from . import archive
from .boilerplate import strip_repeats
from .brief import generate_brief
from .chunk import chunk_doc
from .crawl import crawl, normalise
from .extract import extract

WALL_HINTS = ("client challenge", "just a moment", "attention required",
              "access denied", "enable javascript", "cloudflare")


async def run(
    root: str,
    *,
    max_pages: int,
    max_depth: int,
    db: str | None,
    save_html: str | None = None,
    from_dir: str | None = None,
    crawl_only: bool = False,
) -> int:
    settings = get_settings()
    if not settings.gemini_api_key and not crawl_only:
        print("GEMINI_API_KEY is not set. Check your .env.", file=sys.stderr)
        return 1

    if from_dir:
        pages = archive.load(from_dir)
        print(f"loaded {len(pages)} archived pages from {from_dir}")
        if not pages:
            print(f"{from_dir} holds no pages.", file=sys.stderr)
            return 1
        report = None
    else:
        pages, report = await _crawl(root, max_pages, max_depth, settings)
        if pages is None:
            return 1
        if save_html:
            archive.save(pages, save_html)
            print(f"archived {len(pages)} pages to {save_html}")
            if crawl_only:
                print("crawl only, stopping here. Commit that directory, then "
                      "run: python -m app.ingest --from-dir " + save_html)
                return 0

    docs, walled, thin = [], 0, 0
    for page in pages:
        doc = extract(page.url, page.html)
        if any(h in doc.title.lower() for h in WALL_HINTS) or (
            doc.words == 0 and len(page.html) > 500
        ):
            walled += 1
            continue
        if doc.words < 25:
            thin += 1
            continue
        docs.append(doc)

    docs, dropped = strip_repeats(docs)
    print(f"\n{len(docs)} usable pages, {thin} too thin, {walled} blocked by a bot wall")
    if dropped:
        print(f"{dropped} repeated lines removed as cross-page chrome "
              f"(testimonials, shared FAQ, product cards)")
    if walled > len(pages) // 3:
        print("WARNING: most pages were blocked. The brief will be thin and the "
              "demo will sound vague. Try a different site or ask for access.")
    if not docs:
        return 1

    chunks = []
    for doc in docs:
        chunks.extend(chunk_doc(doc.url, doc.title, doc.text))

    # Facts the business supplied are indexed too, so lookup_site can find
    # them for questions the summary is too short to cover.
    facts = load_facts(settings.facts_file)
    if facts:
        fact_chunks = chunk_doc(FACTS_SOURCE, "Provided by the business", facts)
        chunks.extend(fact_chunks)
        print(f"{len(fact_chunks)} chunks from {settings.facts_file} "
              f"(facts the website does not contain)")
    else:
        print(f"no {settings.facts_file}. Anything the website omits will get "
              f"'that is not on the site', which is correct but makes for a "
              f"thin demo. Run: python -m app.ingest --write-facts-template")
    print(f"{len(chunks)} chunks, {sum(len(c.text) for c in chunks) // 1000}k chars")
    if report is not None and report.failures:
        print("fetch failures: " + ", ".join(
            f"{k}={v}" for k, v in sorted(report.failures.items())))

    print("writing brief...")
    meta = await generate_brief(
        docs, api_key=settings.gemini_api_key, model=settings.gemini_text_model
    )
    print(f"  name: {meta['name']}")
    print(f"  greeting: {meta['greeting']}")
    print(f"  brief: {len(meta['brief'].split())} words")

    print(f"embedding {len(chunks)} chunks with {settings.embedding_model}...")
    vectors = await embed_documents(
        [f"{c.title}\n{c.heading}\n{c.text}" for c in chunks],
        api_key=settings.gemini_api_key,
        model=settings.embedding_model,
        dims=settings.embedding_dims,
    )

    conn = connect(db or settings.site_db)
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM pages")
    for doc in docs:
        conn.execute(
            "INSERT OR REPLACE INTO pages (url,title,description,words,text)"
            " VALUES (?,?,?,?,?)",
            (doc.url, doc.title, doc.description, doc.words, doc.text),
        )
    if facts:
        conn.execute(
            "INSERT OR REPLACE INTO pages (url,title,description,words,text)"
            " VALUES (?,?,?,?,?)",
            (FACTS_SOURCE, "Provided by the business", "", len(facts.split()), facts),
        )
    for chunk, vec in zip(chunks, vectors):
        conn.execute(
            "INSERT INTO chunks (url,title,heading,text,embedding) VALUES (?,?,?,?,?)",
            (chunk.url, chunk.title, chunk.heading, chunk.text, pack(vec)),
        )
    conn.commit()
    save_site(
        conn,
        root_url=root,
        name=meta["name"] or root,
        brief=meta["brief"],
        greeting=meta["greeting"],
        crawled_at=datetime.now(timezone.utc).strftime("%d %B %Y"),
        page_count=len(docs),
        dims=settings.embedding_dims,
        facts=facts,
    )
    conn.close()
    print(f"\ndone. {db or settings.site_db} is ready. restart the service to load it.")
    return 0


async def _crawl(root, max_pages, max_depth, settings):
    root = normalise(root)
    print(f"crawling {root} (max {max_pages} pages, depth {max_depth})")
    pages, report = await crawl(
        root,
        max_pages=max_pages,
        max_depth=max_depth,
        timeout_s=settings.crawl_timeout_s,
        concurrency=settings.crawl_concurrency,
        on_page=lambda p, n: print(f"  [{n:3d}] {p.url}"),
    )
    if not pages:
        print("\nnothing fetched. what happened:\n", file=sys.stderr)
        print(report.render(), file=sys.stderr)
        print(
            "\nIf the raw TCP connect timed out, the site is dropping traffic "
            "from this network rather than refusing it. Run the crawl on a "
            "machine with a residential connection:\n"
            "  python -m app.ingest <url> --crawl-only --save-html data/pages\n"
            "then commit data/pages and run here:\n"
            "  python -m app.ingest --from-dir data/pages",
            file=sys.stderr,
        )
        return None, report
    if report.final_root != report.root:
        print(f"note: {report.root} redirected to {report.final_root}")
    if report.ua_fallback_used:
        print("note: the site refused a declared crawler, so a browser user "
              "agent was used instead")
    if report.ipv4_forced:
        print("note: the default socket timed out, IPv4 was forced")
    return pages, report


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.ingest")
    parser.add_argument("url", nargs="?")
    parser.add_argument(
        "--write-facts-template",
        action="store_true",
        help="write a starter facts file and exit",
    )
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument(
        "--save-html",
        metavar="DIR",
        help="archive the fetched pages so ingest can run on another machine",
    )
    parser.add_argument(
        "--from-dir",
        metavar="DIR",
        help="skip the crawl and read an archive written by --save-html",
    )
    parser.add_argument(
        "--crawl-only",
        action="store_true",
        help="stop after archiving. Needs no API key.",
    )
    args = parser.parse_args()
    settings = get_settings()
    if args.write_facts_template:
        from ..facts import TEMPLATE

        target = pathlib.Path(settings.facts_file)
        if target.exists():
            raise SystemExit(f"{target} already exists, leaving it alone")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(TEMPLATE)
        print(f"wrote {target}. Edit it, then run ingest.")
        raise SystemExit(0)
    if not args.url and not args.from_dir:
        parser.error("a url is required, or --from-dir to use an archive")
    if args.crawl_only and not args.save_html:
        parser.error("--crawl-only needs --save-html DIR")
    raise SystemExit(
        asyncio.run(
            run(
                args.url or "",
                max_pages=args.max_pages or settings.max_pages,
                max_depth=args.max_depth or settings.max_depth,
                db=args.db,
                save_html=args.save_html,
                from_dir=args.from_dir,
                crawl_only=args.crawl_only,
            )
        )
    )


if __name__ == "__main__":
    main()
