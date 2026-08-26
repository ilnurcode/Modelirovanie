import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test, { after, before } from "node:test";
import { DatabaseSync } from "node:sqlite";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { ErpGraphStore } from "../../mcp/erp-graph-store.mjs";

let root;
let databasePath;

function buildFixture(database) {
  const connection = new DatabaseSync(database);
  connection.exec(`
    CREATE TABLE nodes (
      node_pk INTEGER PRIMARY KEY,
      id TEXT NOT NULL UNIQUE,
      canonical_id TEXT NOT NULL UNIQUE,
      id_schema_version INTEGER NOT NULL,
      content_version TEXT NOT NULL,
      source_version TEXT NOT NULL,
      title TEXT NOT NULL,
      path TEXT NOT NULL,
      layer INTEGER NOT NULL,
      node_type TEXT NOT NULL,
      level INTEGER NOT NULL,
      preview TEXT NOT NULL,
      metadata_json TEXT NOT NULL,
      search_metadata TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE nodes_fts USING fts5(
      title, path, preview, search_metadata,
      content='nodes', content_rowid='node_pk',
      tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TABLE edges (
      source_pk INTEGER NOT NULL,
      target_pk INTEGER NOT NULL,
      relation TEXT NOT NULL,
      edge_key TEXT NOT NULL,
      weight REAL NOT NULL,
      evidence_json TEXT NOT NULL,
      properties_json TEXT NOT NULL,
      PRIMARY KEY (source_pk, target_pk, relation, edge_key)
    ) WITHOUT ROWID;
    CREATE INDEX edges_source_relation ON edges(source_pk, relation);
    CREATE INDEX edges_target_relation ON edges(target_pk, relation);
    CREATE INDEX nodes_layer_type ON nodes(layer, node_type);
    CREATE TABLE semantic_terms (term_idx INTEGER PRIMARY KEY, term TEXT NOT NULL UNIQUE, idf REAL NOT NULL);
    CREATE TABLE semantic_postings (
      term_idx INTEGER NOT NULL, node_pk INTEGER NOT NULL, weight REAL NOT NULL,
      PRIMARY KEY (term_idx, node_pk)
    ) WITHOUT ROWID;
    CREATE INDEX semantic_postings_node ON semantic_postings(node_pk);
    CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
  `);
  connection.prepare("INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)").run(
    1, "scenario-sale", "onec:1c-erp:2.5:idv1:l1:sale", 1, "content-1", "2.5",
    "Продажа клиенту", "scenarios/sale", 1, "scenario", 1,
    "Заказ клиента приводит к реализации", "{}", "продажа заказ клиент реализация",
  );
  connection.prepare("INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)").run(
    2, "ERPcode/Documents/ЗаказКлиента", "onec:1c-erp:2.5:idv1:l3:order", 1, "content-2", "2.5",
    "Документ Заказ клиента", "ERPcode/Documents/ЗаказКлиента", 3,
    "metadata", 1, "Объект метаданных: Документ", '{"metadata_type":"Document"}', "документ заказ клиента",
  );
  connection.exec("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')");
  connection.prepare("INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?)").run(
    1, 2, "entry_doc", "0", 1, '{"method":"fixture"}', "{}",
  );
  connection.prepare("INSERT INTO semantic_terms VALUES (?, ?, ?)").run(1, "заказ", 1.2);
  connection.prepare("INSERT INTO semantic_terms VALUES (?, ?, ?)").run(2, "клиента", 1.1);
  connection.prepare("INSERT INTO semantic_terms VALUES (?, ?, ?)").run(3, "заказ клиента", 1.5);
  const posting = connection.prepare("INSERT INTO semantic_postings VALUES (?, ?, ?)");
  posting.run(1, 1, 0.45); posting.run(2, 1, 0.40); posting.run(3, 1, 0.60);
  posting.run(1, 2, 0.50); posting.run(2, 2, 0.48); posting.run(3, 2, 0.65);
  const meta = connection.prepare("INSERT INTO graph_meta VALUES (?, ?)");
  for (const [key, value] of Object.entries({
    schema_version: "3", id_schema_version: "1", built_at: "2026-08-13T00:00:00Z", nodes: "2", edges: "1",
    semantic_nodes: "2", semantic_postings: "6", search_mode: "hybrid", raw_duplicate_ids: "0",
  })) meta.run(key, value);
  connection.close();
}

before(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "erp-graph-mcp-test-"));
  const data = path.join(root, "graph_rag_data");
  fs.mkdirSync(data, { recursive: true });
  for (const skill of ["erp-graph-research", "erp-requirements-modeling", "erp-solution-authoring"]) {
    const skillDirectory = path.join(root, ".agents", "skills", skill);
    fs.mkdirSync(skillDirectory, { recursive: true });
    fs.writeFileSync(path.join(skillDirectory, "SKILL.md"), `# ${skill}\n`, "utf8");
  }
  fs.writeFileSync(path.join(root, "model-policy.json"), JSON.stringify({
    schema_version: 1,
    policy_id: "fixture-policy",
    reject_unreported_model: true,
    required_artifact_fields: ["provider", "model", "parameters", "skill", "skill_version", "run_id"],
    allowed_skills: ["erp-graph-research", "erp-requirements-modeling", "erp-solution-authoring"],
  }), "utf8");
  const modeler = path.join(root, "1c_modeler_upgrade", "graphs");
  fs.mkdirSync(modeler, { recursive: true });
  fs.writeFileSync(path.join(modeler, "graph_manifest.json"), JSON.stringify({
    configuration: "1С:ERP 2.5", release: "2.5.27.49",
  }), "utf8");
  fs.writeFileSync(path.join(modeler, "1c_erp_2_5_route_graph.json"), JSON.stringify({
    release: "2.5.27.49", nodes: { "Route.OK": { id: "Route.OK" } }, edges: [],
  }), "utf8");
  databasePath = path.join(data, "erp_graph_mcp.sqlite");
  buildFixture(databasePath);
});

after(() => {
  const resolved = path.resolve(root);
  assert.ok(resolved.startsWith(path.resolve(os.tmpdir())));
  fs.rmSync(resolved, { recursive: true, force: true });
});

test("search and typed expansion use the compact SQLite sidecar", () => {
  const store = new ErpGraphStore(root, databasePath);
  const search = store.search({
    query: "заказ клиента",
    layers: [1, 3],
    nodeTypes: [],
    limit: 10,
    strategy: "any",
  });
  assert.equal(search.count, 2);
  assert.equal(search.mode_effective, "hybrid");
  assert.ok(search.results.every((item) => item.channels.includes("semantic")));
  assert.ok(search.results.some((item) => item.id === "scenario-sale"));

  const canonical = store.getNodes(["onec:1c-erp:2.5:idv1:l3:order"]);
  assert.equal(canonical[0].id, "ERPcode/Documents/ЗаказКлиента");

  const expanded = store.expand({
    seeds: ["scenario-sale"], direction: "out", relations: ["entry_doc"], depth: 1, limit: 10,
  });
  assert.deepEqual(expanded.nodes.map((item) => item.id).sort(), [
    "ERPcode/Documents/ЗаказКлиента", "scenario-sale",
  ]);
  assert.equal(expanded.edges[0].relation, "entry_doc");
  store.close();
});

test("project state versions decisions and gates final answers by approval", () => {
  const store = new ErpGraphStore(root, databasePath);
  const created = store.createProject({
    title: "Продажи", requirements: "Оформлять заказ клиента", product: "1С:ERP", release: "2.5.27.49",
  });
  const projectId = created.project.project_id;
  assert.equal(created.created, true);
  assert.throws(
    () => store.createProject({ title: "Bad", requirements: "x", projectId: "../outside" }),
    /недопустимые/u,
  );
  assert.throws(
    () => store.saveArtifact(projectId, { kind: "answer", content: "# Ответ" }),
    /подтверждения/u,
  );

  const started = store.startAgentRun({
    projectId,
    skill: "erp-requirements-modeling",
    provider: "test-provider",
    model: "test/model-v1",
    parameters: { temperature: 0 },
    requestSummary: "Моделирование продаж",
  });
  const provenance = started.provenance;

  const decision = store.recordDecisions(projectId, [{
    id: "delivery-mode", question_id: "Q-DELIVERY", question: "Как доставлять?", answer: "Силами продавца",
    normalized_value: "Силами продавца", allowed_values: ["Силами продавца", "Перевозчик"],
    affected_requirement_ids: ["REQ-001"],
    affected_node_ids: ["scenario-sale"],
  }]);
  assert.equal(decision.revision, 2);
  assert.throws(() => store.recordDecisions(projectId, [{
    id: "payment", question: "Вариант оплаты?", answer: "Подтверждаю настройки",
    normalized_value: "Подтверждаю настройки", allowed_values: ["Вариант 1", "Вариант 2"],
  }]), /неоднозначен/u);
  store.saveArtifact(projectId, { kind: "requirement-map", content: { requirements: [{
    id: "REQ-001", coverage_status: "covered", solution_element_ids: ["STEP-001"], acceptance_test_ids: ["AT-001"],
  }] }, provenance });
  store.saveArtifact(projectId, { kind: "evidence-map", content: { evidence: [{
    id: "E-001", status: "verified_metadata", source_ref: "ERPcode/Documents/ЗаказКлиента",
    object_ref: "ERPcode/Documents/ЗаказКлиента", route_ref: "Route.OK",
  }] }, provenance });
  store.saveArtifact(projectId, { kind: "solution-model", content: {
    elements: [{ id: "STEP-001", requirement_ids: ["REQ-001"] }], processes: [],
  }, provenance });
  store.saveArtifact(projectId, { kind: "traceability", content: { links: [{
    requirement_id: "REQ-001", solution_element_ids: ["STEP-001"], acceptance_test_ids: ["AT-001"],
  }] }, provenance });
  store.saveArtifact(projectId, { kind: "acceptance-tests", content: { tests: [{
    id: "AT-001", requirement_ids: ["REQ-001"], preconditions: [], actions: [], expected_result: "Заказ создан",
  }] }, provenance });
  store.saveArtifact(projectId, { kind: "gap-register", content: { gaps: [] }, provenance });
  store.saveArtifact(projectId, { kind: "quality-gate", content: { ready_for_design_approval: true, gaps: [] }, provenance });
  const designValidation = store.validateProject(projectId, { forFinal: false });
  assert.equal(designValidation.passed, true, designValidation.errors.join("; "));
  const wrongRelease = store.loadProjectState(projectId);
  wrongRelease.release = "2.5.99.1";
  store.saveProjectState(wrongRelease);
  assert.match(store.validateProject(projectId, { forFinal: false }).errors.join(" "), /нельзя использовать/u);
  wrongRelease.release = "2.5.27.49";
  store.saveProjectState(wrongRelease);
  const approval = store.recordApproval(projectId, { stage: "design", approved: true, note: "Модель согласована" });
  store.finishAgentRun(started.run.run_id);
  const authoring = store.startAgentRun({
    projectId, skill: "erp-solution-authoring", provider: "test-provider", model: "test/model-v1",
    parameters: { temperature: 0 }, requestSummary: "Авторинг",
  });
  const saved = store.saveArtifact(projectId, {
    kind: "answer", title: "Инструкция", content: "# Ответ", provenance: authoring.provenance,
  });
  const validation = store.validateProject(projectId, { forFinal: true });
  assert.equal(validation.passed, true);
  store.recordApproval(projectId, { stage: "final", approved: true, note: "Итог подтверждён" });
  assert.equal(approval.approval.approved, true);
  assert.match(saved.artifact.path, /^answers_md\//u);
  assert.equal(saved.artifact.provenance.model, "test/model-v1");
  store.recordToolCall("search_nodes", { query: "заказ" }, { nodes: [{ id: "scenario-sale" }], revision: 3 }, 12);
  const run = store.getAgentRun(authoring.run.run_id);
  assert.ok(run.run.selected_node_ids.includes("scenario-sale"));
  assert.ok(run.run.artifact_paths.some((item) => item.startsWith("answers_md/")));
  assert.equal(store.finishAgentRun(authoring.run.run_id).run.status, "completed");
  assert.equal(store.getProject(projectId).status, "successful");
  store.close();
});

test("project questions persist exact provider tokens, durations and request counts", () => {
  const store = new ErpGraphStore(root, databasePath);
  const created = store.createProject({
    title: "Телеметрия", requirements: "Проверить заказ клиента", product: "1С:ERP", release: "2.5.27.49",
  });
  const projectId = created.project.project_id;
  const query = store.startProjectQuery(projectId, { question: "Какие документы нужны для процесса?" });
  store.recordToolCall("search_nodes", { query: "заказ клиента" }, { count: 2 }, 17);
  store.recordModelCallTelemetry({
    provider: "test-api", model: "test/model", reasoningEffort: "medium",
    skillVersion: "sha256:test", applicationVersion: "4.0.0", graphVersion: "3", graphHash: "fixture",
    inputTokens: 1200, cachedInputTokens: 300, outputTokens: 450, reasoningTokens: 75,
    durationMs: 920, attempt: 1, result: "completed", usageSource: "provider_response",
  });
  const finished = store.finishProjectQuery(projectId, query.query_id, {
    status: "completed", answerPath: "answers_md/example.md",
  });
  assert.equal(finished.query.request_counts.model, 1);
  assert.equal(finished.query.request_counts.mcp, 1);
  assert.equal(finished.query.tokens.input, 1200);
  assert.equal(finished.query.tokens.cached_input, 300);
  assert.equal(finished.query.tokens.output, 450);
  assert.equal(finished.query.tokens.reasoning, 75);
  assert.equal(finished.query.tokens.exact, true);
  assert.equal(finished.query.model_time_ms, 920);
  assert.ok(finished.query.wall_time_ms >= 0);
  assert.equal(finished.aggregate.requests.model, 1);
  assert.equal(finished.aggregate.requests.mcp, 1);
  assert.equal(finished.aggregate.tokens.input, 1200);
  assert.ok(fs.existsSync(path.join(root, "results", projectId, "analysis", "telemetry-report.json")));

  const hostQuery = store.startProjectQuery(projectId, { question: "Вопрос, отвеченный текущим Codex-хостом" });
  const hostFinished = store.finishProjectQuery(projectId, hostQuery.query_id, { status: "completed" });
  assert.equal(hostFinished.query.tokens.exact, false);
  assert.equal(hostFinished.query.tokens.availability, "unavailable_from_codex_host");
  store.close();
});

test("stdio MCP server exposes graph and project tools", async () => {
  const serverPath = path.resolve("mcp/erp-graph-server.mjs");
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverPath],
    env: { ...process.env, ERP_GRAPH_ROOT: root },
    stderr: "pipe",
  });
  const client = new Client({ name: "erp-graph-test", version: "1.0.0" });
  await client.connect(transport);
  try {
    const tools = await client.listTools();
    assert.ok(tools.tools.some((item) => item.name === "search_nodes"));
    assert.ok(tools.tools.some((item) => item.name === "start_agent_run"));
    assert.ok(tools.tools.some((item) => item.name === "get_agent_run"));
    assert.ok(tools.tools.some((item) => item.name === "record_project_approval"));
    assert.ok(tools.tools.some((item) => item.name === "start_project_query"));
    assert.ok(tools.tools.some((item) => item.name === "record_model_call_telemetry"));
    assert.ok(tools.tools.some((item) => item.name === "finish_project_query"));
    assert.ok(tools.tools.some((item) => item.name === "get_project_telemetry"));
    assert.ok(tools.tools.some((item) => item.name === "get_api_agent_status"));
    assert.ok(tools.tools.some((item) => item.name === "run_api_agent"));
    const response = await client.callTool({
      name: "search_nodes",
      arguments: { query: "заказ клиента", limit: 5 },
    });
    assert.equal(response.structuredContent.count, 2);
  } finally {
    await client.close();
  }
});
