"""Cross-chain tamper-evident hash chain of trust-score snapshots.

After each indexer pass we append a single entry to ``data/scores_chain.jsonl``
containing the current score for every (chain, agent_id) in scope. The entry
hash folds in the previous entry's hash; any retroactive edit breaks every
subsequent hash and the next ``verify()`` fails.

Stdlib only — no third-party deps. Standalone CLI:

    python3 provable.py verify     # -> "CHAIN OK (N entries) head=..."
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from datetime import datetime, timezone
from typing import Iterable

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
CHAIN_PATH = DATA / "scores_chain.jsonl"

GENESIS_PREV = "GENESIS"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(obj_without_h: dict) -> str:
    return hashlib.sha256(_canonical(obj_without_h).encode("utf-8")).hexdigest()


def _iter_entries() -> Iterable[dict]:
    if not CHAIN_PATH.exists(): return
    with CHAIN_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            yield json.loads(line)


def _read_tail() -> tuple[int, str, list[dict] | None, int | None]:
    """Return (last_seq, last_h, last_entries_list, last_total_latest_blocks_sum_or_None)."""
    last_seq = -1
    last_h = GENESIS_PREV
    last_entries = None
    last_per_chain_blocks_sum: int | None = None
    for e in _iter_entries():
        last_seq = e["seq"]
        last_h = e["h"]
        last_entries = e.get("chains")
        # Sum the latest_block across all chains in this entry — used as a quick
        # idempotency signal alongside the per-chain entries list comparison.
        if isinstance(last_entries, list):
            try:
                last_per_chain_blocks_sum = sum(int(c.get("latest_block", 0)) for c in last_entries)
            except Exception:
                last_per_chain_blocks_sum = None
    return last_seq, last_h, last_entries, last_per_chain_blocks_sum


def _scores_snapshot() -> list[dict]:
    """Return a deterministic-ordered list of per-chain blocks.
    Each block: {chain, latest_block, agents: [{agent_id, score, status, feedback_count, distinct_clients}]}
    Empty chains are included with agents=[] so the snapshot covers every wired
    chain — useful evidence later that 'we looked, there was nothing'."""
    from scoring import compute_score, list_agents, feedback_universe, known_chains

    out: list[dict] = []
    for chain in sorted(known_chains()):
        universe = feedback_universe(chain)
        agent_ids = sorted(set(list_agents(chain)) | set(universe["agents_with_feedback"]))
        latest_block = universe.get("latest_block_seen", 0)
        agents_block: list[dict] = []
        for aid in agent_ids:
            s = compute_score(chain, aid, latest_block_hint=latest_block)
            inputs = s.get("inputs", {})
            agents_block.append({
                "agent_id":         aid,
                "score":            s.get("score"),
                "status":           s.get("status"),
                "feedback_count":   inputs.get("feedback_count", 0),
                "distinct_clients": inputs.get("distinct_clients", 0),
            })
        out.append({
            "chain":        chain,
            "latest_block": latest_block,
            "agents":       agents_block,
        })
    return out


def append_snapshot() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    last_seq, last_h, last_chains, last_block_sum = _read_tail()
    chains_block = _scores_snapshot()
    block_sum = sum(int(c["latest_block"]) for c in chains_block)

    if last_chains is not None and _canonical(last_chains) == _canonical(chains_block):
        return {
            "appended":     False,
            "reason":       "no_change",
            "seq":          last_seq,
            "head_hash":    last_h,
            "block_sum":    block_sum,
            "n_chains":     len(chains_block),
        }

    seq = last_seq + 1
    entry = {
        "seq":      seq,
        "ts_utc":   datetime.now(timezone.utc).isoformat(),
        "chains":   chains_block,
        "prev":     last_h,
    }
    entry["h"] = _hash(entry)
    with CHAIN_PATH.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(entry) + "\n")

    return {
        "appended":  True,
        "seq":       seq,
        "head_hash": entry["h"],
        "n_chains":  len(chains_block),
        "n_agents":  sum(len(c["agents"]) for c in chains_block),
        "block_sum": block_sum,
    }


def verify() -> dict:
    prev = GENESIS_PREV
    count = 0
    head = GENESIS_PREV
    last_seq = -1
    for entry in _iter_entries():
        count += 1
        if entry.get("seq") != last_seq + 1:
            return {"ok": False, "error": "seq_gap", "at_seq": entry.get("seq"),
                    "expected": last_seq + 1, "count": count, "head_hash": head}
        last_seq = entry["seq"]
        if entry.get("prev") != prev:
            return {"ok": False, "error": "prev_mismatch", "at_seq": entry["seq"],
                    "expected": prev, "found": entry.get("prev"),
                    "count": count, "head_hash": head}
        stored_h = entry.get("h")
        recomputed = _hash({k: v for k, v in entry.items() if k != "h"})
        if stored_h != recomputed:
            return {"ok": False, "error": "hash_mismatch", "at_seq": entry["seq"],
                    "expected": recomputed, "found": stored_h,
                    "count": count, "head_hash": head}
        prev = stored_h
        head = stored_h
    return {"ok": True, "count": count, "head_hash": head}


def head() -> dict | None:
    last: dict | None = None
    for e in _iter_entries():
        last = e
    return last


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "verify"
    if cmd == "verify":
        r = verify()
        if r["ok"]:
            print(f"CHAIN OK ({r['count']} entries) head={r['head_hash']}")
            return 0
        print(f"CHAIN BROKEN at seq {r.get('at_seq')}: {r.get('error')} (had {r['count']} entries)")
        return 1
    if cmd == "append":
        print(json.dumps(append_snapshot(), indent=2))
        return 0
    if cmd == "head":
        h = head()
        print(json.dumps(h, indent=2) if h else "(empty)")
        return 0
    print(f"usage: {argv[0]} verify|append|head")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
