"""Transparent trust scoring v1 for ERC-8004 agents.

Design goals (in order):
  1. Honest — refuses to score when data is too thin (returns null + status).
  2. Transparent — every output carries a `breakdown` showing each component.
  3. Boring math — no ML, no opaque weights, every coefficient is named here.

Score range: 0..100.

Components (each 0..100, then weighted):
  - value_avg       weighted mean of feedback values, normalised onto 0..100
  - client_breadth  log-scaled count of distinct clients (sybil-resistance via diversity)
  - volume          log-scaled count of feedback entries
  - recency         time-decay applied to feedback values (older feedback < newer)

Refusal threshold: <3 distinct clients => score=null, status="insufficient_data".
This prevents one client farming a high score by self-rating.
"""
from __future__ import annotations

import json
import math
import pathlib
import time
from collections import defaultdict
from typing import Iterable

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
AGENTS_PATH = DATA / "agents.jsonl"
FEEDBACK_PATH = DATA / "feedback.jsonl"
REVOKED_PATH = DATA / "revoked.jsonl"

WEIGHTS = {
    "value_avg":      0.50,
    "client_breadth": 0.20,
    "volume":         0.15,
    "recency":        0.15,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

MIN_DISTINCT_CLIENTS = 3
RECENCY_HALFLIFE_BLOCKS = 50_000  # ~28h on Base; gentle decay
VOLUME_REF = 50.0                  # 50+ feedbacks already saturates the volume axis
CLIENT_BREADTH_REF = 25.0          # 25+ distinct clients saturates the breadth axis
METHODOLOGY_URL = "https://trust.nsgoods.org/methodology"


def _load_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _dedup(rows: Iterable[dict], key_fn) -> list[dict]:
    seen = set()
    out: list[dict] = []
    for row in rows:
        k = key_fn(row)
        if k in seen:
            continue
        seen.add(k)
        out.append(row)
    return out


def load_active_feedback(agent_id: int) -> tuple[list[dict], int, list[dict]]:
    """Return (active_feedback, latest_block_seen, revoked_keys)."""
    fb = _load_jsonl(FEEDBACK_PATH)
    rv = _load_jsonl(REVOKED_PATH)

    fb = _dedup(fb, key_fn=lambda r: (r.get("tx"), r.get("log_index")))
    rv = _dedup(rv, key_fn=lambda r: (r.get("tx"),))

    revoked_keys = {(r["agent_id"], r["client"].lower(), r["feedback_index"]) for r in rv}
    active = [
        r for r in fb
        if r["agent_id"] == agent_id
        and (r["agent_id"], r["client"].lower(), r["feedback_index"]) not in revoked_keys
    ]
    latest_block = max((r.get("block", 0) for r in fb), default=0)
    return active, latest_block, [r for r in rv if r["agent_id"] == agent_id]


def _normalise_value_to_100(value: float) -> float:
    """Project a feedback value onto 0..100. ERC-8004 values are signed decimals
    with an arbitrary scale (e.g. 99.77 or 5.0 or -3.0). We clamp to [-100, 100]
    then linearly remap to [0, 100]. This is the boring, defensible choice."""
    v = max(-100.0, min(100.0, float(value)))
    return (v + 100.0) / 2.0


def _log_axis(count: int, ref: float) -> float:
    if count <= 0:
        return 0.0
    return min(100.0, 100.0 * math.log1p(count) / math.log1p(ref))


def _recency_weight(block: int, latest_block: int) -> float:
    if latest_block <= 0:
        return 1.0
    age = max(0, latest_block - block)
    return 0.5 ** (age / RECENCY_HALFLIFE_BLOCKS)


def compute_score(agent_id: int, *, latest_block_hint: int = 0) -> dict:
    active, latest_block, revoked = load_active_feedback(agent_id)
    if latest_block_hint > latest_block:
        latest_block = latest_block_hint

    distinct_clients = sorted({r["client"].lower() for r in active})
    n_feedback = len(active)
    n_revoked = len(revoked)

    base = {
        "agent_id": agent_id,
        "score": None,
        "status": "insufficient_data",
        "components": {},
        "inputs": {
            "feedback_count": n_feedback,
            "distinct_clients": len(distinct_clients),
            "revoked_count": n_revoked,
            "min_distinct_clients_required": MIN_DISTINCT_CLIENTS,
            "latest_block_seen": latest_block,
        },
        "methodology": METHODOLOGY_URL,
        "weights": WEIGHTS,
    }

    if len(distinct_clients) < MIN_DISTINCT_CLIENTS:
        base["status"] = (
            "insufficient_data — fewer than "
            f"{MIN_DISTINCT_CLIENTS} distinct clients have left feedback"
        )
        return base

    weighted_sum = 0.0
    weight_sum = 0.0
    for r in active:
        w = _recency_weight(r.get("block", latest_block), latest_block)
        v = _normalise_value_to_100(r.get("value", 0.0))
        weighted_sum += w * v
        weight_sum += w
    recency_value_avg = weighted_sum / weight_sum if weight_sum > 0 else 0.0

    simple_value_avg = sum(_normalise_value_to_100(r.get("value", 0.0)) for r in active) / n_feedback

    breadth = _log_axis(len(distinct_clients), CLIENT_BREADTH_REF)
    volume  = _log_axis(n_feedback,            VOLUME_REF)

    components = {
        "value_avg":      round(simple_value_avg, 2),
        "client_breadth": round(breadth,         2),
        "volume":         round(volume,          2),
        "recency":        round(recency_value_avg, 2),
    }
    score = sum(components[k] * WEIGHTS[k] for k in WEIGHTS)
    base["components"] = components
    base["score"] = round(score, 2)
    base["status"] = "ok"
    return base


def list_agents() -> list[int]:
    rows = _dedup(_load_jsonl(AGENTS_PATH), key_fn=lambda r: r["agent_id"])
    return sorted({r["agent_id"] for r in rows})


def feedback_universe() -> dict:
    """For /preview and /health: a quick scan of who has any feedback at all."""
    fb = _dedup(_load_jsonl(FEEDBACK_PATH), key_fn=lambda r: (r.get("tx"), r.get("log_index")))
    by_agent: dict[int, int] = defaultdict(int)
    for r in fb:
        by_agent[r["agent_id"]] += 1
    latest_block = max((r.get("block", 0) for r in fb), default=0)
    return {
        "agents_with_feedback": sorted(by_agent.keys()),
        "feedback_count_per_agent": dict(sorted(by_agent.items())),
        "total_feedback": sum(by_agent.values()),
        "latest_block_seen": latest_block,
    }
