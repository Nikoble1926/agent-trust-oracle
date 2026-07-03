# Agent Trust Oracle — x402 pay-per-call API

Pay-per-call **[ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) trust scores** for AI agents, served over the [x402](https://x402.org) protocol on **Base mainnet**. An agent calls an endpoint, receives HTTP 402, pays USDC, and gets a machine-readable, **ECDSA-signed** trust score — no accounts, no API keys. Read-only on-chain analytics across **5 EVM chains** (Ethereum, Base, BSC, Mantle, Polygon). Open-source, MIT.

**Live:** https://trust.nsgoods.org · **Manifest:** https://trust.nsgoods.org/.well-known/x402 · **Methodology:** https://trust.nsgoods.org/methodology

## Why this API
- **Signed & verifiable** — every response carries `signed_by` + `signature`; recover against the published signer to prove the score is authentic and untampered, without paying.
- **Tamper-evident** — a public sha256 hash-chain (`/provable/verify`, `/provable/head`) is mirrored to GitHub (`provable/scores_chain.jsonl`), so every indexer run is committed on-chain-of-custody and anyone can audit history.
- **Straight from canonical registries** — scores are computed only from the canonical ERC-8004 Identity + Reputation registries; nothing is editorial or off-chain.
- **No keys, no signup, free preview** — try `/agent-trust/preview` for free, then pay per call in USDC on Base.

## Endpoints
| Path | Cost | Description |
|------|------|-------------|
| `GET /agent-trust?agent=<id>&chain=<name>` | $0.005 USDC | Signed trust score (0–100) + component breakdown + sources for one ERC-8004 agent. `agent` = ERC-8004 `uint256` id; `chain` ∈ `base`/`bsc`/`ethereum`/`mantle`/`polygon` (default `ethereum`) |
| `GET /agent-trust/preview` | free | Free demo score for a real agent with sufficient data |
| `GET /provable/verify` | free | Verify the tamper-evident hash-chain |
| `GET /provable/head` | free | Latest hash-chain head |
| `GET /methodology` | free | Full scoring math (pulled live from `scoring.py`) |
| `GET /health` | free | Per-chain summary (agents / feedback / latest block) |
| `GET /.well-known/x402` | free | x402 service manifest with `chain` param schema |
| `GET /` | free | Human-readable landing page |

Paid endpoint settles in USDC on Base mainnet (eip155:8453). Default chain is `ethereum`, where ~99% of ERC-8004 agent activity currently lives. Identity is per-chain — agent IDs are not merged across chains.

## Quickstart (call & pay in ~5 minutes)
Start free — no wallet, no funds:

```bash
curl -s https://trust.nsgoods.org/agent-trust/preview | jq
```

A runnable demo agent lives in the [agent-starter-x402](https://github.com/Nikoble1926/agent-starter-x402) template: it calls the FREE preview and prints the decoded x402 `402 Payment Required` challenge for the paid endpoint — no funds needed. Add a low-balance Base hot-wallet key and the same call auto-pays $0.005 USDC on Base for the signed score.

## Verify a signed response
Every response carries `signed_by` + `signature`: an EIP-191 `personal_sign` over the JSON body (canonical `json.dumps(sort_keys=True, separators=(",",":"))`) **before** those two keys are added. Anyone can recover the signing address and confirm it equals the published signer `0x5e63d01d6A266BC17f577B80199a2a07B15053C7` — proving the score is authentic and untampered, **without paying**:

```python
import json
from eth_account import Account
from eth_account.messages import encode_defunct

payload    = json.loads(response_body)
sig        = payload.pop("signature")
claimed    = payload.pop("signed_by")
canonical  = json.dumps(payload, sort_keys=True, separators=(",", ":"))
recovered  = Account.recover_message(encode_defunct(text=canonical), signature=sig)
assert recovered.lower() == claimed.lower()    # authentic & untampered
```

Run it against the FREE `/agent-trust/preview` endpoint — no funds required.

### Tamper-evidence & sources
Each indexer run appends a sha256-chained snapshot to `data/scores_chain.jsonl`, mirrored to GitHub at [`provable/scores_chain.jsonl`](provable/scores_chain.jsonl) so every push is a public commit-of-record. Verify the chain live at [`/provable/verify`](https://trust.nsgoods.org/provable/verify). Scores derive only from the canonical read-only registries:

- `IdentityRegistry`   `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`
- `ReputationRegistry` `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63`
- Wired chain ids: 1 (ethereum), 8453 (base), 137 (polygon), 56 (bsc), 5000 (mantle)

## Discovery
Listed on **[x402scan](https://www.x402scan.com)** (as *Agent Trust Oracle*) and the **[x402 Bazaar](https://docs.cdp.coinbase.com/x402/bazaar)** (CDP Facilitator) — agents can find this API via Bazaar semantic search / merchant lookup. Settlement runs through the [Coinbase CDP Facilitator](https://docs.cdp.coinbase.com/x402).

## Use with Claude Desktop / Cursor (MCP)
Wire the oracle into **Claude Desktop**, **Cursor**, or any MCP client as tools — no clone, no build. Add this to your client config and `npx` fetches and runs it:

```json
{
  "mcpServers": {
    "agent-trust": {
      "command": "npx",
      "args": ["-y", "@nikosble1926/agent-trust-mcp"]
    }
  }
}
```

That gives you the **free preview** out of the box. To enable the **paid** tool, add a low-balance Base wallet key:

```json
{
  "mcpServers": {
    "agent-trust": {
      "command": "npx",
      "args": ["-y", "@nikosble1926/agent-trust-mcp"],
      "env": {
        "EVM_PRIVATE_KEY": "0x<your-low-balance-base-wallet-private-key>"
      }
    }
  }
}
```

Restart the client, then ask: *"Get an agent trust preview"* (free) or *"Get the trust score for agent 6976 on ethereum"* (paid).

| Tool | Cost | Description |
|------|------|-------------|
| `get_trust_preview` | **free** | Example ERC-8004 agent trust score (0–100) + component breakdown, no wallet key required |
| `get_agent_trust` | $0.005 USDC | Signed trust score for one agent. Params: `agent` (uint256 id), `chain` (`base`/`bsc`/`ethereum`/`mantle`/`polygon`, default `ethereum`) |

The `EVM_PRIVATE_KEY` is **yours** and stays **local** in your client config — it is never sent to the API. See [`mcp/`](./mcp) for full setup.

## Run
```bash
pip install -r requirements.txt
cp .env.example .env       # then fill in real values
python3 indexer.py         # one-shot scan; idempotent
python3 app.py             # Flask dev server on :4023
```

## Security
Only your **public** Base receiving address goes in `.env` on the service side. Never put a private key or seed phrase in this repo. For MCP paid mode, use a throwaway hot wallet holding only a little USDC.

## Disclaimer
Read-only on-chain analytics, informational only. **Not** investment, employment, or counterparty advice — a score reflects observed ERC-8004 feedback and nothing more. Absence of evidence is not evidence of trustworthiness. Not a security audit of any agent.

## License
[MIT](./LICENSE) — © 2026 Nikoble1926.
