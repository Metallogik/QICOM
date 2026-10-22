from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from qicom.demo import DEMO_SEED, create_demo_bundle
from qicom.radiomics.project import load_radiomics_project


def test_demo_bundle_spans_three_modules_is_phi_free_and_reopenable(tmp_path: Path) -> None:
    destination = create_demo_bundle(tmp_path / "demo")
    manifest = json.loads((destination / "qicom_demo.json").read_text(encoding="utf-8"))

    assert manifest["seed"] == DEMO_SEED
    assert manifest["contains_phi"] is False
    assert set(manifest["scope"]) == {"quantitative", "qualitative", "radiomics"}
    assert (destination / "quantitative_results.csv").is_file()
    assert (destination / "qualitative_answers.csv").is_file()
    assert (destination / "radiomics_tidy.json").is_file()
    assert (destination / "demo_report.html").is_file()
    assert "http://" not in (destination / "demo_report.html").read_text(encoding="utf-8")

    quantitative = pd.read_csv(destination / "quantitative_results.csv")
    qualitative = pd.read_csv(destination / "qualitative_answers.csv")
    radiomics = load_radiomics_project(destination / "radiomics_demo.zip")
    assert not quantitative.empty
    assert set(qualitative["is_synthetic"]) == {True}
    assert radiomics.identity.subject_id == "DEMO-SUBJECT"
    assert not radiomics.features.empty and not radiomics.mask_qc.empty
    assert radiomics.reproducibility_receipt["result_hash"]
    assert manifest["qicom_version"]
    for name, expected_hash in manifest["file_sha256"].items():
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == expected_hash


def test_demo_destination_must_be_new(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    try:
        create_demo_bundle(existing)
    except FileExistsError:
        pass
    else:
        raise AssertionError("Existing demo destination must not be overwritten.")


def test_two_generations_agree_on_every_file_not_marked_run_specific(tmp_path: Path) -> None:
    """The documented procedure compares demo hashes across runs; files that
    record their own creation time used to make every comparison fail."""
    import json

    first = json.loads((create_demo_bundle(tmp_path / "a") / "qicom_demo.json").read_text())
    second = json.loads((create_demo_bundle(tmp_path / "b") / "qicom_demo.json").read_text())

    stable = set(first["file_sha256"]) - set(first["run_specific_files"])
    assert stable and all(
        first["file_sha256"][name] == second["file_sha256"][name] for name in stable
    )
    assert first["run_specific_files"] == second["run_specific_files"]
