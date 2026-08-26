#!/usr/bin/env node

import { ErpGraphStore } from "../mcp/erp-graph-store.mjs";

const store = new ErpGraphStore();
try {
  const status = store.status();
  const queries = [
    "выпуск продукции производство хлеба",
    "заказ клиента отгрузка",
    "оплата после отгрузки расчеты с клиентом",
  ];
  const searches = queries.map((query) => {
    const response = store.search({ query, mode: "hybrid", strategy: "any", limit: 5 });
    return {
      query,
      count: response.count,
      nodes: response.results.map((item) => ({ id: item.id, layer: item.layer, title: item.title })),
    };
  });
  const report = { ready: status.ready && !status.stale, product: status.product, release: status.product_version, searches };
  console.log(JSON.stringify(report, null, 2));
  if (!report.ready || searches.some((item) => item.count === 0)) process.exitCode = 1;
} finally {
  store.close();
}
