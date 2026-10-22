"""Full quantitative pre-flight page shown before calculation.

Calculate's progress belongs to this page rather than to a modal dialog
floating over it. A long run used to open an application-modal
QProgressDialog, which is a second window covering the very page that
started the work; here the bottom edge of the page simply turns from the
Calculate button into a progress panel - same place, same width - with
the bar, the metric currently running and a Cancel button in it.

The page is not "still usable while a run is in flight", though: the
metric list and the data range are the inputs of the run that is already
executing, so they are disabled for its duration (MainWindow does the
same for the rest of the window - see its own `_set_calculation_lock`),
which is what the modality of the old dialog was really providing. What
is lost is only the window on top; what is gained is that the progress
is where the user was already looking."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.widgets.metric_selection_panel import MetricSelectionPanel
from qicom.i18n import translator
from qicom.metrics.catalog import catalog


class AnalysisSetupPanel(QWidget):
    calculate_requested = Signal(float)
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._calculating = False
        self._title = QLabel()
        self._title.setObjectName("panelSectionTitle")
        self._description = QLabel()
        self._description.setObjectName("panelSectionHint")
        self._description.setWordWrap(True)
        self._effort_summary = QLabel()
        self._effort_summary.setObjectName("analysisEffortSummary")

        self.metric_panel = MetricSelectionPanel(metric_selection_settings.enabled_ids())
        self.metric_panel.selection_changed.connect(self._on_selection_changed)

        self._data_range_label = QLabel()
        self._data_range = QDoubleSpinBox()
        self._data_range.setRange(1e-6, 1e12)
        self._data_range.setDecimals(4)
        self._data_range.setValue(1.0)
        # The one action of this page, and the page is a wall of metric
        # rows each wearing a green/amber/coral effort badge - so Calculate
        # gets a color that belongs to nothing else (see
        # themes/*.qss #calculateButton) and the full width of the bottom
        # edge, outside the content margins, where nothing else competes
        # with it.
        self._calculate = QPushButton()
        self._calculate.setObjectName("calculateButton")
        self._calculate.setMinimumHeight(54)
        self._calculate.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._calculate.setCursor(Qt.CursorShape.PointingHandCursor)
        self._calculate.clicked.connect(self._on_calculate)

        # Takes the Calculate button's own place at the foot of the page
        # while a run is in flight, so the answer to "did my click do
        # anything?" appears exactly where the click happened.
        self._progress_panel = QFrame()
        self._progress_panel.setObjectName("analysisProgressPanel")
        self._progress_title = QLabel()
        self._progress_title.setObjectName("analysisProgressTitle")
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("analysisProgressBar")
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setMinimumHeight(22)
        self._progress_step = QLabel()
        self._progress_step.setObjectName("analysisProgressStep")
        self._progress_step.setWordWrap(True)
        # Says out loud what the window is doing to itself: the other
        # steps really are refusing clicks, they are not broken.
        self._progress_notice = QLabel()
        self._progress_notice.setObjectName("analysisProgressNotice")
        self._progress_notice.setWordWrap(True)
        self._progress_cancel = QPushButton()
        self._progress_cancel.setObjectName("analysisProgressCancel")
        self._progress_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._progress_cancel.clicked.connect(self._on_cancel)

        progress_layout = QVBoxLayout(self._progress_panel)
        progress_layout.setContentsMargins(22, 12, 22, 12)
        progress_layout.setSpacing(6)
        progress_header = QHBoxLayout()
        progress_header.addWidget(self._progress_title, 1)
        progress_header.addWidget(self._progress_cancel)
        progress_layout.addLayout(progress_header)
        progress_layout.addWidget(self._progress_bar)
        progress_layout.addWidget(self._progress_step)
        progress_layout.addWidget(self._progress_notice)
        self._progress_panel.hide()

        data_range_row = QHBoxLayout()
        data_range_row.addWidget(self._data_range_label)
        data_range_row.addWidget(self._data_range)
        data_range_row.addStretch(1)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(22, 18, 22, 12)
        content_layout.addWidget(self._title)
        content_layout.addWidget(self._description)
        content_layout.addWidget(self._effort_summary)
        content_layout.addWidget(self.metric_panel, 1)
        content_layout.addLayout(data_range_row)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(content, 1)
        # Only ever one of the two is visible - see set_calculating.
        layout.addWidget(self._calculate)
        layout.addWidget(self._progress_panel)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_effort()

    def _on_selection_changed(self) -> None:
        metric_selection_settings.set_enabled_ids(self.metric_panel.selected_ids())
        self._refresh_effort()

    def _on_calculate(self) -> None:
        self._on_selection_changed()
        self.calculate_requested.emit(self._data_range.value())

    def _on_cancel(self) -> None:
        """Cancellation is a request, not a fact: the worker stops at the
        next metric boundary (see AppController.cancel_calculation), so
        MainWindow is the one that decides whether it was accepted and
        closes this panel when the run actually ends."""
        self.cancel_requested.emit()

    def set_default_data_range(self, value: float) -> None:
        self._data_range.setValue(value)

    def data_range(self) -> float:
        return self._data_range.value()

    def is_calculating(self) -> bool:
        """Whether the page is showing a run of its own - read by
        MainWindow.closeEvent, which must not let Qt tear the window (and
        with it the running QThread) down mid-calculation."""
        return self._calculating

    def set_calculating(self, calculating: bool) -> None:
        """Swap the foot of the page between its two states: the
        Calculate button, or the progress panel with this run's own inputs
        locked behind it.

        The bar starts indeterminate (min == max == 0, Qt's own busy
        animation) because the number of steps is not known until
        run_all_metrics reports its first one - set_progress flips it to a
        real 0-100% bar then, exactly as the modal dialog used to."""
        # Tracked explicitly rather than read back off the widgets:
        # isVisible() is False for every child of a window that has not
        # been shown yet, which would make a running calculation look
        # finished to closeEvent's own guard.
        self._calculating = calculating
        self._calculate.setVisible(not calculating)
        self._calculate.setEnabled(not calculating)
        self._progress_panel.setVisible(calculating)
        # The metric selection and the data range are this run's inputs;
        # editing them mid-run would describe a calculation that is not the
        # one actually executing.
        self.metric_panel.setEnabled(not calculating)
        self._data_range.setEnabled(not calculating)
        if calculating:
            self._progress_bar.setRange(0, 0)
            self._progress_bar.setValue(0)
            self._progress_step.setText(translator.tr("analysis_setup.progress_preparing"))
            self._progress_cancel.setEnabled(True)
            self._progress_cancel.setText(translator.tr("metric_selection.cancel"))
            self._progress_title.setText(translator.tr("results_table.calculating"))

    def set_progress(self, completed: int, total: int) -> None:
        """One more step of the run finished. A zero total is ignored
        rather than dividing by it - the indeterminate animation simply
        continues."""
        if total <= 0:
            return
        self._progress_bar.setRange(0, total)
        self._progress_bar.setValue(completed)

    def set_progress_step(self, step: str) -> None:
        """The short human-readable name of whatever is running right
        now (see qicom.metrics.runner's own step_callback docstring) - so a
        long run reads as "doing something specific" rather than as a
        stalled bar."""
        self._progress_step.setText(step)

    def set_cancelling(self) -> None:
        """Cancel was pressed and accepted: the run stops at the next
        metric boundary, so the button closes rather than inviting a second
        press that would do nothing."""
        self._progress_cancel.setEnabled(False)
        self._progress_cancel.setText(translator.tr("learned_weights.cancelling"))

    def refresh_selection(self) -> None:
        self.metric_panel.set_selected_ids(metric_selection_settings.enabled_ids())
        self._refresh_effort()

    def _refresh_effort(self) -> None:
        counts = {"low": 0, "medium": 0, "high": 0}
        for metric_id in self.metric_panel.selected_ids():
            descriptor = catalog.get_descriptor(metric_id)
            counts[descriptor.effort] += 1
        overall = "high" if counts["high"] else "medium" if counts["medium"] else "low"
        self._effort_summary.setText(
            translator.tr(
                "analysis_setup.summary",
                selected=sum(counts.values()),
                total=len(catalog.all_descriptors()),
                effort=translator.tr(f"metric_selection.effort_{overall}"),
            )
        )

    def retranslate_ui(self) -> None:
        self._title.setText(translator.tr("analysis_setup.title"))
        self._description.setText(translator.tr("analysis_setup.description"))
        self._data_range_label.setText(translator.tr("results_table.data_range"))
        self._calculate.setText(translator.tr("results_table.calculate"))
        self._progress_title.setText(translator.tr("results_table.calculating"))
        self._progress_notice.setText(translator.tr("analysis_setup.progress_locked"))
        self._progress_cancel.setText(
            translator.tr("metric_selection.cancel")
            if self._progress_cancel.isEnabled()
            else translator.tr("learned_weights.cancelling")
        )
        self._refresh_effort()


__all__ = ["AnalysisSetupPanel"]
