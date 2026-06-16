"""Tamper-evident hash chain of trust-score snapshots.

After each indexer pass we append a single entry to ``data/scores_chain.jsonl``
containing the current score for every agent in scope. The entry hash folds in
the previous entry's hash, so any retroactive edit breaks every subsequent hash
and the next ``verify()`` fails.

Stdlib only — no third-party deps — so it runs standalone:

    python3 provable.py verify     # -> "CHAIN OK (N entries) head=..."

The trust_oracle indexer.py invokes ``append_snapshot()`` at the end of each
run and (best-effort) pushes the updated chain to the public GitHub repo as a
third-party timestamp anchor.
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
    if not CHAIN_PATH.exists():
        return
    with CHAIN_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _read_tail() -> tuple[int, str, list[dict] | None]:
    """Return (last_seq, last_h, last_agents_or_None)."""
    last_seq = -1
    last_h = GENESIS_PREV
    last_agents = None
    for e in _iter_entries():
        last_seq = e["seq"]
        last_h = e["h"]
        last_agents = e.get("agents")
    return last_seq, last_h, last_agents


def _scores_snapshot() -> tuple[int, list[dict]]:
    """Compute current scores for the agent universe in a deterministic shape."""
    from scoring import compute_score, list_agents, feedback_universe  # local import = stdlib-only at module load

    universe = feedback_universe()
    agent_ids = sorted(set(list_agents()) | set(universe["agents_with_feedback"]))
    latest_block = universe.get("latest_block_seen", 0)

    out: list[dict] = []
    for aid in agent_ids:
        s = compute_score(aid, latest_block_hint=latest_block)
        inputs = s.get("inputs", {})
        out.append({
            "agent_id":         aid,
            "score":            s.get("score"),
            "status":           s.get("status"),
            "feedback_count":   inputs.get("feedback_count", 0),
            "distinct_clients": inputs.get("distinct_clients", 0),
        })
    return latest_block, out


def _agents_equal(a: list[dict] | None, b: list[dict]) -> bool:
    """Idempotency check: agents lists are equal as sets of (id,score,status,counts)."""
    if a is None:
        return False
    return _canonical(a) == _canonical(b)


def append_snapshot() -> dict:
    """Append a snapshot. Returns a small status dict.

    Idempotent: if (latest_block + per-agent state) is unchanged from the last
    entry we do not append.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    last_seq, last_h, last_agents = _read_tail()
    latest_block, agents = _scores_snapshot()

    # Idempotency: skip the append if nothing has changed.
    # We compare both block height AND agent state — block could advance with
    # no relevant feedback change (still a no-op), and an unchanged block could
    # still trigger an append if the agent list shifted (defensive).
    if last_agents is not None:
        prev_blk = None
        # Walk back one entry to read prev latest_block (cheap: tail-line)
        with CHAIN_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    prev_blk = json.loads(line).get("latest_block")
                except Exception:
                    pass
        if prev_blk == latest_block and _agents_equal(last_agents, agents):
            return {
                "appended":     False,
                "reason":       "no_change",
                "seq":          last_seq,
                "head_hash":    last_h,
                "latest_block": latest_block,
                "n_agents":     len(agents),
            }

    seq = last_seq + 1
    entry = {
        "seq":          seq,
        "ts_utc":       datetime.now(timezone.utc).isoformat(),
        "latest_block": latest_block,
        "agents":       agents,
        "prev":         last_h,
    }
    entry["h"] = _hash(entry)

    with CHAIN_PATH.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(entry) + "\n")

    return {
        "appended":     True,
        "seq":          seq,
        "head_hash":    entry["h"],
        "latest_block": latest_block,
        "n_agents":     len(agents),
    }


def verify() -> dict:
    """Re-hash every entry, check that prev chains correctly."""
    prev = GENESIS_PREV
    count = 0
    head = GENESIS_PREV
    last_seq = -1
    for entry in _iter_entries():
        count += 1
        if entry.get("seq") != last_seq + 1:
            return {
                "ok":         False,
                "error":      "seq_gap",
                "at_seq":     entry.get("seq"),
                "expected":   last_seq + 1,
                "count":      count,
                "head_hash":  head,
            }
        last_seq = entry["seq"]
        if entry.get("prev") != prev:
            return {
                "ok":         False,
                "error":      "prev_mismatch",
                "at_seq":     entry["seq"],
                "expected":   prev,
                "found":      entry.get("prev"),
                "count":      count,
                "head_hash":  head,
            }
        stored_h = entry.get("h")
        recomputed = _hash({k: v for k, v in entry.items() if k != "h"})
        if stored_h != recomputed:
            return {
                "ok":         False,
                "error":      "hash_mismatch",
                "at_seq":     entry["seq"],
                "expected":   recomputed,
                "found":      stored_h,
                "count":      count,
                "head_hash":  head,
            }
        prev = stored_h
        head = stored_h

    return {"ok": True, "count": count, "head_hash": head}


def head() -> dict | None:
    """Return the latest entry verbatim, or None if the chain is empty."""
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
        print(f"CHAIN BROKEN at seq {r.get('at_seq')}: {r.get('error')}  (had {r['count']} entries)")
        return 1
    if cmd == "append":
        r = append_snapshot()
        print(json.dumps(r, indent=2))
        return 0
    if cmd == "head":
        h = head()
        print(json.dumps(h, indent=2) if h else "(empty)")
        return 0
    print(f"usage: {argv[0]} verify|append|head")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
