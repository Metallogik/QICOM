"""A radiomics project gives back the tables it was saved with, value for value.

The audit of 1.1.0.dev0 saved a ROI named "001" and a status "NA": the
reopened project held the integer 1 and a missing value, the result hash no
longer matched the receipt, and the cohort reader pooled the altered table -
as it pooled projects whose own manifest declared their results stale.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qicom.provenance import build_reproducibility_receipt, hash_dataframe
from qicom.radiomics.cohort import (
    aggregate_loaded_project_tables,
    aggregate_project_tables,
    load_cohort_project_tables,
)
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.models import (
    ImageGeometry,
    RadiomicsImage,
    RadiomicsRoi,
    RadiomicsStudyIdentity,
)
from qicom.radiomics.project import (
    RADIOMICS_MANIFEST,
    RadiomicsProjectData,
    load_radiomics_project,
    read_legacy_table,
    save_radiomics_project,
    table_from_typed_payload,
    typed_table_payload,
)


def _features() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "roi_id": ["r1", "r1", "r1"],
            "roi_name": ["001", "001", "001"],
            "image_role": ["reference", "comparison", "comparison"],
            "feature_key": ["mean", "mean", "max"],
            "value": [3.0, 0.1 + 0.2, float("nan")],
            "status": ["NA", "", None],
            "count": [1, 2, 3],
            "flag": [True, False, True],
            "note": pd.Series(["null", None, float("nan")], dtype=object),
        }
    )


def _project(features: pd.DataFrame | None = None, **changes: object) -> RadiomicsProjectData:
    pixels = np.arange(36, dtype=np.float32).reshape(6, 6)
    image = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")
    mask = np.ones((6, 6), dtype=bool)
    table = _features() if features is None else features
    data = RadiomicsProjectData(
        reference=image,
        comparison=image,
        rois=[RadiomicsRoi("r1", "001", "manual", mask, mask)],
        config=RadiomicsConfig(),
        features=table,
        delta=pd.DataFrame({"roi_id": ["r1"], "delta_absolute": [1.0]}),
        identity=RadiomicsStudyIdentity("subject-1", "case-1", "lesion-1", "t0", "acq-1"),
        reproducibility_receipt=build_reproducibility_receipt(
            inputs={"reference": pixels},
            masks=None,
            configuration={},
            requested_metrics=["mean"],
            outcomes=[],
            results=table,
        ).to_dict(),
    )
    return replace(data, **changes)


class TestTypedTables:
    def test_every_value_and_dtype_survives_a_save(self, tmp_path: Path) -> None:
        original = _features()

        restored = load_radiomics_project(
            save_radiomics_project(tmp_path / "study.zip", _project())
        ).features

        assert restored["roi_name"].tolist() == ["001", "001", "001"]
        assert restored["status"].iloc[0] == "NA"
        assert restored["status"].iloc[1] == ""
        assert restored["note"].iloc[0] == "null"
        assert restored["note"].iloc[1] is None
        assert np.isnan(restored["note"].iloc[2])
        assert restored["value"].iloc[1] == 0.1 + 0.2
        assert dict(restored.dtypes.astype(str)) == dict(original.dtypes.astype(str))
        assert hash_dataframe(restored) == hash_dataframe(original)

    def test_the_cohort_reader_gets_the_same_table(self, tmp_path: Path) -> None:
        path = save_radiomics_project(tmp_path / "study.zip", _project())

        cohort = load_cohort_project_tables(path)

        assert hash_dataframe(cohort.features) == hash_dataframe(_features())

    def test_the_csv_is_still_written_for_other_tools(self, tmp_path: Path) -> None:
        path = save_radiomics_project(tmp_path / "study.zip", _project())

        with zipfile.ZipFile(path) as archive:
            csv = pd.read_csv(io.BytesIO(archive.read("tables/features.csv")))
        assert len(csv) == 3

    def test_a_table_the_format_cannot_carry_exactly_is_left_to_the_csv(self) -> None:
        assert typed_table_payload(pd.DataFrame({1: [1.0]})) is None
        assert typed_table_payload(pd.DataFrame({"a": pd.Categorical(["x"])})) is None
        assert typed_table_payload(pd.DataFrame({"a": pd.array([1, None], dtype="Int64")})) is None
        assert typed_table_payload(pd.DataFrame({"a": [object()]})) is None
        assert typed_table_payload(pd.DataFrame([[1, 2]], columns=["a", "a"])) is None

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"qicom_typed_table_version": 99},
            {"qicom_typed_table_version": True, "columns": [], "dtypes": {}, "rows": []},
            {"qicom_typed_table_version": 1.0, "columns": [], "dtypes": {}, "rows": []},
            {"qicom_typed_table_version": "1", "columns": [], "dtypes": {}, "rows": []},
            {"qicom_typed_table_version": 1, "columns": ["a"], "dtypes": {}, "rows": []},
            {"qicom_typed_table_version": 1, "columns": ["a"], "dtypes": {"a": "f8"}, "rows": [[]]},
            {"qicom_typed_table_version": 1, "columns": ["a"], "dtypes": {"a": 3}, "rows": []},
            {"qicom_typed_table_version": 1, "columns": ["a"], "dtypes": {"a": "nope"}, "rows": []},
            {
                "qicom_typed_table_version": 1,
                "columns": ["a"],
                "dtypes": {"a": "M8[ns]"},
                "rows": [],
            },
        ],
    )
    def test_a_malformed_typed_table_is_refused(self, payload: object) -> None:
        with pytest.raises(ValueError):
            table_from_typed_payload(payload)

    def test_an_empty_table_round_trips(self) -> None:
        payload = typed_table_payload(pd.DataFrame())

        assert payload is not None
        assert table_from_typed_payload(payload).empty


class TestProjectsWrittenBeforeTypedTables:
    """A project from an older QICOM has only its CSV: known text columns
    keep their text, which is everything that CSV can still tell."""

    def test_known_text_columns_keep_their_text(self) -> None:
        csv = "roi_id,roi_name,status,value\nr1,001,NA,1.5\nr1,,ok,\n"

        table = read_legacy_table(io.BytesIO(csv.encode()))

        assert table["roi_name"].iloc[0] == "001"
        assert pd.isna(table["roi_name"].iloc[1])
        assert table["status"].iloc[0] == "NA"
        assert table["value"].iloc[0] == 1.5
        assert np.isnan(table["value"].iloc[1])

    def test_a_project_without_typed_tables_is_still_read(self, tmp_path: Path) -> None:
        path = save_radiomics_project(tmp_path / "study.zip", _project())
        legacy = tmp_path / "legacy.zip"
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(legacy, "w") as target:
            for item in source.infolist():
                if item.filename.endswith(".json") and item.filename.startswith("tables/"):
                    continue
                payload = source.read(item.filename)
                if item.filename == RADIOMICS_MANIFEST:
                    manifest = json.loads(payload)
                    del manifest["typed_tables"]
                    payload = json.dumps(manifest).encode()
                target.writestr(item, payload)

        restored = load_radiomics_project(legacy)

        assert restored.features["roi_name"].tolist() == ["001", "001", "001"]
        assert restored.features["status"].iloc[0] == "NA"


class TestCohortRefusesWhatIsNotTheExtraction:
    def test_a_stale_project_is_refused(self, tmp_path: Path) -> None:
        path = save_radiomics_project(tmp_path / "stale.zip", _project(results_stale=True))

        with pytest.raises(ValueError, match="stale"):
            load_cohort_project_tables(path)
        with pytest.raises(ValueError, match="stale"):
            aggregate_project_tables([path])

    def test_stale_records_built_by_hand_are_refused_too(self, tmp_path: Path) -> None:
        study = load_cohort_project_tables(save_radiomics_project(tmp_path / "s.zip", _project()))

        with pytest.raises(ValueError, match="Stale"):
            aggregate_loaded_project_tables([replace(study, results_stale=True)])

    def test_a_table_that_no_longer_matches_its_receipt_is_refused(self, tmp_path: Path) -> None:
        data = _project()
        edited = replace(data, features=data.features.assign(value=[9.0, 9.0, 9.0]))
        path = save_radiomics_project(tmp_path / "edited.zip", edited)

        with pytest.raises(ValueError, match="result hash"):
            load_cohort_project_tables(path)

    def test_a_project_without_a_result_hash_is_accepted(self, tmp_path: Path) -> None:
        path = save_radiomics_project(tmp_path / "old.zip", _project(reproducibility_receipt={}))

        assert len(load_cohort_project_tables(path).features) == 3
