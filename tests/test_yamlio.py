from __future__ import annotations

import unittest

from consultant_cli.infrastructure import yamlio


class YamlIoTest(unittest.TestCase):
    def test_round_trip_nested_mapping(self):
        source = {
            "project_id": "test-project",
            "enabled": True,
            "revision": 2,
            "configuration": {"product": "1С:ERP", "release": "2.5.27.49"},
            "outputs": ["markdown", "json"],
        }
        self.assertEqual(source, yamlio.loads(yamlio.dumps(source)))


if __name__ == "__main__":
    unittest.main()

