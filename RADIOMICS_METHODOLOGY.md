# QICOM radiomics methodology

## Scope and intended use

The radiomics workbench is a local research and quality-control tool. It is not a medical
device, does not provide clinical thresholds, and does not perform predictive feature selection.
Pairwise results describe one reference/comparison project. Formal repeatability is confined to
the multi-project cohort workflow and requires explicitly identified independent units.

## Image geometry

A DICOM volume's geometry is read in the order its pixels are stacked: the loader sorts series
files and multi-frame frames along the plane normal and hands each slice's position over in that
order, so origin and slice spacing describe the sorted volume. The slice spacing is the mean step
between consecutive positions, and the volume is refused when any step departs from it by more
than 1% of the spacing (at least 0.001 mm): a missing or irregular slice cannot be represented by
one spacing value, and resampling such a grid would place voxels where nothing was acquired. A
slice shifted within its own plane - a tilted gantry, a sheared stack - by more than 0.01 mm (or 1%
of the pixel spacing) is refused too: the grid puts every slice on the normal through the first.
So is a multi-frame object whose frames lie in different orientations, however regular their
positions: no single direction matrix places them.

## Versioned extraction profile

`RadiomicsConfig` is hashed as canonical JSON and records a profile name, id and version. The
default `qicom-ibsi-core@2.0` keeps normalization, clipping and resegmentation disabled. None of
these preprocessing methods is enabled silently.

The configuration records:

- physical target spacing and image interpolator; masks always use nearest-neighbour;
- dimensionality, image types and feature classes;
- fixed bin width/count and fixed-width origin (`roi-minimum` or an explicit absolute
  resegmentation lower bound);
- normalization (`none`, `z-score`, multiplicative `scale`, or percentile mapping), scale,
  optional outlier clipping and percentile definition;
- optional absolute or within-ROI sigma resegmentation;
- the primary percent-delta definition and near-zero epsilon.

Preprocessing provenance includes input/output geometry, voxel counts, effective bins, chosen
parameters and warnings. CT, MR and PET warnings are modality-specific but never change a value
or block a run by themselves.

## Pairwise delta and agreement

For a reference value `R` and comparison value `C`, QICOM reports:

- absolute delta: `C - R`;
- classic percent delta: `100 (C - R) / |R|` when `|R| > epsilon`;
- symmetric percent delta: `200 (C - R) / (|C| + |R|)` when the denominator exceeds epsilon.

The symmetric definition is the primary default in profile 2.0. The legacy `delta_percent`
column remains the classic value for project/export compatibility. Undefined near-zero cases
remain missing and carry an explicit status; they are never coerced to zero.

Within one pairwise project, QICOM reports counts and descriptive mean/median/absolute feature
differences. Multiple lesions or ROIs from one subject are not promoted to independent subjects,
and no project-level ICC/CCC is presented as formal inference.

## Mask QC and segmentation exchange

Masks are compared after transfer to a common physical grid. The separate mask-QC table contains
Dice, Jaccard, relative volume difference, centroid shift in millimetres, HD95, average symmetric
surface distance, physical overlap volume, border contact/minimum border distance, voxel counts
before/after transfer, geometry status and hashes.

NIfTI labelmaps must contain finite non-negative integer labels. Each non-zero label becomes one
ROI. Size, spacing, origin and direction are checked strictly. A mismatch is rejected unless the
user explicitly confirms nearest-neighbour resampling; the source/target geometries, interpolator
and hashes are then recorded. Original-space and preprocessed masks are exported with a JSON
sidecar containing label metadata, geometry, provenance and SHA-256 values. DICOM SEG and RTSTRUCT
are intentionally outside this iteration.

## Robustness and preprocessing sensitivity

Physical perturbations are configurable and deterministic: erosion, dilation and translation in
millimetres; small in-plane rotations in degrees; signed contour offsets in millimetres; and
seeded smooth contour jitter. Perturbation families remain separate. Each run performs real
PyRadiomics extraction for the baseline and every perturbation.

The per-case robustness summary reports range, median, MAD, robust CV, maximum absolute/percent
change, P05/P95 percent change, sensitivity per parameter unit and a status based only on an
optional user threshold. It explicitly contains no single-case perturbation ICC.

The Cartesian preprocessing sweep accepts any validated `RadiomicsConfig` field, including bin
width/count, spacing, interpolator, normalization, resegmentation and LoG sigma. Its primary
output is the full feature-by-configuration table. The descriptive sensitivity summary uses
`100 * range / |median|`; the percentage is undefined near zero, while absolute range and MAD
remain available. It performs no automatic clinical or predictive feature selection.

## Feature QC

Feature QC flags missing/non-finite values, near-zero variance, robust outliers, small ROIs or
insufficient effective bins, border contact, incompatible profile hashes, perturbation and
preprocessing sensitivity, and reference-verification coverage. Flags request review and
documentation; they never delete a feature. Mathematical ranges are used only when an exact range
is defined in feature metadata. QICOM does not invent universal clinical ranges.

## Cohort statistics

The cohort loader reads only the project manifest and derived CSV tables from each ZIP; it does
not load image pixels. All projects must have a compatible configuration/profile hash. The user
declares the independent unit (`subject`, `case`, `lesion`, `timepoint`, or `acquisition`), and
every required identifier must be complete. Repeated lesions are aggregated inside a subject
when subject is the declared unit.

At least three matched independent units and two measurements are required for ICC/CCC. QICOM
reports the two-way ICC design (`random` or `fixed`), definition (`agreement` or `consistency`),
unit (`single` or `mean`), notation, mean squares and effective sample size. The default is
two-way random-effects, absolute agreement, single measurement: ICC(A,1). Lin's CCC is decomposed
into precision, accuracy, location bias and scale bias. Accuracy is computed directly as
`2 * sd_x * sd_y / (var_x + var_y + (mean_x - mean_y)^2)`, so zero correlation with
positive variances does not make it undefined. Identical constant pairs have an undefined
accuracy term (zero denominator). SEM and MDC95 (`1.96 * sqrt(2) * SEM`)
are emitted only for a finite ICC in `[0, 1]`; otherwise they are undefined.

Pearson/Spearman matrices and effective-N matrices are exploratory. Volume correlations are
associations, not automatic proof of confounding. Benjamini-Hochberg is applied only to the real
cohort hypothesis p-values in that association table. Redundancy clusters expose separate
candidates for stability, missingness and volume association but never select a predictor.

## IBSI reference verification

GUI and CI share `qicom/radiomics/ibsi_validation.py`, the same manifest, loader, comparison
engine and tolerance rules. The bundled manifest maps 104 mandatory features but redistributes no
third-party phantom or spreadsheet. On explicit consent, resources are downloaded from their
official pinned locations, verified by SHA-256 and cached for fully offline reuse.

Current reference set:

- official IBSI data repository, commit
  `6da96021bc91faf4c0cb7fd7fa56a4225d2064a8`;
- digital phantom SHA-256
  `83773ac2a288aa93cf819a98eaf301c18d35d5876182ec9c18733184e2b7a83a`;
- mask SHA-256
  `3032d340944b577b83bc559ea7ae9a6034b84633a840eff39570ec17aa1d6281`;
- official IBSI-1 workbook SHA-256
  `3af20577b580f2161bd546e147338ff226abb1676488e2906f2ce908b7985710`.

The digital phantom and mask are CC BY 4.0. The workbook is downloaded rather than redistributed
because its download page does not state a machine-readable redistribution grant. The official
sources are the [IBSI site](https://theibsi.github.io/),
[IBSI-1 reference page](https://theibsi.github.io/ibsi1/), and
[official data repository](https://github.com/theibsi/data_sets).

This is a **digital phantom feature check**: it covers resource integrity, NIfTI loading and
geometry, label 1, the declared discretization, extraction and QICOM's derived IBSI shape rows.
It does not claim a full DICOM/NIfTI preprocessing-pipeline validation. PASS requires every
mandatory mapped feature to be comparable and within its manifest tolerance; a missing feature
produces INCOMPLETE. Near-zero references use the manifest epsilon and do not create an invalid
relative error.

`reference-verified` applies only to the exact reference-set version, image, mask, configuration,
tolerance and backend recorded in the receipt. Duplicating the check as an experiment unlocks the
configuration but irreversibly removes the reference verdict. QICOM never uses “IBSI certified”,
“clinically validated”, or equivalent wording.

## Reproducibility and exports

Every project can carry the common metric-outcome registry and receipt: QICOM/calculation schema
versions, the build revision the numbers came from (and whether that checkout was modified),
formula/profile versions, OS and backend versions, hashes of inputs/masks/configuration,
requested outcomes with readable and technical reasons, preflight warnings/diagnostics, declared
statistical unit, timestamp and result hash.

Radiomics exports include Excel, a CSV set and a local self-contained HTML report. The report has
a restrictive content-security policy, escapes user text, contains no remote content and enforces
a size limit. See [OPERATIONS_SCHEMAS.md](OPERATIONS_SCHEMAS.md) for schema versions and migrations.

## Deliberate limits

No automatic registration, deep-learning segmentation, universal clinical threshold, LASSO,
Rad-Score, clinical ROC/AUC or predictive model is implemented. Results remain research/QC
outputs that require design-aware interpretation and independent validation.
