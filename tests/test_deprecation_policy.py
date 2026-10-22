from __future__ import annotations

import ast
from pathlib import Path

_DEPRECATED_FUNCTION_OWNERS = {
    "try_load_dicom_slice": Path("qicom/io/dicom_io.py"),
    "compute_per_slice_reference_metrics": Path("qicom/metrics/runner.py"),
    "resolve_radiomics_cache_budget": Path("qicom/radiomics/extraction.py"),
}


def test_deprecated_functions_have_no_qicom_runtime_callers() -> None:
    root = Path(__file__).resolve().parents[1]
    sources = list((root / "qicom").rglob("*.py"))

    for function_name, owner in _DEPRECATED_FUNCTION_OWNERS.items():
        callers = [
            path.relative_to(root)
            for path in sources
            if path.relative_to(root) != owner and function_name in path.read_text(encoding="utf-8")
        ]
        assert callers == [], f"{function_name} is still referenced by {callers}"


def test_radiomics_window_does_not_use_the_synchronous_legacy_loader() -> None:
    root = Path(__file__).resolve().parents[1]
    window_source = (root / "qicom/gui/radiomics_main_window.py").read_text(encoding="utf-8")

    assert ".load_project(" not in window_source
    assert "load_radiomics_project" in window_source
    assert "apply_project_data" in window_source


def test_memory_detection_has_only_shared_runtime_callers() -> None:
    root = Path(__file__).resolve().parents[1]
    for path in (root / "qicom").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "qicom.radiomics.extraction":
                    assert "available_memory_bytes" not in {
                        alias.name for alias in node.names
                    }, path
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "available_memory_bytes":
                    assert ast.unparse(node.func.value) == "runtime", path
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "available_memory_bytes":
                    assert path == root / "qicom/runtime.py", path
