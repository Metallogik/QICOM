# QICOM — Quality Image Comparator

Desktop application with three separate workflows for medical images: quantitative analysis,
qualitative evaluation and pairwise 2D/3D radiomics on DICOM/NIfTI; alongside them, a collection
of utility tools (segmentation, masks, conversion, DICOM headers, anonymization).

> **For research and quality-control use only.** QICOM is not a medical device and is not
> intended for clinical diagnosis or for any decision concerning a patient's care.
> This notice is also shown in the application's launch window
> (`qicom/gui/widgets/launcher_dialog.py`) and in **Help ▸ About QICOM**.

QICOM supports DICOM® medical images. DICOM® is the registered trademark of the National
Electrical Manufacturers Association (NEMA) for its standards publications relating to digital
communications of medical information. QICOM is an independent software project and is not
affiliated with, sponsored by, or endorsed by NEMA or the DICOM Standards Committee.

Conceived and developed by Fabio Mattiussi in Lugano, Switzerland. Licensed under
[Apache-2.0](LICENSE).

## Documentation

| For | Go to |
| --- | --- |
| Launching and using QICOM | [Starting the GUI](#gui), [tester guide (Italian)](packaging/LEGGIMI-TESTER.txt) |
| Understanding the analyses | [Metrics](#implemented-metrics), [qualitative methodology](QUALITATIVE_METHODOLOGY.md), [radiomics methodology](RADIOMICS_METHODOLOGY.md) |
| Sharing results and knowing the limits | [AI export](#ai-assistant-export), [security and privacy](SECURITY.md), [intended use](#intended-use) |
| What's new | [Changelog](CHANGELOG.md) |
| Developing QICOM | [Installation](#installation-development), [data formats](OPERATIONS_SCHEMAS.md), [tests](#tests), [benchmarks](benchmarks/README.md) |

## Installation (development)

Requires **Python >=3.11** and, for the GUI, **PySide6-Essentials >=6.6,<7 (Qt 6)**.
CI covers Python 3.11/3.12; the bundles use 3.12 and include the interpreter.
The bundle targets are Windows x64, macOS 14 Apple Silicon and Linux x64.
Windows 10 from 1809 and Windows 11 are expected targets, not two desktop environments
verified separately from the Windows Server runner. On Linux the bundle also depends on
the glibc and system libraries of the machine it is built on.
Dependency minimums, tested platforms and the limits of the evidence are set out separately in
[Compatibility and verification matrix](packaging/DISTRIBUTION.md#compatibility-boundary).
Hardware profiles do not lower these requirements and there is no Qt 5 fallback.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Before any dependency: pip is what downloads and runs the build backends,
# so it is the first thing that has to be trustworthy. Pinning the versions of
# what it installs is pointless if the installer itself is vulnerable.
python -m pip install --upgrade "pip>=26.2"

pip install -e ".[dev]"          # core + development tools
pip install -e ".[gui]"          # + graphical interface
pip install -e ".[radiomics]"   # + PyRadiomics backend for the dedicated module
pip install -e ".[radiomics,mirp]"  # + optional MIRP backend for full IBSI 1
pip install -e ".[learned]"      # + learned metrics (LPIPS, DISTS, piq) — heavy, optional
```

`packaging/generate_constraints.py` records the exact manifest of the installed environment
that PyInstaller is about to freeze: full versions (including local tags such as `+cpu`) and,
for VCS dependencies such as PyRadiomics, repository and commit. It does not run a second
resolution that could use an index different from the build's. The file is build evidence,
**not** a hash-pinned artifact lock, and must not be used with `--require-hashes`; the
integrity of the published files is covered separately by `SHA256SUMS`. The workflow
generates it on each platform and publishes it alongside the bundle.

For the learned metrics, the CPU-only build of torch is recommended on all platforms:

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
python packaging/prepare_learned_weights.py
```

The second command is intentionally explicit: it downloads AlexNet/VGG16/DISTS, checks each
file against its expected SHA-256 and installs it in the Torch cache. QICOM never downloads
models during an analysis; if a checkpoint is missing or altered, the metric is omitted without
any network access.

**Checkpoints are not included in any bundle**, learned bundles included: they weigh about
760 MB, more than the rest of the application, and the policy is "on request, with explicit
consent". A learned bundle contains the libraries (torch, torchvision, lpips, piq), so the five
metrics that use no weights (MS-SSIM, FSIM, GMSD, VIF, HaarPSI) work straight away; LPIPS and
DISTS stay unavailable until the user prepares the weights, from the download button in the
"Select Metrics" window or with `python packaging/prepare_learned_weights.py`. On first launch,
with an empty cache, LPIPS and DISTS are therefore omitted with a stated cause and without any
network access: the packaging workflow verifies exactly this behavior on the frozen bundle
(`--smoke-test`, field `learned_policy`).

As an alternative to the `extras` in `pyproject.toml`, equivalent files are available in
`requirements/` (`base.txt`, `gui.txt`, `radiomics.txt`, `learned.txt`, `dev.txt`,
`packaging-base.txt` and `packaging.txt` for the full profile).

### Practical hardware requirements

- Practical minimum: 8 GB of RAM and 4 cores for 2D images and moderate stacks.
- Recommended: 16 GB of RAM and 6–8 cores for DICOM stacks, multiple comparisons and radiomics.
- Heavy workloads: 32 GB of RAM for large 3D volumes, many ROIs or learned metrics. The learned
  metrics remain optional and download their checkpoints only on explicit request.

Core counts are logical cores.

The learned metrics use `lpips` (official, AlexNet backbone) for LPIPS and `piq` for
MS-SSIM/FSIM/GMSD/VIF/DISTS. Every function in `reference_structural.py` imports these
libraries lazily: the module stays importable (and SSIM stays usable) even without the
`learned` extra installed; the individual functions that need them raise `ImportError` only
when actually called without the library available.

NSS feature distance and NSS patch-distribution distance (in `no_reference.py`) are **not**
part of the `learned` extra: they are QICOM's own reimplementation of the natural scene
statistics (NSS) of Mittal et al., written from scratch with numpy/scipy only (base
dependencies), without depending on `pyiqa`. Since no pre-trained model or third-party dataset
is included, the default "natural image" reference model is fitted on synthetically generated
images with a 1/f spectrum (`qicom/metrics/nss_reference.py`); for use calibrated on your own
reference corpus, pass a `reference_model` obtained from `fit_reference_model()`. The values
returned are therefore a relative index, not comparable with the original BRISQUE/NIQE scale.

**Canonical BRISQUE and NIQE** are also available, as additional metrics with IDs
`brisque_canonical` and `niqe_canonical`. BRISQUE uses PIQ 0.8.0; NIQE is implemented in QICOM
from the original paper. The official models are downloaded only on request from Select
Metrics, with size and SHA-256 verified. Without them the outcome is `unavailable-backend`: the
analysis does not access the network. Two absolute scores are reported, one per image, with
lower values better. These metrics evaluate the whole image even when background exclusion is
active. Definitions, normalization and checks are in
[NO_REFERENCE_METHODOLOGY.md](NO_REFERENCE_METHODOLOGY.md); the current inventory is
[metric_inventory.csv](metric_inventory.csv), distinct from the frozen inventory of the paper
that describes QICOM.

NIQE is fitted on photographs and, on CT images, can rank a noisier image as more "natural".
For CT, **RIQE** is available (`riqe`, Radiology Image Quality Evaluator, model `riqe-v1.0`,
[doi:10.5281/zenodo.23055559](https://doi.org/10.5281/zenodo.23055559)): the same construction
as NIQE with a fixed HU mapping, field-of-view and body masks, and a model fitted on full-dose
CT. The model (CC BY 4.0) ships with the package and is verified with SHA-256: no download.
RIQE scores only images declared in Hounsfield units (in the GUI: DICOM with Modality CT, or
NIfTI; in the CLI `--hounsfield-units`) and rejects slices without air (less than 1% of the
field of view ≤ −900 HU), so MR, bitmaps or a non-CT NIfTI receive no score. As in its
validation, it evaluates the comparison image on the reference's masks. It is meant to rank a
degraded image against its source within the same protocol; it must not be used on its own to
choose, compare or tune a denoiser.

## Implemented metrics

| Module | Metrics | Dimensional notes |
|---|---|---|
| `reference_error` | MSE, MAE, RMSE, NMSE, PSNR, bias (`comparison - reference`), residual SD, median/P5/P95 of the error and of the absolute error | native 3D; the voxel-wise summaries are descriptive |
| `reference_statistical` | NMI, PCC, Wasserstein-1 | native 3D; Wasserstein-1 ignores spatial position |
| `edge_detail` | EPI, HFEN | native 3D |
| `reference_structural` | SSIM (native 3D) · MS-SSIM, FSIM, GMSD, VIF, HaarPSI, LPIPS, DISTS (slice-wise on 3D) | see the minimum-size constraints in the metric descriptors (SSIM ≥7 per axis, MS-SSIM ≥161×161, HaarPSI ≥16×16) |
| `no_reference` | NSS feature distance, NSS patch-distribution distance (slice-wise on 3D, QICOM's own numpy/scipy reimplementation) · Blur/VoL (native 3D, scipy only) | The NSS patch distance requires images ≥96×96; no dependency on the `learned` extra |
| `brisque_canonical`, `niqe_canonical` | BRISQUE (PIQ), NIQE (QICOM with the LIVE pristine MVG) | slice-wise on 3D; BRISQUE ≥14×14 and non-constant, NIQE at least two valid 96×96 patches; models downloaded explicitly |
| `riqe` | RIQE (CT model `riqe-v1.0`, included) | slice-wise on 3D; CT in declared HU only; at least 72 valid 24×24 patches inside the body; the comparison uses the reference's masks |
| `roi_based` | SNR, CNR, Noise SD, Noise Var, CV, Mean, Min, Max | ROI masks passed explicitly |

`run_all_metrics(img1, img2, roi_masks=None, config=None)` in `qicom/metrics/runner.py`
computes every applicable metric and returns a `pandas.DataFrame` with columns
`metric_id, metric_name, category, image, scope, value, std, n_slices, better_direction,
target_value` — `scope` always distinguishes "per-slice" from "stack-aggregate".

Non-finite numeric inputs are rejected before computation. Metrics that are undefined on
degenerate inputs (for example PCC/EPI on constant images) are omitted, without producing NaN in
the report; programming errors, on the other hand, are not disguised as merely missing metrics.

### Background air exclusion

In a typical slice most pixels are the air around the patient: averaging it into a whole-image
metric means largely measuring the background (the error looks diluted, SSIM/PSNR are pushed
towards "identical" because air matches air). `qicom/metrics/background.py` builds the tissue
mask (`build_tissue_mask`); air *inside* the body (lung, bowel gas) stays included, because only
the air around the patient is excluded.

In the GUI the flow is guided in three steps and is semi-automatic plus manual: you create a
single **green circle** (`DualImageView.add_air_roi`) and place it on a background-only area —
it is the only place the air level is read from, never a measurement ROI drawn earlier (a noise
ROI sits in the air on purpose, a signal ROI never does); `Detect air` turns that sample into a
threshold and immediately colors on the images everything that would be excluded, with
adjustable tolerance and exact threshold; a paint-style **eraser** puts back into the
measurement whatever the threshold took by mistake (tissue as dark as air), with
`Undo corrections` to return to the detected result.

`run_all_metrics(..., tissue_mask=...)` applies it according to what each metric actually is:

- pixel-independent metrics (`reference_error`, `reference_statistical`): recomputed exactly on
  the selected pixels only, slice by slice (mean/std/range remain those of the stack);
- SSIM/EPI/HFEN: only the pixels whose entire kernel support falls inside the mask, as already
  happens for ROI rows;
- all the others (VIF, FSIM, GMSD, MS-SSIM, HaarPSI, DISTS, LPIPS, NSS feature distance, NSS
  patch-distribution distance): they have no per-pixel decomposition to restrict, so they receive
  the pair with the *background suppressed* (the same constant outside the mask, cropped to the
  tissue bounding box) — a weaker guarantee, declared as such in the interface and in the export
  summary.

ROI measurements and `roi_based` metrics (SNR/CNR) are never touched: a hand-drawn ROI already
says which pixels matter, and the noise/background ROI is often placed in the air on purpose.

### Quantitative profiles, diagnostics and reproducibility

The quantitative workbench defaults to the versioned **Fast Core** profile instead of selecting
every metric. Structural, Experimental Perceptual, Noise/ROI QC, Complete, Phantom/task-based and
Custom remain available. Manual selection does not drop any metric: it produces an explicit
Custom profile. The QICOM Index shows profile, version and composition, is marked
experimental/not clinically validated and is not comparable across different profiles or
versions; an index with missing mandatory components is declared partial.

Each computation also produces:

- an outcome register for every metric (`requested-and-computed`, `not-requested`,
  `not-applicable`, `failed`, `unavailable-backend`, `invalid/degenerate-input`, `cancelled`),
  visible in the workbench and included in the export;
- a versioned reproducibility receipt with hashes of images, masks, configuration and results,
  QICOM/backend versions, profile/formulas, preflight warnings and statistical unit;
- translation-only preflight diagnostics using 2D/3D phase cross-correlation, with the shift in
  pixels and in mm when the spacing is known. It applies no transformation and does not modify
  the pixels;
- for stacks, median, IQR, MAD, P5/P95, Tukey 1.5×IQR outliers and a link to the worst slice.
  The historical CIs are kept for compatibility but are described as optimistic, since slices
  and ROIs of the same case are not independent units.

The bias map uses `comparison - reference`, a blue-white-red LUT centered on zero, a symmetric
range and tissue/ROI masks; the PNG export saves the scale's JSON alongside. The NPS uses only
dedicated `noise_texture_roi` ROIs, declared detrending and windowing, window energy correction,
PixelSpacing and frequencies in mm⁻¹, multiple ROIs/slices, radial curve, anisotropy, integrated
power, fP, fA and sample count. The full parameters can be exported to JSON/CSV/Excel, and the
tool belongs to the Noise/ROI QC profile, not to the Core.

Acceptance profiles are user YAML/JSON files with hash, version, applicability, baseline and
higher/lower/target/range rules; QICOM does not distribute clinical thresholds. From Settings,
the two NSS distances can use a local NSS model fitted on approved QA images, with name,
version, date, corpus hash and fitting parameters; scores obtained with different models are not
comparable. 10–90% rise distance, edge MTF/TTF/MTF50 and d′ PW/NPW/NPWE are expert APIs of the
Phantom/task-based profile. They require, respectively, a profile, an explicitly declared
suitable edge, and a configured task function/TTF/NPS. The edge must be parallel to the row
axis, with the transition along the columns: non-zero angles are rejected, since slanted-edge
projection is not implemented. The PASS flag checks the CNR only.

**Python API only.** The NPS (`qicom.metrics.nps`), the acceptance profiles
(`qicom.metrics.acceptance`), the Phantom/task-based tools (`qicom.metrics.task_based`),
trending (`qicom.metrics.trending`) and the radiomics robustness check
(`qicom.radiomics.robustness`) are not reachable from the desktop application: they are used
from scripts. In the app the Noise/ROI QC profile computes only the ROI metrics, and the
Phantom/task-based profile selects no metrics; the radiomics workbench shows and saves
robustness tables that already exist, but does not compute them.

The d′ v2 formula distinguishes PW (`sqrt(∫S/NPS df)`), NPW (`∫S df / sqrt(∫S·NPS df)`) and NPWE
(`∫S·E² df / sqrt(∫S·E⁴·NPS df)`), with `S = |task·TTF|²`. NPWE requires an explicit
`eye_filter`, sampled on the same axis: QICOM assumes no viewing geometry or internal noise.
Integration is 1D over the interval supplied, with no `2πf` radial factor or multiplier for
negative frequencies; a 2D radial NPS must not be read automatically as a 1D NPS.
These formulas derive from the models described by
[Solomon and Samei (2016)](https://doi.org/10.1117/1.JMI.3.3.035506), restricted here to the 1D
domain. Old unversioned results used the PW formula under both the NPW and NPWE labels:
`DetectabilityResult.from_dict()` preserves value and label, assigns `formula_version=1`
and reports `effective_observer="PW"`; new results have version 2. To reproduce the historical
computation use `observer="PW"`. The Phantom/task-based profile moves to version 2.0.

`qicom.provenance.StatisticalUnit` represents `subject_id`, `case_id`, `acquisition_id`,
`timepoint`, `lesion_id`/`roi_id` and `reader_id`. Without at least three identified
independent units, the statistical APIs return descriptive statistics only, with a reason,
without promoting slices or ROIs to independent observations.

## CLI

```bash
qicom-cli confronta reference.dcm comparison.dcm [--data-range 4095] [--output results.xlsx]
```

(`confronta` is Italian for "compare".) Loads the two images/volumes (DICOM, NIfTI,
JPG/PNG/BMP through `qicom/io/`), computes every applicable metric with `run_all_metrics` and
prints the table; `--output` also exports to CSV, Excel or — with an `.md` suffix — the Markdown
report described in [AI assistant export](#ai-assistant-export). If `--data-range` is not given,
it is inferred from the dtype/range of the reference image and always printed explicitly (never
silently assumed).

## AI assistant export

QICOM can export the results as a single self-contained Markdown file (`*_ai_assistant.md` from
the GUI, `--output report.md` from the CLI) meant to be pasted into a chat with an AI assistant
that can then explain the numbers. Next to each value the file gives the metric definition taken
from the `qicom/metrics/descriptors/*.yaml` cards — the same source of truth as the interface
tooltip — so the model does not have to guess the meaning of a metric that shares a name with a
different one, and it follows the language selected in the application.

**The Markdown export does not contact a generative assistant** and uses no API keys.
The optional learned metrics, by contrast, use local models to measure the images.
The export only writes a file to disk; whether, where and to whom to upload it is a decision —
and a responsibility — that is entirely the user's. That is why the content is decided by an
explicit whitelist (`qicom/reporting/llm_export.py`): only metric id/name/category, value,
confidence intervals, units, the descriptor text and the configuration not derived from the
image (data range, 2D/3D, ROI shape and size) go in. File names and paths, DICOM/NIfTI headers,
patient/study/series identifiers, dates and institution, operator or physician names, hostname
and user have no way into the file, and ROI names — free text typed by the user — are counted,
not reproduced. The file is readable Markdown: open it and read it before sending it anywhere.

A **Citation** section asks the model to make any article, report or presentation written from
the file cite QICOM with the reference in **Help ▸ About QICOM ▸ Citation**; when RIQE is among
the results, also the study (arXiv:2610.00384), the model release (doi:10.5281/zenodo.23055559)
and the attributions its CC BY 4.0 license requires. The references stay in English in every
language.

The file stays small enough to paste into a chat: at most 200 result rows and an explicit byte
budget. When the analysis produces more, the rows shown are chosen deterministically, one per
metric in rotation, so that every metric appears at least once; the file states how many rows
exist, how many it shows and how many it omits, and the provenance digest at the bottom covers
**the entire** result set, not the excerpt. The file is written atomically: an error leaves the
previous file intact, never a half-written report that looks complete.

## Security and privacy

- All image processing happens locally, on the machine QICOM runs on: no image data is
  transmitted, there is no telemetry and no analysis opens network connections. The explicit
  downloads are the optional learned-metric checkpoints and the IBSI verification resources;
  both use pinned checksums.
- Portable projects and evaluation packages anonymize DICOM by default, remove
  private/identifying tags and regenerate UIDs consistently.
- Detection is fail-closed: a DICOM dataset saved without the PS3.10 preamble (common in PACS
  exports and older archives) is anonymized too, and a file recognizable as DICOM but not
  parseable blocks the export instead of being copied as is. In quantitative ZIPs non-DICOM
  files are copied unchanged and the original names are kept: these ZIPs do not guarantee the
  removal of identifying data from NIfTI headers or file names. The NIfTI sanitization of
  qualitative packages does not currently extend to quantitative ZIPs.
- The result is verified, not taken on trust: no value with which the original file identified a
  person can survive anywhere in the copy (nested sequences included). A file that fails this
  check — for example a DICOMDIR, whose directory records are not covered by the attribute
  rules — is blocked at export instead of being declared anonymous.
- A clinical DICOM with pixels but without `BurnedInAnnotation=NO` is blocked at export. The only
  exception allowed is a narrow one for minimal Secondary Capture images (typically JPGs
  converted to DICOM) with no direct identifying attributes; the file is explicitly marked as not
  verified for annotations burned into the pixels. QICOM performs no OCR and does not remove text
  embedded in the pixels.
- If the export is blocked by these checks, QICOM shows the reason and asks whether to export
  anyway. Only with explicit consent are the rejected files included, anonymized as far as
  possible and marked `PatientIdentityRemoved=NO` (not verified); the list stays in the error log.
- Errors can be selected in the Errors window and saved to a text report (version, platform,
  recent log with traceback) to attach to a bug report. QICOM sends nothing.
- Imported ZIPs are checked before extraction (path traversal, symbolic links, file count/size
  and compression ratio); manifests and answers are bounded and validated before use.
- Sensitive local files and logs use owner-only permissions on POSIX systems. Logs redact
  sensitive fields and the user's home path.
- Text exported to Excel is neutralized when it starts like a formula; sheet names are validated
  and made unique.
- The Markdown AI assistant export is built on a whitelist, not a blacklist: it writes only
  explicitly allowed fields (values, metric descriptors, configuration not derived from the
  image) and no data coming from an image header. A test suite verifies this starting from the
  whitelist itself. QICOM uploads nothing: sending the file, if it happens, is the user's act.

See also [SECURITY.md](SECURITY.md) for the threat model and operational limits.

## Data/session layer

`qicom/session/` (no GUI dependency) provides:
- **`models.py`**: `Project`, `Analysis`, `RoiDefinition`, `Annotation` (immutable dataclasses).
- **`storage.py`**: `SessionStorage`, local SQLite persistence (default location via
  `platformdirs`, cross-platform) with full CRUD; `save_analysis(analysis, results)` atomically
  saves analysis, results, ROIs and annotations in a single SQLite transaction.
- **`history.py`**: `AnalysisHistory`/`AnalysisBundle`, a read-only facade to retrieve/reopen
  past analyses (date/time, images, metrics, ROIs, annotations).
- **`commands.py`**: `Command`/`CommandHistory`, an in-memory undo/redo engine (Ctrl+Z/Ctrl+Y),
  ready for the GUI's concrete commands.
- **`app_logging.py`**: JSON-lines application log on a rotating file (+ console), readable from
  the GUI or exportable. It records session start/end, every background operation (name,
  duration and — on error — the exception with its traceback) and every
  computation/save/load/export, with shapes and counts but no file paths or patient-derived
  text.

## GUI

```bash
qicom-gui
# or, without installing the script:
python -m qicom.gui.app
```

`qicom/gui/` (PySide6 + pyqtgraph) contains only presentation/orchestration - no computation
logic of its own. The three windows share a task-oriented workbench: a navigation bar exposes one
step at a time and keeps the viewer free of panels that do not apply.

- **Quantitative**: `Import → Inspect → Measure → Analyze → Results → Heatmaps → Export`.
  ROIs/lines/annotations, computation and charts no longer compete for space at the same time;
  when the computation finishes the window moves to the results automatically. `Import` offers
  both image loading and import of a saved project; `Results` and `Export` hide the canvas
  entirely (numbers, charts and the written summary are the only useful content there), while
  `Heatmaps` does the opposite: full-size images and only the table of mappable metrics. Heatmaps
  are colored with standard medical-imaging LUTs (the perceptually uniform Inferno, Magma and
  Viridis, Hot Iron from the DICOM palette, plus grayscale). The direction of the scale follows
  the metric: for difference maps (MSE/MAE/NMSE) zero is transparent, while for SSIM — which is
  1.0 where the images match — the LUT is applied in reverse, so it is still the degraded areas
  that stand out (legend included). `Analyze` also hosts background air exclusion (see above).
  `Export` lists in writing what was analyzed, what was generated and which files will be
  written, with two exclusion options (heatmap images and key images with ROIs, both on by
  default). History, log and errors are no longer a step: they live in `View → Panels`, and
  unread errors stay flagged by an indicator in the status bar.
- **Qualitative**: the author role uses
  `Images → Questions → All pages → Preview → Save`. The first two steps are a guided pass over
  **one** page — you load its images, then write the questions about those images — and each
  step ends with a `Next` button that leads to the following one, so the path does not have to be
  discovered by clicking on the tabs. `All pages` is the overview: one row per page with how many
  images and questions it has, from which an existing pair can be opened, duplicated, renamed or
  deleted, or a new one started (which leads back to the `Images` step, always the same one, not
  an extra section). The evaluator sees only `Evaluate → Complete & save`, with no authoring
  tools.
- **Radiomics**: `Images → ROI/VOI → Pre-process → Extract → Results → Export`, with
  configuration and segmentation shown as separate contextual panels.
- Menus and the application bar are uniform (`File`, `Edit` where applicable, `View`, `Tools`,
  `Help`); theme, language and number format do not take up operational actions.
- The quantitative table uses translated, readable headers and includes a text filter across all
  columns; the original technical keys remain unchanged in the DataFrames and exports.

Main components:
- **`app.py`**: `QApplication` bootstrap + `configure_logging()`.
- **`main_window.py`**: chrome of the quantitative workbench (menus, navigation, contextual docks,
  status bar); builds an `AppController` and stays a thin shell over the business logic.
- **`controller.py`** (`AppController`): orchestrates image loading, metric computation
  (in a background thread, `workers.py`), `CommandHistory` (undo/redo, Ctrl+Z/Ctrl+Y),
  `SessionStorage`/`AnalysisHistory` (save/reopen past analyses).
- **`commands.py`**: concrete subclasses of `qicom.session.commands.Command` for every undoable
  action (ROI, window/level, annotations).
- **`widgets/dual_image_view.py`**: two synchronized `pg.ImageView` panels (slice;
  circle/rectangle/polygon ROIs duplicated on both panels; text annotations).
- **`widgets/background_panel.py`**: air exclusion in the `Analyze` step, in three steps (green
  sampling circle → `Detect air` → eraser for manual corrections), with adjustable
  tolerance/threshold and a summary of the excluded percentage. The step is declared optional by
  a fixed notice at the top of the column and is switched on from a full-width toggle, not a
  checkbox: skipping it is a legitimate choice (the metrics then average over the whole image,
  air included), but it should be a conscious one. There is **only one** air sample — once the
  circle is placed, the button that creates it is disabled.
- **`widgets/evaluation_setup_panel.py`**: the first step of the qualitative composer — answer
  anonymity, general instructions, randomization and blinding, hidden repeats, statistics and
  timing. Nothing here concerns a single page or question: it is the behavior of the whole
  evaluation, decided before anything exists to apply it to.
- **`widgets/page_overview_panel.py`**: the overview of the qualitative form composer — one row
  per page with the number of images and questions, and the actions to open, duplicate, rename
  or delete it.
- **`widgets/wizard_step_bar.py`**: the `Back`/`Next` bar at the bottom of every build step,
  showing which page is being worked on.
- **`widgets/roi_manager.py`**, **`widgets/annotation_manager.py`**, **`widgets/history_panel.py`**,
  **`widgets/log_panel.py`**, **`widgets/results_table.py`**, **`widgets/concat_dialog.py`**,
  **`widgets/import_panel.py`**, **`widgets/export_summary_panel.py`**,
  **`widgets/toolbar.py`**: dock panels/dialogs, each one pure presentation.
- **`widgets/workflow_rail.py`**: shared, translatable navigation; it reports the selected step
  to the owning window without knowing anything about the controller or the scientific data. It
  is a strip of browser-style horizontal tabs, hand-drawn (`QPainterPath`: curved shoulders
  cannot be expressed in QSS) and hosted in a toolbar at the top of the window: the active tab is
  filled with the color of the surface beneath it and merges into it, while the others stay
  detached on the recessed background. Only one step is visible at a time — they are the steps
  of a single flow, not documents open side by side.
- **`i18n/`**: runtime translation through JSON dictionaries
  (`locales/{en,it,fr,de,es,pt,zh-Hans,ja}.json`), not Qt `.ts`/`.qm` files (no build step);
  the language switches live, without a restart.
- **`theming.py`**: light/gray/dark QSS (`themes/{light,gray,dark}.qss`) applied both to the Qt
  chrome and (explicitly, via `pg.setConfigOption` + a refresh of the canvases already built) to
  the pyqtgraph panels, which QSS alone does not reach.

Before every computation QICOM compares the physical metadata of the loaded images and reports
— in the Errors panel and in the log, without ever blocking the computation — mismatched
photometric interpretation, pixel spacing, orientation or slice spacing, and series with
unevenly spaced slices. A MONOCHROME1 image (displayed inverted compared with a PACS) is also
marked as such in the panel overlay. The pixels are always the stored ones: QICOM does not
invert or resample anything on its own initiative.

Asynchronous loads and computations carry a generation identifier: a stale result cannot
overwrite images or results requested more recently. On closing, the "Save" option persists the
editable portable project, not just a snapshot of the results.

## Qualitative evaluations and statistics

In the first **Setup** step, the author can mark the evaluation as **anonymous-response**:
evaluators are not asked for a name, no name is recorded, and each submission is identified only
by its own **submission id** — a UTC timestamp plus a random suffix, generated once per submitted
answer set. It is a different key from the computer's identity
(`load_or_create_evaluator_identity`, which only serves to find an interrupted evaluation on that
machine): several people answering from the same workstation produce separate submissions, and
the same person submitting twice still produces two. Whoever aggregates sees how many arrived and
decides: if 10 were expected and 20 arrive, someone answered more than once.

The file an evaluator saves at the end of the evaluation contains **only their answers** (a
single `Raw Answers` sheet). The statistics are not there: for a single evaluator they would be
degenerate — a "mean" over a single judge is that judge's own score, and inter-rater agreement
and ICC are undefined — and they are the job of the aggregation step, which computes them across
evaluators. That sheet is also exactly what the aggregation module reads back when you load the
`.xlsx` instead of the `.json`.

From the launcher, **Aggregate Results** opens a four-step workspace (the same `WorkflowRail` as
the main windows): you load the package with the questions that were sent out, then the answer
files received — QICOM `.json`, the Excel report each evaluator saves at the end of the
evaluation, or a CSV of the same records. Each row of the list is an evaluation with a green or
red flag: the check is structural (pages, questions, question text and comparison images
actually defined in the package, plus the test id when the file declares one), so a report that
answers a different questionnaire is shown with the reason for the mismatch and excluded from the
analysis instead of making the aggregation fail. The following steps compute and display the
statistics and the mean-score chart in QICOM, and export everything to Excel (with native charts)
or to a folder of CSVs.

The aggregated answers must refer to pages, questions, panels, slices and score ranges actually
defined in the package; duplicate records and evaluators are not aggregated. The report includes
mean/deviation, Cohen/Fleiss kappa, ICC(2,1) and paired comparisons. For the latter QICOM first
averages slices per evaluator, pairs the comparisons within the same evaluator, computes the
probability of superiority and uses the Wilcoxon signed-rank test. It does not treat spatially
correlated slices as independent subjects. The report's `Methodology` sheet documents the
statistical unit and the limits of the bootstrap intervals.

### Qualitative design v4

The first **Setup** step configures general instructions, randomization of
cases/questions/panels, counterbalancing, seed, A/B/C labels, calibration pages, hidden repeats
and optional timing collection. These choices apply to the whole evaluation and change what each
evaluator sees, so they are made before building the pages, not while writing the questions.
**Create Blinded Delivery** produces a public ZIP with generic names and a separate JSON decoding
map, reserved for the author. Seed and order are saved in the delivery and in the answers:
resuming never changes what the evaluator sees.

Evaluators sign in with their own local user (ID and password) so that they can pause and
resume: their evaluations, pending and completed, are encrypted on that computer and visible only
to them. Without signing in, the evaluation takes place in a single session and nothing is kept.
The author can also protect the copy to be sent and the blinded delivery with an access code:
without the code they cannot be opened or modified and their answers cannot be aggregated; the
answers come back encrypted with the same code. Limits in `SECURITY.md`.

Numeric scales support text anchors and mandatory comments below a threshold. Missing answers,
explicit skips and **Not assessable** are kept distinct. Forced-choice A/B 2-AFC, paired
preference A/B/no difference, categories, ranking and open text are also available.

The report uses median/IQR, distributions, top/bottom box, divergent answers and response rates.
The primary agreement measure is Krippendorff's ordinal alpha; Gwet's AC2 is declared advanced.
Comparisons first take the median over slices in each evaluator×case cell, apply an independent
bootstrap over evaluator and case clusters and also report Wilcoxon with Holm correction.
Repeats, descriptive QC, completeness and precision have separate sheets; no flag excludes or
re-centers an evaluator automatically. Details in
[QUALITATIVE_METHODOLOGY.md](QUALITATIVE_METHODOLOGY.md).

## Pairwise radiomics

From the launcher, **Radiomics** opens a window and a project format independent of the
quantitative and qualitative modules. It accepts only a DICOM image/series or a NIfTI file for
each of the two roles (reference and comparison); JPG/PNG/BMP are rejected because they lack the
necessary physical geometry.

The dedicated workflow includes:

- resampling centered in physical coordinates to configurable isotropic voxels, with B-spline by
  default for the image and nearest-neighbor for the mask;
- IBSI discretization by bin count or fixed bin width. In the latter case, during extraction,
  QICOM replaces PyRadiomics's default anchoring at zero with the anchoring at the ROI minimum
  required by the IBSI profile;
- explicit normalization, clipping and resegmentation, included in the profile hash; the default
  remains `none`, with no silent changes to the values;
- synchronized manual ROIs/VOIs, thresholding and region growing without deep learning;
- multi-label NIfTI import with strict size/spacing/origin/direction checks, explicit
  confirmation before nearest-neighbor resampling, and original/preprocessed export with a
  sidecar;
- 2D/3D shape, first-order, GLCM, GLRLM, GLSZM, GLDM and NGTDM on Original, Wavelet and/or LoG;
- absolute, classic percentage and symmetric percentage delta for every feature; the symmetric
  one is primary in profile 2.0, while `delta_percent` keeps the classic value for compatibility;
- strictly descriptive pairwise agreement: no ROI/lesion of the same subject is treated as an
  independent unit, and formal ICC/CCC is reserved for the cohort aggregator;
- separate QC of the masks (Dice, Jaccard, RVD, centroid shift, HD95, ASSD, physical overlap,
  borders, voxels and hashes) and of the features;
- a robustness check with physical perturbations per family and a configurable preprocessing
  sweep, with no ICC on the single case and no automatic clinical selection (API only);
- a multi-project aggregator that reads only the tables, verifies the profile hashes and computes
  ICC/CCC only with at least three declared independent units; it includes Pearson/Spearman,
  effective N, associations with volume and exploratory redundancy clusters;
- a portable ZIP project with derived NIfTI copies, masks, configuration and results;
  Excel/CSV/HTML export with separate sections for features, deltas, receipt, outcome register,
  QC, robustness, sweep, validation and methodology.

On the Images page, **Verify QICOM with an IBSI phantom** opens the dedicated fourth mode. The
manifest shared with CI downloads the official image, mask and workbook only after consent,
checks them against their pinned SHA-256 and reuses them offline. The current verification
compares 104 features of the IBSI-1 digital phantom and is limited to the declared formulas, 3D
aggregation, NIfTI loading/geometry, labels, discretization and extraction: it is not a
full-pipeline DICOM verification and it is not a certification. PASS requires every mandatory
feature to be comparable and within tolerance; a missing feature produces INCOMPLETE. An editable
copy becomes `derived-experiment` and loses the reference verdict.

Every row reports backend, profile hash and coverage status. The shared receipt saves versions,
hashes of inputs/masks/configuration/results, warnings, outcomes and statistical unit. LASSO,
Rad-Score, ROC/AUC, predictive models, automatic registration and clinical thresholds are out of
scope. Full methodology in [RADIOMICS_METHODOLOGY.md](RADIOMICS_METHODOLOGY.md); schemas, batch,
trending, HTML and demo in [OPERATIONS_SCHEMAS.md](OPERATIONS_SCHEMAS.md).

**Known limitations of this prototype** (stated explicitly, not hidden): the freehand ROI is a
vertex polygon (`pg.PolyLineROI`), not a continuous stroke; stack concatenation is manual only
(a start index for each image + `numpy.concatenate` — automatic ordering from DICOM/NIfTI
metadata would require an extension of the core). DICOM SEG and RTSTRUCT are not included in
this iteration.

## Utility tools

From the launcher, **Utility Tools** opens a window with the catalog on the left and the tool on
the right; the same catalog is in the `Tools` menu of every working window. The tools read their
sources without modifying them: every result is a copy, and a set of files appears in the (new or
empty) destination folder all at once or not at all. A NIfTI written by a tool is positioned from
the source geometry (`qicom/io/geometry.py`) and copies no header field. Every processing and
saving operation shows its progress percentage in a window of its own; in the image area the
wheel scrolls through the slices (Ctrl+wheel zooms), dragging with the left button adjusts window
and level, the middle button pans the image and the right button zooms it, and a click, or a
dropped file, loads the image; a colored button always shows the next step.

| Tool | What it does |
| --- | --- |
| Semi-automatic segmentation | guided tabbed pages (image, label, sample, selection, apply, save) repeated for each region: sampled ROI, intensity range, preview, then "Remove from image", "Keep only this" or "Assign to label"; presets with indicative HU ranges for CT; undo/redo; saves NIfTI + a JSON label table |
| Mask operations and comparison | union, intersection, difference, XOR, filling empty slices, Dice/Jaccard/volumes/Hausdorff in mm |
| Convert images | DICOM, NIfTI or bitmap stacks to NIfTI, 8- or 16-bit PNG slices (on-screen window) or TIFF (real values), in a new `<name>_<format>` folder inside the chosen one |
| Resample | to a given spacing or onto a reference's grid, by patient coordinates (no registration) |
| Image arithmetic | difference, absolute difference, sum, mean, ratio |
| Controlled degradation | one effect at a time (noise, blur, resolution loss, JPEG) with a list of removable effects, from a seed, with reproducible provenance |
| Inspect and edit DICOM tags | full header with sequences and private tags, search and CSV export; modified copies in a new folder, pixel tags locked, measurement tags only after confirmation |
| Compare DICOM headers | two headers element by element, in full; "Only differences" and "Hide UIDs" filters |
| Check and organize DICOM folders | studies and series, missing instances, irregular spacing; sorted copy without identifying names |
| Decompress DICOM | a compressed DICOM (JPEG lossless, JPEG-LS, JPEG 2000, RLE…) rewritten uncompressed, one file per frame, in the new `<name>` folder inside the chosen one; multi-frame images receive new instance and series UIDs, Enhanced images stay Enhanced |
| Anonymize images | the same fail-closed rules as the exports; file tree shown before writing; copy to `<folder>_anonymized` with the same subfolders and the same names (the names are not de-identified) or, optionally, with generic names in `anonymized` |

Segmentation separates intensity classes, not organs; the preset HU ranges are starting points to
adjust, not clinical thresholds. The privacy limits of each tool are in [SECURITY.md](SECURITY.md),
the formats written in [OPERATIONS_SCHEMAS.md](OPERATIONS_SCHEMAS.md).

## Local and headless operations

`qicom-cli` offers `batch-quantitative`, `aggregate-qualitative` and
`batch-radiomics-cohort` driven by a JSON configuration, with progress, logging, exit codes, error
isolation, tidy CSV/JSON and receipts. The radiomics Tools menu also exposes multi-project
aggregation. `TrendStore` keeps versioned baselines, variations, Shewhart, optional EWMA and user
limits kept distinct from statistical limits.

### Synthetic bundle (demo and bug reproduction)

QICOM generates a self-contained folder of deterministic sample data covering the three modules,
with results, reproducibility receipts and a local HTML report. Everything derives from the fixed
seed `20260517`: the same build always produces the same files, and `qicom_demo.json` lists the
QICOM version, the platform and a SHA-256 per file.

```bash
qicom-cli demo ./QICOM-synthetic-demo
```

In the app: **Settings → Diagnostics**, or **Help → Report a Bug**, which is where it is really
needed — with the bundle attached to a report, the recipient regenerates it and compares the
hashes, without any patient data having to travel. It contains no PHI and is not a clinical or
IBSI validation. The destination folder is never overwritten (from the GUI the next free name,
`-2`, `-3`, …, is chosen automatically).

## Tests

To set up a complete development environment and run, before pushing, the same checks that
GitHub uses:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python packaging/bootstrap_environment.py
python -m pip install -e ".[gui,learned,radiomics,dev]"
python packaging/check_ci.py
```

The bootstrap first upgrades `pip`, then `setuptools` using the requirement declared in
`[build-system]`. A plain `pip install -e .` prepares the build tools in an isolated environment
and can leave an outdated `setuptools` in the environment that is then audited. `wheel` stays in
the isolated build environment, so that PyInstaller does not collect it into the executable. The
GitHub jobs also create a dedicated venv: packages preinstalled on the runner do not enter the
verified or distributed environment.

The check command verifies dependency versions and compatibility, and runs `pip-audit`, lint,
formatting, mypy, the repository gates and the suite with coverage. It also prepares the verified
learned checkpoints and enables the comparison with the official IBSI references. It needs
network access for the audit and for resources not yet cached; these accesses belong to
development verification. The reports end up in `build/audit/`, `build/coverage.json` and
`build/ibsi-ci/`. Any error stops the command with a non-zero exit status.

For the test profile without learned metrics, use a **separate venv**, install
`.[gui,radiomics,dev]` after the bootstrap and run
`python packaging/check_ci.py --profile base`. This profile runs the suite and also checks
collection with `python -m pytest`; full coverage stays in the full profile.
On Linux you also need the Qt libraries listed in the `Install Qt runtime dependencies` step of
`.github/workflows/ci.yml`.

The phases `--phase audit`, `--phase static` and `--phase test --profile full|base` are invoked
directly by CI. The pre-push check verifies the current interpreter only: the 3.11/3.12 matrix is
still needed. It installs no Git hooks and makes no commits or pushes.

CI is configured to run the suite on **Linux, Python 3.11 and 3.12**: the base job includes GUI
and radiomics but omits learned; the full job also includes torch and runs on 3.12.
Windows and macOS have native builds with smoke tests, not the same full suite.
CI also runs every Monday to catch new vulnerabilities without waiting for a commit. The audit
runs before the tests, and its JSON report is kept as an artifact for seven days even when
vulnerabilities are found. The individual gates can still be run separately:

```bash
python packaging/check_complexity.py           # no new function above C901 20 (ruff)
python packaging/check_release_consistency.py  # consistent version and a changelog that describes it
python packaging/check_coverage.py --coverage-json build/coverage.json
pytest -q tests/radiomics/test_ibsi_validation.py   # IBSI reference, mandatory on tags
```

Declared dependencies have upper bounds to avoid implicit major upgrades.
Per-OS freezing is no longer just an intention: the packaging workflow runs
`packaging/generate_constraints.py` in the same environment as the build and publishes the
manifest of installed versions and VCS origins alongside the bundle (see the Installation
section).

## Cross-platform packaging

Standalone executable via [PyInstaller](https://pyinstaller.org/), spec in
`packaging/qicom.spec`:

```bash
# Base profile: quantitative + qualitative GUI
pip install -e ".[gui,packaging]"
QICOM_PACKAGE_PROFILE=base pyinstaller packaging/qicom.spec

# Full profile: adds the radiomics workflow
pip install -e ".[gui,radiomics,packaging]"
QICOM_PACKAGE_PROFILE=full pyinstaller packaging/qicom.spec

# Bundle smoke test (add .exe on Windows)
dist/qicom-full/qicom --smoke-test
```

Notes:
- **PyInstaller does not cross-compile**: it has to run on each target operating system
  (Windows, macOS, Linux) to produce that system's build - a Linux build runs only on Linux, and
  so on.
- The `base` profile excludes PyRadiomics/SimpleITK; the `full` profile, the default, includes
  them. The outputs are `dist/qicom-base/` and `dist/qicom-full/` respectively.
- Both profiles use `PySide6-Essentials`: QICOM does not need the Addons wheel
  (WebEngine, Multimedia, Qt 3D and other unused modules).
- By default the spec **excludes** the learned-metric backends (`torch`, `torchvision`, `lpips`,
  `piq`) — a lighter and faster build, matching the `gui,radiomics` extras (not `learned`) of
  `pyproject.toml`. Every other metric (including the NSS distances, which no longer need them),
  image loading, ROIs, session/history and export remain fully functional; calling an excluded
  learned metric raises `ImportError` naming the missing library, exactly as in a development
  install without the `learned` extra.
- To include the learned metrics in the build as well:
  `pip install -e ".[gui,radiomics,learned,packaging]"`, then
  `python packaging/prepare_learned_weights.py` and
  `QICOM_PACKAGE_PROFILE=full QICOM_INCLUDE_LEARNED_METRICS=1 pyinstaller packaging/qicom.spec`
  — a much larger build (`torch` dominates both size and build time) that is slower to produce.
- The spec uses targeted collections and includes the data files it needs
  (`gui/i18n/locales/*.json`, `gui/themes/*.qss`, `metrics/descriptors/*.yaml`, icons and the
  `THIRD_PARTY_LICENSES.txt` generated for that profile), so they resolve correctly both in a
  checkout and in the PyInstaller bundle. Inventory and SBOM also include PyInstaller: its
  bootloader/runtime is redistributed in the frozen app, even though the other packaging
  dependencies remain build tools.
- The `.github/workflows/package.yml` workflow configures native builds for Linux x64, Windows
  x64 and macOS 14 arm64 and runs `--smoke-test` on the frozen app before uploading the
  artifacts. The platforms actually produced depend on the selection and outcome of each run;
  the [verification matrix](packaging/DISTRIBUTION.md#build-and-verification-matrix) details
  their coverage. The Windows executable embeds product name, description, author, copyright and
  version; every bundle contains `SHA256SUMS` and every archive is published with a `.sha256`
  sidecar, useful for verifying and authorizing exactly the build received. Checksums do not,
  however, authenticate the publisher. The artifacts remain unsigned: Windows Authenticode
  signing and Apple signing/notarization require the distributor's certificates and credentials.

## Intended use, attribution and license

### Intended use

QICOM is intended exclusively for research and quality-control use. It is not a medical device.
The numbers it produces describe images, not patients: deciding whether a comparison is
meaningful, and interpreting its result, remains the responsibility of the qualified user who
set it up.

Two limitations that must always be stated together with the metrics:

- **No registration.** QICOM compares images element by element and never registers them. Every
  full-reference metric (MSE, MAE, RMSE, PSNR, SSIM and the others) therefore assumes that the
  two inputs already correspond spatially. `qicom/io/consistency.py` flags the mismatches that
  can be read from the DICOM headers — photometric interpretation, pixel spacing, orientation,
  slice spacing — but cannot detect a misalignment the headers do not describe.
- **IBSI: aligned, not certified.** The radiomics module follows the IBSI reference definitions
  and applies the IBSI convention where PyRadiomics departs from it, but it is not IBSI
  certified: IBSI does not run any certification scheme.

### DICOM attribution

The following sentences are reproduced verbatim in [NOTICE](NOTICE), in `qicom/about.py` and in
the **Legal & DICOM** tab of the About window:

> QICOM supports DICOM® medical images.
>
> DICOM® is the registered trademark of the National Electrical Manufacturers Association (NEMA)
> for its standards publications relating to digital communications of medical information.
>
> QICOM is an independent software project and is not affiliated with, sponsored by, or endorsed
> by NEMA or the DICOM Standards Committee.
>
> QICOM reads DICOM files; it implements no DICOM network or storage service, claims no
> conformance to the DICOM Standard, and publishes no DICOM Conformance Statement.

### License

Apache-2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party components distributed with
the application remain subject to their respective licenses. The build generates the inventory
for the actual profile and reproduces in its own `THIRD_PARTY_LICENSES.txt` the texts declared by
the installed distributions; [packaging/THIRD_PARTY_LICENSES.txt](packaging/THIRD_PARTY_LICENSES.txt)
remains the repository's reference copy.

### How to cite QICOM

```
Mattiussi, F. (2026). QICOM — Quality Image COMparator (Version 1.1.0.dev0)
[Computer software]. https://github.com/Metallogik/QICOM
```

The repository contains [CITATION.cff](CITATION.cff); the equivalent BibTeX entry can be copied
from **Help ▸ About QICOM ▸ Citation**. A DOI will be added in both places once the first release
is archived.

### Versioning and changelog

QICOM follows [Semantic Versioning](https://semver.org/). [CHANGELOG.md](CHANGELOG.md) gives a
section of its own — *Changes affecting numerical results* — to every change for which **the same
inputs produce different numbers**: formulas, preprocessing, radiomics discretization, masks,
resampling or defaults that enter a computation. These never end up under a generic "bug fixes".
Deprecated Python APIs, their replacements and the minimum removal window are recorded in
[DEPRECATIONS.md](DEPRECATIONS.md); no public 1.x API is removed before 2.0.

A version names a release; it does **not** identify the code that produced a result. Any number
of commits can lie between two builds that carry the same number. That is why the
reproducibility receipt also records `build_revision` — the exact commit — and `source_dirty`,
and the bundle can state them without a `.git` directory at hand. The full strategy (one
version, four files, who enforces it, and why `main` carries a `.dev` version) is in
[VERSIONING.md](VERSIONING.md). The distribution requirements, signing and notarization included,
are in [packaging/DISTRIBUTION.md](packaging/DISTRIBUTION.md).

### In-app information

**Help ▸ About QICOM** — present in all three main windows and reachable from the launcher's ⓘ
button — gathers version and build, author and location, license, repository and support contact,
intended use (what QICOM does and does not do), DICOM attribution, privacy statement and a
copyable citation. All of this comes from `qicom/about.py`, the single source shared with
`pyproject.toml`, `CITATION.cff` and this README: their consistency is verified by
`tests/test_about.py`.

### MIRP backend and IBSI 1 references

The default backend remains PyRadiomics. The optional MIRP 2.7.0 backend adds local intensity,
intensity histogram, intensity-volume histogram, GLDZM and the 2D/2.5D/3D aggregations. It is
selected in the radiomics configuration panel and supports Original images. Results record
backend, version and configuration; existing PyRadiomics profiles keep their serialization and
their hash.

The IBSI verification window lets you select the original PyRadiomics subset (104 rows) or the
six MIRP manifests: digital phantom (482 rows), CT A/B (406 each) and CT C/D/E (270 each). The
manifests cover every row with a numeric reference and tolerance in the six IBSI 1 sheets. The
1,622 CT rows include 300 preprocessing diagnostics; A–E are configurations of the same case.
Values and tolerances are read from the official workbook, verified by hash. The verification
needs no network once the cache is prepared.

For MIRP extractions, QICOM computes Moran's I and Geary's C with exact sums of inverse distances
via FFT convolution, without random sampling. The receipts distinguish these two outputs and the
diagnostics from the other values extracted with MIRP.

MIRP is distributed separately under EUPL-1.2. Required citation:
[Zwanenburg and Löck, MIRP (JOSS, 2024)](https://doi.org/10.21105/joss.06413).
The CT reference data are CC BY-NC 3.0; URLs, hashes and attributions are in the manifests,
while the image files are not included in the QICOM distribution.
