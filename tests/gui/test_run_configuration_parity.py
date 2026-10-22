"""The same calculation carries the same configuration hash whichever way it ran.

The GUI added the NSS reference model and the declared intensity units, the
batch only the units it was told were HU, and the batch and the CLI added the
per-role data ranges even with no metric to read them: one calculation had a
different receipt hash in each entry point.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import pandas as pd
import pytest

from qicom.gui.calculation_service import CalculationContext
from qicom.io.loader import load_image_with_metadata
from qicom.metrics.catalog import catalog
from qicom.metrics.profiles import profile_for_id
from qicom.provenance import configuration_hash


class _Captured(Exception):
    def __init__(self, configuration: dict[str, Any]) -> None:
        super().__init__("captured")
        self.configuration = configuration


def _capture(*_args: object, config: dict[str, Any], **_kwargs: object) -> pd.DataFrame:
    raise _Captured(dict(config))


@pytest.fixture
def images(tmp_path: Path) -> tuple[Path, Path]:
    paths = []
    for name, offset in (("reference", 0.0), ("comparison", 5.0)):
        pixels = np.linspace(-1000, 1000, 3 * 32 * 32, dtype=np.float32).reshape(3, 32, 32)
        path = tmp_path / f"{name}.nii.gz"
        nib.save(nib.Nifti1Image(pixels + offset, np.eye(4)), path)
        paths.append(path)
    return paths[0], paths[1]


def _gui_configuration(
    reference: Path, comparison: Path, requested: frozenset[str], profile: tuple[str, str]
) -> dict[str, Any]:
    loaded = [load_image_with_metadata(path) for path in (reference, comparison)]
    context = CalculationContext(
        data_range=2000.0,
        roi_masks=None,
        roi_regions=None,
        selected_metric_ids=requested,
        tissue_mask=None,
        profile_id=profile[0],
        profile_version=profile[1],
        hounsfield_units=(True, True),
        role_data_ranges=(loaded[0].data_range, loaded[1].data_range),
        slice_orientations=(loaded[0].slice_orientation, loaded[1].slice_orientation),
    )
    with pytest.raises(_Captured) as captured:
        context.run(loaded[0].pixels, loaded[1].pixels, runner=_capture)
    return captured.value.configuration


def test_the_gui_and_a_batch_job_hash_the_same_calculation_alike(
    images: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qicom import batch

    profile = profile_for_id("complete")
    captured: list[dict[str, Any]] = []

    def capture(*_args: object, config: dict[str, Any], **_kwargs: object) -> pd.DataFrame:
        captured.append(dict(config))
        raise RuntimeError("captured")

    monkeypatch.setattr(batch, "run_all_metrics", capture)
    batch.run_quantitative_batch(
        {
            "output_directory": str(tmp_path / "out"),
            "profile": "complete",
            "jobs": [
                {
                    "reference": str(images[0]),
                    "comparison": str(images[1]),
                    "data_range": 2000.0,
                    "hounsfield_units": True,
                }
            ],
        }
    )

    gui = _gui_configuration(*images, profile.metric_ids, (profile.profile_id, profile.version))
    assert configuration_hash(captured[0]) == configuration_hash(gui)


def test_the_gui_and_the_cli_hash_the_same_calculation_alike(
    images: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from qicom.cli import main as cli_main

    captured: list[dict[str, Any]] = []

    def capture(*_args: object, config: dict[str, Any], **_kwargs: object) -> pd.DataFrame:
        captured.append(dict(config))
        raise ValueError("captured")

    monkeypatch.setattr(cli_main, "run_all_metrics", capture)
    args = argparse.Namespace(
        reference=images[0],
        comparison=images[1],
        data_range=2000.0,
        hounsfield_units=True,
        output=None,
    )
    assert cli_main._run_confronta(args) == 1

    every_metric = frozenset(catalog.all_descriptors())
    gui = _gui_configuration(*images, every_metric, ("custom", "1.0"))
    assert configuration_hash(captured[0]) == configuration_hash(gui)


def test_keys_follow_the_metrics_that_read_them() -> None:
    from qicom.metrics.run_configuration import quantitative_configuration

    plain = quantitative_configuration(
        255.0,
        requested=frozenset({"psnr"}),
        role_data_ranges=(255.0, 255.0),
        hounsfield_units=(True, True),
        slice_orientations=("axial", "axial"),
    )

    assert plain == {"data_range": 255.0}


def test_a_run_without_nss_metrics_never_reads_the_nss_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model is read from a disk cache: an unreadable cache used to stop
    a calculation that asked for MSE alone."""
    from qicom.gui import calculation_service
    from qicom.gui.nss_model_settings import NssModelSettings

    class Unreadable(NssModelSettings):
        @property
        def model(self):  # type: ignore[no-untyped-def,override]
            raise PermissionError("cache directory not accessible")

    monkeypatch.setattr(calculation_service, "nss_model_settings", Unreadable())
    context = CalculationContext(
        data_range=255.0,
        roi_masks=None,
        roi_regions=None,
        selected_metric_ids=frozenset({"mse"}),
        tissue_mask=None,
    )

    with pytest.raises(_Captured) as captured:
        context.run(np.zeros((8, 8)), np.ones((8, 8)), runner=_capture)
    assert "reference_model" not in captured.value.configuration

    nss = CalculationContext(
        data_range=255.0,
        roi_masks=None,
        roi_regions=None,
        selected_metric_ids=frozenset({"niqe"}),
        tissue_mask=None,
    )
    with pytest.raises(PermissionError):
        nss.run(np.zeros((8, 8)), np.ones((8, 8)), runner=_capture)
