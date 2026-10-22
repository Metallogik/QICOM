# Canonical BRISQUE, NIQE and RIQE in QICOM

QICOM is not a medical device. These quality scores are not clinical thresholds and
have not been calibrated for a diagnostic task.

## Five different descriptors

| Persistent ID | Display name | Definition |
| --- | --- | --- |
| `brisque` | NSS feature distance | Existing distance to QICOM's declared reference distribution |
| `niqe` | NSS patch-distribution distance | Existing pooled-covariance NSS distance |
| `brisque_canonical` | BRISQUE | PIQ 0.8.0's calibrated SVM score |
| `niqe_canonical` | NIQE | Independent QICOM implementation with the original LIVE pristine MVG |
| `riqe` | RIQE | NIQE-style distance from the CT-fitted `riqe-v1.0` model; CT in HU only |

The first two descriptors, functions, aliases, reference-model format, formulas and
directions are unchanged. Their synthetic 1/f default and optional local QA models
do not enter any canonical metric. Saved IDs are never remapped. The current
catalog contains 40 descriptors; `complete@1.2` includes all of them (`complete@1.1`
was the same set without `riqe`). Core and the QICOM Index component sets are
unchanged. RIQE is described in its own section below. `metric_inventory.csv` is the current
inventory; the inventory and experiments of the QICOM article remain frozen.

## Input and reporting contract

Both new functions accept a grayscale 2D image or a 3D stack, plus an explicit
`data_range`. The existing `category: no_reference` dispatch reports separate
`reference` and `comparison` rows. Both have `better_direction: lower` and no target.
No delta or bounded quality grade is manufactured. BRISQUE may fall outside the
usual 0–100 range. NIQE is a nonnegative distance.

The transform is calculated independently for each image:

```
lower = min(0, image.min())
scale = max(data_range, image.max() - lower)
normalized = (image - lower) / scale
```

It preserves inputs already in `[0, data_range]`, shifts negative inputs, and expands
an insufficient declared range. Only endpoint roundoff is clipped to `[0, 1]`.
The same transform is used across a volume's slices.

`data_range` here is the image's **own** range, as its loader reports it (the dtype or
`BitsStored` range of an integer image, the observed span of a floating-point one), passed
per role in the configuration key `no_reference_data_range`. It is never the range shared
by the pair: GUI, CLI and batch derive that one from both images, so a brighter partner
used to rescale the reference too and change its score. Changing a comparison partner
therefore cannot change the reference's score. A library caller that does not supply the
per-role ranges gets the shared `data_range` for both images, as before. Scores from different intensity transforms
must not be interpreted as measurements on the same displayed intensity scale.
The receipt records the transform separately for both roles.

Canonical scores always use the whole image. They do not use ROI bounding boxes,
tissue masks, artificial background filling, window/level display settings or the
local NSS reference model. NIQE's block truncation is part of its declared algorithm,
not a user ROI. The existing NSS/blur background-suppression behavior is preserved.

3D inputs use the existing slice aggregation: mean, population SD, robust summaries
and the existing confidence interval. Slices are not independent subjects. If a slice
is degenerate, the metric's volume result is invalid; slices are not silently dropped.
Role-specific outcomes preserve failures even when the other image has a valid score.

## Model acquisition and network boundary

`qicom/metrics/learned_weights.py` is the single manifest and downloader for both models:

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `brisque_svm_weights.pt` | 114997 | `e5b048ef5325bae6c57965cf077fadfa5834fcefb02e13611b4ea2d36cf3c52c` |
| `modelparameters.mat` | 8347 | `fa2aa4d12dd9595c3fb6bf8b4e1a15c79d5d1469238d0901c88dead195be578d` |

BRISQUE comes from the [PIQ v0.4.0 release asset](https://github.com/photosynthesis-team/piq/releases/download/v0.4.0/brisque_svm_weights.pt).
NIQE comes from [LIVE's official model](https://github.com/utlive/niqe/blob/d80aead297ff48b0254c39806f3a277b3ecbfc8d/modelparameters.mat),
at revision `d80aead297ff48b0254c39806f3a277b3ecbfc8d`.
The NIQE model is identified as `live-niqe-pristine-125-2012`: 125 natural pristine
images, 96×96 training blocks, training sharpness threshold 0.75.

The user downloads models explicitly from Select Metrics, or prepares them with:

```bash
python packaging/prepare_learned_weights.py --torch-home build/torch-home \
  --only brisque_svm_weights.pt --only modelparameters.mat
```

No model is fetched during calculation, including when a file is absent or corrupt.
Absent files yield `unavailable-backend`; corrupt files yield `failed` and are never
deserialized. Size and SHA-256 are checked before every load. The NIQE MAT reader
selects only the two numeric arrays and validates dimensions, finiteness, symmetry
and covariance positive semidefiniteness. Torch loads BRISQUE with `weights_only=True`
and validates the two SVM tensors.

The initial plan proposed bundling the tiny NIQE MAT file. This implementation instead
acquires it from its original publisher through the existing consent mechanism: QICOM
does not redistribute model bytes, relabel their license as Apache-2.0, or incorporate
LIVE MATLAB source. The release notice is preserved at
`packaging/notices/NIQE-LIVE.txt`, with its immutable source URL. The upstream README
permits use for any purpose with attribution; its wording refers to source and
documentation, so this implementation does not make an additional redistribution
claim about the MAT file.

## BRISQUE numerical implementation

The adapter calls PIQ 0.8.0's own BRISQUE computation, including feature scaling and
SVR. It uses float32, one channel, kernel size 7, sigma 7/6, input range 1 and no batch
reduction. Private Python function bindings replace only the scorer's URL loader.
PIQ and Torch global loaders are never patched by QICOM. Every score gets fresh SVM
tensors because PIQ transposes its support-vector tensor in place. No backend version
other than the validated PIQ 0.8.0 is accepted by this adapter.

The adapter requires a nonconstant image with both dimensions at least 14. Degenerate
NSS fits become invalid-input outcomes. PIQ/Torch imports are lazy; base installations
can still import and use the classical QICOM metrics and NIQE without Torch.

## NIQE numerical implementation

`niqe_canonical.py` was written independently from the statistical construction in
Mittal, Soundararajan and Bovik (2013), equations 1–10. Neither the legacy QICOM NSS
implementation nor external NIQE source is reused. The numeric conventions for
`niqe-live-36d-v1` are:

1. Float64 normalized grayscale intensities on the 0–255 scale, without additional
   integer quantization. Discard incomplete right/bottom 96-pixel blocks.
2. Global MSCN: normalized 7×7 Gaussian, sigma 7/6, replicated boundaries, denominator
   `local_sd + 1`. Absolute residual variance handles cancellation near constant values.
3. Nonoverlapping 96×96 blocks and corresponding 48×48 blocks at half resolution.
   Reduction uses a separable Keys cubic (`a = -1/2`), antialiasing, half-pixel centres
   and symmetric boundaries. All QICOM intermediates remain float64.
4. Eighteen ordered features per scale: marginal shape and average left/right scale;
   then shape, mean, left scale and right scale for horizontal, vertical and the two
   diagonal products. The asymmetric moment fit contains the symmetric GGD case.
   Shape is selected on `[0.2, 10]` with step 0.001. Product shifts wrap within a block.
   Scales are concatenated into a 36-dimensional vector.
5. No evaluation-time sharpness selection: the article's 0.75 selection threshold is
   a training convention. Undefined marginal fits produce missing features. Feature
   means use available finite observations; sample covariance (`ddof=1`) uses complete
   feature rows. At least two complete valid rows are required. One-block and constant
   images are invalid, rather than receiving a fabricated covariance or fallback.
6. Equation 10 uses the pseudoinverse of the pooled pristine/test covariance. No ridge
   penalty, synthetic model substitution or score clipping is added.

## Verification and its limits

`tests/metrics/test_canonical_no_reference.py` covers direct PIQ integration, offline
failure modes, repeated/concurrent loading, role-specific outcomes, normalization,
partner independence, whole-image support, stack aggregation and project/receipt
round-trips. It also checks NIQE's distance against an analytic diagonal-covariance
solution, symmetric moments and interpolation identities.

NIQE's full numerical oracle is a separately executed BasicSR grayscale implementation
at revision `8d56e3a045f9fb3e1d8872f92ee4a4f07f886b0a`, under
[Apache-2.0](https://github.com/XPixelGroup/BasicSR/blob/8d56e3a045f9fb3e1d8872f92ee4a4f07f886b0a/LICENSE.txt).
It is not a QICOM runtime/test dependency and its implementation is not included here.
Both executions use the exact same official MVG, float64 grayscale inputs and blocks.
BasicSR's resize internally uses float32; QICOM keeps float64. The frozen tolerance is
`atol=1e-4, rtol=1e-5`. Five QICOM-authored fixtures cover structure, blur, noise,
nonmultiple dimensions and the two-block minimum. Maximum observed absolute error was
`3.12e-5`. Inputs, source hashes, expected outputs and actual comparisons are stored in:

- `tests/fixtures/niqe/inputs.npz` and `reference_scores.json`;
- `validation/no_reference/niqe_verification.json`.

`validation/no_reference/compare_niqe_reference.py` regenerates only the independent
reference outputs when provided the pinned external source files, model and input NPZ.
It does not import QICOM's algorithm or reach the network. Example:

```bash
python validation/no_reference/compare_niqe_reference.py \
  /path/to/BasicSR/basicsr/metrics/niqe.py \
  /path/to/BasicSR/basicsr/utils/matlab_functions.py \
  /path/to/modelparameters.mat tests/fixtures/niqe/inputs.npz /tmp/niqe-reference.json
```

An additional natural-image check used BasicSR's `baboon.png`, converted to rounded
studio-range Y. QICOM returned 5.72996025 and the independently executed float64-input
oracle 5.72996537. BasicSR with float32 input returned 5.72957567; its documentation
[reports MATLAB R2021a 5.72957338](https://basicsr.readthedocs.io/en/latest/api/basicsr.metrics.niqe.html).
That published MATLAB execution was **not rerun here**. The dtype-dependent difference
is recorded, not hidden by asserting bitwise MATLAB equivalence. The natural image is
not redistributed. This verifies numerical implementation on the stated scope, not
perceptual accuracy on medical images or universal monotonicity under distortions.

## RIQE: a NIQE-style model fitted on CT

NIQE's distributed model is fitted on 125 photographs. On CT, the same construction
with photographic parameters can rank a noisier slice as closer to "natural": the RIQE
paper reports that a refit with the published NIQE parameters ranks every simulated
reduced-dose abdominal image better than its full-dose counterpart. RIQE (Radiology
Image Quality Evaluator; F. Mattiussi, *RIQE: a NIQE-style reference model for
Computed Tomography*, release `riqe-v1.0`,
[doi:10.5281/zenodo.23055559](https://doi.org/10.5281/zenodo.23055559),
code at <https://github.com/Metallogik/RIQE>) keeps NIQE's features and distance and
changes what defines the scale: a fixed HU mapping, field-of-view and body masks and a
model fitted on 3,792 full-dose slices of 158 patients of TCIA LDCT-and-Projection-data
v7 (CC BY 4.0).

### What it is for

On 40 held-out patients RIQE ranks simulated reduced-dose reconstructions worse than
the full-dose reconstruction of the same slice in 240/240 chest and 230/240 abdominal
pairs, and ranks added noise and blur worse than the source. Its preferences among
filtered images do not follow lesion signal: it prefers bilateral filtering at strengths
that leave 29% of the matched-filter signal of a 4 mm, +10 HU lesion and never prefers
Gaussian smoothing that leaves 70%. RIQE is therefore suited to **paired ranking** of a
degraded or processed image against its source within one protocol (anatomy, kernel,
slice thickness). It must not be the sole criterion to choose, compare or tune a
denoiser, and a lower score after processing does not mean a better image. The score is
a distance from the declared model, not comparable with NIQE values or across protocols.

### Input and reporting contract

`riqe` accepts a 2D slice or a 3D stack of axial slices in Hounsfield units, and scores
an image only when its unit is declared as `HU`:

- in the GUI, each panel declares HU when it is a DICOM whose Modality is CT, or a NIfTI
  file (the same rule as the HU readout; NIfTI carries no modality, so a non-CT NIfTI
  must not be scored with RIQE);
- in `qicom-cli confronta`, `--hounsfield-units` declares both inputs;
- in a quantitative batch job, `"hounsfield_units": true` declares both inputs;
- programmatically, `config["intensity_units"] = {"reference": "HU", "comparison": "HU"}`.

An undeclared image yields `invalid/degenerate-input` for its role, never a number. A
declaration is also checked against the pixels: a slice in which less than 1% of the field
of view is air (at or below -900 HU) is refused as not Hounsfield units, as is a mask source
slice that fails the same test. MR, bitmaps and unrescaled stored values have no such air, so a
non-CT NIfTI or a misused `--hounsfield-units` yields no score. On the public CT data the
smallest share of air was 3.5% of the field of view (fitting corpus 7.3%), over 7,224 slices
including the ends of every full-dose series; none fell below the threshold.
Rows are reported per role, lower is better, with no target. As in the RIQE validation,
the reference is scored on its own field-of-view and body masks and **the comparison is
scored on the reference's masks**: a degradation must not change the scored domain, and
the body mask of a noisy low-dose slice fragments (a 10%-dose chest slice scored on its
own masks can have no valid patch at all). The comparison is therefore scored only when
the reference is declared HU too, and its score belongs to the pair; in an N-image sweep
the calculation cache keys it by both images. The pair must be co-registered, as for
every reference-based metric. User ROIs and background exclusion do not apply.

A slice is refused when fewer than 72 patches (twice the feature dimension) lie entirely
in the field of view and at least 90% in the body. A 3D stack is invalid if any slice is,
as for the other canonical scores; score single slices with the current-slice resolution
when the ends of a volume have too little body.

### Numerical implementation

`qicom/metrics/riqe.py` reproduces the released scoring path (`riqe/hu.py`, `nss.py`,
`extract.py`, `model.py` at revision `73a1d42`) operation by operation:

1. HU as float32; `L = 255 * (clip(HU, -1000, 1000) + 1000) / 2000`, float32, not rounded.
2. Field of view `HU >= -1024`; body `HU > -300` inside it, 5x5 opening, largest
   connected component, holes filled.
3. MSCN with a separable 7x7 Gaussian (sigma 1.0, reflective boundaries) and `C = 0.01`.
4. Non-overlapping 24x24 patches and the corresponding 12x12 patches after the same
   Gaussian and 2:1 decimation; masks are halved by requiring all four pixels. Moment
   matching inverts GGD/AGGD ratios by linear interpolation on 40,001 shape values in
   `[0.05, 20]`; neighbour products use reflective extension. 36 features per patch.
5. No sharpness selection at scoring time. Equation 10 of NIQE with the sample
   covariance of the valid patches and a pseudo-inverse with `rcond = 1e-10`.

Three declared differences from the reference code. QICOM does not carry the DICOM
PixelPaddingValue, so the field of view always uses the reference fallback `HU >= -1024`;
on GE slices padded at -3024 HU the result is identical (see below). And odd sizes on
which the reference code itself fails (its half-scale image and mask block grids differ,
for example 311 rows) are refused with an explicit error instead of an array error. And
slices without air are refused as not HU (above); the reference code would score them.

### Model distribution

The model is the released artifact `riqe-v1.0.npz` (15,452 bytes, SHA-256
`b1a4ffdf8288c7c6667fd3b0ca674756c69e5f1c85e9b5a53d596bf19b32f595`), bundled unmodified
in `qicom/metrics/models/` under CC BY 4.0. Unlike the LIVE NIQE parameters it may be
redistributed with attribution, so no download or network access is involved. Size and
SHA-256 are checked before every load; only the arrays `nu` and `sigma` are read, without
pickle, and validated for shape, finiteness, exact symmetry and positive definiteness.
The licence and required attributions are in `packaging/notices/RIQE-MODEL.txt`. The
receipt records, under `canonical_no_reference.models.riqe`, the model identity, the
mapping, masks, patch parameters, formula version `riqe-ct-36d-v1` and the declared
unit of each role.

### Verification and its limits

- `tests/fixtures/riqe/`: synthetic HU slices written by
  `validation/no_reference/make_riqe_inputs.py` and scored by the released code through
  `validation/no_reference/compare_riqe_reference.py`, which pins the reference sources
  by SHA-256 and does not import QICOM. `tests/metrics/test_riqe.py` requires agreement
  within `rtol = 1e-9`; on the recorded environment the scores are bit-identical.
- `validation/no_reference/riqe_verification.json`, from
  `validation/no_reference/verify_riqe_tcia.py` on the public TCIA data: all 480
  reduced-dose test pairs of the paper, read with QICOM's DICOM loader, are bit-identical
  to the reference scoring API on the same masks; the maximum relative deviation from the
  published scores is 5.2e-8, because the paper's pipeline caches features in float32;
  the paper's 240/240 and 230/240 are reproduced; on 20 GE slices the reference reading
  PixelPaddingValue and QICOM agree exactly; the air shares that justify the HU check are
  recorded in the same file. Patient data are not redistributed.

This verifies the implementation, not diagnostic performance: no reader study was
performed, the reduced-dose images are simulated, and the paper's limitations apply.

## Attribution and provenance

The existing citation audit distinguishes method papers from software. The added rows
follow the same convention without editing the article's frozen records:

- BRISQUE method (`brisque`): A. Mittal, A. K. Moorthy and A. C. Bovik,
  *No-Reference Image Quality Assessment in the Spatial Domain*, IEEE TIP 21(12),
  4695–4708 (2012), [doi:10.1109/TIP.2012.2214050](https://doi.org/10.1109/TIP.2012.2214050).
- BRISQUE implementation (`piq`, `piqsoftware`): S. Kastryulin, J. Zakirov,
  D. Prokopenko and D. V. Dylov, *PyTorch Image Quality: Metrics for Image Quality
  Assessment* (2022), [doi:10.48550/arXiv.2208.14818](https://doi.org/10.48550/arXiv.2208.14818);
  [PIQ 0.8.0](https://github.com/photosynthesis-team/piq/tree/v0.8.0), Apache-2.0.
- NIQE method (`niqe`): A. Mittal, R. Soundararajan and A. C. Bovik,
  *Making a “Completely Blind” Image Quality Analyzer*, IEEE SPL 20(3), 209–212
  (2013), [doi:10.1109/LSP.2012.2227726](https://doi.org/10.1109/LSP.2012.2227726).
- NIQE parameters: Mittal, Soundararajan and Bovik, *NIQE Software Release*, LIVE,
  UT Austin (2012); immutable model URL and release notice above.
- RIQE method and model (`riqe`): F. Mattiussi, *RIQE: a NIQE-style reference model for
  Computed Tomography* (2026), study [arXiv:2610.00384](https://arxiv.org/abs/2610.00384);
  model release `riqe-v1.0`,
  [doi:10.5281/zenodo.23055559](https://doi.org/10.5281/zenodo.23055559). Model corpus:
  C. McCollough et al., *Low Dose CT Image and Projection Data* (version 7), TCIA (2020),
  [doi:10.7937/9npb-2637](https://doi.org/10.7937/9npb-2637); K. Clark et al., *The Cancer
  Imaging Archive (TCIA)*, J Digit Imaging 26(6), 1045–1057 (2013). NIBIB grants
  EB017095 and EB017185 (PI: Cynthia McCollough).

The existing receipt's extensible configuration contains `canonical_no_reference`
only when a new score is requested: model URL/size/digest, algorithm version, dtype,
parameters, normalization by role and spatial support. BRISQUE also records installed
PIQ/Torch versions in the existing backend dictionary. Existing JSON canonicalization,
hash functions and persistence schemas are unchanged. Historical receipts are not
rewritten; newly generated receipts naturally include the expanded outcome catalog.
