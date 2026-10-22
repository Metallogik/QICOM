from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from qicom.reporting.tidy import (
    TIDY_SCHEMA_VERSION,
    export_self_contained_html,
    export_tidy_csv,
    export_tidy_json,
    load_tidy_json,
    make_tidy_results,
)


def test_tidy_csv_json_are_versioned_and_round_trip(tmp_path: Path) -> None:
    source = pd.DataFrame(
        [
            {
                "subject_id": "s1",
                "roi_id": "r1",
                "image_role": "reference",
                "feature_key": "mean",
                "value": 2.5,
                "unit": "HU",
                "profile_hash": "abc",
            }
        ]
    )
    tidy = make_tidy_results(
        source,
        module="radiomics",
        analysis_id="a1",
        key_column="feature_key",
        value_column="value",
        provenance_columns=("profile_hash",),
    )

    csv_path = export_tidy_csv(tmp_path / "result", tidy)
    json_path = export_tidy_json(tmp_path / "result", tidy, metadata={"profile": "core@2"})
    restored = load_tidy_json(json_path)

    assert csv_path.suffix == ".csv"
    assert set(pd.read_csv(csv_path)["schema_version"]) == {TIDY_SCHEMA_VERSION}
    assert restored.metadata == {"profile": "core@2"}
    pd.testing.assert_frame_equal(restored.results, tidy, check_dtype=False)


def test_tidy_json_detects_tampering(tmp_path: Path) -> None:
    tidy = make_tidy_results(
        pd.DataFrame([{"key": "mse", "value": 1.0}]),
        module="quantitative",
        analysis_id="a1",
        key_column="key",
        value_column="value",
        unit_column=None,
    )
    path = export_tidy_json(tmp_path / "result.json", tidy)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["results"][0]["value"] = 99.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hash does not match"):
        load_tidy_json(path)


def test_self_contained_html_escapes_user_text_has_no_remote_content_and_limits_size(
    tmp_path: Path,
) -> None:
    path = export_self_contained_html(
        tmp_path / "report.html",
        title="<script>alert(1)</script>",
        sections={"Results <unsafe>": pd.DataFrame([{"comment": "<img src=x>"}])},
        methodology="Method <b>not markup</b>",
        warnings=["Warning <script>"],
        profile={"name": "local"},
        provenance={"source": "synthetic"},
    )
    text = path.read_text(encoding="utf-8")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "&lt;img src=x&gt;" in text
    assert "http://" not in text and "https://" not in text
    assert "default-src 'none'" in text

    with pytest.raises(ValueError, match="limit"):
        export_self_contained_html(
            tmp_path / "too-large.html",
            title="x",
            sections={"large": pd.DataFrame([{"value": "x" * 5000}])},
            methodology="test",
            maximum_bytes=1024,
        )
