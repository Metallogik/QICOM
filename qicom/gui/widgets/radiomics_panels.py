"""Dedicated configuration and result panels for radiomics.

``RadiomicsSegmentationPanel`` now lives in
:mod:`qicom.gui.widgets.radiomics_roi_panel` (it grew into the whole ROI
workspace) and is re-exported here so existing imports keep working.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import explained_error
from qicom.gui.widgets.full_width_tabs import use_full_width_tabs
from qicom.gui.widgets.radiomics_roi_panel import RadiomicsSegmentationPanel
from qicom.i18n import translator
from qicom.radiomics.config import MIRP_ADDITIONAL_CLASSES, RadiomicsConfig
from qicom.radiomics.descriptors import CATEGORY_DESCRIPTORS
from qicom.radiomics.models import RadiomicsStudyIdentity


class DataFrameTable(QTableView):
    def __init__(self, parent: QWidget | None = None, *, heatmap: bool = False) -> None:
        super().__init__(parent)
        self._heatmap = heatmap
        self._model = QStandardItemModel(self)
        # Sort on the value stored in UserRole, not on the formatted text:
        # QStandardItem's default comparison is lexicographic, which
        # ordered a feature/delta column as 1, 10, 2 - a column that looks
        # sorted and isn't. Every cell (text included) carries a UserRole
        # value so no column falls back to comparing against nothing.
        self._model.setSortRole(Qt.ItemDataRole.UserRole)
        self.setModel(self._model)
        self.setSortingEnabled(True)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        header = self.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)

    def set_dataframe(self, dataframe: pd.DataFrame) -> None:
        self.setSortingEnabled(False)
        self._model.clear()
        self._model.setHorizontalHeaderLabels([str(column) for column in dataframe.columns])
        # A missing value has to sort *somewhere*: as -inf in a numeric
        # column (all the blanks group at one end) and as an empty string
        # in a text one, so the sort key never mixes types within a column.
        numeric_column = [bool(pd.api.types.is_numeric_dtype(dtype)) for dtype in dataframe.dtypes]
        for record in dataframe.itertuples(index=False, name=None):
            row: list[QStandardItem] = []
            for column, value in enumerate(record):
                if pd.isna(value):
                    item = QStandardItem("")
                    item.setData(
                        float("-inf") if numeric_column[column] else "",
                        Qt.ItemDataRole.UserRole,
                    )
                elif isinstance(value, float):
                    item = QStandardItem(f"{value:.10g}")
                    item.setData(float(value), Qt.ItemDataRole.UserRole)
                    if self._heatmap and -1.0 <= value <= 1.0:
                        strength = int(abs(value) * 150)
                        color = (
                            QColor(70, 120, 235, strength)
                            if value >= 0
                            else QColor(225, 80, 75, strength)
                        )
                        item.setBackground(color)
                elif isinstance(value, (int, bool)):
                    item = QStandardItem(str(value))
                    item.setData(float(value), Qt.ItemDataRole.UserRole)
                else:
                    item = QStandardItem(str(value))
                    item.setData(str(value), Qt.ItemDataRole.UserRole)
                row.append(item)
            self._model.appendRow(row)
        self.resizeColumnsToContents()
        self.setSortingEnabled(True)


class RadiomicsConfigPanel(QWidget):
    """All settings that define one reproducible extraction profile."""

    config_edited = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # (apply, key, fallback) for every visible text: replayed on a language
        # change, which this panel used to ignore until the window was reopened.
        self._translations: list[tuple[Callable[[str], None], str, str | None]] = []
        defaults = RadiomicsConfig()
        self._ibsi_preset = "none"
        self._backend = QComboBox()
        self._backend.addItem("PyRadiomics", "pyradiomics")
        self._backend.addItem("MIRP", "mirp")
        self._profile_name = defaults.profile_name
        self._profile_id = defaults.profile_id
        self._profile_version = defaults.profile_version
        self._subject_id = QLineEdit()
        self._case_id = QLineEdit()
        self._lesion_id = QLineEdit()
        self._timepoint_id = QLineEdit()
        self._acquisition_id = QLineEdit()
        identity_group = self._translated_group("radiomics.identity.title")
        identity_form = QFormLayout(identity_group)
        self._translated_row(identity_form, "radiomics.identity.subject", self._subject_id)
        self._translated_row(identity_form, "radiomics.identity.case", self._case_id)
        self._translated_row(identity_form, "radiomics.identity.lesion", self._lesion_id)
        self._translated_row(identity_form, "radiomics.identity.timepoint", self._timepoint_id)
        self._translated_row(identity_form, "radiomics.identity.acquisition", self._acquisition_id)
        self._spacing_x = self._positive_spin(1.0, 0.01, 20.0, 3)
        self._spacing_y = self._positive_spin(1.0, 0.01, 20.0, 3)
        self._spacing_z = self._positive_spin(1.0, 0.01, 20.0, 3)
        self._interpolator = QComboBox()
        self._translated_item(self._interpolator, "radiomics.config.interp_bspline", "bspline")
        self._translated_item(self._interpolator, "radiomics.config.interp_linear", "linear")
        self._translated_item(self._interpolator, "radiomics.config.interp_nearest", "nearest")
        self._dimensionality = QComboBox()
        self._translated_item(self._dimensionality, "radiomics.config.dimension_auto", "auto")
        self._dimensionality.addItem("2D", "2d")
        self._dimensionality.addItem("3D", "3d")

        spacing_row = QWidget()
        spacing_layout = QHBoxLayout(spacing_row)
        spacing_layout.setContentsMargins(0, 0, 0, 0)
        for label, spin in (
            ("X", self._spacing_x),
            ("Y", self._spacing_y),
            ("Z", self._spacing_z),
        ):
            spacing_layout.addWidget(QLabel(label))
            spacing_layout.addWidget(spin)

        spatial_group = self._translated_group("radiomics.config.spatial")
        spatial_form = QFormLayout(spatial_group)
        self._translated_row(spatial_form, "radiomics.config.backend", self._backend)
        self._translated_row(spatial_form, "radiomics.config.dimension", self._dimensionality)
        self._translated_row(spatial_form, "radiomics.config.spacing", spacing_row)
        self._translated_row(spatial_form, "radiomics.config.interpolator", self._interpolator)

        self._binning_mode = QComboBox()
        self._translated_item(self._binning_mode, "radiomics.config.bin_width", "width")
        self._translated_item(self._binning_mode, "radiomics.config.bin_count", "count")
        self._bin_width = self._positive_spin(25.0, 0.01, 100000.0, 3)
        self._bin_count = QSpinBox()
        self._bin_count.setRange(2, 4096)
        self._bin_count.setValue(64)
        binning_group = self._translated_group("radiomics.config.binning")
        binning_form = QFormLayout(binning_group)
        self._translated_row(binning_form, "radiomics.config.method", self._binning_mode)
        self._translated_row(binning_form, "radiomics.config.width_value", self._bin_width)
        self._translated_row(binning_form, "radiomics.config.count_value", self._bin_count)
        self._bin_origin = QComboBox()
        self._translated_item(self._bin_origin, "radiomics.config.bin_origin_roi", "roi-minimum")
        self._translated_item(
            self._bin_origin, "radiomics.config.bin_origin_reseg", "resegmentation-lower"
        )
        self._translated_row(binning_form, "radiomics.config.bin_origin", self._bin_origin)
        self._binning_mode.currentIndexChanged.connect(self._refresh_binning_enabled)

        self._normalization = QComboBox()
        self._translated_item(self._normalization, "radiomics.config.norm_none", "none")
        self._translated_item(self._normalization, "radiomics.config.norm_zscore", "z-score")
        self._translated_item(self._normalization, "radiomics.config.norm_scale", "scale")
        self._translated_item(self._normalization, "radiomics.config.norm_percentile", "percentile")
        self._normalization_scale = self._positive_spin(1.0, 0.000001, 1000000.0, 6)
        self._clip_outliers = self._translated_check_box("radiomics.config.clip_enable")
        self._clip_std = self._positive_spin(3.0, 0.01, 100.0, 3)
        self._percentile_low = QDoubleSpinBox()
        self._percentile_low.setRange(0.0, 99.99)
        self._percentile_low.setValue(1.0)
        self._percentile_high = QDoubleSpinBox()
        self._percentile_high.setRange(0.01, 100.0)
        self._percentile_high.setValue(99.0)
        normalization_group = self._translated_group("radiomics.config.normalization")
        normalization_form = QFormLayout(normalization_group)
        self._translated_row(normalization_form, "radiomics.config.method", self._normalization)
        self._translated_row(
            normalization_form, "radiomics.config.normalization_scale", self._normalization_scale
        )
        normalization_form.addRow(self._clip_outliers, self._clip_std)
        percentile_row = QWidget()
        percentile_layout = QHBoxLayout(percentile_row)
        percentile_layout.setContentsMargins(0, 0, 0, 0)
        percentile_layout.addWidget(self._percentile_low)
        percentile_layout.addWidget(QLabel("–"))
        percentile_layout.addWidget(self._percentile_high)
        self._translated_row(
            normalization_form, "radiomics.config.percentile_range", percentile_row
        )

        self._resegmentation = self._translated_check_box("radiomics.config.reseg_enable")
        self._resegmentation_mode = QComboBox()
        self._translated_item(
            self._resegmentation_mode, "radiomics.config.reseg_absolute", "absolute"
        )
        self._translated_item(self._resegmentation_mode, "radiomics.config.reseg_sigma", "sigma")
        self._resegmentation_low = QDoubleSpinBox()
        self._resegmentation_low.setRange(-1e9, 1e9)
        self._resegmentation_low.setDecimals(4)
        self._resegmentation_high = QDoubleSpinBox()
        self._resegmentation_high.setRange(-1e9, 1e9)
        self._resegmentation_high.setDecimals(4)
        self._resegmentation_high.setValue(100.0)
        resegmentation_group = self._translated_group("radiomics.config.resegmentation")
        resegmentation_form = QFormLayout(resegmentation_group)
        resegmentation_form.addRow(self._resegmentation)
        self._translated_row(
            resegmentation_form, "radiomics.config.method", self._resegmentation_mode
        )
        resegmentation_row = QWidget()
        resegmentation_layout = QHBoxLayout(resegmentation_row)
        resegmentation_layout.setContentsMargins(0, 0, 0, 0)
        resegmentation_layout.addWidget(self._resegmentation_low)
        resegmentation_layout.addWidget(QLabel("–"))
        resegmentation_layout.addWidget(self._resegmentation_high)
        self._translated_row(
            resegmentation_form, "radiomics.config.reseg_range", resegmentation_row
        )

        self._delta_method = QComboBox()
        self._translated_item(self._delta_method, "radiomics.config.delta_symmetric", "symmetric")
        self._translated_item(self._delta_method, "radiomics.config.delta_classic", "classic")
        self._delta_epsilon = self._positive_spin(1e-12, 1e-15, 1.0, 15)
        delta_group = self._translated_group("radiomics.config.delta")
        delta_form = QFormLayout(delta_group)
        self._translated_row(delta_form, "radiomics.config.method", self._delta_method)
        self._translated_row(delta_form, "radiomics.config.delta_epsilon", self._delta_epsilon)

        self._original = self._translated_check_box("radiomics.config.filter_original")
        self._original.setChecked(True)
        self._wavelet = self._translated_check_box("radiomics.config.filter_wavelet")
        self._log = self._translated_check_box("radiomics.config.filter_log")
        self._log_sigmas = QLineEdit("1.0, 2.0, 3.0")
        filters_group = self._translated_group("radiomics.config.image_types")
        filters_layout = QVBoxLayout(filters_group)
        filters_layout.addWidget(self._original)
        filters_layout.addWidget(self._wavelet)
        log_row = QHBoxLayout()
        log_row.addWidget(self._log)
        log_row.addWidget(self._translated_label("radiomics.config.log_sigma_unit"))
        log_row.addWidget(self._log_sigmas)
        filters_layout.addLayout(log_row)

        features_group = self._translated_group("radiomics.config.feature_classes")
        features_layout = QVBoxLayout(features_group)
        self._feature_checks: dict[str, QCheckBox] = {}
        for key, descriptor in CATEGORY_DESCRIPTORS.items():
            checkbox = self._translated_check_box(
                f"radiomics.family.{key}", default=descriptor.title
            )
            checkbox.setChecked(key not in MIRP_ADDITIONAL_CLASSES)
            checkbox.setEnabled(key not in MIRP_ADDITIONAL_CLASSES)
            checkbox.setToolTip(
                f"{descriptor.description}\n\n{descriptor.mathematical_range}\n\n{descriptor.sensitivity}"
            )
            self._feature_checks[key] = checkbox
            features_layout.addWidget(checkbox)

        self._backend.currentIndexChanged.connect(self._backend_changed)
        self._profile_summary = QLabel()
        self._profile_summary.setWordWrap(True)
        self._profile_summary.setObjectName("radiomicsProfileSummary")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(identity_group)
        layout.addWidget(spatial_group)
        layout.addWidget(binning_group)
        layout.addWidget(normalization_group)
        layout.addWidget(resegmentation_group)
        layout.addWidget(delta_group)
        layout.addWidget(filters_group)
        layout.addWidget(features_group)
        layout.addWidget(self._profile_summary)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        editable_widgets: list[QWidget] = [
            *self.findChildren(QComboBox),
            *self.findChildren(QDoubleSpinBox),
            *self.findChildren(QSpinBox),
            *self.findChildren(QCheckBox),
            *self.findChildren(QLineEdit),
        ]
        for widget in editable_widgets:
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._on_edited)
            elif isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                widget.valueChanged.connect(self._on_edited)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._on_edited)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._on_edited)
        self._refresh_binning_enabled()
        self._refresh_summary()
        translator.on_language_changed(self.retranslate_ui)

    def _translate(
        self, apply: Callable[[str], None], key: str, default: str | None = None
    ) -> None:
        self._translations.append((apply, key, default))
        apply(translator.tr(key) if default is None else translator.tr_or(key, default))

    def _translated_group(self, key: str) -> QGroupBox:
        group = QGroupBox()
        self._translate(group.setTitle, key)
        return group

    def _translated_check_box(self, key: str, *, default: str | None = None) -> QCheckBox:
        check_box = QCheckBox()
        self._translate(check_box.setText, key, default)
        return check_box

    def _translated_label(self, key: str) -> QLabel:
        label = QLabel()
        self._translate(label.setText, key)
        return label

    def _translated_row(self, form: QFormLayout, key: str, field: QWidget) -> None:
        form.addRow(self._translated_label(key), field)

    def _translated_item(self, combo: QComboBox, key: str, data: str) -> None:
        index = combo.count()
        combo.addItem("", data)
        self._translate(lambda text: combo.setItemText(index, text), key)

    def retranslate_ui(self) -> None:
        for apply, key, default in self._translations:
            apply(translator.tr(key) if default is None else translator.tr_or(key, default))
        self._refresh_summary()

    @staticmethod
    def _positive_spin(
        value: float, minimum: float, maximum: float, decimals: int
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setValue(value)
        return spin

    def _refresh_binning_enabled(self) -> None:
        is_width = self._binning_mode.currentData() == "width"
        self._bin_width.setEnabled(is_width)
        self._bin_count.setEnabled(not is_width)

    def _on_edited(self, *_args: object) -> None:
        self._config_changed()
        self._refresh_summary()

    def _parse_sigmas(self) -> tuple[float, ...]:
        if not self._log.isChecked():
            return ()
        parts = [part.strip() for part in self._log_sigmas.text().split(",") if part.strip()]
        if not parts:
            raise ValueError("Enter at least one LoG sigma in millimetres.")
        try:
            return tuple(float(part) for part in parts)
        except ValueError as exc:
            raise ValueError("LoG sigma values must be comma-separated numbers.") from exc

    def _config_changed(self) -> None:
        if self._ibsi_preset != "none":
            self._ibsi_preset = "none"
            self._profile_id = "qicom-custom-mirp"
            self._profile_name = "Custom MIRP profile"
        self.config_edited.emit()

    def _refresh_backend_controls(self) -> None:
        mirp = self._backend.currentData() == "mirp"
        self._original.setEnabled(not mirp)
        if mirp:
            self._original.setChecked(True)
        for key in MIRP_ADDITIONAL_CLASSES:
            self._feature_checks[key].setEnabled(mirp)
            if not mirp:
                self._feature_checks[key].setChecked(False)
        for widget in (self._wavelet, self._log):
            widget.setEnabled(not mirp)
            if mirp:
                widget.setChecked(False)

    def _backend_changed(self) -> None:
        self._refresh_backend_controls()
        self._config_changed()

    def config(self) -> RadiomicsConfig:
        return RadiomicsConfig(
            backend=self._backend.currentData(),
            ibsi_preset=self._ibsi_preset,
            profile_name=self._profile_name,
            profile_id=self._profile_id,
            profile_version=self._profile_version,
            target_spacing=(
                self._spacing_x.value(),
                self._spacing_y.value(),
                self._spacing_z.value(),
            ),
            interpolator=self._interpolator.currentData(),
            binning_mode=self._binning_mode.currentData(),
            bin_width=self._bin_width.value(),
            bin_count=self._bin_count.value(),
            fixed_bin_origin=self._bin_origin.currentData(),
            dimensionality=self._dimensionality.currentData(),
            enable_original=self._original.isChecked(),
            enable_wavelet=self._wavelet.isChecked(),
            log_sigmas_mm=self._parse_sigmas(),
            feature_classes=tuple(
                key for key, checkbox in self._feature_checks.items() if checkbox.isChecked()
            ),
            normalization=self._normalization.currentData(),
            normalization_scale=self._normalization_scale.value(),
            clipping_outliers_std=(
                self._clip_std.value() if self._clip_outliers.isChecked() else None
            ),
            percentile_range=(self._percentile_low.value(), self._percentile_high.value()),
            resegmentation_range=(
                (self._resegmentation_low.value(), self._resegmentation_high.value())
                if self._resegmentation.isChecked()
                else None
            ),
            resegmentation_mode=self._resegmentation_mode.currentData(),
            delta_percent_method=self._delta_method.currentData(),
            delta_near_zero_epsilon=self._delta_epsilon.value(),
        )

    def identity(self) -> RadiomicsStudyIdentity:
        return RadiomicsStudyIdentity(
            subject_id=self._subject_id.text().strip(),
            case_id=self._case_id.text().strip(),
            lesion_id=self._lesion_id.text().strip(),
            timepoint_id=self._timepoint_id.text().strip(),
            acquisition_id=self._acquisition_id.text().strip(),
        )

    def set_identity(self, identity: RadiomicsStudyIdentity) -> None:
        widgets = (
            self._subject_id,
            self._case_id,
            self._lesion_id,
            self._timepoint_id,
            self._acquisition_id,
        )
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self._subject_id.setText(identity.subject_id)
            self._case_id.setText(identity.case_id)
            self._lesion_id.setText(identity.lesion_id)
            self._timepoint_id.setText(identity.timepoint_id)
            self._acquisition_id.setText(identity.acquisition_id)
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def set_config(self, config: RadiomicsConfig) -> None:
        widgets = self.findChildren(QWidget)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self._ibsi_preset = config.ibsi_preset
            self._set_combo_data(self._backend, config.backend)
            self._refresh_backend_controls()
            self._profile_name = config.profile_name
            self._profile_id = config.profile_id
            self._profile_version = config.profile_version
            self._spacing_x.setValue(config.target_spacing[0])
            self._spacing_y.setValue(config.target_spacing[1])
            self._spacing_z.setValue(config.target_spacing[2])
            self._set_combo_data(self._interpolator, config.interpolator)
            self._set_combo_data(self._binning_mode, config.binning_mode)
            self._set_combo_data(self._dimensionality, config.dimensionality)
            self._bin_width.setValue(config.bin_width)
            self._bin_count.setValue(config.bin_count)
            self._set_combo_data(self._bin_origin, config.fixed_bin_origin)
            self._original.setChecked(config.enable_original)
            self._wavelet.setChecked(config.enable_wavelet)
            self._log.setChecked(bool(config.log_sigmas_mm))
            self._log_sigmas.setText(", ".join(str(value) for value in config.log_sigmas_mm))
            self._set_combo_data(self._normalization, config.normalization)
            self._normalization_scale.setValue(config.normalization_scale)
            self._clip_outliers.setChecked(config.clipping_outliers_std is not None)
            if config.clipping_outliers_std is not None:
                self._clip_std.setValue(config.clipping_outliers_std)
            self._percentile_low.setValue(config.percentile_range[0])
            self._percentile_high.setValue(config.percentile_range[1])
            self._resegmentation.setChecked(config.resegmentation_range is not None)
            if config.resegmentation_range is not None:
                self._resegmentation_low.setValue(config.resegmentation_range[0])
                self._resegmentation_high.setValue(config.resegmentation_range[1])
            self._set_combo_data(self._resegmentation_mode, config.resegmentation_mode)
            self._set_combo_data(self._delta_method, config.delta_percent_method)
            self._delta_epsilon.setValue(config.delta_near_zero_epsilon)
            for key, checkbox in self._feature_checks.items():
                checkbox.setChecked(key in config.feature_classes)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._refresh_binning_enabled()
        self._refresh_summary()

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _refresh_summary(self) -> None:
        try:
            config = self.config()
        except ValueError as exc:
            self._profile_summary.setText(explained_error("errors.summary.invalid_config", exc))
            return
        filters = [
            name
            for name, enabled in (
                ("Original", config.enable_original),
                ("Wavelet", config.enable_wavelet),
                ("LoG", bool(config.log_sigmas_mm)),
            )
            if enabled
        ]
        binning = (
            f"width={config.bin_width:g}"
            if config.binning_mode == "width"
            else f"count={config.bin_count}"
        )
        self._profile_summary.setText(
            translator.tr(
                "radiomics.config.summary",
                profile=config.profile_name,
                version=config.profile_version,
                spacing=config.target_spacing,
                binning=binning,
                filters=", ".join(filters),
                normalization=config.normalization,
                resegmentation=config.resegmentation_range or "none",
                delta=config.delta_percent_method,
                hash=config.config_hash[:16],
            )
        )


class RadiomicsResultsPanel(QWidget):
    extract_requested = Signal()
    export_excel_requested = Signal()
    export_csv_requested = Signal()
    export_html_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._busy = False
        self._stale = False
        self._last_context = (False, False, 0, "", 0)
        self._features = DataFrameTable()
        self._delta = DataFrameTable()
        self._repeatability = DataFrameTable()
        self._correlation = DataFrameTable(heatmap=True)
        self._provenance = DataFrameTable()
        self._mask_qc = DataFrameTable()
        self._feature_qc = DataFrameTable()
        self._outcomes = DataFrameTable()
        self._tabs = QTabWidget()
        use_full_width_tabs(self._tabs)
        self._tabs.addTab(self._features, translator.tr("radiomics.results.features"))
        self._tabs.addTab(self._delta, translator.tr("radiomics.results.delta"))
        self._tabs.addTab(self._repeatability, translator.tr("radiomics.results.repeatability"))
        self._tabs.addTab(self._correlation, translator.tr("radiomics.results.correlation"))
        self._tabs.addTab(self._provenance, translator.tr("radiomics.results.provenance"))
        self._tabs.addTab(self._mask_qc, translator.tr("radiomics.results.mask_qc"))
        self._tabs.addTab(self._feature_qc, translator.tr("radiomics.results.feature_qc"))
        self._tabs.addTab(self._outcomes, translator.tr("radiomics.results.outcomes"))
        self._extract = QPushButton(translator.tr("radiomics.results.extract"))
        self._extract.setObjectName("radiomicsExtractButton")
        self._extract.clicked.connect(self.extract_requested)
        self._export_excel = QPushButton(translator.tr("radiomics.results.export_excel"))
        self._export_excel.clicked.connect(self.export_excel_requested)
        self._export_csv = QPushButton(translator.tr("radiomics.results.export_csv"))
        self._export_csv.clicked.connect(self.export_csv_requested)
        self._export_html = QPushButton(translator.tr("radiomics.results.export_html"))
        self._export_html.clicked.connect(self.export_html_requested)
        self._status = QLabel(translator.tr("status.ready"))
        self._stale_banner = QLabel(translator.tr("radiomics.results.stale"))
        self._stale_banner.setObjectName("radiomicsStaleBanner")
        self._stale_banner.setWordWrap(True)
        self._stale_banner.setVisible(False)
        self._extract_summary = QLabel()
        self._extract_summary.setObjectName("workflowSummaryCard")
        self._extract_summary.setWordWrap(True)
        self._extract_title = QLabel()
        self._extract_title.setObjectName("panelSectionTitle")
        self._extract_description = QLabel()
        self._extract_description.setObjectName("panelSectionHint")
        self._extract_description.setWordWrap(True)
        extract_page = QWidget()
        extract_layout = QVBoxLayout(extract_page)
        extract_layout.setContentsMargins(28, 24, 28, 24)
        extract_layout.addWidget(self._extract_title)
        extract_layout.addWidget(self._extract_description)
        extract_layout.addWidget(self._extract_summary)
        extract_layout.addWidget(self._status)
        extract_layout.addStretch(1)
        extract_layout.addWidget(self._extract)

        results_page = QWidget()
        results_layout = QVBoxLayout(results_page)
        results_layout.setContentsMargins(12, 12, 12, 12)
        results_layout.addWidget(self._stale_banner)
        results_layout.addWidget(self._tabs, 1)

        self._export_summary = QLabel()
        self._export_summary.setObjectName("workflowSummaryCard")
        self._export_summary.setWordWrap(True)
        self._export_title = QLabel()
        self._export_title.setObjectName("panelSectionTitle")
        self._export_description = QLabel()
        self._export_description.setObjectName("panelSectionHint")
        self._export_description.setWordWrap(True)
        export_buttons = QHBoxLayout()
        export_buttons.addWidget(self._export_excel)
        export_buttons.addWidget(self._export_csv)
        export_buttons.addWidget(self._export_html)
        export_buttons.addStretch(1)
        export_page = QWidget()
        export_layout = QVBoxLayout(export_page)
        export_layout.setContentsMargins(28, 24, 28, 24)
        export_layout.addWidget(self._export_title)
        export_layout.addWidget(self._export_description)
        export_layout.addWidget(self._export_summary)
        export_layout.addStretch(1)
        export_layout.addLayout(export_buttons)

        self._workflow_stack = QStackedWidget()
        self._workflow_stack.addWidget(extract_page)
        self._workflow_stack.addWidget(results_page)
        self._workflow_stack.addWidget(export_page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._workflow_stack)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def set_context_summary(
        self,
        *,
        reference_loaded: bool,
        comparison_loaded: bool,
        roi_count: int,
        profile_name: str,
        result_rows: int,
    ) -> None:
        """Describe inputs/outputs without bringing diagnostic images into task pages."""
        ready_images = int(reference_loaded) + int(comparison_loaded)
        self._last_context = (
            reference_loaded,
            comparison_loaded,
            roi_count,
            profile_name,
            result_rows,
        )
        self._extract_summary.setText(
            translator.tr(
                "radiomics.extract.summary",
                image_count=ready_images,
                roi_count=roi_count,
                profile=profile_name,
            )
        )
        self._export_summary.setText(
            translator.tr("radiomics.export.summary", row_count=result_rows)
        )

    def retranslate_ui(self) -> None:
        self._extract_title.setText(translator.tr("radiomics.extract.title"))
        self._extract_description.setText(translator.tr("radiomics.extract.description"))
        self._export_title.setText(translator.tr("radiomics.export.title"))
        self._export_description.setText(translator.tr("radiomics.export.description"))
        self._extract.setText(translator.tr("radiomics.results.extract"))
        self._export_excel.setText(translator.tr("radiomics.results.export_excel"))
        self._export_csv.setText(translator.tr("radiomics.results.export_csv"))
        self._export_html.setText(translator.tr("radiomics.results.export_html"))
        tab_keys = (
            "features",
            "delta",
            "repeatability",
            "correlation",
            "provenance",
            "mask_qc",
            "feature_qc",
            "outcomes",
        )
        for index, key in enumerate(tab_keys):
            self._tabs.setTabText(index, translator.tr(f"radiomics.results.{key}"))
        reference, comparison, roi_count, profile, result_rows = self._last_context
        self.set_context_summary(
            reference_loaded=reference,
            comparison_loaded=comparison,
            roi_count=roi_count,
            profile_name=profile,
            result_rows=result_rows,
        )

    def set_results(
        self,
        features: pd.DataFrame,
        delta: pd.DataFrame,
        repeatability: pd.DataFrame | None = None,
        correlation: pd.DataFrame | None = None,
        provenance: pd.DataFrame | None = None,
        mask_qc: pd.DataFrame | None = None,
        feature_qc: pd.DataFrame | None = None,
        outcomes: pd.DataFrame | None = None,
    ) -> None:
        self._features.set_dataframe(features)
        self._delta.set_dataframe(delta)
        self._repeatability.set_dataframe(
            pd.DataFrame() if repeatability is None else repeatability
        )
        self._correlation.set_dataframe(pd.DataFrame() if correlation is None else correlation)
        self._provenance.set_dataframe(pd.DataFrame() if provenance is None else provenance)
        self._mask_qc.set_dataframe(pd.DataFrame() if mask_qc is None else mask_qc)
        self._feature_qc.set_dataframe(pd.DataFrame() if feature_qc is None else feature_qc)
        self._outcomes.set_dataframe(pd.DataFrame() if outcomes is None else outcomes)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_results_stale(self, stale: bool) -> None:
        """Report that the displayed tables no longer match the current
        images/configuration/masks.  The numbers stay on screen - deleting
        the user's results behind their back is what this replaces - but
        they are labelled outdated and cannot be exported until the
        extraction is run again."""
        self._stale = stale
        self._stale_banner.setVisible(stale)
        self._refresh_actions()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        self._extract.setEnabled(not self._busy)
        exportable = not self._busy and not self._stale
        self._export_excel.setEnabled(exportable)
        self._export_csv.setEnabled(exportable)
        self._export_html.setEnabled(exportable)

    def set_workflow_stage(self, stage: str) -> None:
        """Each task owns a clean page: setup, tables, or export choices."""
        page_by_stage = {"extract": 0, "results": 1, "export": 2}
        if stage in page_by_stage:
            self._workflow_stack.setCurrentIndex(page_by_stage[stage])
        self._extract.setVisible(stage == "extract")
        exporting = stage == "export"
        self._export_excel.setVisible(exporting)
        self._export_csv.setVisible(exporting)
        self._export_html.setVisible(exporting)


__all__ = [
    "DataFrameTable",
    "RadiomicsConfigPanel",
    "RadiomicsResultsPanel",
    "RadiomicsSegmentationPanel",
]
