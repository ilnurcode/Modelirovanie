from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from consultant_cli.domain.models import ConfigurationInfo, Project, ProjectMode
from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.modeler import ModelerReviewService


class ModelerReviewTest(unittest.TestCase):
    def test_context_reads_compact_index_from_each_graph(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph_dir = root / "1c_modeler_upgrade" / "graphs"
            graph_dir.mkdir(parents=True)
            index = graph_dir / "search-index.ndjson.gz"
            with gzip.open(index, "wt", encoding="utf-8") as stream:
                for graph in ("source", "object", "route", "semantic"):
                    stream.write(
                        json.dumps(
                            {
                                "graph": graph,
                                "configuration": "1С:ERP 2.5",
                                "release": "",
                                "id": f"{graph}.Закупка",
                                "label": "Закупка",
                                "type": "Test",
                                "properties": {},
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            project = Project(
                "test",
                "Test",
                ProjectMode.FULL,
                configuration=ConfigurationInfo("1С:ERP", "", "2.5.27.49"),
            )

            context = ModelerReviewService(RepositoryPaths(root)).context(
                project, "процесс закупки"
            )

            for graph in ("source", "object", "route", "semantic"):
                self.assertIn(f'"{graph}"', context)
                self.assertIn(f'"{graph}.Закупка"', context)

    def test_unconfirmed_release_never_verifies_matching_route(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph_dir = root / "1c_modeler_upgrade" / "graphs"
            graph_dir.mkdir(parents=True)
            (graph_dir / "graph_manifest.json").write_text(
                json.dumps({"configuration": "1С:ERP 2.5", "graphs": []}),
                encoding="utf-8",
            )
            (graph_dir / "1c_erp_2_5_route_graph.json").write_text(
                json.dumps(
                    {
                        "configuration": "1С:ERP 2.5",
                        "status": "ГОТОВ",
                        "nodes": {
                            "r1": {
                                "id": "r1",
                                "label": "Заказ поставщику",
                                "type": "InterfaceRoute",
                                "properties": {
                                    "path": "Закупки → Создать → Заказ поставщику",
                                    "source_path": "C:/missing/interface.md",
                                },
                            }
                        },
                        "edges": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (graph_dir / "1c_erp_2_5_semantic_graph.json").write_text(
                json.dumps({"status": "НЕПОЛНЫЙ", "nodes": {}, "edges": []}),
                encoding="utf-8",
            )
            project = Project(
                "test",
                "Test",
                ProjectMode.FULL,
                configuration=ConfigurationInfo(
                    "1С:ERP Управление предприятием 2", "2.5", "2.5.27.49"
                ),
            )
            result = {
                "steps": [
                    {
                        "id": "P01",
                        "ui_path": "Закупки → Создать → Заказ поставщику",
                    }
                ]
            }

            report = ModelerReviewService(RepositoryPaths(root)).review(project, result)

            self.assertEqual("product_only", report["compatibility"])
            self.assertEqual("inferred", report["path_checks"][0]["status"])
            self.assertEqual("review_required", report["verdict"])


if __name__ == "__main__":
    unittest.main()
