"""State/orchestration for the dedicated radiomics application window."""

from __future__ import annotations

import dataclasses
import json
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from qicom.cancellation import cancellation_scope, check_cancelled
from qicom.dataframe_snapshots import dataframe_snapshot
from qicom.gui.revision_cache import RevisionClock
from qicom.gui.workers import run_in_thread_and_wait
from qicom.i18n import translator
from qicom.provenance import (
    MetricOutcome,
    OutcomeStatus,
    StatisticalUnit,
    build_reproducibility_receipt,
)
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.extraction import (
    FEATURE_COLUMNS,
    PROVENANCE_COLUMNS,
    RadiomicsExtractionSession,
    estimated_preprocessed_image_bytes,
    provenance_dataframe,
)
from qicom.radiomics.feature_qc import feature_quality_flags
from qicom.radiomics.io import image_content_hash, load_radiomics_image
from qicom.radiomics.mask_qc import MASK_QC_COLUMNS, paired_mask_qc
from qicom.radiomics.models import RadiomicsImage, RadiomicsRoi, RadiomicsStudyIdentity
from qicom.radiomics.project import (
    RadiomicsProjectData,
    load_radiomics_project,
    save_radiomics_project,
    utc_now,
)
from qicom.radiomics.reporting import export_radiomics_csv_set, export_radiomics_excel
from qicom.radiomics.reproducibility import (
    DELTA_COLUMNS,
    PAIRWISE_AGREEMENT_COLUMNS,
    analyze_pair,
    feature_correlation_matrix,
    pairwise_feature_agreement,
)
from qicom.reporting.tidy import export_self_contained_html
from qicom.runtime import MEBIBYTE, detect_runtime_profile, resolve_cache_budget


@dataclasses.dataclass(frozen=True, slots=True)
class RadiomicsMemoryEstimate:
    """Image/mask memory visible before a PyRadiomics run begins."""

    source_bytes: int
    roi_mask_bytes: int
    prepared_image_bytes: int
    cache_budget_bytes: int
    estimated_peak_bytes: int


class RadiomicsController:
    """Owns only radiomics state; never calls the quantitative metric runner.

    Mutating the analysis inputs (images, configuration, masks) never
    deletes an extraction's results - it bumps ``revision``, which makes
    ``results_stale`` true.  The two concerns used to be fused: every
    ``add_roi`` call cleared every result table, so the mask
    resynchronisation that ``save``/``extract`` run beforehand silently
    wiped a finished extraction and wrote empty tables to disk.  Results
    are the user's data: they are labelled outdated, never discarded
    behind their back, and only actually reset when the images they
    describe are themselves replaced.
    """

    def __init__(self) -> None:
        self.reference: RadiomicsImage | None = None
        self.comparison: RadiomicsImage | None = None
        self.rois: list[RadiomicsRoi] = []
        self.config = RadiomicsConfig()
        self.identity = RadiomicsStudyIdentity()
        self.notes: str | None = None
        self.created_at: str | None = None
        self.project_path: Path | None = None
        self.dirty = False
        # Monotonic counter over every genuine change to the analysis
        # inputs, compared against the revision the current results were
        # produced at - see results_stale/mark_results_current.
        self._revision = RevisionClock()
        self._image_revision = RevisionClock()
        self._results_revision = 0
        self._results_config: RadiomicsConfig | None = None
        self._reset_result_tables()

    def reset(self) -> None:
        self.reference = None
        self.comparison = None
        self.rois = []
        self.config = RadiomicsConfig()
        self.identity = RadiomicsStudyIdentity()
        self.notes = None
        self.created_at = None
        self.project_path = None
        self.dirty = False
        self._revision.reset()
        self._image_revision.reset()
        self._results_revision = 0
        self._results_config = None
        self._reset_result_tables()

    # -- results lifecycle ----------------------------------------------

    def _reset_result_tables(self) -> None:
        """Drop every result table - only for state where keeping them
        would be meaningless (a new project, or images replaced under the
        existing masks), never as a side effect of an ordinary edit."""
        self.features = pd.DataFrame(columns=FEATURE_COLUMNS)
        self.delta = pd.DataFrame(columns=DELTA_COLUMNS)
        self.repeatability = pd.DataFrame(columns=PAIRWISE_AGREEMENT_COLUMNS)
        self.correlation = pd.DataFrame(columns=["feature_key"])
        self.provenance = pd.DataFrame(columns=PROVENANCE_COLUMNS)
        self.mask_qc = pd.DataFrame(columns=MASK_QC_COLUMNS)
        self.feature_qc = pd.DataFrame()
        self.robustness = pd.DataFrame()
        self.preprocessing_sweep = pd.DataFrame()
        self.validation_status = pd.DataFrame()
        self.outcomes = pd.DataFrame(
            columns=["metric_id", "status", "reason", "technical_reason", "scope"]
        )
        self.reproducibility_receipt: dict[str, object] = {}

    def _record_input_change(self) -> None:
        self._revision.bump()
        self.dirty = True

    @property
    def revision(self) -> int:
        """Monotonic public revision of the current analysis inputs."""
        return self._revision.value

    @property
    def results_stale(self) -> bool:
        """True when results exist but were computed from inputs that have
        since changed - the GUI shows them with an explicit warning and
        blocks export until the extraction is re-run.

        The profile is compared by value rather than counted, so editing a
        setting and putting it back does not leave the results permanently
        branded as outdated; masks stay on the conservative counter, since
        a shape dragged away and back is not reliably the same mask.
        """
        if self.features.empty:
            return False
        return self.revision != self._results_revision or self.config != self._results_config

    def note_external_edit(self) -> None:
        """Record a change the GUI holds outside the controller - a live
        manual ROI dragged on the canvas, whose mask is only handed back
        at extract/save time."""
        self._record_input_change()

    def mark_results_current(self) -> None:
        """Declare the current result tables as matching the current
        inputs (a finished extraction, or a project loaded from disk)."""
        self._results_revision = self.revision
        self._results_config = self.config

    @staticmethod
    def read_image(path: str | Path, *, series_identifier: str | None = None) -> RadiomicsImage:
        return load_radiomics_image(path, series_identifier=series_identifier)

    def set_image(self, role: str, image: RadiomicsImage) -> None:
        if role == "reference":
            self.reference = image
        elif role == "comparison":
            self.comparison = image
        else:
            raise ValueError(f"Unknown radiomics image role {role!r}.")
        # Every mask/result belongs to the previous pair's physical grids -
        # the one case where discarding results really is the honest
        # outcome. RadiomicsMainWindow confirms this with the user first.
        self.rois.clear()
        self._reset_result_tables()
        self._image_revision.bump()
        self._record_input_change()

    def _validate_roi(self, roi: RadiomicsRoi, *, existing: Sequence[RadiomicsRoi]) -> None:
        if any(other.roi_id == roi.roi_id for other in existing):
            raise ValueError(f"A radiomics ROI with id {roi.roi_id!r} already exists.")
        if self.reference is None or self.comparison is None:
            raise ValueError("Load both radiomics images before adding a paired ROI.")
        if roi.reference_mask.shape != self.reference.pixels.shape:
            raise ValueError("Reference ROI mask does not match the reference image.")
        if roi.comparison_mask.shape != self.comparison.pixels.shape:
            raise ValueError("Comparison ROI mask does not match the comparison image.")

    def add_roi(self, roi: RadiomicsRoi) -> None:
        self._validate_roi(roi, existing=self.rois)
        self.rois.append(roi)
        self._record_input_change()

    @staticmethod
    def _rois_equivalent(first: Sequence[RadiomicsRoi], second: Sequence[RadiomicsRoi]) -> bool:
        """Whether two ROI lists describe the same analysis inputs.

        The GUI re-materialises every manual ROI's mask from its live
        graphics item before each save and each extraction, so the objects
        handed to replace_rois() are always new even when the user has
        changed nothing.  Comparing content - not identity - is what keeps
        an ordinary save from being mistaken for an edit.
        """
        if len(first) != len(second):
            return False
        for left, right in zip(first, second, strict=True):
            if (left.roi_id, left.name, left.method) != (right.roi_id, right.name, right.method):
                return False
            if not np.array_equal(left.reference_mask, right.reference_mask):
                return False
            if not np.array_equal(left.comparison_mask, right.comparison_mask):
                return False
        return True

    def replace_rois(self, rois: Sequence[RadiomicsRoi]) -> None:
        """Swap in a whole ROI list, atomically.

        Every ROI is validated against the loaded images before anything
        is committed: a single invalid mask used to leave the controller
        holding whichever prefix of the list happened to validate first,
        with the rest silently gone.
        """
        accepted: list[RadiomicsRoi] = []
        for roi in rois:
            self._validate_roi(roi, existing=accepted)
            accepted.append(roi)
        if self._rois_equivalent(accepted, self.rois):
            self.rois = accepted
            return
        self.rois = accepted
        self._record_input_change()

    def rename_roi(self, roi_id: str, name: str) -> None:
        """Rename a ROI without invalidating the results.

        A name is a label, not an analysis input - re-extracting because
        of one would be disproportionate. The already-computed tables are
        relabelled in place instead, so they never end up showing a name
        that no longer exists anywhere in the project.
        """
        renamed = False
        for index, roi in enumerate(self.rois):
            if roi.roi_id == roi_id and roi.name != name:
                self.rois[index] = dataclasses.replace(roi, name=name)
                renamed = True
        for table in (self.features, self.delta):
            if not table.empty and {"roi_id", "roi_name"} <= set(table.columns):
                matching = table["roi_id"] == roi_id
                if bool(matching.any()):
                    table.loc[matching, "roi_name"] = name
                    renamed = True
        if renamed:
            self.dirty = True

    def remove_roi(self, roi_id: str) -> None:
        previous = len(self.rois)
        self.rois = [roi for roi in self.rois if roi.roi_id != roi_id]
        if len(self.rois) != previous:
            self._record_input_change()

    def set_config(self, config: RadiomicsConfig) -> None:
        if config == self.config:
            return
        self.config = config
        # No revision bump: staleness compares the profile by value (see
        # results_stale), so returning a setting to what produced the
        # current results clears the warning instead of latching it.
        self.dirty = True

    def set_identity(self, identity: RadiomicsStudyIdentity) -> None:
        if identity == self.identity:
            return
        self.identity = identity
        # Identity controls cohort interpretation/provenance, not extraction values.
        self.dirty = True

    def analyze(
        self,
        *,
        max_cached_image_bytes: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        step_callback: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Extract every ROI; `should_cancel` stops it between ROIs and images.

        A cancelled run raises ComputationCancelled before any result is
        stored, so the previous results stay exactly as they were.
        """
        if self.reference is None or self.comparison is None:
            raise ValueError("Load both a reference and comparison DICOM/NIfTI image.")
        if not self.rois:
            raise ValueError("Define at least one radiomics ROI/VOI before extraction.")
        feature_tables: list[pd.DataFrame] = []
        delta_tables: list[pd.DataFrame] = []
        provenance_tables: list[pd.DataFrame] = []
        mask_qc_tables: list[pd.DataFrame] = []
        total = len(self.rois)
        with (
            cancellation_scope(should_cancel),
            RadiomicsExtractionSession(
                self.config, max_cached_image_bytes=max_cached_image_bytes
            ) as session,
        ):
            for index, roi in enumerate(self.rois, start=1):
                check_cancelled()
                if step_callback is not None:
                    step_callback(translator.tr("radiomics.status.extracting_roi", name=roi.name))
                result = analyze_pair(
                    self.reference,
                    self.comparison,
                    roi.reference_mask,
                    self.config,
                    comparison_mask=roi.comparison_mask,
                    roi_id=roi.roi_id,
                    roi_name=roi.name,
                    session=session,
                    reference_revision=f"reference:{self._image_revision.value}",
                    comparison_revision=f"comparison:{self._image_revision.value}",
                )
                feature_tables.append(result.features)
                delta_tables.append(result.delta)
                provenance_tables.append(
                    provenance_dataframe([result.reference_features, result.comparison_features])
                )
                mask_qc_tables.append(
                    paired_mask_qc(
                        roi.reference_mask,
                        self.reference,
                        roi.comparison_mask,
                        self.comparison,
                        roi_id=roi.roi_id,
                    )
                )
                if progress_callback is not None:
                    progress_callback(index, total)
            check_cancelled()
        self.features = pd.concat(feature_tables, ignore_index=True)
        self.delta = pd.concat(delta_tables, ignore_index=True)
        self.repeatability = pairwise_feature_agreement(self.features)
        self.correlation = feature_correlation_matrix(self.features)
        self.provenance = pd.concat(provenance_tables, ignore_index=True)
        self.mask_qc = pd.concat(mask_qc_tables, ignore_index=True)
        self.feature_qc = feature_quality_flags(
            self.features,
            robustness=self.robustness,
            preprocessing_sweep=self.preprocessing_sweep,
            preprocessing_qc=self.provenance,
            mask_qc=self.mask_qc,
            minimum_roi_voxels=self.config.minimum_roi_voxels,
        )
        feature_keys = tuple(sorted(set(self.features["feature_key"].astype(str))))
        outcome_records = tuple(
            MetricOutcome(
                feature_key,
                OutcomeStatus.COMPUTED,
                "The requested radiomics feature was computed.",
                scope="radiomics-feature",
            )
            for feature_key in feature_keys
        )
        self.outcomes = pd.DataFrame(
            [item.to_dict() for item in outcome_records],
            columns=["metric_id", "status", "reason", "technical_reason", "scope"],
        )
        warnings_list: list[str] = []
        for payload in self.provenance.get("preprocessing_qc_json", pd.Series(dtype=str)).dropna():
            try:
                parsed = json.loads(str(payload))
            except (TypeError, ValueError):
                continue
            warnings_list.extend(str(value) for value in parsed.get("warnings", ()))
        unit = StatisticalUnit(
            subject_id=self.identity.subject_id or None,
            case_id=self.identity.case_id or None,
            acquisition_id=self.identity.acquisition_id or None,
            timepoint=self.identity.timepoint_id or None,
            lesion_id=self.identity.lesion_id or None,
            roi_id=self.rois[0].roi_id if len(self.rois) == 1 else None,
        )
        receipt = build_reproducibility_receipt(
            inputs={
                "reference": self.reference.pixels,
                "comparison": self.comparison.pixels,
            },
            masks={f"{roi.roi_id}:reference": roi.reference_mask for roi in self.rois}
            | {f"{roi.roi_id}:comparison": roi.comparison_mask for roi in self.rois},
            configuration={
                **self.config.to_dict(),
                "reference_geometry": self.reference.geometry.to_dict(),
                "comparison_geometry": self.comparison.geometry.to_dict(),
            },
            requested_metrics=feature_keys,
            outcomes=outcome_records,
            warnings=warnings_list,
            statistical_unit=unit,
            formula_versions={"radiomics_profile": self.config.profile_version},
            results=self.features,
        )
        self.reproducibility_receipt = receipt.to_dict()
        self.reproducibility_receipt["input_hashes"] = {
            "reference": image_content_hash(self.reference),
            "comparison": image_content_hash(self.comparison),
        }
        self.mark_results_current()
        self.dirty = True
        return (
            self.features,
            self.delta,
            self.repeatability,
            self.correlation,
            self.provenance,
        )

    def estimate_extraction_memory(
        self,
        max_cached_image_bytes: int | None = None,
        *,
        available_bytes: int | None = None,
    ) -> RadiomicsMemoryEstimate:
        """Estimate the image/mask peak and resolve the adaptive cache cap."""
        if self.reference is None or self.comparison is None:
            raise ValueError("Load both a reference and comparison DICOM/NIfTI image.")
        prepared_sizes = (
            estimated_preprocessed_image_bytes(self.reference, self.config),
            estimated_preprocessed_image_bytes(self.comparison, self.config),
        )
        source_bytes = int(self.reference.pixels.nbytes + self.comparison.pixels.nbytes)
        roi_mask_bytes = sum(
            int(roi.reference_mask.nbytes + roi.comparison_mask.nbytes) for roi in self.rois
        )
        prepared_image_bytes = sum(prepared_sizes)
        if max_cached_image_bytes is None:
            max_cached_image_bytes = (
                detect_runtime_profile().resources.radiomics_cache_mib * MEBIBYTE
            )
        cache_budget = resolve_cache_budget(max_cached_image_bytes, available_bytes=available_bytes)
        # Source images and ROI masks are already resident. During extraction
        # both prepared float32 grids can overlap with one active uint8 mask;
        # PyRadiomics' feature matrices are data-dependent and intentionally
        # excluded from this checkable image/mask estimate.
        active_resampled_mask = max(prepared_sizes) // np.dtype(np.float32).itemsize
        return RadiomicsMemoryEstimate(
            source_bytes=source_bytes,
            roi_mask_bytes=roi_mask_bytes,
            prepared_image_bytes=prepared_image_bytes,
            cache_budget_bytes=cache_budget,
            estimated_peak_bytes=(
                source_bytes + roi_mask_bytes + prepared_image_bytes + active_resampled_mask
            ),
        )

    def project_data(self) -> RadiomicsProjectData:
        if self.reference is None or self.comparison is None:
            raise ValueError("Both radiomics images are required to save a project.")
        return RadiomicsProjectData(
            reference=self.reference,
            comparison=self.comparison,
            rois=list(self.rois),
            config=self.config,
            features=dataframe_snapshot(self.features),
            delta=dataframe_snapshot(self.delta),
            notes=self.notes,
            provenance=dataframe_snapshot(self.provenance),
            repeatability=dataframe_snapshot(self.repeatability),
            correlation=dataframe_snapshot(self.correlation),
            results_stale=self.results_stale,
            identity=self.identity,
            mask_qc=dataframe_snapshot(self.mask_qc),
            feature_qc=dataframe_snapshot(self.feature_qc),
            robustness=dataframe_snapshot(self.robustness),
            preprocessing_sweep=dataframe_snapshot(self.preprocessing_sweep),
            validation_status=dataframe_snapshot(self.validation_status),
            outcomes=dataframe_snapshot(self.outcomes),
            reproducibility_receipt=dict(self.reproducibility_receipt),
            created_at=self.created_at,
        )

    def save_project(self, path: str | Path | None = None) -> Path:
        destination = self.project_path if path is None else Path(path)
        if destination is None:
            raise ValueError("Choose a destination for the radiomics project.")
        if self.created_at is None:
            self.created_at = utc_now()  # the first save creates the project
        data = self.project_data()
        saved = run_in_thread_and_wait(save_radiomics_project, destination, data)
        self.project_path = saved
        self.dirty = False
        return saved

    def load_project(self, path: str | Path) -> RadiomicsProjectData:
        """Load synchronously; deprecated in favor of the worker-friendly split API."""
        warnings.warn(
            "RadiomicsController.load_project() is deprecated; use "
            "load_radiomics_project() followed by apply_project_data() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        data = load_radiomics_project(path)
        self.apply_project_data(data, path)
        return data

    def apply_project_data(self, data: RadiomicsProjectData, path: str | Path) -> None:
        self._image_revision.bump()
        self.reference = data.reference
        self.comparison = data.comparison
        self.rois = list(data.rois)
        self.config = data.config
        self.identity = data.identity
        self.features = data.features
        self.delta = data.delta
        self.repeatability = (
            pairwise_feature_agreement(self.features)
            if data.repeatability.empty
            else data.repeatability
        )
        self.correlation = (
            feature_correlation_matrix(self.features)
            if data.correlation.empty
            else data.correlation
        )
        self.provenance = data.provenance
        self.mask_qc = data.mask_qc
        self.feature_qc = data.feature_qc
        self.robustness = data.robustness
        self.preprocessing_sweep = data.preprocessing_sweep
        self.validation_status = data.validation_status
        self.outcomes = data.outcomes
        self.reproducibility_receipt = dict(data.reproducibility_receipt)
        self.notes = data.notes
        self.created_at = data.created_at
        self.project_path = Path(path)
        # The loaded tables describe the loaded inputs, unless the project
        # was explicitly saved in a stale state - in which case reopening
        # it must keep saying so rather than quietly blessing the numbers.
        self.mark_results_current()
        if data.results_stale:
            self._revision.bump()
        self.dirty = False

    def export_excel(self, path: str | Path) -> Path:
        data = self.project_data()
        if data.features.empty:
            raise ValueError("Run radiomics extraction before exporting results.")
        return run_in_thread_and_wait(
            export_radiomics_excel,
            path,
            features=data.features,
            delta=data.delta,
            config=data.config,
            reference=data.reference,
            comparison=data.comparison,
            rois=data.rois,
            repeatability=data.repeatability,
            correlation=data.correlation,
            provenance=data.provenance,
            mask_qc=data.mask_qc,
            feature_qc=data.feature_qc,
            robustness=data.robustness,
            preprocessing_sweep=data.preprocessing_sweep,
            validation_status=data.validation_status,
            outcomes=data.outcomes,
            reproducibility_receipt=data.reproducibility_receipt,
        )

    def export_csv(self, directory: str | Path, base_name: str) -> list[Path]:
        data = self.project_data()
        if data.features.empty:
            raise ValueError("Run radiomics extraction before exporting results.")
        return run_in_thread_and_wait(
            export_radiomics_csv_set,
            directory,
            base_name,
            features=data.features,
            delta=data.delta,
            config=data.config,
            reference=data.reference,
            comparison=data.comparison,
            rois=data.rois,
            repeatability=data.repeatability,
            correlation=data.correlation,
            provenance=data.provenance,
            mask_qc=data.mask_qc,
            feature_qc=data.feature_qc,
            robustness=data.robustness,
            preprocessing_sweep=data.preprocessing_sweep,
            validation_status=data.validation_status,
            outcomes=data.outcomes,
            reproducibility_receipt=data.reproducibility_receipt,
        )

    def export_html(self, path: str | Path) -> Path:
        """Write a local, self-contained research/QC report."""
        data = self.project_data()
        if data.features.empty:
            raise ValueError("Run radiomics extraction before exporting results.")
        warning_payload = data.reproducibility_receipt.get("preflight_warnings", ())
        warnings_list = (
            [str(value) for value in warning_payload]
            if isinstance(warning_payload, list | tuple)
            else []
        )
        return run_in_thread_and_wait(
            export_self_contained_html,
            path,
            title="QICOM radiomics research/QC report",
            sections={
                "Radiomics features": data.features,
                "Delta (descriptive pairwise)": data.delta,
                "Pairwise agreement (descriptive)": data.repeatability,
                "Feature correlation (exploratory)": data.correlation,
                "Mask QC": data.mask_qc,
                "Feature QC": data.feature_qc,
                "Robustness": data.robustness,
                "Preprocessing sweep": data.preprocessing_sweep,
                "Validation status": data.validation_status,
                "Metric outcomes": data.outcomes,
            },
            methodology=(
                "Pairwise delta and agreement are descriptive. Formal ICC/CCC belongs to the "
                "multi-project cohort workflow with at least three explicitly identified "
                "independent units. The primary percent delta is symmetric unless the stored "
                "profile explicitly selects the classic reference-denominator definition. "
                "No result is a clinical threshold or an automatic feature selection."
            ),
            warnings=warnings_list,
            profile=data.config.to_dict(),
            provenance=data.reproducibility_receipt,
        )
