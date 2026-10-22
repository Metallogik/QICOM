from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import urllib.error
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qicom.radiomics import ibsi_validation
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.ibsi_validation import (
    IbsiReferenceSet,
    IbsiVerificationStudy,
    ReferenceFeature,
    ReferenceResourcesMissing,
    compare_with_ibsi_reference,
    ensure_reference_resources,
    export_ibsi_receipt_csv,
    export_ibsi_receipt_excel,
    export_ibsi_receipt_html,
    export_ibsi_receipt_json,
    load_ibsi_reference_set,
    load_ibsi_verification_project,
    load_reference_manifest,
    manifest_hash,
    run_ibsi_verification,
    save_ibsi_verification_project,
    verify_resource_integrity,
)
from qicom.radiomics.models import ImageGeometry, RadiomicsImage


def _resource_manifest(payload: bytes) -> dict[str, object]:
    return {
        "resources": [
            {
                "id": "one",
                "filename": "one.bin",
                "url": "https://official.invalid/one.bin",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ]
    }


def test_packaged_manifest_is_versioned_hash_pinned_and_non_certifying() -> None:
    manifest = load_reference_manifest()

    assert manifest["schema_version"] == 1
    assert len(manifest["features"]) >= 100
    assert len(manifest_hash(manifest)) == 64
    assert manifest["license"]["redistributed_by_qicom"] is False
    assert all(len(resource["sha256"]) == 64 for resource in manifest["resources"])
    rendered = json.dumps(manifest).lower()
    assert "ibsi certified" not in rendered
    assert "clinically validated" not in rendered


def test_reference_download_requires_consent_and_is_atomic(tmp_path: Path) -> None:
    payload = b"official-test-payload"
    manifest = _resource_manifest(payload)

    with pytest.raises(ReferenceResourcesMissing, match="consent"):
        ensure_reference_resources(manifest, tmp_path)

    def downloader(_url: str, destination: Path) -> None:
        destination.write_bytes(payload)

    paths = ensure_reference_resources(manifest, tmp_path, consent=True, downloader=downloader)

    assert paths["one"].read_bytes() == payload
    assert (
        verify_resource_integrity(manifest, tmp_path)["one"] == hashlib.sha256(payload).hexdigest()
    )
    assert not list(tmp_path.glob("*.download"))


def test_reference_download_rejects_wrong_checksum(tmp_path: Path) -> None:
    manifest = _resource_manifest(b"expected")

    def downloader(_url: str, destination: Path) -> None:
        destination.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="SHA-256"):
        ensure_reference_resources(manifest, tmp_path, consent=True, downloader=downloader)


def _reference_set(expected: float = 0.0, tolerance: float = 0.0) -> IbsiReferenceSet:
    image = RadiomicsImage(np.ones((2, 2), dtype=np.float32), ImageGeometry.unit((2, 2)), "array")
    manifest = {
        "reference_set_id": "known",
        "reference_set_version": "1",
        "pipeline_components": ["formula"],
        "scope": "unit test",
        "reference_values": {"relative_zero_threshold": 1e-12},
    }
    return IbsiReferenceSet(
        manifest=manifest,
        manifest_hash="a" * 64,
        resource_paths={},
        resource_hashes={},
        image=image,
        mask=np.ones((2, 2), dtype=bool),
        config=RadiomicsConfig(minimum_roi_voxels=2),
        features=(
            ReferenceFeature(
                "original_firstorder_Mean",
                "stat_mean",
                "intensity",
                "Statistics",
                "Mean",
                expected,
                tolerance,
            ),
        ),
    )


def _calculated(value: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "feature_key": "original_firstorder_Mean",
                "value": value,
                "unit": "intensity",
                "provider": "pyradiomics-test",
            }
        ]
    )


def test_comparison_handles_zero_reference_out_of_tolerance_and_missing() -> None:
    # A reference value of 0 with a tolerance of 0 leaves no room for error: 0.4
    # is a real deviation. Before the near-zero rule it was truncated to exactly
    # zero and reported as an official PASS.
    failed_at_zero = compare_with_ibsi_reference(_calculated(0.4), _reference_set())
    exact = compare_with_ibsi_reference(_calculated(0.0), _reference_set())
    failed = compare_with_ibsi_reference(_calculated(1.2), _reference_set())
    missing = compare_with_ibsi_reference(_calculated(1.0).iloc[0:0], _reference_set())

    assert failed_at_zero.overall_status == "FAIL"
    assert failed_at_zero.table.loc[0, "comparison_error"] == pytest.approx(0.4)
    assert exact.overall_status == "PASS"
    assert np.isnan(failed_at_zero.table.loc[0, "relative_error"])
    assert failed.overall_status == "FAIL"
    assert missing.overall_status == "INCOMPLETE"
    assert missing.table.loc[0, "status"] == "NOT-TESTED"


def test_near_zero_reference_states_why_the_relative_error_is_undefined() -> None:
    result = compare_with_ibsi_reference(_calculated(0.4), _reference_set())
    row = result.table.loc[0]

    assert np.isnan(row["relative_error"])
    assert "not defined" in row["reason"]
    assert "untruncated absolute difference" in row["reason"]
    assert "absolute-near-zero-v2" in row["tolerance_applied"]


def test_near_zero_reference_respects_an_explicit_absolute_tolerance() -> None:
    # reference 0, tolerance 0.5: 0.25 is inside it and 0.75 is outside, and
    # neither is silently truncated away.
    inside = compare_with_ibsi_reference(_calculated(0.25), _reference_set(tolerance=0.5))
    outside = compare_with_ibsi_reference(_calculated(0.75), _reference_set(tolerance=0.5))

    assert inside.overall_status == "PASS"
    assert inside.table.loc[0, "comparison_error"] == pytest.approx(0.25)
    assert outside.overall_status == "FAIL"
    assert outside.table.loc[0, "comparison_error"] == pytest.approx(0.75)


def test_a_nonzero_reference_still_truncates_to_three_significant_digits() -> None:
    # The official spreadsheet rule is unchanged away from zero: three
    # significant digits of a reference of 1.0 truncate the difference at the
    # 0.01 scale, so 1.004 stays a PASS at tolerance 0 while 1.04 does not.
    within = compare_with_ibsi_reference(
        _calculated(1.004), _reference_set(expected=1.0, tolerance=0.0)
    )
    beyond = compare_with_ibsi_reference(
        _calculated(1.04), _reference_set(expected=1.0, tolerance=0.0)
    )

    assert within.overall_status == "PASS"
    assert within.table.loc[0, "comparison_error"] == pytest.approx(0.0)
    assert beyond.overall_status == "FAIL"
    assert beyond.table.loc[0, "comparison_error"] == pytest.approx(0.04)
    assert beyond.table.loc[0, "relative_error"] == pytest.approx(0.04)


def test_receipt_exports_and_verification_project_round_trip(tmp_path: Path) -> None:
    result = compare_with_ibsi_reference(_calculated(0.0), _reference_set())
    paths = [
        export_ibsi_receipt_json(tmp_path / "receipt", result),
        export_ibsi_receipt_csv(tmp_path / "receipt", result),
        export_ibsi_receipt_excel(tmp_path / "receipt", result),
        export_ibsi_receipt_html(tmp_path / "receipt", result),
    ]
    study = IbsiVerificationStudy("known", "a" * 64, config=RadiomicsConfig(), result=result)
    project = save_ibsi_verification_project(tmp_path / "study", study)
    restored = load_ibsi_verification_project(project)

    assert all(path.is_file() for path in paths)
    assert restored.mode == "reference-verification"
    assert restored.result is not None and restored.result.overall_status == "PASS"
    derived = restored.duplicate_as_experiment()
    assert derived.mode == "derived-experiment"
    assert not derived.verifiable
    assert derived.result is None


@pytest.mark.skipif(
    os.environ.get("QICOM_RUN_IBSI_REFERENCE") != "1",
    reason="Set QICOM_RUN_IBSI_REFERENCE=1 to download/run the official IBSI check.",
)
def test_real_pyradiomics_reproduces_hash_pinned_ibsi_reference() -> None:
    """The scientific gate: real PyRadiomics against the official IBSI 1
    digital phantom, hash-pinned.

    Fetching the official resources needs the network, and a runner that
    cannot reach them has not disproved anything. QICOM_IBSI_ALLOW_NETWORK_
    FAILURE=1 lets a *transport* failure be reported as a skip with the cause
    named - set on pull requests only. It never covers a verification result:
    a feature that disagrees with the reference fails whatever the flag says,
    and on a tag the flag is absent, so an unreachable resource fails too.
    Turning this into a green skip is the one thing that must not happen.
    """
    cache_value = os.environ.get("QICOM_IBSI_CACHE")
    try:
        reference_set = load_ibsi_reference_set(
            cache_dir=None if cache_value is None else Path(cache_value), consent=True
        )
    except (OSError, urllib.error.URLError) as exc:
        if os.environ.get("QICOM_IBSI_ALLOW_NETWORK_FAILURE") != "1":
            raise
        pytest.skip(f"the official IBSI resources could not be fetched: {exc!r}")
    result = run_ibsi_verification(reference_set)

    assert result.overall_status == "PASS"
    assert result.summary["passed"] == len(reference_set.features)
    assert result.summary["failed"] == 0
    assert result.summary["not_tested"] == 0

    artifact_value = os.environ.get("QICOM_IBSI_ARTIFACT_DIR")
    if artifact_value:
        artifact_dir = Path(artifact_value)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        export_ibsi_receipt_json(artifact_dir / "ibsi-reference-report", result)
        export_ibsi_receipt_csv(artifact_dir / "ibsi-reference-report", result)
        shutil.copy2(
            reference_set.resource_paths["reference_values"],
            artifact_dir / "official-reference-values.xlsx",
        )


def _sized_manifest(payload: bytes, *, size_bytes: int | None = None) -> dict[str, object]:
    manifest = _resource_manifest(payload)
    resources = manifest["resources"]
    assert isinstance(resources, list)
    resources[0]["size_bytes"] = len(payload) if size_bytes is None else size_bytes
    return manifest


class TestDownloadBudget:
    """A pinned SHA-256 says a file was wrong only once the whole of it has
    been written - which is no protection at all when "the whole of it" is
    what the other end chose to send. The manifest now pins the size too, and
    the transfer is bounded on the way in."""

    def test_the_packaged_manifest_pins_a_size_for_every_resource(self) -> None:
        manifest = load_reference_manifest()
        resources = manifest["resources"]

        assert all(int(resource["size_bytes"]) > 0 for resource in resources)

    def test_a_download_larger_than_its_pinned_size_is_refused(self, tmp_path: Path) -> None:
        payload = b"official-test-payload"
        manifest = _sized_manifest(payload)

        def oversized(_url: str, destination: Path) -> None:
            destination.write_bytes(payload + b"!")

        with pytest.raises(ValueError, match="the manifest pins"):
            ensure_reference_resources(manifest, tmp_path, consent=True, downloader=oversized)

        assert not list(tmp_path.glob("*"))

    def test_a_download_smaller_than_its_pinned_size_is_refused(self, tmp_path: Path) -> None:
        payload = b"official-test-payload"
        manifest = _sized_manifest(payload)

        def truncated(_url: str, destination: Path) -> None:
            destination.write_bytes(payload[:-1])

        with pytest.raises(ValueError, match="the manifest pins"):
            ensure_reference_resources(manifest, tmp_path, consent=True, downloader=truncated)

    def test_an_overrun_keeps_the_previously_verified_file(self, tmp_path: Path) -> None:
        payload = b"official-test-payload"
        manifest = _sized_manifest(payload)
        ensure_reference_resources(
            manifest,
            tmp_path,
            consent=True,
            downloader=lambda _url, destination: destination.write_bytes(payload),
        )
        good = tmp_path / "one.bin"
        assert good.read_bytes() == payload

        def oversized(_url: str, destination: Path) -> None:
            destination.write_bytes(b"x" * 10_000)

        # A verified local copy is used as-is; force the download path by
        # corrupting the cached file first.
        good.write_bytes(b"corrupted")
        with pytest.raises(ValueError):
            ensure_reference_resources(manifest, tmp_path, consent=True, downloader=oversized)

        assert good.read_bytes() == b"corrupted"
        assert not list(tmp_path.glob("*.download"))

    def test_a_download_is_refused_when_the_disk_cannot_hold_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _sized_manifest(b"payload", size_bytes=1_000_000)
        monkeypatch.setattr(
            ibsi_validation.shutil,
            "disk_usage",
            lambda _path: shutil._ntuple_diskusage(total=0, used=0, free=1000),
        )

        with pytest.raises(ValueError, match="free space"):
            ensure_reference_resources(
                manifest,
                tmp_path,
                consent=True,
                downloader=lambda _url, destination: destination.write_bytes(b"payload"),
            )


class TestBoundedTransfer:
    """_download itself, against a server that lies or simply sends too much."""

    class _Response:
        def __init__(self, body: bytes, declared: str | None) -> None:
            self._stream = io.BytesIO(body)
            self.headers = {} if declared is None else {"Content-Length": declared}

        def read(self, size: int = -1) -> bytes:
            return self._stream.read(size)

        def __enter__(self) -> TestBoundedTransfer._Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def _serve(self, monkeypatch: pytest.MonkeyPatch, body: bytes, declared: str | None) -> None:
        monkeypatch.setattr(
            ibsi_validation.urllib.request,
            "urlopen",
            lambda *_a, **_k: self._Response(body, declared),
        )

    def test_a_declared_length_over_the_limit_is_refused_before_transferring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._serve(monkeypatch, b"x" * 10, declared="999999")
        target = tmp_path / "out.bin"

        with pytest.raises(ValueError, match="the server declares"):
            ibsi_validation._download("https://official.invalid/x", target, 100)

    def test_a_missing_content_length_is_still_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counter is what holds when the header is absent."""
        self._serve(monkeypatch, b"x" * 5_000, declared=None)
        target = tmp_path / "out.bin"

        with pytest.raises(ValueError, match="exceeded the"):
            ibsi_validation._download("https://official.invalid/x", target, 100)

    def test_a_lying_content_length_is_still_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server willing to send the wrong file will send the wrong length
        with it - so the header being small proves nothing."""
        self._serve(monkeypatch, b"x" * 5_000, declared="10")
        target = tmp_path / "out.bin"

        with pytest.raises(ValueError, match="exceeded the"):
            ibsi_validation._download("https://official.invalid/x", target, 100)

    def test_an_unparsable_content_length_does_not_crash_the_transfer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._serve(monkeypatch, b"x" * 10, declared="not-a-number")
        target = tmp_path / "out.bin"

        ibsi_validation._download("https://official.invalid/x", target, 100)

        assert target.read_bytes() == b"x" * 10

    def test_a_transfer_within_the_budget_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._serve(monkeypatch, b"x" * 50, declared="50")
        target = tmp_path / "out.bin"

        ibsi_validation._download("https://official.invalid/x", target, 100)

        assert target.read_bytes() == b"x" * 50
