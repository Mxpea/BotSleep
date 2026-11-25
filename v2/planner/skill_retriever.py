"""L4 Zero-dependency local BM25 + Blackboard tag retriever over skills_kb/*.md."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Dict, List, Optional, Sequence, Tuple


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass(frozen=True)
class SkillCard:
    id: str
    path: Path
    tags: Tuple[str, ...]
    body: str
    tokens: Tuple[str, ...]


class BM25SkillRetriever:
    """Zero-dependency BM25 + tag-boosted retriever injecting Top-K micro-cards under a byte cap."""

    def __init__(self, kb_dir: Optional[Path] = None) -> None:
        self.kb_dir = kb_dir or (_REPO_ROOT / "skills_kb")
        self.cards: List[SkillCard] = []
        self._df: Dict[str, int] = {}
        self._avgdl: float = 1.0
        self._load_cards()

    def _load_cards(self) -> None:
        if not self.kb_dir.is_dir():
            return
        loaded: List[SkillCard] = []
        for md in sorted(self.kb_dir.glob("*.md")):
            if md.name.lower() == "readme.md":
                continue
            raw = md.read_text(encoding="utf-8")
            cid = md.stem
            tags: List[str] = []
            body = raw
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) == 3:
                    header, body = parts[1], parts[2].strip()
                    for line in header.splitlines():
                        if line.strip().startswith("id:"):
                            cid = line.split(":", 1)[1].strip()
                        elif line.strip().startswith("tags:"):
                            raw_tags = line.split(":", 1)[1].strip().strip("[]")
                            tags = [t.strip().lower() for t in raw_tags.split(",") if t.strip()]
            tokens = tuple(_tokenize(cid + " " + " ".join(tags) + " " + body))
            loaded.append(SkillCard(cid, md, tuple(tags), body.strip(), tokens))

        self.cards = loaded
        if not self.cards:
            return
        self._avgdl = sum(len(c.tokens) for c in self.cards) / max(1, len(self.cards))
        df: Dict[str, int] = {}
        for c in self.cards:
            for tok in set(c.tokens):
                df[tok] = df.get(tok, 0) + 1
        self._df = df

    def retrieve(
        self,
        query: str,
        *,
        tags: Sequence[str] = (),
        top_k: int = 3,
        max_bytes: int = 1500,
    ) -> List[SkillCard]:
        """Rank cards via BM25 + tag match boost and return up to top_k cards within max_bytes."""
        if not self.cards:
            return []
        q_tokens = _tokenize(query)
        tag_set = {t.lower() for t in tags}
        n_docs = len(self.cards)
        k1, b = 1.5, 0.75

        scored: List[Tuple[float, SkillCard]] = []
        for card in self.cards:
            tf: Dict[str, int] = {}
            for tok in card.tokens:
                tf[tok] = tf.get(tok, 0) + 1
            dl = len(card.tokens)
            score = 0.0
            for qt in q_tokens:
                f = tf.get(qt, 0)
                if f == 0:
                    continue
                df = self._df.get(qt, 0)
                idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
                score += idf * (f * (k1 + 1.0)) / (f + k1 * (1.0 - b + b * dl / self._avgdl))
            # Explicit tag boost
            tag_hits = len(tag_set.intersection(card.tags))
            score += tag_hits * 3.0
            scored.append((score, card))

        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        selected: List[SkillCard] = []
        used_bytes = 0
        for _sc, card in scored[: max(top_k * 2, top_k)]:
            if len(selected) >= top_k:
                break
            card_bytes = len(card.body.encode("utf-8"))
            if selected and used_bytes + card_bytes > max_bytes:
                continue
            selected.append(card)
            used_bytes += card_bytes
        return selected

    def format_context(self, query: str, *, tags: Sequence[str] = (), top_k: int = 3, max_bytes: int = 1500) -> str:
        cards = self.retrieve(query, tags=tags, top_k=top_k, max_bytes=max_bytes)
        if not cards:
            return ""
        chunks: List[str] = []
        budget = max_bytes
        for card in cards:
            snippet = f"[{card.id}]\n{card.body}"
            enc = snippet.encode("utf-8")
            if len(enc) > budget:
                snippet = enc[: max(0, budget - 3)].decode("utf-8", errors="ignore") + "..."
                chunks.append(snippet)
                break
            chunks.append(snippet)
            budget -= len(enc) + 2
            if budget <= 64:
                break
        return "\n\n".join(chunks)
