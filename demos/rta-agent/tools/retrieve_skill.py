"""Skill retrieval — reads skills/*.md, matches by trigger keywords.

This is the simplest possible retrieval layer (substring + keyword overlap).
In a real Hermes deployment this would be FTS5 with embedding fallback;
the interface is the same.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"


@dataclass
class Skill:
    id: str
    path: Path
    trigger_keywords: List[str]
    body: str

    @classmethod
    def load(cls, path: Path) -> Optional["Skill"]:
        text = path.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n(.*)", text, flags=re.DOTALL)
        if not m:
            return None
        front, body = m.group(1), m.group(2)
        meta: dict = {}
        for line in front.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        keywords_raw = meta.get("keywords", "")
        keywords = [k.strip().lower() for k in keywords_raw.strip("[]").split(",") if k.strip()]
        return cls(
            id=meta.get("id", path.stem),
            path=path,
            trigger_keywords=keywords,
            body=body,
        )


def load_all_skills() -> List[Skill]:
    if not SKILLS_DIR.exists():
        return []
    skills: List[Skill] = []
    for p in sorted(SKILLS_DIR.glob("*.md")):
        if p.name == "README.md":
            continue
        s = Skill.load(p)
        if s:
            skills.append(s)
    return skills


def search(query: str, top_k: int = 5) -> List[Skill]:
    q_terms = {t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query)}
    scored: List[tuple] = []
    for s in load_all_skills():
        kw_overlap = len(q_terms & set(s.trigger_keywords))
        body_hits = sum(1 for t in q_terms if t in s.body.lower())
        score = kw_overlap * 5 + body_hits
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:top_k]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    q = " ".join(args.query)
    hits = search(q)
    if args.json:
        print(json.dumps(
            [{"id": h.id, "path": str(h.path), "keywords": h.trigger_keywords} for h in hits],
            indent=2,
        ))
        return
    if not hits:
        print(f"(no skill matches for: {q})")
        return
    for h in hits:
        print(f"  - {h.id}  ({', '.join(h.trigger_keywords)})")
        print(f"    -> {h.path}")


if __name__ == "__main__":
    main()
