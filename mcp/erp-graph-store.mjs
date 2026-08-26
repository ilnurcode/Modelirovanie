import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
export const DEFAULT_ROOT = path.resolve(process.env.ERP_GRAPH_ROOT || path.join(MODULE_DIR, ".."));
const INDEX_NAME = "erp_graph_mcp.sqlite";
const MAX_PROJECT_TEXT = 500_000;
const MODEL_POLICY_NAME = "model-policy.json";
const RUN_DIRECTORY = "_agent_runs";
const UNREPORTED_MODELS = new Set(["", "unknown", "unreported", "host-selected", "default"]);

function parseJson(value, fallback = {}) {
  if (!value) return fallback;
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function isoNow() {
  return new Date().toISOString();
}

function safeId(value, label = "ID") {
  const result = String(value || "").trim();
  if (!result || result.length > 120 || result.includes("..") || /[\\/:*?"<>|\x00-\x1f]/u.test(result)) {
    throw new Error(`${label} содержит недопустимые символы`);
  }
  return result;
}

function slug(value, limit = 48) {
  const normalized = String(value || "project")
    .normalize("NFKC")
    .toLocaleLowerCase("ru-RU")
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, limit)
    .replace(/-+$/g, "");
  return normalized || "project";
}

function atomicWrite(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(temporary, content, "utf8");
  fs.renameSync(temporary, file);
}

function atomicJson(file, payload) {
  atomicWrite(file, `${JSON.stringify(payload, null, 2)}\n`);
}

function inside(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function signature(file) {
  const stat = fs.statSync(file, { bigint: true });
  return `${stat.size}:${stat.mtimeNs}`;
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function yamlScalar(value) {
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  if (value === null || value === undefined) return "null";
  return JSON.stringify(String(value));
}

function pythonProjectYaml(project) {
  const status = project.status === "successful" ? "successful" : "configured";
  return [
    'managed_by: "newagent-mcp"',
    `project_id: ${yamlScalar(project.project_id)}`,
    `title: ${yamlScalar(project.title)}`,
    'mode: "full"',
    `status: ${yamlScalar(status)}`,
    `created_at: ${yamlScalar(project.created_at)}`,
    `updated_at: ${yamlScalar(project.updated_at)}`,
    "configuration:",
    `  product: ${yamlScalar(project.product || "1C:ERP")}`,
    '  edition: "2.5"',
    `  release: ${yamlScalar(project.release || "")}`,
    "generation:",
    '  questions: "required"',
    "  follow_up_questions: true",
    "  diagram: true",
    '  detail_level: "balanced"',
    "sources:",
    '  internet_policy: "official_and_allowed_web"',
    '  local_configuration_id: ""',
    'agent_profile: ""',
    `revision: ${Number(project.revision || 1)}`,
    "requirements_version: 0",
    "design_version: 0",
    `instruction_version: ${project.artifacts?.some((item) => item.kind === "answer") ? 1 : 0}`,
    'last_error: ""',
    "",
  ].join("\n");
}

function compactForJournal(value, depth = 0) {
  if (depth > 3) return "[truncated]";
  if (value === null || ["string", "number", "boolean"].includes(typeof value)) {
    return typeof value === "string" && value.length > 2_000 ? `${value.slice(0, 2_000)}…` : value;
  }
  if (Array.isArray(value)) return value.slice(0, 30).map((item) => compactForJournal(item, depth + 1));
  if (typeof value !== "object") return String(value);
  const result = {};
  for (const [key, item] of Object.entries(value).slice(0, 40)) {
    if (["content", "requirements", "specification"].includes(key)) {
      result[key] = typeof item === "string" ? `${item.slice(0, 500)}${item.length > 500 ? "…" : ""}` : "[omitted]";
    } else {
      result[key] = compactForJournal(item, depth + 1);
    }
  }
  return result;
}

function collectJournalFacts(value, facts = { node_ids: new Set(), artifacts: new Set(), revisions: new Set() }, depth = 0) {
  if (depth > 6 || value === null || value === undefined) return facts;
  if (Array.isArray(value)) {
    for (const item of value.slice(0, 500)) collectJournalFacts(item, facts, depth + 1);
    return facts;
  }
  if (typeof value !== "object") return facts;
  for (const [key, item] of Object.entries(value)) {
    if (["id", "node_id", "canonical_id", "source", "target", "start", "end"].includes(key)
      && typeof item === "string" && (item.startsWith("onec:") || item.startsWith("ERPcode/") || item.startsWith("scenario-") || item.startsWith("clarification-") || item.startsWith("section-"))) {
      facts.node_ids.add(item);
    }
    if (["path", "artifact_path"].includes(key) && typeof item === "string"
      && (item.startsWith("agent_artifacts/") || item.startsWith("answers_md/"))) facts.artifacts.add(item);
    if (key === "revision" && Number.isInteger(item)) facts.revisions.add(item);
    collectJournalFacts(item, facts, depth + 1);
  }
  return facts;
}

function terms(query) {
  return [...new Set(String(query || "").normalize("NFKC").toLocaleLowerCase("ru-RU").match(/[\p{L}\p{N}_]{2,}/gu) || [])]
    .slice(0, 24);
}

function ftsExpression(query, strategy) {
  const tokens = terms(query);
  if (!tokens.length) throw new Error("Поисковый запрос не содержит индексируемых слов");
  const operator = strategy === "all" ? " AND " : " OR ";
  return tokens.map((token) => `"${token.replaceAll('"', '""')}"*`).join(operator);
}

function semanticTerms(query) {
  const tokens = terms(query);
  const counts = new Map();
  for (const token of tokens) counts.set(token, (counts.get(token) || 0) + 1);
  for (let index = 0; index + 1 < tokens.length; index += 1) {
    const bigram = `${tokens[index]} ${tokens[index + 1]}`;
    counts.set(bigram, (counts.get(bigram) || 0) + 1);
  }
  return counts;
}

function normalizeScores(rows, field) {
  if (!rows.length) return rows;
  const values = rows.map((row) => Number(row[field] || 0));
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  return rows.map((row, index) => ({
    ...row,
    [`${field}_normalized`]: maximum > minimum ? (values[index] - minimum) / (maximum - minimum) : values[index] > 0 ? 1 : 0,
  }));
}

function compactNode(row, previewChars = 900) {
  return {
    id: row.id,
    canonical_id: row.canonical_id || row.id,
    id_schema_version: Number(row.id_schema_version || 0),
    content_version: row.content_version || "",
    source_version: row.source_version || "",
    title: row.title,
    path: row.path,
    layer: Number(row.layer),
    node_type: row.node_type,
    level: Number(row.level),
    preview: String(row.preview || "").slice(0, previewChars),
    metadata: parseJson(row.metadata_json),
  };
}

function placeholders(length) {
  return Array.from({ length }, () => "?").join(", ");
}

export class ErpGraphStore {
  constructor(root = DEFAULT_ROOT, databasePath = null) {
    this.root = path.resolve(root);
    const localDatabase = path.join(this.root, "graph_rag_data", INDEX_NAME);
    const siblingGraphRoot = path.resolve(this.root, "..", "RAGAgent");
    const siblingDatabase = path.join(siblingGraphRoot, "graph_rag_data", INDEX_NAME);
    const selectedDatabase = databasePath
      || process.env.ERP_GRAPH_DATABASE
      || (fs.existsSync(localDatabase) ? localDatabase : siblingDatabase);
    this.graphSourceRoot = path.resolve(
      process.env.ERP_GRAPH_SOURCE_ROOT
      || (path.resolve(selectedDatabase) === path.resolve(siblingDatabase) ? siblingGraphRoot : this.root),
    );
    this.databasePath = path.resolve(
      selectedDatabase,
    );
    this.dataDir = path.dirname(this.databasePath);
    // Codex and the Python application intentionally share one project root.
    this.taskRoot = path.resolve(process.env.ERP_PROJECT_ROOT || path.join(this.root, "results"));
    this.runRoot = path.join(this.taskRoot, RUN_DIRECTORY);
    this.modelPolicyPath = path.join(this.root, MODEL_POLICY_NAME);
    this.database = null;
    this.documentRoot = null;
    this.activeRunId = null;
    this.activeProjectQuery = null;
    this.columns = new Map();
    this.modelerManifest = null;
    this.routeGraphIds = null;
  }

  close() {
    this.database?.close();
    this.database = null;
  }

  tableColumns(table) {
    if (!this.columns.has(table)) {
      this.columns.set(table, new Set(this.db().prepare(`PRAGMA table_info(${table})`).all().map((row) => row.name)));
    }
    return this.columns.get(table);
  }

  db() {
    if (this.database) return this.database;
    if (!fs.existsSync(this.databasePath)) {
      throw new Error(
        `MCP-индекс не найден: ${this.databasePath}. Выполните .\\.venv\\Scripts\\python.exe graph_rag_1c_erp.py build`,
      );
    }
    this.database = new DatabaseSync(this.databasePath, { readOnly: true });
    return this.database;
  }

  status() {
    const exists = fs.existsSync(this.databasePath);
    if (!exists) {
      return {
        ready: false,
        index: this.databasePath,
        build_command: ".\\.venv\\Scripts\\python.exe graph_rag_1c_erp.py build",
      };
    }
    const rows = this.db().prepare("SELECT key, value FROM graph_meta").all();
    const meta = Object.fromEntries(rows.map((row) => [row.key, row.value]));
    const chunksPath = path.join(this.dataDir, "chunks_meta.json");
    const staleReasons = [];
    if (meta.chunks_meta_signature && fs.existsSync(chunksPath) && meta.chunks_meta_signature !== signature(chunksPath)) {
      staleReasons.push("chunks_meta.json изменён");
    }
    const legacyGraphPath = path.join(this.dataDir, "knowledge_graph.pkl");
    if (meta.legacy_pickle_signature && fs.existsSync(legacyGraphPath) && meta.legacy_pickle_signature !== signature(legacyGraphPath)) {
      staleReasons.push("legacy knowledge_graph.pkl изменён после миграции");
    }
    return {
      ready: true,
      stale: staleReasons.length > 0,
      stale_reasons: staleReasons,
      index: this.databasePath,
      size_mb: Number((fs.statSync(this.databasePath).size / 1024 / 1024).toFixed(2)),
      schema_version: Number(meta.schema_version || 0),
      id_schema_version: Number(meta.id_schema_version || 0),
      product: meta.product || "",
      product_version: meta.product_version || "",
      build_mode: meta.build_mode || "",
      built_at: meta.built_at,
      nodes: Number(meta.nodes || 0),
      edges: Number(meta.edges || 0),
      l3_l4_logical_links: Number(meta.l3_l4_logical_links || 0),
      l3_l4_rule_version: meta.l3_l4_rule_version || "",
      search_mode: meta.search_mode || "fts",
      semantic_nodes: Number(meta.semantic_nodes || 0),
      semantic_postings: Number(meta.semantic_postings || 0),
      raw_duplicate_ids: Number(meta.raw_duplicate_ids || 0),
    };
  }

  lexicalSearch({ query, layers = [], nodeTypes = [], limit = 50, strategy = "any" }) {
    const conditions = ["nodes_fts MATCH ?"];
    const parameters = [ftsExpression(query, strategy)];
    if (layers.length) {
      conditions.push(`n.layer IN (${placeholders(layers.length)})`);
      parameters.push(...layers);
    }
    if (nodeTypes.length) {
      conditions.push(`n.node_type IN (${placeholders(nodeTypes.length)})`);
      parameters.push(...nodeTypes);
    }
    parameters.push(limit);
    const rows = this.db().prepare(
      `SELECT n.*, bm25(nodes_fts, 8.0, 2.0, 4.0, 1.0) AS rank
       FROM nodes_fts JOIN nodes n ON n.node_pk = nodes_fts.rowid
       WHERE ${conditions.join(" AND ")}
       ORDER BY rank, n.layer, n.title
       LIMIT ?`,
    ).all(...parameters);
    return rows.map((row, index) => ({
      ...row,
      lexical_rank: index + 1,
      lexical_bm25: Number(Number(row.rank).toFixed(8)),
      lexical_score: 1 / (index + 1),
    }));
  }

  semanticSearch({ query, layers = [], nodeTypes = [], limit = 50 }) {
    if (!this.tableColumns("semantic_terms").has("term_idx")) return [];
    const requested = semanticTerms(query);
    if (!requested.size) return [];
    const queryTerms = [...requested.keys()];
    const termRows = this.db().prepare(
      `SELECT term_idx,term,idf FROM semantic_terms WHERE term IN (${placeholders(queryTerms.length)})`,
    ).all(...queryTerms);
    if (!termRows.length) return [];
    const rawWeights = termRows.map((row) => {
      const count = requested.get(row.term) || 1;
      return { term_idx: Number(row.term_idx), weight: (1 + Math.log(count)) * Number(row.idf) };
    });
    const norm = Math.sqrt(rawWeights.reduce((sum, item) => sum + item.weight ** 2, 0)) || 1;
    const weighted = rawWeights.map((item) => ({ ...item, weight: item.weight / norm }));
    const valuesSql = weighted.map(() => "(?, ?)").join(", ");
    const conditions = [];
    const parameters = weighted.flatMap((item) => [item.term_idx, item.weight]);
    if (layers.length) {
      conditions.push(`n.layer IN (${placeholders(layers.length)})`);
      parameters.push(...layers);
    }
    if (nodeTypes.length) {
      conditions.push(`n.node_type IN (${placeholders(nodeTypes.length)})`);
      parameters.push(...nodeTypes);
    }
    parameters.push(limit);
    const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
    const rows = this.db().prepare(
      `WITH query_terms(term_idx,query_weight) AS (VALUES ${valuesSql})
       SELECT n.*, SUM(p.weight * q.query_weight) AS semantic_score
       FROM query_terms q
       JOIN semantic_postings p ON p.term_idx=q.term_idx
       JOIN nodes n ON n.node_pk=p.node_pk
       ${where}
       GROUP BY n.node_pk
       HAVING semantic_score > 0
       ORDER BY semantic_score DESC, n.layer, n.title
       LIMIT ?`,
    ).all(...parameters);
    return rows.map((row, index) => ({
      ...row,
      semantic_rank: index + 1,
      semantic_score: Number(Number(row.semantic_score).toFixed(8)),
    }));
  }

  search({ query, layers = [], nodeTypes = [], limit = 12, strategy = "any", mode = "hybrid" }) {
    const candidateLimit = Math.min(200, Math.max(limit * 5, 40));
    const semanticAvailable = this.status().semantic_postings > 0;
    const lexical = mode === "semantic" && semanticAvailable
      ? []
      : this.lexicalSearch({ query, layers, nodeTypes, limit: candidateLimit, strategy });
    const semantic = mode === "fts" || !semanticAvailable
      ? []
      : this.semanticSearch({ query, layers, nodeTypes, limit: candidateLimit });
    const semanticNormalized = normalizeScores(semantic, "semantic_score");
    const candidates = new Map();
    for (const row of lexical) candidates.set(row.id, { row, lexical: row });
    for (const row of semanticNormalized) {
      const current = candidates.get(row.id) || { row, lexical: null };
      current.semantic = row;
      candidates.set(row.id, current);
    }
    const phrase = String(query).trim().toLocaleLowerCase("ru-RU");
    const reranked = [...candidates.values()].map(({ row, lexical: lexicalRow, semantic: semanticRow }) => {
      const title = String(row.title || "").toLocaleLowerCase("ru-RU");
      const exactTitle = title.includes(phrase) ? 1 : 0;
      const lexicalScore = Number(lexicalRow?.lexical_score || 0);
      const semanticScore = Number(semanticRow?.semantic_score_normalized || 0);
      const reciprocalFusion = (lexicalRow ? 1 / (60 + lexicalRow.lexical_rank) : 0)
        + (semanticRow ? 1 / (60 + semanticRow.semantic_rank) : 0);
      const rerankScore = 0.32 * lexicalScore + 0.43 * semanticScore + 0.15 * exactTitle + 0.10 * reciprocalFusion;
      return {
        ...compactNode(row),
        lexical_rank: lexicalRow?.lexical_rank || null,
        lexical_bm25: lexicalRow?.lexical_bm25 ?? null,
        semantic_rank: semanticRow?.semantic_rank || null,
        semantic_score: semanticRow?.semantic_score ?? null,
        rerank_score: Number(rerankScore.toFixed(8)),
        channels: [lexicalRow && "fts5", semanticRow && "semantic"].filter(Boolean),
        match_reasons: [exactTitle && "exact_title_phrase", lexicalRow && "lexical", semanticRow && "semantic"].filter(Boolean),
      };
    }).sort((left, right) => right.rerank_score - left.rerank_score || left.layer - right.layer || left.title.localeCompare(right.title, "ru"));
    const effectiveMode = mode === "hybrid" && !semanticAvailable ? "fts-fallback" : mode;
    return {
      query,
      strategy,
      mode_requested: mode,
      mode_effective: effectiveMode,
      semantic_available: semanticAvailable,
      candidates: { lexical: lexical.length, semantic: semantic.length, merged: candidates.size },
      count: Math.min(limit, reranked.length),
      results: reranked.slice(0, limit),
    };
  }

  getNodes(ids, previewChars = 1_500) {
    const uniqueIds = [...new Set(ids.map(String))].slice(0, 50);
    if (!uniqueIds.length) return [];
    const hasCanonical = this.tableColumns("nodes").has("canonical_id");
    const condition = hasCanonical
      ? `id IN (${placeholders(uniqueIds.length)}) OR canonical_id IN (${placeholders(uniqueIds.length)})`
      : `id IN (${placeholders(uniqueIds.length)})`;
    const parameters = hasCanonical ? [...uniqueIds, ...uniqueIds] : uniqueIds;
    const rows = this.db().prepare(`SELECT * FROM nodes WHERE ${condition}`).all(...parameters);
    const byId = new Map();
    for (const row of rows) {
      const node = compactNode(row, previewChars);
      byId.set(row.id, node);
      if (row.canonical_id) byId.set(row.canonical_id, node);
    }
    return uniqueIds.map((id) => byId.get(id)).filter(Boolean);
  }

  resolveNodeIds(ids) {
    return this.getNodes(ids, 200).map((node) => node.id);
  }

  edgeRows(frontier, direction, relations, rowLimit = 5_000) {
    if (!frontier.length) return [];
    const result = [];
    const relationSql = relations.length ? ` AND e.relation IN (${placeholders(relations.length)})` : "";
    const evidenceSelect = this.tableColumns("edges").has("evidence_json") ? "e.evidence_json" : "'{}' AS evidence_json";
    const select = `SELECT ns.id AS source, nt.id AS target, e.relation, e.edge_key, e.weight, e.properties_json, ${evidenceSelect}
      FROM edges e JOIN nodes ns ON ns.node_pk = e.source_pk JOIN nodes nt ON nt.node_pk = e.target_pk`;
    if (direction === "out" || direction === "both") {
      result.push(...this.db().prepare(
        `${select} WHERE ns.id IN (${placeholders(frontier.length)})${relationSql} LIMIT ?`,
      ).all(...frontier, ...relations, rowLimit));
    }
    if (direction === "in" || direction === "both") {
      result.push(...this.db().prepare(
        `${select} WHERE nt.id IN (${placeholders(frontier.length)})${relationSql} LIMIT ?`,
      ).all(...frontier, ...relations, rowLimit));
    }
    return result;
  }

  expand({ seeds, direction = "both", relations = [], depth = 1, limit = 120 }) {
    const requestedSeeds = [...new Set(seeds.map(String))].slice(0, 20);
    const seedIds = this.resolveNodeIds(requestedSeeds);
    const visited = new Map(seedIds.map((id) => [id, 0]));
    const edges = new Map();
    let frontier = seedIds;
    let truncated = false;
    for (let currentDepth = 1; currentDepth <= depth && frontier.length; currentDepth += 1) {
      const edgeLimit = Math.min(10_000, Math.max(200, limit * 10));
      const rows = this.edgeRows(frontier, direction, relations, edgeLimit);
      if (rows.length >= edgeLimit) truncated = true;
      const next = [];
      for (const row of rows) {
        const key = `${row.source}\u0000${row.target}\u0000${row.relation}\u0000${row.edge_key}`;
        edges.set(key, {
          source: row.source,
          target: row.target,
          relation: row.relation,
          weight: Number(row.weight),
          evidence: parseJson(row.evidence_json),
          properties: parseJson(row.properties_json),
        });
        for (const nodeId of [row.source, row.target]) {
          if (visited.has(nodeId)) continue;
          if (visited.size >= limit) {
            truncated = true;
            continue;
          }
          visited.set(nodeId, currentDepth);
          next.push(nodeId);
        }
      }
      frontier = [...new Set(next)];
    }
    const nodes = this.getNodes([...visited.keys()]).map((node) => ({ ...node, distance: visited.get(node.id) }));
    const found = new Set(nodes.map((node) => node.id));
    return {
      seeds: seedIds,
      requested_seeds: requestedSeeds,
      missing_seeds: requestedSeeds.filter((id) => !this.getNodes([id], 200).length),
      depth,
      truncated,
      nodes,
      edges: [...edges.values()].filter((edge) => found.has(edge.source) && found.has(edge.target)),
    };
  }

  findPaths({ start, end, direction = "out", relations = [], maxDepth = 6, maxPaths = 5 }) {
    const resolvedStart = this.resolveNodeIds([start])[0] || String(start);
    const resolvedEnd = this.resolveNodeIds([end])[0] || String(end);
    const queue = [[resolvedStart]];
    const paths = [];
    const bestDepth = new Map([[resolvedStart, 0]]);
    const maxVisited = 5_000;
    let truncated = false;
    while (queue.length && paths.length < maxPaths) {
      const currentPath = queue.shift();
      const current = currentPath.at(-1);
      if (currentPath.length - 1 >= maxDepth) continue;
      const rows = this.edgeRows([current], direction, relations, 2_000);
      if (rows.length >= 2_000) truncated = true;
      for (const row of rows) {
        const next = direction === "in" ? row.source : row.source === current ? row.target : row.source;
        if (currentPath.includes(next)) continue;
        const nextPath = [...currentPath, next];
        if (next === resolvedEnd) {
          paths.push(nextPath);
          if (paths.length >= maxPaths) break;
          continue;
        }
        const nextDepth = nextPath.length - 1;
        if ((bestDepth.get(next) ?? Infinity) < nextDepth) continue;
        if (bestDepth.size >= maxVisited) {
          truncated = true;
          continue;
        }
        bestDepth.set(next, nextDepth);
        queue.push(nextPath);
      }
    }
    const nodeIds = [...new Set(paths.flat())];
    return { start: resolvedStart, end: resolvedEnd, requested_start: start, requested_end: end, max_depth: maxDepth, truncated, paths, nodes: this.getNodes(nodeIds) };
  }

  findDocumentRoot() {
    if (this.documentRoot !== null) return this.documentRoot;
    const configured = process.env.ERP_DOCS_ROOT;
    if (configured && fs.existsSync(configured)) {
      this.documentRoot = path.resolve(configured);
      return this.documentRoot;
    }
    const match = fs.readdirSync(this.graphSourceRoot, { withFileTypes: true })
      .find((entry) => entry.isDirectory() && /1[СC]-ERP/ui.test(entry.name));
    this.documentRoot = match ? path.join(this.graphSourceRoot, match.name) : "";
    return this.documentRoot;
  }

  sourcePath(node) {
    const metadataPath = node.metadata?.source_xml;
    if (metadataPath) {
      const resolved = path.resolve(metadataPath);
      if (inside(this.graphSourceRoot, resolved) && fs.existsSync(resolved)) return resolved;
    }
    const candidates = [];
    if (node.layer === 4) {
      const documentRoot = this.findDocumentRoot();
      if (documentRoot) candidates.push(path.join(documentRoot, `${node.path}.md`));
    }
    candidates.push(
      path.join(this.graphSourceRoot, `${node.path}.md`),
      path.join(this.graphSourceRoot, node.path),
    );
    return candidates.find((candidate) => inside(this.graphSourceRoot, candidate) && fs.existsSync(candidate)) || "";
  }

  readSource({ nodeId, query = "", offset = 0, maxChars = 16_000 }) {
    const node = this.getNodes([nodeId], 2_000)[0];
    if (!node) throw new Error(`Узел не найден: ${nodeId}`);
    const source = this.sourcePath(node);
    if (!source) {
      return { node, source_found: false, preview: node.preview };
    }
    const stat = fs.statSync(source);
    if (stat.size > 64 * 1024 * 1024) throw new Error("Исходный файл больше 64 МиБ; уточните узел или используйте preview");
    const content = fs.readFileSync(source, "utf8");
    if (!query.trim()) {
      const start = Math.min(offset, content.length);
      return {
        node,
        source_found: true,
        source: path.relative(this.graphSourceRoot, source),
        offset: start,
        next_offset: start + maxChars < content.length ? start + maxChars : null,
        content: content.slice(start, start + maxChars),
      };
    }
    const lowered = content.toLocaleLowerCase("ru-RU");
    const needles = [query.toLocaleLowerCase("ru-RU"), ...terms(query)].filter(Boolean);
    const positions = [];
    for (const needle of needles) {
      let position = lowered.indexOf(needle);
      while (position >= 0 && positions.length < 8) {
        positions.push(position);
        position = lowered.indexOf(needle, position + Math.max(needle.length, 1));
      }
      if (positions.length >= 8) break;
    }
    const uniquePositions = [...new Set(positions)].sort((left, right) => left - right).slice(0, 5);
    const windowSize = Math.max(800, Math.floor(maxChars / Math.max(uniquePositions.length, 1)));
    const snippets = uniquePositions.map((position) => {
      const start = Math.max(0, position - Math.floor(windowSize / 3));
      return { offset: start, content: content.slice(start, start + windowSize) };
    });
    return {
      node,
      source_found: true,
      source: path.relative(this.graphSourceRoot, source),
      query,
      matches: snippets.length,
      snippets: snippets.length ? snippets : [{ offset: 0, content: content.slice(0, Math.min(maxChars, content.length)) }],
    };
  }

  modelPolicy() {
    if (!fs.existsSync(this.modelPolicyPath)) throw new Error(`Model policy не найден: ${this.modelPolicyPath}`);
    const policy = parseJson(fs.readFileSync(this.modelPolicyPath, "utf8"), null);
    if (!policy) throw new Error("Model policy содержит невалидный JSON");
    const skillVersions = {};
    for (const skill of policy.allowed_skills || []) skillVersions[skill] = this.skillVersion(skill);
    return { ...policy, skill_versions: skillVersions };
  }

  skillVersion(skill) {
    const name = safeId(skill, "Skill");
    const file = path.join(this.root, ".agents", "skills", name, "SKILL.md");
    if (!inside(path.join(this.root, ".agents", "skills"), file) || !fs.existsSync(file)) {
      throw new Error(`Skill не найден: ${name}`);
    }
    return `sha256:${sha256(fs.readFileSync(file)).slice(0, 16)}`;
  }

  runFile(runId, extension = "json") {
    return path.join(this.runRoot, `${safeId(runId, "Run ID")}.${extension}`);
  }

  loadRun(runId) {
    const file = this.runFile(runId);
    if (!fs.existsSync(file)) throw new Error(`Агентный запуск не найден: ${runId}`);
    return parseJson(fs.readFileSync(file, "utf8"), null);
  }

  appendRunEvent(runId, event) {
    const summary = this.loadRun(runId);
    const record = {
      sequence: Number(summary.event_count || 0) + 1,
      timestamp: isoNow(),
      ...event,
    };
    fs.mkdirSync(this.runRoot, { recursive: true });
    fs.appendFileSync(this.runFile(runId, "jsonl"), `${JSON.stringify(record)}\n`, "utf8");
    summary.event_count = record.sequence;
    summary.updated_at = record.timestamp;
    summary.last_event = record.event;
    const facts = collectJournalFacts(record);
    summary.selected_node_ids = [...new Set([...(summary.selected_node_ids || []), ...facts.node_ids])].slice(-500);
    summary.artifact_paths = [...new Set([...(summary.artifact_paths || []), ...facts.artifacts])].slice(-200);
    if (facts.revisions.size) summary.current_revision = Math.max(...facts.revisions);
    atomicJson(this.runFile(runId), summary);
    return record;
  }

  startAgentRun({ projectId = "", skill, provider, model, parameters = {}, requestSummary = "" }) {
    if (this.activeRunId) {
      const active = this.loadRun(this.activeRunId);
      if (active.status === "running") throw new Error(`Сначала завершите активный agent run: ${active.run_id}`);
      this.activeRunId = null;
    }
    const policy = this.modelPolicy();
    const cleanProvider = String(provider || "").trim();
    const cleanModel = String(model || "").trim();
    if (policy.reject_unreported_model && (UNREPORTED_MODELS.has(cleanProvider.toLocaleLowerCase("en-US"))
      || UNREPORTED_MODELS.has(cleanModel.toLocaleLowerCase("en-US")))) {
      throw new Error("Нужно указать фактические provider и model; unknown/default/host-selected запрещены model policy");
    }
    if (!(policy.allowed_skills || []).includes(skill)) throw new Error(`Skill не разрешён model policy: ${skill}`);
    if (!parameters || typeof parameters !== "object" || Array.isArray(parameters)) throw new Error("parameters должен быть объектом");
    if (projectId) safeId(projectId, "Project ID");
    const runId = `run-${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-${crypto.randomUUID()}`;
    const timestamp = isoNow();
    const summary = {
      schema_version: 1,
      run_id: runId,
      status: "running",
      project_id: projectId,
      provider: cleanProvider,
      model: cleanModel,
      parameters,
      skill,
      skill_version: policy.skill_versions[skill],
      policy_id: policy.policy_id,
      request_summary: String(requestSummary || "").trim().slice(0, 4_000),
      started_at: timestamp,
      updated_at: timestamp,
      event_count: 0,
      selected_node_ids: [],
      artifact_paths: [],
      current_revision: projectId && fs.existsSync(this.projectFile(projectId)) ? this.loadProjectState(projectId).revision : null,
    };
    atomicJson(this.runFile(runId), summary);
    this.activeRunId = runId;
    this.appendRunEvent(runId, { event: "run_started", skill, skill_version: summary.skill_version, provider: cleanProvider, model: cleanModel });
    return {
      run: this.loadRun(runId),
      provenance: {
        provider: cleanProvider,
        model: cleanModel,
        parameters,
        skill,
        skill_version: summary.skill_version,
        run_id: runId,
        policy_id: policy.policy_id,
      },
    };
  }

  recordToolCall(tool, input, output, durationMs, error = "") {
    this.recordProjectQueryToolCall(tool, durationMs, error);
    if (!this.activeRunId) return null;
    let summary;
    try {
      summary = this.loadRun(this.activeRunId);
    } catch {
      this.activeRunId = null;
      return null;
    }
    if (summary.status !== "running") {
      this.activeRunId = null;
      return null;
    }
    return this.appendRunEvent(summary.run_id, {
      event: "mcp_tool_call",
      tool,
      duration_ms: durationMs,
      success: !error,
      error: error ? String(error).slice(0, 4_000) : "",
      input: compactForJournal(input),
      output: compactForJournal(output),
    });
  }

  projectQueryDirectory(projectId) {
    return path.join(this.projectDirectory(projectId), "analysis", "queries");
  }

  projectQueryFile(projectId, queryId) {
    return path.join(this.projectQueryDirectory(projectId), `${safeId(queryId, "Query ID")}.json`);
  }

  loadProjectQuery(projectId, queryId) {
    const file = this.projectQueryFile(projectId, queryId);
    if (!fs.existsSync(file)) throw new Error(`Телеметрия вопроса не найдена: ${queryId}`);
    return parseJson(fs.readFileSync(file, "utf8"), null);
  }

  startProjectQuery(projectId, { question, metadata = {} } = {}) {
    const project = this.loadProjectState(projectId);
    if (this.activeProjectQuery) {
      const active = this.loadProjectQuery(this.activeProjectQuery.project_id, this.activeProjectQuery.query_id);
      if (active?.status === "running") throw new Error(`Сначала завершите активный вопрос проекта: ${active.query_id}`);
      this.activeProjectQuery = null;
    }
    const text = String(question || "").trim();
    if (!text) throw new Error("Текст вопроса к проекту пуст");
    const queryId = `query-${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-${crypto.randomUUID()}`;
    const record = {
      schema_version: 1,
      query_id: queryId,
      project_id: projectId,
      project_revision: project.revision,
      question: text.slice(0, 100_000),
      status: "running",
      started_at: isoNow(),
      finished_at: null,
      wall_time_ms: null,
      model_time_ms: 0,
      mcp_time_ms: 0,
      request_counts: { model: 0, mcp: 0, model_attempts: 0, failed_model: 0, failed_mcp: 0 },
      tokens: {
        input: 0,
        cached_input: 0,
        output: 0,
        reasoning: 0,
        total: 0,
        exact: true,
        source: "provider_usage",
        availability: "pending",
      },
      model_calls: [],
      mcp_calls: [],
      answer_path: "",
      error: "",
      metadata: metadata && typeof metadata === "object" && !Array.isArray(metadata) ? metadata : {},
    };
    atomicJson(this.projectQueryFile(projectId, queryId), record);
    this.activeProjectQuery = { project_id: projectId, query_id: queryId, started_ms: Date.now() };
    return record;
  }

  recordProjectQueryToolCall(tool, durationMs, error = "") {
    if (!this.activeProjectQuery || ["start_project_query", "finish_project_query", "get_project_telemetry"].includes(tool)) return null;
    const { project_id: projectId, query_id: queryId } = this.activeProjectQuery;
    let query;
    try { query = this.loadProjectQuery(projectId, queryId); } catch { return null; }
    if (query.status !== "running") return null;
    const duration = Math.max(0, Number(durationMs || 0));
    query.request_counts.mcp += 1;
    if (error) query.request_counts.failed_mcp += 1;
    query.mcp_time_ms += duration;
    query.mcp_calls.push({
      sequence: query.request_counts.mcp,
      timestamp: isoNow(),
      tool: String(tool || ""),
      duration_ms: duration,
      result: error ? "failed" : "completed",
      error: error ? String(error).slice(0, 4_000) : "",
    });
    atomicJson(this.projectQueryFile(projectId, queryId), query);
    return query.mcp_calls.at(-1);
  }

  recordModelCallTelemetry({
    projectId = "", queryId = "", provider, model, reasoningEffort = "", skillVersion = "",
    applicationVersion = "", graphVersion = "", graphHash = "", inputTokens = null,
    cachedInputTokens = null, outputTokens = null, reasoningTokens = null, durationMs = 0,
    attempt = 1, result = "completed", error = "", usageSource = "provider_response",
  }) {
    const active = this.activeProjectQuery;
    const resolvedProjectId = projectId || active?.project_id || "";
    const resolvedQueryId = queryId || active?.query_id || "";
    if (!resolvedProjectId || !resolvedQueryId) throw new Error("Нет активного вопроса проекта для модельной телеметрии");
    const query = this.loadProjectQuery(resolvedProjectId, resolvedQueryId);
    if (query.status !== "running") throw new Error("Телеметрия модели принимается только для активного вопроса");
    const numbers = [inputTokens, cachedInputTokens, outputTokens, reasoningTokens];
    const usageReported = numbers.every((value) => Number.isFinite(value) && Number(value) >= 0);
    const normalized = numbers.map((value) => usageReported ? Number(value) : null);
    const duration = Math.max(0, Number(durationMs || 0));
    const call = {
      sequence: query.request_counts.model_attempts + 1,
      timestamp: isoNow(),
      provider: String(provider || "unreported"),
      model: String(model || "unreported"),
      reasoning_effort: String(reasoningEffort || ""),
      skill_version: String(skillVersion || ""),
      application_version: String(applicationVersion || ""),
      graph_version: String(graphVersion || ""),
      graph_hash: String(graphHash || ""),
      input_tokens: normalized[0],
      cached_input_tokens: normalized[1],
      output_tokens: normalized[2],
      reasoning_tokens: normalized[3],
      usage_reported: usageReported,
      usage_source: usageReported ? String(usageSource || "provider_response") : "unavailable",
      duration_ms: duration,
      attempt: Math.max(1, Number.parseInt(String(attempt || 1), 10) || 1),
      result: String(result || "completed"),
      error: error ? String(error).slice(0, 4_000) : "",
    };
    query.model_calls.push(call);
    query.request_counts.model_attempts += 1;
    if (call.result === "completed") query.request_counts.model += 1;
    else query.request_counts.failed_model += 1;
    query.model_time_ms += duration;
    if (usageReported) {
      query.tokens.input += call.input_tokens;
      query.tokens.cached_input += call.cached_input_tokens;
      query.tokens.output += call.output_tokens;
      query.tokens.reasoning += call.reasoning_tokens;
      query.tokens.total += call.input_tokens + call.output_tokens;
    } else {
      query.tokens.exact = false;
    }
    query.tokens.availability = query.tokens.exact ? "exact" : "partially_unavailable";
    atomicJson(this.projectQueryFile(resolvedProjectId, resolvedQueryId), query);
    return call;
  }

  finishProjectQuery(projectId, queryId, { status = "completed", answerPath = "", error = "", metadata = {} } = {}) {
    const query = this.loadProjectQuery(projectId, queryId);
    if (query.status !== "running") return { query, changed: false, aggregate: this.aggregateProjectTelemetry(projectId) };
    const finished = Date.now();
    const started = Date.parse(query.started_at);
    query.status = status;
    query.finished_at = new Date(finished).toISOString();
    query.wall_time_ms = Math.max(0, finished - (Number.isFinite(started) ? started : finished));
    query.answer_path = String(answerPath || "").replaceAll("\\", "/");
    query.error = error ? String(error).slice(0, 8_000) : "";
    query.metadata = { ...(query.metadata || {}), ...(metadata && typeof metadata === "object" && !Array.isArray(metadata) ? metadata : {}) };
    if (!query.model_calls.length) {
      query.tokens.exact = false;
      query.tokens.availability = "unavailable_from_codex_host";
      query.tokens.source = "not_exposed_by_host";
    } else if (query.model_calls.some((call) => !call.usage_reported)) {
      query.tokens.exact = false;
      query.tokens.availability = "partially_unavailable";
    } else {
      query.tokens.exact = true;
      query.tokens.availability = "exact";
    }
    atomicJson(this.projectQueryFile(projectId, queryId), query);
    if (this.activeProjectQuery?.query_id === queryId) this.activeProjectQuery = null;
    return { query, changed: true, aggregate: this.aggregateProjectTelemetry(projectId) };
  }

  aggregateProjectTelemetry(projectId, limit = 100) {
    this.loadProjectState(projectId);
    const directory = this.projectQueryDirectory(projectId);
    const queries = fs.existsSync(directory)
      ? fs.readdirSync(directory, { withFileTypes: true })
        .filter((entry) => entry.isFile() && entry.name.endsWith(".json"))
        .map((entry) => parseJson(fs.readFileSync(path.join(directory, entry.name), "utf8"), null))
        .filter(Boolean)
        .sort((left, right) => String(right.started_at).localeCompare(String(left.started_at)))
      : [];
    const completed = queries.filter((query) => query.status !== "running");
    const report = {
      schema_version: 1,
      project_id: projectId,
      generated_at: isoNow(),
      questions: { total: queries.length, completed: completed.filter((item) => item.status === "completed").length, failed: completed.filter((item) => item.status === "failed").length, running: queries.filter((item) => item.status === "running").length },
      requests: {
        model: queries.reduce((sum, item) => sum + Number(item.request_counts?.model || 0), 0),
        model_attempts: queries.reduce((sum, item) => sum + Number(item.request_counts?.model_attempts || 0), 0),
        mcp: queries.reduce((sum, item) => sum + Number(item.request_counts?.mcp || 0), 0),
      },
      tokens: {
        input: queries.reduce((sum, item) => sum + Number(item.tokens?.input || 0), 0),
        cached_input: queries.reduce((sum, item) => sum + Number(item.tokens?.cached_input || 0), 0),
        output: queries.reduce((sum, item) => sum + Number(item.tokens?.output || 0), 0),
        reasoning: queries.reduce((sum, item) => sum + Number(item.tokens?.reasoning || 0), 0),
        exact_for_all_questions: queries.length > 0 && queries.every((item) => item.tokens?.exact === true),
        questions_with_unavailable_usage: queries.filter((item) => item.tokens?.exact !== true).length,
      },
      time: {
        wall_ms: completed.reduce((sum, item) => sum + Number(item.wall_time_ms || 0), 0),
        model_ms: queries.reduce((sum, item) => sum + Number(item.model_time_ms || 0), 0),
        mcp_ms: queries.reduce((sum, item) => sum + Number(item.mcp_time_ms || 0), 0),
      },
      recent_queries: queries.slice(0, Math.max(1, Math.min(500, limit))).map((item) => ({
        query_id: item.query_id,
        project_revision: item.project_revision,
        question: String(item.question || "").slice(0, 500),
        status: item.status,
        started_at: item.started_at,
        wall_time_ms: item.wall_time_ms,
        model_time_ms: item.model_time_ms,
        request_counts: item.request_counts,
        tokens: item.tokens,
        answer_path: item.answer_path,
      })),
    };
    atomicJson(path.join(this.projectDirectory(projectId), "analysis", "telemetry-report.json"), report);
    return report;
  }

  listAgentRuns({ projectId = "", limit = 50 } = {}) {
    if (!fs.existsSync(this.runRoot)) return [];
    const runs = fs.readdirSync(this.runRoot, { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name.endsWith(".json") && !entry.name.endsWith(".jsonl"))
      .map((entry) => parseJson(fs.readFileSync(path.join(this.runRoot, entry.name), "utf8"), null))
      .filter((item) => item && (!projectId || item.project_id === projectId));
    return runs.sort((left, right) => String(right.started_at).localeCompare(String(left.started_at))).slice(0, limit);
  }

  getAgentRun(runId, eventLimit = 200) {
    const run = this.loadRun(runId);
    const eventFile = this.runFile(runId, "jsonl");
    const events = fs.existsSync(eventFile)
      ? fs.readFileSync(eventFile, "utf8").trim().split(/\r?\n/u).filter(Boolean).slice(-eventLimit).map((line) => parseJson(line, {}))
      : [];
    return { run, events };
  }

  finishAgentRun(runId, { status = "completed", summary = "" } = {}) {
    const run = this.loadRun(runId);
    if (run.status !== "running") return { run, changed: false };
    this.appendRunEvent(runId, { event: "run_finished", status, summary: String(summary || "").slice(0, 8_000) });
    const updated = this.loadRun(runId);
    updated.status = status;
    updated.finished_at = isoNow();
    updated.result_summary = String(summary || "").slice(0, 8_000);
    atomicJson(this.runFile(runId), updated);
    if (this.activeRunId === runId) this.activeRunId = null;
    return { run: updated, changed: true };
  }

  validateProvenance(provenance, projectId = "") {
    const policy = this.modelPolicy();
    if (!provenance || typeof provenance !== "object" || Array.isArray(provenance)) throw new Error("Артефакт требует provenance согласно model policy");
    for (const field of policy.required_artifact_fields || []) {
      if (provenance[field] === undefined || provenance[field] === null || provenance[field] === "") throw new Error(`В provenance отсутствует ${field}`);
    }
    const run = this.loadRun(provenance.run_id);
    if (run.status !== "running") throw new Error("Артефакты можно сохранять только в активном agent run");
    if (projectId && run.project_id && run.project_id !== projectId) throw new Error("run_id относится к другому проекту");
    for (const field of ["provider", "model", "skill", "skill_version"]) {
      if (provenance[field] !== run[field]) throw new Error(`provenance.${field} не совпадает с журналом запуска`);
    }
    if (stableJson(provenance.parameters) !== stableJson(run.parameters)) throw new Error("provenance.parameters не совпадает с журналом запуска");
    if (provenance.skill_version !== policy.skill_versions[provenance.skill]) throw new Error("Версия skill изменилась после начала запуска; начните новый agent run");
    return { ...provenance, policy_id: policy.policy_id };
  }

  projectDirectory(projectId) {
    return path.join(this.taskRoot, safeId(projectId, "Project ID"));
  }

  projectFile(projectId) {
    return path.join(this.projectDirectory(projectId), "agent_project.json");
  }

  createProject({ title, requirements, projectId = "", source = "", product = "1C:ERP", release = "" }) {
    if (!requirements.trim()) throw new Error("Требования проекта пусты");
    if (requirements.length > MAX_PROJECT_TEXT) throw new Error(`Требования превышают ${MAX_PROJECT_TEXT} символов`);
    const hash = crypto.createHash("sha256").update(requirements).digest("hex").slice(0, 10);
    const id = projectId ? safeId(projectId, "Project ID") : `${slug(title)}-${hash}`;
    const file = this.projectFile(id);
    if (fs.existsSync(file)) return { created: false, project: this.projectSummary(this.loadProjectState(id)) };
    const timestamp = isoNow();
    const project = {
      schema_version: 3,
      architecture: "skill-mcp",
      project_id: id,
      title: title.trim() || id,
      status: "modeling",
      revision: 1,
      created_at: timestamp,
      updated_at: timestamp,
      specification: { source: source.trim(), text: requirements },
      product: String(product || "1C:ERP").trim(),
      release: String(release || "").trim(),
      decisions: [],
      decision_history: [],
      contexts: [],
      approvals: [],
      model_approved_revision: null,
      final_approved_revision: null,
      approval_state: { requirements: false, design: false, final: false },
      artifacts: [],
    };
    this.saveProjectState(project);
    const requestPath = path.join(this.projectDirectory(id), "00-request.md");
    if (!fs.existsSync(requestPath)) {
      atomicWrite(
        requestPath,
        `---\nartifact: "request"\nproject_id: "${id}"\ncreated_at: "${timestamp}"\n---\n\n# Исходный запрос: ${project.title}\n\n${requirements.trim()}\n`,
      );
    }
    const pythonState = path.join(this.projectDirectory(id), "project.yaml");
    if (!fs.existsSync(pythonState)) atomicWrite(pythonState, pythonProjectYaml(project));
    return { created: true, project: this.projectSummary(project) };
  }

  projectSummary(project) {
    return {
      project_id: project.project_id,
      title: project.title,
      status: project.status,
      revision: project.revision,
      decisions: project.decisions?.length || 0,
      approved: project.model_approved_revision === project.revision,
      final_approved: project.final_approved_revision === project.revision,
      updated_at: project.updated_at,
    };
  }

  loadProjectState(projectId) {
    const file = this.projectFile(projectId);
    if (!fs.existsSync(file)) throw new Error(`Skill/MCP-проект не найден: ${projectId}`);
    return parseJson(fs.readFileSync(file, "utf8"), null);
  }

  saveProjectState(project) {
    project.updated_at = isoNow();
    atomicJson(this.projectFile(project.project_id), project);
    atomicJson(path.join(this.projectDirectory(project.project_id), "agent_project_summary.json"), this.projectSummary(project));
    const pythonState = path.join(this.projectDirectory(project.project_id), "project.yaml");
    if (fs.existsSync(pythonState)) {
      const current = fs.readFileSync(pythonState, "utf8");
      if (/^managed_by:\s*["']?newagent-mcp["']?\s*$/mu.test(current)) {
        atomicWrite(pythonState, pythonProjectYaml(project));
      }
    }
  }

  listProjects(limit = 50) {
    if (!fs.existsSync(this.taskRoot)) return [];
    const projects = [];
    for (const entry of fs.readdirSync(this.taskRoot, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const directory = path.join(this.taskRoot, entry.name);
      const file = path.join(directory, "agent_project.json");
      if (!fs.existsSync(file)) continue;
      const summaryFile = path.join(directory, "agent_project_summary.json");
      const project = parseJson(fs.readFileSync(fs.existsSync(summaryFile) ? summaryFile : file, "utf8"), {});
      projects.push({
        project_id: project.project_id || entry.name,
        title: project.title || entry.name,
        status: project.status,
        revision: project.revision,
        decisions: typeof project.decisions === "number" ? project.decisions : project.decisions?.length || 0,
        approved: typeof project.approved === "boolean"
          ? project.approved
          : project.model_approved_revision === project.revision,
        updated_at: project.updated_at,
      });
    }
    return projects.sort((left, right) => String(right.updated_at).localeCompare(String(left.updated_at))).slice(0, limit);
  }

  getProject(projectId) {
    const project = this.loadProjectState(projectId);
    return {
      ...project,
      specification: {
        ...project.specification,
        text: String(project.specification?.text || "").slice(0, MAX_PROJECT_TEXT),
      },
    };
  }

  readArtifact(projectId, { kind = "", artifactPath = "", maxChars = 100_000 }) {
    const project = this.loadProjectState(projectId);
    let artifact;
    if (artifactPath) {
      artifact = [...project.artifacts].reverse().find((item) => item.path === artifactPath);
    } else if (kind) {
      artifact = [...project.artifacts].reverse().find((item) => item.kind === kind);
    } else {
      throw new Error("Укажите kind или artifact_path");
    }
    if (!artifact) throw new Error("Артефакт проекта не найден");
    const target = path.join(this.projectDirectory(projectId), artifact.path);
    if (!inside(this.projectDirectory(projectId), target) || !fs.existsSync(target)) {
      throw new Error("Файл артефакта проекта не найден или вышел за границы проекта");
    }
    const content = fs.readFileSync(target, "utf8");
    return {
      artifact,
      truncated: content.length > maxChars,
      content: content.slice(0, maxChars),
    };
  }

  latestArtifact(project, kind) {
    return [...(project.artifacts || [])].reverse().find((item) => item.kind === kind) || null;
  }

  artifactJson(projectId, project, kind) {
    const artifact = this.latestArtifact(project, kind);
    if (!artifact) return null;
    const target = path.join(this.projectDirectory(projectId), artifact.path);
    if (!inside(this.projectDirectory(projectId), target) || !fs.existsSync(target)) return null;
    return parseJson(fs.readFileSync(target, "utf8"), null);
  }

  invalidateApprovals(project) {
    project.model_approved_revision = null;
    project.final_approved_revision = null;
    project.approval_state = { requirements: false, design: false, final: false };
    project.status = "modeling";
  }

  recordDecisions(projectId, decisions) {
    const project = this.loadProjectState(projectId);
    const timestamp = isoNow();
    for (const input of decisions) {
      const question = String(input.question || "").trim();
      const answer = String(input.answer || "").trim();
      if (!question || !answer) throw new Error("Каждое решение должно содержать question и answer");
      const allowed = (input.allowed_values || []).map((item) => String(item).trim()).filter(Boolean);
      const normalizedValue = String(input.normalized_value || answer).trim();
      if (allowed.length && !allowed.some((item) => item.toLocaleLowerCase("ru-RU") === normalizedValue.toLocaleLowerCase("ru-RU"))) {
        throw new Error(
          `Ответ на вопрос ${input.question_id || input.id || ""} неоднозначен: укажите ровно один допустимый вариант (${allowed.join(" | ")})`,
        );
      }
      const decisionId = input.id
        ? safeId(input.id, "Decision ID")
        : crypto.createHash("sha256").update(question).digest("hex").slice(0, 16);
      const previous = project.decisions.find((item) => item.id === decisionId);
      if (previous) project.decision_history.push({ ...previous, superseded_at: timestamp });
      const decision = {
        id: decisionId,
        question_id: String(input.question_id || input.id || decisionId),
        question,
        answer,
        exact_user_answer: answer,
        normalized_value: normalizedValue,
        rationale: String(input.rationale || "").trim(),
        affected_requirement_ids: [...new Set((input.affected_requirement_ids || []).map(String))].slice(0, 500),
        affected_node_ids: [...new Set((input.affected_node_ids || []).map(String))].slice(0, 100),
        revision: Number(project.revision || 1) + 1,
        recorded_at: timestamp,
      };
      project.decisions = project.decisions.filter((item) => item.id !== decisionId);
      project.decisions.push(decision);
    }
    project.revision += 1;
    this.invalidateApprovals(project);
    this.saveProjectState(project);
    this.updateExampleRegistry(projectId);
    return { project_id: projectId, revision: project.revision, decisions: project.decisions.length };
  }

  addContext(projectId, { mode, text, supersedesIds = [] }) {
    const project = this.loadProjectState(projectId);
    if (!text.trim()) throw new Error("Контекст пуст");
    const timestamp = isoNow();
    if (mode === "correction") {
      const superseded = new Set(supersedesIds.map(String));
      for (const context of project.contexts) {
        if (superseded.has(context.id)) {
          context.active = false;
          context.superseded_at = timestamp;
        }
      }
    }
    const context = {
      id: crypto.randomUUID(),
      mode,
      text: text.trim(),
      supersedes_ids: supersedesIds.map(String),
      active: true,
      recorded_at: timestamp,
    };
    project.contexts.push(context);
    project.revision += 1;
    this.invalidateApprovals(project);
    this.saveProjectState(project);
    this.updateExampleRegistry(projectId);
    return { project_id: projectId, revision: project.revision, context };
  }

  recordApproval(projectId, { approved, note = "", stage = "design" }) {
    const project = this.loadProjectState(projectId);
    if (!["requirements", "design", "final"].includes(stage)) throw new Error(`Неизвестный этап approval: ${stage}`);
    project.approval_state ||= { requirements: false, design: false, final: false };
    if (approved && stage === "design") {
      for (const kind of ["solution-model", "traceability", "quality-gate"]) {
        if (!this.latestArtifact(project, kind)) throw new Error(`Для design approval отсутствует ${kind}`);
      }
      const gate = this.artifactJson(projectId, project, "quality-gate");
      if (!gate || gate.ready_for_approval === false || gate.ready_for_design_approval === false) {
        throw new Error("Quality gate не разрешает утверждение проекта решения");
      }
    }
    if (approved && stage === "final") {
      if (!project.approval_state.design) throw new Error("Сначала требуется design approval");
      const report = this.validateProject(projectId, { forFinal: true, persist: true });
      if (!report.passed) throw new Error(`Финальный approval заблокирован: ${report.errors.slice(0, 5).join("; ")}`);
    }
    project.revision += 1;
    const approval = {
      id: crypto.randomUUID(),
      approved: Boolean(approved),
      stage,
      note: String(note).trim(),
      revision: project.revision,
      recorded_at: isoNow(),
    };
    project.approvals.push(approval);
    project.approval_state[stage] = Boolean(approved);
    if (!approved) {
      if (stage === "requirements") project.approval_state = { requirements: false, design: false, final: false };
      if (stage === "design") Object.assign(project.approval_state, { design: false, final: false });
      if (stage === "final") project.approval_state.final = false;
    }
    project.model_approved_revision = project.approval_state.design ? project.revision : null;
    project.final_approved_revision = project.approval_state.final ? project.revision : null;
    project.status = project.approval_state.final
      ? "successful"
      : project.approval_state.design ? "approved" : "modeling";
    this.saveProjectState(project);
    this.updateExampleRegistry(projectId);
    return { project_id: projectId, revision: project.revision, approval };
  }

  saveArtifact(projectId, { kind, content, title = "", metadata = {}, provenance }) {
    const project = this.loadProjectState(projectId);
    const safeKind = safeId(kind, "Artifact kind").toLocaleLowerCase("en-US");
    project.approval_state ||= { requirements: false, design: false, final: false };
    if (safeKind === "answer" && !project.approval_state.design) {
      throw new Error("Финальный ответ нельзя сохранить до явного подтверждения проекта решения");
    }
    const validatedProvenance = this.validateProvenance(provenance, projectId);
    const extension = typeof content === "string" ? "md" : "json";
    const timestamp = isoNow().replace(/[:.]/g, "-");
    const relative = safeKind === "answer"
      ? path.join("answers_md", `${timestamp}-${slug(title || "answer", 64)}.${extension}`)
      : path.join("agent_artifacts", `${safeKind}.r${project.revision}.${extension}`);
    const target = path.join(this.projectDirectory(projectId), relative);
    const serialized = typeof content === "string" ? `${content.trim()}\n` : `${JSON.stringify(content, null, 2)}\n`;
    atomicWrite(target, serialized);
    if (safeKind !== "answer") {
      atomicWrite(path.join(this.projectDirectory(projectId), "agent_artifacts", `${safeKind}.latest.${extension}`), serialized);
    }
    const artifact = {
      id: crypto.randomUUID(),
      kind: safeKind,
      title: title.trim(),
      path: relative.replaceAll("\\", "/"),
      revision: project.revision,
      metadata,
      provenance: validatedProvenance,
      saved_at: isoNow(),
    };
    project.artifacts.push(artifact);
    if (safeKind === "answer") {
      project.status = "feedback_pending";
      project.approval_state.final = false;
      project.final_approved_revision = null;
    } else if (["requirement-map", "evidence-map", "questions", "solution-model", "traceability", "quality-gate", "gap-register", "acceptance-tests"].includes(safeKind)
      && (project.approval_state.design || project.approval_state.final)) {
      this.invalidateApprovals(project);
    }
    this.saveProjectState(project);
    this.appendRunEvent(validatedProvenance.run_id, {
      event: "artifact_saved",
      project_id: projectId,
      revision: project.revision,
      artifact: { kind: safeKind, path: artifact.path, id: artifact.id },
    });
    return { project_id: projectId, artifact };
  }

  validateProject(projectId, { forFinal = false, persist = true } = {}) {
    const project = this.loadProjectState(projectId);
    const errors = [];
    const warnings = [];
    const requirementMap = this.artifactJson(projectId, project, "requirement-map");
    const evidenceMap = this.artifactJson(projectId, project, "evidence-map");
    const solutionModel = this.artifactJson(projectId, project, "solution-model");
    const traceability = this.artifactJson(projectId, project, "traceability");
    const qualityGate = this.artifactJson(projectId, project, "quality-gate");
    const acceptanceArtifact = this.artifactJson(projectId, project, "acceptance-tests");
    const gapArtifact = this.artifactJson(projectId, project, "gap-register");
    const requirements = requirementMap?.requirements || [];
    const evidence = evidenceMap?.evidence || evidenceMap?.claims || [];
    const links = traceability?.links || traceability?.rows || [];
    const acceptanceTests = acceptanceArtifact?.tests || traceability?.acceptance_tests || [];
    const gaps = gapArtifact?.gaps || qualityGate?.gaps || [];
    if (!requirements.length) errors.push("requirement-map не содержит требований");
    for (const kind of ["evidence-map", "solution-model", "traceability", "quality-gate"]) {
      if (!this.latestArtifact(project, kind)) errors.push(`отсутствует ${kind}`);
    }
    const requirementIds = requirements.map((item) => String(item.id || item.requirement_id || "")).filter(Boolean);
    if (new Set(requirementIds).size !== requirementIds.length) errors.push("ID требований не уникальны");
    const linkByRequirement = new Map(links.map((item) => [String(item.requirement_id || ""), item]));
    const gapsByRequirement = new Set(gaps.flatMap((item) => item.requirement_ids || []));
    const testsByRequirement = new Set(acceptanceTests.flatMap((item) => item.requirement_ids || []));
    const solutionIds = new Set([
      ...(solutionModel?.elements || []).map((item) => String(item.id || "")),
      ...(solutionModel?.processes || []).flatMap((process) => (process.steps || []).map((item) => String(item.id || ""))),
    ].filter(Boolean));
    for (const requirement of requirements) {
      const id = String(requirement.id || requirement.requirement_id || "");
      if (!id) { errors.push("требование без стабильного ID"); continue; }
      const coverage = String(requirement.coverage_status || requirement.status || "gap");
      const link = linkByRequirement.get(id);
      if (!link) errors.push(`${id}: отсутствует traceability`);
      const linkedSolutions = link?.solution_element_ids || link?.solution_elements || requirement.solution_element_ids || [];
      if (coverage === "covered" && !linkedSolutions.some((item) => solutionIds.has(String(item)))) {
        errors.push(`${id}: covered без элемента решения`);
      }
      const linkedTests = link?.acceptance_test_ids || requirement.acceptance_test_ids || [];
      if (coverage === "covered" && !linkedTests.length && !testsByRequirement.has(id)) {
        errors.push(`${id}: covered без приёмочного теста`);
      }
      if (coverage === "gap" && !gapsByRequirement.has(id)) errors.push(`${id}: gap без записи GAP`);
    }
    const allowedEvidence = new Set(["verified_source", "verified_metadata", "user_decision", "candidate", "unresolved"]);
    const manifestPath = path.join(this.root, "1c_modeler_upgrade", "graphs", "graph_manifest.json");
    if (this.modelerManifest === null) {
      this.modelerManifest = fs.existsSync(manifestPath)
        ? parseJson(fs.readFileSync(manifestPath, "utf8"), {})
        : {};
    }
    const exactRelease = String(this.modelerManifest.release || "");
    const routePath = path.join(this.root, "1c_modeler_upgrade", "graphs", "1c_erp_2_5_route_graph.json");
    if (this.routeGraphIds === null) {
      const routeGraph = fs.existsSync(routePath)
        ? parseJson(fs.readFileSync(routePath, "utf8"), {})
        : {};
      this.routeGraphIds = new Set([
        ...Object.keys(routeGraph.nodes || {}),
        ...Object.values(routeGraph.nodes || {}).map((item) => String(item?.id || "")),
      ].filter(Boolean));
    }
    for (const item of evidence) {
      const status = String(item.status || item.evidence_status || "unresolved");
      if (!allowedEvidence.has(status)) errors.push(`недопустимый статус доказательства: ${status}`);
      if (["verified_source", "verified_metadata"].includes(status) && !String(item.source_ref || "").trim()) {
        errors.push(`${item.id || "evidence"}: verified без source_ref`);
      }
      if (status === "verified_metadata" && exactRelease && project.release && project.release !== exactRelease) {
        errors.push(`${item.id || "evidence"}: XML-evidence релиза ${exactRelease} нельзя использовать для ${project.release}`);
      }
      const objectRef = String(item.object_ref || item.object || "").trim();
      if (status === "verified_metadata" && objectRef && !this.resolveNodeIds([objectRef]).length) {
        errors.push(`${item.id || "evidence"}: неизвестный ERP-объект ${objectRef}`);
      }
      const routeRef = String(item.route_ref || item.route || "").trim();
      if (["verified_source", "verified_metadata"].includes(status) && routeRef
        && !this.routeGraphIds.has(routeRef)) {
        errors.push(`${item.id || "evidence"}: неразрешённый пользовательский маршрут ${routeRef}`);
      }
    }
    const openCritical = gaps.filter((item) => String(item.criticality || "").toLowerCase() === "critical"
      && !["closed", "excluded"].includes(String(item.status || "open").toLowerCase()));
    if (openCritical.length) errors.push(`открытые critical GAP: ${openCritical.map((item) => item.id || "без ID").join(", ")}`);
    if (qualityGate && (qualityGate.ready_for_approval === false || qualityGate.ready_for_design_approval === false)) {
      errors.push("quality-gate не готов к approval");
    }
    if (forFinal) {
      if (!project.approval_state?.design) errors.push("нет design approval");
      if (!this.latestArtifact(project, "answer")) errors.push("итоговый ответ не сохранён в answers_md");
    }
    if (!acceptanceTests.length) warnings.push("отдельный acceptance-tests ещё не сохранён");
    const report = {
      schema_version: 1,
      project_id: projectId,
      revision: project.revision,
      checked_at: isoNow(),
      mode: forFinal ? "final" : "design",
      passed: errors.length === 0,
      errors,
      warnings,
      summary: {
        requirements: requirements.length,
        trace_links: links.length,
        evidence: evidence.length,
        acceptance_tests: acceptanceTests.length,
        gaps: gaps.length,
        open_critical_gaps: openCritical.length,
      },
    };
    if (persist) atomicJson(path.join(this.projectDirectory(projectId), "analysis", "modeler-report.json"), report);
    return report;
  }

  updateExampleRegistry(projectId) {
    const index = path.join(this.root, "examples", "approved", "index.ndjson");
    const preserved = [];
    if (fs.existsSync(index)) {
      for (const line of fs.readFileSync(index, "utf8").split(/\r?\n/u).filter(Boolean)) {
        const record = parseJson(line, null);
        // Preserve malformed or unrelated user records byte-for-byte.
        if (!record || record.project_id !== projectId) preserved.push(line);
      }
    }
    const project = this.loadProjectState(projectId);
    if (project.status === "successful" && project.final_approved_revision === project.revision) {
      const answer = this.latestArtifact(project, "answer");
      if (answer) preserved.push(JSON.stringify({
        project_id: project.project_id,
        title: project.title,
        product: project.product || "",
        release: project.release || "",
        instruction_path: path.relative(this.root, path.join(this.projectDirectory(projectId), answer.path)).replaceAll("\\", "/"),
        approved_at: project.updated_at,
        revision: project.revision,
        status: "successful",
      }));
    }
    atomicWrite(index, preserved.join("\n") + (preserved.length ? "\n" : ""));
    return { project_id: projectId, registered: project.status === "successful" };
  }
}
