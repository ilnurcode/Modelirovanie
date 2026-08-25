from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from consultant_cli.infrastructure.store import atomic_write_text


class AtomicWriteTest(unittest.TestCase):
    def test_retries_temporary_windows_replace_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "state.json"
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("temporary lock")
                real_replace(source, destination)

            with mock.patch(
                "consultant_cli.infrastructure.store.os.replace",
                side_effect=flaky_replace,
            ), mock.patch("consultant_cli.infrastructure.store.time.sleep") as sleep:
                atomic_write_text(target, "ready\n")

            self.assertEqual("ready\n", target.read_text(encoding="utf-8"))
            self.assertEqual(2, attempts)
            sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
