import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const ROLE_CONFIG = Object.freeze({
  "erp-translator": {
    skill: "erp-requirements-modeling",
    prompt: "erp-translator.md",
    output: "json",
    artifactKinds: ["requirement-map", "evidence-map", "questions"],
  },
  "erp-process-planner": {
    skill: "erp-requirements-modeling",
    prompt: "erp-process-planner.md",
    output: "json",
    artifactKinds: ["requirement-map", "solution-model", "gap-register", "traceability", "acceptance-tests", "quality-gate"],
  },
  "instruction-writer": {
    skill: "erp-solution-authoring",
    prompt: "instruction-writer.md",
    output: "markdown",
    artifactKinds: ["answer"],
  },
});

function unquote(value) {
  if (value.length < 2) return value;
  const quote = value[0];
  if ((quote !== '"' && quote !== "'") || value.at(-1) !== quote) return value;
  const inner = value.slice(1, -1);
  return quote === "'" ? inner : inner.replace(/\\n/g, "\n").replace(/\\r/g, "\r").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
}

export function readDotEnvValue(content, name) {
  for (const rawLine of String(content).replace(/^\uFEFF/u, "").split(/\r?\n/u)) {
    const match = rawLine.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/u);
    if (!match || match[1] !== name) continue;
    let value = match[2].trim();
    if (!value.startsWith('"') && !value.startsWith("'")) value = value.replace(/\s+#.*$/u, "").trim();
    return unquote(value).trim();
  }
  return "";
}

export function resolveApiKey(roots, names, env = process.env) {
  for (const name of names) {
    if (typeof env?.[name] === "string" && env[name].trim()) {
      return { name, value: env[name].trim(), source: "environment" };
    }
  }
  for (const root of roots.filter(Boolean)) {
    const envFile = path.join(root, ".env");
    if (!fs.existsSync(envFile)) continue;
    const content = fs.readFileSync(envFile, "utf8");
    for (const name of names) {
      const value = readDotEnvValue(content, name);
      if (value) return { name, value, source: envFile };
    }
  }
  return null;
}

function parseJsonText(text) {
  const clean = String(text || "").trim().replace(/^```(?:json)?\s*/iu, "").replace(/\s*```$/u, "");
  try {
    return JSON.parse(clean);
  } catch {
    const start = clean.indexOf("{");
    const end = clean.lastIndexOf("}");
    if (start >= 0 && end > start) return JSON.parse(clean.slice(start, end + 1));
    throw new Error("API-агент вернул невалидный JSON");
  }
}

function assistantText(payload) {
  const content = payload?.choices?.[0]?.message?.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.filter((item) => item?.type === "text").map((item) => item.text || "").join("\n");
  throw new Error("API-провайдер не вернул текст ответа");
}

function usageFromPayload(payload) {
  const usage = payload?.usage || {};
  const promptDetails = usage.prompt_tokens_details || usage.input_tokens_details || {};
  const completionDetails = usage.completion_tokens_details || usage.output_tokens_details || {};
  return {
    inputTokens: Number(usage.prompt_tokens ?? usage.input_tokens ?? 0) || 0,
    cachedInputTokens: Number(promptDetails.cached_tokens ?? 0) || 0,
    outputTokens: Number(usage.completion_tokens ?? usage.output_tokens ?? 0) || 0,
    reasoningTokens: Number(completionDetails.reasoning_tokens ?? 0) || 0,
  };
}

function compactProject(store, projectId, maxChars) {
  const project = store.getProject(projectId);
  const artifacts = {};
  for (const kind of ["requirement-map", "evidence-map", "questions", "decision-register", "solution-model", "gap-register", "traceability", "acceptance-tests", "quality-gate"]) {
    try {
      artifacts[kind] = parseJsonText(store.readArtifact(projectId, { kind, maxChars }).content);
    } catch { /* Artifact is optional at earlier lifecycle stages. */ }
  }
  return { project, artifacts };
}

function graphContext(store, project, limitChars = 50_000) {
  const raw = [project.title, project.specification?.text || "", ...(project.contexts || []).filter((item) => item.active !== false).map((item) => item.text || "")].join("\n");
  const queries = [...new Set(raw.split(/[\n.!?;]+/u).map((item) => item.trim()).filter((item) => item.length >= 8).slice(0, 10))];
  const nodes = new Map();
  for (const query of queries) {
    let found;
    try { found = store.search({ query: query.slice(0, 1_000), layers: [], nodeTypes: [], limit: 5, strategy: "any", mode: "hybrid" }); }
    catch { continue; }
    for (const item of found?.results || found?.nodes || []) {
      const id = String(item.id || item.node_id || "");
      if (id && !nodes.has(id)) nodes.set(id, item);
    }
  }
  const selected = [...nodes.values()].slice(0, 30);
  const text = JSON.stringify(selected);
  return { nodes: selected, serialized: text.slice(0, limitChars) };
}

function requireLifecycle(role, project) {
  if (role === "erp-process-planner" && !project.approval_state?.requirements) {
    throw new Error("Сначала требуется явное подтверждение требований");
  }
  if (role === "instruction-writer" && !project.approval_state?.design) {
    throw new Error("Сначала требуется явное подтверждение проекта и схемы");
  }
}

export class ErpApiAgents {
  constructor(store, { root = store.root, fetchImpl = globalThis.fetch, env = process.env } = {}) {
    this.store = store;
    this.root = root;
    this.fetch = fetchImpl;
    this.env = env;
    this.active = false;
  }

  policy() {
    const file = path.join(this.root, "agent-runtime-policy.json");
    if (!fs.existsSync(file)) throw new Error(`Agent runtime policy не найден: ${file}`);
    const stored = JSON.parse(fs.readFileSync(file, "utf8"));
    const models = stored.models_by_agent || stored.models_by_role || {};
    return {
      ...stored,
      provider: stored.provider || "wormsoft-gateway",
      api_key_precedence: stored.api_key_precedence || [stored.api_key_env || "WORMSOFT_API_KEY"],
      allowed_agents: stored.allowed_agents || Object.keys(ROLE_CONFIG),
      models_by_agent: models,
      max_subagent_calls_per_revision: Number(stored.max_subagent_calls_per_revision || stored.max_api_calls_per_revision || 3),
      timeout_sec: Number(stored.timeout_sec || 900),
    };
  }

  keyStatus() {
    const policy = this.policy();
    const key = resolveApiKey([this.root, this.store.graphSourceRoot], policy.api_key_precedence, this.env);
    return {
      profile: policy.default_profile || policy.profile || "api-key-multi-agent",
      provider: policy.provider,
      configured: Boolean(key),
      key_name: key?.name || policy.api_key_precedence[0],
      key_source: key ? (key.source === "environment" ? "environment" : path.relative(this.root, key.source) || ".env") : "not_found",
      allowed_agents: policy.allowed_agents,
      models_by_agent: policy.models_by_agent,
      max_calls_per_revision: policy.max_subagent_calls_per_revision,
      automatic_retries: 0,
    };
  }

  async run({ projectId, role, task = "", queryId = "" }) {
    const config = ROLE_CONFIG[role];
    if (!config) throw new Error(`Неизвестная роль API-агента: ${role}`);
    const policy = this.policy();
    if (!policy.allowed_agents.includes(role)) throw new Error(`Роль ${role} запрещена agent-runtime-policy.json`);
    if (this.active) throw new Error("Другой ERP API-агент уже выполняется; политика разрешает только последовательные вызовы");
    const model = policy.models_by_agent[role];
    if (!model) throw new Error(`Для роли ${role} не настроена модель`);
    const key = resolveApiKey([this.root, this.store.graphSourceRoot], policy.api_key_precedence, this.env);
    if (!key) throw new Error(`Не найден API-ключ (${policy.api_key_precedence.join(", ")}). Добавьте его в .env проекта или окружение MCP`);

    const state = compactProject(this.store, projectId, Number(policy.max_input_chars || 140_000));
    requireLifecycle(role, state.project);
    const calls = this.store.listAgentRuns({ projectId, limit: 200 }).filter((item) =>
      item.provider === policy.provider && item.current_revision === state.project.revision && item.status !== "failed");
    if (calls.length >= policy.max_subagent_calls_per_revision) {
      throw new Error(`Для ревизии ${state.project.revision} исчерпан лимит ${policy.max_subagent_calls_per_revision} API-агентов`);
    }
    const evidence = graphContext(this.store, state.project);
    const systemPrompt = fs.readFileSync(path.join(this.root, "mcp", "prompts", config.prompt), "utf8");
    const userPrompt = [
      `Project ID: ${projectId}`,
      `Project revision: ${state.project.revision}`,
      task ? `Задача интерфейса: ${task.slice(0, 4_000)}` : "",
      "Состояние проекта и текущие артефакты:",
      JSON.stringify(state),
      "Детерминированно найденный контекст ERP Graph (кандидаты; verified только при точном source_ref):",
      evidence.serialized,
    ].filter(Boolean).join("\n\n").slice(0, Number(policy.max_input_chars || 140_000));
    const parameters = { temperature: 0, max_output_tokens: Number(policy.max_output_tokens || 30_000), reasoning_effort: policy.reasoning_effort_by_role?.[role] || "" };
    let effectiveQueryId = queryId;
    let ownsQuery = false;
    if (!effectiveQueryId && this.store.activeProjectQuery?.project_id === projectId) {
      effectiveQueryId = this.store.activeProjectQuery.query_id;
    }
    if (!effectiveQueryId) {
      effectiveQueryId = this.store.startProjectQuery(projectId, { question: task || `API-agent ${role}`, metadata: { initiated_by: "run_api_agent", role } }).query_id;
      ownsQuery = true;
    }
    let startedRun;
    try {
      startedRun = this.store.startAgentRun({ projectId, skill: config.skill, provider: policy.provider, model, parameters, requestSummary: task || role });
    } catch (error) {
      if (ownsQuery) this.store.finishProjectQuery(projectId, effectiveQueryId, { status: "failed", error: error?.message || String(error) });
      throw error;
    }
    const runId = startedRun.run.run_id;
    const startedAt = Date.now();
    this.active = true;
    try {
      const endpointBase = policy.wormsoft_base_url || policy.openai_base_url || "https://ai.wormsoft.ru/api/gpt";
      const url = endpointBase.endsWith("/chat/completions") ? endpointBase : `${endpointBase.replace(/\/$/u, "")}/chat/completions`;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), policy.timeout_sec * 1_000);
      let response;
      try {
        response = await this.fetch(url, {
          method: "POST",
          headers: { Authorization: `Bearer ${key.value}`, "Content-Type": "application/json" },
          body: JSON.stringify({ model, messages: [{ role: "system", content: systemPrompt }, { role: "user", content: userPrompt }], temperature: 0, max_tokens: parameters.max_output_tokens }),
          signal: controller.signal,
        });
      } finally { clearTimeout(timer); }
      if (!response.ok) throw new Error(`API-провайдер вернул HTTP ${response.status}: ${(await response.text()).slice(0, 1_000)}`);
      const payload = await response.json();
      const text = assistantText(payload);
      const usage = usageFromPayload(payload);
      const durationMs = Date.now() - startedAt;
      this.store.recordModelCallTelemetry({ projectId, queryId: effectiveQueryId, provider: policy.provider, model, reasoningEffort: parameters.reasoning_effort, skillVersion: startedRun.provenance.skill_version, applicationVersion: "4.0.0", inputTokens: usage.inputTokens, cachedInputTokens: usage.cachedInputTokens, outputTokens: usage.outputTokens, reasoningTokens: usage.reasoningTokens, durationMs, attempt: 1, result: "completed", usageSource: "provider_response" });

      const saved = [];
      if (config.output === "json") {
        const parsed = parseJsonText(text);
        for (const kind of config.artifactKinds) {
          const content = parsed.artifacts?.[kind];
          if (content === undefined) throw new Error(`API-агент не вернул обязательный артефакт ${kind}`);
          saved.push(this.store.saveArtifact(projectId, { kind, content, title: kind, metadata: { role, graph_node_ids: evidence.nodes.map((item) => item.id || item.node_id).filter(Boolean) }, provenance: startedRun.provenance }).artifact);
        }
      } else {
        saved.push(this.store.saveArtifact(projectId, { kind: "answer", content: text, title: state.project.title, metadata: { role, project_revision: state.project.revision }, provenance: startedRun.provenance }).artifact);
      }
      this.store.finishAgentRun(runId, { status: "completed", summary: `${role}: сохранено ${saved.length} артефактов` });
      if (ownsQuery) this.store.finishProjectQuery(projectId, effectiveQueryId, { status: role === "instruction-writer" ? "completed" : "needs_input", answerPath: saved.find((item) => item.kind === "answer")?.path || "" });
      return { role, provider: policy.provider, model, project_id: projectId, project_revision: state.project.revision, query_id: effectiveQueryId, artifacts: saved, usage, duration_ms: durationMs };
    } catch (error) {
      const durationMs = Date.now() - startedAt;
      try { this.store.recordModelCallTelemetry({ projectId, queryId: effectiveQueryId, provider: policy.provider, model, reasoningEffort: parameters.reasoning_effort, skillVersion: startedRun.provenance.skill_version, applicationVersion: "4.0.0", inputTokens: null, cachedInputTokens: null, outputTokens: null, reasoningTokens: null, durationMs, attempt: 1, result: error?.name === "AbortError" ? "timeout" : "failed", error: error?.message || String(error), usageSource: "provider_response" }); } catch { /* Preserve original error. */ }
      this.store.finishAgentRun(runId, { status: "failed", summary: error?.message || String(error) });
      if (ownsQuery) {
        try { this.store.finishProjectQuery(projectId, effectiveQueryId, { status: "failed", error: error?.message || String(error) }); } catch { /* Preserve original error. */ }
      }
      throw error;
    } finally {
      this.active = false;
    }
  }
}

export { ROLE_CONFIG };
