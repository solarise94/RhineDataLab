"""Pytest configuration shared by the backend test suite.

The suite is unittest.TestCase based; this conftest exists only to register
the ``serial`` marker used to flag tests that must NOT run under
``pytest-xdist`` parallel workers because they touch cross-process shared
state (e.g. ``test_install_deploy`` reads/writes the real repo ``.env``).

Running the suite with the wrapper script (``scripts/run_backend_tests.sh``)
or the documented two-command fast path keeps serial tests on a single,
non-xdist process. The marker is registered here so ``pytest`` does not warn
about unknown markers.
"""
from __future__ import annotations


# Modules whose tests must run serially (not under pytest-xdist) because they
# touch cross-process shared state. Add new modules here instead of scattering
# marker decorators across test classes.
SERIAL_TEST_MODULES = {"test_install_deploy"}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "serial: must run serially (not under pytest-xdist) due to shared "
        "filesystem/env state across processes; see run_backend_tests.sh.",
    )


def pytest_collection_modifyitems(config, items):
    """Auto-tag tests in serial modules so they cannot quietly slip into the
    parallel worker pool and race on shared repo state."""
    for item in items:
        module_name = item.module.__name__.rsplit(".", 1)[-1]
        if module_name in SERIAL_TEST_MODULES:
            item.add_marker("serial")
