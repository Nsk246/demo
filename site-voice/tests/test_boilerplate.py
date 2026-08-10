"""Cross-page chrome removal."""
from app.ingest.boilerplate import repeated_lines, strip_repeats


class Doc:
    def __init__(self, url, text):
        self.url, self.text = url, text

    @property
    def words(self):
        return len(self.text.split())


TESTIMONIAL = "My son loves coming here. Great place for kids."
CARD = "VEX IQ Basic Ages 8-10 years $179 / 4 week"


def pages(n=10):
    return [
        Doc(f"https://x/p{i}", f"## Page {i}\nUnique body sentence for page {i}.\n"
                              f"{TESTIMONIAL}\n{CARD}")
        for i in range(n)
    ]


def test_blocks_repeated_on_every_page_are_chrome():
    chrome = repeated_lines(pages())
    assert TESTIMONIAL in chrome
    assert CARD in chrome


def test_unique_content_survives():
    docs, removed = strip_repeats(pages())
    assert removed == 20
    for i, doc in enumerate(docs):
        assert f"Unique body sentence for page {i}." in doc.text
        assert TESTIMONIAL not in doc.text


def test_repeated_headings_are_stripped_too():
    """Keeping them meant 'Frequently Asked Questions' and a registration
    banner appeared on every page and started winning matches. The chunker
    falls back to the page title, which cites better anyway."""
    docs = [Doc(f"https://x/{i}", "## Frequently Asked Questions\nbody " + str(i))
            for i in range(8)]
    docs, removed = strip_repeats(docs)
    assert removed == 8
    assert not any("Frequently Asked Questions" in d.text for d in docs)


def test_a_heading_unique_to_one_page_survives():
    docs = [Doc(f"https://x/{i}", f"## Programme {i}\nunique body {i}\nshared footer line")
            for i in range(8)]
    docs, _ = strip_repeats(docs)
    assert "## Programme 0" in docs[0].text
    assert "shared footer line" not in docs[0].text


def test_small_crawls_are_left_alone():
    """On three pages, two hits looks like boilerplate and usually is not."""
    assert repeated_lines(pages(3)) == set()


def test_a_price_card_cannot_leak_across_products():
    """Asking about one product must not retrieve another product's card."""
    docs, _ = strip_repeats(pages())
    assert not any(CARD in d.text for d in docs)


def test_facts_file_ignores_comment_only_content():
    from app.facts import for_prompt, load

    import tempfile
    import pathlib

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "facts.md"
        p.write_text("<!-- just a comment -->\n<!-- another -->\n")
        assert load(p) == ""
        assert for_prompt(load(p)) == ""
        assert load(pathlib.Path(d) / "missing.md") == ""
