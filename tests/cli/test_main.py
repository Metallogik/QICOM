"""Tests for qicom.cli.main (`qicom-cli confronta` and `qicom-cli demo`)."""

from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pandas as pd
import pytest
from PIL import Image

from qicom.cli.main import main


def _save_png(path: Path, seed: int) -> None:
    # 176x176 satisfies MS-SSIM (>=161x161) and NIQE (>=96x96) size constraints.
    data = np.random.default_rng(seed).integers(0, 256, size=(176, 176), dtype=np.uint8)
    Image.fromarray(data).save(path)


@pytest.fixture(scope="module")
def image_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp_path = tmp_path_factory.mktemp("cli_images")
    reference = tmp_path / "reference.png"
    comparison = tmp_path / "comparison.png"
    _save_png(reference, seed=0)
    _save_png(comparison, seed=1)
    return reference, comparison


def test_confronta_prints_results_table(
    image_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reference, comparison = image_paths
    monkeypatch.setattr("sys.argv", ["qicom-cli", "confronta", str(reference), str(comparison)])
    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "not given, inferred 255.0" in captured.err
    assert "mse" in captured.out
    assert "psnr" in captured.out


def test_confronta_with_explicit_data_range_skips_inference_message(
    image_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reference, comparison = image_paths
    monkeypatch.setattr(
        "sys.argv",
        ["qicom-cli", "confronta", str(reference), str(comparison), "--data-range", "255"],
    )
    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "inferred" not in captured.err


def test_confronta_writes_csv_output(
    image_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reference, comparison = image_paths
    output = tmp_path / "results.csv"
    monkeypatch.setattr(
        "sys.argv",
        ["qicom-cli", "confronta", str(reference), str(comparison), "--output", str(output)],
    )
    exit_code = main()

    assert exit_code == 0
    assert output.exists()
    df = pd.read_csv(output)
    assert "metric_id" in df.columns
    assert (df["metric_id"] == "mse").any()


def test_confronta_notes_the_confidence_interval_caveat_for_a_volume(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A CSV has nowhere to carry the caveat the GUI shows under its
    results table, so the CLI prints it to stderr instead - see
    qicom.metrics.statistics."""
    reference = tmp_path / "reference.nii.gz"
    comparison = tmp_path / "comparison.nii.gz"
    for path, seed in ((reference, 0), (comparison, 1)):
        data = np.random.default_rng(seed).random((8, 176, 176)).astype(np.float32)
        nib.save(nib.Nifti1Image(data, affine=np.eye(4)), path)
    monkeypatch.setattr("sys.argv", ["qicom-cli", "confronta", str(reference), str(comparison)])

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "not independent observations" in captured.err
    assert "ci_lower" in captured.out


def test_confronta_omits_the_confidence_interval_caveat_for_2d_images(
    image_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reference, comparison = image_paths
    monkeypatch.setattr("sys.argv", ["qicom-cli", "confronta", str(reference), str(comparison)])

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "not independent observations" not in captured.err


def test_confronta_missing_file_reports_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "missing.png"
    other = tmp_path / "other.png"
    _save_png(other, seed=2)
    monkeypatch.setattr("sys.argv", ["qicom-cli", "confronta", str(missing), str(other)])
    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error" in captured.err.lower()


def test_confronta_unsupported_extension_reports_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = tmp_path / "data.txt"
    bad.write_text("not an image")
    other = tmp_path / "other2.png"
    _save_png(other, seed=3)
    monkeypatch.setattr("sys.argv", ["qicom-cli", "confronta", str(bad), str(other)])
    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error" in captured.err.lower()


def test_confronta_reports_an_unwritable_output_path_without_a_traceback(
    image_paths: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The metrics succeeded; only the write failed. That has to read as a
    diagnostic line and a non-zero exit, not as a crash."""
    reference, comparison = image_paths
    unwritable = tmp_path / "missing-directory" / "results.csv"
    monkeypatch.setattr(
        "sys.argv",
        [
            "qicom-cli",
            "confronta",
            str(reference),
            str(comparison),
            "--data-range",
            "255",
            "--output",
            str(unwritable),
        ],
    )

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "could not write" in captured.err
    assert not unwritable.exists()


class TestDemoCommand:
    """The CLI half of Settings > Diagnostics: whoever receives a bug
    report regenerates the bundle here and diffs the manifest."""

    def test_writes_a_phi_free_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        destination = tmp_path / "bundle"
        monkeypatch.setattr("sys.argv", ["qicom-cli", "demo", str(destination)])

        assert main() == 0

        manifest = json.loads((destination / "qicom_demo.json").read_text(encoding="utf-8"))
        assert manifest["contains_phi"] is False
        assert (destination / "demo_report.html").is_file()

    def test_refuses_an_existing_destination_without_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        destination = tmp_path / "already-there"
        destination.mkdir()
        monkeypatch.setattr("sys.argv", ["qicom-cli", "demo", str(destination)])

        assert main() == 1
        assert "already exists" in capsys.readouterr().err


def _save_riqe_pair(tmp_path: Path) -> tuple[Path, Path]:
    fixture = Path(__file__).parents[1] / "fixtures" / "riqe" / "inputs.npz"
    paths = []
    with np.load(fixture, allow_pickle=False) as data:
        for name in ("full", "low"):
            path = tmp_path / f"{name}.nii.gz"
            nib.save(nib.Nifti1Image(data[name].astype(np.float32), affine=np.eye(4)), path)
            paths.append(path)
    return paths[0], paths[1]


@pytest.mark.parametrize("declared", [True, False])
def test_confronta_computes_riqe_only_on_declared_hounsfield_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    from qicom.io.loader import load_image_with_metadata
    from qicom.metrics.riqe import riqe

    reference, comparison = _save_riqe_pair(tmp_path)
    output = tmp_path / "results.csv"
    argv = ["qicom-cli", "confronta", str(reference), str(comparison), "--output", str(output)]
    monkeypatch.setattr("sys.argv", argv + (["--hounsfield-units"] if declared else []))

    assert main() == 0

    rows = pd.read_csv(output).query("metric_id == 'riqe'").set_index("image")["value"]
    if not declared:
        assert rows.empty
        return
    full = load_image_with_metadata(reference).pixels
    low = load_image_with_metadata(comparison).pixels
    assert rows["reference"] == pytest.approx(riqe(full, "HU"), rel=1e-12)
    expected = riqe(low, "HU", domain_image=full, domain_unit="HU")
    assert rows["comparison"] == pytest.approx(expected, rel=1e-12)


def test_the_default_data_range_is_the_guis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUI, CLI and batch used three copies of the default-range rule, and
    the CLI's lacked the GUI's floor of 1: two float images in [0, 0.5] got
    PSNR values 6 dB apart depending on the entry point."""
    import nibabel as nib

    from qicom.io.loader import combined_default_data_range, load_image_with_metadata

    rng = np.random.default_rng(4)
    paths = []
    for name in ("ref.nii", "cmp.nii"):
        path = tmp_path / name
        pixels = (rng.random((16, 16)) * 0.5).astype(np.float32)
        nib.save(nib.Nifti1Image(pixels, np.eye(4)), str(path))
        paths.append(path)
    expected = combined_default_data_range([load_image_with_metadata(path) for path in paths])

    monkeypatch.setattr("sys.argv", ["qicom-cli", "confronta", str(paths[0]), str(paths[1])])
    assert main() == 0

    assert f"inferred {expected!r}" in capsys.readouterr().err
    assert expected == 1.0


def test_confronta_passes_the_loader_orientation_to_riqe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RIQE refuses a stack its loader read as non-axial - in the GUI. The CLI
    used to drop the orientation and score a sagittal NIfTI as axial CT."""
    from qicom.cli import main as cli_main
    from qicom.metrics.runner import run_all_metrics

    fixture = Path(__file__).parents[1] / "fixtures" / "riqe" / "inputs.npz"
    with np.load(fixture, allow_pickle=False) as data:
        stack = np.repeat(data["full"].astype(np.float32)[None], 3, axis=0)
    path = tmp_path / "sagittal.nii.gz"
    nib.save(nib.Nifti1Image(stack, affine=np.eye(4)), path)
    output = tmp_path / "results.csv"

    def riqe_only(*args: object, **kwargs: object) -> pd.DataFrame:
        return run_all_metrics(*args, **kwargs, selected_metric_ids={"riqe"})  # type: ignore[arg-type]

    monkeypatch.setattr(cli_main, "run_all_metrics", riqe_only)
    monkeypatch.setattr(
        "sys.argv",
        [
            "qicom-cli",
            "confronta",
            str(path),
            str(path),
            "--hounsfield-units",
            "--output",
            str(output),
        ],
    )

    assert main() == 0
    results = pd.read_csv(output)
    assert results.query("metric_id == 'riqe'").empty
