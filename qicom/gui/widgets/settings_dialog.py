"""Application-wide persistent Settings dialog."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.demo_bundle import create_demo_bundle_interactive
from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.number_format import NOTATIONS, number_format_settings
from qicom.gui.preferences import (
    MAX_RADIOMICS_CACHE_MIB,
    preferences_store,
    remember_language,
    remember_number_notation,
    remember_theme,
)
from qicom.gui.theming import THEME_CHOICES, theme_manager
from qicom.gui.widgets.adaptive_layout import ScreenFitDialog, make_panel_scrollable
from qicom.gui.widgets.metric_selection_panel import MetricSelectionPanel
from qicom.gui.widgets.nss_model_dialog import NssModelDialog
from qicom.i18n import SUPPORTED_LANGUAGES, translator
from qicom.metrics.reference_structural import set_keep_learned_models
from qicom.runtime import detect_runtime_profile


class SettingsDialog(ScreenFitDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.resize(720, 640)

        self._tabs = QTabWidget()
        self._general_tab = QWidget()
        self._metrics_tab = QWidget()
        self._identity_tab = QWidget()
        self._diagnostics_tab = QWidget()
        self._tabs.addTab(self._general_tab, "")
        self._tabs.addTab(self._metrics_tab, "")
        self._tabs.addTab(self._identity_tab, "")
        self._tabs.addTab(self._diagnostics_tab, "")

        self._language_combo = QComboBox()
        for language in SUPPORTED_LANGUAGES:
            self._language_combo.addItem("", language)
        self._language_combo.currentIndexChanged.connect(self._on_language_changed)

        self._theme_combo = QComboBox()
        for theme in THEME_CHOICES:
            self._theme_combo.addItem("", theme)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)

        self._notation_combo = QComboBox()
        for notation in NOTATIONS:
            self._notation_combo.addItem("", notation)
        self._notation_combo.currentIndexChanged.connect(self._on_notation_changed)

        self._precision_combo = QComboBox()
        self._precision_combo.addItem("", None)
        for precision in range(2, 9):
            self._precision_combo.addItem(str(precision), precision)
        self._precision_combo.currentIndexChanged.connect(self._on_precision_changed)

        self._language_label = QLabel()
        self._theme_label = QLabel()
        self._notation_label = QLabel()
        self._precision_label = QLabel()
        general_form = QFormLayout(self._general_tab)
        general_form.addRow(self._language_label, self._language_combo)
        general_form.addRow(self._theme_label, self._theme_combo)
        general_form.addRow(self._notation_label, self._notation_combo)
        general_form.addRow(self._precision_label, self._precision_combo)

        self._metrics_description = QLabel()
        self._metrics_description.setWordWrap(True)
        self.metric_panel = MetricSelectionPanel(metric_selection_settings.default_enabled_ids())
        self.metric_panel.selection_changed.connect(self._on_default_metrics_changed)
        self._keep_learned_models_label = QLabel()
        self._keep_learned_models = QComboBox()
        for value in (None, True, False):
            self._keep_learned_models.addItem("", value)
        self._keep_learned_models.currentIndexChanged.connect(self._on_keep_learned_models_changed)
        self._nss_model_button = QPushButton()
        self._nss_model_button.clicked.connect(lambda: NssModelDialog(self).exec())
        self._radiomics_cache_mib = QSpinBox()
        self._radiomics_cache_mib.setRange(-1, MAX_RADIOMICS_CACHE_MIB)
        self._radiomics_cache_mib.setSuffix(" MiB")
        self._radiomics_cache_mib.valueChanged.connect(self._on_radiomics_cache_changed)
        self._radiomics_cache_label = QLabel()
        self._pair_workers_label = QLabel()
        self._pair_workers = QSpinBox()
        self._pair_workers.setRange(0, 2)
        self._pair_workers.valueChanged.connect(self._on_pair_workers_changed)
        metrics_layout = QVBoxLayout(self._metrics_tab)
        metrics_layout.addWidget(self._metrics_description)
        metrics_layout.addWidget(self.metric_panel, 1)
        metrics_layout.addWidget(self._nss_model_button)
        radiomics_cache_row = QFormLayout()
        radiomics_cache_row.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        radiomics_cache_row.addRow(self._keep_learned_models_label, self._keep_learned_models)
        radiomics_cache_row.addRow(self._radiomics_cache_label, self._radiomics_cache_mib)
        radiomics_cache_row.addRow(self._pair_workers_label, self._pair_workers)
        metrics_layout.addLayout(radiomics_cache_row)

        self._identity_group = QGroupBox()
        self._name_label = QLabel()
        self._name_edit = QLineEdit()
        self._name_edit.setMaxLength(200)
        self._name_edit.editingFinished.connect(self._on_name_changed)
        identity_form = QFormLayout(self._identity_group)
        identity_form.addRow(self._name_label, self._name_edit)
        self._identity_description = QLabel()
        self._identity_description.setWordWrap(True)
        identity_layout = QVBoxLayout(self._identity_tab)
        identity_layout.addWidget(self._identity_description)
        identity_layout.addWidget(self._identity_group)
        identity_layout.addStretch(1)

        # Diagnostics: the synthetic demo bundle used to be a button on the
        # launcher's front page, where it competed for attention with the
        # three workflows and nothing downstream ever consumed it.  It
        # belongs here instead - it is a support/reproduction tool, not a
        # way into the app - and Help > Report a Bug offers the same action
        # at the moment it is actually needed (see qicom.gui.demo_bundle).
        self._diagnostics_description = QLabel()
        self._diagnostics_description.setWordWrap(True)
        self._demo_button = QPushButton()
        self._demo_button.clicked.connect(lambda: create_demo_bundle_interactive(self))
        self._demo_hint = QLabel()
        self._demo_hint.setObjectName("panelSectionHint")
        self._demo_hint.setWordWrap(True)
        diagnostics_layout = QVBoxLayout(self._diagnostics_tab)
        diagnostics_layout.addWidget(self._diagnostics_description)
        diagnostics_layout.addWidget(self._demo_button)
        diagnostics_layout.addWidget(self._demo_hint)
        diagnostics_layout.addStretch(1)

        self._reset_button = QPushButton()
        self._reset_button.clicked.connect(self._reset_all)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        self._buttons = buttons
        buttons.addButton(self._reset_button, QDialogButtonBox.ButtonRole.ResetRole)

        self._saved_hint = QLabel()
        self._saved_hint.setObjectName("panelSectionHint")
        self._saved_hint.setAlignment(Qt.AlignmentFlag.AlignRight)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs, 1)
        layout.addWidget(self._saved_hint)
        layout.addWidget(buttons)

        make_panel_scrollable(self)

        self._populate_from_preferences()
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _populate_from_preferences(self) -> None:
        prefs = preferences_store.preferences
        widgets = (
            self._language_combo,
            self._theme_combo,
            self._notation_combo,
            self._precision_combo,
            self._name_edit,
            self._keep_learned_models,
            self._radiomics_cache_mib,
            self._pair_workers,
        )
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self._set_combo_data(self._language_combo, translator.language)
            self._set_combo_data(self._theme_combo, theme_manager.selection)
            self._set_combo_data(self._notation_combo, number_format_settings.notation)
            self._set_combo_data(self._precision_combo, number_format_settings.precision)
            self._name_edit.setText(prefs.evaluator_name)
            self._set_combo_data(self._keep_learned_models, prefs.keep_learned_models)
            self._radiomics_cache_mib.setValue(
                -1 if prefs.radiomics_cache_mib is None else prefs.radiomics_cache_mib
            )
            self._pair_workers.setValue(0 if prefs.pair_workers is None else prefs.pair_workers)
            self.metric_panel.set_selected_ids(metric_selection_settings.default_enabled_ids())
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def _on_language_changed(self) -> None:
        language = self._language_combo.currentData()
        if isinstance(language, str):
            remember_language(language)

    def _on_theme_changed(self) -> None:
        theme = self._theme_combo.currentData()
        app = QApplication.instance()
        if isinstance(theme, str) and isinstance(app, QApplication):
            remember_theme(app, theme)

    def _on_notation_changed(self) -> None:
        notation = self._notation_combo.currentData()
        if isinstance(notation, str):
            remember_number_notation(notation)

    def _on_precision_changed(self) -> None:
        precision = self._precision_combo.currentData()
        if precision is None or isinstance(precision, int):
            number_format_settings.set_precision(precision)
            preferences_store.update(number_precision=precision)

    def _on_default_metrics_changed(self) -> None:
        selected = self.metric_panel.selected_ids()
        metric_selection_settings.set_default_enabled_ids(selected)
        preferences_store.update(default_metric_ids=tuple(sorted(selected)))

    def _on_keep_learned_models_changed(self) -> None:
        prefs = preferences_store.update(
            keep_learned_models=self._keep_learned_models.currentData()
        )
        set_keep_learned_models(prefs.resolved_resources().keep_learned_models)

    def _on_radiomics_cache_changed(self, value: int) -> None:
        preferences_store.update(radiomics_cache_mib=None if value == -1 else value)

    def _on_pair_workers_changed(self, value: int) -> None:
        preferences_store.update(pair_workers=None if value == 0 else value)

    def _on_name_changed(self) -> None:
        preferences_store.update(evaluator_name=self._name_edit.text().strip())

    def _reset_all(self) -> None:
        app = QApplication.instance()
        preferences_store.reset(app if isinstance(app, QApplication) else None)
        self._populate_from_preferences()
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("settings.title"))
        self._tabs.setTabText(0, translator.tr("settings.general"))
        self._tabs.setTabText(1, translator.tr("settings.metrics"))
        self._tabs.setTabText(2, translator.tr("settings.identity"))
        self._tabs.setTabText(3, translator.tr("settings.diagnostics"))
        self._language_label.setText(translator.tr("settings.language"))
        self._theme_label.setText(translator.tr("settings.theme"))
        self._notation_label.setText(translator.tr("settings.number_notation"))
        self._precision_label.setText(translator.tr("settings.number_precision"))
        for index in range(self._language_combo.count()):
            language = str(self._language_combo.itemData(index))
            self._language_combo.setItemText(index, translator.tr(f"language.{language}"))
        for index in range(self._theme_combo.count()):
            theme = str(self._theme_combo.itemData(index))
            self._theme_combo.setItemText(index, translator.tr(f"settings.theme_{theme}"))
        for index in range(self._notation_combo.count()):
            notation = str(self._notation_combo.itemData(index))
            self._notation_combo.setItemText(
                index, translator.tr(f"menu.view.number_format_{notation}")
            )
        self._precision_combo.setItemText(0, translator.tr("settings.precision_auto"))
        self._metrics_description.setText(translator.tr("settings.metrics_description"))
        self._keep_learned_models_label.setText(translator.tr("settings.keep_learned_models"))
        self._keep_learned_models.setToolTip(translator.tr("settings.keep_learned_models_tooltip"))
        resources = detect_runtime_profile().resources
        keep_label = translator.tr("settings.learned_keep")
        release_label = translator.tr("settings.learned_release")
        self._keep_learned_models.setItemText(
            0,
            translator.tr(
                "settings.automatic_value",
                value=(keep_label if resources.keep_learned_models else release_label),
            ),
        )
        self._keep_learned_models.setItemText(1, keep_label)
        self._keep_learned_models.setItemText(2, release_label)
        self._radiomics_cache_mib.setSpecialValueText(
            translator.tr("settings.automatic_value", value=f"{resources.radiomics_cache_mib} MiB")
        )
        self._pair_workers.setSpecialValueText(
            translator.tr("settings.automatic_value", value=str(resources.pair_workers))
        )
        self._pair_workers_label.setText(translator.tr("settings.pair_workers"))
        self._pair_workers.setToolTip(translator.tr("settings.pair_workers_tooltip"))
        self._nss_model_button.setText(translator.tr("settings.nss_model"))
        self._nss_model_button.setToolTip(translator.tr("settings.nss_model_tooltip"))
        self._radiomics_cache_label.setText(translator.tr("settings.radiomics_cache"))
        self._radiomics_cache_mib.setToolTip(translator.tr("settings.radiomics_cache_tooltip"))
        self._identity_group.setTitle(translator.tr("settings.identity"))
        self._name_label.setText(translator.tr("settings.evaluator_name"))
        self._identity_description.setText(translator.tr("settings.identity_description"))
        self._name_edit.setPlaceholderText(translator.tr("settings.evaluator_name_placeholder"))
        self._diagnostics_description.setText(translator.tr("demo.description"))
        self._demo_button.setText(translator.tr("demo.create"))
        self._demo_button.setToolTip(translator.tr("demo.tooltip"))
        self._demo_hint.setText(translator.tr("demo.hint"))
        self._saved_hint.setText(translator.tr("settings.saved_automatically"))
        self._reset_button.setText(translator.tr("settings.reset"))
        close_button = self._buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.setText(translator.tr("settings.close"))


__all__ = ["SettingsDialog"]
