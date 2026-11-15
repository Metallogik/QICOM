"""Write ARCHITECTURE.md: where things are, derived from where they actually are.

A map of a 54,000-line codebase is the document most likely to start lying.
Every module added, moved or renamed invalidates it, and "update it with every
change" is precisely the promise that does not get kept - this repository has
the evidence, in seven documents that described a version, a schema and a
dependency set the code had already moved past.

So nothing here is written by hand. Every line comes from the code:

* the module list, from the filesystem;
* what each module is for, from the first sentence of its own docstring - all
  167 of them have one, and they are good;
* the layering rule, from importing each core package and checking whether Qt
  came with it;
* the trust boundaries, from coverage-floor.json, which is already curated and
  already tested;
* the gates, from the docstrings of packaging/check_*.py and generate_*.py;
* the entry points, from pyproject.toml.

`tests/test_architecture_map.py` regenerates it and compares, so a stale map
fails the suite instead of quietly misleading whoever reads it next.

    python packaging/generate_architecture_map.py

Line counts are rounded to the nearest hundred on purpose: a map that churns
every time a function grows by three lines is a map people stop regenerating.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Literal, NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "ARCHITECTURE.md"

#: Packages that must stay importable without PySide6. The rule is QICOM's
#: own (a receipt is built in worker threads and in the CLI, with no GUI in
#: the process); it is verified below rather than asserted.
QT_FREE_PACKAGES = (
    "metrics",
    "io",
    "session",
    "radiomics",
    "reporting",
    "segmentation",
    "processing",
)
#: Everything else outside qicom.gui that the CLI, the batch runner and the
#: receipt depend on, held to the same rule.
QT_FREE_MODULES = (
    "about",
    "batch",
    "build_identity",
    "cancellation",
    "cli",
    "dataframe_snapshots",
    "demo",
    "events",
    "i18n",
    "memory",
    "provenance",
    "runtime",
    "trending",
    "updates",
    "verified_download",
)

_SENTENCE_END = re.compile(r"(?<=[.])\s")
_VERSION_CONSTANT = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]*(?:SCHEMA|FORMAT)_VERSION)\s*=\s*(?P<value>\d+)", re.MULTILINE
)


def _summary(source: str) -> str:
    """A one-line summary from a module docstring.

    The first sentence when there is one, otherwise the first paragraph: a
    docstring that opens with "`qicom-cli` entry point:" and then lists what
    the entry point does has no first sentence, and truncating at the colon
    would produce a row that says nothing.
    """
    docstring = ast.get_docstring(ast.parse(source))
    if not docstring:
        return ""
    paragraph = " ".join(docstring.split("\n\n")[0].split())
    sentence = _SENTENCE_END.split(paragraph, maxsplit=1)[0]
    # A "sentence" that is only a lead-in ("Coverage gates:") is not a summary.
    summary = paragraph if len(sentence) < 40 or sentence.endswith(":") else sentence
    if len(summary) > 150:
        summary = summary[:147].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."
    return summary.replace("|", "\\|")


def _module_size(source: str) -> str:
    """A size that only changes on a structural change.

    Rounded to the nearest hundred so a function growing by three lines does
    not churn the map, and reported as "<100" below that rather than as a
    misleading "~100" for a five-line package marker.
    """
    count = len(source.splitlines())
    return "<100" if count < 100 else f"~{round(count / 100) * 100}"


def _modules(package: Path) -> list[tuple[str, str, str]]:
    """(relative path, size, summary) for one package, sorted."""
    rows = []
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "_build_identity.py":
            continue
        source = path.read_text(encoding="utf-8")
        rows.append(
            (path.relative_to(REPO_ROOT).as_posix(), _module_size(source), _summary(source))
        )
    return rows


class QtImportReport(NamedTuple):
    package: str
    status: Literal["qt-free", "imports-qt", "unimportable"]
    detail: str = ""
    missing_module: str | None = None


_QT_IMPORT_PROBE = """
import importlib
import json
import pkgutil
import sys

error = ""
missing_module = None
imported = 0
try:
    module = importlib.import_module(sys.argv[1])
    imported = 1
    # Every submodule, not only the package: an __init__ that imports
    # nothing would otherwise vouch for modules it never loads.
    for info in pkgutil.walk_packages(getattr(module, "__path__", []), module.__name__ + "."):
        importlib.import_module(info.name)
        imported += 1
except BaseException as exc:
    # Even SystemExit from an import is a failed probe, never evidence of Qt freedom.
    error = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, ModuleNotFoundError):
        missing_module = exc.name
qt_modules = sorted(m for m in sys.modules if m == "PySide6" or m.startswith("PySide6."))
print("\\n" + json.dumps({
    "qt_modules": qt_modules, "error": error, "missing_module": missing_module,
    "imported": imported,
}))
"""


def _probe_qt_import(package: str, extra_path: Path | None = None) -> QtImportReport:
    """Keep failed imports distinct from measured Qt dependencies.

    A fresh interpreter prevents the caller's Qt imports from contaminating
    the result. Qt loaded before a subsequent import failure still counts as
    a layering violation; an unavailable package never counts as Qt-free.
    """
    env = None
    if extra_path is not None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(extra_path), env.get("PYTHONPATH", "")) if item
        )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _QT_IMPORT_PROBE, package],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return QtImportReport(package, "unimportable", f"Probe failed: {exc}")
    if completed.returncode:
        return QtImportReport(
            package,
            "unimportable",
            f"Probe exit {completed.returncode}: {completed.stderr.strip()}",
        )
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return QtImportReport(package, "unimportable", "Probe returned no valid import report")
    if payload["qt_modules"]:
        detail = ", ".join(payload["qt_modules"])
        if payload["error"]:
            detail += f"; {payload['error']}"
        return QtImportReport(package, "imports-qt", detail, payload["missing_module"])
    if payload["error"]:
        return QtImportReport(package, "unimportable", payload["error"], payload["missing_module"])
    imported = int(payload.get("imported", 1))
    return QtImportReport(package, "qt-free", f"moduli importati: {imported}")


def _qt_free_report() -> list[QtImportReport]:
    return [_probe_qt_import(f"qicom.{name}") for name in (*QT_FREE_PACKAGES, *QT_FREE_MODULES)]


def _schema_constants() -> list[tuple[str, str, str]]:
    """(constant, value, file) for every persisted-format version in the code."""
    found = []
    for path in sorted(Path(REPO_ROOT / "qicom").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for match in _VERSION_CONSTANT.finditer(path.read_text(encoding="utf-8")):
            found.append((match["name"], match["value"], path.relative_to(REPO_ROOT).as_posix()))
    return sorted(found)


def _gates() -> list[tuple[str, str]]:
    scripts = sorted(Path(REPO_ROOT / "packaging").glob("check_*.py")) + sorted(
        Path(REPO_ROOT / "packaging").glob("generate_*.py")
    )
    return [
        (path.relative_to(REPO_ROOT).as_posix(), _summary(path.read_text(encoding="utf-8")))
        for path in scripts
    ]


def _entry_points() -> list[tuple[str, str]]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return sorted(project.get("scripts", {}).items())


def _trust_boundaries() -> list[tuple[str, str]]:
    floor = json.loads((REPO_ROOT / "coverage-floor.json").read_text(encoding="utf-8"))
    return sorted((path, entry["why"]) for path, entry in floor["trust_boundaries"].items())


def render() -> str:
    lines: list[str] = [
        "# QICOM — architecture map",
        "",
        "> **Generated file. Do not edit it by hand.**",
        "> Regenerate with `python packaging/generate_architecture_map.py`.",
        "> Local document excluded from Git. The tests check the generator and the boundaries",
        "> between packages; regenerate this copy after changing the code.",
        "",
        "Every line comes from the code: the module list from the filesystem, the description",
        "from the first sentence of each module's docstring, the layering rule from a real",
        "import, the trust boundaries from `coverage-floor.json`. Line counts are rounded",
        "to the nearest hundred: a map that changes every time a function grows by three",
        "lines is a map nobody regenerates any more.",
        "",
        "## Entry points",
        "",
        "| command | function |",
        "| --- | --- |",
    ]
    for name, target in _entry_points():
        lines.append(f"| `{name}` | `{target}` |")

    lines += [
        "",
        "## Layering rule",
        "",
        "The core does not depend on PySide6: a receipt is built in worker threads and",
        "by the CLI, with no GUI in the process. Verified by importing each package with",
        "all its submodules, and every other module outside `qicom.gui`, in a clean",
        "interpreter. An unimportable package is not a passed check: an extra may be",
        "missing, or there may be an error to diagnose. If Qt was loaded before an",
        "import error, the violation is still reported.",
        "",
        "| package | import outcome | detail |",
        "| --- | --- | --- |",
    ]
    labels = {
        "qt-free": "Qt-free",
        "imports-qt": "**imports Qt**",
        "unimportable": "**not importable in this environment**",
    }
    for report in _qt_free_report():
        detail = " ".join(report.detail.split()).replace("|", "\\|")
        lines.append(f"| `{report.package}` | {labels[report.status]} | {detail} |")

    lines += [
        "",
        "`qicom.gui` is the only package that imports Qt, and it already imports it in its",
        "`__init__`: every module under `qicom.gui`, even if its own code does not use Qt",
        "(for example `panel_roles` or the calculation and export services), brings Qt into",
        "the process when it is imported.",
        "",
        "## Trust boundaries",
        "",
        "Where untrusted input enters QICOM. The list and the reasons come from",
        "`coverage-floor.json`, which is already curated and already tested.",
        "",
        "| module | why |",
        "| --- | --- |",
    ]
    for path, why in _trust_boundaries():
        lines.append(f"| `{path}` | {why} |")

    lines += [
        "",
        "## Persisted formats",
        "",
        "The schema version constants. Changing one requires an explicit migration",
        "and round-trip tests (see `OPERATIONS_SCHEMAS.md`).",
        "",
        "| constant | value | file |",
        "| --- | ---: | --- |",
    ]
    for name, value, path in _schema_constants():
        lines.append(f"| `{name}` | {value} | `{path}` |")

    lines += [
        "",
        "## Build and release gates",
        "",
        "| script | what it prevents |",
        "| --- | --- |",
    ]
    for path, summary in _gates():
        lines.append(f"| `{path}` | {summary} |")

    root_modules = [row for row in _modules(REPO_ROOT / "qicom") if row[0].count("/") == 1]
    lines += [
        "",
        "## `qicom/` — top-level modules",
        "",
        "| module | lines | what it does |",
        "| --- | ---: | --- |",
    ]
    for path, size, summary in root_modules:
        lines.append(f"| `{path}` | {size} | {summary} |")

    packages = (
        p
        for p in (REPO_ROOT / "qicom").iterdir()
        if p.is_dir() and not p.name.startswith(("_", "."))
    )
    for package in sorted(packages):
        rows = _modules(package)
        if not rows:
            continue
        lines += [
            "",
            f"## `qicom/{package.name}/`",
            "",
            "| module | lines | what it does |",
            "| --- | ---: | --- |",
        ]
        for path, size, summary in rows:
            lines.append(f"| `{path}` | {size} | {summary} |")

    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the file on disk is not what would be generated now.",
    )
    arguments = parser.parse_args(argv)

    rendered = render()
    if arguments.check:
        current = arguments.output.read_text(encoding="utf-8") if arguments.output.exists() else ""
        if current != rendered:
            print(
                f"{arguments.output} is stale. Regenerate it with "
                "`python packaging/generate_architecture_map.py`.",
                file=sys.stderr,
            )
            return 1
        print(f"{arguments.output} is current.")
        return 0

    arguments.output.write_text(rendered, encoding="utf-8")
    print(f"Wrote {arguments.output} ({len(rendered.splitlines())} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
