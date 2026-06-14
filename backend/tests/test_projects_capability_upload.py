"""API-level tests for the capability upload endpoint.

Covers:
- Skill upload streams the file without reading it all into memory.
- Flat and nested .skill packages install with the filename stem as id.
- Malicious zip-slip archives are rejected.
"""
from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import UploadFile

from app.core.config import Settings, get_settings
from app.services.app_config_service import AppConfigService
from app.services.library_registry_service import LibraryRegistryService
from app.services.project_service import ProjectService

# The endpoint is async and uses FastAPI dependencies. Import it here so we can
# patch the dependency before invoking it directly.
from app.api import projects as projects_api


class TestProjectsCapabilityUpload(unittest.TestCase):
    def setUp(self):
        self._original_data_root = get_settings().data_root
        self.data_root = Path(tempfile.mkdtemp())
        self.settings = Settings(data_root=self.data_root)
        get_settings.cache_clear()

        with patch("app.services.project_service.get_settings", return_value=self.settings):
            project_service = ProjectService()
        app_config_service = AppConfigService(settings=self.settings)
        self.library_service = LibraryRegistryService(
            project_service=project_service,
            app_config_service=app_config_service,
            settings=self.settings,
        )

        def _get_library_service():
            return self.library_service

        self._orig_dep = projects_api.get_library_registry_service
        projects_api.get_library_registry_service = _get_library_service

    def tearDown(self):
        projects_api.get_library_registry_service = self._orig_dep
        get_settings.cache_clear()
        get_settings().data_root = self._original_data_root

    def _make_skill_archive(self, layout: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            if layout == "flat":
                zf.writestr("SKILL.md", "# Flat skill\n")
                zf.writestr("manifest.json", "{}")
            elif layout == "nested":
                zf.writestr("inner-name/SKILL.md", "# Nested skill\n")
                zf.writestr("inner-name/manifest.json", "{}")
            elif layout == "slip":
                zf.writestr("../../../tmp/slip.txt", "evil")
            else:
                raise ValueError(layout)
        return buf.getvalue()

    def _upload_file(self, filename: str, data: bytes) -> dict:
        upload = UploadFile(filename=filename, file=io.BytesIO(data))
        return asyncio.run(
            projects_api.upload_skill(
                project_id="test-project",
                file=upload,
                overwrite=False,
                library_service=self.library_service,
            )
        )

    def test_upload_streams_via_copyfileobj(self):
        data = self._make_skill_archive("flat")
        with patch.object(projects_api.shutil, "copyfileobj") as mock_copy:
            mock_copy.side_effect = lambda src, dst: dst.write(src.read())
            result = self._upload_file("from-upload.skill", data)
        self.assertTrue(mock_copy.called)
        self.assertEqual(result["installed_id"], "from-upload")

    def test_flat_skill_package_uses_filename_stem(self):
        data = self._make_skill_archive("flat")
        result = self._upload_file("my-flat-skill.skill", data)
        self.assertEqual(result["installed_id"], "my-flat-skill")
        self.assertEqual(result["installed_name"], "my-flat-skill")

        entries = self.library_service.list_entries("skill")["items"]
        self.assertEqual(entries[0]["id"], "my-flat-skill")

    def test_nested_skill_package_uses_filename_stem_not_directory_name(self):
        data = self._make_skill_archive("nested")
        result = self._upload_file("my-nested-skill.skill", data)
        self.assertEqual(result["installed_id"], "my-nested-skill")

        entries = self.library_service.list_entries("skill")["items"]
        self.assertEqual(entries[0]["id"], "my-nested-skill")
        self.assertNotEqual(entries[0]["id"], "inner-name")

    def test_malicious_archive_rejected(self):
        data = self._make_skill_archive("slip")
        with self.assertRaises(projects_api.HTTPException) as ctx:
            self._upload_file("evil.skill", data)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Unsafe", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
