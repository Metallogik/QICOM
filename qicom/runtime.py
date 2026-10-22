"""Adapt resource retention and concurrency without changing scientific inputs or results.

"Results" means the values to their stated precision, not the last bits: when
two pairs run at once each is limited to one native BLAS/OpenMP thread, which
changes the summation order inside BRISQUE, NIQE, canonical NIQE and RIQE, so
their trailing bits - and therefore a receipt's `result_hash` - can differ
between a run with one pair worker and one with two. Compare the values, or
receipts made with the same `pair_workers`.

The standard profile preserves the previous 512 MiB cache and two-pair ceiling.
Below 4 GiB of available RAM or with at most two logical CPUs, a 128 MiB cache,
released learned models and one pair leave more room for foreground interaction.
A workstation needs at least 32 GiB available and eight CPUs before retaining
up to 1 GiB; concurrency still never exceeds the existing two-pair ceiling.
These are conservative resource budgets, not estimates of a metric's peak RAM.
Unknown RAM or CPU detection selects standard. The existing one-eighth free-RAM
cache ceiling still applies independently, including to explicit user caps.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MEBIBYTE = 1024 * 1024
DEFAULT_MAX_CACHED_IMAGE_BYTES = 512 * MEBIBYTE
_AVAILABLE_MEMORY_CACHE_FRACTION = 8


def available_memory_bytes() -> int | None:
    """Best-effort currently available physical RAM without a new dependency."""
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        # ctypes.WinDLL only exists on Windows builds of CPython, so it must be
        # reached dynamically: a direct attribute access fails type checking on
        # every other platform even though this branch never runs there.
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return None
        try:
            kernel32 = win_dll("kernel32", use_last_error=True)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available_physical)
        except (AttributeError, OSError):
            return None
        return None
    if sys.platform == "darwin":
        return _macos_available_memory_bytes()
    linux = _linux_available_memory_bytes()
    if linux is not None:
        return linux
    sysconf = getattr(os, "sysconf", None)
    if not callable(sysconf):
        return None
    try:
        page_size = int(sysconf("SC_PAGE_SIZE"))
        available_pages = int(sysconf("SC_AVPHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return max(0, page_size * available_pages)


def _linux_available_memory_bytes(
    meminfo: Path = Path("/proc/meminfo"), cgroup_root: Path = Path("/sys/fs/cgroup")
) -> int | None:
    """MemAvailable - what the kernel estimates can be allocated without
    swapping - rather than MemFree, which leaves out the reclaimable page
    cache and made a busy Linux host look far fuller than it was. Bounded by
    the cgroup v2 limit when QICOM runs in a container."""
    try:
        text = meminfo.read_text(encoding="ascii")
    except OSError:
        return None
    available: int | None = None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                available = int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                return None
            break
    if available is None:
        return None
    try:
        limit_text = (cgroup_root / "memory.max").read_text(encoding="ascii").strip()
        used = int((cgroup_root / "memory.current").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return available
    if limit_text == "max":
        return available
    try:
        return max(0, min(available, int(limit_text) - used))
    except ValueError:
        return available


def _macos_available_memory_bytes(vm_stat_output: str | None = None) -> int | None:
    """Free, inactive and speculative pages from `vm_stat`: macOS has no
    SC_AVPHYS_PAGES, and returning None there meant the memory preflight
    never asked before loading a volume larger than the machine's RAM."""
    if vm_stat_output is None:
        try:
            completed = subprocess.run(
                ["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=2, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        vm_stat_output = completed.stdout
    page_match = re.search(r"page size of (\d+) bytes", vm_stat_output)
    if page_match is None:
        return None
    pages = 0
    found = False
    for label in ("Pages free", "Pages inactive", "Pages speculative"):
        match = re.search(rf"^{label}:\s+(\d+)\.", vm_stat_output, re.MULTILINE)
        if match is not None:
            pages += int(match.group(1))
            found = True
    return pages * int(page_match.group(1)) if found else None


def resolve_cache_budget(
    maximum_bytes: int | None = None,
    *,
    available_bytes: int | None = None,
) -> int:
    """Apply the configured cap and reserve at least seven eighths of free RAM."""
    maximum = DEFAULT_MAX_CACHED_IMAGE_BYTES if maximum_bytes is None else maximum_bytes
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise ValueError("max_cached_image_bytes must be an integer >= 0 or None.")
    available = available_memory_bytes() if available_bytes is None else available_bytes
    if available is None:
        return maximum
    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise ValueError("available_bytes must be an integer >= 0 or None.")
    return min(maximum, available // _AVAILABLE_MEMORY_CACHE_FRACTION)


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    radiomics_cache_mib: int
    keep_learned_models: bool
    pair_workers: int


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    name: Literal["constrained", "standard", "workstation"]
    resources: RuntimeResources


CONSTRAINED_PROFILE = RuntimeProfile("constrained", RuntimeResources(128, False, 1))
STANDARD_PROFILE = RuntimeProfile("standard", RuntimeResources(512, True, 2))
WORKSTATION_PROFILE = RuntimeProfile("workstation", RuntimeResources(1024, True, 2))


def available_cpu_count() -> int | None:
    """Best-effort count of the logical CPUs this process may run on; an
    unknown count is never guessed. The CPUs the process is allowed to use
    (affinity, a container's cpuset), not every CPU in the machine."""
    try:
        process_count = getattr(os, "process_cpu_count", None)
        if callable(process_count):
            count = process_count()
        elif hasattr(os, "sched_getaffinity"):
            count = len(os.sched_getaffinity(0))
        else:
            count = os.cpu_count()
    except (OSError, NotImplementedError):
        return None
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else None


def profile_for_hardware(*, available_bytes: int | None, cpu_count: int | None) -> RuntimeProfile:
    """Select only resource defaults; the profile has no scientific settings."""
    if available_bytes is None or cpu_count is None or available_bytes < 0 or cpu_count < 1:
        return STANDARD_PROFILE
    if available_bytes < 4 * 1024**3 or cpu_count <= 2:
        return CONSTRAINED_PROFILE
    if available_bytes >= 32 * 1024**3 and cpu_count >= 8:
        return WORKSTATION_PROFILE
    return STANDARD_PROFILE


def detect_runtime_profile() -> RuntimeProfile:
    return profile_for_hardware(
        available_bytes=available_memory_bytes(), cpu_count=available_cpu_count()
    )
