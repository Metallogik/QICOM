"""Non-visual state owned by one quantitative image panel."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from qicom.gui.revision_cache import RevisionClock


@dataclass(slots=True)
class ImagePanelState:
    """Keep source/orientation lifecycle separate from Qt presentation."""

    role: str
    path: Path | None = None
    source_files: list[Path] | None = None
    orientation_history: list[str] = field(default_factory=list)
    image_revision: RevisionClock = field(default_factory=RevisionClock)

    def record_image_change(self) -> int:
        return self.image_revision.bump()

    def clear_source(self) -> None:
        self.path = None
        self.source_files = None
        self.orientation_history.clear()

    def loaded(self, path: Path | None, source_files: list[Path] | None = None) -> None:
        self.path = path
        self.source_files = source_files
        self.orientation_history.clear()
