#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { ErpGraphStore } from "./erp-graph-store.mjs";
import { ErpApiAgents } from "./erp-api-agents.mjs";

const store = new ErpGraphStore();
try {
  const status = store.status();
  status.api_agents = new ErpApiAgents(store).keyStatus();
  status.codex = {
    config: `${store.root}\\.codex\\config.toml`,
    config_ready: fs.existsSync(path.join(store.root, ".codex", "config.toml")),
    skills_ready: ["erp-graph-research", "erp-requirements-modeling", "erp-solution-authoring"]
      .every((name) => fs.existsSync(path.join(store.root, ".agents", "skills", name, "SKILL.md"))),
    project_root: store.taskRoot,
    answers_policy: "results/<project>/answers_md",
  };
  console.log(JSON.stringify(status, null, 2));
  if (!status.ready || status.stale || !status.api_agents.configured || !status.codex.config_ready || !status.codex.skills_ready) process.exitCode = 1;
} finally {
  store.close();
}
