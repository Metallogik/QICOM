"""The suite must never read or write the developer's real user profile.

QICOM persists real state per user - the recent-projects shortcut list, the
session database, preferences, the evaluator identity, downloaded IBSI
reference sets. Before the global redirect in `tests/conftest.py`, any test
that exercised one of those paths mutated the developer's own profile, and the
same test failed outright wherever that profile is read-only (a locked-down CI
image, a container run as a foreign uid). These tests pin the redirect down.
"""

from __future__ import annotations

import os
from pathlib import Path

import learned_checkpoint_bridge
import platformdirs
import pytest

from qicom.gui import preferences as preferences_module
from qicom.metrics import learned_weights
from qicom.metrics import nss_reference as nss_reference_module
from qicom.radiomics import ibsi_validation as ibsi_validation_module
from qicom.session import app_logging as app_logging_module
from qicom.session import evaluation_package as evaluation_package_module
from qicom.session import recent_projects as recent_projects_module
from qicom.session import storage as storage_module
from qicom.session.recent_projects import load_recent_projects, record_recent_project

# Resolved at import time, i.e. before any per-test redirect is installed.
_REAL_HOME = Path.home().resolve()
_REAL_DATA_DIR = Path(platformdirs.user_data_dir("qicom", appauthor=False)).resolve()


def _is_under(path: Path, ancestor: Path) -> bool:
    return ancestor == path or ancestor in path.resolve().parents


class TestEveryUserPathIsRedirected:
    """One assertion per real call site that resolves a per-user location."""

    @pytest.mark.parametrize(
        "resolve",
        [
            pytest.param(
                lambda: recent_projects_module._recent_projects_path(), id="recent_projects"
            ),
            pytest.param(lambda: storage_module.default_db_path(), id="session_database"),
            pytest.param(lambda: preferences_module.default_preferences_path(), id="preferences"),
            pytest.param(lambda: app_logging_module._platform_log_path(), id="log_file"),
            pytest.param(
                lambda: ibsi_validation_module.default_reference_cache(),
                id="ibsi_reference_sets",
            ),
        ],
    )
    def test_resolves_outside_the_real_profile(self, resolve, _isolated_user_directories) -> None:
        path = Path(resolve())

        assert _is_under(path, _isolated_user_directories)
        assert not _is_under(path, _REAL_DATA_DIR)

    def test_home_environment_is_redirected(self, _isolated_user_directories: Path) -> None:
        assert Path.home().resolve() != _REAL_HOME
        assert _is_under(Path.home(), _isolated_user_directories)
        for variable in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TORCH_HOME"):
            assert _is_under(Path(os.environ[variable]), _isolated_user_directories), variable

    def test_the_nss_reference_cache_is_redirected(self, _isolated_user_directories: Path) -> None:
        cache_dir = Path(nss_reference_module.platformdirs.user_cache_dir("qicom"))

        assert _is_under(cache_dir, _isolated_user_directories)

    def test_the_evaluation_package_user_dir_is_redirected(
        self, _isolated_user_directories: Path
    ) -> None:
        base = Path(evaluation_package_module.platformdirs.user_data_dir("qicom"))

        assert _is_under(base, _isolated_user_directories)

    def test_each_test_gets_a_fresh_profile(self, _isolated_user_directories: Path) -> None:
        # Recorded by the *previous* test in this class would leak in here if
        # the redirect were session-scoped instead of per-test.
        assert load_recent_projects() == []
        marker = _isolated_user_directories / "marker"
        marker.mkdir()
        record_recent_project(marker, "quantitative")
        assert len(load_recent_projects()) == 1

    def test_each_test_gets_a_fresh_profile_again(self) -> None:
        assert load_recent_projects() == []


class TestReadOnlyProfile:
    """A profile the process cannot write to must degrade, never abort."""

    @staticmethod
    def _read_only_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        base = tmp_path / "locked_profile"
        (base / "data").mkdir(parents=True)
        base.chmod(0o500)
        (base / "data").chmod(0o500)
        monkeypatch.setattr(
            platformdirs, "user_data_dir", lambda *a, **k: str(base / "data" / "qicom")
        )
        return base

    @pytest.fixture(autouse=True)
    def _skip_when_root(self) -> None:
        if os.name == "posix" and os.geteuid() == 0:
            pytest.skip("root ignores the write bits this test relies on")

    def test_recording_a_recent_project_degrades_quietly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._read_only_profile(tmp_path, monkeypatch)
        project = tmp_path / "project"
        project.mkdir()

        record_recent_project(project, "quantitative", name="Study")

        assert load_recent_projects() == []

    def test_loading_recent_projects_degrades_quietly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._read_only_profile(tmp_path, monkeypatch)

        assert load_recent_projects() == []

    def test_logging_falls_back_to_a_private_temporary_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = tmp_path / "locked_profile"
        (base / "log").mkdir(parents=True)
        (base / "log").chmod(0o500)
        monkeypatch.setattr(
            platformdirs, "user_log_dir", lambda *a, **k: str(base / "log" / "qicom")
        )

        with pytest.warns(RuntimeWarning, match="cannot use the system log directory"):
            logger = app_logging_module.configure_logging(console=False)

        try:
            logger.info("still logging")
            assert not _is_under(app_logging_module.default_log_path(), base)
        finally:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)


class TestPreparedLearnedCheckpointsStayReachable:
    """The redirect must hide the profile, not the learned-metric weights.

    LPIPS and DISTS refuse to run without their checkpoints and never fetch
    one mid-analysis, so the CI job downloads them first with
    packaging/prepare_learned_weights.py. Those land in the *real* cache,
    which the redirect above then points away from - and the learned tests,
    which cannot run at all without real weights, started failing on every
    push while passing on any machine without the `learned` extra, where
    they are skipped instead. learned_checkpoint_bridge is the
    bridge; these tests are what keep it honest.
    """

    def test_a_prepared_checkpoint_is_linked_into_the_profile(self, tmp_path: Path) -> None:
        # The smallest pinned checkpoint: is_installed() compares against the
        # pinned size, and the two backbones are 244 and 553 MB.
        name = min(
            learned_weights.CHECKPOINTS, key=lambda n: learned_weights.CHECKPOINTS[n].size_bytes
        )
        checkpoint = learned_weights.CHECKPOINTS[name]
        source = tmp_path / "real-cache" / name
        source.parent.mkdir(parents=True)
        source.write_bytes(b"x" * checkpoint.size_bytes)
        base = tmp_path / "profile"

        with pytest.MonkeyPatch.context() as patch:
            learned_checkpoint_bridge.link_prepared_checkpoints(base, sources=(source,))
            patch.setenv("TORCH_HOME", str(learned_checkpoint_bridge.fake_torch_home(base)))

            assert learned_weights.checkpoint_path(name).read_bytes() == source.read_bytes()
            assert learned_weights.is_installed(name)

    def test_the_bridge_writes_where_the_redirect_reads(self, tmp_path: Path) -> None:
        """The one way the two halves can drift apart: a bridge that
        populates a directory `checkpoints_dir()` does not resolve to."""
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("TORCH_HOME", str(learned_checkpoint_bridge.fake_torch_home(tmp_path)))

            assert (
                learned_weights.checkpoints_dir().parent.parent
                == learned_checkpoint_bridge.fake_torch_home(tmp_path)
            )

    def test_no_prepared_checkpoint_leaves_the_profile_empty(self, tmp_path: Path) -> None:
        """A machine that never prepared the weights must not gain an empty
        directory that looks like a cache."""
        learned_checkpoint_bridge.link_prepared_checkpoints(tmp_path, sources=())

        assert not (tmp_path / "cache").exists()

    def test_every_prepared_checkpoint_is_visible_from_inside_a_test(
        self, _isolated_user_directories: Path
    ) -> None:
        """End to end, in a redirected profile like any other test's.

        Vacuous where the weights were never downloaded - which is every
        base install, and why the regression reached CI unseen. The full CI
        job prepares them, so there it asserts the thing that actually broke.
        """
        for source in learned_checkpoint_bridge.PREPARED_CHECKPOINTS:
            assert learned_weights.is_installed(source.name), source.name
            # Lexical on purpose: resolve() would follow the link back out to
            # the real cache, which is the whole point of the bridge.
            assert learned_weights.checkpoint_path(source.name).is_relative_to(
                _isolated_user_directories
            )
