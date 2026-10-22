"""Verify the locally generated architecture map and the Qt-free core layers."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
GENERATOR = REPO_ROOT / "packaging" / "generate_architecture_map.py"
generator = runpy.run_path(str(GENERATOR))


@pytest.fixture(scope="module")
def generated_map(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("architecture") / "ARCHITECTURE.md"
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--output", str(output)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return output


class TestTheMapIsCurrent:
    def test_the_cli_writes_the_rendered_map(self, generated_map: Path) -> None:
        assert generated_map.read_text(encoding="utf-8") == generator["render"]()

    def test_the_check_mode_agrees(self, generated_map: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check", "--output", str(generated_map)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr

    def test_check_mode_fails_on_a_stale_file(self, tmp_path: Path) -> None:
        """A checker that cannot fail is not a checker."""
        stale = tmp_path / "ARCHITECTURE.md"
        stale.write_text("# not the map\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check", "--output", str(stale)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "stale" in result.stderr


class TestTheMapIsUseful:
    """A map that is merely correct but says nothing is a directory listing."""

    def test_every_module_in_the_package_appears(self, generated_map: Path) -> None:
        text = generated_map.read_text(encoding="utf-8")
        missing = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in sorted((REPO_ROOT / "qicom").rglob("*.py"))
            if "__pycache__" not in path.parts
            and path.name != "_build_identity.py"
            and f"`{path.relative_to(REPO_ROOT).as_posix()}`" not in text
        ]

        assert missing == [], missing

    def test_every_module_carries_a_description(self, generated_map: Path) -> None:
        """All 167 modules have a docstring; a blank cell would mean the
        extraction broke, not that the module is undocumented."""
        rows = [
            line
            for line in generated_map.read_text(encoding="utf-8").splitlines()
            if line.startswith("| `qicom/") and line.endswith(" |")
        ]
        blank = [row for row in rows if row.rsplit("|", 2)[1].strip() == ""]

        assert rows, "no module rows in the map at all"
        assert blank == [], blank

    def test_it_answers_where_the_entry_points_are(self, generated_map: Path) -> None:
        text = generated_map.read_text(encoding="utf-8")

        assert "qicom.cli.main:main" in text
        assert "qicom.gui.app:main" in text

    def test_it_answers_which_packages_must_stay_qt_free(self, generated_map: Path) -> None:
        text = generated_map.read_text(encoding="utf-8")

        for package in generator["QT_FREE_PACKAGES"]:
            assert f"`qicom.{package}`" in text, package

    def test_it_answers_where_the_persisted_schemas_are(self, generated_map: Path) -> None:
        text = generated_map.read_text(encoding="utf-8")

        assert "PERSISTENCE_SCHEMA_VERSION" in text
        assert "EVALUATION_FORMAT_VERSION" in text

    def test_it_answers_where_the_trust_boundaries_are(self, generated_map: Path) -> None:
        text = generated_map.read_text(encoding="utf-8")

        assert "qicom/session/archive_safety.py" in text
        assert "qicom/io/dicom_anonymize.py" in text

    def test_it_says_it_is_generated_and_how_to_regenerate_it(self, generated_map: Path) -> None:
        text = generated_map.read_text(encoding="utf-8")

        assert "File generato" in text
        assert "packaging/generate_architecture_map.py" in text


class TestTheLayeringRuleIsVerifiedNotAsserted:
    """The map states that the core imports without Qt. It must establish
    that by importing, not by repeating a claim from a docstring."""

    @pytest.mark.parametrize("package", ["metrics", "io", "session", "radiomics", "reporting"])
    def test_each_core_package_imports_without_qt(self, package: str) -> None:
        report = generator["_probe_qt_import"](f"qicom.{package}")
        if (
            package == "radiomics"
            and report.status == "unimportable"
            and report.missing_module == "SimpleITK"
        ):
            pytest.skip(f"Qt boundary not verified without the radiomics extra: {report.detail}")
        # Unexpected import errors fail too, with a different diagnostic from Qt imports.
        assert report.status == "qt-free", report

    def test_a_qt_import_in_a_submodule_is_caught(self, tmp_path: Path) -> None:
        """Importing only the package used to vouch for every module in it:
        an `__init__` that imports nothing hid a Qt import one level down."""
        package = tmp_path / "fakecore"
        (package / "deep").mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "deep" / "__init__.py").write_text("")
        (package / "deep" / "widgets.py").write_text("import PySide6.QtCore\n")

        report = generator["_probe_qt_import"]("fakecore", extra_path=tmp_path)

        assert report.status == "imports-qt", report

    @pytest.mark.parametrize("module", ["batch", "cli", "provenance", "runtime", "i18n"])
    def test_the_modules_outside_the_core_packages_import_without_qt(self, module: str) -> None:
        assert generator["_probe_qt_import"](f"qicom.{module}").status == "qt-free"

    def test_the_map_reports_the_result_it_measured(self) -> None:
        reports = generator["_qt_free_report"]()
        assert [report.package for report in reports] == [
            f"qicom.{package}"
            for package in (*generator["QT_FREE_PACKAGES"], *generator["QT_FREE_MODULES"])
        ]
        for report in reports:
            assert report == generator["_probe_qt_import"](report.package)


@pytest.fixture
def probe_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Real fresh-process imports, independent of the host's optional extras."""
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    (tmp_path / "PySide6.py").write_text("", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("source", "status", "detail", "missing_module"),
    [
        ("print('an import can write to stdout')", "qt-free", "", None),
        ("import PySide6", "imports-qt", "PySide6", None),
        (
            "raise ModuleNotFoundError('missing extra', name='absent_extra')",
            "unimportable",
            "ModuleNotFoundError: missing extra",
            "absent_extra",
        ),
        ("raise RuntimeError('broken import')", "unimportable", "RuntimeError", None),
        ("raise SystemExit(0)", "unimportable", "SystemExit: 0", None),
        (
            "import PySide6\nraise ModuleNotFoundError('missing extra', name='absent_extra')",
            "imports-qt",
            "PySide6; ModuleNotFoundError",
            "absent_extra",
        ),
        ("import sys\nsys.modules['PySide6_lookalike'] = object()", "qt-free", "", None),
        ("import os\nos._exit(1)", "unimportable", "Probe exit 1", None),
        ("import os\nos._exit(0)", "unimportable", "no valid import report", None),
    ],
)
def test_probe_distinguishes_qt_from_failed_imports(
    probe_modules: Path, source: str, status: str, detail: str, missing_module: str | None
) -> None:
    (probe_modules / "layer_probe.py").write_text(source, encoding="utf-8")
    report = generator["_probe_qt_import"]("layer_probe")

    assert report.package == "layer_probe"
    assert report.status == status
    assert detail in report.detail
    assert report.missing_module == missing_module


@pytest.mark.parametrize(
    ("blocked", "status"), [("SimpleITK", "unimportable"), ("radiomics", "qt-free")]
)
def test_radiomics_reports_a_missing_optional_dependency(
    probe_modules: Path, blocked: str, status: str
) -> None:
    # Simulate an absent extra without uninstalling anything or mocking the probe's result.
    (probe_modules / "sitecustomize.py").write_text(
        "import importlib.abc, sys\n"
        "class MissingExtra(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        f"        if fullname == {blocked!r} or fullname.startswith({blocked!r} + '.'):\n"
        "            raise ModuleNotFoundError('missing extra: ' + fullname, name=fullname)\n"
        "sys.meta_path.insert(0, MissingExtra())\n",
        encoding="utf-8",
    )
    if blocked == "radiomics":
        pytest.importorskip("SimpleITK")
    report = generator["_probe_qt_import"]("qicom.radiomics")

    assert report.status == status, report
    assert report.missing_module == ("SimpleITK" if blocked == "SimpleITK" else None)


@pytest.mark.parametrize(
    "error", [OSError("cannot launch"), subprocess.TimeoutExpired("probe", 30)]
)
def test_an_unavailable_probe_is_not_qt_free(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    report = generator["_probe_qt_import"]("qicom.metrics")

    assert report.status == "unimportable"
    assert "Probe failed" in report.detail


def test_render_keeps_all_three_import_outcomes_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    report_type = generator["QtImportReport"]
    reports = [
        report_type("qicom.metrics", "qt-free"),
        report_type("qicom.io", "imports-qt", "PySide6.QtCore"),
        report_type("qicom.radiomics", "unimportable", "ModuleNotFoundError: x|y\nextra"),
    ]
    monkeypatch.setitem(generator["render"].__globals__, "_qt_free_report", lambda: reports)
    rendered = generator["render"]()

    assert "| `qicom.metrics` | Qt-free |" in rendered
    assert "| `qicom.io` | **importa Qt** | PySide6.QtCore |" in rendered
    assert (
        "| `qicom.radiomics` | **non importabile in questo ambiente** | "
        "ModuleNotFoundError: x\\|y extra |" in rendered
    )
