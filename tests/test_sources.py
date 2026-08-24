from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from consultant_cli.domain.models import ConfigurationInfo
from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.sources import SourceRouter
from tests.helpers import make_repository


class SourceRouterTest(unittest.TestCase):
    def route(self, product: str, release: str):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        make_repository(root)
        return SourceRouter(RepositoryPaths(root)).route(
            ConfigurationInfo(product=product, release=release), "закупка"
        )

    def test_exact_release_enables_xml(self):
        route = self.route("1С:ERP Управление предприятием 2", "2.5.27.49")
        self.assertEqual("exact", route.compatibility)
        self.assertTrue(route.use_xml)
        self.assertFalse(route.web_search_required)

    def test_other_erp_release_disables_xml(self):
        route = self.route("1С:ERP Управление предприятием 2", "2.5.28.1")
        self.assertEqual("product_only", route.compatibility)
        self.assertFalse(route.use_xml)
        self.assertTrue(route.web_search_required)

    def test_non_erp_disables_erp_sources(self):
        route = self.route("1С:Бухгалтерия предприятия", "3.0")
        self.assertEqual("different_product", route.compatibility)
        self.assertFalse(route.use_xml)
        self.assertTrue(route.external_docs_required)


if __name__ == "__main__":
    unittest.main()
