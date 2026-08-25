from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.graph_search import GraphSearchService


class PortableRuntimeTests(unittest.TestCase):
    def test_installed_data_and_graph_survive_application_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            application = root / "app" / "4.1.0"
            data = root / "data"
            graph = data / "graphs" / "erp-2.5.27.49" / "2.5.27.49" / "0.5.0"
            database = graph / "graph_rag_data" / "erp_graph_mcp.sqlite"
            database.parent.mkdir(parents=True)
            database.touch()
            state = {
                "graphs": {
                    "erp-2.5.27.49": {
                        "path": str(graph),
                        "installed_at": "2026-08-25T00:00:00Z",
                    }
                }
            }
            (data / "config").mkdir(parents=True)
            (data / "config" / "installed.json").write_text(
                json.dumps(state), encoding="utf-8"
            )

            with patch.dict(os.environ, {"CONSULTANT_DATA_DIR": str(data)}):
                paths = RepositoryPaths(application)
                self.assertEqual(data.resolve() / "results", paths.results)
                self.assertEqual(
                    data.resolve() / "config" / "consultant.local.toml",
                    paths.local_config,
                )
                self.assertEqual(graph.resolve(), paths.modeler_graphs())
                self.assertEqual(database.resolve(), GraphSearchService(paths).database_path)


if __name__ == "__main__":
    unittest.main()
