#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import * as z from "zod/v4";
import { ErpGraphStore } from "./erp-graph-store.mjs";
import { ErpApiAgents } from "./erp-api-agents.mjs";

const store = new ErpGraphStore();
const apiAgents = new ErpApiAgents(store);

const server = new McpServer(
  { name: "newagent-1c", version: "4.0.0" },
  {
    instructions:
      "Codex is only the user interface. ERP semantic modeling and authoring must run through run_api_agent, backed by the project API key. Use MCP for graph evidence, project state, approvals and persistence. Record only explicit human decisions. No LLM may set successful.",
  },
);

function result(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    structuredContent: payload,
  };
}

function journaled(tool, handler) {
  return async (input) => {
    const started = performance.now();
    try {
      const response = await handler(input);
      store.recordToolCall(tool, input, response?.structuredContent || {}, Math.round(performance.now() - started));
      return response;
    } catch (error) {
      store.recordToolCall(tool, input, {}, Math.round(performance.now() - started), error?.message || String(error));
      throw error;
    }
  };
}

const rawRegisterTool = server.registerTool.bind(server);
server.registerTool = (name, config, handler) => rawRegisterTool(name, config, journaled(name, handler));

const readOnly = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false,
};

const writeOnly = {
  readOnlyHint: false,
  destructiveHint: false,
  idempotentHint: false,
  openWorldHint: false,
};

server.registerTool(
  "get_model_policy",
  {
    title: "Get ERP agent model policy",
    description: "Return the required runtime provenance fields, allowed ERP skills and their content-addressed versions.",
    inputSchema: {},
    annotations: readOnly,
  },
  async () => result(store.modelPolicy()),
);

server.registerTool(
  "get_api_agent_status",
  {
    title: "Get project API-agent status",
    description: "Check the API-key multi-agent profile without making a paid or remote request. The secret itself is never returned.",
    inputSchema: {},
    annotations: readOnly,
  },
  async () => result(apiAgents.keyStatus()),
);

server.registerTool(
  "run_api_agent",
  {
    title: "Run bounded ERP API agent",
    description:
      "Run one project specialist through the configured API key and persist its artifacts and exact provider usage. Codex must use this tool instead of authoring requirements, solution models or final instructions itself.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      role: z.enum(["erp-translator", "erp-process-planner", "instruction-writer"]),
      task: z.string().max(4_000).default(""),
      query_id: z.string().max(120).default(""),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, query_id: queryId, ...input }) =>
    result(await apiAgents.run({ ...input, projectId, queryId })),
);

server.registerTool(
  "start_agent_run",
  {
    title: "Start journaled ERP agent run",
    description: "Start an observable skill execution and return immutable provenance for all artifacts created by this run.",
    inputSchema: {
      project_id: z.string().max(120).default(""),
      skill: z.enum(["erp-graph-research", "erp-requirements-modeling", "erp-solution-authoring"]),
      provider: z.string().min(1).max(200).describe("Actual provider reported by the MCP host"),
      model: z.string().min(1).max(300).describe("Actual model identifier reported by the MCP host"),
      parameters: z.record(z.string(), z.unknown()).default({}),
      request_summary: z.string().max(4_000).default(""),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, request_summary: requestSummary, ...input }) =>
    result(store.startAgentRun({ ...input, projectId, requestSummary })),
);

server.registerTool(
  "list_agent_runs",
  {
    title: "List ERP agent runs",
    description: "List journaled runs with active skill, model, selected nodes, artifacts and current project revision.",
    inputSchema: {
      project_id: z.string().max(120).default(""),
      limit: z.number().int().min(1).max(200).default(50),
    },
    annotations: readOnly,
  },
  async ({ project_id: projectId, limit }) => result({ runs: store.listAgentRuns({ projectId, limit }) }),
);

server.registerTool(
  "get_agent_run",
  {
    title: "Get ERP agent execution journal",
    description: "Read the run summary and recent MCP calls, selected nodes, artifacts and revisions.",
    inputSchema: {
      run_id: z.string().min(1).max(120),
      event_limit: z.number().int().min(1).max(2_000).default(200),
    },
    annotations: readOnly,
  },
  async ({ run_id: runId, event_limit: eventLimit }) => result(store.getAgentRun(runId, eventLimit)),
);

server.registerTool(
  "finish_agent_run",
  {
    title: "Finish ERP agent run",
    description: "Close a journaled skill execution with an explicit outcome.",
    inputSchema: {
      run_id: z.string().min(1).max(120),
      status: z.enum(["completed", "needs_input", "failed"]).default("completed"),
      summary: z.string().max(8_000).default(""),
    },
    annotations: writeOnly,
  },
  async ({ run_id: runId, ...input }) => result(store.finishAgentRun(runId, input)),
);

server.registerTool(
  "graph_status",
  {
    title: "ERP graph status",
    description: "Check whether the read-optimized ERP graph sidecar exists and matches the published graph.",
    inputSchema: {},
    annotations: readOnly,
  },
  async () => result(store.status()),
);

server.registerTool(
  "search_nodes",
  {
    title: "Search ERP graph nodes",
    description:
      "Search the published four-layer ERP graph with FTS5 plus sparse semantic retrieval and a deterministic lightweight reranker. Run several narrow searches; ranks are candidate signals, not business decisions.",
    inputSchema: {
      query: z.string().min(2).max(1_000).describe("Focused Russian or metadata search phrase"),
      layers: z.array(z.number().int().min(1).max(4)).max(4).default([]).describe("Optional layers: 1 scenarios, 2 clarifications, 3 metadata/UI, 4 knowledge"),
      node_types: z.array(z.string().min(1).max(80)).max(20).default([]),
      limit: z.number().int().min(1).max(50).default(12),
      strategy: z.enum(["any", "all"]).default("any").describe("any is recall-oriented; all is precision-oriented"),
      mode: z.enum(["hybrid", "fts", "semantic"]).default("hybrid"),
    },
    annotations: readOnly,
  },
  async ({ query, layers, node_types: nodeTypes, limit, strategy, mode }) =>
    result(store.search({ query, layers, nodeTypes, limit, strategy, mode })),
);

server.registerTool(
  "get_nodes",
  {
    title: "Get ERP graph nodes",
    description: "Fetch exact node metadata and previews for known graph IDs without expanding neighbors.",
    inputSchema: {
      ids: z.array(z.string().min(1).max(1_000)).min(1).max(50),
      preview_chars: z.number().int().min(200).max(8_000).default(1_500),
    },
    annotations: readOnly,
  },
  async ({ ids, preview_chars: previewChars }) => result({ nodes: store.getNodes(ids, previewChars) }),
);

server.registerTool(
  "expand_graph",
  {
    title: "Expand ERP graph",
    description:
      "Traverse typed graph edges from selected seeds. Use relations to constrain expansion and keep the evidence set compact.",
    inputSchema: {
      seeds: z.array(z.string().min(1).max(1_000)).min(1).max(20),
      direction: z.enum(["out", "in", "both"]).default("both"),
      relations: z.array(z.string().min(1).max(100)).max(30).default([]),
      depth: z.number().int().min(1).max(4).default(1),
      limit: z.number().int().min(2).max(500).default(120),
    },
    annotations: readOnly,
  },
  async (input) => result(store.expand(input)),
);

server.registerTool(
  "find_paths",
  {
    title: "Find ERP graph paths",
    description: "Find short typed paths between two known nodes for document chains, dependencies and traceability.",
    inputSchema: {
      start: z.string().min(1).max(1_000),
      end: z.string().min(1).max(1_000),
      direction: z.enum(["out", "in", "both"]).default("out"),
      relations: z.array(z.string().min(1).max(100)).max(30).default([]),
      max_depth: z.number().int().min(1).max(10).default(6),
      max_paths: z.number().int().min(1).max(20).default(5),
    },
    annotations: readOnly,
  },
  async ({ max_depth: maxDepth, max_paths: maxPaths, ...input }) =>
    result(store.findPaths({ ...input, maxDepth, maxPaths })),
);

server.registerTool(
  "read_source",
  {
    title: "Read ERP node source",
    description:
      "Read the source Markdown/XML for a selected node. Prefer a query to return bounded evidence snippets instead of loading a whole document.",
    inputSchema: {
      node_id: z.string().min(1).max(1_000),
      query: z.string().max(1_000).default(""),
      offset: z.number().int().min(0).default(0),
      max_chars: z.number().int().min(500).max(40_000).default(16_000),
    },
    annotations: readOnly,
  },
  async ({ node_id: nodeId, max_chars: maxChars, ...input }) =>
    result(store.readSource({ ...input, nodeId, maxChars })),
);

server.registerTool(
  "list_projects",
  {
    title: "List skill-driven ERP projects",
    description: "List projects managed by the skill/MCP architecture. Legacy Python projects remain untouched.",
    inputSchema: { limit: z.number().int().min(1).max(200).default(50) },
    annotations: readOnly,
  },
  async ({ limit }) => result({ projects: store.listProjects(limit) }),
);

server.registerTool(
  "create_project",
  {
    title: "Create ERP project",
    description:
      "Persist raw requirements for LLM-led modeling. This does not normalize, map or plan them; those reasoning steps belong to the requirements-modeling skill.",
    inputSchema: {
      title: z.string().min(1).max(300),
      requirements: z.string().min(1).max(500_000),
      project_id: z.string().max(120).default(""),
      source: z.string().max(2_000).default(""),
      product: z.string().min(1).max(300).default("1C:ERP"),
      release: z.string().max(100).default(""),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, ...input }) => result(store.createProject({ ...input, projectId })),
);

server.registerTool(
  "get_project",
  {
    title: "Get ERP project state",
    description: "Load the raw specification, recorded decisions, active context, approvals and saved artifacts.",
    inputSchema: { project_id: z.string().min(1).max(120) },
    annotations: readOnly,
  },
  async ({ project_id: projectId }) => result(store.getProject(projectId)),
);

server.registerTool(
  "start_project_query",
  {
    title: "Start metered project question",
    description:
      "Start local telemetry for one user question about an existing project. Call before graph, modeling or authoring tools.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      question: z.string().min(1).max(100_000),
      metadata: z.record(z.string(), z.unknown()).default({}),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, ...input }) => result(store.startProjectQuery(projectId, input)),
);

server.registerTool(
  "record_model_call_telemetry",
  {
    title: "Record provider model usage",
    description:
      "Record one model call using exact usage returned by an API provider. Pass null when the host does not expose a counter; never estimate tokens.",
    inputSchema: {
      project_id: z.string().max(120).default(""),
      query_id: z.string().max(120).default(""),
      provider: z.string().min(1).max(200),
      model: z.string().min(1).max(300),
      reasoning_effort: z.string().max(100).default(""),
      skill_version: z.string().max(200).default(""),
      application_version: z.string().max(100).default(""),
      graph_version: z.string().max(200).default(""),
      graph_hash: z.string().max(300).default(""),
      input_tokens: z.number().int().min(0).nullable().default(null),
      cached_input_tokens: z.number().int().min(0).nullable().default(null),
      output_tokens: z.number().int().min(0).nullable().default(null),
      reasoning_tokens: z.number().int().min(0).nullable().default(null),
      duration_ms: z.number().int().min(0),
      attempt: z.number().int().min(1).max(100).default(1),
      result: z.enum(["completed", "failed", "timeout", "cancelled"]).default("completed"),
      error: z.string().max(4_000).default(""),
      usage_source: z.string().max(100).default("provider_response"),
    },
    annotations: writeOnly,
  },
  async ({
    project_id: projectId, query_id: queryId, reasoning_effort: reasoningEffort,
    skill_version: skillVersion, application_version: applicationVersion,
    graph_version: graphVersion, graph_hash: graphHash, input_tokens: inputTokens,
    cached_input_tokens: cachedInputTokens, output_tokens: outputTokens,
    reasoning_tokens: reasoningTokens, duration_ms: durationMs, usage_source: usageSource, ...input
  }) => result(store.recordModelCallTelemetry({
    ...input, projectId, queryId, reasoningEffort, skillVersion, applicationVersion,
    graphVersion, graphHash, inputTokens, cachedInputTokens, outputTokens, reasoningTokens,
    durationMs, usageSource,
  })),
);

server.registerTool(
  "finish_project_query",
  {
    title: "Finish metered project question",
    description:
      "Finish one project question and write both the per-question record and aggregate analysis/telemetry-report.json.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      query_id: z.string().min(1).max(120),
      status: z.enum(["completed", "failed", "needs_input", "cancelled"]).default("completed"),
      answer_path: z.string().max(1_000).default(""),
      error: z.string().max(8_000).default(""),
      metadata: z.record(z.string(), z.unknown()).default({}),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, query_id: queryId, answer_path: answerPath, ...input }) =>
    result(store.finishProjectQuery(projectId, queryId, { ...input, answerPath })),
);

server.registerTool(
  "get_project_telemetry",
  {
    title: "Get project question telemetry",
    description:
      "Return aggregate question counts, exact provider token usage, wall/model/MCP time and recent question records.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      limit: z.number().int().min(1).max(500).default(100),
    },
    annotations: readOnly,
  },
  async ({ project_id: projectId, limit }) => result(store.aggregateProjectTelemetry(projectId, limit)),
);

server.registerTool(
  "read_project_artifact",
  {
    title: "Read ERP project artifact",
    description: "Read the latest artifact of a kind, or a specific artifact path returned by get_project.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      kind: z.string().max(120).default(""),
      artifact_path: z.string().max(1_000).default(""),
      max_chars: z.number().int().min(500).max(500_000).default(100_000),
    },
    annotations: readOnly,
  },
  async ({ project_id: projectId, artifact_path: artifactPath, max_chars: maxChars, ...input }) =>
    result(store.readArtifact(projectId, { ...input, artifactPath, maxChars })),
);

server.registerTool(
  "record_decisions",
  {
    title: "Record ERP project decisions",
    description:
      "Version explicit human answers to modeling questions. Do not use this tool for assumptions made by the model.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      decisions: z.array(z.object({
        id: z.string().max(120).optional(),
        question_id: z.string().max(120).optional(),
        question: z.string().min(1).max(4_000),
        answer: z.string().min(1).max(20_000),
        normalized_value: z.string().max(20_000).default(""),
        allowed_values: z.array(z.string().min(1).max(2_000)).max(30).default([]),
        rationale: z.string().max(20_000).default(""),
        affected_requirement_ids: z.array(z.string().min(1).max(120)).max(500).default([]),
        affected_node_ids: z.array(z.string().min(1).max(1_000)).max(100).default([]),
      })).min(1).max(100),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, decisions }) => result(store.recordDecisions(projectId, decisions)),
);

server.registerTool(
  "add_project_context",
  {
    title: "Add ERP project context",
    description: "Add a versioned supplement or correction without deleting project history.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      mode: z.enum(["supplement", "correction"]),
      text: z.string().min(1).max(200_000),
      supersedes_ids: z.array(z.string().min(1).max(120)).max(100).default([]),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, supersedes_ids: supersedesIds, ...input }) =>
    result(store.addContext(projectId, { ...input, supersedesIds })),
);

server.registerTool(
  "record_project_approval",
  {
    title: "Approve an ERP project lifecycle stage",
    description:
      "Record the user's explicit requirements, design, or final approval. Only final approval after deterministic validation can set successful.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      approved: z.boolean(),
      stage: z.enum(["requirements", "design", "final"]).default("design"),
      note: z.string().max(20_000).default(""),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, ...input }) => result(store.recordApproval(projectId, input)),
);

server.registerTool(
  "save_artifact",
  {
    title: "Save ERP project artifact",
    description:
      "Persist a modeling artifact or Markdown answer. Answers are written to answers_md after design approval and remain unconfirmed until final approval.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      kind: z.enum(["requirement-map", "evidence-map", "questions", "decision-register", "solution-model", "gap-register", "traceability", "acceptance-tests", "quality-gate", "telemetry-report", "answer"]),
      title: z.string().max(300).default(""),
      content: z.union([z.string().max(1_000_000), z.record(z.string(), z.unknown()), z.array(z.unknown())]),
      metadata: z.record(z.string(), z.unknown()).default({}),
      provenance: z.object({
        provider: z.string().min(1).max(200),
        model: z.string().min(1).max(300),
        parameters: z.record(z.string(), z.unknown()),
        skill: z.enum(["erp-graph-research", "erp-requirements-modeling", "erp-solution-authoring"]),
        skill_version: z.string().min(1).max(200),
        run_id: z.string().min(1).max(120),
        policy_id: z.string().max(200).optional(),
      }),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, ...input }) => result(store.saveArtifact(projectId, input)),
);

server.registerTool(
  "validate_project",
  {
    title: "Deterministically validate ERP project",
    description:
      "Run schema, traceability, coverage, evidence and critical-GAP checks without an LLM. Writes analysis/modeler-report.json.",
    inputSchema: {
      project_id: z.string().min(1).max(120),
      for_final: z.boolean().default(false),
    },
    annotations: writeOnly,
  },
  async ({ project_id: projectId, for_final: forFinal }) =>
    result(store.validateProject(projectId, { forFinal, persist: true })),
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("ERP Graph MCP server ready on stdio");
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, async () => {
    store.close();
    await server.close();
    process.exit(0);
  });
}

main().catch((error) => {
  console.error("ERP Graph MCP server failed:", error);
  process.exit(1);
});
