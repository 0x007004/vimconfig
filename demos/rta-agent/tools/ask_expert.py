"""Ask-an-expert tool.

In the demo, "asking an expert" is simulated by looking up canned answers in
a registry below. In a real deployment this tool would post the structured
question to Slack/Feishu, wait for a reply, and return it. The interface
is identical — only the transport changes.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml  # type: ignore[import-not-found]

ROOT = Path(__file__).resolve().parent.parent
EXPERTS_PATH = ROOT / "experts.yaml"
ASKS_DIR = ROOT / "asks"


@dataclass
class Ask:
    asked_at: float
    topic: str
    question: str
    routed_to: str
    answer: str
    answered_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


CANNED_ANSWERS: Dict[str, Dict[str, str]] = {
    "wangwu": {
        "bearads-latency-budget": (
            "BearAds 合同写的 80ms 是 hard deadline，按我们历史经验扣掉网络 RTT "
            "(15-25ms) 和媒体侧解析 (~10ms) 还要留 5ms 安全边距。建议内部预算 "
            "设 40ms。p99 超过这个值就告警。"
        ),
        "bearads-frequency-cap": (
            "BearAds 协议里写的 1 bid / oaid / 30s 是他们自己强制的，超了直接静"
            "默丢弃。我们这边不需要再加一层，但要把 oaid 频次记到 metrics 里方"
            "便对账。"
        ),
    },
    "lisi": {
        "bearads-deviceid": (
            "BearAds 的 oaid_sha256 是他们已经帮我们 sha256 过了，注意是 raw oaid "
            "做 sha256，**不是** 先 md5 再 sha256。我们 adapter 里收到这个字段"
            "直接用，不要再 hash。没有 oaid 就 no-bid，他们没有 androidid fallback。"
        ),
    },
    "zhangsan": {
        "bearads-signature": (
            "BearAds 用 HMAC-SHA512（不是 256），输入是 nonce + \".\" + body 的 "
            "sha256 hex，不是 body 本身。这是他们文档里没写清楚但我跟他们 SA "
            "确认过的：\n"
            "  sign = hmac_sha512(secret, nonce + \".\" + sha256_hex(body))\n"
            "时钟容忍是 nonce 校验，不是 timestamp，所以不需要 NTP 那么严格。"
        ),
        "bearads-bearer-token": (
            "X-Bear-Auth 的 bearer token 是接入时一次性发的长期 token，跟签名"
            "secret 是两个东西，不要混。token 只校验是不是合法接入方，签名才"
            "校验请求体没被篡改。"
        ),
    },
    "zhaoliu": {
        "bearads-reconciliation": (
            "BearAds 每 5 分钟在 https://rta.bearads.example/recon/<api_key> 推一"
            "次对账文件（gzipped jsonl），字段 our_bids / their_bids / win_count。"
            "差异超 5% 就要找他们 BD 撕，按 P-005。"
        ),
    },
}


def load_experts() -> List[Dict]:
    return yaml.safe_load(EXPERTS_PATH.read_text(encoding="utf-8")).get("experts", [])


def _normalize(s: str) -> str:
    return s.lower().replace("-", "_").replace(" ", "_")


def route(topic: str) -> str:
    """Pick the best expert id for this topic.

    Tokenizes both topic and subtopic so 'frequency-cap' matches
    'frequency_capping' (and similar dash/underscore variations).
    """
    experts = load_experts()
    topic_n = _normalize(topic)
    topic_tokens = set(topic_n.split("_"))
    best_id, best_score = "zhangsan", -1
    for e in experts:
        score = 0
        for area in e.get("expertise", []):
            for sub in area.get("subtopics", []):
                sub_n = _normalize(str(sub))
                if sub_n in topic_n or topic_n in sub_n:
                    score += 5 if area.get("level") == "expert" else 3
                else:
                    sub_tokens = set(sub_n.split("_"))
                    if topic_tokens & sub_tokens:
                        score += 3 if area.get("level") == "expert" else 2
            domain_n = _normalize(area.get("domain", ""))
            if domain_n and (domain_n in topic_n or topic_n in domain_n):
                score += 4
            domain_tokens = set(domain_n.split("_")) - {"rta"}
            if domain_tokens & topic_tokens:
                score += 2
        if score > best_score:
            best_id, best_score = e["id"], score
    return best_id


def ask(topic: str, question: str, expert_id: Optional[str] = None) -> Ask:
    expert_id = expert_id or route(topic)
    asked_at = time.time()
    canned = CANNED_ANSWERS.get(expert_id, {}).get(topic)
    if canned:
        answer = canned
    else:
        answer = (
            f"[mock-fallback] {expert_id} 暂时不在；建议 (a) 查现有 skills，"
            f"(b) 看 protocols/<media>.md 原文，(c) 如确实需要专家，留到下班后异步问。"
        )
    a = Ask(
        asked_at=asked_at,
        topic=topic,
        question=question,
        routed_to=expert_id,
        answer=answer,
        answered_at=time.time(),
    )
    ASKS_DIR.mkdir(exist_ok=True)
    fname = ASKS_DIR / f"{int(asked_at)}-{expert_id}-{topic}.json"
    fname.write_text(a.to_json(), encoding="utf-8")
    return a


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--expert", help="Override the auto-routed expert.")
    args = parser.parse_args()
    a = ask(args.topic, args.question, args.expert)
    print(f"[asked {a.routed_to}] {args.topic}")
    print("---")
    print(a.answer)


if __name__ == "__main__":
    main()
