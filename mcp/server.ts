#!/usr/bin/env node
/**
 * Agent Trust Oracle — MCP server (x402 buyer bridge)
 * Exposes the live x402 API at https://trust.nsgoods.org as MCP tools.
 * Free preview needs no wallet key; the paid tool activates only when EVM_PRIVATE_KEY is set.
 * Mirrors the official x402 MCP client pattern: https://docs.x402.org/guides/mcp-server-with-x402
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import type { AxiosInstance } from "axios";
import axios from "axios";
import { x402Client, wrapAxiosWithPayment } from "@x402/axios";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";
import { config } from "dotenv";

config();

const evmPrivateKey = process.env.EVM_PRIVATE_KEY as `0x${string}` | undefined;
const baseURL = process.env.RESOURCE_SERVER_URL || "https://trust.nsgoods.org";

const plain: AxiosInstance = axios.create({ baseURL });
let api: AxiosInstance = plain;
let paidEnabled = false;
if (evmPrivateKey) {
  const client = new x402Client();
  client.register("eip155:*", new ExactEvmScheme(privateKeyToAccount(evmPrivateKey)));
  api = wrapAxiosWithPayment(axios.create({ baseURL }), client);
  paidEnabled = true;
}

const ok = (data: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(data) }] });
const needKey = () => ({
  content: [{ type: "text" as const, text: JSON.stringify({
    error: "payment_not_configured",
    message: "This is a paid tool. Set EVM_PRIVATE_KEY (a low-balance Base wallet with a little USDC) to enable USDC-on-Base payments. The free get_trust_preview tool works without a key.",
  }) }],
  isError: true,
});

const server = new McpServer({ name: "Agent Trust Oracle", version: "1.0.0" });

server.tool(
  "get_trust_preview",
  "FREE demo: an example ERC-8004 agent trust score (0-100) with component breakdown, no payment, no wallet key required.",
  {},
  async () => {
    const r = await plain.get("/agent-trust/preview");
    return ok(r.data);
  }
);

server.tool(
  "get_agent_trust",
  "Paid ($0.005 USDC): signed ERC-8004 trust score (0-100) + component breakdown (client breadth, recency, value, volume) for one agent on a chosen chain. Reads on-chain Identity + Reputation registries.",
  {
    agent: z.union([z.number(), z.string()]).describe("ERC-8004 agent id (uint256), e.g. 6976"),
    chain: z.enum(["base","bsc","ethereum","mantle","polygon"]).default("ethereum").describe("Chain to read the agent on (default ethereum)"),
  },
  async ({ agent, chain }) => {
    if (!paidEnabled) return needKey();
    const r = await api.get("/agent-trust", { params: { agent: String(agent), chain } });
    return ok(r.data);
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
