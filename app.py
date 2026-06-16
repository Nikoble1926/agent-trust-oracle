"""Agent Trust Oracle — x402 service over ERC-8004 reputation on Base.

GET /                       free landing
GET /health                 free liveness
GET /llms.txt               free AI-discovery
GET /.well-known/x402       free manifest
GET /agent-trust/preview    free demo score (no payment)
GET /agent-trust?agent=N    PAID $0.005 USDC on Base mainnet
                             returns signed score + breakdown + methodology
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync, PaymentOption
from x402.http.facilitator_client_base import CreateHeadersAuthProvider
from x402.http.middleware.flask import payment_middleware
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServerSync

from cdp.auth import GetAuthHeadersOptions, get_auth_headers
from eth_account import Account
from eth_account.messages import encode_defunct

from scoring import (
    compute_score, list_agents, feedback_universe, METHODOLOGY_URL,
    WEIGHTS, MIN_DISTINCT_CLIENTS, RECENCY_HALFLIFE_BLOCKS,
    VOLUME_REF, CLIENT_BREADTH_REF,
)

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

DEMO_AGENT_ID = int(os.getenv("DEMO_AGENT_ID", "25975"))

_DISCLAIMER = (
    "Read-only on-chain analytics over ERC-8004 Identity/Reputation registries on "
    "Base mainnet. Informational only — not investment, employment, or counterparty "
    "advice. Score reflects observed feedback only; absence of evidence is not "
    "evidence of trustworthiness. See methodology link for the scoring math."
)


# ---------------------------------------------------------------- signed util

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


# ---------------------------------------------------------------- app + x402

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


# ---------------------------------------------------------------- free routes

@app.route("/")
def landing():
    return (
        "<!doctype html><meta charset=utf-8><title>Agent Trust Oracle (x402)</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 18px;color:#1e293b;line-height:1.6}"
        "code{background:#f3f4f6;padding:2px 6px;border-radius:4px}"
        "a{color:#1d4ed8}</style>"
        "<h1>Agent Trust Oracle</h1>"
        "<p>Read-only trust scores for <a href='https://eips.ethereum.org/EIPS/eip-8004'>ERC-8004</a> "
        "agents on Base mainnet. Sources: "
        f"<code>{ '0x8004A1...A432' }</code> (IdentityRegistry) + "
        f"<code>{ '0x8004BA...9b63' }</code> (ReputationRegistry).</p>"
        "<h2>Endpoints</h2><ul>"
        "<li><code>GET /agent-trust/preview</code> — free demo score</li>"
        f"<li><code>GET /agent-trust?agent=&lt;id&gt;</code> — paid {PRICE} USDC (Base mainnet) — signed JSON</li>"
        "<li><code>GET /health</code> — liveness</li>"
        "<li><code>GET /.well-known/x402</code> — x402 manifest</li>"
        "<li><code>GET /llms.txt</code> — AI-discovery</li>"
        "</ul>"
        f"<p>Methodology: <a href='{METHODOLOGY_URL}'>{METHODOLOGY_URL}</a></p>"
        f"<p><em>{_DISCLAIMER}</em></p>"
    )


@app.route("/health")
def health():
    universe = feedback_universe()
    agents = list_agents()
    return jsonify({
        "status": "ok",
        "registered_agents":   len(agents),
        "agents_with_feedback": len(universe["agents_with_feedback"]),
        "total_feedback":       universe["total_feedback"],
        "latest_block_seen":    universe["latest_block_seen"],
        "signer":               _SIGNER_ADDRESS,
        "now":                  datetime.now(timezone.utc).isoformat(),
    })


@app.route("/llms.txt")
def llms_txt():
    body = (
        "# Agent Trust Oracle\n\n"
        "> Pay-per-call trust scores for ERC-8004 agents on Base mainnet. "
        f"Endpoint /agent-trust?agent=<id> returns a signed score + transparent breakdown for {PRICE} USDC.\n\n"
        "## Endpoints\n"
        "- GET /agent-trust/preview — free demo score (sample agent)\n"
        f"- GET /agent-trust?agent=<id> — paid {PRICE}, returns signed score + breakdown + methodology\n"
        "- GET /health — liveness + universe summary\n"
        "- GET /.well-known/x402 — machine-readable manifest\n\n"
        "## Source data\n"
        "- IdentityRegistry:   0x8004A169FB4a3325136EB29fA0ceB6D2e539a432 (Base mainnet)\n"
        "- ReputationRegistry: 0x8004BAa17C55a88189AE136b182e5fdA19dE9b63 (Base mainnet)\n"
        f"- Methodology: {METHODOLOGY_URL}\n"
    )
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/agent-trust/preview")
def preview():
    s = compute_score(DEMO_AGENT_ID)
    payload = {
        "preview":     True,
        "demo_agent":  DEMO_AGENT_ID,
        "result":      s,
        "note":        f"For any agent id, GET /agent-trust?agent=<id> ({PRICE} USDC on Base mainnet) returns a signed response.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer":   _DISCLAIMER,
    }
    return jsonify(_signed(payload))


@app.route("/methodology")
def methodology():
    """Static HTML describing the exact scoring math, pulled from scoring.py
    constants so it can never drift from the actual implementation."""
    w = WEIGHTS
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
<h1>Agent Trust Oracle &mdash; Methodology (v1)</h1>
<p>Read-only trust scores for <a href="https://eips.ethereum.org/EIPS/eip-8004">ERC-8004</a> agents on Base mainnet. This page is the canonical reference for the scoring math; the values below are pulled live from <code>scoring.py</code> so they can never drift from the implementation.</p>

<h2>Score range</h2>
<p><strong>0 to 100</strong>. Higher = more positive feedback from more independent clients, recently. Or <strong>null</strong> when data is too thin (see refusal threshold below).</p>

<h2>Components &amp; weights</h2>
<p>Each component is computed independently on a 0&ndash;100 scale and then combined with the fixed weights below. <strong>Sum of weights = {sum(w.values()):.2f}.</strong></p>
<table>
<thead><tr><th>Component</th><th>Weight</th><th>What it measures</th></tr></thead>
<tbody>
<tr><td><code>value_avg</code></td><td>{w['value_avg']:.2f}</td><td>Simple arithmetic mean of feedback values, after each value is normalised onto 0&ndash;100.</td></tr>
<tr><td><code>client_breadth</code></td><td>{w['client_breadth']:.2f}</td><td>Log-scaled count of <em>distinct</em> client addresses that have left feedback.</td></tr>
<tr><td><code>volume</code></td><td>{w['volume']:.2f}</td><td>Log-scaled total count of (non-revoked) feedback entries.</td></tr>
<tr><td><code>recency</code></td><td>{w['recency']:.2f}</td><td>Weighted mean of feedback values, with older feedback decaying exponentially.</td></tr>
</tbody></table>

<h2>Refusal threshold (insufficient_data)</h2>
<p>If the agent has fewer than <strong>{MIN_DISTINCT_CLIENTS} distinct clients</strong> leaving feedback, we return <code>score = null</code> with <code>status = "insufficient_data &mdash; fewer than {MIN_DISTINCT_CLIENTS} distinct clients have left feedback"</code>. This prevents one client farming a high score by self-rating and is the single largest sybil-mitigation lever in v1.</p>

<h2>Value normalisation (&minus;100..100 &rarr; 0..100)</h2>
<p>ERC-8004 feedback values are signed decimals with arbitrary scale (e.g. <code>99.77</code>, <code>5.0</code>, <code>-3.0</code>). We clamp to the inclusive range <code>[-100, +100]</code> then linearly remap to <code>[0, 100]</code>:</p>
<pre>normalised_value = (clamp(value, -100, +100) + 100) / 2</pre>

<h2>Log-axis reference points</h2>
<p>Distinct-client breadth and total feedback volume are mapped via <code>log1p</code> to the 0&ndash;100 range. Saturation references:</p>
<ul>
<li><code>CLIENT_BREADTH_REF = {CLIENT_BREADTH_REF}</code> distinct clients &rarr; component saturates at 100.</li>
<li><code>VOLUME_REF = {VOLUME_REF}</code> feedback entries &rarr; component saturates at 100.</li>
</ul>
<pre>def log_axis(count, ref):
    if count &lt;= 0: return 0.0
    return min(100, 100 * log1p(count) / log1p(ref))</pre>

<h2>Recency decay</h2>
<p>Each feedback entry is weighted by an exponential decay on chain-block age:</p>
<pre>recency_weight(block) = 0.5 ** ((latest_block - block) / {RECENCY_HALFLIFE_BLOCKS})</pre>
<p>Half-life is <strong>{RECENCY_HALFLIFE_BLOCKS:,} blocks</strong> (~{RECENCY_HALFLIFE_BLOCKS * 2 / 3600:.1f} hours at Base&rsquo;s 2-second block time). Half of a year-old feedback is roughly worth <code>0.5 ** ({365*24*3600/2/RECENCY_HALFLIFE_BLOCKS:.1f})</code> &asymp; vanishingly small.</p>

<h2>Final aggregate</h2>
<pre>score = sum(component[k] * WEIGHTS[k] for k in WEIGHTS)
      = value_avg * {w['value_avg']:.2f}
      + client_breadth * {w['client_breadth']:.2f}
      + volume * {w['volume']:.2f}
      + recency * {w['recency']:.2f}</pre>

<h2>What we do NOT do (yet)</h2>
<ul>
<li>No client-clustering / address-graph sybil detection.</li>
<li>No tag-based weighting (the <code>indexedTag1</code> &amp; tag1/tag2 strings are passed through in raw feedback but do not bias the score).</li>
<li>No off-chain reputation imports (Twitter, GitHub, etc.).</li>
<li>No agent-supplied URI scraping &mdash; <code>agent_uri</code> on Registered events is surfaced verbatim, never trusted.</li>
</ul>
<p>v2 work-items would tighten the sybil model and add tag-conditional scoring. For now we prefer to refuse cleanly when data is thin (see threshold above).</p>

<h2>Sources (read-only, Base mainnet)</h2>
<ul>
<li><code>IdentityRegistry &nbsp; 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432</code></li>
<li><code>ReputationRegistry 0x8004BAa17C55a88189AE136b182e5fdA19dE9b63</code></li>
<li>Source code: <a href="https://github.com/Nikoble1926/agent-trust-oracle">github.com/Nikoble1926/agent-trust-oracle</a></li>
</ul>

<div class="note"><strong>Signatures.</strong> Every paid <code>/agent-trust</code> response and the free <code>/agent-trust/preview</code> response carry <code>signed_by</code> + <code>signature</code>. Verify with Ethereum <code>personal_sign</code>/<code>ecrecover</code> over <code>json.dumps(payload, sort_keys=True, separators=(",",":"))</code> with the two keys stripped &mdash; <code>recover == signed_by</code> &rArr; authentic &amp; untampered. Signer: <code>{_SIGNER_ADDRESS or '(unset)'}</code>.</div>

<div class="disc"><strong>Disclaimer.</strong> Read-only on-chain analytics. Informational only &mdash; not investment, employment, or counterparty advice. Score reflects observed feedback only; absence of evidence is not evidence of trustworthiness.</div>
</body></html>"""
    return body, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/.well-known/x402")
def x402_manifest():
    return jsonify({
        "x402Version": 2,
        "name":        "Agent Trust Oracle",
        "description": (
            "Pay-per-call ERC-8004 trust scores for AI agents on Base mainnet. "
            "Reads on-chain Identity + Reputation registries, returns transparent score "
            "+ component breakdown + methodology link. ECDSA-signed responses."
        ),
        "signer":      _SIGNER_ADDRESS,
        "signature_scheme": (
            "Every /agent-trust and /agent-trust/preview response carries 'signed_by' "
            "(address) and 'signature'. Verify with Ethereum personal_sign / ecrecover "
            "over json.dumps(payload, sort_keys=True, separators=(',',':')) with the "
            "two keys stripped. recover == signed_by => authentic & untampered."
        ),
        "sources": {
            "chain_id": 8453,
            "chain":    "base-mainnet",
            "identity_registry":   "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
            "reputation_registry": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
        },
        "endpoints": [
            {"path": "/agent-trust", "method": "GET", "price": PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"agent": "ERC-8004 agent id (uint256)"},
             "description": "Signed trust score + breakdown for one agent"},
            {"path": "/agent-trust/preview", "method": "GET", "price": "$0",
             "network": NETWORK, "mimeType": "application/json",
             "description": "Free demo score (sample agent)"},
        ],
        "methodology": METHODOLOGY_URL,
        "disclaimer":  _DISCLAIMER,
    })


# ---------------------------------------------------------------- paid route

@app.route("/agent-trust")
def agent_trust():
    raw = request.args.get("agent", "").strip()
    if not raw.isdigit():
        return jsonify({"error": "bad_request", "message": "query param 'agent' must be a non-negative integer (ERC-8004 agentId)"}), 400
    agent_id = int(raw)
    s = compute_score(agent_id)
    payload = {
        "agent_id":     agent_id,
        "result":       s,
        "sources": {
            "chain":               "base-mainnet",
            "identity_registry":   "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
            "reputation_registry": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
        },
        "methodology":  METHODOLOGY_URL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer":   _DISCLAIMER,
    }
    return jsonify(_signed(payload))


# ---------------------------------------------------------------- middleware

routes = {
    "GET /agent-trust": RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=EVM_ADDRESS, price=PRICE, network=NETWORK)],
        mime_type="application/json",
        description=(
            "ERC-8004 trust score for one agent — signed JSON with transparent "
            "component breakdown (value_avg / client_breadth / volume / recency) "
            "and methodology link. USDC pay-per-call on Base mainnet."
        ),
    ),
}

payment_middleware(app, routes=routes, server=server)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
