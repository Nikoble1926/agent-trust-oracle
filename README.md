# Agent Trust Oracle

> Read-only, pay-per-call trust scores for [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) agents across multiple EVM chains (Ethereum-primary) — served over [x402](https://github.com/coinbase/x402) with ECDSA-signed responses.

**Live service:** [https://trust.nsgoods.org](https://trust.nsgoods.org) · **Methodology:** [https://trust.nsgoods.org/methodology](https://trust.nsgoods.org/methodology)

The service indexes the canonical ERC-8004 IdentityRegistry + ReputationRegistry on every supported chain — currently **ethereum, base, polygon, bsc, mantle** — computes a transparent trust score from the on-chain feedback, and serves it through an x402 paywall ($0.005 USDC per call on Base mainnet). Every paid response is signed; verifying clients can ecrecover and prove authenticity. Default chain is `ethereum` (where ~99% of agent activity actually lives in mid-2026). Identity is per-chain — we do not merge agent IDs across chains.

---

## Build an agent on this API

Starter template (free preview → paid pay-per-call over x402): https://github.com/Nikoble1926/agent-starter-x402

## What it actually does

1. **Indexer** (`indexer.py`) scans `Registered`, `NewFeedback`, and `FeedbackRevoked` events from the two canonical registries on every wired chain in 9,500-block chunks, rotating across multiple free RPCs. Idempotent per chain: each chain has its own `data/<chain>/state.json` and resumes from there.

2. **Scoring** (`scoring.py`) combines four components into a 0–100 score, keyed on `(chain, agent_id)`:

   | Component | Weight | Measures |
   |---|---|---|
   | `value_avg` | 0.50 | Mean of feedback values, normalised −100..100 → 0..100 |
   | `client_breadth` | 0.20 | Log-scaled count of distinct client addresses |
   | `volume` | 0.15 | Log-scaled count of (non-revoked) feedback entries |
   | `recency` | 0.15 | Recency-weighted mean of values (~28h half-life on Base block time) |

   **Refusal threshold**: if fewer than 3 distinct clients have left feedback for the agent, the score is `null` with `status="insufficient_data"`. This is the single largest sybil-mitigation lever in v1 — we'd rather say "we don't know" than fabricate a score from one client.

3. **API** (`app.py`) is a Flask service behind the x402 payment middleware. Endpoints:

   | Endpoint | Price | Behaviour |
   |---|---|---|
   | `GET /` | free | landing |
   | `GET /health` | free | per-chain summary (agents / feedback / latest block) |
   | `GET /methodology` | free | full scoring math (pulled live from `scoring.py`) |
   | `GET /llms.txt` | free | AI-discovery feed |
   | `GET /.well-known/x402` | free | x402 manifest with `chain` param schema |
   | `GET /provable/head` + `/provable/verify` | free | tamper-evident snapshot chain |
   | `GET /agent-trust/preview` | free | demo score (a real Ethereum agent with sufficient data) |
   | `GET /agent-trust?agent=<id>&chain=<name>` | **$0.005 USDC (Base mainnet)** | signed JSON: per-(chain, agent) score + breakdown + sources + methodology link. `chain` defaults to `ethereum`. |

## Example response

```bash
curl -s https://trust.nsgoods.org/agent-trust/preview | jq
```

```jsonc
{
  "preview": true,
  "demo_agent": 25975,
  "result": {
    "agent_id": 25975,
    "score": 65.57,
    "status": "ok",
    "components": {
      "value_avg":      50.50,
      "client_breadth": 88.71,
      "volume":        100.00,
      "recency":        50.50
    },
    "inputs": {
      "feedback_count":   1403,
      "distinct_clients": 17,
      "revoked_count":    0,
      "min_distinct_clients_required": 3,
      "latest_block_seen": 47419930
    },
    "methodology": "https://trust.nsgoods.org/methodology",
    "weights": {"value_avg": 0.5, "client_breadth": 0.2, "volume": 0.15, "recency": 0.15}
  },
  "signed_by": "0x5e63d01d6A266BC17f577B80199a2a07B15053C7",
  "signature": "0x..."
}
```

## Verifying a signed response

```python
import json
from eth_account import Account
from eth_account.messages import encode_defunct

payload = json.loads(response_body)
sig     = payload.pop("signature")
claimed = payload.pop("signed_by")
canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
recovered = Account.recover_message(encode_defunct(text=canonical), signature=sig)
assert recovered.lower() == claimed.lower()    # authentic & untampered
```

## Sources (read-only, canonical on every chain)

- `IdentityRegistry`   `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`
- `ReputationRegistry` `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63`
- Event topics in `indexer.py` (Registered, NewFeedback, FeedbackRevoked)
- Wired chain ids: 1 (ethereum), 8453 (base), 137 (polygon), 56 (bsc), 5000 (mantle)

### Why Ethereum-primary?

A probe on 2026-06-16 found agent counts of approximately **34,800 (ethereum), 17 (base), 3 (bsc), 0 (polygon), 0 (mantle)**. Defaulting `chain=ethereum` matches where the actual ERC-8004 market currently lives.

### Cross-chain identity is per-chain

We do **not** merge agent IDs across chains. The probe found **zero owner wallets** registered on more than one chain, so an "operator view" (merge by owner wallet) has no signal yet. We'll add it once that changes.

## Running locally

```bash
git clone https://github.com/Nikoble1926/agent-trust-oracle
cd agent-trust-oracle
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then fill in real values
python3 indexer.py         # one-shot scan; idempotent
python3 app.py             # Flask dev server on :4023
```

## Deploying to a VPS

Example unit files are in the repo:

- `trust-oracle.service.example` — gunicorn web service
- `trust-oracle-indexer.service.example` + `trust-oracle-indexer.timer.example` — hourly oneshot indexer
- `nginx.conf.example` — TLS termination + reverse proxy to `127.0.0.1:4023` (use with `certbot --nginx`)

## What this is NOT

- **Not financial advice.** Read-only on-chain analytics.
- **Not a security audit of any agent.** Score reflects observed feedback, nothing more. Absence of evidence is not evidence of trustworthiness.
- **Not a sybil oracle (yet).** v1 uses a "≥3 distinct clients" floor as the sole sybil heuristic. Client-graph clustering is a v2 work-item.

## License

[MIT](./LICENSE) — © 2026 Nikoble1926.
