import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { ErpApiAgents, readDotEnvValue, resolveApiKey } from "../../mcp/erp-api-agents.mjs";

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "newagent-api-"));
  fs.mkdirSync(path.join(root, "mcp", "prompts"), { recursive: true });
  fs.writeFileSync(path.join(root, "mcp", "prompts", "erp-translator.md"), "system", "utf8");
  fs.writeFileSync(path.join(root, "agent-runtime-policy.json"), JSON.stringify({
    provider: "wormsoft-gateway",
    api_key_precedence: ["WORMSOFT_API_KEY"],
    wormsoft_base_url: "https://example.invalid/api/gpt",
    allowed_agents: ["erp-translator"],
    max_subagent_calls_per_revision: 3,
    models_by_agent: { "erp-translator": "wormsoft/agent/low" },
  }), "utf8");
  const events = { saved: [], telemetry: [], finishedQueries: [] };
  const project = { project_id: "p1", title: "Тест", revision: 1, specification: { text: "Закупка товара" }, contexts: [], approval_state: {} };
  const provenance = { provider: "wormsoft-gateway", model: "wormsoft/agent/low", parameters: {}, skill: "erp-requirements-modeling", skill_version: "sha256:test", run_id: "run-1" };
  const store = {
    root,
    graphSourceRoot: "",
    activeProjectQuery: null,
    getProject: () => project,
    readArtifact: () => { throw new Error("missing"); },
    search: () => ({ results: [{ id: "node-1", title: "Закупка", source_ref: "knowledge/a.md" }] }),
    listAgentRuns: () => [],
    startProjectQuery: () => ({ query_id: "query-1" }),
    startAgentRun: ({ parameters }) => ({ run: { run_id: "run-1" }, provenance: { ...provenance, parameters } }),
    recordModelCallTelemetry: (value) => events.telemetry.push(value),
    saveArtifact: (_projectId, value) => { const artifact = { kind: value.kind, path: `agent_artifacts/${value.kind}.json` }; events.saved.push(value); return { artifact }; },
    finishAgentRun: () => {},
    finishProjectQuery: (...args) => events.finishedQueries.push(args),
  };
  return { root, store, events };
}

test("dotenv parser and key resolution do not require inherited environment", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "newagent-env-"));
  fs.writeFileSync(path.join(root, ".env"), 'WORMSOFT_API_KEY="project-secret"\n', "utf8");
  assert.equal(readDotEnvValue('WORMSOFT_API_KEY="quoted-secret"\n', "WORMSOFT_API_KEY"), "quoted-secret");
  assert.deepEqual(resolveApiKey([root], ["WORMSOFT_API_KEY"], {}), { name: "WORMSOFT_API_KEY", value: "project-secret", source: path.join(root, ".env") });
});

test("translator uses API model, persists all artifacts and exact usage", async () => {
  const { root, store, events } = fixture();
  const responsePayload = {
    choices: [{ message: { content: JSON.stringify({ artifacts: {
      "requirement-map": { schema_version: 4, requirements: [] },
      "evidence-map": { schema_version: 4, evidence: [] },
      questions: { schema_version: 4, questions: [] },
    } }) } }],
    usage: { prompt_tokens: 120, completion_tokens: 45, prompt_tokens_details: { cached_tokens: 20 }, completion_tokens_details: { reasoning_tokens: 5 } },
  };
  const calls = [];
  const agents = new ErpApiAgents(store, {
    root,
    env: { WORMSOFT_API_KEY: "secret" },
    fetchImpl: async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => responsePayload }; },
  });
  const result = await agents.run({ projectId: "p1", role: "erp-translator", task: "Разобрать ТЗ" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://example.invalid/api/gpt/chat/completions");
  assert.equal(JSON.parse(calls[0].options.body).model, "wormsoft/agent/low");
  assert.deepEqual(events.saved.map((item) => item.kind), ["requirement-map", "evidence-map", "questions"]);
  assert.equal(events.telemetry[0].inputTokens, 120);
  assert.equal(events.telemetry[0].outputTokens, 45);
  assert.equal(result.provider, "wormsoft-gateway");
  assert.equal(events.finishedQueries.length, 1);
});
