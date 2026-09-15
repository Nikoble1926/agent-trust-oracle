### agent-trust-oracle

Read only, pay per call trust scores for ERC-8004 agents, served over the x402 protocol. Every response is a signed, verifiable verdict.

**What it does**

- Returns a signed trust score for an agent, on demand, per call over HTTP 402
- Read only: it never writes on chain, it only scores
- Built for the ERC-8004 agent identity and reputation ecosystem
- Free preview endpoint so you can see the shape before you pay

**How it works**

- Signed with EIP-191; every verdict is canonicalized and independently verifiable
- Settled per call in USDC on Base
- You pay only for a successful `2xx` response

**Live**: [trust.nsgoods.org](https://trust.nsgoods.org)

Part of **nsgoods**, a suite of signed x402 data oracles. Hub: [x402.nsgoods.org](https://x402.nsgoods.org)
