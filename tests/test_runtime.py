"""Hardware detection changes resource defaults without importing analysis backends."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qicom import runtime

GIB = 1024**3


@pytest.mark.parametrize(
    ("memory", "cpus", "expected"),
    [
        (0, 8, runtime.CONSTRAINED_PROFILE),
        (4 * GIB - 1, 8, runtime.CONSTRAINED_PROFILE),
        (64 * GIB, 2, runtime.CONSTRAINED_PROFILE),
        (4 * GIB, 3, runtime.STANDARD_PROFILE),
        (32 * GIB - 1, 8, runtime.STANDARD_PROFILE),
        (32 * GIB, 7, runtime.STANDARD_PROFILE),
        (32 * GIB, 8, runtime.WORKSTATION_PROFILE),
        (None, 2, runtime.STANDARD_PROFILE),
        (GIB, None, runtime.STANDARD_PROFILE),
        (None, None, runtime.STANDARD_PROFILE),
        (-1, 8, runtime.STANDARD_PROFILE),
        (GIB, 0, runtime.STANDARD_PROFILE),
    ],
)
def test_detection_selects_conservative_defaults(
    monkeypatch: pytest.MonkeyPatch,
    memory: int | None,
    cpus: int | None,
    expected: runtime.RuntimeProfile,
) -> None:
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: memory)
    monkeypatch.setattr(runtime, "available_cpu_count", lambda: cpus)
    assert runtime.detect_runtime_profile() == expected


@pytest.mark.parametrize("count", [None, 0, -1, True, "8", 1, 8])
def test_cpu_count_rejects_unknown_or_invalid_results(monkeypatch, count) -> None:
    monkeypatch.setattr(runtime, "os", SimpleNamespace(cpu_count=lambda: count))
    expected = count if type(count) is int and count > 0 else None
    assert runtime.available_cpu_count() == expected


@pytest.mark.parametrize("error", [OSError, NotImplementedError])
def test_cpu_detection_failure_is_unknown(monkeypatch, error) -> None:
    monkeypatch.setattr(runtime, "os", SimpleNamespace(cpu_count=Mock(side_effect=error)))
    assert runtime.available_cpu_count() is None


@pytest.mark.parametrize("pages", [0, -1, 1024])
def test_posix_memory_detection_uses_available_pages(monkeypatch, pages) -> None:
    # The sysconf fallback, for a POSIX system without /proc/meminfo.
    monkeypatch.setattr(runtime, "_linux_available_memory_bytes", lambda: None)
    values = {"SC_PAGE_SIZE": 4096, "SC_AVPHYS_PAGES": pages}
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="posix", sysconf=values.__getitem__))
    assert runtime.available_memory_bytes() == max(0, 4096 * pages)


@pytest.mark.parametrize("error", [AttributeError, OSError, TypeError, ValueError])
def test_posix_memory_detection_failure_is_unknown(monkeypatch, error) -> None:
    # The sysconf fallback, for a POSIX system without /proc/meminfo.
    monkeypatch.setattr(runtime, "_linux_available_memory_bytes", lambda: None)
    monkeypatch.setattr(
        runtime, "os", SimpleNamespace(name="posix", sysconf=Mock(side_effect=error))
    )
    assert runtime.available_memory_bytes() is None


def test_memory_detection_without_sysconf_is_unknown(monkeypatch) -> None:
    # The sysconf fallback, for a POSIX system without /proc/meminfo.
    monkeypatch.setattr(runtime, "_linux_available_memory_bytes", lambda: None)
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="posix"))
    assert runtime.available_memory_bytes() is None


@pytest.mark.parametrize("succeeds", [False, True])
def test_windows_memory_detection_uses_global_memory_status(monkeypatch, succeeds) -> None:
    def get_status(pointer):
        pointer._obj.available_physical = 3 * GIB
        assert pointer._obj.length == ctypes.sizeof(pointer._obj)
        return succeeds

    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt"))
    loader = Mock(return_value=SimpleNamespace(GlobalMemoryStatusEx=get_status))
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    assert runtime.available_memory_bytes() == (3 * GIB if succeeds else None)
    loader.assert_called_once_with("kernel32", use_last_error=True)


@pytest.mark.parametrize("error", [None, AttributeError, OSError])
def test_windows_detection_failure_is_unknown(monkeypatch, error) -> None:
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        ctypes, "WinDLL", None if error is None else Mock(side_effect=error), raising=False
    )
    assert runtime.available_memory_bytes() is None


def test_runtime_import_and_detection_need_no_gui_or_scientific_backend() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from qicom.runtime import detect_runtime_profile
assert detect_runtime_profile().name in {'constrained', 'standard', 'workstation'}
for name in ('PySide6', 'numpy', 'torch', 'SimpleITK', 'radiomics', 'psutil'):
    assert name not in sys.modules, name
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_releasing_unloaded_models_does_not_import_a_backend() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from qicom.metrics.reference_structural import set_keep_learned_models
set_keep_learned_models(False)
assert 'torch' not in sys.modules
assert 'piq' not in sys.modules
assert 'lpips' not in sys.modules
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_linux_memory_is_memavailable_not_memfree(tmp_path) -> None:
    """MemFree leaves out the reclaimable page cache: a busy host looked far
    fuller than it was and prompted for memory it had."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 64000000 kB\nMemFree: 1000 kB\nMemAvailable: 2048 kB\n")

    assert runtime._linux_available_memory_bytes(meminfo, tmp_path / "no-cgroup") == 2048 * 1024


def test_a_container_limit_bounds_the_available_memory(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 8388608 kB\n")  # 8 GiB on the host
    (tmp_path / "memory.max").write_text(str(3 * GIB))
    (tmp_path / "memory.current").write_text(str(1 * GIB))

    assert runtime._linux_available_memory_bytes(meminfo, tmp_path) == 2 * GIB


def test_macos_memory_comes_from_vm_stat() -> None:
    """macOS has no SC_AVPHYS_PAGES: the preflight never asked there."""
    output = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               10000.\n"
        "Pages active:                             99999.\n"
        "Pages inactive:                           20000.\n"
        "Pages speculative:                         3000.\n"
    )

    assert runtime._macos_available_memory_bytes(output) == (10000 + 20000 + 3000) * 16384
    assert runtime._macos_available_memory_bytes("unexpected") is None


def test_cpu_count_follows_the_processes_affinity(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime,
        "os",
        SimpleNamespace(cpu_count=lambda: 64, sched_getaffinity=lambda _pid: {0, 1, 2}),
    )

    assert runtime.available_cpu_count() == 3
