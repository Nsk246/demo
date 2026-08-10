"""In-memory cosine retrieval over the crawled site.

The whole index is a numpy matrix. At demo scale, a hundred pages is a few
thousand chunks, which is under 10 MB at 768 dimensions and searches in well
under a millisecond. A vector database here would add a network hop to a path
that is already competing with a 1.6 second model round trip.
"""
from __future__ import annotations

import numpy as np

from .store import Retrieved, load_matrix


class Index:
    def __init__(self, conn, dims: int):
        self.dims = dims
        self.matrix, self.rows = load_matrix(conn, dims)
        self.last_stats: dict = {}

    @property
    def size(self) -> int:
        return len(self.rows)

    @staticmethod
    def z(scores) -> float:
        std = float(scores.std())
        if std == 0:
            return 0.0
        return (float(scores.max()) - float(scores.mean())) / std

    def search(
        self,
        query_vec,
        *,
        top_k: int = 4,
        min_score: float = 0.55,
        min_z: float = 0.0,
        max_per_page: int = 2,
        expand_top_page: bool = True,
    ) -> list[Retrieved]:
        """Cosine similarity with a floor.

        There was a distribution-relative gate here, on the theory that an
        unrelated question is uniformly mediocre while a real one spikes. On
        real data it was worse than useless: it is anti-correlated with
        relevance. A query matching many chunks, "when is the summer camp"
        against four camp pages, raises the mean and flattens its own z. A
        query relevant to nothing sits against a low mean with tiny variance,
        so a weak spike looks like an outlier. Measured on 26 questions, "what
        is your refund policy for hotel bookings" scored z 2.8 while "when is
        the summer camp" scored 1.6.

        Raw score separated them cleanly at 0.57: every real question above,
        every control below. So the floor is the floor, and `min_z` is off by
        default and kept only because the number is worth printing in eval.

        The floor is a coarse filter, not the judge. It is fitted to one site
        and will not transfer. What actually decides whether a caller gets an
        answer is the model reading the passages and saying they do not cover
        the question, which is what `render` below tells it to do.
        """
        if self.size == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        norm = np.linalg.norm(q)
        if norm == 0:
            return []
        scores = self.matrix @ (q / norm)

        self.last_stats = {
            "top": float(scores.max()),
            "mean": float(scores.mean()),
            "std": float(scores.std()),
            "z": self.z(scores),
        }
        # Off by default. See the note above: this rejects good questions
        # more often than bad ones.
        if min_z > 0 and self.size >= 30 and self.last_stats["z"] < min_z:
            return []

        order = np.argsort(-scores)[: max(top_k * 6, top_k)]
        hits: list[Retrieved] = []
        per_page: dict[str, int] = {}
        for i in order:
            score = float(scores[i])
            if score < min_score:
                break
            row = self.rows[int(i)]
            # At most two chunks per page. One was too few: a vague question
            # like "tell me more about X" scores highest on a page's marketing
            # blurb, and returning only that hides the detail chunk on the very
            # page the query just identified as relevant. The same question
            # phrased as "what is the curriculum for X" answered correctly,
            # which is not a difference a caller should be able to feel.
            if per_page.get(row["url"], 0) >= max_per_page:
                continue
            per_page[row["url"]] = per_page.get(row["url"], 0) + 1
            hits.append(
                Retrieved(
                    url=row["url"],
                    title=row["title"],
                    heading=row["heading"],
                    text=row["text"],
                    score=score,
                )
            )
            if len(hits) >= top_k:
                break

        # Pull in the rest of the winning page, even below the floor. Once a
        # page has scored well the question is what it says, not whether each
        # paragraph independently clears a threshold. This is what turns
        # "focuses on critical thinking" into the actual curriculum.
        # Only expand a page that clearly won. A top hit sitting just above
        # the floor is a weak match, and expanding it dumps an entire
        # irrelevant page into the model's context: "what happens if we miss a
        # class" returned six chunks of the summer camp page, none of which
        # answered it. Enrich a confident match, never inflate a marginal one.
        confident = hits and hits[0].score >= min_score + 0.04
        if expand_top_page and confident:
            best_url = hits[0].url
            added = 0
            have = {(h.url, h.text) for h in hits}
            for i in np.argsort(-scores):
                row = self.rows[int(i)]
                if row["url"] != best_url or (row["url"], row["text"]) in have:
                    continue
                if float(scores[i]) < min_score * 0.8:
                    break
                added += 1
                hits.append(
                    Retrieved(
                        url=row["url"],
                        title=row["title"],
                        heading=row["heading"],
                        text=row["text"],
                        score=float(scores[i]),
                    )
                )
                # Two extra at most. More is the same page said again.
                if added >= 2 or len(hits) >= top_k + 2:
                    break
        return hits


def render(hits: list[Retrieved]) -> dict:
    """Shape handed back to the model as a tool result.

    Passages stay separate and each keeps its source so the agent can attribute
    an answer. `found: false` is explicit because a model reads an empty list as
    an error and starts improvising.
    """
    if not hits:
        return {
            "found": False,
            "passages": [],
            "hint": "Nothing on the site covers this. Say so plainly and offer to take a message.",
        }
    return {
        "found": True,
        "passages": [
            {
                "source": h.url,
                "section": h.heading or h.title,
                "text": h.text,
            }
            for h in hits
        ],
        # The retriever matches on similarity, not on whether the passage
        # answers anything. A question about something the business does not
        # do still returns its closest page. Deciding is the model's job and
        # it has to be told that, or it treats any passage as an answer.
        "hint": (
            "These passages are the closest text on the site, which is not the "
            "same as an answer. Read them. If they do not actually answer the "
            "question, say it is not on the site and offer to take a message. "
            "Do not infer, estimate, or generalise from a related passage."
        ),
    }
