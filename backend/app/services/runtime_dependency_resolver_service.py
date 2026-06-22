"""Runtime dependency resolution planner (P1).

The resolver is a deterministic planning layer that runs *before* the installer
creates a background job. Its job is to answer the question "what would happen
if I tried to install this dependency request?" without mutating any runtime
state.

The resolver:

- inspects every package in the request, classifying it as ``conda_installable``,
  ``fallback_required``, ``manual_preparation_required``, ``unsupported_source_spec``,
  ``runtime_missing``, or ``unknown``;
- produces a request-level summary status that the caller can map directly to
  P0 normalized fields;
- caches channel probes in memory for a short TTL to avoid repeated slow
  ``mamba repoquery search`` / ``conda search --json`` invocations;
- never executes installers and never mutates the environment.

The fallback policy is enforced here as well: ``allow_safe_registry_install``
is the current runtime default and may emit structured registry installer
actions for validated bare names, while ``report_only`` keeps fallback families
advisory-only. Actual execution is still owned by the backend installer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Iterable

from app.core.config import (
    default_conda_base,
    default_conda_base_candidates,
    derive_conda_base_from_runtime_path,
    find_conda_solver,
)
from app.services.runtime_dependency_state_service import compute_dedupe_key


# ---------------------------------------------------------------------------
# Status vocabularies
# ---------------------------------------------------------------------------

# Request-level statuses. The mapping to P0 fields is documented in doc 41 P1.1.
RESOLVER_STATUS_FULLY_INSTALLABLE = "fully_installable"
RESOLVER_STATUS_PARTIAL_RESOLUTION = "partial_resolution_requires_manual_preparation"
RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS = "fallback_available_but_policy_disallows"
RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS = "fallback_available_but_ambiguous"
RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED = "manual_preparation_required"
RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC = "unsupported_source_spec"
RESOLVER_STATUS_RUNTIME_MISSING = "runtime_missing"
RESOLVER_STATUS_SOLVER_ERROR = "solver_error"
RESOLVER_STATUS_RESOLUTION_UNKNOWN = "resolution_unknown"

RESOLVER_REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        RESOLVER_STATUS_FULLY_INSTALLABLE,
        RESOLVER_STATUS_PARTIAL_RESOLUTION,
        RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS,
        RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS,
        RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED,
        RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC,
        RESOLVER_STATUS_RUNTIME_MISSING,
        RESOLVER_STATUS_SOLVER_ERROR,
        RESOLVER_STATUS_RESOLUTION_UNKNOWN,
    }
)

# Per-package statuses.
PACKAGE_STATUS_CONDA_INSTALLABLE = "conda_installable"
PACKAGE_STATUS_FALLBACK_REQUIRED = "fallback_required"
PACKAGE_STATUS_MANUAL_PREPARATION_REQUIRED = "manual_preparation_required"
PACKAGE_STATUS_UNSUPPORTED_SOURCE_SPEC = "unsupported_source_spec"
PACKAGE_STATUS_RUNTIME_MISSING = "runtime_missing"
PACKAGE_STATUS_SOLVER_ERROR = "solver_error"
PACKAGE_STATUS_UNKNOWN = "unknown"

# Mapping from resolver request-level status to P0 normalized fields.
RESOLVER_TO_P0_FIELDS: dict[str, dict[str, str | None]] = {
    RESOLVER_STATUS_FULLY_INSTALLABLE: {"error_code": None, "retry_hint": None},
    RESOLVER_STATUS_PARTIAL_RESOLUTION: {
        "error_code": RESOLVER_STATUS_PARTIAL_RESOLUTION,
        "retry_hint": "manual_preparation_required",
    },
    RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS: {
        "error_code": "package_not_found_in_conda_channels",
        "retry_hint": "choose_fallback",
    },
    RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS: {
        "error_code": "package_not_found_in_conda_channels",
        "retry_hint": "manual_preparation_required",
    },
    RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED: {
        "error_code": RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED,
        "retry_hint": "manual_preparation_required",
    },
    RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC: {
        "error_code": "github_source_install_not_supported",
        "retry_hint": "do_not_retry_installer",
    },
    RESOLVER_STATUS_RUNTIME_MISSING: {
        "error_code": "dependency_install_start_failed",
        "retry_hint": "manual_runtime_preparation_required",
    },
    RESOLVER_STATUS_SOLVER_ERROR: {
        "error_code": "dependency_probe_failed",
        "retry_hint": "inspect_stderr",
    },
    RESOLVER_STATUS_RESOLUTION_UNKNOWN: {
        "error_code": "dependency_resolution_unknown",
        "retry_hint": "inspect_stderr",
    },
}

# Fallback families for each ecosystem.
FALLBACK_FAMILIES_PYTHON: list[str] = ["pip"]
FALLBACK_FAMILIES_R: list[str] = ["cran", "bioconductor"]

# Known Bioconductor-only packages. When a request contains any of these,
# force the bioconductor fallback family: BiocManager::install can also
# install CRAN packages, so mixed requests are safe, but install.packages()
# will fail for Bioconductor-only packages like DESeq2.
BIOCONDUCTOR_ONLY_PACKAGES: frozenset[str] = frozenset({
    "deseq2", "edger", "limma", "clusterprofiler", "complexheatmap",
    "sva", "genefilter", "genomicfeatures", "rtracklayer",
    "annotationdbi", "biomart", "goseq", "pathview", "reactomepa",
    "gsva", "scran", "scater", "soupx",
})

# Grammar used to validate bare names when a fallback install action is emitted.
# We keep it strict to refuse source-style inputs.
BARE_NAME_GRAMMAR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Strict version pin grammar: <bare-name>[==<version>]?  (e.g. numpy or numpy==1.26.4)
VERSION_PIN_GRAMMAR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(==[A-Za-z0-9_.+-]+)?$")

# Source-install patterns that the resolver must always reject.
SOURCE_SPEC_PATTERNS = (
    "github.com",
    "git+",
    "http://",
    "https://",
    ".tar.gz",
    ".zip",
    ".whl",
)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    value: ProbeResult | None  # None means "checked and not found"
    expires_at: float


class _ResolverCache:
    """In-memory cache for conda package probes.

    Keys are tuples of (channel_set_signature, package_name). Values are
    _CacheEntry instances. Entries are bounded by a default TTL of 1 hour.
    """

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._entries: dict[tuple[str, str], _CacheEntry] = {}
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str]) -> ProbeResult | None | object:
        """Return cached value or the sentinel ``_MISS`` if absent/expired.

        Use ``is`` checks to distinguish from a real ``None`` value.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return _MISS
            if entry.expires_at < time.time():
                self._entries.pop(key, None)
                return _MISS
            return entry.value

    def set(self, key: tuple[str, str], value: ProbeResult | None) -> None:
        with self._lock:
            self._entries[key] = _CacheEntry(
                value=value,
                expires_at=time.time() + self._ttl_seconds,
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_MISS = object()


@dataclass
class RuntimeProbeResult:
    """Structured return from _probe_runtime."""
    present: bool
    resolved_path: Path | None = None
    error_message: str | None = None


@dataclass
class ProbeResult:
    """Structured return from a conda package probe."""
    status: str  # "found", "not_found", "solver_error"
    match: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# Public dataclasses (plan + actions)
# ---------------------------------------------------------------------------


@dataclass
class ResolverPackageEntry:
    name: str
    normalized_name: str
    classification: str  # e.g. "conda", "pip", "cran", "bioconductor", "source"
    conda_candidates: list[str] = field(default_factory=list)
    conda_match: str | None = None
    fallback_available: list[str] = field(default_factory=list)
    status: str = PACKAGE_STATUS_UNKNOWN
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "normalized_name": self.normalized_name,
            "classification": self.classification,
            "conda_candidates": list(self.conda_candidates),
            "fallback_available": list(self.fallback_available),
            "status": self.status,
        }
        if self.conda_match is not None:
            out["conda_match"] = self.conda_match
        if self.reason is not None:
            out["reason"] = self.reason
        if self.message is not None:
            out["message"] = self.message
        return out


@dataclass
class ResolverInstallAction:
    installer: str  # "conda" | "pip" | "cran" | "bioconductor"
    name: str  # bare name (no source paths or URLs)
    candidate: str | None = None  # conda candidate if applicable
    version_pin: str | None = None  # exact version if pinned

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": "install",
            "installer": self.installer,
            "name": self.name,
        }
        if self.candidate is not None:
            out["candidate"] = self.candidate
        if self.version_pin is not None:
            out["version_pin"] = self.version_pin
        return out


@dataclass
class ResolverBlockedEntry:
    name: str
    reason: str
    attempted_candidates: list[str] = field(default_factory=list)
    fallback_available: list[str] = field(default_factory=list)
    recommended_action: str = "manual_preparation_or_policy_approved_fallback"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "reason": self.reason,
            "attempted_candidates": list(self.attempted_candidates),
            "fallback_available": list(self.fallback_available),
            "recommended_action": self.recommended_action,
        }


@dataclass
class RuntimeDependencyResolutionPlan:
    ok: bool
    tool: str = "resolve_runtime_dependencies"
    ecosystem: str = ""
    runtime: str = ""
    status: str = RESOLVER_STATUS_RESOLUTION_UNKNOWN
    error_code: str | None = None
    retry_hint: str | None = None
    message: str | None = None
    request_dedupe_key: str = ""
    packages: list[ResolverPackageEntry] = field(default_factory=list)
    installable: list[ResolverInstallAction] = field(default_factory=list)
    blocked: list[ResolverBlockedEntry] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    # Internal: raw conda actions discovered before the policy step.
    # Not serialized.
    _candidate_actions: list[ResolverInstallAction] = field(default_factory=list)
    cache: _ResolverCache | None = None  # internal; not serialized

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "tool": self.tool,
            "ecosystem": self.ecosystem,
            "runtime": self.runtime,
            "status": self.status,
            "packages": [pkg.to_dict() for pkg in self.packages],
            "installable": [act.to_dict() for act in self.installable],
            "blocked": [blk.to_dict() for blk in self.blocked],
            "recommended_actions": list(self.recommended_actions),
            "request_dedupe_key": self.request_dedupe_key,
        }
        if self.error_code is not None:
            out["error_code"] = self.error_code
        if self.retry_hint is not None:
            out["retry_hint"] = self.retry_hint
        if self.message is not None:
            out["message"] = self.message
        return out


# ---------------------------------------------------------------------------
# Resolver service
# ---------------------------------------------------------------------------


class RuntimeDependencyResolverService:
    """Deterministic resolver for runtime dependency requests.

    The service does not perform installation. It only inspects a request,
    optionally probes conda channels for package presence, and returns a
    structured plan.
    """

    def __init__(
        self,
        *,
        conda_solver: str | None = None,
        cache_ttl_seconds: int = 3600,
        probe_timeout_seconds: int = 60,
    ) -> None:
        self._explicit_conda_solver = conda_solver
        self._cache = _ResolverCache(ttl_seconds=cache_ttl_seconds)
        self._probe_timeout = probe_timeout_seconds
        # Fail-closed default for the fallback policy. resolve() overwrites this
        # with the normalized requested policy on every call; until then (and if a
        # helper is ever reached before resolve() runs) the restrictive
        # ``report_only`` applies, so an unset policy can never silently authorize a
        # registry install. See audit §6.2.
        self._active_policy: str = "report_only"

    # -- public API --------------------------------------------------------

    def clear_cache(self) -> None:
        self._cache.clear()

    def resolve(
        self,
        project_id: str,
        payload: dict[str, Any],
        *,
        settings: Any | None = None,
        policy: str = "allow_safe_registry_install",
    ) -> RuntimeDependencyResolutionPlan:
        ecosystem = str(payload.get("ecosystem") or "").strip()
        runtime = str(payload.get("runtime") or "").strip()
        raw_packages = payload.get("packages") or []
        if not isinstance(raw_packages, list):
            raw_packages = [str(raw_packages)]
        # Preserve order, dedupe case-insensitively, strip empties.
        seen: set[str] = set()
        packages: list[str] = []
        for item in raw_packages:
            name = str(item or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            packages.append(name)

        # Stash the active settings on the resolver service so internal
        # helpers (e.g. _probe_runtime) can use the same object without
        # threading it through every call.
        self._settings = settings or _resolver_settings_stub()
        # Normalize policy once so the rest of the resolver can use the
        # canonical ``"report_only"`` / ``"allow_safe_registry_install"`` pair.
        self._active_policy = normalize_fallback_policy(policy)

        # Optional per-request channel overrides. For R ecosystem, conda-forge
        # and bioconda are automatically ensured because most bioinformatics R
        # packages live on bioconda with conda-forge dependencies.
        raw_channels = payload.get("channels") or []
        extra_channels: list[str] = [str(c).strip() for c in raw_channels if str(c).strip()]
        if ecosystem.lower() == "r":
            lower_set = {c.lower() for c in extra_channels}
            if "conda-forge" not in lower_set:
                extra_channels.append("conda-forge")
            if "bioconda" not in lower_set:
                extra_channels.append("bioconda")
        self._extra_channels = extra_channels

        plan = RuntimeDependencyResolutionPlan(
            ok=False,
            ecosystem=ecosystem,
            runtime=runtime,
            request_dedupe_key=compute_dedupe_key(
                ecosystem or "unknown",
                runtime or "unknown",
                packages,
            ),
            cache=self._cache,
        )

        if ecosystem.lower() not in {"python", "r"}:
            plan.status = RESOLVER_STATUS_RESOLUTION_UNKNOWN
            plan.error_code = "dependency_resolution_unknown"
            plan.message = f"Unsupported ecosystem: {ecosystem!r}; expected python or R."
            plan.recommended_actions = ["Do not call install_runtime_dependencies with an unknown ecosystem."]
            return plan

        if ecosystem.lower() == "r":
            # Preserve "R" casing so the dedupe key aligns with the
            # ``runtime_dependency_state_service`` cooling lookup.
            ecosystem = "R"

        if not runtime or runtime == "__system__":
            plan.status = RESOLVER_STATUS_RUNTIME_MISSING
            plan.error_code = RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_RUNTIME_MISSING]["error_code"]
            plan.retry_hint = RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_RUNTIME_MISSING]["retry_hint"]
            plan.message = "Selected runtime is missing or marked as __system__; manual runtime preparation is required."
            plan.recommended_actions = ["Choose a non-system Python or R runtime before retrying."]
            return plan

        if not packages:
            plan.status = RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED
            plan.error_code = RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED]["error_code"]
            plan.retry_hint = RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED]["retry_hint"]
            plan.message = "No packages were provided."
            plan.recommended_actions = ["Provide at least one bare package name before retrying."]
            return plan

        # Source install inputs are rejected up-front.
        source_packages = [pkg for pkg in packages if _looks_like_source_spec(pkg)]
        if source_packages:
            for pkg in packages:
                if _looks_like_source_spec(pkg):
                    plan.packages.append(
                        ResolverPackageEntry(
                            name=pkg,
                            normalized_name=pkg.lower(),
                            classification="source",
                            conda_candidates=[],
                            fallback_available=[],
                            status=PACKAGE_STATUS_UNSUPPORTED_SOURCE_SPEC,
                            reason="source_install_not_supported",
                            message="Source-install dependencies are not supported; use a registry-backed bare name.",
                        )
                    )
                else:
                    plan.packages.append(
                        ResolverPackageEntry(
                            name=pkg,
                            normalized_name=pkg.lower(),
                            classification=_classify_for_ecosystem(pkg, ecosystem),
                            conda_candidates=[],
                            fallback_available=_fallback_families_for(ecosystem),
                            status=PACKAGE_STATUS_MANUAL_PREPARATION_REQUIRED,
                            reason="request_rejected_for_source_specs",
                        )
                    )
            plan.status = RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC
            plan.error_code = RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC]["error_code"]
            plan.retry_hint = RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC]["retry_hint"]
            plan.message = "Source-install dependencies are not supported by the resolver."
            plan.recommended_actions = [
                "Use a bare package name (or version pin) instead of a URL, GitHub reference, or tarball.",
            ]
            for pkg in source_packages:
                plan.blocked.append(
                    ResolverBlockedEntry(
                        name=pkg,
                        reason="source_install_not_supported",
                        attempted_candidates=[],
                        fallback_available=[],
                    )
                )
            return plan

        # Try to resolve the runtime path. If we cannot find it, mark all
        # packages as runtime_missing rather than guess.
        probe_result = self._probe_runtime(runtime, ecosystem)
        if not probe_result.present:
            for pkg in packages:
                plan.packages.append(
                    ResolverPackageEntry(
                        name=pkg,
                        normalized_name=_normalize_for_ecosystem(pkg, ecosystem),
                        classification=_classify_for_ecosystem(pkg, ecosystem),
                        conda_candidates=[],
                        fallback_available=_fallback_families_for(ecosystem),
                        status=PACKAGE_STATUS_RUNTIME_MISSING,
                        reason="runtime_path_unresolved",
                        message=probe_result.error_message,
                    )
                )
            plan.status = RESOLVER_STATUS_RUNTIME_MISSING
            plan.error_code = RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_RUNTIME_MISSING]["error_code"]
            plan.retry_hint = RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_RUNTIME_MISSING]["retry_hint"]
            plan.message = probe_result.error_message
            plan.recommended_actions = [
                "Verify the selected runtime path exists and is executable before retrying.",
            ]
            return plan

        # Per-package resolution. First probe conda for each package; this
        # populates ``plan.packages`` with the per-package status. We do NOT
        # add conda actions to ``plan.installable`` yet — that decision is
        # made after we know which fallback actions, if any, the policy
        # allows.
        conda_bin = self._resolve_conda_solver(ecosystem, probe_result.resolved_path)
        channel_signature = self._channel_signature(
            conda_bin, ecosystem, runtime,
            conda_base=derive_conda_base_from_runtime_path(probe_result.resolved_path),
            extra_channels=self._extra_channels,
        )

        # Batch pre-warm: for mamba/micromamba, probe ALL candidates in one
        # subprocess call and populate the cache. This reduces 23 serial
        # repoquery invocations to 1, avoiding the 300s HTTP timeout.
        self._batch_prefetch_conda(conda_bin, packages, ecosystem, channel_signature, self._extra_channels)

        for pkg in packages:
            entry, conda_action = self._resolve_package(
                pkg,
                ecosystem=ecosystem,
                conda_bin=conda_bin,
                channel_signature=channel_signature,
            )
            plan.packages.append(entry)
            # Record the conda action as a candidate; whether we keep it
            # in the final plan depends on the request-level status.
            if conda_action is not None:
                plan._candidate_actions.append(conda_action)  # type: ignore[attr-defined]

        # Build the installable / blocked action split. Mixed-installer
        # requests are blocked even under allow_safe_registry_install.
        self._populate_installable_actions(plan)

        # Build a stable mapping of blocked entries (every package whose
        # final action was not retained is "blocked" relative to the
        # installer's current plan).
        for entry in plan.packages:
            if entry.status in {
                PACKAGE_STATUS_CONDA_INSTALLABLE,
                PACKAGE_STATUS_FALLBACK_REQUIRED,
            }:
                # May or may not be retained; reflect whatever the final
                # plan.installable / plan.blocked list says.
                if not any(
                    action.name == entry.name for action in plan.installable
                ):
                    plan.blocked.append(
                        ResolverBlockedEntry(
                            name=entry.name,
                            reason=entry.reason or entry.status,
                            attempted_candidates=list(entry.conda_candidates),
                            fallback_available=list(entry.fallback_available),
                        )
                    )
                continue
            plan.blocked.append(
                ResolverBlockedEntry(
                    name=entry.name,
                    reason=entry.reason or entry.status,
                    attempted_candidates=list(entry.conda_candidates),
                    fallback_available=list(entry.fallback_available),
                )
            )

        plan.status, plan.error_code, plan.message = self._aggregate_status(plan)
        plan.ok = plan.status == RESOLVER_STATUS_FULLY_INSTALLABLE
        plan.recommended_actions = self._recommended_actions(plan)
        # Always populate error_code / retry_hint from the P0 mapping for tool
        # consumers (Manager, frontend, workboard).
        mapping = RESOLVER_TO_P0_FIELDS.get(plan.status)
        if mapping is not None:
            if plan.error_code is None:
                plan.error_code = mapping["error_code"]
            if plan.retry_hint is None:
                plan.retry_hint = mapping["retry_hint"]
        return plan

    def resolve_reference_download(
        self,
        project_id: str,
        payload: dict[str, Any],
        *,
        settings: Any | None = None,
    ) -> dict[str, Any]:
        """Plan-only resolver path for reference-data downloads (Layer C).

        Returns a plan-shaped dict that tells Manager "a background download
        job will be created for this source".  This keeps Manager's
        ``resolve_runtime_dependencies`` "what would happen?" accurate for
        reference assets without forking the job machinery.
        """
        source_container = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        source_spec = source_container.get("spec") if isinstance(source_container, dict) else None
        role = str(payload.get("role") or source_container.get("role") or "").strip()
        if not isinstance(source_spec, dict) or not source_spec.get("sha256"):
            return {
                "ok": False,
                "tool": "resolve_reference_download",
                "background": False,
                "async_boundary": False,
                "do_not_poll": False,
                "wait_for_wake": False,
                "status": RESOLVER_STATUS_RESOLUTION_UNKNOWN,
                "error_code": "invalid_reference_payload",
                "retry_hint": "manual_preparation_required",
                "message": "Reference download payload is missing a valid source spec.",
            }
        sha256 = str(source_spec["sha256"]).strip()
        return {
            "ok": True,
            "tool": "resolve_reference_download",
            "background": True,
            "async_boundary": True,
            "do_not_poll": True,
            "wait_for_wake": True,
            "status": RESOLVER_STATUS_FULLY_INSTALLABLE,
            "descriptor": {
                "role": role,
                "source": source_spec,
            },
            "request_dedupe_key": f"ref:{sha256}",
        }

    # -- internals ---------------------------------------------------------

    def _batch_prefetch_conda(
        self,
        conda_bin: Path | None,
        packages: list[str],
        ecosystem: str,
        channel_signature: str,
        extra_channels: list[str] | None = None,
    ) -> None:
        """Pre-warm the probe cache with a single batched repoquery call.

        For mamba/micromamba, one repoquery subprocess can search all
        candidates at once. This avoids N serial subprocess calls (each
        potentially waiting for repodata fetch) and keeps total resolve
        time well under HTTP timeout limits.

        This is a best-effort optimization: if it fails for any reason,
        the per-package path will still run normally via _probe_conda.
        """
        if conda_bin is None:
            return
        solver_name = conda_bin.name.lower()
        if solver_name not in {"mamba", "micromamba"}:
            return

        # Collect all candidates that are not already cached.
        all_candidates: list[str] = []
        for pkg in packages:
            if _looks_like_source_spec(pkg) or not _is_valid_grammar(pkg, ecosystem.lower()):
                continue
            candidates = _conda_candidates_for(pkg, ecosystem)
            cache_key = (channel_signature, candidates[0] if candidates else pkg.strip().lower())
            if self._cache.get(cache_key) is not _MISS:
                continue
            all_candidates.extend(candidates)

        if not all_candidates:
            return

        self._repoquery_prefetch_into_cache(
            conda_bin, packages, ecosystem, channel_signature, all_candidates, extra_channels=extra_channels
        )

    def _repoquery_prefetch_into_cache(
        self,
        conda_bin: Path,
        packages: list[str],
        ecosystem: str,
        channel_signature: str,
        all_candidates: list[str],
        extra_channels: list[str] | None = None,
    ) -> None:
        """Run one repoquery call and populate per-package cache entries.

        This is best-effort: on timeout, OS error, or unparseable output,
        we skip caching entirely and let the per-package path handle it
        with proper error classification.
        """
        cmd = [str(conda_bin), "repoquery", "search", "--json"]
        if extra_channels:
            cmd.append("--override-channels")
            for ch in extra_channels:
                cmd.extend(["-c", ch])
        cmd.extend(all_candidates)
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self._probe_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._cache_for_all_packages(
                packages, ecosystem, channel_signature,
                ProbeResult(status="solver_error", error_code="probe_timeout",
                            error_detail="batch repoquery timed out"),
            )
            return
        except OSError:
            return

        if result.returncode != 0:
            stderr = (result.stderr or "")[:500]
            if _is_packages_not_found(stderr):
                self._cache_for_all_packages(packages, ecosystem, channel_signature, ProbeResult(status="not_found"))
            else:
                self._cache_for_all_packages(
                    packages, ecosystem, channel_signature,
                    ProbeResult(status="solver_error", error_code="mamba_solver_error",
                                error_detail=stderr[:200]),
                )
            return

        stdout = result.stdout or ""
        found_names = self._extract_found_names(stdout, all_candidates)

        if found_names is None:
            # Unparseable output — don't cache, let per-package path handle.
            return

        # Populate cache per-package based on found_names
        for pkg in packages:
            if _looks_like_source_spec(pkg) or not _is_valid_grammar(pkg, ecosystem.lower()):
                continue
            candidates = _conda_candidates_for(pkg, ecosystem)
            cache_key = (channel_signature, candidates[0] if candidates else pkg.strip().lower())
            if self._cache.get(cache_key) is not _MISS:
                continue
            matched = None
            for c in candidates:
                if c in found_names:
                    matched = c
                    break
            if matched:
                self._cache.set(cache_key, ProbeResult(status="found", match=matched))
            else:
                self._cache.set(cache_key, ProbeResult(status="not_found"))

    def _extract_found_names(self, stdout: str, candidates: list[str]) -> set[str] | None:
        """Extract the set of found package names from repoquery output.

        Returns a set of found names (possibly empty) on success.
        Returns None if stdout is non-blank but unparseable (neither JSON nor
        tabular) — callers should treat this as solver_error.
        """
        import re as _re

        payload = self._try_extract_json(stdout)
        if isinstance(payload, dict):
            result_obj = payload.get("result")
            if isinstance(result_obj, dict):
                pkgs_list = result_obj.get("pkgs")
                if isinstance(pkgs_list, list):
                    return {p.get("name") for p in pkgs_list if isinstance(p, dict) and p.get("name")}

        # Try tabular regex fallback
        found: set[str] = set()
        for c in candidates:
            pattern = _re.compile(rf"(?m)^\s*{_re.escape(c)}(?:\s|$)")
            if pattern.search(stdout):
                found.add(c)
        if found:
            return found

        # Non-blank stdout that couldn't be parsed either way
        if stdout.strip():
            return None
        return set()

    def _cache_for_all_packages(
        self,
        packages: list[str],
        ecosystem: str,
        channel_signature: str,
        probe_result: ProbeResult,
    ) -> None:
        """Write the same ProbeResult into cache for all uncached packages."""
        for pkg in packages:
            if _looks_like_source_spec(pkg) or not _is_valid_grammar(pkg, ecosystem.lower()):
                continue
            candidates = _conda_candidates_for(pkg, ecosystem)
            cache_key = (channel_signature, candidates[0] if candidates else pkg.strip().lower())
            if self._cache.get(cache_key) is _MISS:
                self._cache.set(cache_key, probe_result)

    def _resolve_package(
        self,
        pkg: str,
        *,
        ecosystem: str,
        conda_bin: Path | None,
        channel_signature: str,
    ) -> tuple[ResolverPackageEntry, ResolverInstallAction | None]:
        normalized = _normalize_for_ecosystem(pkg, ecosystem)
        classification = _classify_for_ecosystem(pkg, ecosystem)
        fallback = _fallback_families_for(ecosystem)
        candidates = _conda_candidates_for(pkg, ecosystem)

        # Grammar check uses the lower-cased ecosystem so the same rule applies
        # regardless of whether the caller sent "r" or "R".
        # Names outside the supported grammar (extras, version comparisons,
        # shell chars, local paths, etc.) are rejected as
        # ``unsupported_source_spec`` so the request-level status gives
        # the caller a strong control signal per doc 41 §P1.3.
        if not _is_valid_grammar(pkg, ecosystem.lower()):
            return ResolverPackageEntry(
                name=pkg,
                normalized_name=normalized,
                classification=classification,
                conda_candidates=candidates,
                fallback_available=[],
                status=PACKAGE_STATUS_UNSUPPORTED_SOURCE_SPEC,
                reason="unsupported_spec",
                message=(
                    f"Package spec {pkg!r} does not match the supported "
                    "bare-name grammar. Only bare names (e.g. numpy) or "
                    "exact version pins (e.g. numpy==1.26.4) are allowed."
                ),
            ), None

        if conda_bin is None:
            return ResolverPackageEntry(
                name=pkg,
                normalized_name=normalized,
                classification=classification,
                conda_candidates=candidates,
                fallback_available=fallback,
                status=PACKAGE_STATUS_FALLBACK_REQUIRED,
                reason="conda_solver_unavailable",
                message="No conda solver was found; only fallback families are available.",
            ), None

        cache_key = (channel_signature, candidates[0] if candidates else normalized)
        cached = self._cache.get(cache_key)
        if cached is _MISS:
            probe_result = self._probe_conda(
                conda_bin, candidates, ecosystem=ecosystem,
                extra_channels=self._extra_channels,
            )
            self._cache.set(cache_key, probe_result)
        else:
            probe_result = cached  # type: ignore[assignment]

        if probe_result.status == "found":
            return ResolverPackageEntry(
                name=pkg,
                normalized_name=normalized,
                classification=classification,
                conda_candidates=candidates,
                conda_match=probe_result.match,
                fallback_available=fallback,
                status=PACKAGE_STATUS_CONDA_INSTALLABLE,
            ), ResolverInstallAction(
                installer="conda",
                name=pkg,
                candidate=probe_result.match,
            )

        if probe_result.status == "solver_error":
            # A probe failure (timeout/solver error) does not mean the package
            # does not exist. When the ecosystem has registry fallback families
            # and the active policy allows safe registry installs, downgrade to
            # fallback_required so the caller can retry via pip / cran /
            # bioconductor instead of blocking on a transient conda issue.
            if fallback and self._active_policy == "allow_safe_registry_install":
                return ResolverPackageEntry(
                    name=pkg,
                    normalized_name=normalized,
                    classification=classification,
                    conda_candidates=candidates,
                    fallback_available=fallback,
                    status=PACKAGE_STATUS_FALLBACK_REQUIRED,
                    reason=f"conda_probe_failed:{probe_result.error_code or 'unknown'}",
                    message=(
                        f"Conda probe failed for {pkg!r} "
                        f"({probe_result.error_detail or 'unknown'}); "
                        "registry fallback available under the active policy."
                    ),
                ), None
            return ResolverPackageEntry(
                name=pkg,
                normalized_name=normalized,
                classification=classification,
                conda_candidates=candidates,
                fallback_available=fallback,
                status=PACKAGE_STATUS_SOLVER_ERROR,
                reason=probe_result.error_code or "conda_solver_error",
                message=(
                    f"Conda probe failed for {pkg!r}: "
                    f"{probe_result.error_detail or 'unknown solver error'}."
                ),
            ), None

        return ResolverPackageEntry(
            name=pkg,
            normalized_name=normalized,
            classification=classification,
            conda_candidates=candidates,
            fallback_available=fallback,
            status=PACKAGE_STATUS_FALLBACK_REQUIRED,
            reason="package_not_found_in_conda_channels",
            message=(
                f"None of the conda candidates {', '.join(candidates) or '(none)'} "
                f"were found in the configured channels."
            ),
        ), None

    def _populate_installable_actions(
        self,
        plan: RuntimeDependencyResolutionPlan,
    ) -> None:
        """Decide which actions to keep in ``plan.installable``.

        Rules (P1.3):
        - All-conda actions are always surfaced.
        - For packages without a conda match, the resolver may build
          registry-fallback actions only under ``allow_safe_registry_install``
          and only when every fallback action belongs to the same family.
        - ``plan.installable`` reflects the RESOLVER's opinion of which
          packages are individually installable.  The request-level
          ``plan.status`` (set by ``_aggregate_status``) controls whether a
          background job is created.  A mixed-installer plan keeps conda
          actions visible but stays at ``partial_resolution_*`` so the
          installer gate blocks it.
        """
        plan.installable = list(
            [a for a in plan._candidate_actions if a.installer == "conda"]
        )
        non_conda_packages = [
            pkg
            for pkg in plan.packages
            if pkg.status == PACKAGE_STATUS_FALLBACK_REQUIRED
        ]
        policy = self._active_policy

        if not non_conda_packages:
            return

        # P1.3: mixed installer families (conda + pip, conda + cran,
        # etc.) are never auto-executable.  If the plan already contains
        # conda actions, fallback actions must not be added — the caller
        # receives a partial resolution plan and may submit a narrower
        # request.
        if plan.installable:
            return

        if not fallback_policy_allows(policy):
            return

        # Build safe fallback actions only when the resolver can reduce the
        # request to one registry family. For R requests, prefer bioconductor
        # when any package is known to be Bioconductor-only; otherwise fall
        # back to the original single-family / CRAN convergence logic.
        is_r_ecosystem = bool(
            non_conda_packages and non_conda_packages[0].classification == "r-package"
        )
        if is_r_ecosystem:
            family = _select_r_fallback_family(non_conda_packages)
        else:
            family = _single_safe_fallback_family(non_conda_packages)
        if family is None:
            return
        for pkg in non_conda_packages:
            action = ResolverInstallAction(installer=family, name=pkg.name)
            if is_registry_fallback_action_safe(action):
                plan.installable.append(action)

    def _aggregate_status(
        self, plan: RuntimeDependencyResolutionPlan
    ) -> tuple[str, str | None, str]:
        statuses = [pkg.status for pkg in plan.packages]
        if not statuses:
            return RESOLVER_STATUS_RESOLUTION_UNKNOWN, "dependency_resolution_unknown", "Empty plan."

        # Source spec rejection wins regardless of policy.
        if any(s == PACKAGE_STATUS_UNSUPPORTED_SOURCE_SPEC for s in statuses):
            return (
                RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC,
                RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC]["error_code"],
                "Source-install dependencies are not supported by the resolver.",
            )

        if any(s == PACKAGE_STATUS_RUNTIME_MISSING for s in statuses):
            return (
                RESOLVER_STATUS_RUNTIME_MISSING,
                RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_RUNTIME_MISSING]["error_code"],
                "The selected runtime path could not be resolved.",
            )

        if any(s == PACKAGE_STATUS_SOLVER_ERROR for s in statuses):
            error_msgs = [
                pkg.message or pkg.reason or pkg.name
                for pkg in plan.packages
                if pkg.status == PACKAGE_STATUS_SOLVER_ERROR
            ]
            return (
                RESOLVER_STATUS_SOLVER_ERROR,
                RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_SOLVER_ERROR]["error_code"],
                f"Dependency probe failed: {'; '.join(error_msgs[:3])}",
            )

        has_conda = any(s == PACKAGE_STATUS_CONDA_INSTALLABLE for s in statuses)
        has_fallback = any(
            pkg.status == PACKAGE_STATUS_FALLBACK_REQUIRED and pkg.fallback_available
            for pkg in plan.packages
        )
        has_unknown = any(
            s in {PACKAGE_STATUS_UNKNOWN, PACKAGE_STATUS_MANUAL_PREPARATION_REQUIRED}
            for s in statuses
        )

        # All packages have a final action: fully installable.
        if plan.installable and not plan.blocked:
            installer_summary = ", ".join(
                sorted({a.installer for a in plan.installable})
            )
            return (
                RESOLVER_STATUS_FULLY_INSTALLABLE,
                None,
                (
                    "All requested packages are installable via the resolver-approved "
                    f"installer set: {installer_summary}."
                ),
            )

        # Mixed installer families: partial.
        if has_conda and has_fallback:
            return (
                RESOLVER_STATUS_PARTIAL_RESOLUTION,
                RESOLVER_STATUS_PARTIAL_RESOLUTION,
                (
                    "Some packages are not installable through the configured conda "
                    "channels; the rest are installable but the resolver does not "
                    "execute a mixed-installer request."
                ),
            )

        # All-fallback: policy-aware.
        if has_fallback and not has_conda:
            policy = self._active_policy
            if fallback_policy_allows(policy) and plan.installable and not plan.blocked:
                # Single-family safe fallback already accepted.
                return (
                    RESOLVER_STATUS_FULLY_INSTALLABLE,
                    None,
                    "All requested packages are installable via the approved fallback family.",
                )
            if fallback_policy_allows(policy):
                return (
                    RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS,
                    RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS]["error_code"],
                    (
                        "Fallback families are available, but the resolver cannot reduce "
                        "the request to one safe registry family for every package."
                    ),
                )
            return (
                RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS,
                RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS]["error_code"],
                (
                    "Fallback families are available for every package, but the active "
                    "policy does not allow automatic fallback installation."
                ),
            )

        if has_unknown:
            return (
                RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED,
                RESOLVER_TO_P0_FIELDS[RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED]["error_code"],
                "The request cannot proceed automatically; manual preparation is required.",
            )

        return (
            RESOLVER_STATUS_RESOLUTION_UNKNOWN,
            "dependency_resolution_unknown",
            "The resolver could not classify the request.",
        )

    def _recommended_actions(self, plan: RuntimeDependencyResolutionPlan) -> list[str]:
        actions: list[str] = []
        if plan.status == RESOLVER_STATUS_FULLY_INSTALLABLE:
            actions.append("Call install_runtime_dependencies with the original payload; the resolver approved every package.")
            return actions

        if plan.installable:
            actions.append(
                "Do not call install_runtime_dependencies for the blocked subset. If the user wants only the installable subset, submit a narrower explicit request."
            )
        if plan.status == RESOLVER_STATUS_PARTIAL_RESOLUTION:
            actions.append(
                "Ask the user to manually prepare the missing packages or approve a narrower install of just the installable subset."
            )
        elif plan.status == RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS:
            actions.append(
                "Fallback families are available, but the active fallback policy is report_only. Switch the policy back to allow_safe_registry_install or proceed with manual preparation."
            )
        elif plan.status == RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS:
            actions.append(
                "Fallback families are available, but the resolver could not choose one safe registry family for every package. Submit a narrower explicit request after manual review."
            )
        elif plan.status == RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED:
            actions.append("Ask the user to prepare the runtime manually before retrying.")
        elif plan.status == RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC:
            actions.append("Replace source-style package specs with registry-backed bare names.")
        elif plan.status == RESOLVER_STATUS_RUNTIME_MISSING:
            actions.append("Verify the selected runtime path before retrying.")
        return actions

    def _probe_runtime(self, runtime: str, ecosystem: str) -> RuntimeProbeResult:
        """Return structured probe result. Never raises."""
        if not runtime:
            return RuntimeProbeResult(present=False, error_message="Runtime path is empty.")
        settings = _resolve_settings(self)
        try:
            if ecosystem.lower() == "python":
                # Lazy import to keep the resolver independent of the workers module.
                from app.workers.command_worker import CommandTemplateWorkerAdapter

                conda_base, env_path = CommandTemplateWorkerAdapter._resolve_conda_runtime(
                    runtime, settings
                )
                if not env_path.exists():
                    return RuntimeProbeResult(
                        present=False,
                        error_message=f"Python runtime not found: {env_path}",
                    )
                return RuntimeProbeResult(present=True, resolved_path=env_path)
            from app.workers.command_worker import CommandTemplateWorkerAdapter

            rscript = CommandTemplateWorkerAdapter._resolve_rscript_runtime(
                runtime, settings
            )
            if rscript is None or not rscript.exists():
                return RuntimeProbeResult(
                    present=False,
                    error_message=f"R runtime not found: {runtime}",
                )
            return RuntimeProbeResult(present=True, resolved_path=rscript)
        except Exception as exc:  # noqa: BLE001 - the resolver is best-effort.
            return RuntimeProbeResult(
                present=False,
                error_message=f"Could not resolve runtime: {exc}",
            )

    def _resolve_conda_solver(
        self, ecosystem: str | None = None, runtime_path: Path | None = None
    ) -> Path | None:
        """Locate the conda solver, preferring the runtime's own conda base."""
        if self._explicit_conda_solver:
            candidate = Path(self._explicit_conda_solver)
            if candidate.exists():
                return candidate

        # 1. Derive base from the resolved runtime path.
        if runtime_path is not None:
            runtime_base = derive_conda_base_from_runtime_path(runtime_path)
            if runtime_base is not None:
                solver = find_conda_solver(runtime_base)
                if solver is not None:
                    return solver

        # 2. Fallback to configured bases and canonical candidates.
        settings = _resolve_settings(self)
        configured_base = Path(
            getattr(settings, "executor_conda_base", default_conda_base())
        )
        for base in default_conda_base_candidates(configured_base):
            solver = find_conda_solver(base)
            if solver is not None:
                return solver

        # 3. Final fallback: $PATH.
        for name in ("mamba", "conda", "micromamba"):
            which = shutil.which(name)
            if which:
                return Path(which)
        return None

    def _channel_signature(
        self,
        conda_bin: Path | None,
        ecosystem: str,
        runtime: str,
        *,
        conda_base: Path | None = None,
        extra_channels: list[str] | None = None,
    ) -> str:
        """Stable, content-addressable signature for the probe environment.

        Includes:
        - the resolved conda solver's realpath (so a swap from mamba → mamba
          at a different path is treated as a new channel set);
        - the **actual** conda base used for the probe (not just settings);
        - the ecosystem casing (``python`` / ``R``);
        - the runtime name (so different runtimes that point at different
          channels get distinct entries);
        - the active fallback policy (so flipping
          ``report_only`` ↔ ``allow_safe_registry_install`` cannot poison a
          cache from the previous policy);
        - the list of channels actually configured for the solver, exposed
          via the resolver settings;
        - any per-request extra channels (e.g. bioconda for R ecosystem).
        """
        if conda_bin is None:
            return "no-conda"
        try:
            solver_realpath = str(conda_bin.resolve())
        except OSError:
            solver_realpath = str(conda_bin)
        settings = _resolve_settings(self)
        conda_base_str = ""
        if conda_base is not None:
            try:
                conda_base_str = str(conda_base.resolve())
            except OSError:
                conda_base_str = str(conda_base)
        else:
            if settings is not None:
                base = getattr(settings, "executor_conda_base", None)
                if base:
                    try:
                        conda_base_str = str(Path(base).resolve())
                    except OSError:
                        conda_base_str = str(base)
        runtime_str = str(runtime or "")
        policy = self._active_policy
        channels = _configured_channels(conda_bin, settings)
        extra = extra_channels or []
        return "|".join(
            [
                solver_realpath,
                conda_base_str,
                ecosystem,
                runtime_str,
                policy,
                ",".join(channels) if channels else "",
                ",".join(extra) if extra else "",
            ]
        )

    def _probe_conda(
        self,
        conda_bin: Path,
        candidates: list[str],
        *,
        ecosystem: str,
        extra_channels: list[str] | None = None,
    ) -> ProbeResult:
        """Probe conda channels for the first matching candidate.

        Returns a :class:`ProbeResult` so "found", "not_found", and
        "solver_error" are never conflated.
        """
        if not candidates:
            return ProbeResult(status="not_found")
        solver_name = conda_bin.name.lower()
        if solver_name in {"mamba", "micromamba"}:
            # Mamba-family solvers support multi-candidate repoquery search.
            # Do not fall back to search --json to avoid divergent behavior.
            return self._repoquery_search(conda_bin, candidates, extra_channels=extra_channels)
        for candidate in candidates:
            result = self._json_search_single(conda_bin, candidate, extra_channels=extra_channels)
            if result.status in {"found", "solver_error"}:
                return result
        return ProbeResult(status="not_found")

    def _repoquery_search(
        self,
        conda_bin: Path,
        candidates: list[str],
        extra_channels: list[str] | None = None,
    ) -> ProbeResult:
        cmd = [str(conda_bin), "repoquery", "search", "--json"]
        if extra_channels:
            cmd.append("--override-channels")
            for ch in extra_channels:
                cmd.extend(["-c", ch])
        cmd.extend(candidates)
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self._probe_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ProbeResult(
                status="solver_error",
                error_code="probe_timeout",
                error_detail=f"repoquery timed out after {exc.timeout}s",
            )
        except OSError as exc:
            return ProbeResult(
                status="solver_error",
                error_code="probe_os_error",
                error_detail=str(exc),
            )
        if result.returncode != 0:
            stderr = (result.stderr or "")[:500]
            if _is_packages_not_found(stderr):
                return ProbeResult(status="not_found")
            return ProbeResult(
                status="solver_error",
                error_code="mamba_solver_error",
                error_detail=stderr,
            )
        stdout = result.stdout or ""
        return self._parse_repoquery_output(stdout, candidates)

    def _parse_repoquery_output(self, stdout: str, candidates: list[str]) -> ProbeResult:
        """Parse repoquery search output, trying JSON first then tabular text."""
        found_names = self._extract_found_names(stdout, candidates)

        if found_names is None:
            # Unparseable non-blank stdout
            return ProbeResult(
                status="solver_error",
                error_code="unknown_probe_output",
                error_detail=stdout[:300],
            )

        for candidate in candidates:
            if candidate in found_names:
                return ProbeResult(status="found", match=candidate)
        return ProbeResult(status="not_found")

    @staticmethod
    def _try_extract_json(text: str) -> dict | None:
        """Try to parse JSON from text, including when prefixed with progress lines."""
        import json as _json

        try:
            return _json.loads(text)
        except ValueError:
            pass
        # micromamba may prepend progress text before the JSON object on stdout
        brace = text.find("{")
        if brace > 0:
            try:
                return _json.loads(text[brace:])
            except ValueError:
                pass
        return None

    def _json_search_single(self, conda_bin: Path, candidate: str, extra_channels: list[str] | None = None) -> ProbeResult:
        """Probe one conda candidate at a time (required for ``conda``)."""
        cmd = [str(conda_bin), "search", "--json"]
        if extra_channels:
            cmd.append("--override-channels")
            for ch in extra_channels:
                cmd.extend(["-c", ch])
        cmd.append(candidate)
        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self._probe_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ProbeResult(
                status="solver_error",
                error_code="probe_timeout",
                error_detail=f"conda search timed out after {exc.timeout}s",
            )
        except OSError as exc:
            return ProbeResult(
                status="solver_error",
                error_code="probe_os_error",
                error_detail=str(exc),
            )
        if result.returncode != 0:
            stderr = (result.stderr or "")[:500]
            if _is_packages_not_found(stderr):
                return ProbeResult(status="not_found")
            return ProbeResult(
                status="solver_error",
                error_code="conda_solver_error",
                error_detail=stderr,
            )
        import json as _json
        stdout = result.stdout or ""
        try:
            payload = _json.loads(stdout)
        except ValueError:
            if stdout.strip():
                return ProbeResult(
                    status="solver_error",
                    error_code="unknown_probe_output",
                    error_detail=stdout[:300],
                )
            return ProbeResult(status="not_found")
        if not isinstance(payload, dict):
            return ProbeResult(status="not_found")
        entry = payload.get(candidate)
        if not entry:
            return ProbeResult(status="not_found")
        if isinstance(entry, dict) and entry.get("result"):
            return ProbeResult(status="found", match=candidate)
        if isinstance(entry, list) and entry:
            return ProbeResult(status="found", match=candidate)
        return ProbeResult(status="not_found")


# ---------------------------------------------------------------------------
# Module-level helpers (exported for tests and shared use)
# ---------------------------------------------------------------------------


def _looks_like_source_spec(pkg: str) -> bool:
    lowered = pkg.lower()
    return any(token in lowered for token in SOURCE_SPEC_PATTERNS) or lowered.startswith("git+") or "/" in pkg


# Characters that must not appear in any package name destined for a shelled
# installer invocation.  The set includes classic shell metacharacters plus a
# few extras that are harmless in isolation but risky in a structured command
# builder (e.g. backtick for command substitution, newline for multi-command
# injection).
_SHELL_DANGER_CHARS = re.compile(r"[;&|$`'\"\\()\n\r\0]")
# Editable-install prefix or local-path prefix for pip.
_PIP_EDITABLE_LOCAL_RE = re.compile(r"^(-e\s+|\./|\.\./|~|/)", re.IGNORECASE)


def _contains_shell_danger(name: str) -> bool:
    """True when ``name`` contains shell metacharacters that make it unsafe."""
    if _SHELL_DANGER_CHARS.search(name):
        return True
    if _PIP_EDITABLE_LOCAL_RE.match(name):
        return True
    return False


def _is_valid_grammar(pkg: str, ecosystem: str) -> bool:
    if ecosystem == "python":
        return bool(VERSION_PIN_GRAMMAR.fullmatch(pkg))
    return bool(BARE_NAME_GRAMMAR.fullmatch(pkg))


def _normalize_for_ecosystem(pkg: str, ecosystem: str) -> str:
    return pkg.strip().lower()


def _classify_for_ecosystem(pkg: str, ecosystem: str) -> str:
    if ecosystem.lower() == "r":
        return "r-package"
    return "python-package"


def _fallback_families_for(ecosystem: str) -> list[str]:
    if ecosystem.lower() == "r":
        return list(FALLBACK_FAMILIES_R)
    return list(FALLBACK_FAMILIES_PYTHON)


def _single_safe_fallback_family(packages: Iterable[ResolverPackageEntry]) -> str | None:
    entries = list(packages)
    if not entries:
        return None
    families: set[str] = set()
    for pkg in entries:
        if len(pkg.fallback_available) != 1:
            families.clear()
            break
        family = str(pkg.fallback_available[0] or "").strip()
        if not family:
            return None
        families.add(family)
    if len(families) == 1:
        return next(iter(families))

    # Current R fallback hints often surface both cran and bioconductor. When
    # every blocked package includes cran, prefer cran as the default registry
    # family rather than rejecting the whole request as ambiguous.
    if all("cran" in pkg.fallback_available for pkg in entries):
        allowed = {"cran", "bioconductor"}
        if all(set(pkg.fallback_available).issubset(allowed) for pkg in entries):
            return "cran"
    return None


def _select_r_fallback_family(packages: Iterable[ResolverPackageEntry]) -> str | None:
    """Choose a single safe fallback family for R packages.

    Priority:
    1. Any package is known to be Bioconductor-only -> bioconductor
       (BiocManager::install can also satisfy CRAN dependencies).
    2. Every package resolves to exactly one family -> that family.
    3. Every blocked package includes cran (R dual-source hints) -> cran.
    4. Otherwise ambiguous -> None.
    """
    entries = list(packages)
    if not entries:
        return None
    lowered = {e.name.lower() for e in entries}
    if lowered & BIOCONDUCTOR_ONLY_PACKAGES:
        return "bioconductor"
    return _single_safe_fallback_family(entries)


def _conda_candidates_for(pkg: str, ecosystem: str) -> list[str]:
    """Return the conda-family candidate names to probe for ``pkg``."""
    if ecosystem.lower() == "r":
        lowered = pkg.strip().lower()
        return [f"r-{lowered}", f"bioconductor-{lowered}"]
    base = pkg.strip()
    lowered = base.lower()
    return [base, lowered, lowered.replace("_", "-"), lowered.replace("-", "_")]


def _is_packages_not_found(stderr: str) -> bool:
    """True when stderr indicates the package is absent (not a solver bug)."""
    lowered = stderr.lower()
    return any(
        token in lowered
        for token in (
            "packagesnotfounderror",
            "no match found",
            "package not found",
            "nothing provides",
        )
    )


def _resolver_settings_stub() -> Any:
    """Return a settings-like object that the worker adapter accepts.

    The resolver only needs to call ``_resolve_conda_runtime`` /
    ``_resolve_rscript_runtime`` for path resolution. We construct a minimal
    duck-typed stub here so the resolver does not depend on the project service
    for the runtime lookup.
    """

    class _Settings:
        executor_conda_base = Path.home() / "miniconda3"
        default_r_runtime = None

    return _Settings()


def _configured_channels(conda_bin: Path, settings: Any | None) -> list[str]:
    """Return the ordered list of channels the solver will actually use.

    The default conda solver auto-discovers channels from
    ``~/.condarc`` / the conda base, but we cannot reliably parse that file
    from a forked backend. We do the next best thing: inspect the conda
    solver's own ``config`` output if available, falling back to an empty
    list (which still produces a stable signature based on the solver
    realpath and ecosystem).
    """
    if conda_bin is None:
        return []
    solver_name = conda_bin.name.lower()
    if solver_name in {"micromamba"}:
        cmd = [str(conda_bin), "config", "list", "--json"]
    else:
        cmd = [str(conda_bin), "config", "--show", "channels", "--json"]
    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not (result.stdout or "").strip():
        return []
    try:
        import json as _json
        payload = _json.loads(result.stdout)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("channels") or []
    if not isinstance(raw, list):
        return []
    # conda sometimes lists defaults at the start; preserve declared order.
    return [str(item) for item in raw if item]


def _resolve_settings(resolver_or_plan: Any) -> Any:
    """Best-effort lookup of the settings object to pass to worker helpers.

    Looks at the plan's ``_settings`` attribute first (set by ``resolve`` when
    called from a manager tool), then at the resolver service's
    ``_settings`` attribute, then falls back to a stub so direct callers
    (unit tests) keep working.
    """
    if hasattr(resolver_or_plan, "_settings"):
        return resolver_or_plan._settings
    return _resolver_settings_stub()


def is_registry_fallback_action_safe(action: ResolverInstallAction | dict[str, Any]) -> bool:
    """True when ``action`` is a safe structured fallback that the installer may execute.

    Safety rules (P1.3 hardening):
    - ``pip``: bare name or ``name==exact_version`` only (no extras ``[]``, no
      editable ``-e``, no local paths, no URLs, no VCS refs, no arbitrary flags).
    - ``cran`` / ``bioconductor``: bare R package name (``^[A-Za-z][A-Za-z0-9.]*$``)
      with no quotes, no semicolons, no callbacks.
    - Always rejected: shell metacharacters (``;``, ``|``, ``$``, ``(``, ``)``,
      backticks, newlines), source-style specs (URLs, GitHub, tarballs), and
      any action whose installer is not one of the three approved families.
    """
    payload: dict[str, Any]
    if isinstance(action, ResolverInstallAction):
        if action.installer not in {"pip", "cran", "bioconductor"}:
            return False
        payload = action.to_dict()
    elif isinstance(action, dict):
        if action.get("installer") not in {"pip", "cran", "bioconductor"}:
            return False
        payload = action
    else:
        return False

    installer = str(payload.get("installer") or "").strip()
    name = str(payload.get("name") or "").strip()
    version_pin = str(payload.get("version_pin") or "").strip() or None

    # Reject empty / obviously dangerous names.
    if not name:
        return False
    if _contains_shell_danger(name):
        return False
    if _looks_like_source_spec(name):
        return False

    if installer == "pip":
        # Must be ``^name$`` or ``^name==version$``. No extras, no editable,
        # no paths, no URLs, no VCS.
        if version_pin is None:
            return bool(VERSION_PIN_GRAMMAR.fullmatch(name))
        candidate = f"{name}=={version_pin}"
        return bool(VERSION_PIN_GRAMMAR.fullmatch(candidate))
    else:
        # cran / bioconductor: bare R package name only, no version pin
        # or extra syntax.
        if version_pin is not None:
            return False
        if payload.get("candidate") is not None:
            return False
        return bool(BARE_NAME_GRAMMAR.fullmatch(name)) and name.strip() != ""


def collect_fallback_actions(
    plan: RuntimeDependencyResolutionPlan,
    *,
    policy: str,
) -> list[ResolverInstallAction]:
    """Translate resolver blocked entries into structured fallback actions.

    - ``report_only`` returns an empty list.
    - ``allow_safe_registry_install`` returns one ``ResolverInstallAction`` per
      blocked entry only when the resolver can reduce the request to one safe
      registry family. For current R dual-source hints, the resolver prefers
      CRAN when every blocked package includes `cran`.
    """
    if policy != "allow_safe_registry_install":
        return []
    fallback_packages = [
        entry for entry in plan.packages
        if entry.status == PACKAGE_STATUS_FALLBACK_REQUIRED
    ]
    is_r_ecosystem = bool(
        fallback_packages and fallback_packages[0].classification == "r-package"
    )
    if is_r_ecosystem:
        family = _select_r_fallback_family(fallback_packages)
    else:
        family = _single_safe_fallback_family(fallback_packages)
    if family is None:
        return []
    actions: list[ResolverInstallAction] = []
    for entry in fallback_packages:
        candidate = ResolverInstallAction(
            installer=family,
            name=entry.name,
        )
        if is_registry_fallback_action_safe(candidate):
            actions.append(candidate)
    return actions


def fallback_policy_allows(policy: str | None) -> bool:
    return policy == "allow_safe_registry_install"


def normalize_fallback_policy(value: str | None) -> str:
    if value in (None, "", "allow_safe_registry_install"):
        return "allow_safe_registry_install"
    if value == "allow_safe_registry_install":
        return "allow_safe_registry_install"
    if value == "report_only":
        return "report_only"
    return "allow_safe_registry_install"


def summarize_blocked(plan: RuntimeDependencyResolutionPlan) -> Iterable[dict[str, Any]]:
    for entry in plan.blocked:
        yield entry.to_dict()
