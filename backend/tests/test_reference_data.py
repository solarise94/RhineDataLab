"""Tests for the reference-data registry (storage, dedup, path safety)."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api.reference_data import delete_reference_data, register_upload
from app.core.config import Settings, get_settings
from app.services.reference_data_service import (
    ReferenceDataError,
    ReferenceDataService,
)


class _UploadFile:
    """Minimal UploadFile stand-in that fails if read() is awaited."""

    def __init__(self, content: bytes, filename: str = "ref.fa"):
        self.file = io.BytesIO(content)
        self.filename = filename

    async def read(self):
        raise AssertionError("endpoint should stream via file.file, not await read()")


class _Base(unittest.TestCase):
    def setUp(self):
        self.data_root = Path(tempfile.mkdtemp())
        self.settings = Settings(data_root=self.data_root)
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()
        shutil.rmtree(self.data_root, ignore_errors=True)


class _FakeCardLibraryService:
    def __init__(self, usages: list[dict] | None = None):
        self._usages = usages or []

    def reference_usage(self, ref_id: str) -> list[dict]:
        return self._usages


class TestRegisterAndDedup(_Base):
    def test_register_local_copies_and_indexes(self):
        svc = ReferenceDataService(settings=self.settings)
        src = self.data_root / "genes.gtf"
        src.write_bytes(b"geneA\tgene\n")

        meta = svc.register_local(src, name="GRCh38 genes", kind="gtf")
        self.assertTrue(meta.ref_id.startswith("ref_"))
        self.assertEqual(meta.kind, "gtf")
        self.assertEqual(meta.size, src.stat().st_size)
        # Stored copy exists and equals source content.
        stored = svc.resolve(meta.ref_id)
        self.assertEqual(stored.read_bytes(), b"geneA\tgene\n")
        # Index/lists reflect it.
        self.assertEqual(len(svc.list()), 1)
        self.assertEqual(svc.get(meta.ref_id)["name"], "GRCh38 genes")

    def test_register_dedups_by_sha256(self):
        svc = ReferenceDataService(settings=self.settings)
        src1 = self.data_root / "a.gtf"
        src2 = self.data_root / "b.gtf"
        src1.write_bytes(b"same")
        src2.write_bytes(b"same")

        m1 = svc.register_local(src1, name="first")
        m2 = svc.register_local(src2, name="second")
        self.assertEqual(m1.ref_id, m2.ref_id)
        self.assertEqual(len(svc.list()), 1)

    def test_register_upload_stream(self):
        svc = ReferenceDataService(settings=self.settings)
        meta = svc.register_upload(
            io.BytesIO(b"hello"),
            filename="ref.fa",
            name="genome",
            kind="fasta",
        )
        self.assertEqual(svc.resolve(meta.ref_id).read_bytes(), b"hello")

    def test_register_upload_api_does_not_read_whole_file(self):
        """API endpoint must stream via file.file and never await file.read()."""
        svc = ReferenceDataService(settings=self.settings)
        upload = _UploadFile(b"streamed content", filename="ref.fa")
        result = register_upload(
            file=upload,
            name="streamed",
            kind="fasta",
            description=None,
            service=svc,
        )
        ref_id = result["entry"]["ref_id"]
        self.assertEqual(svc.resolve(ref_id).read_bytes(), b"streamed content")

    def test_delete_removes_registry_copy_only(self):
        svc = ReferenceDataService(settings=self.settings)
        src = self.data_root / "keep.gtf"
        src.write_bytes(b"data")
        meta = svc.register_local(src, name="keep")

        result = svc.delete(meta.ref_id)
        self.assertTrue(result["ok"])
        self.assertEqual(svc.list(), [])
        # Original source file untouched.
        self.assertTrue(src.exists())
        self.assertEqual(src.read_bytes(), b"data")
        with self.assertRaises(ReferenceDataError):
            svc.resolve(meta.ref_id)

    def test_delete_api_blocks_when_referenced(self):
        svc = ReferenceDataService(settings=self.settings)
        src = self.data_root / "genes.gtf"
        src.write_bytes(b"data")
        meta = svc.register_local(src, name="genes")
        fake_library = _FakeCardLibraryService(
            usages=[{"type": "blueprint", "blueprint_id": "bp-1", "title": "Used"}]
        )
        with self.assertRaises(HTTPException) as ctx:
            delete_reference_data(meta.ref_id, service=svc, card_library_service=fake_library)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("references", ctx.exception.detail)
        self.assertTrue(svc.list())  # Not deleted

    def test_delete_api_succeeds_when_unused(self):
        svc = ReferenceDataService(settings=self.settings)
        src = self.data_root / "genes.gtf"
        src.write_bytes(b"data")
        meta = svc.register_local(src, name="genes")
        fake_library = _FakeCardLibraryService(usages=[])
        result = delete_reference_data(meta.ref_id, service=svc, card_library_service=fake_library)
        self.assertTrue(result["ok"])
        self.assertEqual(svc.list(), [])


class TestPathSafety(_Base):
    def test_rejects_path_outside_approved_roots(self):
        svc = ReferenceDataService(settings=self.settings)
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_bytes(b"x")
        try:
            with self.assertRaises(ReferenceDataError):
                svc.register_local(outside, name="secret")
        finally:
            shutil.rmtree(outside.parent, ignore_errors=True)

    def test_rejects_symlink_escaping_root(self):
        svc = ReferenceDataService(settings=self.settings)
        outside_dir = Path(tempfile.mkdtemp())
        target = outside_dir / "real.gtf"
        target.write_bytes(b"real")
        link = self.data_root / "link.gtf"
        os.symlink(target, link)
        try:
            with self.assertRaises(ReferenceDataError):
                svc.register_local(link, name="via symlink")
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_resolve_blocks_traversal(self):
        svc = ReferenceDataService(settings=self.settings)
        # ref_id must match the safe charset; a crafted id is rejected.
        with self.assertRaises(ReferenceDataError):
            svc.resolve("..%2f..%2fetc")
        with self.assertRaises(ReferenceDataError):
            svc.resolve("ref_../../etc")


if __name__ == "__main__":
    unittest.main()
