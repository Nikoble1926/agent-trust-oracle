"""Per-(chain, agent_id) trust scoring v2.

Same 4-component math as v1 with the same refusal threshold — but the
universe is now keyed by (chain, agent_id) and feedback/agents are read
from per-chain JSONL under data/<chain>/.

We do NOT merge agent IDs across chains (probe on 2026-06-16 showed
zero wallets registered on ≥2 chains, so a cross-chain operator view
has no signal yet). The agentId namespace is per-chain.

Score range: 0..100.
Refusal: <3 distinct clients => score=null, status="insufficient_data".
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

WEIGHTS = {
    "value_avg":      0.50,
    "client_breadth": 0.20,
    "volume":         0.15,
    "recency":        0.15,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

MIN_DISTINCT_CLIENTS    = 3
RECENCY_HALFLIFE_BLOCKS = 50_000
VOLUME_REF              = 50.0
CLIENT_BREADTH_REF      = 25.0
METHODOLOGY_URL         = "https://trust.nsgoods.org/methodology"

# Default chain used when callers omit ?chain= — set to ethereum because
# that's where the activity actually is in mid-2026.
DEFAULT_CHAIN = "ethereum"

# Set of chains we expect to find under data/. Populated lazily.
KNOWN_CHAINS_FALLBACK = ("ethereum", "base", "polygon", "bsc", "mantle")


def _chain_dir(chain: str) -> pathlib.Path:
    return DATA / chain


def _load_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _dedup(rows: Iterable[dict], key_fn) -> list[dict]:
    seen = set(); out: list[dict] = []
    for row in rows:
        k = key_fn(row)
        if k in seen: continue
        seen.add(k); out.append(row)
    return out


def known_chains() -> list[str]:
    """Chains that currently have a data/<chain>/ directory."""
    if not DATA.is_dir():
        return list(KNOWN_CHAINS_FALLBACK)
    found = []
    for child in DATA.iterdir():
        if not child.is_dir(): continue
        if child.name.startswith("_"): continue   # _legacy_base_pre_redeploy etc.
        if (child / "agents.jsonl").exists() or (child / "feedback.jsonl").exists():
            found.append(child.name)
    return sorted(found) if found else list(KNOWN_CHAINS_FALLBACK)


def load_active_feedback(chain: str, agent_id: int) -> tuple[list[dict], int, list[dict]]:
    cd = _chain_dir(chain)
    fb = _load_jsonl(cd / "feedback.jsonl")
    rv = _load_jsonl(cd / "revoked.jsonl")

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
    v = max(-100.0, min(100.0, float(value)))
    return (v + 100.0) / 2.0


def _log_axis(count: int, ref: float) -> float:
    if count <= 0: return 0.0
    return min(100.0, 100.0 * math.log1p(count) / math.log1p(ref))


def _recency_weight(block: int, latest_block: int) -> float:
    if latest_block <= 0: return 1.0
    age = max(0, latest_block - block)
    return 0.5 ** (age / RECENCY_HALFLIFE_BLOCKS)


def compute_score(chain: str, agent_id: int, *, latest_block_hint: int = 0) -> dict:
    active, latest_block, revoked = load_active_feedback(chain, agent_id)
    if latest_block_hint > latest_block: latest_block = latest_block_hint

    distinct_clients = sorted({r["client"].lower() for r in active})
    n_feedback = len(active)
    n_revoked = len(revoked)

    base = {
        "chain":    chain,
        "agent_id": agent_id,
        "score":    None,
        "status":   "insufficient_data",
        "components": {},
        "inputs": {
            "feedback_count":              n_feedback,
            "distinct_clients":            len(distinct_clients),
            "revoked_count":               n_revoked,
            "min_distinct_clients_required": MIN_DISTINCT_CLIENTS,
            "latest_block_seen":           latest_block,
        },
        "methodology": METHODOLOGY_URL,
        "weights":     WEIGHTS,
    }

    if len(distinct_clients) < MIN_DISTINCT_CLIENTS:
        base["status"] = (
            "insufficient_data — fewer than "
            f"{MIN_DISTINCT_CLIENTS} distinct clients have left feedback"
        )
        return base

    weighted_sum = 0.0; weight_sum = 0.0
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
    base["score"]      = round(score, 2)
    base["status"]     = "ok"
    return base


def list_agents(chain: str) -> list[int]:
    rows = _dedup(_load_jsonl(_chain_dir(chain) / "agents.jsonl"),
                  key_fn=lambda r: r["agent_id"])
    return sorted({r["agent_id"] for r in rows})


def feedback_universe(chain: str) -> dict:
    fb = _dedup(_load_jsonl(_chain_dir(chain) / "feedback.jsonl"),
                key_fn=lambda r: (r.get("tx"), r.get("log_index")))
    by_agent: dict[int, int] = defaultdict(int)
    by_agent_clients: dict[int, set] = defaultdict(set)
    for r in fb:
        by_agent[r["agent_id"]] += 1
        by_agent_clients[r["agent_id"]].add(r["client"].lower())
    latest_block = max((r.get("block", 0) for r in fb), default=0)
    return {
        "chain":                       chain,
        "agents_with_feedback":        sorted(by_agent.keys()),
        "feedback_count_per_agent":    dict(sorted(by_agent.items())),
        "distinct_clients_per_agent":  {a: len(c) for a, c in by_agent_clients.items()},
        "total_feedback":              sum(by_agent.values()),
        "latest_block_seen":           latest_block,
    }


def pick_demo_agent(chain: str) -> int | None:
    """Return an agentId on `chain` that has ≥MIN_DISTINCT_CLIENTS distinct
    clients (so its score will return 'ok', never 'insufficient_data').
    Pick the one with the most feedback for a stable demo."""
    u = feedback_universe(chain)
    eligible = [(a, n) for a, n in u["feedback_count_per_agent"].items()
                if u["distinct_clients_per_agent"].get(a, 0) >= MIN_DISTINCT_CLIENTS]
    if not eligible:
        return None
    eligible.sort(key=lambda t: (-t[1], t[0]))
    return eligible[0][0]
