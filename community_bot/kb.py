"""The knowledge base: Markdown files split into sections at their headings, searched
with BM25 (plain keyword ranking, no embeddings, no extra service). Each section keeps
its file and heading so an answer can name exactly where it came from."""
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List

STOP = set("""a an and are as at be but by can do does for from has have how i if in is it its me my of on or our
so that the their them there this to was we what when where which who why will with you your yours""".split())


@dataclass(frozen=True)
class Section:
    source: str   # file name, e.g. membership.md
    heading: str  # e.g. Membership > What Premium includes
    text: str


@dataclass
class Hit:
    section: Section
    score: float


def tokens(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for w in words:
        if w in STOP or len(w) < 2:
            continue
        for suffix in ("ing", "es", "s"):  # a light stem so "refunds" finds "refund"
            if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out


def split_markdown(source: str, text: str) -> List[Section]:
    sections, title, current, buf = [], "", "", []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            heading = f"{title} > {current}" if title and current else (current or title or source)
            sections.append(Section(source, heading, body))

    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            flush()
            buf = []
            if len(m.group(1)) == 1:
                title, current = m.group(2).strip(), ""
            else:
                current = m.group(2).strip()
        else:
            buf.append(line)
    flush()
    return sections


class KnowledgeBase:
    def __init__(self, sections: List[Section]):
        self.sections = sections
        self.docs = [tokens(s.heading + " " + s.text) for s in sections]
        self.df = Counter(t for d in self.docs for t in set(d))
        self.avg = sum(len(d) for d in self.docs) / max(len(self.docs), 1)

    @classmethod
    def load(cls, folder) -> "KnowledgeBase":
        sections = []
        for path in sorted(Path(folder).glob("*.md")):
            sections.extend(split_markdown(path.name, path.read_text(encoding="utf-8")))
        return cls(sections)

    def search(self, query: str, k: int = 5, k1: float = 1.4, b: float = 0.75) -> List[Hit]:
        q = set(tokens(query))
        n = len(self.docs)
        hits = []
        for section, doc in zip(self.sections, self.docs):
            tf = Counter(doc)
            score = 0.0
            for t in q:
                if t not in tf:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(doc) / self.avg))
            if score > 0:
                hits.append(Hit(section, score))
        hits.sort(key=lambda h: -h.score)
        return hits[:k]
