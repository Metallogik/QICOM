"""The Export step's own written stock-take of the session.

The Export step used to show the results table a second time, which said
nothing about what a click on "Save/Export..." was actually going to
write to disk - reported as "in the export section I want to see only a
written report of everything that was analysed, generated and that will
be exported". This panel is that report: a plain, readable list of what
the session currently holds, followed by the files the export will
produce, plus the two opt-out choices (heatmap images and the key images
carrying the ROIs) that materially change how much gets written.

It owns no export logic of its own - it renders whatever
``set_summary`` is handed and re-emits the user's intent, exactly like
ResultsTablePanel's own export button did before it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.theming import theme_manager
from qicom.i18n import translator


@dataclass(frozen=True, slots=True)
class ExportSummary:
    """Everything the Export step reports on, already reduced to display
    strings by MainWindow - this panel deliberately knows nothing about
    ROIs, pairs or workbooks.

    `analysed` is what the session currently holds (images, ROIs, lines,
    annotations, metric results); `generated` is what the analysis
    produced beyond the results table itself (charts, heatmaps);
    `files` is what an export will write, in the order it writes them.
    """

    analysed: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def _render_section(title: str, entries: list[str], empty_text: str) -> str:
    """Escaped, not interpolated raw: entries carry user-authored text
    (ROI and line names, image file names), and an unescaped "<" there
    would silently swallow the rest of the line."""
    items = "".join(f"<li>{escape(entry)}</li>" for entry in entries)
    body = f"<ul>{items}</ul>" if entries else f"<p><i>{escape(empty_text)}</i></p>"
    return f"<h3>{escape(title)}</h3>{body}"


class ExportSummaryPanel(QWidget):
    export_requested = Signal()
    # Both opt-outs change what an export writes, so MainWindow rebuilds
    # the file list from scratch whenever either one is toggled.
    options_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._heading = QLabel()
        self._heading.setObjectName("workflowTitle")
        self._heading.setWordWrap(True)

        self._report = QTextBrowser()
        self._report.setOpenExternalLinks(False)

        # Both on by default: the request was explicitly "they are
        # selected by default, the user has to untick them if they don't
        # want them", so an export nobody configures still contains
        # everything it did before this panel existed.
        self._include_heatmaps_check = QCheckBox()
        self._include_heatmaps_check.setChecked(True)
        self._include_heatmaps_check.toggled.connect(self._on_option_toggled)
        self._include_key_images_check = QCheckBox()
        self._include_key_images_check.setChecked(True)
        self._include_key_images_check.toggled.connect(self._on_option_toggled)
        self._include_llm_report_check = QCheckBox()
        self._include_llm_report_check.setChecked(True)
        self._include_llm_report_check.toggled.connect(self._on_option_toggled)

        self._export_button = QPushButton()
        self._export_button.setObjectName("export")
        self._export_button.setEnabled(False)
        self._export_button.clicked.connect(self.export_requested.emit)

        button_row = QHBoxLayout()
        button_row.addWidget(self._export_button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._heading)
        layout.addWidget(self._report, 1)
        layout.addWidget(self._include_key_images_check)
        layout.addWidget(self._include_heatmaps_check)
        layout.addWidget(self._include_llm_report_check)
        layout.addLayout(button_row)

        self._summary = ExportSummary()
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_icons(theme_manager.theme)
        theme_manager.on_theme_changed(self._refresh_icons)

    def _refresh_icons(self, theme: str) -> None:
        self._export_button.setIcon(paint_icon("export", ink_color(theme)))

    @property
    def include_heatmaps(self) -> bool:
        return self._include_heatmaps_check.isChecked()

    @property
    def include_key_images(self) -> bool:
        return self._include_key_images_check.isChecked()

    @property
    def include_llm_report(self) -> bool:
        return self._include_llm_report_check.isChecked()

    def set_summary(self, summary: ExportSummary) -> None:
        """Replace the whole report. `summary.files` already reflects the
        current checkbox state - MainWindow rebuilds it on every toggle
        (see _on_option_toggled), so the file list never promises output
        the user just opted out of."""
        self._summary = summary
        self._render()

    def set_export_enabled(self, enabled: bool) -> None:
        self._export_button.setEnabled(enabled)

    def _on_option_toggled(self, _checked: bool) -> None:
        self.options_changed.emit()

    def _render(self) -> None:
        self._report.setHtml(
            _render_section(
                translator.tr("export_summary.analysed"),
                self._summary.analysed,
                translator.tr("export_summary.nothing_yet"),
            )
            + _render_section(
                translator.tr("export_summary.generated"),
                self._summary.generated,
                translator.tr("export_summary.nothing_yet"),
            )
            + _render_section(
                translator.tr("export_summary.files"),
                self._summary.files,
                translator.tr("export_summary.nothing_yet"),
            )
        )

    def retranslate_ui(self) -> None:
        self._heading.setText(translator.tr("export_summary.heading"))
        self._include_heatmaps_check.setText(translator.tr("export_summary.include_heatmaps"))
        self._include_heatmaps_check.setToolTip(
            translator.tr("export_summary.include_heatmaps.tooltip")
        )
        self._include_key_images_check.setText(translator.tr("export_summary.include_key_images"))
        self._include_llm_report_check.setText(translator.tr("export_summary.include_llm_report"))
        self._include_llm_report_check.setToolTip(
            translator.tr("export_summary.include_llm_report.tooltip")
        )
        self._include_key_images_check.setToolTip(
            translator.tr("export_summary.include_key_images.tooltip")
        )
        self._export_button.setText(translator.tr("results_table.export_button"))
        self._render()
