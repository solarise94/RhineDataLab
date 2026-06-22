import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.patch_apply import PatchApplyService
from app.services.utils import atomic_write_bytes


class AtomicWriteBytesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="atomic-write-bytes-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_bytes_verbatim_without_reencoding(self) -> None:
        # Snapshot payloads are already-serialized bytes; they must be written
        # byte-for-byte, including bytes that JSON re-encoding would corrupt.
        target = self.tmpdir / "graph" / "graph.json"
        payload = b'{"k": "v"}\n\x00\x01\x02'
        atomic_write_bytes(target, payload)
        self.assertEqual(target.read_bytes(), payload)
        # No temp residue left behind.
        self.assertEqual(list(target.parent.glob(".*.tmp")), [])

    def test_failed_rename_leaves_original_untouched(self) -> None:
        target = self.tmpdir / "graph.json"
        target.write_bytes(b"ORIGINAL")
        with mock.patch(
            "app.services.utils.os.replace",
            side_effect=OSError("No space left on device"),
        ):
            with self.assertRaises(OSError):
                atomic_write_bytes(target, b"REPLACEMENT")
        # The original file is preserved verbatim, not truncated/half-written.
        self.assertEqual(target.read_bytes(), b"ORIGINAL")
        self.assertEqual(list(self.tmpdir.glob(".*.tmp")), [])


class RestoreSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="restore-snapshot-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_restores_modified_file_and_removes_created_file(self) -> None:
        graph_path = self.tmpdir / "graph" / "graph.json"
        created_path = self.tmpdir / "graph" / "claims.json"
        # On-disk state after a failed patch: graph.json mutated, claims.json
        # newly created by the patch (its pre-patch snapshot value is None).
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph_path.write_bytes(b"MUTATED_BY_PATCH")
        created_path.write_bytes(b"CREATED_BY_PATCH")
        snapshot: dict[Path, bytes | None] = {
            graph_path: b"ORIGINAL_GRAPH",
            created_path: None,
        }

        result = PatchApplyService._restore_snapshot(snapshot)

        self.assertTrue(result)
        self.assertEqual(graph_path.read_bytes(), b"ORIGINAL_GRAPH")
        self.assertFalse(created_path.exists())
        self.assertEqual(list(graph_path.parent.glob(".*.tmp")), [])

    def test_write_failure_leaves_no_truncated_file_and_logs(self) -> None:
        graph_path = self.tmpdir / "graph" / "graph.json"
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        # Current on-disk content represents the half-applied patch state we are
        # trying to roll back from. A failed restore must not truncate it.
        graph_path.write_bytes(b"PRE_RESTORE_STATE")
        snapshot: dict[Path, bytes | None] = {graph_path: b"ROLLBACK_TARGET"}

        with mock.patch(
            "app.services.utils.os.replace",
            side_effect=OSError("No space left on device"),
        ):
            with self.assertLogs("app.services.patch_apply", level="ERROR") as logs:
                result = PatchApplyService._restore_snapshot(snapshot)

        # Caller contract: False -> "recovery required".
        self.assertFalse(result)
        # Atomicity: the file is never left truncated/half-written.
        self.assertEqual(graph_path.read_bytes(), b"PRE_RESTORE_STATE")
        self.assertEqual(list(graph_path.parent.glob(".*.tmp")), [])
        # Observability: the swallowed failure is now logged with a traceback.
        self.assertTrue(any("restore" in line.lower() for line in logs.output))


if __name__ == "__main__":
    unittest.main()
