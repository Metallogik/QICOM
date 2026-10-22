"""Fresh-process startup and idle-window benchmarks for QICOM.

Each observation runs in its own Python process.  Import caches and Qt
singletons would otherwise make launcher/window measurements depend on case
ordering instead of the code being measured.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import importlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_WORKER_CASES = (
    "launcher_import",
    "launcher_construct",
    "quantitative_idle",
    "qualitative_idle",
    "radiomics_idle",
)


def _current_rss_bytes() -> int | None:
    if os.name == "nt":

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        )
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        process = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb)
        return int(counters.WorkingSetSize) if ok else None
    try:
        statm = Path("/proc/self/statm").read_text(encoding="ascii").split()
        return int(statm[1]) * int(os.sysconf("SC_PAGE_SIZE"))  # type: ignore[attr-defined]
    except (OSError, ValueError):
        return None


def _total_memory_bytes() -> int | None:
    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return int(status.ullTotalPhys) if ok else None
    try:
        sysconf = os.sysconf  # type: ignore[attr-defined]
        return int(sysconf("SC_PHYS_PAGES")) * int(sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError):
        return None


def _timed(operation: Callable[[], Any]) -> tuple[Any, float, float]:
    cpu_started = time.process_time_ns()
    wall_started = time.perf_counter_ns()
    result = operation()
    wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000.0
    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000.0
    return result, wall_ms, cpu_ms


def _qt_application() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _launcher_import_worker() -> dict[str, Any]:
    gc.collect()
    rss_before = _current_rss_bytes()
    _module, wall_ms, cpu_ms = _timed(
        lambda: importlib.import_module("qicom.gui.widgets.launcher_dialog")
    )
    gc.collect()
    return {
        "wall_ms": wall_ms,
        "cpu_ms": cpu_ms,
        "rss_before_bytes": rss_before,
        "rss_idle_bytes": _current_rss_bytes(),
        "loaded_module_count": len(sys.modules),
    }


def _launcher_construct_worker() -> dict[str, Any]:
    app = _qt_application()
    launcher_module: Any = importlib.import_module("qicom.gui.widgets.launcher_dialog")
    launcher_module.load_recent_projects = lambda: []
    gc.collect()
    rss_before = _current_rss_bytes()
    dialog, wall_ms, cpu_ms = _timed(launcher_module.LauncherDialog)
    app.processEvents()
    gc.collect()
    result = {
        "wall_ms": wall_ms,
        "cpu_ms": cpu_ms,
        "rss_before_bytes": rss_before,
        "rss_idle_bytes": _current_rss_bytes(),
        "loaded_module_count": len(sys.modules),
    }
    dialog.close()
    dialog.deleteLater()
    app.processEvents()
    return result


def _window_idle_worker(case: str) -> dict[str, Any]:
    app = _qt_application()
    gc.collect()
    rss_before_import = _current_rss_bytes()

    if case == "quantitative_idle":

        def import_window() -> tuple[type[Any], dict[str, Any]]:
            from qicom.gui.main_window import MainWindow
            from qicom.gui.widgets import log_panel
            from qicom.session.storage import SessionStorage

            log_panel.default_log_path = lambda: Path("qicom-benchmark-missing.log")
            return MainWindow, {"storage": SessionStorage(":memory:")}

    elif case == "qualitative_idle":

        def import_window() -> tuple[type[Any], dict[str, Any]]:
            from qicom.gui.widgets.qualitative_main_window import QualitativeMainWindow

            return QualitativeMainWindow, {}

    else:

        def import_window() -> tuple[type[Any], dict[str, Any]]:
            from qicom.gui.radiomics_main_window import RadiomicsMainWindow

            return RadiomicsMainWindow, {}

    (window_type, kwargs), import_wall_ms, import_cpu_ms = _timed(import_window)
    gc.collect()
    rss_before_construct = _current_rss_bytes()
    window, construct_wall_ms, construct_cpu_ms = _timed(lambda: window_type(**kwargs))
    app.processEvents()
    gc.collect()
    result = {
        "import_wall_ms": import_wall_ms,
        "import_cpu_ms": import_cpu_ms,
        "construct_wall_ms": construct_wall_ms,
        "construct_cpu_ms": construct_cpu_ms,
        "rss_before_import_bytes": rss_before_import,
        "rss_before_construct_bytes": rss_before_construct,
        "rss_idle_bytes": _current_rss_bytes(),
        "loaded_module_count": len(sys.modules),
    }
    window.close()
    window.deleteLater()
    app.processEvents()
    return result


def _run_worker(case: str) -> int:
    if case == "launcher_import":
        result = _launcher_import_worker()
    elif case == "launcher_construct":
        result = _launcher_construct_worker()
    else:
        result = _window_idle_worker(case)
    print(json.dumps(result, sort_keys=True))
    return 0


def _fresh_observation(case: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Keep benchmark construction independent from the user's recent
    # projects, preferences and log, and writable in restricted CI.  The
    # existing results directory is intentionally used instead of a Windows
    # TemporaryDirectory: hardened runners may give child processes no access
    # to dynamically-created ACL-restricted directories.
    settings_dir = str(Path.cwd() / "benchmarks" / "results")
    for name in ("APPDATA", "LOCALAPPDATA", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        environment[name] = settings_dir
    completed = subprocess.run(
        [sys.executable, "-m", "benchmarks.startup_baseline", "--worker", case],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not output_lines:
        raise RuntimeError(f"Worker {case!r} produced no JSON output.")
    return dict(json.loads(output_lines[-1]))


def _summarize_observations(observations: list[dict[str, Any]]) -> dict[str, Any]:
    keys = observations[0]
    medians: dict[str, int | float | None] = {}
    for key in keys:
        values = [item[key] for item in observations if item[key] is not None]
        medians[key] = statistics.median(values) if values else None
    return {"status": "ok", "repeats": len(observations), "median": medians}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=_WORKER_CASES)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=_WORKER_CASES, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.worker is not None:
        return _run_worker(args.worker)
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")

    selected = args.case or list(_WORKER_CASES)
    report: dict[str, Any] = {
        "schema_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "total_memory_bytes": _total_memory_bytes(),
        },
        "benchmarks": {},
    }
    for case in selected:
        try:
            observations = [_fresh_observation(case) for _ in range(args.repeats)]
            report["benchmarks"][case] = _summarize_observations(observations)
        except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            report["benchmarks"][case] = {
                "status": "error",
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }

    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if all(item["status"] == "ok" for item in report["benchmarks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
