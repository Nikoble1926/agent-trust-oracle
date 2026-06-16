"""ERC-8004 indexer for Base mainnet.

Off-chain JSONL store of `Registered`, `NewFeedback`, `FeedbackRevoked` events
from the canonical IdentityRegistry + ReputationRegistry. Idempotent: keeps
last_scanned_block in state.json and resumes from there. Revoked feedback is
subtracted at read-time by `scoring.load_active_feedback`.

Read-only on chain. No private keys, no writes.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Iterable

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

RPCS = [
    os.getenv("RPC_URL", "https://base-rpc.publicnode.com"),
    "https://base.llamarpc.com",
    "https://1rpc.io/base",
    "https://mainnet.base.org",
]
IDENTITY_ADDR   = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
REPUTATION_ADDR = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
DEPLOY_ID  = 47_408_284
DEPLOY_REP = 47_408_290

T_REGISTERED        = "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a"
T_NEW_FEEDBACK      = "0x6a4a61743519c9d648a14e6493f47dbe3ff1aa29e7785c96c8326a205e58febc"
T_FEEDBACK_REVOKED  = "0x25156fd3288212246d8b008d5921fde376c71ed14ac2e072a506eb06fde6d09d"

CHUNK = 9500  # safely under public RPC's 10,000 cap

STATE_PATH = DATA / "state.json"
AGENTS_PATH = DATA / "agents.jsonl"
FEEDBACK_PATH = DATA / "feedback.jsonl"
REVOKED_PATH = DATA / "revoked.jsonl"


def _rpc(method: str, params: list, retries: int = 6) -> dict:
    """Call an RPC, rotating through RPCS on transient errors/empty body."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    last_err: dict = {"error": {"message": "no attempts"}}
    for attempt in range(retries):
        rpc_url = RPCS[attempt % len(RPCS)]
        r = subprocess.run(
            ["curl", "-s", "--max-time", "25", "-X", "POST",
             "-H", "content-type: application/json",
             "-H", "User-Agent: Mozilla/5.0",
             "--data", body, rpc_url],
            capture_output=True, text=True,
        )
        if not r.stdout.strip():
            last_err = {"error": {"message": f"empty body from {rpc_url}"}}
            time.sleep(0.5 + attempt)
            continue
        try:
            res = json.loads(r.stdout)
        except Exception:
            last_err = {"error": {"message": f"parse from {rpc_url}: {r.stdout[:200]}"}}
            time.sleep(0.5 + attempt)
            continue
        if "result" in res:
            return res
        msg = (res.get("error", {}).get("message") or "").lower()
        if "rate" in msg or "too many" in msg or "limit" in msg and "10,000" not in msg:
            last_err = res
            time.sleep(1.0 + attempt)
            continue
        return res  # surface non-transient error to caller
    return last_err


def _to_hex(n: int) -> str:
    return hex(int(n))


def _latest_block() -> int:
    return int(_rpc("eth_blockNumber", [])["result"], 16)


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def _append_jsonl(path: pathlib.Path, rows: Iterable[dict]) -> int:
    n = 0
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            n += 1
    return n


def _decode_registered(log: dict) -> dict:
    agent_id = int(log["topics"][1], 16)
    owner = "0x" + log["topics"][2][-40:]
    data = log["data"]
    s = data[2:] if data.startswith("0x") else data
    try:
        offset = int(s[0:64], 16) * 2
        length = int(s[offset:offset + 64], 16)
        uri = bytes.fromhex(s[offset + 64:offset + 64 + length * 2]).decode("utf-8", "replace")
    except Exception:
        uri = ""
    return {
        "agent_id":     agent_id,
        "owner":        owner,
        "agent_uri":    uri,
        "block":        int(log["blockNumber"], 16),
        "tx":           log.get("transactionHash"),
        "log_index":    int(log.get("logIndex", "0x0"), 16),
    }


def _decode_new_feedback(log: dict) -> dict:
    agent_id        = int(log["topics"][1], 16)
    client_address  = "0x" + log["topics"][2][-40:]
    indexed_tag1    = log["topics"][3] if len(log["topics"]) > 3 else None

    data = log["data"]
    s = data[2:] if data.startswith("0x") else data

    def word(i: int) -> str:
        return s[i * 64:(i + 1) * 64]

    feedback_index = int(word(0), 16)

    value_raw = int(word(1), 16)
    if value_raw >= 2 ** 127:
        value_raw -= 2 ** 128

    value_decimals = int(word(2), 16)

    value = value_raw / (10 ** value_decimals) if value_decimals else float(value_raw)

    return {
        "agent_id":        agent_id,
        "client":          client_address,
        "feedback_index":  feedback_index,
        "value":           value,
        "value_raw":       value_raw,
        "value_decimals":  value_decimals,
        "indexed_tag1":    indexed_tag1,
        "block":           int(log["blockNumber"], 16),
        "tx":              log.get("transactionHash"),
        "log_index":       int(log.get("logIndex", "0x0"), 16),
    }


def _decode_revoked(log: dict) -> dict:
    return {
        "agent_id":       int(log["topics"][1], 16),
        "client":         "0x" + log["topics"][2][-40:],
        "feedback_index": int(log["topics"][3], 16),
        "block":          int(log["blockNumber"], 16),
        "tx":             log.get("transactionHash"),
    }


def scan_topic(address: str, topic0: str, from_block: int, to_block: int) -> list[dict]:
    out: list[dict] = []
    start = from_block
    chunk = CHUNK
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        r = _rpc("eth_getLogs", [{
            "address": address,
            "topics": [topic0],
            "fromBlock": _to_hex(start),
            "toBlock": _to_hex(end),
        }])
        if "error" in r:
            msg = (r["error"].get("message") or "").lower()
            if "limit" in msg or "range" in msg:
                chunk = max(1000, chunk // 2)
                continue
            raise RuntimeError(f"RPC error scanning {address} {start}-{end}: {r['error']}")
        logs = r["result"]
        out.extend(logs)
        start = end + 1
    return out


def main() -> int:
    state = _load_state()
    latest = _latest_block()
    last_id_block  = state.get("last_block_identity",   DEPLOY_ID - 1)
    last_rep_block = state.get("last_block_reputation", DEPLOY_REP - 1)

    if last_id_block < latest:
        new_reg = scan_topic(IDENTITY_ADDR, T_REGISTERED, last_id_block + 1, latest)
        n_reg = _append_jsonl(AGENTS_PATH, (_decode_registered(l) for l in new_reg))
        state["last_block_identity"] = latest
        state["updated_at"] = int(time.time())
        _save_state(state)  # commit per-stage: a later RPC failure can't cause double-append
        print(f"[indexer] +{n_reg} Registered events (blocks {last_id_block + 1:,}..{latest:,})")
    else:
        n_reg = 0

    if last_rep_block < latest:
        new_fb = scan_topic(REPUTATION_ADDR, T_NEW_FEEDBACK,     last_rep_block + 1, latest)
        new_rv = scan_topic(REPUTATION_ADDR, T_FEEDBACK_REVOKED, last_rep_block + 1, latest)
        n_fb = _append_jsonl(FEEDBACK_PATH, (_decode_new_feedback(l) for l in new_fb))
        n_rv = _append_jsonl(REVOKED_PATH, (_decode_revoked(l) for l in new_rv))
        state["last_block_reputation"] = latest
        state["updated_at"] = int(time.time())
        _save_state(state)
        print(f"[indexer] +{n_fb} NewFeedback, +{n_rv} FeedbackRevoked (blocks {last_rep_block + 1:,}..{latest:,})")
    else:
        n_fb = n_rv = 0

    state["latest_block"] = latest
    state["updated_at"] = int(time.time())
    _save_state(state)
    print(f"[indexer] state saved: latest={latest:,}  reg={n_reg}  fb={n_fb}  rv={n_rv}")

    # Provable snapshot + best-effort GitHub timestamp anchor
    try:
        import provable
        snap = provable.append_snapshot()
        if snap.get("appended"):
            print(f"[provable] snapshot seq={snap['seq']} head={snap['head_hash'][:16]} agents={snap['n_agents']}")
            _anchor_to_github()
        else:
            print(f"[provable] no_change at seq={snap.get('seq')} (head={snap.get('head_hash','?')[:16]})")
    except Exception as exc:
        print(f"[provable] skipped: {exc}", file=sys.stderr)

    return 0


def _anchor_to_github() -> None:
    """Push the chain JSONL to the public repo as a third-party timestamp.
    Wrapped in broad except — a GitHub outage must NOT break the indexer."""
    pub = pathlib.Path("/root/agent-trust-oracle-pub")
    if not pub.is_dir():
        print("[anchor] public repo dir absent — skipping", file=sys.stderr)
        return
    try:
        chain_src = DATA / "scores_chain.jsonl"
        chain_dst_dir = pub / "provable"
        chain_dst_dir.mkdir(parents=True, exist_ok=True)
        chain_dst = chain_dst_dir / "scores_chain.jsonl"
        chain_dst.write_bytes(chain_src.read_bytes())

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        msg = f"provable: score snapshot {ts}"
        for cmd in (
            ["git", "-C", str(pub), "add", "-f", "provable/scores_chain.jsonl"],
            ["git", "-C", str(pub),
             "-c", "user.name=Nikoble1926",
             "-c", "user.email=dimitriadisn9@gmail.com",
             "commit", "-qm", msg],
            ["git", "-C", str(pub), "push", "-q"],
        ):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                # 'nothing to commit' is a normal no-op; surface other errors but don't raise.
                if "nothing to commit" in (r.stdout + r.stderr):
                    print("[anchor] chain unchanged — nothing to push", file=sys.stderr)
                    return
                print(f"[anchor] git failed ({' '.join(cmd[:4])}…): "
                      f"stdout={r.stdout[:160]}  stderr={r.stderr[:160]}", file=sys.stderr)
                return
        print("[anchor] pushed to GitHub")
    except Exception as exc:
        print(f"[anchor] skipped: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
