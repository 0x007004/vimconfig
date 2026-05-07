"""RTA integration agent — minimum harness loop.

This is the orchestrator. It implements the 5-step Harness Engineering
minimum closed loop end-to-end:

    1. Pick task    → integrate(media)
    2. Map context  → read protocol + retrieve skills + list references
    3. Write rules  → applied skills + pitfalls become guardrails for codegen
    4. Self-verify  → tools.verify runs the four-sensor suite
    5. Write back   → unresolved questions → ask_expert → write_skill

Run:
    python agent.py integrate bearads
    python agent.py integrate bearads --llm   # delegate codegen to Claude

The agent is intentionally stateless between runs — all state lives on disk
in skills/, asks/, adapters/. That's what makes the complibition durable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

if sys.version_info < (3, 9):
    sys.stderr.write(
        f"this demo requires Python 3.9+ (you have {sys.version.split()[0]}); "
        "uses str.removeprefix and PEP 585 builtin generics.\n"
    )
    sys.exit(2)

AGENT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_ROOT))

from tools import ask_expert, generate_adapter, retrieve_skill, write_skill  # noqa: E402
from tools.paths import PROTOCOLS, WORKSPACE_ROOT, is_external_workspace  # noqa: E402
from tools.verify import VerifyReport, verify_media  # noqa: E402


@dataclass
class RunLog:
    media: str
    steps: List[str] = field(default_factory=list)

    def step(self, msg: str) -> None:
        self.steps.append(msg)
        print(msg)

    def section(self, title: str) -> None:
        bar = "─" * (len(title) + 2)
        print(f"\n┌{bar}┐\n│ {title} │\n└{bar}┘")


def _wrap(s: str, width: int = 88, indent: str = "    ") -> str:
    return "\n".join(
        textwrap.fill(line, width=width, initial_indent=indent,
                      subsequent_indent=indent)
        for line in s.splitlines()
    )


def _verify_subprocess(media: str) -> VerifyReport:
    """Run verify in a fresh interpreter so the freshly-written adapter
    module is loaded cleanly (no Python import caching surprises).

    The subprocess inherits env (incl. RTA_WORKSPACE_ROOT) so it operates
    on the same workspace as the parent.
    """
    cmd = [sys.executable, "-m", "tools.verify", media, "--json"]
    proc = subprocess.run(cmd, cwd=str(AGENT_ROOT), capture_output=True, text=True)
    raw = (proc.stdout or proc.stderr).strip()
    payload = json.loads(raw)
    from tools.verify import CheckResult
    return VerifyReport(
        media=payload["media"],
        checks=[CheckResult(**c) for c in payload["checks"]],
    )


def integrate(media: str, use_llm: bool = False, max_asks: int = 3) -> int:
    log = RunLog(media=media)

    if is_external_workspace():
        log.step(f"workspace = {WORKSPACE_ROOT}  (external)")
    log.section(f"step 1 — read protocol for {media}")
    protocol_path = PROTOCOLS / f"{media}.md"
    if not protocol_path.exists():
        log.step(f"  ✗ no protocol spec at {protocol_path}; aborting")
        return 2
    spec_text = protocol_path.read_text(encoding="utf-8")
    log.step(f"  ✓ read protocols/{media}.md ({len(spec_text)} chars)")

    log.section("step 2 — retrieve relevant skills (Guides)")
    query_terms = " ".join({
        *_keywords_from_spec(spec_text),
        media,
    })
    matched = retrieve_skill.search(query_terms, top_k=5)
    if matched:
        for s in matched:
            log.step(f"  ↳ {s.id}  (keywords: {', '.join(s.trigger_keywords)})")
    else:
        log.step("  (no skill match — likely the first integration of this kind)")

    log.section("step 3 — identify open questions, ask experts")
    questions = _identify_open_questions(media, spec_text, matched)
    asked: List[ask_expert.Ask] = []
    for q in questions[:max_asks]:
        log.step(f"  ? topic={q['topic']}  → routing...")
        a = ask_expert.ask(topic=q["topic"], question=q["question"])
        log.step(f"    routed to {a.routed_to}")
        log.step(_wrap(a.answer, indent="      "))
        asked.append(a)

    log.section("step 4 — generate adapter (delegate codegen)")
    skill_ids = [s.id for s in matched]
    src = generate_adapter.generate(media, applied_skills=skill_ids, use_llm=use_llm)
    out_path = generate_adapter.write(media, src)
    log.step(f"  ✓ wrote {out_path}")
    log.step(f"  ✓ source: {len(src.splitlines())} lines, "
             f"{'LLM (Claude)' if use_llm else 'deterministic template'} mode")

    log.section("step 5 — self-verify (Sensors)")
    report = _verify_subprocess(media)
    for c in report.checks:
        log.step(f"  {c}")

    log.section("step 6 — write back to skill library")
    if not report.passed:
        log.step("  ✗ verify failed; not writing skill (would memoize a bad pattern)")
        return 1

    for a in asked:
        skill_id = f"rta.{media}.{a.topic.replace(media + '-', '').replace('-', '_')}"
        topic_tail = a.topic.replace(media + "-", "")
        keywords = sorted({media, *topic_tail.split("-")})
        write_skill.write_skill(
            skill_id=skill_id,
            title=f"{media}: {a.topic}",
            keywords=keywords,
            body=(
                f"## Q\n{a.question}\n\n"
                f"## A (from {a.routed_to})\n{a.answer}\n\n"
                f"## Source\nasked during agent integration of {media}, "
                f"verified passing on {report.media}-sensor-suite."
            ),
            source=f"asks/{int(a.asked_at)}-{a.routed_to}-{a.topic}.json",
            created_by="agent",
        )
        log.step(f"  ↳ sunk skill: {skill_id}")

    log.section("done")
    log.step(f"  media={media}: PR ready, {len(asked)} new skills sunk")
    return 0


def _keywords_from_spec(spec: str) -> List[str]:
    keywords: List[str] = []
    for line in spec.lower().splitlines():
        for kw in [
            "oaid", "idfa", "androidid", "imei", "sha256", "sha512", "md5",
            "hmac", "nonce", "tmax", "deadline", "no-bid", "nbr", "204",
            "price_cents", "frequency",
        ]:
            if kw in line:
                keywords.append(kw)
    return list(set(keywords))


def _identify_open_questions(media: str, spec: str, matched_skills: List) -> List[dict]:
    """Heuristic: which protocol-spec aspects are not yet covered by an
    existing verified skill? Those become questions for an expert.

    Critically: if a media-specific skill already exists for this topic
    (e.g. rta.bearads.signature), do NOT ask again — that's the whole
    point of skill sinking. The agent's question budget per integration
    should monotonically decrease as skills accumulate.
    """
    matched_kw = {kw for s in matched_skills for kw in s.trigger_keywords}
    media_specific_topics = {
        s.id.removeprefix(f"rta.{media}.") for s in matched_skills
        if s.id.startswith(f"rta.{media}.")
    }
    candidates: List[dict] = []

    if any(t in spec.lower() for t in ["sha512", "hmac", "nonce"]) and "sha512" not in matched_kw:
        candidates.append({
            "topic": f"{media}-signature",
            "covered_by_skill_topic": "signature",
            "question": (
                f"{media} 协议里签名说用 HMAC-SHA512 + nonce + body sha256，"
                f"和我们之前接的媒体都不一样。具体输入串怎么拼？是 nonce + "
                f'\".\" + sha256_hex(body) 还是 nonce + body？'
            ),
        })

    if "frequency" in spec.lower() or "1 bid per" in spec.lower():
        candidates.append({
            "topic": f"{media}-frequency-cap",
            "covered_by_skill_topic": "frequency_cap",
            "question": (
                f"{media} 在协议里说有 frequency cap (1 bid / oaid / 30s)，"
                f"我们这边需要在 adapter 层做计数器吗？还是依赖媒体自己强制？"
            ),
        })

    if "oaid_sha256" in spec or "sha256" in spec.lower():
        if "deviceid_priority" not in {s.id.split(".")[-1] for s in matched_skills}:
            candidates.append({
                "topic": f"{media}-deviceid",
                "covered_by_skill_topic": "deviceid",
                "question": (
                    f"{media} 用的是 oaid_sha256 (媒体已经预 hash 过)，我们 adapter "
                    f"是直接透传还是要再做处理？没有 oaid 时有 fallback 吗？"
                ),
            })

    if "tmax_ms" in spec or "deadline" in spec.lower() or "ms hard" in spec.lower():
        if "timeout_budget" not in {s.id.split(".")[-1] for s in matched_skills}:
            candidates.append({
                "topic": f"{media}-latency-budget",
                "covered_by_skill_topic": "latency_budget",
                "question": (
                    f"{media} 的 deadline 是 80ms，按 0.6x 我们内部预算应该是 48ms，"
                    f"但他们说在 CN-South PoP 网络更慢，要不要分区域设不同 budget？"
                ),
            })

    return [q for q in candidates if q["covered_by_skill_topic"] not in media_specific_topics]


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_int = sub.add_parser("integrate", help="integrate a new RTA media")
    p_int.add_argument("media")
    p_int.add_argument("--llm", action="store_true",
                       help="delegate code generation to Claude (requires ANTHROPIC_API_KEY)")
    p_int.add_argument("--max-asks", type=int, default=3)
    args = parser.parse_args()
    if args.cmd == "integrate":
        return integrate(args.media, use_llm=args.llm, max_asks=args.max_asks)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
