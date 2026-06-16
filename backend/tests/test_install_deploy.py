from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestInstallDeployBehavior(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.install_script = cls.repo_root / "scripts" / "install_blueprint_re.sh"
        cls.deploy_script = cls.repo_root / "scripts" / "deploy_user_systemd.sh"

    def setUp(self) -> None:
        self._env_backup = None
        env_file = self.repo_root / ".env"
        if env_file.exists():
            self._env_backup = env_file.read_text(encoding="utf-8")

    def tearDown(self) -> None:
        env_file = self.repo_root / ".env"
        if self._env_backup is not None:
            env_file.write_text(self._env_backup, encoding="utf-8")
        elif env_file.exists():
            env_file.unlink()

    @staticmethod
    def _parse_env_file(content: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                result[key] = value
        return result

    def _write_test_env(self, lines: list[str]) -> None:
        (self.repo_root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_install_rejects_missing_deepseek_key(self) -> None:
        """Install must fail before entering deploy when BLUEPRINT_DEEPSEEK_API_KEY is missing."""
        self._write_test_env([
            "BLUEPRINT_DEEPSEEK_API_BASE_URL=https://api.deepseek.com/anthropic",
            "BLUEPRINT_PI_DEEPSEEK_BASE_URL=https://api.deepseek.com",
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_bin = Path(tmpdir) / "bin"
            temp_bin.mkdir()

            # Mock bash to detect if deploy is ever invoked
            mock_bash = temp_bin / "bash"
            mock_bash.write_text(
                '#!/bin/bash\n'
                'if [[ "$*" == *"deploy_user_systemd.sh"* ]]; then\n'
                '  echo "DEPLOY_CALLED" >&2\n'
                '  exit 1\n'
                'fi\n'
                'exec /bin/bash "$@"\n',
                encoding="utf-8",
            )
            mock_bash.chmod(0o755)

            env = os.environ.copy()
            env.pop("BLUEPRINT_DEEPSEEK_API_KEY", None)
            env["PATH"] = f"{temp_bin}:{env.get('PATH', '')}"

            result = subprocess.run(
                ["/bin/bash", str(self.install_script), "--non-interactive"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(self.repo_root),
            )

        self.assertNotEqual(
            result.returncode, 0,
            "Install script must exit non-zero when DEEPSEEK key is missing",
        )
        combined = result.stdout + result.stderr
        self.assertIn(
            "BLUEPRINT_DEEPSEEK_API_KEY is required",
            combined,
            "Error message must name the missing key",
        )
        self.assertNotIn(
            "DEPLOY_CALLED",
            combined,
            "Deploy must not be invoked when key is missing",
        )

    def test_deploy_requires_deepseek_key(self) -> None:
        """Deploy must fail-fast when BLUEPRINT_DEEPSEEK_API_KEY is not set."""
        self._write_test_env([])

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_home = Path(tmpdir) / "home"
            temp_home.mkdir()
            temp_bin = Path(tmpdir) / "bin"
            temp_bin.mkdir()
            for cmd in ("systemctl", "bwrap", "git"):
                (temp_bin / cmd).write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
                (temp_bin / cmd).chmod(0o755)

            env = os.environ.copy()
            env.pop("BLUEPRINT_DEEPSEEK_API_KEY", None)
            env["HOME"] = str(temp_home)
            env["PATH"] = f"{temp_bin}:{env.get('PATH', '')}"

            result = subprocess.run(
                ["/bin/bash", str(self.deploy_script)],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(self.repo_root),
            )

        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn(
            "BLUEPRINT_DEEPSEEK_API_KEY is required for production deployment",
            combined,
        )

    def _run_deploy_with_stubs(self, env_lines: list[str], extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        """Run deploy in a temp HOME with stubbed external commands and return (result, parsed backend.env).

        These tests verify backend.env generation, not live HTTP health. The stub
        environment has no real backend/nginx, so health probes would always fail
        and force exit 1; skip them via BLUEPRINT_DEPLOY_SKIP_HEALTH_CHECK.
        """
        self._write_test_env(env_lines)

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_home = Path(tmpdir) / "home"
            temp_home.mkdir()
            temp_bin = Path(tmpdir) / "bin"
            temp_bin.mkdir()

            for cmd in ("systemctl", "bwrap", "git", "nginx"):
                (temp_bin / cmd).write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
                (temp_bin / cmd).chmod(0o755)

            # npm stub: fast-pass all commands and create build artifacts on demand
            npm_stub = (
                '#!/bin/bash\n'
                'if [[ "$1" == "run" && "$2" == "build" ]]; then\n'
                '  mkdir -p .next/standalone .next/static\n'
                'fi\n'
                'exit 0\n'
            )
            (temp_bin / "npm").write_text(npm_stub, encoding="utf-8")
            (temp_bin / "npm").chmod(0o755)

            # Python wrapper: intercept "python -m venv" so we can inject a stub pip
            # into the created venv. This avoids flaky network calls during tests.
            real_python = sys.executable
            python_wrapper = (
                '#!/bin/bash\n'
                f'REAL_PYTHON="{real_python}"\n'
                'if [[ "$1" == "-m" && "$2" == "venv" ]]; then\n'
                '  "$REAL_PYTHON" "$@"\n'
                '  VENV_DIR="${!#}"\n'
                '  cat > "${VENV_DIR}/bin/pip" <<\'PIPEEOF\'\n'
                '#!/bin/bash\n'
                'exit 0\n'
                'PIPEEOF\n'
                '  chmod +x "${VENV_DIR}/bin/pip"\n'
                '  exit 0\n'
                'fi\n'
                'exec "$REAL_PYTHON" "$@"\n'
            )
            for py_cmd in ("python3.13", "python3"):
                (temp_bin / py_cmd).write_text(python_wrapper, encoding="utf-8")
                (temp_bin / py_cmd).chmod(0o755)

            env = os.environ.copy()
            env["HOME"] = str(temp_home)
            env["PATH"] = f"{temp_bin}:{env.get('PATH', '')}"
            # Skip HTTP health probes in stub env (see docstring); extra_env can override.
            env["BLUEPRINT_DEPLOY_SKIP_HEALTH_CHECK"] = "1"
            if extra_env:
                env.update(extra_env)

            result = subprocess.run(
                ["/bin/bash", str(self.deploy_script)],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(self.repo_root),
                timeout=180,
            )

            backend_env_path = temp_home / ".config" / "blueprint-re" / "backend.env"
            parsed = (
                self._parse_env_file(backend_env_path.read_text(encoding="utf-8"))
                if backend_env_path.exists()
                else {}
            )
            return result, parsed

    def test_deploy_default_env_writes_all_required_keys_and_defaults(self) -> None:
        """Verify deploy writes all required backend.env keys, the default
        extra_ro_binds includes ~/.nvm and ~/.local, and codex is not
        auto-configured when no codex command is set.  One deploy run
        replaces the three previously separate tests to avoid redundant
        subprocess calls."""
        required_keys = {
            "PATH",
            "BACKEND_HOST",
            "BACKEND_PORT",
            "BLUEPRINT_FRONTEND_ORIGIN",
            "BLUEPRINT_MANAGER_BACKEND",
            "BLUEPRINT_DEFAULT_WORKER_TYPE",
            "BLUEPRINT_PI_MANAGER_URL",
            "BLUEPRINT_BACKEND_API_BASE_URL",
            "BLUEPRINT_DEFAULT_PYTHON_RUNTIME",
            "BLUEPRINT_DEFAULT_R_RUNTIME",
            "BLUEPRINT_EXECUTOR_CONDA_BASE",
            "BLUEPRINT_DEEPSEEK_API_KEY",
            "BLUEPRINT_DEEPSEEK_API_BASE_URL",
            "BLUEPRINT_PI_DEEPSEEK_BASE_URL",
            "BLUEPRINT_MANAGER_MODEL",
            "BLUEPRINT_MANAGER_TEMPERATURE",
            "BLUEPRINT_MANAGER_MAX_TOKENS",
            "BLUEPRINT_MANAGER_TIMEOUT_SECONDS",
            "BLUEPRINT_EXECUTOR_MODEL",
            "BLUEPRINT_REVIEWER_MODEL",
            "BLUEPRINT_LIBRARY_SUMMARIZER_MODEL",
            "BLUEPRINT_REVIEWER_MAX_TOKENS",
            "BLUEPRINT_REVIEWER_MAX_TURNS",
            "BLUEPRINT_EXECUTOR_SANDBOX_MODE",
            "BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS",
            "BLUEPRINT_EXECUTOR_HOST_ROOT_READONLY",
            "BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS",
            "BLUEPRINT_INTERNAL_TOOL_TOKEN",
            "BLUEPRINT_PI_COMMAND_JSON",
            "BLUEPRINT_OPENCODE_COMMAND_JSON",
            "BLUEPRINT_CLAUDE_CODE_COMMAND_JSON",
        }

        result, parsed = self._run_deploy_with_stubs([
            "BLUEPRINT_DEEPSEEK_API_KEY=test-key",
        ])

        self.assertEqual(result.returncode, 0, f"Deploy must succeed. stderr:\n{result.stderr}")

        missing = required_keys - set(parsed.keys())
        self.assertEqual(missing, set(), f"backend.env missing required keys: {sorted(missing)}")

        binds = parsed.get("BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS", "")
        self.assertIn(".nvm", binds, "Default extra_ro_binds must include ~/.nvm")
        self.assertIn(".local", binds, "Default extra_ro_binds must include ~/.local")

        self.assertNotIn(
            "BLUEPRINT_CODEX_COMMAND_JSON",
            parsed,
            "Codex must not appear in backend.env when not configured",
        )

    def test_deploy_extra_ro_binds_allows_explicit_empty_override(self) -> None:
        """An explicit empty BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS must remain empty
        instead of falling back to the deploy default."""
        result, parsed = self._run_deploy_with_stubs([
            "BLUEPRINT_DEEPSEEK_API_KEY=test-key",
            "BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS=",
        ])

        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS",
            parsed,
            "backend.env must preserve the explicit empty override",
        )
        self.assertEqual(
            parsed["BLUEPRINT_EXECUTOR_EXTRA_RO_BINDS"],
            "",
            "Explicit empty extra_ro_binds must not fall back to ~/.nvm,~/.local",
        )

    def test_deploy_codex_passthrough(self) -> None:
        """If BLUEPRINT_CODEX_COMMAND_JSON is set in .env, deploy must forward it
        into backend.env so the manual path survives a managed deploy rerun."""
        codex_json = '["codex","exec","{executor_prompt_path}"]'
        result, parsed = self._run_deploy_with_stubs([
            "BLUEPRINT_DEEPSEEK_API_KEY=test-key",
            f"BLUEPRINT_CODEX_COMMAND_JSON='{codex_json}'",
        ])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            parsed.get("BLUEPRINT_CODEX_COMMAND_JSON"),
            codex_json,
            "Codex command JSON must survive deploy passthrough",
        )

if __name__ == "__main__":
    unittest.main()
