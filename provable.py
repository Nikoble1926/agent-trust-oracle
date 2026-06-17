"""Compact tamper-evident hash chain of trust-score snapshots.

Each entry stores only:
  - per-chain counters (registered, with_feedback)
  - per-chain `agents_sha256`: sha256 of the canonical-JSON of the full sorted
    [{agent_id, score, status, feedback_count, distinct_clients}, ...] list.
  - prev (previous entry's h)
  - h = sha256(canonical(entry without h))

The full per-agent table is reproducible from data/<chain>/feedback.jsonl +
data/<chain>/agents.jsonl. Anyone can recompute the table and check
agents_sha256 against the published entry — that's the tamper-evidence.

Stdlib only — runs standalone:

    python3 provable.py verify
    python3 provable.py recompute ethereum   # rebuild table + check digest

Concurrency-safe: a fcntl flock around read-tail+append serialises
concurrent indexer runs (the earlier race that produced a duplicate seq=3
can't happen again).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import pathlib
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
CHAIN_PATH = DATA / "scores_chain.jsonl"
LOCK_PATH = DATA / ".chain.lock"

GENESIS_PREV = "GENESIS"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(obj_without_h: dict) -> str:
    return hashlib.sha256(_canonical(obj_without_h).encode("utf-8")).hexdigest()


def _digest_agents(agents: list[dict]) -> str:
    """sha256 of the canonical JSON of the sorted per-agent list.

    The list ordering must be deterministic so the same on-chain state always
    produces the same digest. We sort by agent_id.
    """
    cleaned = [
        {
            "agent_id":         a.get("agent_id"),
            "score":            a.get("score"),
            "status":           a.get("status"),
            "feedback_count":   a.get("feedback_count", 0),
            "distinct_clients": a.get("distinct_clients", 0),
        }
        for a in sorted(agents, key=lambda x: x.get("agent_id", 0))
    ]
    return "sha256:" + hashlib.sha256(_canonical(cleaned).encode("utf-8")).hexdigest()


@contextmanager
def _chain_lock():
    DATA.mkdir(parents=True, exist_ok=True)
    fd = LOCK_PATH.open("w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()


def _iter_entries() -> Iterable[dict]:
    if not CHAIN_PATH.exists(): return
    with CHAIN_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            yield json.loads(line)


def _read_tail() -> tuple[int, str, list[dict] | None]:
    """Return (last_seq, last_h, last_chains_compact_or_None)."""
    last_seq = -1
    last_h = GENESIS_PREV
    last_chains = None
    for e in _iter_entries():
        last_seq = e["seq"]
        last_h = e["h"]
        last_chains = e.get("chains")
    return last_seq, last_h, last_chains


def _scores_snapshot_full() -> list[dict]:
    """Live snapshot in v2 (verbose) shape, used internally before compacting.

    [{chain, latest_block, agents: [...]}, ...]
    """
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


def _to_compact(chains_full: list[dict]) -> list[dict]:
    """Verbose v2 → compact: drop agents[], keep counters + digest."""
    compact: list[dict] = []
    for c in chains_full:
        agents = c.get("agents", []) or []
        compact.append({
            "chain":            c["chain"],
            "latest_block":     int(c.get("latest_block", 0) or 0),
            "registered":       len(agents),
            "with_feedback":    sum(1 for a in agents if a.get("feedback_count", 0) > 0),
            "agents_sha256":    _digest_agents(agents),
        })
    return compact


def append_snapshot() -> dict:
    """Append a compact snapshot. Idempotent: if every per-chain digest equals
    the previous entry's (and chain set + latest_block matches), no append."""
    DATA.mkdir(parents=True, exist_ok=True)
    with _chain_lock():
        last_seq, last_h, last_chains = _read_tail()
        chains_full = _scores_snapshot_full()
        chains_compact = _to_compact(chains_full)

        if last_chains is not None and _canonical(last_chains) == _canonical(chains_compact):
            return {
                "appended":  False,
                "reason":    "no_change",
                "seq":       last_seq,
                "head_hash": last_h,
                "n_chains":  len(chains_compact),
            }

        seq = last_seq + 1
        entry = {
            "seq":     seq,
            "ts_utc":  datetime.now(timezone.utc).isoformat(),
            "chains":  chains_compact,
            "prev":    last_h,
        }
        entry["h"] = _hash(entry)
        with CHAIN_PATH.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(entry) + "\n")

        return {
            "appended":  True,
            "seq":       seq,
            "head_hash": entry["h"],
            "n_chains":  len(chains_compact),
            "total_registered":    sum(c["registered"]    for c in chains_compact),
            "total_with_feedback": sum(c["with_feedback"] for c in chains_compact),
        }


def verify() -> dict:
    """Re-hash every entry, check prev linkage + seq sequentiality."""
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


def recompute_chain_digest(chain: str) -> str:
    """Reproduce the per-chain agents_sha256 from the live scoring tables.
    Lets any third party verify that the published digest matches the
    current state of data/<chain>/feedback.jsonl + agents.jsonl."""
    from scoring import compute_score, list_agents, feedback_universe
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
    return _digest_agents(agents_block)


# ----------------------------------------------------------------- rebuild

def rebuild_compact_from_verbose(src_path: pathlib.Path, dst_path: pathlib.Path) -> dict:
    """One-shot: read the OLD verbose chain (mixed v1 single-chain + v2
    multi-chain shapes), rewrite each entry in compact form preserving
    seq + ts_utc, re-linking prev → newly-computed h chain-of-trust.

    Result: a small file where each entry's digest still proves what the
    full table was at that timestamp.
    """
    prev = GENESIS_PREV
    n = 0
    with src_path.open("r") as fh_in, dst_path.open("w") as fh_out:
        for line in fh_in:
            line = line.strip()
            if not line: continue
            old = json.loads(line)

            if "chains" in old:  # v2 (multi-chain) — already in the shape we want, just digest each chain
                chains_compact = _to_compact(old["chains"])
            else:  # v1 (single-chain, Base only) — promote top-level agents into a single base chain block
                agents = old.get("agents", []) or []
                chains_compact = [{
                    "chain":         "base",
                    "latest_block":  int(old.get("latest_block", 0) or 0),
                    "registered":    len(agents),
                    "with_feedback": sum(1 for a in agents if a.get("feedback_count", 0) > 0),
                    "agents_sha256": _digest_agents(agents),
                }]

            new_entry = {
                "seq":     old["seq"],
                "ts_utc":  old["ts_utc"],
                "chains":  chains_compact,
                "prev":    prev,
            }
            new_entry["h"] = _hash(new_entry)
            fh_out.write(_canonical(new_entry) + "\n")
            prev = new_entry["h"]
            n += 1

    return {"rebuilt_entries": n, "head_hash": prev}


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
    if cmd == "recompute" and len(argv) > 2:
        chain = argv[2]
        print(recompute_chain_digest(chain))
        return 0
    if cmd == "rebuild" and len(argv) > 3:
        src = pathlib.Path(argv[2]); dst = pathlib.Path(argv[3])
        r = rebuild_compact_from_verbose(src, dst)
        print(json.dumps(r, indent=2))
        return 0
    print(f"usage: {argv[0]} verify | append | head | recompute <chain> | rebuild <src> <dst>")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
