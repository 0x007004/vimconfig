"""Sink a Q&A or pitfall into the skill library."""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import List

from .paths import SKILLS as SKILLS_DIR


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9_.\-]+", "-", text.lower()).strip("-")


def write_skill(
    skill_id: str,
    title: str,
    keywords: List[str],
    body: str,
    source: str = "",
    created_by: str = "agent",
) -> Path:
    SKILLS_DIR.mkdir(exist_ok=True)
    fname = SKILLS_DIR / f"{_slugify(skill_id)}.md"
    today = dt.date.today().isoformat()
    front = "\n".join([
        "---",
        f"id: {skill_id}",
        f"keywords: [{', '.join(keywords)}]",
        f"created_by: {created_by}",
        f"created_at: {today}",
        f"source: {source}",
        "verified: false",
        "hit_count: 0",
        "---",
        "",
    ])
    md = front + f"# {title}\n\n{body}\n"
    fname.write_text(md, encoding="utf-8")
    return fname


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, dest="skill_id")
    parser.add_argument("--title", required=True)
    parser.add_argument("--keywords", required=True, help="comma-separated")
    parser.add_argument("--body", required=True)
    parser.add_argument("--source", default="")
    args = parser.parse_args()
    p = write_skill(
        skill_id=args.skill_id,
        title=args.title,
        keywords=[k.strip() for k in args.keywords.split(",") if k.strip()],
        body=args.body,
        source=args.source,
    )
    print(f"wrote skill: {p}")


if __name__ == "__main__":
    main()
