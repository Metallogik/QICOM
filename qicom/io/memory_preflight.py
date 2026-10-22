"""Let callers review large image allocations before a decoder materializes pixels.

The estimate is a lower bound, not a peak: decoder, framework and analysis
temporaries are excluded. At half of currently available RAM, ask the caller
to decide while there is still headroom. Unknown RAM never implies safety.
Library callers receive a warning by default; GUI/CLI install their own
confirmation handler. Policies and nested stack approvals are context-local.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import numpy as np
from numpy.typing import DTypeLike

from qicom import runtime
from qicom.memory import estimate_image_memory


class ImageMemoryWarning(UserWarning):
    """A metadata-derived allocation would consume much of the available RAM."""


class ImageLoadCancelled(ValueError):
    """The caller declined an image allocation before pixel decoding."""


@dataclass(frozen=True, slots=True)
class ImageAllocationWarning:
    source_format: str
    shape: tuple[int, ...]
    minimum_bytes: int
    available_bytes: int

    def message(self) -> str:
        return (
            f"Loading {self.source_format} ({' × '.join(map(str, self.shape))}) needs at least "
            f"{format_memory_bytes(self.minimum_bytes)} of additional array memory; "
            f"{format_memory_bytes(self.available_bytes)} of RAM is currently available. "
            "This is a lower bound, not a peak: decoder buffers, the interface and "
            "metric-specific temporary arrays are excluded. Continuing may still run out of memory."
        )


MemoryWarningHandler = Callable[[ImageAllocationWarning], bool]
_handler: ContextVar[MemoryWarningHandler | None] = ContextVar("image_memory_handler", default=None)
_enclosing_bytes: ContextVar[int] = ContextVar("image_memory_enclosing_bytes", default=0)


def format_memory_bytes(value: int) -> str:
    for unit, divisor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value >= divisor:
            return f"{value / divisor:.1f} {unit}"
    return f"{value} B"


@contextmanager
def image_memory_policy(handler: MemoryWarningHandler) -> Iterator[None]:
    """Choose how to confirm a warning without changing pixels or persistent settings."""
    token = _handler.set(handler)
    try:
        yield
    finally:
        _handler.reset(token)


@contextmanager
def image_allocation_preflight(
    shape: Sequence[int],
    *,
    source_format: str,
    dtype: DTypeLike = np.float32,
    volume_count: int = 1,
    slice_buffer: int = 0,
) -> Iterator[None]:
    """Check metadata before entering the block that allocates/decodes its arrays.

    A stack checks its whole volume plus slice workspace first. Nested slice
    readers then need no duplicate prompt if covered by that same bound.
    Independent loads always recheck RAM; approvals are never global or saved.
    """
    estimate = estimate_image_memory(
        shape, dtype=dtype, volume_count=volume_count, slice_buffer=slice_buffer
    )
    minimum = estimate.estimated_peak_bytes
    enclosing = _enclosing_bytes.get()
    if minimum > enclosing:
        available = runtime.available_memory_bytes()
        if available is not None and minimum * 2 >= available:
            warning = ImageAllocationWarning(source_format, estimate.shape, minimum, available)
            handler = _handler.get()
            if handler is None:
                warnings.warn(warning.message(), ImageMemoryWarning, stacklevel=2)
            elif not handler(warning):
                raise ImageLoadCancelled("Image loading cancelled before pixel allocation.")
    token = _enclosing_bytes.set(max(minimum, enclosing))
    try:
        yield
    finally:
        _enclosing_bytes.reset(token)
