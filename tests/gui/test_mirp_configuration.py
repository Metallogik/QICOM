"""Keep the extended backend selectable and prevent reference presets hiding user edits."""

import dataclasses

from PySide6.QtWidgets import QScrollArea
from pytestqt.qtbot import QtBot

from qicom.gui.widgets.radiomics_panels import RadiomicsConfigPanel
from qicom.radiomics.config import MIRP_ADDITIONAL_CLASSES, RadiomicsConfig


def test_backend_switch_restricts_filters_and_feature_families(qtbot: QtBot) -> None:
    panel = RadiomicsConfigPanel()
    qtbot.addWidget(panel)
    panel._wavelet.setChecked(True)
    panel._backend.setCurrentIndex(panel._backend.findData("mirp"))
    assert panel.config().backend == "mirp"
    assert not panel.config().enable_wavelet
    assert panel.config().enable_original
    assert not panel._original.isEnabled()
    assert not panel._wavelet.isEnabled()
    panel._feature_checks["gldzm"].setChecked(True)
    assert "gldzm" in panel.config().feature_classes
    panel._backend.setCurrentIndex(panel._backend.findData("pyradiomics"))
    assert not (set(panel.config().feature_classes) & set(MIRP_ADDITIONAL_CLASSES))
    assert panel._wavelet.isEnabled()


def test_editing_loaded_reference_preset_makes_a_custom_profile(qtbot: QtBot) -> None:
    panel = RadiomicsConfigPanel()
    qtbot.addWidget(panel)
    preset = dataclasses.replace(RadiomicsConfig(), backend="mirp", ibsi_preset="ct-a")
    panel.set_config(preset)
    assert panel.config().ibsi_preset == "ct-a"
    panel._bin_width.setValue(17)
    result = panel.config()
    assert result.ibsi_preset == "none"
    assert result.bin_width == 17
    assert result.profile_id == "qicom-custom-mirp"


def test_configuration_is_scrollable_at_desktop_height(qtbot: QtBot) -> None:
    panel = RadiomicsConfigPanel()
    qtbot.addWidget(panel)
    panel.resize(480, 640)
    panel.show()
    scroll = panel.findChild(QScrollArea)
    assert scroll is not None
    assert panel.height() == 640
    assert scroll.verticalScrollBar().maximum() > 0
