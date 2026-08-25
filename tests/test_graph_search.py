from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.graph_search import GraphSearchService
from tests.helpers import make_repository


class GraphSearchTest(unittest.TestCase):
    def test_hybrid_search_and_typed_expansion_preserve_source_ref(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_repository(root)
            data = root / "graph_rag_data"
            data.mkdir()
            database = data / "erp_graph_mcp.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE graph_meta(key TEXT PRIMARY KEY, value TEXT);
                    CREATE TABLE nodes(
                      node_pk INTEGER PRIMARY KEY, id TEXT, canonical_id TEXT, title TEXT,
                      path TEXT, layer INTEGER, node_type TEXT, level INTEGER,
                      preview TEXT, metadata_json TEXT
                    );
                    CREATE VIRTUAL TABLE nodes_fts USING fts5(title, preview, path, node_type);
                    CREATE TABLE semantic_terms(term_idx INTEGER, term TEXT, idf REAL);
                    CREATE TABLE semantic_postings(term_idx INTEGER, node_pk INTEGER, weight REAL);
                    CREATE TABLE edges(
                      source_pk INTEGER, target_pk INTEGER, relation TEXT, edge_key TEXT,
                      weight REAL, properties_json TEXT, evidence_json TEXT
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO graph_meta VALUES (?,?)",
                    [("schema_version", "3"), ("nodes", "2"), ("edges", "1"),
                     ("semantic_postings", "2"), ("product", "1c-erp"),
                     ("product_version", "2.5")],
                )
                nodes = [
                    (1, "l4-order", "onec:l4", "Заказ клиента", "its/order", 4, "knowledge", 1,
                     "Заказ клиента запускает процесс", "{}"),
                    (2, "ERPcode/Documents/ЗаказКлиента", "onec:l3", "Документ Заказ клиента", "ERPcode/Documents/ЗаказКлиента", 3,
                     "document", 1, "Документ метаданных", json.dumps({"metadata_type": "Document", "source_xml": "ERPcode/ЗаказКлиента.xml"})),
                ]
                connection.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?)", nodes)
                connection.executemany(
                    "INSERT INTO nodes_fts(rowid,title,preview,path,node_type) VALUES (?,?,?,?,?)",
                    [(row[0], row[3], row[8], row[4], row[6]) for row in nodes],
                )
                connection.executemany(
                    "INSERT INTO semantic_terms VALUES (?,?,?)",
                    [(1, "заказ", 1.0), (2, "клиента", 1.0)],
                )
                connection.executemany(
                    "INSERT INTO semantic_postings VALUES (?,?,?)",
                    [(1, 1, 0.7), (2, 1, 0.7)],
                )
                connection.execute(
                    "INSERT INTO edges VALUES (?,?,?,?,?,?,?)",
                    (1, 2, "describes_metadata", "edge-1", 1.0, "{}",
                    json.dumps({"source_path": "its/order.md"})),
                )
                connection.commit()
            finally:
                connection.close()
            service = GraphSearchService(RepositoryPaths(root))
            result = service.search("заказ клиента", limit=2)
            self.assertEqual("l4-order", result["results"][0]["id"])
            self.assertIn("semantic", result["results"][0]["channels"])
            expanded = service.expand(["l4-order"])
            self.assertEqual("describes_metadata", expanded["edges"][0]["relation"])
            self.assertEqual("its/order.md", expanded["edges"][0]["source_ref"])

            payload = {
                "document_flow": [
                    {
                        "documents": [
                            {"name": "Заказ клиента", "node_id": "ERPcode/Documents/ЗаказКлиента"}
                        ]
                    }
                ]
            }
            repairs = service.normalize_document_flow(payload)
            self.assertEqual(
                "Документ Заказ клиента",
                payload["document_flow"][0]["documents"][0]["name"],
            )
            self.assertEqual(2, len(repairs))
            self.assertEqual([], service.document_flow_errors(payload))
            self.assertEqual(1, len(payload["sources"]))
            self.assertEqual(
                "ERPcode/Documents/ЗаказКлиента",
                payload["sources"][0]["node_id"],
            )

            alias_payload = {
                "document_flow": [
                    {
                        "documents": [
                            {
                                "name": "Заказ клиента",
                                "node_id": "Document.ЗаказКлиента",
                                "evidence_refs": [],
                            }
                        ]
                    }
                ],
                "sources": [],
            }
            service.normalize_document_flow(alias_payload)
            self.assertEqual(
                "ERPcode/Documents/ЗаказКлиента",
                alias_payload["document_flow"][0]["documents"][0]["node_id"],
            )
            self.assertEqual([], service.document_flow_errors(alias_payload))

            query_payload = {
                "document_flow": [
                    {
                        "title": "Настройки и первый документ",
                        "documents": [
                            {
                                "name": "Склады и магазины",
                                "node_id": "design:P04",
                                "evidence_refs": [],
                            },
                            {
                                "name": "Заказ клиента (первый документ продаж)",
                                "node_id": "design:P01",
                                "evidence_refs": [],
                            },
                        ],
                    }
                ],
                "sources": [],
            }
            service.normalize_document_flow(query_payload)
            pruned = service.prune_non_document_flow(query_payload)
            self.assertTrue(pruned)
            self.assertEqual(1, len(query_payload["document_flow"][0]["documents"]))
            self.assertEqual(
                "ERPcode/Documents/ЗаказКлиента",
                query_payload["document_flow"][0]["documents"][0]["node_id"],
            )
            self.assertEqual([], service.document_flow_errors(query_payload))

            first_context = service.project_context("demo", "заказ клиента", 1)
            service.search = lambda *_args, **_kwargs: self.fail("cache was not reused")
            service.expand = lambda *_args, **_kwargs: self.fail("cache was not reused")
            second_context = service.project_context("demo", "заказ клиента", 1)
            self.assertEqual(first_context, second_context)


if __name__ == "__main__":
    unittest.main()
