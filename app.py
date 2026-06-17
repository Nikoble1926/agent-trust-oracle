"""Agent Trust Oracle — x402 service over ERC-8004 reputation.

Cross-chain, Ethereum-primary. Free routes for discovery and verification;
paid /agent-trust route gated by the x402 middleware.
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync, PaymentOption
from x402.http.facilitator_client_base import CreateHeadersAuthProvider
from x402.http.middleware.flask import payment_middleware
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServerSync
from x402.extensions.bazaar import declare_discovery_extension, OutputConfig

from cdp.auth import GetAuthHeadersOptions, get_auth_headers
from eth_account import Account
from eth_account.messages import encode_defunct

from scoring import (
    compute_score, list_agents, feedback_universe, known_chains, pick_demo_agent,
    METHODOLOGY_URL, DEFAULT_CHAIN,
    WEIGHTS, MIN_DISTINCT_CLIENTS, RECENCY_HALFLIFE_BLOCKS,
    VOLUME_REF, CLIENT_BREADTH_REF,
)
import provable

ROOT = pathlib.Path(__file__).parent
load_dotenv(ROOT / ".env")

EVM_ADDRESS         = os.getenv("EVM_ADDRESS", "0xc87a06DEE4c0E85912296002617120BBfd5EF990")
NETWORK             = os.getenv("NETWORK", "eip155:8453")
PRICE               = os.getenv("PRICE", "$0.005")
SIGNING_KEY         = os.getenv("SIGNING_KEY")
CDP_API_KEY_ID      = os.getenv("CDP_API_KEY_ID")
CDP_API_KEY_SECRET  = os.getenv("CDP_API_KEY_SECRET")
FACILITATOR_URL     = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
PORT                = int(os.getenv("PORT", "4023"))

_SIGNER_ADDRESS = Account.from_key(SIGNING_KEY).address if SIGNING_KEY else None

_DISCLAIMER = (
    "Read-only on-chain analytics over ERC-8004 Identity/Reputation registries "
    "on multiple EVM chains. Informational only — not investment, employment, or "
    "counterparty advice. Score reflects observed on-chain feedback only; absence "
    "of evidence is not evidence of trustworthiness. See methodology link for the "
    "scoring math."
)


# --------------------------------------------------------------- signing util

def _signed(payload: dict) -> dict:
    if not SIGNING_KEY:
        return payload
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = Account.sign_message(encode_defunct(text=body), private_key=SIGNING_KEY).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    out = dict(payload)
    out["signed_by"] = _SIGNER_ADDRESS
    out["signature"] = sig
    return out


def _resolve_chain(arg: str | None) -> str:
    """Lowercase chain name or fall back to default. Unknown chains are NOT
    rejected here — scoring returns insufficient_data cleanly for empties."""
    if not arg:
        return DEFAULT_CHAIN
    return arg.strip().lower()


# --------------------------------------------------------------- app + x402

app = Flask(__name__)

CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
_CDP_HOST = "api.cdp.coinbase.com"
_CDP_BASE_PATH = "/platform/v2/x402"


def _cdp_endpoint_headers(method: str, path: str) -> dict:
    return get_auth_headers(GetAuthHeadersOptions(
        api_key_id=CDP_API_KEY_ID,
        api_key_secret=CDP_API_KEY_SECRET,
        request_method=method,
        request_host=_CDP_HOST,
        request_path=path,
    ))


def cdp_create_headers() -> dict:
    return {
        "verify":    _cdp_endpoint_headers("POST", f"{_CDP_BASE_PATH}/verify"),
        "settle":    _cdp_endpoint_headers("POST", f"{_CDP_BASE_PATH}/settle"),
        "supported": _cdp_endpoint_headers("GET",  f"{_CDP_BASE_PATH}/supported"),
        "bazaar":    _cdp_endpoint_headers("POST", f"{_CDP_BASE_PATH}/bazaar"),
    }


if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(
        url=CDP_FACILITATOR_URL,
        auth_provider=CreateHeadersAuthProvider(cdp_create_headers),
    ))
else:
    facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL))

server = x402ResourceServerSync(facilitator)
server.register(NETWORK, ExactEvmServerScheme())


# --------------------------------------------------------------- free routes

@app.route("/")
def landing():
    chains = known_chains()
    chain_pills = "&nbsp;".join(f"<code>{c}</code>" for c in chains)
    return (
        "<!doctype html><meta charset=utf-8><title>Agent Trust Oracle (x402)</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:780px;margin:40px auto;padding:0 18px;color:#1e293b;line-height:1.6}"
        "code{background:#f3f4f6;padding:2px 6px;border-radius:4px}a{color:#1d4ed8}</style>"
        "<h1>Agent Trust Oracle</h1>"
        "<p>Read-only, signed, pay-per-call trust scores for "
        "<a href='https://eips.ethereum.org/EIPS/eip-8004'>ERC-8004</a> agents. "
        "Multi-chain, Ethereum-primary. Source registries are the canonical "
        f"<code>0x8004A1…A432</code> + <code>0x8004BA…9b63</code> on every chain.</p>"
        f"<p><strong>Wired chains:</strong> {chain_pills} &nbsp; (default: <code>{DEFAULT_CHAIN}</code>)</p>"
        "<h2>Endpoints</h2><ul>"
        "<li><code>GET /agent-trust/preview</code> — free demo score (a real Ethereum agent with ≥3 distinct clients)</li>"
        f"<li><code>GET /agent-trust?agent=&lt;id&gt;&amp;chain=&lt;name&gt;</code> — paid {PRICE} USDC (Base mainnet) — signed JSON</li>"
        "<li><code>GET /health</code> — per-chain summary</li>"
        "<li><code>GET /methodology</code> — exact scoring math</li>"
        "<li><code>GET /provable/head</code> + <code>/provable/verify</code> — tamper-evident snapshot chain</li>"
        "<li><code>GET /.well-known/x402</code> — x402 manifest</li>"
        "<li><code>GET /llms.txt</code> — AI-discovery</li>"
        "</ul>"
        f"<p>Methodology: <a href='{METHODOLOGY_URL}'>{METHODOLOGY_URL}</a></p>"
        f"<p><em>{_DISCLAIMER}</em></p>"
    )


@app.route("/health")
def health():
    per_chain = []
    total_registered = total_feedback = 0
    for c in known_chains():
        agents = list_agents(c)
        u = feedback_universe(c)
        per_chain.append({
            "chain":                c,
            "registered_agents":    len(agents),
            "agents_with_feedback": len(u["agents_with_feedback"]),
            "total_feedback":       u["total_feedback"],
            "latest_block_seen":    u.get("latest_block_seen", 0),
        })
        total_registered += len(agents)
        total_feedback   += u["total_feedback"]
    return jsonify({
        "status":            "ok",
        "default_chain":     DEFAULT_CHAIN,
        "chains":            per_chain,
        "total_registered":  total_registered,
        "total_feedback":    total_feedback,
        "signer":            _SIGNER_ADDRESS,
        "now":               datetime.now(timezone.utc).isoformat(),
    })


@app.route("/llms.txt")
def llms_txt():
    body = (
        "# Agent Trust Oracle\n\n"
        "> Pay-per-call trust scores for ERC-8004 agents across multiple EVM chains "
        f"(default: {DEFAULT_CHAIN}). Endpoint /agent-trust?agent=<id>&chain=<name> returns a signed score + "
        f"transparent breakdown for {PRICE} USDC.\n\n"
        "## Endpoints\n"
        "- GET /agent-trust/preview — free demo score (a real Ethereum agent with sufficient data)\n"
        f"- GET /agent-trust?agent=<id>&chain=<name> — paid {PRICE}, signed JSON\n"
        "- GET /health — per-chain summary (agents, feedback, latest_block)\n"
        "- GET /provable/head — latest hash-chain snapshot head\n"
        "- GET /provable/verify — re-hash whole chain, return {ok, count, head_hash}\n"
        "- GET /.well-known/x402 — machine-readable manifest\n"
        "- GET /methodology — exact scoring math\n\n"
        "## Wired chains\n"
        f"- {', '.join(known_chains())} (default: {DEFAULT_CHAIN})\n"
        "- Canonical Identity/Reputation registries at the same 0x8004A1…/0x8004BA… addresses on every chain.\n\n"
        "## Tamper-evidence\n"
        "Every indexer run appends a sha256-chained, COMPACT snapshot to data/scores_chain.jsonl. "
        "Each entry stores per-chain counters + sha256 of the full sorted per-agent table — "
        "not the table itself (that stays in data/<chain>/feedback.jsonl + agents.jsonl, which is "
        "reproducible from the on-chain logs). Anyone can recompute the table for a given chain and "
        "check the digest with `python3 provable.py recompute <chain>`. The chain is mirrored to "
        "github.com/Nikoble1926/agent-trust-oracle/provable/scores_chain.jsonl so every push is a "
        "third-party timestamp anchor. Verify locally with `python3 provable.py verify`.\n"
        "\n## What this is NOT (yet)\n"
        "Identity is per-chain. We do not merge agent IDs across chains. As of 2026-06-16 zero owner "
        "wallets have agents on more than one chain — a cross-chain operator view has no signal yet. "
        "Future work-item once that changes.\n"
    )
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/provable/head")
def provable_head():
    h = provable.head()
    if not h:
        return jsonify({"empty": True, "message": "no snapshots yet"}), 200
    chains_block = h.get("chains") or []
    per_chain = [
        {
            "chain":          c.get("chain"),
            "latest_block":   c.get("latest_block"),
            "registered":     c.get("registered"),
            "with_feedback":  c.get("with_feedback"),
            "agents_sha256":  c.get("agents_sha256"),
        }
        for c in chains_block
    ]
    return jsonify({
        "seq":              h["seq"],
        "ts_utc":           h["ts_utc"],
        "n_chains":         len(per_chain),
        "total_registered":    sum((c.get("registered") or 0)    for c in per_chain),
        "total_with_feedback": sum((c.get("with_feedback") or 0) for c in per_chain),
        "chains":           per_chain,
        "prev":             h["prev"],
        "head_hash":        h["h"],
    })


@app.route("/provable/verify")
def provable_verify():
    return jsonify(provable.verify())


@app.route("/methodology")
def methodology():
    w = WEIGHTS
    chains_list = ", ".join(f"<code>{c}</code>" for c in known_chains())
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Trust Oracle &mdash; Methodology</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;max-width:780px;margin:40px auto;padding:0 18px;color:#1e293b;line-height:1.65}}
code,pre{{background:#f3f4f6;padding:2px 6px;border-radius:4px;font-size:14px}}
pre{{padding:14px 16px;overflow-x:auto;font-size:13px;line-height:1.45}}
h1,h2{{letter-spacing:-.3px}}
h2{{margin-top:32px;border-bottom:1px solid #e5e7eb;padding-bottom:4px}}
table{{width:100%;border-collapse:collapse;margin:14px 0;font-size:15px}}
th,td{{border:1px solid #e5e7eb;padding:8px 10px;text-align:left}}
th{{background:#f8fafc}}
a{{color:#1d4ed8}}
.note{{background:#eef6ff;border:1px solid #cfe3fb;border-radius:8px;padding:12px 14px;color:#234;margin:14px 0}}
.disc{{background:#fffbef;border:1px solid #f3e2b3;border-radius:8px;padding:10px 14px;color:#6b5a2a;font-size:14px;margin-top:24px}}
</style></head><body>
<h1>Agent Trust Oracle &mdash; Methodology (v2, cross-chain)</h1>
<p>Read-only trust scores for <a href="https://eips.ethereum.org/EIPS/eip-8004">ERC-8004</a> agents across multiple EVM chains. Default chain is <code>{DEFAULT_CHAIN}</code>. Wired chains: {chains_list}. Values below are pulled live from <code>scoring.py</code>.</p>

<h2>Score range</h2>
<p><strong>0 to 100</strong>. Or <strong>null</strong> when data is too thin (see refusal threshold).</p>

<h2>Components &amp; weights</h2>
<table>
<thead><tr><th>Component</th><th>Weight</th><th>What it measures</th></tr></thead>
<tbody>
<tr><td><code>value_avg</code></td><td>{w['value_avg']:.2f}</td><td>Mean of feedback values, normalised &minus;100..100 &rarr; 0..100.</td></tr>
<tr><td><code>client_breadth</code></td><td>{w['client_breadth']:.2f}</td><td>Log-scaled count of distinct client addresses.</td></tr>
<tr><td><code>volume</code></td><td>{w['volume']:.2f}</td><td>Log-scaled count of (non-revoked) feedback entries.</td></tr>
<tr><td><code>recency</code></td><td>{w['recency']:.2f}</td><td>Weighted mean of values with exponential block-age decay.</td></tr>
</tbody></table>

<h2>Refusal threshold (insufficient_data)</h2>
<p>If the agent has fewer than <strong>{MIN_DISTINCT_CLIENTS} distinct clients</strong>, the response is <code>score = null</code> with <code>status = "insufficient_data"</code>. Single largest sybil-mitigation lever.</p>

<h2>Value normalisation (&minus;100..100 &rarr; 0..100)</h2>
<pre>normalised_value = (clamp(value, -100, +100) + 100) / 2</pre>

<h2>Log-axis reference points</h2>
<ul>
<li><code>CLIENT_BREADTH_REF = {CLIENT_BREADTH_REF}</code> distinct clients &rarr; component saturates at 100.</li>
<li><code>VOLUME_REF = {VOLUME_REF}</code> feedback entries &rarr; component saturates at 100.</li>
</ul>
<pre>def log_axis(count, ref):
    if count &lt;= 0: return 0.0
    return min(100, 100 * log1p(count) / log1p(ref))</pre>

<h2>Recency decay</h2>
<pre>recency_weight(block) = 0.5 ** ((latest_block - block) / {RECENCY_HALFLIFE_BLOCKS})</pre>
<p>Half-life: <strong>{RECENCY_HALFLIFE_BLOCKS:,} blocks</strong>. Block-rate varies per chain; we still apply the same half-life — older entries weigh less proportionally to that chain&rsquo;s block velocity.</p>

<h2>Final aggregate</h2>
<pre>score = value_avg * {w['value_avg']:.2f}
      + client_breadth * {w['client_breadth']:.2f}
      + volume * {w['volume']:.2f}
      + recency * {w['recency']:.2f}</pre>

<h2>Identity is per-chain</h2>
<p>We score by <strong>(chain, agent_id)</strong>. We do NOT merge agent IDs across chains. A probe on 2026-06-16 found zero owner wallets with agents on more than one chain, so a cross-chain operator view has no signal today. Future work-item once that changes.</p>

<h2>What we do NOT do (yet)</h2>
<ul>
<li>No client-graph sybil detection.</li>
<li>No tag-based weighting.</li>
<li>No off-chain reputation imports.</li>
<li>No cross-chain agent identity merge.</li>
</ul>

<h2>Sources (read-only)</h2>
<ul>
<li>Canonical Identity/Reputation registries at <code>0x8004A169FB4a3325136EB29fA0ceB6D2e539a432</code> + <code>0x8004BAa17C55a88189AE136b182e5fdA19dE9b63</code> on every wired chain.</li>
<li>Source code: <a href="https://github.com/Nikoble1926/agent-trust-oracle">github.com/Nikoble1926/agent-trust-oracle</a></li>
<li>Tamper-evidence: <a href="/provable/verify">/provable/verify</a> + <a href="/provable/head">/provable/head</a>. Each entry stores per-chain counters + a <code>agents_sha256</code> digest of the full sorted per-agent table (not the table itself). Anyone can recompute the table from <code>data/&lt;chain&gt;/feedback.jsonl</code> and check the digest with <code>python3 provable.py recompute &lt;chain&gt;</code>. Chain mirrored to <a href="https://github.com/Nikoble1926/agent-trust-oracle/blob/main/provable/scores_chain.jsonl">github.com/Nikoble1926/agent-trust-oracle/blob/main/provable/scores_chain.jsonl</a>.</li>
</ul>

<div class="note"><strong>Signatures.</strong> Every paid <code>/agent-trust</code> response and the free <code>/agent-trust/preview</code> response carry <code>signed_by</code> + <code>signature</code>. Verify with Ethereum <code>personal_sign</code>/<code>ecrecover</code> over canonical JSON (signed_by + signature stripped). Signer: <code>{_SIGNER_ADDRESS or '(unset)'}</code>.</div>

<div class="disc"><strong>Disclaimer.</strong> Read-only on-chain analytics. Informational only &mdash; not investment, employment, or counterparty advice.</div>
</body></html>"""
    return body, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/agent-trust/preview")
def preview():
    # Choose a real Ethereum agent with ≥MIN_DISTINCT_CLIENTS — return a real score.
    demo_chain = DEFAULT_CHAIN
    aid = pick_demo_agent(demo_chain)
    if aid is None:
        # Fall back to any chain with eligible agents
        for c in known_chains():
            if c == demo_chain: continue
            aid = pick_demo_agent(c)
            if aid is not None:
                demo_chain = c
                break
    result = compute_score(demo_chain, aid) if aid is not None else {
        "score": None, "status": "no_eligible_agent_in_universe",
    }
    payload = {
        "preview":      True,
        "demo_chain":   demo_chain,
        "demo_agent":   aid,
        "result":       result,
        "note":         f"For any agent id, GET /agent-trust?agent=<id>&chain=<name> ({PRICE} USDC on Base mainnet) returns a signed response.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer":   _DISCLAIMER,
    }
    return jsonify(_signed(payload))


@app.route("/.well-known/x402")
def x402_manifest():
    return jsonify({
        "x402Version": 2,
        "name":        "Agent Trust Oracle",
        "description": (
            "Pay-per-call ERC-8004 trust scores for AI agents across multiple EVM "
            f"chains (default: {DEFAULT_CHAIN}). Reads on-chain Identity + Reputation "
            "registries, returns transparent score + component breakdown + methodology "
            "link. ECDSA-signed responses. Tamper-evident snapshot chain mirrored to "
            "GitHub for third-party timestamping."
        ),
        "signer":      _SIGNER_ADDRESS,
        "signature_scheme": (
            "Every /agent-trust and /agent-trust/preview response carries 'signed_by' "
            "(address) and 'signature'. Verify with Ethereum personal_sign / ecrecover "
            "over json.dumps(payload, sort_keys=True, separators=(',',':')) with the "
            "two keys stripped. recover == signed_by => authentic & untampered."
        ),
        "chains":      known_chains(),
        "default_chain": DEFAULT_CHAIN,
        "sources": {
            "identity_registry":   "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
            "reputation_registry": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
        },
        "endpoints": [
            {"path": "/agent-trust", "method": "GET", "price": PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {
                 "agent": "ERC-8004 agent id (uint256) on the selected chain",
                 "chain": f"one of {known_chains()}; default {DEFAULT_CHAIN}",
             },
             "description": "Signed trust score + breakdown for one (chain, agent) pair"},
            {"path": "/agent-trust/preview", "method": "GET", "price": "$0",
             "network": NETWORK, "mimeType": "application/json",
             "description": "Free demo score (a real Ethereum agent with sufficient data)"},
        ],
        "methodology": METHODOLOGY_URL,
        "disclaimer":  _DISCLAIMER,
    })


@app.route("/agent-trust")
def agent_trust():
    raw = request.args.get("agent", "").strip()
    chain = _resolve_chain(request.args.get("chain"))
    if not raw.isdigit():
        return jsonify({"error": "bad_request", "message": "query param 'agent' must be a non-negative integer (ERC-8004 agentId)"}), 400
    agent_id = int(raw)
    result = compute_score(chain, agent_id)
    cfg_chains = known_chains()
    if chain not in cfg_chains:
        result.setdefault("status", "unknown_chain")
    payload = {
        "chain":        chain,
        "agent_id":     agent_id,
        "result":       result,
        "sources": {
            "identity_registry":   "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
            "reputation_registry": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
        },
        "wired_chains": cfg_chains,
        "methodology":  METHODOLOGY_URL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer":   _DISCLAIMER,
    }
    return jsonify(_signed(payload))


# --------------------------------------------------------------- middleware

_TRUST_EXAMPLE = {
    "chain": "ethereum",
    "agent_id": 123,
    "result": {
        "score": 78.4,
        "status": "ok",
        "components": {"value_avg": 75.0, "client_breadth": 80.5, "volume": 88.2, "recency": 76.1},
        "inputs": {"feedback_count": 42, "distinct_clients": 18},
    },
    "methodology": "https://trust.nsgoods.org/methodology",
}

_TRUST_SCHEMA = {
    "type": "object",
    "properties": {
        "chain":       {"type": "string"},
        "agent_id":    {"type": "integer"},
        "result":      {"type": "object"},
        "methodology": {"type": "string"},
    },
    "required": ["chain", "agent_id", "result"],
}

_BAZAAR_TRUST_EXT = declare_discovery_extension(
    input_schema={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "ERC-8004 agentId (uint256), e.g. 25975."},
            "chain": {"type": "string", "description": f"one of {known_chains()}; default {DEFAULT_CHAIN}."},
        },
        "required": ["agent"],
    },
    output=OutputConfig(example=_TRUST_EXAMPLE, schema=_TRUST_SCHEMA),
)
_BAZAAR_TRUST_EXT["bazaar"]["info"]["input"]["method"] = "GET"


routes = {
    "GET /agent-trust": RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=EVM_ADDRESS, price=PRICE, network=NETWORK)],
        mime_type="application/json",
        description=(
            "Cross-chain ERC-8004 trust score for one (chain, agent) pair — signed "
            "JSON with transparent component breakdown (value_avg / client_breadth / "
            "volume / recency), per-chain context, and methodology link. USDC pay-per-call "
            "on Base mainnet."
        ),
        extensions=_BAZAAR_TRUST_EXT,
    ),
}

payment_middleware(app, routes=routes, server=server)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
