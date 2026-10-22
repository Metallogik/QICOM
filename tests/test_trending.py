from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from qicom.trending import TrendStore


def _value(number: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "measure_key": "mse",
                "value": number,
                "unit": "intensity^2",
                "status": "requested-and-computed",
            }
        ]
    )


def test_versioned_baseline_shewhart_ewma_acceptance_and_drilldown(tmp_path: Path) -> None:
    store = TrendStore(tmp_path / "trends.sqlite3")
    for index, value in enumerate((9.0, 10.0, 11.0), start=1):
        store.add_run(
            _value(value),
            module="quantitative",
            project_path=f"project-{index}.zip",
            equipment_id="scanner-a",
            protocol_id="qa-head",
            timestamp=f"2026-01-0{index}T00:00:00+00:00",
        )

    baseline = store.create_baseline(
        name="scanner-a-qa",
        version="1.0",
        measure_key="mse",
        filters={"equipment_id": "scanner-a", "protocol_id": "qa-head"},
        acceptance_lower=5.0,
        acceptance_upper=12.0,
    )
    store.add_run(
        _value(20.0),
        module="quantitative",
        project_path="outlier.zip",
        equipment_id="scanner-a",
        protocol_id="qa-head",
        timestamp="2026-01-04T00:00:00+00:00",
    )

    restored = store.load_baseline("scanner-a-qa", "1.0")
    chart = store.chart(
        restored,
        ewma_lambda=0.3,
        filters={"equipment_id": "scanner-a", "protocol_id": "qa-head"},
    )

    assert restored.baseline_hash == baseline.baseline_hash
    assert chart.iloc[-1]["project_path"] == "outlier.zip"
    assert bool(chart.iloc[-1]["shewhart_signal"])
    assert chart.iloc[-1]["acceptance_status"] == "FAIL"
    assert "ewma" in chart.columns
    assert set(chart["baseline_version"]) == {"1.0"}


def test_trend_store_requires_two_baseline_runs_and_compatible_units(tmp_path: Path) -> None:
    store = TrendStore(tmp_path / "trends.sqlite3")
    store.add_run(_value(1.0), module="radiomics")
    with pytest.raises(ValueError, match="at least two"):
        store.create_baseline(name="one", version="1", measure_key="mse")

    store.add_run(
        pd.DataFrame([{"measure_key": "mse", "value": 2.0, "unit": "other"}]),
        module="radiomics",
    )
    with pytest.raises(ValueError, match="incompatible units"):
        store.create_baseline(name="bad", version="1", measure_key="mse")


def _store_with_baseline(path: Path) -> TrendStore:
    store = TrendStore(path)
    for index, value in enumerate((9.0, 10.0, 11.0), start=1):
        store.add_run(
            _value(value),
            module="quantitative",
            timestamp=f"2026-01-0{index}T00:00:00+00:00",
        )
    store.create_baseline(name="qa", version="1.0", measure_key="mse")
    return store


def test_a_newer_database_is_refused_before_any_table_is_added(tmp_path: Path) -> None:
    path = tmp_path / "trends.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE trend_schema (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO trend_schema(version) VALUES (99)")

    with pytest.raises(ValueError, match="Unsupported"):
        TrendStore(path)

    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert tables == {"trend_schema"}


def test_a_baseline_edited_after_it_was_recorded_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "trends.sqlite3"
    store = _store_with_baseline(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE trend_baselines SET mean = 0.0")

    with pytest.raises(ValueError, match="changed after it was recorded"):
        store.load_baseline("qa", "1.0")


def test_a_baseline_recorded_before_the_canonical_text_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "trends.sqlite3"
    store = _store_with_baseline(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE trend_baselines SET canonical_json = NULL")

    assert store.load_baseline("qa", "1.0").mean == pytest.approx(10.0)
