"""Versioned scientific provenance shared by every QICOM workflow.

The objects in this module deliberately contain no GUI or storage code.  They
are small JSON-compatible records that can be embedded in a project, written
as a report sheet, or displayed by a workbench without changing their meaning.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from qicom.__version__ import __version__
from qicom.build_identity import UNKNOWN_REVISION, build_identity
from qicom.schema_version import is_supported_version

CALCULATION_SCHEMA_VERSION = 2
# 3 adds build_revision/source_dirty (see qicom.build_identity). Versions 1
# and 2 remain readable: their receipts predate the field and are restored
# with an unknown revision rather than being attributed to the running build.
# 4 hashes with the strict canonical JSON (see canonical_json); a schema-3
# receipt keeps the legacy rules, so its receipt_hash never changes.
PERSISTENCE_SCHEMA_VERSION = 4
_LAST_LEGACY_HASH_SCHEMA = 3
EXPORT_SCHEMA_VERSION = 2

# Strings a non-finite float is written as. A *string* with one of these values,
# or one starting with the escape, is written with the escape in front, so the
# float NaN and the text "NaN" no longer hash alike.
_NON_FINITE_TOKENS = frozenset({"NaN", "+Infinity", "-Infinity"})
_STRING_ESCAPE = "\x00"


def canonical_json(value: Any, *, strict: bool = True) -> str:
    """Return the stable UTF-8 JSON representation used for hashes.

    `strict` (the default) is injective where the legacy form was not: in the
    legacy form a float NaN and the string "NaN" serialized alike, mapping keys
    1 and "1" collapsed into one, and a NumPy boolean became the string "True".
    The legacy form is kept, unchanged, for hashes already persisted with it:
    schema-3 receipts, NSS model identities and acceptance profiles.
    """
    return json.dumps(
        _json_value(value, strict=strict),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_HASH_BLOCK_ELEMENTS = 1_048_576


def hash_array(array: np.ndarray) -> str:
    """Hash dtype, shape and C-order bytes, including non-contiguous arrays.

    A non-contiguous array is hashed in blocks of whole leading-axis slabs:
    flattening it once per block copied the entire array every time, which
    made a transposed volume quadratically slow. The bytes, and so the hash,
    are the same either way.
    """
    values = np.asarray(array)
    digest = hashlib.sha256()
    digest.update(values.dtype.str.encode("ascii"))
    digest.update(canonical_json(list(values.shape), strict=False).encode("ascii"))
    if values.flags.c_contiguous or values.ndim <= 1:
        flat = np.ascontiguousarray(values).reshape(-1)
        for start in range(0, flat.size, _HASH_BLOCK_ELEMENTS):
            digest.update(flat[start : start + _HASH_BLOCK_ELEMENTS].view(np.uint8))
    else:
        rows = max(1, _HASH_BLOCK_ELEMENTS // max(1, values[0].size))
        for start in range(0, values.shape[0], rows):
            digest.update(np.ascontiguousarray(values[start : start + rows]).view(np.uint8))
    return digest.hexdigest()


def hash_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def hash_dataframe(dataframe: pd.DataFrame, *, strict: bool = True) -> str:
    """Stable result hash independent of pandas' platform-specific CSV EOL."""
    if not strict:
        legacy = {
            "columns": [str(column) for column in dataframe.columns],
            "rows": [
                {str(key): _json_scalar(value) for key, value in record.items()}
                for record in dataframe.to_dict(orient="records")
            ],
        }
        return sha256_bytes(canonical_json(legacy, strict=False).encode("utf-8"))
    payload = {
        "columns": [str(column) for column in dataframe.columns],
        "rows": dataframe.to_dict(orient="records"),
    }
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def _strict_text(text: str) -> str:
    if text in _NON_FINITE_TOKENS or text.startswith(_STRING_ESCAPE):
        return _STRING_ESCAPE + text
    return text


def _json_scalar(value: Any, *, strict: bool = False) -> Any:
    if strict:
        if isinstance(value, np.bool_):
            return bool(value)
        if isinstance(value, str):
            return _strict_text(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if np.isnan(number):
            return "NaN"
        if np.isposinf(number):
            return "+Infinity"
        if np.isneginf(number):
            return "-Infinity"
        return number
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (tuple, list)):
        return [_json_scalar(item, strict=strict) for item in value]
    return _strict_text(str(value)) if strict else str(value)


def _json_value(value: Any, *, strict: bool = False) -> Any:
    """Convert scientific/configuration objects into stable JSON values."""
    if isinstance(value, Mapping):
        converted = {str(key): _json_value(item, strict=strict) for key, item in value.items()}
        if strict and len(converted) != len(value):
            raise ValueError("Mapping keys collide once converted to text (e.g. 1 and '1').")
        return converted
    if isinstance(value, (tuple, list, set, frozenset)):
        items = [_json_value(item, strict=strict) for item in value]
        return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, np.ndarray):
        return {
            "array_hash": hash_array(value),
            "dtype": value.dtype.str,
            "shape": list(value.shape),
        }
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict(), strict=strict)
    return _json_scalar(value, strict=strict)


@dataclass(frozen=True, slots=True)
class StatisticalUnit:
    """Explicit identifiers for the observational hierarchy.

    Older projects have none of these fields and migrate to an empty unit.
    An empty unit is deliberately *unknown*, never interpreted as one unit per
    slice or ROI.
    """

    subject_id: str | None = None
    case_id: str | None = None
    acquisition_id: str | None = None
    timepoint: str | None = None
    lesion_id: str | None = None
    roi_id: str | None = None
    reader_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None.")
            # Trimmed here, as reading a receipt back trims: " S1 " used to be
            # stored as typed and reloaded as "S1", which changed the hash and
            # made one subject look like two.
            if isinstance(value, str) and value != value.strip():
                object.__setattr__(self, name, value.strip())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> StatisticalUnit:
        if payload is None:
            return cls()
        lesion = payload.get("lesion_id", payload.get("roi_id"))
        roi = payload.get("roi_id", payload.get("lesion_id"))
        return cls(
            subject_id=_optional_text(payload.get("subject_id")),
            case_id=_optional_text(payload.get("case_id")),
            acquisition_id=_optional_text(payload.get("acquisition_id")),
            timepoint=_optional_text(payload.get("timepoint")),
            lesion_id=_optional_text(lesion),
            roi_id=_optional_text(roi),
            reader_id=_optional_text(payload.get("reader_id")),
        )

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)

    def independent_key(self, level: str = "subject") -> tuple[str, ...] | None:
        """Key used for inference; never falls back to slice or ROI identity."""
        if level == "subject":
            return None if self.subject_id is None else (self.subject_id,)
        if level == "case":
            if self.case_id is None:
                return None
            return tuple(value for value in (self.subject_id, self.case_id) if value is not None)
        if level == "acquisition":
            if self.acquisition_id is None:
                return None
            return tuple(
                value
                for value in (self.subject_id, self.case_id, self.acquisition_id)
                if value is not None
            )
        raise ValueError("level must be 'subject', 'case' or 'acquisition'.")


def _stored_flag(value: object, name: str) -> bool:
    """A JSON boolean as written; `bool("false")` is True."""
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}.")
    return value


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True, slots=True)
class IndependenceAssessment:
    n_independent: int
    can_infer: bool
    unit_level: str
    reason: str


def assess_independence(
    units: Sequence[StatisticalUnit], *, level: str = "subject", minimum: int = 3
) -> IndependenceAssessment:
    """Count declared independent units and state why inference is/isn't valid."""
    if minimum < 2:
        raise ValueError("minimum must be at least 2.")
    keys = {key for unit in units if (key := unit.independent_key(level)) is not None}
    count = len(keys)
    if count >= minimum:
        reason = f"{count} independent {level} units are explicitly identified."
    elif not units:
        reason = "No observations were supplied; only descriptive output is available."
    elif not keys:
        reason = (
            f"No {level}_id is available. Slices and ROIs from one case are correlated and "
            "are not promoted to independent observations."
        )
    else:
        reason = (
            f"Only {count} independent {level} unit(s) are identified; at least {minimum} "
            "are required for formal inference."
        )
    return IndependenceAssessment(count, count >= minimum, level, reason)


class OutcomeStatus(StrEnum):
    COMPUTED = "requested-and-computed"
    NOT_REQUESTED = "not-requested"
    NOT_APPLICABLE = "not-applicable"
    FAILED = "failed"
    UNAVAILABLE_BACKEND = "unavailable-backend"
    INVALID_INPUT = "invalid/degenerate-input"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MetricOutcome:
    metric_id: str
    status: OutcomeStatus
    reason: str
    technical_reason: str = ""
    scope: str = "analysis"

    def to_dict(self) -> dict[str, str]:
        return {
            "metric_id": self.metric_id,
            "status": self.status.value,
            "reason": self.reason,
            "technical_reason": self.technical_reason,
            "scope": self.scope,
        }


class MetricOutcomeRegistry:
    """Mutable run-local builder whose exported records are immutable."""

    def __init__(self) -> None:
        self._outcomes: dict[tuple[str, str], MetricOutcome] = {}

    def record(
        self,
        metric_id: str,
        status: OutcomeStatus,
        reason: str,
        technical_reason: str = "",
        *,
        scope: str = "analysis",
    ) -> None:
        key = (metric_id, scope)
        existing = self._outcomes.get(key)
        # A successful computation is definitive.  Otherwise preserve the
        # first concrete failure instead of replacing it with not-applicable.
        if existing is not None and existing.status == OutcomeStatus.COMPUTED:
            return
        if existing is not None and status == OutcomeStatus.NOT_APPLICABLE:
            return
        self._outcomes[key] = MetricOutcome(metric_id, status, reason, technical_reason, scope)

    def record_partial(
        self,
        metric_id: str,
        reason: str,
        technical_reason: str = "",
        *,
        scope: str = "analysis",
    ) -> None:
        """A value computed on part of its input: computed, and saying so.

        Overrides whatever the parts recorded - a failure on one slice and a
        plain success on the others - because neither describes the value
        reported, and, being a computed outcome, it is then definitive.
        """
        self._outcomes[(metric_id, scope)] = MetricOutcome(
            metric_id, OutcomeStatus.COMPUTED, reason, technical_reason, scope
        )

    def mark_catalog(self, available_ids: Iterable[str], requested_ids: Iterable[str]) -> None:
        requested = set(requested_ids)
        for metric_id in available_ids:
            if metric_id not in requested:
                self.record(
                    metric_id,
                    OutcomeStatus.NOT_REQUESTED,
                    "Metric not selected by the active quantitative profile.",
                )

    def finalize_requested(self, requested_ids: Iterable[str]) -> None:
        for metric_id in requested_ids:
            key = (metric_id, "analysis")
            if key not in self._outcomes:
                self.record(
                    metric_id,
                    OutcomeStatus.NOT_APPLICABLE,
                    "The metric was requested but its required inputs or ROI role were absent.",
                )

    @property
    def outcomes(self) -> tuple[MetricOutcome, ...]:
        return tuple(sorted(self._outcomes.values(), key=lambda item: (item.metric_id, item.scope)))

    def to_dataframe(self) -> pd.DataFrame:
        columns = ["metric_id", "status", "reason", "technical_reason", "scope"]
        return pd.DataFrame([item.to_dict() for item in self.outcomes], columns=columns)

    def to_list(self) -> list[dict[str, str]]:
        return [item.to_dict() for item in self.outcomes]


@dataclass(frozen=True)
class ReproducibilityReceipt:
    qicom_version: str
    calculation_schema_version: int
    formula_versions: dict[str, str]
    platform: dict[str, str]
    backend_versions: dict[str, str]
    input_hashes: dict[str, str]
    mask_hashes: dict[str, str]
    configuration: dict[str, Any]
    configuration_hash: str
    requested_metrics: tuple[str, ...]
    metric_outcomes: tuple[dict[str, str], ...]
    preflight_warnings: tuple[str, ...]
    preflight_diagnostics: dict[str, Any]
    statistical_unit: dict[str, str | None]
    timestamp: str
    result_hash: str | None = None
    # The exact source that produced these numbers - the one identity a
    # version string cannot supply (see qicom.build_identity). Defaulted so
    # every receipt written before schema 3 still constructs.
    build_revision: str = UNKNOWN_REVISION
    source_dirty: bool = False
    receipt_schema_version: int = PERSISTENCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def receipt_hash(self) -> str:
        strict = self.receipt_schema_version > _LAST_LEGACY_HASH_SCHEMA
        return sha256_bytes(canonical_json(self.to_dict(), strict=strict).encode("utf-8"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ReproducibilityReceipt:
        version = payload.get("receipt_schema_version", 1)
        if not is_supported_version(version, (1, 2, 3, PERSISTENCE_SCHEMA_VERSION)):
            raise ValueError(f"Unsupported reproducibility receipt version {version!r}.")
        calculation_version = payload.get("calculation_schema_version", 1)
        if not is_supported_version(calculation_version, range(1, CALCULATION_SCHEMA_VERSION + 1)):
            raise ValueError(f"Unsupported calculation schema version {calculation_version!r}.")
        unit = StatisticalUnit.from_dict(payload.get("statistical_unit")).to_dict()
        return cls(
            qicom_version=str(payload.get("qicom_version", "unknown")),
            calculation_schema_version=calculation_version,
            formula_versions={
                str(key): str(value)
                for key, value in dict(payload.get("formula_versions", {})).items()
            },
            platform={
                str(key): str(value) for key, value in dict(payload.get("platform", {})).items()
            },
            backend_versions={
                str(key): str(value)
                for key, value in dict(payload.get("backend_versions", {})).items()
            },
            input_hashes={
                str(key): str(value) for key, value in dict(payload.get("input_hashes", {})).items()
            },
            mask_hashes={
                str(key): str(value) for key, value in dict(payload.get("mask_hashes", {})).items()
            },
            configuration=dict(payload.get("configuration", {})),
            configuration_hash=str(payload.get("configuration_hash", "")),
            requested_metrics=tuple(str(value) for value in payload.get("requested_metrics", ())),
            metric_outcomes=tuple(dict(value) for value in payload.get("metric_outcomes", ())),
            preflight_warnings=tuple(str(value) for value in payload.get("preflight_warnings", ())),
            preflight_diagnostics=dict(payload.get("preflight_diagnostics", {})),
            statistical_unit=unit,
            timestamp=str(payload.get("timestamp", "")),
            result_hash=(
                None if payload.get("result_hash") is None else str(payload.get("result_hash"))
            ),
            build_revision=str(payload.get("build_revision") or UNKNOWN_REVISION),
            source_dirty=_stored_flag(payload.get("source_dirty", False), "source_dirty"),
            # Schemas 1 and 2 are normalized to 3; 3 stays 3, so its hash keeps
            # the legacy canonical rules it was computed with.
            receipt_schema_version=max(version, _LAST_LEGACY_HASH_SCHEMA),
        )


# Read from the installed metadata, not by importing: torch alone takes seconds
# to import, and a receipt must not pay that for a backend it did not use.
_DISTRIBUTION_VERSIONS = {"pyradiomics": "pyradiomics", "pywt": "PyWavelets", "torch": "torch"}


def _installed_backend_versions(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    versions: dict[str, str] = {}
    for key, distribution in _DISTRIBUTION_VERSIONS.items():
        try:
            versions[key] = metadata_version(distribution)
        except PackageNotFoundError:
            continue
    for module_name in ("numpy", "scipy", "pandas", "skimage", "nibabel", "SimpleITK"):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        version = getattr(module, "__version__", None)
        if version is None and module_name == "SimpleITK":
            version = module.Version_VersionString()
        versions[module_name] = str(version or "unknown")
    if extra:
        versions.update({str(key): str(value) for key, value in extra.items()})
    return versions


def build_reproducibility_receipt(
    *,
    inputs: Mapping[str, np.ndarray | bytes | str | Path],
    masks: Mapping[str, np.ndarray] | None,
    configuration: Mapping[str, Any],
    requested_metrics: Iterable[str],
    outcomes: Iterable[MetricOutcome] = (),
    warnings: Iterable[str] = (),
    preflight_diagnostics: Mapping[str, Any] | None = None,
    statistical_unit: StatisticalUnit | None = None,
    formula_versions: Mapping[str, str] | None = None,
    backend_versions: Mapping[str, str] | None = None,
    results: pd.DataFrame | None = None,
) -> ReproducibilityReceipt:
    config = dict(configuration)
    input_hashes = {name: _hash_input(value) for name, value in sorted(inputs.items())}
    mask_hashes = {
        name: hash_array(np.asarray(value, dtype=bool))
        for name, value in sorted((masks or {}).items())
    }
    identity = build_identity()
    return ReproducibilityReceipt(
        qicom_version=__version__,
        build_revision=identity.revision,
        source_dirty=identity.source_dirty,
        calculation_schema_version=CALCULATION_SCHEMA_VERSION,
        formula_versions=dict(formula_versions or {"quantitative": "2.0"}),
        platform={
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
        },
        backend_versions=_installed_backend_versions(backend_versions),
        input_hashes=input_hashes,
        mask_hashes=mask_hashes,
        configuration=_json_value(config),
        configuration_hash=configuration_hash(config),
        requested_metrics=tuple(sorted(set(requested_metrics))),
        metric_outcomes=tuple(item.to_dict() for item in outcomes),
        preflight_warnings=tuple(str(value) for value in warnings),
        preflight_diagnostics=_json_value(dict(preflight_diagnostics or {})),
        statistical_unit=(statistical_unit or StatisticalUnit()).to_dict(),
        timestamp=datetime.now(UTC).isoformat(),
        result_hash=None if results is None else hash_dataframe(results),
    )


#: Configuration entry for where a run's files were (inputs, output folder).
#: Recorded in the receipt, left out of `configuration_hash`: the same
#: calculation run from another folder or machine is the same calculation.
LOCATIONS_KEY = "locations"
#: Prefix of an input "hash" for an input that was not there to be read.
MISSING_INPUT_PREFIX = "missing:"


def configuration_hash(configuration: Mapping[str, Any]) -> str:
    """Hash of the configuration that defines the calculation - everything
    except its `locations` entry (see LOCATIONS_KEY)."""
    defining = {key: value for key, value in configuration.items() if key != LOCATIONS_KEY}
    return sha256_bytes(canonical_json(defining).encode("utf-8"))


def _hash_input(value: np.ndarray | bytes | str | Path) -> str:
    if isinstance(value, np.ndarray):
        return hash_array(value)
    if isinstance(value, bytes):
        return sha256_bytes(value)
    path = Path(value)
    if path.is_file():
        return hash_file(path)
    if path.is_dir():
        digest = hashlib.sha256()
        for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
            digest.update(os.fspath(child.relative_to(path)).replace("\\", "/").encode("utf-8"))
            digest.update(hash_file(child).encode("ascii"))
        return digest.hexdigest()
    # Not a content hash: there was no content to read. Marked as such, so a
    # receipt never attests to an input that was never read.
    return MISSING_INPUT_PREFIX + sha256_bytes(str(value).encode("utf-8"))


__all__ = [
    "CALCULATION_SCHEMA_VERSION",
    "LOCATIONS_KEY",
    "MISSING_INPUT_PREFIX",
    "configuration_hash",
    "EXPORT_SCHEMA_VERSION",
    "PERSISTENCE_SCHEMA_VERSION",
    "IndependenceAssessment",
    "MetricOutcome",
    "MetricOutcomeRegistry",
    "OutcomeStatus",
    "ReproducibilityReceipt",
    "StatisticalUnit",
    "assess_independence",
    "build_reproducibility_receipt",
    "canonical_json",
    "hash_array",
    "hash_dataframe",
    "hash_file",
    "sha256_bytes",
]
