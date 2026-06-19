"""Tests for the reference-data registry (storage, dedup, path safety, fetch)."""

from __future__ import annotations

import hashlib
import http.server
import io
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api.reference_data import delete_reference_data, register_upload
from app.core.config import Settings, get_settings
from app.models.card_blueprint import ReferenceDataSourceSpec
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


class _MockReferenceHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that serves per-path responses and records request counts."""

    # Populated by the harness before each test: path -> (status, body)
    routes: dict[str, tuple[int, bytes]] = {}
    request_counts: dict[str, int] = {}

    def do_GET(self):
        _MockReferenceHandler.request_counts[self.path] = (
            _MockReferenceHandler.request_counts.get(self.path, 0) + 1
        )
        status, body = _MockReferenceHandler.routes.get(self.path, (404, b"not found"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class _MockServer:
    def __init__(self, routes: dict[str, tuple[int, bytes]]):
        _MockReferenceHandler.routes = routes
        _MockReferenceHandler.request_counts = {}
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _MockReferenceHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def request_counts(self) -> dict[str, int]:
        return _MockReferenceHandler.request_counts

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TestFetchAndRegister(_Base):
    def setUp(self):
        super().setUp()
        self.server: _MockServer | None = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
        super().tearDown()

    def _start_server(self, routes: dict[str, tuple[int, bytes]]) -> _MockServer:
        self.server = _MockServer(routes)
        return self.server

    def _make_settings(self, retries: int = 2, timeout_s: int = 5) -> Settings:
        return Settings(
            data_root=self.data_root,
            reference_download_retries=retries,
            reference_download_timeout_s=timeout_s,
        )

    def test_fetch_and_register_downloads_and_verifies_sha256(self):
        content = b"gencode.v44.annotation.gtf.gz content"
        digest = hashlib.sha256(content).hexdigest()
        server = self._start_server({"/ref.gtf": (200, content)})

        svc = ReferenceDataService(settings=self._make_settings())
        source = ReferenceDataSourceSpec(
            url=f"{server.base_url}/ref.gtf",
            sha256=digest,
            kind="gtf",
            filename="gencode.v44.annotation.gtf.gz",
        )
        meta = svc.fetch_and_register(source)

        self.assertTrue(meta.ref_id.startswith("ref_"))
        self.assertEqual(meta.kind, "gtf")
        self.assertEqual(meta.sha256, digest)
        self.assertEqual(meta.size, len(content))
        self.assertEqual(meta.stored_filename, "gencode.v44.annotation.gtf.gz")
        self.assertEqual(svc.resolve(meta.ref_id).read_bytes(), content)
        self.assertEqual(server.request_counts.get("/ref.gtf"), 1)

    def test_fetch_and_register_dedups_on_cache_hit(self):
        content = b"shared reference content"
        digest = hashlib.sha256(content).hexdigest()
        server = self._start_server({"/ref.fa": (200, content)})

        svc = ReferenceDataService(settings=self._make_settings())
        source = ReferenceDataSourceSpec(
            url=f"{server.base_url}/ref.fa",
            sha256=digest,
            kind="fasta",
        )
        meta1 = svc.fetch_and_register(source)
        meta2 = svc.fetch_and_register(source)

        self.assertEqual(meta1.ref_id, meta2.ref_id)
        self.assertEqual(len(svc.list()), 1)
        # Pre-check dedup: the second fetch sees the registered sha256 and skips
        # network I/O entirely.
        self.assertEqual(server.request_counts.get("/ref.fa"), 1)

    def test_fetch_and_register_rejects_tampered_file(self):
        content = b"good content"
        digest = hashlib.sha256(content).hexdigest()
        server = self._start_server({"/ref.gtf": (200, b"tampered content")})

        svc = ReferenceDataService(settings=self._make_settings(retries=1))
        source = ReferenceDataSourceSpec(
            url=f"{server.base_url}/ref.gtf",
            sha256=digest,
            kind="gtf",
        )
        with self.assertRaises(ReferenceDataError) as ctx:
            svc.fetch_and_register(source)

        self.assertIn("sha256 mismatch", str(ctx.exception).lower())
        # Only one request because mismatches do not retry within the same URL.
        self.assertEqual(server.request_counts.get("/ref.gtf"), 1)

    def test_fetch_and_register_retry_budget_transport_failure(self):
        content = b"mirror content"
        digest = hashlib.sha256(content).hexdigest()
        server = self._start_server({
            "/primary.gtf": (500, b"server error"),
            "/mirror.gtf": (200, content),
        })

        svc = ReferenceDataService(settings=self._make_settings(retries=2))
        source = ReferenceDataSourceSpec(
            url=f"{server.base_url}/primary.gtf",
            mirrors=[f"{server.base_url}/mirror.gtf"],
            sha256=digest,
            kind="gtf",
        )
        meta = svc.fetch_and_register(source)

        self.assertEqual(meta.sha256, digest)
        self.assertEqual(server.request_counts.get("/primary.gtf"), 1)
        self.assertEqual(server.request_counts.get("/mirror.gtf"), 1)

    def test_fetch_and_register_sha256_mismatch_does_not_consume_retry(self):
        """With retries=1 a primary mismatch must still allow the mirror to be tried."""
        content = b"mirror content"
        digest = hashlib.sha256(content).hexdigest()
        server = self._start_server({
            "/primary.gtf": (200, b"bad content"),
            "/mirror.gtf": (200, content),
        })

        svc = ReferenceDataService(settings=self._make_settings(retries=1))
        source = ReferenceDataSourceSpec(
            url=f"{server.base_url}/primary.gtf",
            mirrors=[f"{server.base_url}/mirror.gtf"],
            sha256=digest,
            kind="gtf",
        )
        meta = svc.fetch_and_register(source)

        self.assertEqual(meta.sha256, digest)
        self.assertEqual(server.request_counts.get("/primary.gtf"), 1)
        self.assertEqual(server.request_counts.get("/mirror.gtf"), 1)

    def test_fetch_and_register_total_attempts_never_exceed_retries(self):
        server = self._start_server({
            "/a.gtf": (500, b"err"),
            "/b.gtf": (500, b"err"),
            "/c.gtf": (500, b"err"),
        })

        svc = ReferenceDataService(settings=self._make_settings(retries=2))
        source = ReferenceDataSourceSpec(
            url=f"{server.base_url}/a.gtf",
            mirrors=[
                f"{server.base_url}/b.gtf",
                f"{server.base_url}/c.gtf",
            ],
            sha256="0" * 64,
            kind="gtf",
        )
        with self.assertRaises(ReferenceDataError):
            svc.fetch_and_register(source)

        self.assertEqual(server.request_counts.get("/a.gtf"), 1)
        self.assertEqual(server.request_counts.get("/b.gtf"), 1)
        self.assertIsNone(server.request_counts.get("/c.gtf"))

    def test_fetch_and_register_disk_pre_check_blocks_before_network(self):
        # Do not start a server; the pre-check must fail before any network I/O.
        svc = ReferenceDataService(settings=self._make_settings(retries=1))
        source = ReferenceDataSourceSpec(
            url="http://127.0.0.1:9/should-not-be-requested.gtf",
            sha256="0" * 64,
            kind="gtf",
            size_hint=10**18,
        )
        with self.assertRaises(ReferenceDataError) as ctx:
            svc.fetch_and_register(source)

        self.assertIn("insufficient_disk_space", str(ctx.exception))
        # No server was started, so no request could have been made.

    def test_fetch_and_register_temp_file_always_cleaned_up(self):
        content = b"temp cleanup check"
        digest = hashlib.sha256(content).hexdigest()
        server = self._start_server({"/ref.gtf": (200, content)})

        svc = ReferenceDataService(settings=self._make_settings())
        source = ReferenceDataSourceSpec(
            url=f"{server.base_url}/ref.gtf",
            sha256=digest,
            kind="gtf",
        )
        svc.fetch_and_register(source)

        tmp_dir = svc._root / "_tmp"
        if tmp_dir.exists():
            self.assertEqual(list(tmp_dir.glob("fetch_*.bin")), [])

    def test_fetch_and_register_no_proxy_honored(self):
        """When 127.0.0.1 is in no_proxy, the bogus proxy must not be used."""
        content = b"local content"
        digest = hashlib.sha256(content).hexdigest()
        server = self._start_server({"/ref.gtf": (200, content)})

        svc = ReferenceDataService(
            settings=Settings(
                data_root=self.data_root,
                reference_download_retries=1,
                reference_download_timeout_s=5,
                http_proxy="http://127.0.0.1:1",  # unreachable
                no_proxy="127.0.0.1",
            )
        )
        source = ReferenceDataSourceSpec(
            url=f"{server.base_url}/ref.gtf",
            sha256=digest,
            kind="gtf",
        )
        meta = svc.fetch_and_register(source)
        self.assertEqual(meta.sha256, digest)


if __name__ == "__main__":
    unittest.main()
