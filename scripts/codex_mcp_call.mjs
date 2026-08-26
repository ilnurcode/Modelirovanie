#!/usr/bin/env node

import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const [toolName, rawArguments = "{}"] = process.argv.slice(2);
if (!toolName) {
  throw new Error("Usage: node scripts/codex_mcp_call.mjs <tool> '<json>'");
}

const serverPath = path.resolve("mcp/erp-graph-server.mjs");
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [serverPath],
  env: {
    ...process.env,
    ERP_GRAPH_DATABASE: path.resolve("../RAGAgent/graph_rag_data/erp_graph_mcp.sqlite"),
    ERP_GRAPH_SOURCE_ROOT: path.resolve("../RAGAgent"),
    ERP_PROJECT_ROOT: path.resolve("results"),
  },
  stderr: "pipe",
});
const client = new Client({ name: "codex-workspace-client", version: "1.0.0" });

await client.connect(transport);
try {
  const response = await client.callTool({
    name: toolName,
    arguments: JSON.parse(rawArguments),
  });
  process.stdout.write(`${JSON.stringify(response.structuredContent ?? response, null, 2)}\n`);
} finally {
  await client.close();
}
