"""Repository-wide pytest configuration: every test runs hermetically.

* All Qazterion state (task DB, keystore, usage log, provider health, index
  cache) goes to a temporary data directory, never the user's real one.
* ``.env`` files are not loaded and provider key variables are removed, so no
  test can spend a real API quota.
* The process-wide model gateway is replaced by an offline one whose adapters
  always fail with a connection error; tests that exercise routing build their
  own gateway with fake adapters.
* Process-wide singletons are reset before and after each test.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="qazterion_tests_"))
os.environ["QAZTERION_DATA_DIR"] = str(_SESSION_DIR / "data")
os.environ["QAZTERION_TASKS_DB"] = str(_SESSION_DIR / "data" / "qazterion_tasks_test.db")
os.environ["QAZTERION_SKIP_DOTENV"] = "1"
os.environ.pop("QAZTERION_KEYSTORE_PATH", None)
os.environ.pop("QAZTERION_USAGE_LOG_PATH", None)
_KEY_VAR = re.compile(r"^[A-Z0-9_]+_(KEY_\d+|API_KEY)$")
for _name in [n for n in os.environ if _KEY_VAR.match(n)]:
    os.environ.pop(_name, None)


def _offline_gateway():
    from qz_providers.catalog import ProviderCatalog
    from qz_providers.exceptions import ConnectionError as ProviderConnectionError
    from qz_providers.gateway import ModelGateway
    from qz_providers.health import HealthTracker
    from qz_providers.keys import KeySource

    class _OfflineAdapter:
        def __init__(self, spec):
            self.spec = spec

        def complete(self, **_kwargs):
            raise ProviderConnectionError("offline test environment")

        def list_models(self, **_kwargs):
            raise ProviderConnectionError("offline test environment")

        def check_key(self, **_kwargs):
            raise ProviderConnectionError("offline test environment")

        def close(self):
            pass

    catalog = ProviderCatalog()
    return ModelGateway(
        catalog=catalog,
        keys=KeySource(catalog, keystore_factory=None, environ={}),
        health=HealthTracker(persist=False),
        adapter_factory=_OfflineAdapter,
        sleep=lambda _s: None,
        log=lambda _m: None,
    )


def _reset_all() -> None:
    from qz_providers.gateway import set_gateway
    from qz_sandbox.manager import reset_manager
    from qz_security.gateway import configure as configure_security
    from qz_tasks.task_manager import reset_manager as reset_task_manager
    from qz_usage_tracker import reset_usage_tracker
    import qz_indexer
    import qz_tools

    set_gateway(_offline_gateway())
    reset_manager()
    reset_task_manager()
    reset_usage_tracker()
    configure_security(ask_handler=None)
    qz_indexer._MEMORY_CACHE.clear()
    qz_tools.reset_task_state()


@pytest.fixture(autouse=True)
def _hermetic_singletons():
    _reset_all()
    yield
    _reset_all()
