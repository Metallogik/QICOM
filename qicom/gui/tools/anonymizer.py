"""De-identify a folder of images into its mirror, `<folder>_anonymized`, after showing what goes.

The rules are those of qicom.io.folder_deidentification. Choosing files or a
folder lists them first, as the tree they form, each with what will happen
to it - de-identified as DICOM, NIfTI or a bitmap, or left out as not an
image - and where the copies will go: beside the original, in a folder of the
same name ending in ``_anonymized``, with the same subfolders and file names,
unless the user picks another place. One option replaces every folder and
file name with a generic one (folder_001, image_0001.dcm, ...) in a folder
named ``anonymized``, for exports whose names identify the patient; the tree
then shows the name each item will have. Listing and writing run as jobs with
their own progress window.

When some file cannot be de-identified, the tool shows the same question
every QICOM export asks (qicom.gui.error_reporting.
confirm_unverified_dicom_export): nothing has been written yet, these are
the files and why, proceed without de-identification for them or cancel.
Files written under that choice are listed afterwards and reported to the
window's error log.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.gui import error_reporting
from qicom.gui.error_reporting import explained_error, unverified_dicom_messages
from qicom.gui.file_names import display_path
from qicom.gui.guidance import guide
from qicom.gui.icons import muted_color
from qicom.gui.theming import theme_manager
from qicom.gui.tools.job_dialog import run_job
from qicom.i18n import translator
from qicom.io.deidentification import ImageDeidentificationRefused, UnverifiedImageFile
from qicom.io.folder_deidentification import (
    DeidentificationReport,
    SourcePlan,
    anonymized_destination,
    deidentify_files,
    destination_name,
    plan_folder,
    plan_sources,
    target_paths,
)

_KIND_KEYS = {
    "dicom": "tools.anonymizer.kind.dicom",
    "nifti": "tools.anonymizer.kind.nifti",
    "bitmap": "tools.anonymizer.kind.bitmap",
    None: "tools.anonymizer.kind.skipped",
}


COLUMN_NAME, COLUMN_ACTION, COLUMN_COPY = range(3)


def _run(
    plan: SourcePlan,
    destination: Path,
    override: bool,
    generic_names: bool = False,
    *,
    progress_callback: object = None,
) -> DeidentificationReport | ImageDeidentificationRefused:
    """The worker: a refusal comes back as a value, so the GUI can ask about it."""
    try:
        return deidentify_files(
            [entry.path for entry in plan.entries],
            destination,
            root=plan.root,
            generic_names=generic_names,
            unverified=[] if override else None,
            progress_callback=progress_callback if callable(progress_callback) else None,
        )
    except ImageDeidentificationRefused as refusal:
        return refusal


class AnonymizerTool(QWidget):
    error_occurred = Signal(str)
    # Recorded for the bug report without a second box: the refusal question
    # and the list of unverified files already show them.
    error_logged = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plan: SourcePlan | None = None
        self._location: Path | None = None
        self._written: Path | None = None
        self._busy = False
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._limits = QLabel()
        self._limits.setObjectName("panelSectionHint")
        self._limits.setWordWrap(True)
        self._names = QLabel()
        self._names.setObjectName("wizardStepNotice")
        self._names.setWordWrap(True)
        self._generic = QCheckBox()
        self._generic.toggled.connect(self._on_generic_toggled)
        self._files_button = QPushButton()
        self._files_button.clicked.connect(self._choose_files)
        self._folder_button = QPushButton()
        self._folder_button.clicked.connect(self._choose_folder)
        self._recursive = QCheckBox()
        self._recursive.setChecked(True)
        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        header = self._tree.header()
        if header is not None:
            header.setStretchLastSection(True)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(COLUMN_NAME, 380)
            header.resizeSection(COLUMN_ACTION, 300)
        self._selection = QLabel()
        self._selection.setWordWrap(True)
        self._destination = QLabel()
        self._destination.setWordWrap(True)
        self._destination.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._location_button = QPushButton()
        self._location_button.clicked.connect(self._choose_location)
        self._run_button = QPushButton()
        self._run_button.setMinimumHeight(36)
        self._run_button.clicked.connect(self.run)
        self._result = QLabel()
        self._result.setWordWrap(True)
        self._unverified_list = QListWidget()
        self._unverified_list.setVisible(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self._files_button)
        buttons.addWidget(self._folder_button)
        buttons.addWidget(self._recursive)
        buttons.addStretch(1)
        destination = QHBoxLayout()
        destination.addWidget(self._destination, 1)
        destination.addWidget(self._location_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self._note)
        layout.addWidget(self._limits)
        layout.addWidget(self._names)
        layout.addWidget(self._generic)
        layout.addLayout(buttons)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._selection)
        layout.addLayout(destination)
        layout.addWidget(self._run_button)
        layout.addWidget(self._result)
        layout.addWidget(self._unverified_list)
        translator.on_language_changed(self.retranslate_ui)
        theme_manager.on_theme_changed(lambda _theme: self._fill_tree())
        self.retranslate_ui()

    # -- choosing ---------------------------------------------------------------------

    @property
    def plan(self) -> SourcePlan | None:
        return self._plan

    @property
    def sources(self) -> tuple[Path, ...]:
        return () if self._plan is None else tuple(entry.path for entry in self._plan.entries)

    @property
    def written_folder(self) -> Path | None:
        """The folder the last run created."""
        return self._written

    @property
    def generic_names(self) -> bool:
        return self._generic.isChecked()

    def set_generic_names(self, generic: bool) -> None:
        self._generic.setChecked(generic)

    def _on_generic_toggled(self, _checked: bool) -> None:
        self._written = None
        self._refresh_names()
        self._fill_tree()
        self._refresh()

    def _refresh_names(self) -> None:
        key = (
            "tools.anonymizer.names_generic"
            if self.generic_names
            else "tools.anonymizer.names_kept"
        )
        self._names.setText(translator.tr(key))
        self._tree.setColumnHidden(COLUMN_COPY, not self.generic_names)

    def _choose_files(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(
            self, translator.tr("tools.anonymizer.files_title"), ""
        )
        if paths:
            self.choose_files([Path(path) for path in paths])

    def choose_files(self, paths: list[Path]) -> None:
        self._start_plan(translator.tr("tools.anonymizer.listing"), plan_sources, paths)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.anonymizer.folder_title"), ""
        )
        if folder:
            self.choose_folder(Path(folder))

    def choose_folder(self, folder: Path) -> None:
        self._start_plan(
            translator.tr("tools.anonymizer.listing"),
            plan_folder,
            folder,
            recursive=self._recursive.isChecked(),
        )

    def _start_plan(self, message: str, fn: object, *args: object, **kwargs: object) -> None:
        if self._busy:
            return
        self._busy = True
        self._refresh()
        run_job(
            self,
            message,
            fn,  # type: ignore[arg-type]
            *args,
            on_success=self._on_planned,
            on_error=self._on_plan_failed,
            **kwargs,
        )

    def _on_planned(self, plan: object) -> None:
        self._busy = False
        if isinstance(plan, SourcePlan):
            self.set_plan(plan)
        self._refresh()

    def _on_plan_failed(self, message: str) -> None:
        self._busy = False
        self._refresh()
        self.error_occurred.emit(explained_error("tools.anonymizer.list_failed", message))

    def set_plan(self, plan: SourcePlan) -> None:
        self._plan = plan
        self._location = None
        self._written = None
        self._result.clear()
        self._unverified_list.clear()
        self._unverified_list.setVisible(False)
        self._fill_tree()
        self._refresh()

    def _choose_location(self) -> None:
        if self._plan is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.anonymizer.location_title"), str(self._plan.root.parent)
        )
        if folder:
            self.set_location(Path(folder))

    def set_location(self, folder: Path | None) -> None:
        """Create the mirror in `folder` instead of beside the original."""
        self._location = folder
        self._refresh()

    def destination_preview(self) -> Path | None:
        if self._plan is None:
            return None
        parent = self._location if self._location is not None else self._plan.root.parent
        return parent / destination_name(self._plan.root, generic_names=self.generic_names)

    # -- the tree ----------------------------------------------------------------------

    def _fill_tree(self) -> None:
        self._tree.clear()
        plan = self._plan
        if plan is None:
            return
        muted = QBrush(QColor(muted_color(theme_manager.theme)))
        generic = self.generic_names
        targets = dict(
            zip(
                (entry.relative for entry in plan.entries),
                target_paths(plan, generic_names=generic),
                strict=True,
            )
        )
        # The name each folder will have, read off the targets of its images.
        folder_names: dict[tuple[str, ...], str] = {}
        for relative, target in targets.items():
            if target is not None:
                for depth in range(1, len(relative.parts)):
                    folder_names[relative.parts[:depth]] = target.parts[depth - 1]
        root = QTreeWidgetItem(
            [
                plan.root.name or str(plan.root),
                "",
                destination_name(plan.root, generic_names=generic),
            ]
        )
        self._tree.addTopLevelItem(root)
        folders: dict[tuple[str, ...], QTreeWidgetItem] = {(): root}

        def folders_first(entry: object) -> list[tuple[int, str]]:
            parts = entry.relative.parts  # type: ignore[attr-defined]
            return [(0, part.lower()) for part in parts[:-1]] + [(1, parts[-1].lower())]

        for entry in sorted(plan.entries, key=folders_first):
            parts = entry.relative.parts
            parent = root
            for depth in range(1, len(parts)):
                key = parts[:depth]
                if key not in folders:
                    folder = QTreeWidgetItem([parts[depth - 1], "", folder_names.get(key, "")])
                    folders[key[:-1]].addChild(folder)
                    folders[key] = folder
                parent = folders[key]
            target = targets.get(entry.relative)
            item = QTreeWidgetItem(
                [
                    parts[-1],
                    translator.tr(_KIND_KEYS[entry.kind]),
                    target.name if target is not None else "",
                ]
            )
            item.setToolTip(COLUMN_NAME, entry.relative.as_posix())
            if entry.kind is None:
                item.setForeground(COLUMN_NAME, muted)
                item.setForeground(COLUMN_ACTION, muted)
            parent.addChild(item)
        if len(plan.entries) <= 5_000:
            self._tree.expandAll()
        else:
            root.setExpanded(True)

    # -- running ------------------------------------------------------------------------

    def run(self) -> None:
        """Anonymize into the destination shown."""
        plan = self._plan
        if plan is None or not plan.images or self._busy:
            return
        try:
            destination = anonymized_destination(
                plan.root, self._location, generic_names=self.generic_names
            )
        except ValueError as exc:
            self.error_occurred.emit(str(exc))
            return
        self.run_into(destination)

    def run_into(self, destination: Path, *, override: bool = False) -> None:
        plan = self._plan
        if plan is None or not plan.images or self._busy:
            return
        self._busy = True
        self._refresh()
        self._result.clear()
        self._unverified_list.clear()
        self._unverified_list.setVisible(False)
        run_job(
            self,
            translator.tr("tools.anonymizer.running"),
            _run,
            plan,
            destination,
            override,
            self.generic_names,
            on_success=lambda result: self._on_finished(destination, result),
            on_error=self._on_failed,
        )

    def _on_finished(self, destination: Path, result: object) -> None:
        self._busy = False
        self._refresh()
        if isinstance(result, ImageDeidentificationRefused):
            self._result.setText(translator.tr("tools.anonymizer.refused"))
            self.error_logged.emit(str(result))
            if error_reporting.confirm_unverified_dicom_export(self, str(result), result.refusals):
                self.run_into(destination, override=True)
            return
        if isinstance(result, DeidentificationReport):
            self._written = result.folder
            self._show_report(result)
            self._refresh()

    def _show_report(self, report: DeidentificationReport) -> None:
        text = translator.tr(
            "tools.anonymizer.done", count=report.written, folder=display_path(report.folder)
        )
        if report.skipped:
            text += " " + translator.tr("tools.anonymizer.skipped", count=len(report.skipped))
        self._result.setText(text)
        self.status_message.emit(text)
        self._list_unverified(report.unverified)

    def _list_unverified(self, files: tuple[UnverifiedImageFile, ...]) -> None:
        messages = unverified_dicom_messages(files)
        self._unverified_list.clear()
        self._unverified_list.addItems(messages)
        self._unverified_list.setVisible(bool(messages))
        for message in messages:
            self.error_logged.emit(message)

    def _on_failed(self, message: str) -> None:
        self._busy = False
        self._refresh()
        self._result.clear()
        self.error_occurred.emit(explained_error("tools.anonymizer.failed", message))

    # -- refreshing -----------------------------------------------------------------

    def _refresh(self) -> None:
        idle = not self._busy
        plan = self._plan
        self._files_button.setEnabled(idle)
        self._folder_button.setEnabled(idle)
        self._recursive.setEnabled(idle)
        self._generic.setEnabled(idle)
        images = len(plan.images) if plan is not None else 0
        self._run_button.setEnabled(idle and images > 0)
        self._location_button.setEnabled(idle and plan is not None)
        if plan is None:
            self._selection.setText(translator.tr("tools.anonymizer.nothing_selected"))
            self._destination.clear()
        else:
            folders = {entry.relative.parent for entry in plan.entries}
            text = translator.tr(
                "tools.anonymizer.selection",
                images=images,
                folders=len(folders),
                skipped=len(plan.entries) - images,
            )
            if plan.links_skipped:
                text += " " + translator.tr(
                    "tools.anonymizer.links_skipped", count=plan.links_skipped
                )
            if plan.truncated:
                text += " " + translator.tr("tools.anonymizer.truncated", count=len(plan.entries))
            self._selection.setText(text)
            preview = self.destination_preview()
            self._destination.setText(
                translator.tr(
                    "tools.anonymizer.destination",
                    folder=display_path(preview) if preview is not None else "",
                )
            )
        if plan is None:
            due: tuple[QPushButton, ...] = (self._files_button, self._folder_button)
        elif self._written is None and images:
            due = (self._run_button,)
        else:
            due = ()
        guide(
            [self._files_button, self._folder_button, self._location_button, self._run_button],
            *due,
        )

    def retranslate_ui(self) -> None:
        tr = translator.tr
        self._note.setText(tr("tools.anonymizer.note"))
        self._limits.setText(tr("tools.anonymizer.limits"))
        self._generic.setText(tr("tools.anonymizer.generic_names"))
        self._generic.setToolTip(tr("tools.anonymizer.generic_names_tooltip"))
        self._refresh_names()
        self._files_button.setText(tr("tools.anonymizer.choose_files"))
        self._folder_button.setText(tr("tools.anonymizer.choose_folder"))
        self._recursive.setText(tr("tools.anonymizer.recursive"))
        self._tree.setHeaderLabels(
            [
                tr("tools.anonymizer.column.name"),
                tr("tools.anonymizer.column.action"),
                tr("tools.anonymizer.column.copy"),
            ]
        )
        self._location_button.setText(tr("tools.anonymizer.change_location"))
        self._run_button.setText(tr("tools.anonymizer.run"))
        self._fill_tree()
        self._refresh()


__all__ = ["AnonymizerTool"]
