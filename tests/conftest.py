"""Test isolation helpers shared by QICOM's whole suite.

The most important of these is `_isolated_user_directories`: every test runs
with the per-user data/config/cache/log/state directories redirected into its
own temporary tree. QICOM writes real files into those locations at runtime
(the recent-projects shortcut list, the session SQLite database, preferences,
the evaluator identity, downloaded IBSI reference sets, learned checkpoints),
so without a global redirect a single unguarded test silently mutates the
developer's own profile - and fails outright when that profile is read-only.
"""

from __future__ import annotations

import gc
import importlib.util
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import platformdirs
import pytest
from learned_checkpoint_bridge import fake_torch_home, link_prepared_checkpoints
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from qicom.gui import workers
from qicom.gui.workers import wait_for_active_jobs

# platformdirs accessor name -> subdirectory of the per-test fake home. The
# split mirrors the real per-OS layout closely enough that code which expects
# cache and data to be distinct locations still sees two distinct locations.
_USER_DIR_KINDS: dict[str, str] = {
    "user_data": "data",
    "user_config": "config",
    "user_cache": "cache",
    "user_state": "state",
    "user_log": "log",
    "user_runtime": "runtime",
    "user_documents": "documents",
    "user_downloads": "downloads",
    "user_desktop": "desktop",
    "user_pictures": "pictures",
    "user_videos": "videos",
    "user_music": "music",
}

REAL_USER_DIRS_MARKER = "real_user_dirs"
_REAL_USER_DIR_SNAPSHOT = pytest.StashKey[dict]()


def _fake_user_dir(base: Path, kind: str, appname: str | None, ensure_exists: bool) -> Path:
    target = base / kind
    if appname:
        target = target / appname
    if ensure_exists:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _redirect_platformdirs(monkeypatch: pytest.MonkeyPatch, base: Path) -> None:
    """Point every `platformdirs.user_*_dir/_path` accessor inside `base`.

    Patching the attributes of the `platformdirs` module itself (rather than a
    per-module copy) covers every QICOM module at once, because they all call
    `platformdirs.user_*_dir(...)` through the shared module object at call
    time. Third-party libraries that resolve user directories the same way are
    contained too.
    """
    for prefix, kind in _USER_DIR_KINDS.items():
        for suffix, cast in (("_dir", str), ("_path", Path)):
            name = f"{prefix}{suffix}"
            if not hasattr(platformdirs, name):
                continue

            def fake(
                appname: str | None = None,
                *_args: Any,
                _kind: str = kind,
                _cast: Any = cast,
                ensure_exists: bool = False,
                **_kwargs: Any,
            ) -> Any:
                return _cast(_fake_user_dir(base, _kind, appname, ensure_exists))

            monkeypatch.setattr(platformdirs, name, fake)


def _redirect_home_environment(monkeypatch: pytest.MonkeyPatch, base: Path) -> None:
    """Contain the environment variables that resolve a home directory.

    `platformdirs` is not the only route to the user's profile: `Path.home()`
    (downloads_dir, app_logging's redaction) reads HOME/USERPROFILE, torch
    reads TORCH_HOME, and any subprocess inherits whatever is set here.
    """
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(base / "config"))
    monkeypatch.setenv("LOCALAPPDATA", str(base / "data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(base / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(base / "state"))
    monkeypatch.setenv("TORCH_HOME", str(fake_torch_home(base)))


@pytest.fixture(autouse=True)
def _cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> int:
    """Evaluator accounts derive their key with scrypt at about 0.2 s and
    128 MiB a call; every login a test makes would pay it. The cost is
    stored with each account, so lowering it here changes nothing else.
    Returns the production value, which test_evaluator_accounts.py checks."""
    from qicom.session import key_derivation

    production = key_derivation.SCRYPT_N
    monkeypatch.setattr(key_derivation, "SCRYPT_N", 2**4)
    return production


@pytest.fixture(autouse=True)
def _isolated_user_directories(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path | None]:
    """Give every test its own throwaway user profile.

    Opt out with `@pytest.mark.real_user_dirs` in the few tests that must
    assert how QICOM resolves the *real* per-OS locations. Those tests may
    read the real paths but must never write to them.
    """
    if request.node.get_closest_marker(REAL_USER_DIRS_MARKER) is not None:
        yield None
        return

    base = tmp_path_factory.mktemp("user-home")
    _redirect_platformdirs(monkeypatch, base)
    _redirect_home_environment(monkeypatch, base)
    link_prepared_checkpoints(base)
    yield base


# Resolved once, at import time, while `platformdirs` is still the real
# thing. Re-resolving later would be wrong: a test whose teardown raised can
# leave the redirect in place, and the tripwire below would then compare the
# real profile against an empty temporary one and blame the suite for every
# file it did not touch.
_REAL_QICOM_USER_PATHS: tuple[Path, ...] = (
    Path(platformdirs.user_data_dir("qicom", appauthor=False)),
    Path(platformdirs.user_config_dir("qicom", appauthor=False)),
    Path(platformdirs.user_cache_dir("qicom", appauthor=False)),
    Path(platformdirs.user_log_dir("qicom", appauthor=False)),
)


def _snapshot_real_user_dirs() -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for root in _REAL_QICOM_USER_PATHS:
        if not root.exists():
            continue
        for entry in root.rglob("*"):
            try:
                stat = entry.stat()
            except OSError:
                continue
            snapshot[str(entry)] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{REAL_USER_DIRS_MARKER}: opt out of the temporary-profile redirect and "
        "resolve the real per-OS user directories (read-only)",
    )
    config.stash[_REAL_USER_DIR_SNAPSHOT] = _snapshot_real_user_dirs()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run if the suite touched the developer's real QICOM profile.

    The redirect above is the fix; this is the tripwire that keeps it honest
    when a new test, or a new call site, escapes it.
    """
    before = session.config.stash.get(_REAL_USER_DIR_SNAPSHOT, None)
    if before is None:
        return
    after = _snapshot_real_user_dirs()
    touched = sorted(set(before) ^ set(after)) + sorted(
        path for path in set(before) & set(after) if before[path] != after[path]
    )
    if not touched:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            "ERROR: the test suite wrote into the real QICOM user profile:", red=True
        )
        for path in touched[:20]:
            reporter.write_line(f"  {path}", red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True, scope="session")
def _trace_worker_threads() -> Iterator[None]:
    """Let coverage see the code QICOM runs in its worker QThreads.

    coverage.py's tracer follows threads started by `threading` only. Once a
    QApplication exists, every background job runs in a QThread and was
    invisible: evaluation_export.py measured 99% in isolation and 13% in the
    full suite, so the coverage floors depended on test order. Installing the
    tracer `threading` would have given a new thread is exactly what coverage
    itself does for its own threads; without coverage running it is a no-op.
    """
    original_run = workers._FunctionThread.run

    def traced_run(self: workers._FunctionThread) -> None:
        tracer = threading.gettrace()
        if tracer is not None:
            sys.settrace(tracer)
        original_run(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(workers._FunctionThread, "run", traced_run)
        yield


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item):  # type: ignore[no-untyped-def]
    """Actually destroy widgets that pytest-qt scheduled with deleteLater().

    QApplication.processEvents() alone does not deliver DeferredDelete when
    no outer Qt event loop is running. Without this flush, hundreds of closed
    image widgets accumulate during the headless suite and eventually starve
    subsequent QThreads of memory/scheduling time.
    """
    is_gui_test = "gui" in item.path.parts
    app = QApplication.instance()
    if is_gui_test and app is not None and not wait_for_active_jobs(timeout_ms=120_000):
        pytest.fail("QICOM background jobs did not finish during GUI test teardown.")

    result = yield

    if is_gui_test and app is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        gc.collect()
    return result


LEARNED_EXTRA_MODULES = ("torch", "piq", "lpips")


def missing_learned_modules() -> tuple[str, ...]:
    """Names of the `learned` extra's imports that are absent from this venv."""
    missing = []
    for name in LEARNED_EXTRA_MODULES:
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    return tuple(missing)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip learned-metric tests with a stated cause when the extra is absent.

    The base install deliberately omits torch/piq/lpips (see the `learned`
    extra in pyproject.toml), so these tests cannot run there. They are skipped
    with the missing package named rather than left to fail on ImportError, and
    the full CI job installs the extra so they execute for real.
    """
    missing = missing_learned_modules()
    if not missing:
        return
    skip = pytest.mark.skip(
        reason=(
            "requires the 'learned' extra: missing "
            + ", ".join(missing)
            + " (install with: pip install -e '.[learned]')"
        )
    )
    for item in items:
        if item.get_closest_marker("requires_learned") is not None:
            item.add_marker(skip)
