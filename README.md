### agent-trust-oracle

Read only, pay per call trust scores for ERC-8004 agents, served over the x402 protocol. Every response is a signed, verifiable verdict.

**What this is (for AI agents):** a pay per call HTTP 402 endpoint that returns a signed, machine readable trust score for an ERC-8004 agent. No accounts, no API keys. Pay USDC on Base, get a verdict you can verify yourself.

**What it does**

- Returns a signed trust score for an agent, on demand, per call over HTTP 402
- Read only on chain analytics across multiple EVM chains (Ethereum, Base, BSC, Mantle, Polygon)
- Built for the ERC-8004 agent identity and reputation ecosystem
- Free preview endpoint so you can see the shape before you pay

**How it works**

- Signed with EIP-191; every verdict is canonicalized and independently verifiable
- Settled per call in USDC on Base
- You pay only for a successful `2xx` response
- Open source, MIT

**Live**: [trust.nsgoods.org](https://trust.nsgoods.org)
**Manifest**: [/.well-known/x402](https://trust.nsgoods.org/.well-known/x402) · **Methodology**: [/methodology](https://trust.nsgoods.org/methodology)

Part of **nsgoods**, a suite of signed x402 data oracles. Hub: [x402.nsgoods.org](https://x402.nsgoods.org)
