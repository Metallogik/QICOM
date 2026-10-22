"""Tests for qicom.session.project_folder (save_project_folder/
load_project_folder, the folder-shaped primitives, plus save_project_zip/
load_project_zip, the single-file format actually used by the GUI now -
see TestProjectZip)."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import qicom.session.project_folder as project_folder
from qicom.metrics.types import RESULT_COLUMNS
from qicom.session.project_folder import (
    PROJECT_FILE_NAME,
    PROJECT_FORMAT_VERSION,
    PanelSource,
    ProjectAnnotation,
    ProjectLine,
    ProjectRoi,
    cleanup_extracted_project,
    copy_panel_source_into,
    count_panel_source_files,
    extract_project_zip,
    load_project_folder,
    load_project_zip,
    save_project_folder,
    save_project_zip,
)


def _write_dicom(path: Path, *, patient_name: str = "Doe^John") -> None:
    import numpy as np
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    pixel_array = np.full((4, 4), 100, dtype=np.uint16)
    dataset.Rows, dataset.Columns = pixel_array.shape
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PatientName = patient_name
    dataset.BurnedInAnnotation = "NO"
    dataset.PixelData = pixel_array.tobytes()
    dataset.save_as(path, enforce_file_format=True)


def _make_results() -> pd.DataFrame:
    rows = [
        {
            "metric_id": "mse",
            "metric_name": "MSE",
            "category": "reference_error",
            "image": "pair",
            "scope": "per-slice",
            "value": 12.5,
            "std": float("nan"),
            "n_slices": None,
            "better_direction": "lower",
            "target_value": 0.0,
        }
    ]
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


class TestSaveAndLoadRoundTrip:
    def test_round_trips_a_single_file_panel(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        ref_file = source_dir / "ref.png"
        ref_file.write_bytes(b"fake png bytes")
        project_dir = tmp_path / "project"

        save_project_folder(
            project_dir,
            panels=[
                PanelSource(
                    role="reference", path=ref_file, explicit_files=None, window_level=(10.0, 200.0)
                )
            ],
            rois=[],
            annotations=[],
            lines=[],
            data_range=255.0,
            results={"": _make_results()},
            notes="hello",
            metric_ids=("mse", "ssim"),
        )

        data = load_project_folder(project_dir)

        assert data.version == PROJECT_FORMAT_VERSION
        assert data.notes == "hello"
        assert data.data_range == 255.0
        assert data.metric_ids == ("mse", "ssim")
        assert data.warnings == []
        assert len(data.panels) == 1
        panel = data.panels[0]
        assert panel.role == "reference"
        assert panel.is_directory_source is False
        assert panel.window_level == (10.0, 200.0)
        copied = project_dir / panel.relative_path
        assert copied.exists()
        assert copied.read_bytes() == b"fake png bytes"
        assert list(data.results[""]["metric_id"]) == ["mse"]

    def test_round_trips_a_directory_source_panel(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "dicom_series"
        source_dir.mkdir()
        (source_dir / "slice1.dcm").write_bytes(b"one")
        (source_dir / "slice2.dcm").write_bytes(b"two")
        project_dir = tmp_path / "project"

        save_project_folder(
            project_dir,
            panels=[
                PanelSource(
                    role="reference", path=source_dir, explicit_files=None, window_level=None
                )
            ],
            rois=[],
            annotations=[],
            lines=[],
            data_range=None,
            results={},
        )

        data = load_project_folder(project_dir)

        panel = data.panels[0]
        assert panel.is_directory_source is True
        assert panel.window_level is None
        resolved = project_dir / panel.relative_path
        assert resolved.is_dir()
        assert {p.name for p in resolved.iterdir()} == {"slice1.dcm", "slice2.dcm"}

    def test_explicit_multi_file_pick_copies_only_those_files(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "mixed_folder"
        source_dir.mkdir()
        picked = [source_dir / f"slice{i}.png" for i in range(3)]
        for f in picked:
            f.write_bytes(b"picked")
        # Extra files in the same folder that were NOT explicitly picked -
        # must never end up in the project folder (see PanelSource's own
        # docstring on why explicit_files takes priority over path).
        (source_dir / "unrelated.png").write_bytes(b"not picked")
        project_dir = tmp_path / "project"

        save_project_folder(
            project_dir,
            panels=[
                PanelSource(
                    role="reference",
                    path=source_dir,
                    explicit_files=picked,
                    window_level=None,
                )
            ],
            rois=[],
            annotations=[],
            lines=[],
            data_range=None,
            results={},
        )

        data = load_project_folder(project_dir)
        resolved = project_dir / data.panels[0].relative_path
        assert data.panels[0].is_directory_source is True
        assert {p.name for p in resolved.iterdir()} == {"slice0.png", "slice1.png", "slice2.png"}

    def test_round_trips_rois_annotations_and_lines(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"

        save_project_folder(
            project_dir,
            panels=[],
            rois=[
                ProjectRoi(
                    name="Signal",
                    shape_type="rectangle",
                    geometry={"pos": [1.0, 2.0], "size": [3.0, 4.0], "angle": 0.0},
                    target_image="reference",
                    role="signal_roi",
                    slice_index=7,
                    persist_across_slices=True,
                )
            ],
            annotations=[
                ProjectAnnotation(text="Bright spot", pos=(5.0, 6.0), target="comparison")
            ],
            lines=[
                ProjectLine(
                    name="Line 1",
                    geometry={"pos": [0.0, 0.0], "points": [[0.0, 0.0], [10.0, 10.0]]},
                )
            ],
            data_range=None,
            results={},
        )

        data = load_project_folder(project_dir)

        assert len(data.rois) == 1
        assert data.rois[0].name == "Signal"
        assert data.rois[0].role == "signal_roi"
        assert data.rois[0].slice_index == 7
        assert data.rois[0].persist_across_slices is True
        assert data.rois[0].geometry == {"pos": [1.0, 2.0], "size": [3.0, 4.0], "angle": 0.0}
        assert len(data.annotations) == 1
        assert data.annotations[0].text == "Bright spot"
        assert data.annotations[0].pos == (5.0, 6.0)
        assert data.annotations[0].target == "comparison"
        assert len(data.lines) == 1
        assert data.lines[0].name == "Line 1"
        assert data.lines[0].geometry == {
            "pos": [0.0, 0.0],
            "points": [[0.0, 0.0], [10.0, 10.0]],
        }

    def test_old_project_roi_defaults_to_slice_zero_without_persistence(
        self, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "project"
        save_project_folder(
            project_dir,
            panels=[],
            rois=[
                ProjectRoi(
                    name="Legacy",
                    shape_type="rectangle",
                    geometry={"pos": [1.0, 2.0], "size": [3.0, 4.0], "angle": 0.0},
                    target_image="reference",
                    role=None,
                )
            ],
            annotations=[],
            lines=[],
            data_range=None,
            results={},
        )
        manifest_path = project_dir / PROJECT_FILE_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["rois"][0].pop("slice_index")
        manifest["rois"][0].pop("persist_across_slices")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        roi = load_project_folder(project_dir).rois[0]

        assert roi.slice_index == 0
        assert roi.persist_across_slices is False

    def test_round_trips_multi_pair_results(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"

        save_project_folder(
            project_dir,
            panels=[],
            rois=[],
            annotations=[],
            lines=[],
            data_range=255.0,
            results={"AvsB": _make_results(), "AvsC": _make_results()},
        )

        data = load_project_folder(project_dir)

        assert set(data.results) == {"AvsB", "AvsC"}
        assert list(data.results["AvsB"]["metric_id"]) == ["mse"]

    def test_manifest_is_human_readable_json(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        save_project_folder(
            project_dir,
            panels=[],
            rois=[],
            annotations=[],
            lines=[],
            data_range=None,
            results={},
        )

        manifest_text = (project_dir / PROJECT_FILE_NAME).read_text(encoding="utf-8")

        assert "\n" in manifest_text  # indent=2, not a single compact line
        assert json.loads(manifest_text)["qicom_project_version"] == PROJECT_FORMAT_VERSION


class TestStrictJsonManifest:
    """Bare NaN/Infinity tokens are not JSON (RFC 8259): strict readers such as
    jq or a browser refused the manifest of any project with a PSNR of +inf."""

    def _results_with_non_finite_values(self) -> pd.DataFrame:
        results = _make_results()
        results.loc[0, "value"] = float("inf")
        results.loc[0, "std"] = float("nan")
        return results

    def test_non_finite_values_round_trip_through_a_strict_manifest(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        save_project_folder(
            project_dir,
            panels=[],
            rois=[],
            annotations=[],
            lines=[],
            data_range=None,
            results={"": self._results_with_non_finite_values()},
        )
        text = (project_dir / PROJECT_FILE_NAME).read_text(encoding="utf-8")

        def refuse(token: str) -> None:
            raise AssertionError(f"non-JSON token {token} in the manifest")

        json.loads(text, parse_constant=refuse)
        restored = load_project_folder(project_dir).results[""]
        assert restored.loc[0, "value"] == float("inf")
        assert np.isnan(restored.loc[0, "std"])

    def test_a_version_1_manifest_with_bare_tokens_still_loads(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        save_project_folder(
            project_dir,
            panels=[],
            rois=[],
            annotations=[],
            lines=[],
            data_range=None,
            results={"": self._results_with_non_finite_values()},
        )
        manifest_path = project_dir / PROJECT_FILE_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["qicom_project_version"] = 1
        record = manifest["results"][""][0]
        record["value"], record["std"] = float("inf"), float("nan")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")  # bare tokens, as v1

        restored = load_project_folder(project_dir).results[""]

        assert restored.loc[0, "value"] == float("inf")
        assert np.isnan(restored.loc[0, "std"])


class TestVersionAndMissingFileHandling:
    def test_rejects_an_unsupported_future_version(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / PROJECT_FILE_NAME).write_text(
            json.dumps({"qicom_project_version": 999}), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="999"):
            load_project_folder(project_dir)

    def test_missing_referenced_image_is_a_warning_not_a_crash(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / PROJECT_FILE_NAME).write_text(
            json.dumps(
                {
                    "qicom_project_version": PROJECT_FORMAT_VERSION,
                    "created_at": "now",
                    "saved_at": "now",
                    "notes": None,
                    "data_range": None,
                    "panels": [
                        {
                            "role": "reference",
                            "relative_path": "images/reference/missing.png",
                            "is_directory_source": False,
                            "window_level": None,
                        }
                    ],
                    "rois": [],
                    "annotations": [],
                    "results": {},
                }
            ),
            encoding="utf-8",
        )

        data = load_project_folder(project_dir)

        assert len(data.warnings) == 1
        assert "reference" in data.warnings[0]
        assert len(data.panels) == 1  # the panel record itself still comes back

    def test_rejects_a_manifest_path_outside_the_project(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / PROJECT_FILE_NAME).write_text(
            json.dumps(
                {
                    "qicom_project_version": PROJECT_FORMAT_VERSION,
                    "panels": [
                        {
                            "role": "reference",
                            "relative_path": "../../patient.dcm",
                            "is_directory_source": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="traversal"):
            load_project_folder(project_dir)


class TestCopyFailureAbortsAndCleansUp:
    def test_a_copy_failure_removes_only_the_subtrees_this_call_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_a = tmp_path / "a.png"
        source_a.write_bytes(b"a")
        source_b = tmp_path / "b.png"
        source_b.write_bytes(b"b")
        project_dir = tmp_path / "project"

        # First, a real successful save establishing a pre-existing
        # "reference" images directory.
        save_project_folder(
            project_dir,
            panels=[
                PanelSource(role="reference", path=source_a, explicit_files=None, window_level=None)
            ],
            rois=[],
            annotations=[],
            lines=[],
            data_range=None,
            results={},
        )
        pre_existing_file = project_dir / "images" / "reference" / "a.png"
        assert pre_existing_file.exists()

        real_copy2 = shutil.copy2
        call_count = {"n": 0}

        def _flaky_copy2(src: Any, dst: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("simulated disk failure")
            return real_copy2(src, dst)

        monkeypatch.setattr(shutil, "copy2", _flaky_copy2)

        with pytest.raises(OSError, match="simulated disk failure"):
            save_project_folder(
                project_dir,
                panels=[
                    PanelSource(
                        role="reference", path=source_a, explicit_files=None, window_level=None
                    ),
                    PanelSource(
                        role="comparison", path=source_b, explicit_files=None, window_level=None
                    ),
                ],
                rois=[],
                annotations=[],
                lines=[],
                data_range=None,
                results={},
            )

        # The pre-existing reference/ directory must survive untouched...
        assert pre_existing_file.exists()
        # ...but nothing was created for comparison/ (this call's own new
        # directory, never reached because reference/ failed first).
        assert not (project_dir / "images" / "comparison").exists()


class TestCopyPanelSourceIntoAnonymization:
    """anonymize_dicom/on_file_copied - opt-in, only ever set by
    qicom.session.evaluation_folder's qualitative save path (see that
    module's own use), never by save_project_folder's quantitative one -
    default False/None keeps every existing call site's behavior
    unchanged."""

    def test_default_leaves_dicom_tags_untouched(self, tmp_path: Path) -> None:
        import pydicom

        source_dicom = tmp_path / "slice.dcm"
        _write_dicom(source_dicom)
        images_dir = tmp_path / "images"

        copy_panel_source_into(
            PanelSource(
                role="reference", path=source_dicom, explicit_files=None, window_level=None
            ),
            images_dir,
        )

        copied = pydicom.dcmread(images_dir / "reference" / "slice.dcm")
        assert str(copied.PatientName) == "Doe^John"

    def test_quantitative_project_save_anonymizes_dicom_by_default(self, tmp_path: Path) -> None:
        import pydicom

        source_dicom = tmp_path / "slice.dcm"
        _write_dicom(source_dicom)
        project_dir = tmp_path / "project"

        save_project_folder(
            project_dir,
            panels=[
                PanelSource(
                    role="reference", path=source_dicom, explicit_files=None, window_level=None
                )
            ],
            rois=[],
            annotations=[],
            lines=[],
            data_range=None,
            results={},
        )

        copied = pydicom.dcmread(project_dir / "images" / "reference" / "slice.dcm")
        assert str(copied.PatientName) != "Doe^John"
        assert copied.PatientIdentityRemoved == "YES"

    def test_anonymize_dicom_strips_patient_tags_for_a_single_file_source(
        self, tmp_path: Path
    ) -> None:
        import pydicom

        source_dicom = tmp_path / "slice.dcm"
        _write_dicom(source_dicom)
        images_dir = tmp_path / "images"

        copy_panel_source_into(
            PanelSource(
                role="reference", path=source_dicom, explicit_files=None, window_level=None
            ),
            images_dir,
            anonymize_dicom=True,
        )

        copied = pydicom.dcmread(images_dir / "reference" / "slice.dcm")
        assert str(copied.PatientName) == ""
        # The original source file, on disk where it was loaded from, is
        # never touched - only the copy inside the new project/evaluation
        # folder is anonymized.
        original = pydicom.dcmread(source_dicom)
        assert str(original.PatientName) == "Doe^John"

    def test_anonymize_dicom_covers_every_file_in_a_directory_source(self, tmp_path: Path) -> None:
        import pydicom

        source_dir = tmp_path / "series"
        source_dir.mkdir()
        _write_dicom(source_dir / "a.dcm")
        _write_dicom(source_dir / "b.dcm")
        images_dir = tmp_path / "images"

        copy_panel_source_into(
            PanelSource(role="reference", path=source_dir, explicit_files=None, window_level=None),
            images_dir,
            anonymize_dicom=True,
        )

        for name in ("a.dcm", "b.dcm"):
            copied = pydicom.dcmread(images_dir / "reference" / name)
            assert str(copied.PatientName) == ""

    def test_anonymize_dicom_covers_an_explicit_file_pick(self, tmp_path: Path) -> None:
        import pydicom

        source_dicom = tmp_path / "slice.dcm"
        _write_dicom(source_dicom)
        images_dir = tmp_path / "images"

        copy_panel_source_into(
            PanelSource(
                role="reference", path=tmp_path, explicit_files=[source_dicom], window_level=None
            ),
            images_dir,
            anonymize_dicom=True,
        )

        copied = pydicom.dcmread(images_dir / "reference" / "slice.dcm")
        assert str(copied.PatientName) == ""

    def test_a_non_dicom_file_is_copied_untouched_even_with_anonymize_dicom_true(
        self, tmp_path: Path
    ) -> None:
        source_png = tmp_path / "slice.png"
        source_png.write_bytes(b"not really a png but not dicom either")
        images_dir = tmp_path / "images"

        copy_panel_source_into(
            PanelSource(role="reference", path=source_png, explicit_files=None, window_level=None),
            images_dir,
            anonymize_dicom=True,
        )

        assert (
            images_dir / "reference" / "slice.png"
        ).read_bytes() == b"not really a png but not dicom either"

    def test_on_file_copied_fires_once_per_file(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "series"
        source_dir.mkdir()
        _write_dicom(source_dir / "a.dcm")
        _write_dicom(source_dir / "b.dcm")
        _write_dicom(source_dir / "c.dcm")
        images_dir = tmp_path / "images"
        calls = {"n": 0}

        copy_panel_source_into(
            PanelSource(role="reference", path=source_dir, explicit_files=None, window_level=None),
            images_dir,
            on_file_copied=lambda: calls.__setitem__("n", calls["n"] + 1),
        )

        assert calls["n"] == 3


class TestCountPanelSourceFiles:
    def test_a_single_file_source_counts_as_one(self, tmp_path: Path) -> None:
        source = tmp_path / "slice.png"
        source.write_bytes(b"x")

        assert (
            count_panel_source_files(
                PanelSource(role="reference", path=source, explicit_files=None, window_level=None)
            )
            == 1
        )

    def test_a_directory_source_counts_every_file(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "series"
        source_dir.mkdir()
        (source_dir / "a.dcm").write_bytes(b"a")
        (source_dir / "b.dcm").write_bytes(b"b")

        assert (
            count_panel_source_files(
                PanelSource(
                    role="reference", path=source_dir, explicit_files=None, window_level=None
                )
            )
            == 2
        )

    def test_an_explicit_file_pick_counts_only_those_files(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "series"
        source_dir.mkdir()
        picked = source_dir / "a.dcm"
        picked.write_bytes(b"a")
        (source_dir / "b.dcm").write_bytes(b"b")  # not picked, must not count

        assert (
            count_panel_source_files(
                PanelSource(
                    role="reference", path=source_dir, explicit_files=[picked], window_level=None
                )
            )
            == 1
        )


class TestResaving:
    def test_resaving_to_the_same_folder_preserves_the_original_created_at(
        self, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "project"
        save_project_folder(
            project_dir,
            panels=[],
            rois=[],
            annotations=[],
            lines=[],
            data_range=None,
            results={},
        )
        first = load_project_folder(project_dir)

        save_project_folder(
            project_dir,
            panels=[],
            rois=[],
            annotations=[],
            lines=[],
            data_range=None,
            results={},
            notes="updated",
        )
        second = load_project_folder(project_dir)

        assert second.created_at == first.created_at
        assert second.notes == "updated"


class TestProjectZip:
    """save_project_zip/extract_project_zip/load_project_zip/
    cleanup_extracted_project - the single-file format the GUI actually
    saves/loads now (see qicom.gui.controller.AppController.
    save_project_zip/load_project_zip)."""

    def _save_zip(self, zip_path: Path, source_image: Path) -> None:
        save_project_zip(
            zip_path,
            panels=[
                PanelSource(
                    role="reference", path=source_image, explicit_files=None, window_level=None
                )
            ],
            rois=[],
            annotations=[],
            lines=[],
            data_range=255.0,
            results={"": _make_results()},
            notes="hello",
        )

    def test_save_produces_a_single_zip_file(self, tmp_path: Path) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "MyProject.zip"

        self._save_zip(zip_path, source_image)

        assert zip_path.is_file()
        assert not zip_path.with_suffix("").exists()  # no loose folder left behind
        with zipfile.ZipFile(zip_path) as zf:
            assert PROJECT_FILE_NAME in zf.namelist()

    def test_save_streams_without_building_a_complete_project_folder(self, tmp_path: Path) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")

        with patch.object(
            project_folder,
            "save_project_folder",
            side_effect=AssertionError("ZIP save must not build a folder copy"),
        ):
            self._save_zip(tmp_path / "streamed.zip", source_image)

    def test_anonymization_reuses_one_temporary_file(self, tmp_path: Path) -> None:
        first = tmp_path / "first.png"
        second = tmp_path / "second.png"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        destinations: list[Path] = []
        original_copy = project_folder.shutil.copy2

        def counted_copy(source: Path, destination: Path) -> str:
            destinations.append(Path(destination))
            return original_copy(source, destination)

        with patch.object(project_folder.shutil, "copy2", side_effect=counted_copy):
            save_project_zip(
                tmp_path / "single-temp.zip",
                panels=[PanelSource("reference", first, [first, second], None)],
                rois=[],
                annotations=[],
                lines=[],
                data_range=None,
                results={},
            )

        assert len(destinations) == 2
        assert len(set(destinations)) == 1

    def test_a_refused_dicom_is_archived_only_under_the_users_override(
        self, tmp_path: Path
    ) -> None:
        import pydicom

        from qicom.io.deidentification import ImageDeidentificationRefused
        from qicom.io.dicom_anonymize import UnverifiedDicomFile

        dicom = tmp_path / "scanner.dcm"
        _write_dicom(dicom)
        dataset = pydicom.dcmread(dicom)
        del dataset.BurnedInAnnotation
        dataset.save_as(dicom, enforce_file_format=True)
        zip_path = tmp_path / "project.zip"
        common: dict[str, Any] = {
            "panels": [PanelSource("reference", dicom, None, None)],
            "rois": [],
            "annotations": [],
            "lines": [],
            "data_range": None,
            "results": {},
        }

        with pytest.raises(ImageDeidentificationRefused) as refusal:
            save_project_zip(zip_path, **common)
        assert not zip_path.exists()
        assert [record.file_name for record in refusal.value.refusals] == ["scanner.dcm"]
        assert not any(record.attributes_scrubbed for record in refusal.value.refusals)

        unverified: list[UnverifiedDicomFile] = []
        save_project_zip(zip_path, **common, unverified_dicom=unverified)

        with zipfile.ZipFile(zip_path) as archive:
            archived = b"".join(
                archive.read(name) for name in archive.namelist() if name.endswith(".dcm")
            )
        assert b"Doe^John" not in archived
        assert [record.file_name for record in unverified] == ["scanner.dcm"]
        assert dicom.read_bytes().count(b"Doe^John") == 1  # the source is never touched

    def test_compression_uses_content_not_filename_extension(self, tmp_path: Path) -> None:
        import pydicom
        from pydicom.encaps import encapsulate
        from pydicom.uid import JPEGBaseline8Bit

        real_png = tmp_path / "pixels.bin"
        real_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"already-compressed")
        fake_png = tmp_path / "pixels.png"
        fake_png.write_bytes(b"plain uncompressed bytes" * 10)
        dicom = tmp_path / "pixels.dcm"
        _write_dicom(dicom)
        compressed_dicom = tmp_path / "compressed-without-extension"
        _write_dicom(compressed_dicom)
        dataset = pydicom.dcmread(compressed_dicom)
        dataset.file_meta.TransferSyntaxUID = JPEGBaseline8Bit
        dataset.PixelData = encapsulate([bytes(dataset.PixelData)])
        dataset["PixelData"].is_undefined_length = True
        dataset.save_as(compressed_dicom, enforce_file_format=True)

        assert project_folder._zip_compression_for_file(real_png) == zipfile.ZIP_STORED
        assert project_folder._zip_compression_for_file(fake_png) == zipfile.ZIP_DEFLATED
        assert project_folder._zip_compression_for_file(dicom) == zipfile.ZIP_DEFLATED
        assert project_folder._zip_compression_for_file(compressed_dicom) == zipfile.ZIP_STORED

    def test_extract_then_cleanup_round_trips_and_removes_the_temp_dir(
        self, tmp_path: Path
    ) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "MyProject.zip"
        self._save_zip(zip_path, source_image)

        extracted = extract_project_zip(zip_path)
        assert (extracted / PROJECT_FILE_NAME).is_file()
        cleanup_extracted_project(extracted)
        assert not extracted.exists()

    def test_load_project_zip_returns_data_and_a_readable_extraction_dir(
        self, tmp_path: Path
    ) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "MyProject.zip"
        self._save_zip(zip_path, source_image)

        data, extracted = load_project_zip(zip_path)
        try:
            assert data.notes == "hello"
            assert len(data.panels) == 1
            panel_path = extracted / data.panels[0].relative_path
            assert panel_path.is_file()
            assert list(data.results[""]["metric_id"]) == ["mse"]
        finally:
            cleanup_extracted_project(extracted)

    @pytest.mark.parametrize(
        ("members", "refusal"),
        [
            (None, "not a readable ZIP archive"),
            ({"../evil.txt": "x"}, "traversal"),
            ({PROJECT_FILE_NAME: "[]"}, "must be a JSON object"),
        ],
    )
    def test_a_refused_archive_leaves_nothing_in_the_temp_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        members: dict[str, str] | None,
        refusal: str,
    ) -> None:
        """The extraction goes to the system temp directory, not to tmp_path:
        pointing that at an empty folder is what makes a leftover visible."""
        temp = tmp_path / "system-temp"
        temp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp))
        archive = tmp_path / "refused.zip"
        if members is None:
            archive.write_bytes(b"not a zip file")
        else:
            with zipfile.ZipFile(archive, "w") as handle:
                for name, content in members.items():
                    handle.writestr(name, content)

        with pytest.raises(ValueError, match=refusal):
            load_project_zip(archive)

        assert list(temp.iterdir()) == []


def test_a_project_zip_clears_nifti_header_text_too(tmp_path: Path) -> None:
    """The quantitative project used to copy NIfTI headers verbatim."""
    import nibabel as nib
    import numpy as np

    source = tmp_path / "scan.nii"
    image = nib.Nifti1Image(np.zeros((3, 4, 5), dtype=np.float32), np.eye(4))
    image.header["descrip"] = b"Patient ROSSI MARIO"
    image.header.extensions.append(nib.nifti1.Nifti1Extension(2, b"ROSSI^MARIO"))
    nib.save(image, str(source))
    zip_path = tmp_path / "project.zip"

    save_project_zip(
        zip_path,
        panels=[PanelSource("reference", source, None, None)],
        rois=[],
        annotations=[],
        lines=[],
        data_range=None,
        results={},
    )

    with zipfile.ZipFile(zip_path) as archive:
        archived = b"".join(
            archive.read(name) for name in archive.namelist() if name.endswith(".nii")
        )
        assert not [name for name in archive.namelist() if name.endswith(("current", ".tmp"))]
    assert archived and b"ROSSI" not in archived
    assert b"ROSSI" in source.read_bytes()  # the source is never touched


def test_a_project_zip_keeps_each_result_tables_receipt_and_outcomes(tmp_path: Path) -> None:
    """The receipt and outcomes travel as DataFrame attributes, which the
    manifest used to drop: a project reopened elsewhere exported numbers
    without build revision, input hashes, configuration or outcomes."""
    from qicom.session.project_folder import load_project_zip

    results = pd.DataFrame([{column: None for column in RESULT_COLUMNS}], columns=RESULT_COLUMNS)
    receipt = {"receipt_schema_version": 3, "configuration_hash": "abc", "build_revision": "r1"}
    outcomes = [{"metric_id": "mse", "status": "requested-and-computed"}]
    results.attrs["qicom_reproducibility_receipt"] = receipt
    results.attrs["qicom_metric_outcomes"] = outcomes
    legacy = pd.DataFrame([{column: None for column in RESULT_COLUMNS}], columns=RESULT_COLUMNS)
    zip_path = tmp_path / "project.zip"

    save_project_zip(
        zip_path,
        panels=[],
        rois=[],
        annotations=[],
        lines=[],
        data_range=None,
        results={"A vs B": results, "legacy": legacy},
    )
    data, extracted = load_project_zip(zip_path)

    assert data.results["A vs B"].attrs["qicom_reproducibility_receipt"] == receipt
    assert data.results["A vs B"].attrs["qicom_metric_outcomes"] == outcomes
    assert "qicom_reproducibility_receipt" not in data.results["legacy"].attrs
    shutil.rmtree(extracted, ignore_errors=True)


class TestDamagedProjects:
    """A truncated archive or a manifest with fields of the wrong type
    escaped the GUI's error handling, and opened from the launcher it ended
    QICOM without a message."""

    def test_a_truncated_archive_is_reported_as_bad_input(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "project.zip"
        save_project_zip(
            zip_path, panels=[], rois=[], annotations=[], lines=[], data_range=None, results={}
        )
        zip_path.write_bytes(zip_path.read_bytes()[:40])

        with pytest.raises(ValueError, match="not a readable ZIP"):
            load_project_zip(zip_path)

    def test_a_manifest_with_wrongly_typed_fields_is_reported_as_bad_input(
        self, tmp_path: Path
    ) -> None:
        folder = tmp_path / "project"
        save_project_folder(
            folder, panels=[], rois=[], annotations=[], lines=[], data_range=None, results={}
        )
        manifest = json.loads((folder / PROJECT_FILE_NAME).read_text())
        manifest["panels"] = [1]
        (folder / PROJECT_FILE_NAME).write_text(json.dumps(manifest))

        with pytest.raises(ValueError, match="malformed"):
            load_project_folder(folder)
