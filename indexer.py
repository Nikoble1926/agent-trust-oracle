"""Multi-chain ERC-8004 indexer.

Scans Registered + NewFeedback + FeedbackRevoked events for the canonical
IdentityRegistry + ReputationRegistry on every configured EVM chain. Stores
raw events as JSONL under data/<chain>/. Idempotent per chain: state.json
keeps the last scanned block.

Read-only. No on-chain writes. After each successful scan it appends a
cross-chain provable snapshot and best-effort pushes the chain to GitHub.
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

IDENTITY_ADDR   = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
REPUTATION_ADDR = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"

T_REGISTERED       = "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a"
T_NEW_FEEDBACK     = "0x6a4a61743519c9d648a14e6493f47dbe3ff1aa29e7785c96c8326a205e58febc"
T_FEEDBACK_REVOKED = "0x25156fd3288212246d8b008d5921fde376c71ed14ac2e072a506eb06fde6d09d"

# Canonical chain config. Deploy blocks below were established via on-chain
# eth_getCode binary search on 2026-06-16. The reputation deploy is a few
# blocks after identity in every case — we just start the scan a couple of
# blocks earlier than identity to be safe; the indexer is idempotent so
# scanning empty leading blocks costs at most one extra getLogs call.
CHAINS: dict[str, dict] = {
    "ethereum": {
        "rpcs": [
            "https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
        ],
        "deploy_block_identity":   24339871,
        "deploy_block_reputation": 24339871,
        "chain_id": 1,
        "explorer": "https://etherscan.io",
    },
    "base": {
        "rpcs": [
            "https://base-rpc.publicnode.com",
            "https://base.llamarpc.com",
        ],
        "deploy_block_identity":   47415059,
        "deploy_block_reputation": 47415064,
        "chain_id": 8453,
        "explorer": "https://basescan.org",
    },
    "polygon": {
        "rpcs": [
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon-rpc.com",
        ],
        "deploy_block_identity":   88623035,
        "deploy_block_reputation": 88623037,
        "chain_id": 137,
        "explorer": "https://polygonscan.com",
    },
    "bsc": {
        "rpcs": [
            "https://bsc-rpc.publicnode.com",
            "https://bsc-dataseed.bnbchain.org",
        ],
        "deploy_block_identity":   104625738,
        "deploy_block_reputation": 104625742,
        "chain_id": 56,
        "explorer": "https://bscscan.com",
    },
    "mantle": {
        "rpcs": [
            "https://mantle-rpc.publicnode.com",
            "https://rpc.mantle.xyz",
        ],
        "deploy_block_identity":   96754588,
        "deploy_block_reputation": 96754591,
        "chain_id": 5000,
        "explorer": "https://mantlescan.xyz",
    },
}

CHUNK_DEFAULT = 9500   # under all public RPC 10k caps
CHUNK_FLOOR   = 500


def _chain_dir(chain: str) -> pathlib.Path:
    d = DATA / chain
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(chain: str)    -> pathlib.Path: return _chain_dir(chain) / "state.json"
def _agents_path(chain: str)   -> pathlib.Path: return _chain_dir(chain) / "agents.jsonl"
def _feedback_path(chain: str) -> pathlib.Path: return _chain_dir(chain) / "feedback.jsonl"
def _revoked_path(chain: str)  -> pathlib.Path: return _chain_dir(chain) / "revoked.jsonl"


def _err_msg(res: dict) -> str:
    e = res.get("error", "")
    if isinstance(e, dict):
        return str(e.get("message") or "").lower()
    return str(e).lower()


def _rpc(rpcs: list[str], method: str, params: list, attempts: int = 6) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    last_err: dict = {"error": {"message": "no attempts"}}
    for i in range(attempts):
        url = rpcs[i % len(rpcs)]
        r = subprocess.run(
            ["curl", "-s", "--max-time", "25", "-X", "POST",
             "-H", "content-type: application/json",
             "-H", "User-Agent: Mozilla/5.0",
             "--data", body, url],
            capture_output=True, text=True,
        )
        if not r.stdout.strip():
            last_err = {"error": {"message": f"empty body from {url}"}}
            time.sleep(0.4 + i * 0.2)
            continue
        try:
            res = json.loads(r.stdout)
        except Exception:
            last_err = {"error": {"message": f"parse from {url}: {r.stdout[:160]}"}}
            time.sleep(0.4 + i * 0.2)
            continue
        if "result" in res and res["result"] is not None:
            return res
        msg = _err_msg(res)
        if "rate" in msg or "too many" in msg or "exceed" in msg or "throttle" in msg:
            last_err = res
            time.sleep(0.8 + i * 0.3)
            continue
        return res
    return last_err


def _to_hex(n: int) -> str: return hex(int(n))


def _latest_block(rpcs: list[str]) -> int:
    r = _rpc(rpcs, "eth_blockNumber", [])
    if "result" not in r:
        raise RuntimeError(f"eth_blockNumber failed: {r.get('error')}")
    return int(r["result"], 16)


def _load_state(chain: str) -> dict:
    p = _state_path(chain)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _save_state(chain: str, state: dict) -> None:
    _state_path(chain).write_text(json.dumps(state, indent=2) + "\n")


def _append_jsonl(path: pathlib.Path, rows: Iterable[dict]) -> int:
    n = 0
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            n += 1
    return n


def _decode_registered(log: dict, chain: str) -> dict:
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
        "chain":     chain,
        "agent_id":  agent_id,
        "owner":     owner,
        "agent_uri": uri,
        "block":     int(log["blockNumber"], 16),
        "tx":        log.get("transactionHash"),
        "log_index": int(log.get("logIndex", "0x0"), 16),
    }


def _decode_new_feedback(log: dict, chain: str) -> dict:
    agent_id       = int(log["topics"][1], 16)
    client_address = "0x" + log["topics"][2][-40:]
    indexed_tag1   = log["topics"][3] if len(log["topics"]) > 3 else None
    data = log["data"]
    s = data[2:] if data.startswith("0x") else data

    def word(i: int) -> str: return s[i * 64:(i + 1) * 64]

    feedback_index = int(word(0), 16)
    value_raw = int(word(1), 16)
    if value_raw >= 2 ** 127:
        value_raw -= 2 ** 128
    value_decimals = int(word(2), 16)
    value = value_raw / (10 ** value_decimals) if value_decimals else float(value_raw)

    return {
        "chain":          chain,
        "agent_id":       agent_id,
        "client":         client_address,
        "feedback_index": feedback_index,
        "value":          value,
        "value_raw":      value_raw,
        "value_decimals": value_decimals,
        "indexed_tag1":   indexed_tag1,
        "block":          int(log["blockNumber"], 16),
        "tx":             log.get("transactionHash"),
        "log_index":      int(log.get("logIndex", "0x0"), 16),
    }


def _decode_revoked(log: dict, chain: str) -> dict:
    return {
        "chain":          chain,
        "agent_id":       int(log["topics"][1], 16),
        "client":         "0x" + log["topics"][2][-40:],
        "feedback_index": int(log["topics"][3], 16),
        "block":          int(log["blockNumber"], 16),
        "tx":             log.get("transactionHash"),
    }


def scan_topic(rpcs: list[str], address: str, topic0: str,
               from_block: int, to_block: int, log_prefix: str = "") -> list[dict]:
    out: list[dict] = []
    start = from_block
    chunk = CHUNK_DEFAULT
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        r = _rpc(rpcs, "eth_getLogs", [{
            "address": address,
            "topics": [topic0],
            "fromBlock": _to_hex(start),
            "toBlock": _to_hex(end),
        }])
        if "result" not in r or r.get("result") is None:
            msg = _err_msg(r)
            if ("limit" in msg or "range" in msg or "exceed" in msg) and chunk > CHUNK_FLOOR:
                chunk = max(CHUNK_FLOOR, chunk // 2)
                continue
            print(f"  {log_prefix}WARN skip {start}-{end}: {msg[:80]}", file=sys.stderr)
            start = end + 1
            continue
        out.extend(r["result"])
        start = end + 1
    return out


def index_chain(chain: str, cfg: dict) -> dict:
    rpcs = cfg["rpcs"]
    state = _load_state(chain)
    try:
        latest = _latest_block(rpcs)
    except Exception as exc:
        print(f"[{chain}] WARN unreachable: {exc}", file=sys.stderr)
        return {"chain": chain, "error": str(exc)}

    deploy_id  = cfg["deploy_block_identity"]
    deploy_rep = cfg["deploy_block_reputation"]
    last_id    = state.get("last_block_identity",   deploy_id - 1)
    last_rep   = state.get("last_block_reputation", deploy_rep - 1)

    n_reg = n_fb = n_rv = 0

    if last_id < latest:
        new_reg = scan_topic(rpcs, IDENTITY_ADDR, T_REGISTERED, last_id + 1, latest, log_prefix=f"[{chain}/reg] ")
        n_reg = _append_jsonl(_agents_path(chain), (_decode_registered(l, chain) for l in new_reg))
        state["last_block_identity"] = latest
        state["updated_at"] = int(time.time())
        _save_state(chain, state)

    if last_rep < latest:
        new_fb = scan_topic(rpcs, REPUTATION_ADDR, T_NEW_FEEDBACK,     last_rep + 1, latest, log_prefix=f"[{chain}/fb] ")
        new_rv = scan_topic(rpcs, REPUTATION_ADDR, T_FEEDBACK_REVOKED, last_rep + 1, latest, log_prefix=f"[{chain}/rv] ")
        n_fb = _append_jsonl(_feedback_path(chain), (_decode_new_feedback(l, chain) for l in new_fb))
        n_rv = _append_jsonl(_revoked_path(chain),  (_decode_revoked(l, chain)      for l in new_rv))
        state["last_block_reputation"] = latest
        state["updated_at"] = int(time.time())
        _save_state(chain, state)

    state["latest_block"] = latest
    state["updated_at"] = int(time.time())
    _save_state(chain, state)

    summary = {"chain": chain, "latest_block": latest,
               "new_registered": n_reg, "new_feedback": n_fb, "new_revoked": n_rv}
    print(f"[{chain}] latest={latest:,}  +reg={n_reg}  +fb={n_fb}  +rv={n_rv}", flush=True)
    return summary


def _anchor_to_github() -> None:
    pub = pathlib.Path("/root/agent-trust-oracle-pub")
    if not pub.is_dir():
        print("[anchor] public repo dir absent — skipping", file=sys.stderr); return
    try:
        chain_src = DATA / "scores_chain.jsonl"
        if not chain_src.exists():
            print("[anchor] no chain file to push", file=sys.stderr); return
        chain_dst_dir = pub / "provable"
        chain_dst_dir.mkdir(parents=True, exist_ok=True)
        (chain_dst_dir / "scores_chain.jsonl").write_bytes(chain_src.read_bytes())

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        msg = f"provable: cross-chain score snapshot {ts}"
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
                if "nothing to commit" in (r.stdout + r.stderr):
                    print("[anchor] chain unchanged — nothing to push", file=sys.stderr); return
                print(f"[anchor] git failed ({' '.join(cmd[:4])}…): "
                      f"stdout={r.stdout[:160]} stderr={r.stderr[:160]}", file=sys.stderr)
                return
        print("[anchor] pushed to GitHub")
    except Exception as exc:
        print(f"[anchor] skipped: {exc}", file=sys.stderr)


def main(only_chains: list[str] | None = None) -> int:
    chains = only_chains or list(CHAINS)
    for c in chains:
        if c not in CHAINS:
            print(f"[indexer] WARN unknown chain {c}", file=sys.stderr); continue
        index_chain(c, CHAINS[c])

    try:
        import provable
        snap = provable.append_snapshot()
        if snap.get("appended"):
            print(f"[provable] snapshot seq={snap['seq']} head={snap['head_hash'][:16]} agents={snap['n_agents']}")
            _anchor_to_github()
        else:
            print(f"[provable] no_change at seq={snap.get('seq')} head={snap.get('head_hash','?')[:16]}")
    except Exception as exc:
        print(f"[provable] skipped: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    only = [c for c in sys.argv[1:] if c]
    sys.exit(main(only or None))
