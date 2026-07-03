# Agent Trust Oracle MCP

Wire the **Agent Trust Oracle** into **Claude Desktop**, **Cursor**, or any MCP
client as tools. Ask your agent for an ERC-8004 trust score and it calls the
tool; for the paid tool the bridge pays **USDC on Base** automatically via
[x402](https://x402.org) and returns the signed result.

The **`get_trust_preview`** tool is **free and needs no wallet key**. The paid
tool costs a few cents per call and activates only when you set a Base wallet key.

## Quick start (no clone, no build)

Add this to your client config and you're done — `npx` fetches and runs it:

**Claude Desktop** — Settings → Developer → Edit Config:

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

That gives you the **free preview** out of the box. To enable the **paid** tool,
add a low-balance Base wallet key:

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

**Cursor** — Settings → MCP → Add new server, same `command`/`args`/`env`.

Restart the client, then ask: *"Get an agent trust preview"* (free) or
*"Get the trust score for agent 6976 on ethereum"* (paid).

## Tools

| Tool | Cost | Description |
|------|------|-------------|
| `get_trust_preview` | **free** | Example ERC-8004 agent trust score (0–100) with component breakdown, no wallet key required |
| `get_agent_trust` | $0.005 USDC | Signed ERC-8004 trust score (0–100) + component breakdown (client breadth, recency, value, volume) for one agent. Params: `agent` (uint256 id, e.g. 6976), `chain` (`base`/`bsc`/`ethereum`/`mantle`/`polygon`, default `ethereum`) |

## Paid mode (optional)

The paid tool needs an `EVM_PRIVATE_KEY`: a **Base wallet private key** for a
**low-balance, dedicated hot wallet** holding a little **USDC** — never your main
wallet. Payments are gasless for the payer (the facilitator covers gas), so you
mainly need a small USDC balance. Without the key, the paid tool returns a clear
"set EVM_PRIVATE_KEY" message and the free preview keeps working.

`RESOURCE_SERVER_URL` is optional and defaults to `https://trust.nsgoods.org`.

## Run from source (dev)

```bash
cd mcp
npm install
npm run build
node dist/server.js   # or: npm run dev
```

## Security

- The `EVM_PRIVATE_KEY` is **yours** (the buyer's) and stays **local** in your
  client config — it is never sent to the API. Use a throwaway hot wallet with
  only a small USDC balance.
- For spend control, see the policy-check hooks in the
  [official x402 MCP guide](https://docs.x402.org/guides/mcp-server-with-x402)
  and cap per-call / per-session amounts.

## Notes

Mirrors the official x402 MCP client pattern:
https://docs.x402.org/guides/mcp-server-with-x402 . The x402 v2 TypeScript
packages (`@x402/axios`, `@x402/evm`) evolve quickly — if `npm install` or the
build fails, check that guide for current package names/versions.

Trust scores read on-chain ERC-8004 Identity + Reputation registries; every paid
`/agent-trust` and free `/agent-trust/preview` response carries `signed_by` +
`signature` for verification. No accounts, no API keys on the service side.
Educational — not financial advice. MIT licensed.
