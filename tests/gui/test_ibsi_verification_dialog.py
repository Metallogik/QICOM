from __future__ import annotations

import numpy as np
import pandas as pd
from pytestqt.qtbot import QtBot

from qicom.gui.radiomics_main_window import RadiomicsMainWindow
from qicom.gui.widgets import ibsi_verification_dialog as dialog_module
from qicom.gui.widgets.ibsi_verification_dialog import IbsiVerificationDialog
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.ibsi_validation import (
    IbsiReferenceSet,
    IbsiVerificationResult,
)
from qicom.radiomics.models import ImageGeometry, RadiomicsImage


def _payload() -> tuple[IbsiReferenceSet, IbsiVerificationResult]:
    image = RadiomicsImage(
        np.arange(80, dtype=np.float32).reshape(4, 4, 5),
        ImageGeometry((5, 4, 4), (2.0, 2.0, 2.0), (0.0, 0.0, 0.0), tuple(np.eye(3).ravel())),
        "array",
    )
    reference_set = IbsiReferenceSet(
        manifest={
            "reference_set_id": "test",
            "reference_set_version": "1",
            "pipeline_components": ["formula"],
            "scope": "test",
        },
        manifest_hash="a" * 64,
        resource_paths={},
        resource_hashes={},
        image=image,
        mask=np.ones(image.pixels.shape, dtype=bool),
        config=RadiomicsConfig(minimum_roi_voxels=2),
        features=(),
    )
    table = pd.DataFrame(
        [
            {
                "feature_key": "original_firstorder_Mean",
                "official_tag": "stat_mean",
                "image_type": "original",
                "feature_class": "firstorder",
                "feature_name": "Mean",
                "official_family": "Statistics",
                "official_feature_name": "Mean",
                "unit": "intensity",
                "qicom_value": 2.15,
                "ibsi_reference_value": 2.15,
                "absolute_error": 0.0,
                "relative_error": 0.0,
                "comparison_error": 0.0,
                "absolute_tolerance": 0.0,
                "tolerance_applied": "test",
                "status": "PASS",
                "reason": "Within tolerance",
                "backend_version": "test",
                "profile_hash": "a" * 64,
                "validation_status": "reference-verified",
            }
        ]
    )
    result = IbsiVerificationResult(
        table,
        {"overall_status": "PASS", "passed": 1, "failed": 0, "not_tested": 0},
        reference_set.manifest,
        {
            "non_certification_statement": "Not certification.",
            "limitations": "test",
        },
    )
    return reference_set, result


def test_dialog_configuration_is_locked_and_results_are_filterable(qtbot: QtBot) -> None:
    dialog = IbsiVerificationDialog()
    qtbot.addWidget(dialog)

    assert dialog.configuration_locked
    dialog._verification_finished(_payload())

    assert dialog._table.rowCount() == 1
    assert dialog._summary.text()
    assert dialog._export.isEnabled()


def test_gui_and_ci_entry_point_share_the_same_loader_and_engine(
    monkeypatch,
) -> None:
    payload = _payload()
    calls: list[str] = []
    monkeypatch.setattr(
        dialog_module,
        "load_ibsi_reference_set",
        lambda **_kwargs: calls.append("load") or payload[0],
    )
    monkeypatch.setattr(
        dialog_module,
        "run_ibsi_verification",
        lambda _reference: calls.append("run") or payload[1],
    )

    returned = dialog_module._load_and_run(consent=False)

    assert returned == payload
    assert calls == ["load", "run"]


def test_radiomics_images_page_exposes_distinct_ibsi_action(qtbot: QtBot) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)

    assert window._verify_ibsi_action.text()
    assert window._ibsi_verify_button.text() == window._verify_ibsi_action.text()
    assert not window._ibsi_verify_button.isHidden()
