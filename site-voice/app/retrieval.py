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

        order = np.argsort(-scores)[: max(top_k * 3, top_k)]
        hits: list[Retrieved] = []
        seen_urls: set[str] = set()
        for i in order:
            score = float(scores[i])
            if score < min_score:
                break
            row = self.rows[int(i)]
            # One chunk per page. Three chunks off the same page sound like one
            # source to a caller and waste the context budget.
            if row["url"] in seen_urls:
                continue
            seen_urls.add(row["url"])
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
