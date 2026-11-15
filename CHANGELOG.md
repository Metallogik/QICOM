# Changelog

All notable changes to QICOM are documented in this file.

QICOM follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html):
`MAJOR.MINOR.PATCH`.

## How to read this file

Every release carries the usual `Added` / `Changed` / `Fixed` / `Removed`
sections, plus one that matters more than the others in a scientific tool:

> **⚠️ Changes affecting numerical results**

Anything listed there means that **the same inputs can produce different
numbers before and after that version**: a corrected or altered metric
formula, a change in preprocessing, in the radiomics discretisation, in how a
mask or a background is derived, in resampling or interpolation, or in the
default value of any parameter that enters a computation. Such a change is
never folded into a generic "bug fixes" line, and it always states what
changed, why, and which metrics or features it touches.

If you have results produced by an earlier version and this section is not
empty between that version and yours, re-run the analysis before comparing.

## [Unreleased]

### Added

- **Updates from inside the app, with consent.** On first launch, a copy installed from qicom.ch
  asks whether it may check at startup for a newer version; the choice can be changed from
  Help > "Check for Updates at Startup", and Help > "Check for Updates..." checks on demand. The
  check reads `latest.json`, which CI publishes in the `latest-build` release after all the files
  it describes (commit, commit date, size and SHA-256 of every file): a build is newer if it comes
  from a later commit, because all main builds have the same version. If the user accepts, the
  update is downloaded with a progress bar, verified and installed after the windows close (with
  the usual prompt to save changes): on Windows the setup runs silently on the same folder and
  for the same users and reopens QICOM (`/RELAUNCH=yes`), on Linux the `.deb` is installed with
  `pkexec apt-get` and the `.tar.gz` swaps its folder for the new one. On macOS and from the
  portable folder the download page opens. Tagged releases and runs from source never check.
  The privacy statement (About window, README) lists the new connection, and the verified
  download of the learned weights and of updates is now a single module
  (`qicom/verified_download.py`).
- **"Load Images" explains what it asks for.** The open window says in one line how loading
  works, and the three buttons have explicit names ("Load a Whole Folder...", "Load One File...",
  "Load Several Files Together...") with a tooltip each: what they accept and in which order they
  become slices (DICOM by position, other images by name). The choice of the destination panel
  and the "Load images..." button of the Import step also have a tooltip.
- **Restoring default values.** On the Heatmaps page, "Restore Defaults" brings the colour scale
  back to the metric's, the min/max threshold to the range of the map shown, and the
  brightness/contrast of each image to the values at loading (each undoable like a normal
  change); the tooltip names every default value. A double-click on the colour bar brings the
  threshold back to the range of the map. In the Remove air step, "Restore default tolerance (3)"
  brings the tolerance back to its default value and removes an exact threshold entered by hand.
- **New Project, Save Project and Save Project As in the File menu** of the quantitative
  analysis, always visible and with the usual shortcuts (Ctrl+N, Ctrl+O, Ctrl+S, Ctrl+Shift+S).
  Previously, saving the project appeared only in the Export step and starting over meant "Reset
  all", which they replace. "New Project" stays in the window: it offers to save unsaved
  changes, asks for confirmation if saved work is open, and asks nothing when the window is empty.
  The window title shows the open project (or "New project (not saved)") and an asterisk when
  there are unsaved changes.
- **Windows installer with the classic setup wizard.** `QICOM-windows-x64.zip`, the same file
  the website points to, now contains `QICOM-Setup.exe` (Inno Setup 6,
  `packaging/build_windows_installer.py` and `packaging/windows/qicom.iss`) and a README: once the
  zip is extracted, the installer opens the setup wizard in English, with the QICOM logo and icon.
  Pages: welcome with the description of QICOM, Apache 2.0 licence, "About QICOM" (what it does
  and what it is for), destination folder (Program Files by default, with "Install for me only"
  for those without administrator permissions), summary, progress bar with percentage, and finish
  with "Launch QICOM". It creates the Start menu entry and, optionally, the desktop shortcut, and
  it uninstalls with "Uninstall QICOM" in the installation folder, next to `qicom.exe`, or from
  Settings > Apps. The same bundle goes in byte for byte, and an update replaces `_internal` in
  full. CI installs it silently for all users, runs the smoke test from the installed folder,
  verifies its build identity and uninstalls it, checking that nothing is left behind. The
  version without installation becomes `QICOM-windows-x64-portable.zip`, with
  `README-FIRST.txt` at the top.
- **Debian/Ubuntu package** (`QICOM-linux-x64.deb`, with a `.sha256` sidecar), produced by the
  Linux build next to the `.tar.gz` by `packaging/build_deb.py`: the same bundle, byte for byte,
  in `/opt/qicom`, the `qicom` command, the menu entry and the icon. It declares the system Qt
  libraries (the same list CI installs, checked by a test), so `apt install` brings them along,
  and `libc6 (>= glibc of the build runner)`, so a system that is too old refuses the package
  instead of installing an application that does not start. `DEBIAN/md5sums` enables
  `dpkg --verify qicom`. CI installs it with apt, launches it from the installed command with the
  smoke test, verifies its build identity and removes it, checking that nothing is left behind.
- **Citation of the QICOM article** (arXiv:2610.31218, doi:10.48550/arXiv.2610.31218): badge and
  citation sentence at the top, in the middle and at the bottom of the README, with the BibTeX
  entry in the new section *Citing the QICOM article*; `CITATION.cff` lists the article as
  `preferred-citation`, so GitHub's *Cite this repository* button offers the article. The
  software citation (version) stays unchanged next to the article's.
- **Utility Tools**, a fourth section of the launcher next to the three workflows and a submenu
  of the Tools menu of every working window: a catalogue of standalone tools in a dedicated window
  (catalogue on the left, tool on the right), which returns to the launcher when closed. The tools
  read the sources and never modify them; every set of output files appears complete or not at
  all (staging folder renamed at the end), in a new or empty folder. The NIfTI written by a tool
  is positioned from the source geometry (`qicom/io/geometry.py`: for DICOM the column, row,
  slice order of dcm2niix and SimpleITK, checked against SimpleITK also with oblique orientation)
  and copies no header field. Every processing and every save opens a window with the progress
  percentage; in the canvases the wheel scrolls through slices and Ctrl+wheel zooms, as in the
  workflows, and a click on the empty area (or a dragged file) loads the image; in each tool only
  one kind of button is coloured at a time, the step to take, as in air removal.
  - **Semi-automatic segmentation**: a label map is built one step at a time, in six tabbed pages
    (Image, Label, Sample, Selection, Apply, Save) with Back/Next and the "Segment another region"
    loop. You choose a label (presets air, lung, fat, soft tissue, contrast medium, bone tissue,
    metal, table, or a free name), draw an ROI (rectangle, ellipse, freehand, chosen in the
    right-hand column), sample its intensity range (percentiles or mean ± k·SD, adjustable on the
    histogram; for CT also an indicative HU range from the preset, not a clinical threshold),
    choose where the selection may reach (this slice or the volume, anywhere or only connected to
    the ROI), with optional refinements (median, holes, small regions, opening and closing, always
    in the slice plane), look at the preview and decide: "Remove from image", "Keep only this",
    "Assign" or "Erase from label". A pixel belongs to at most one label; a step takes only
    unassigned pixels, unless overwriting is explicit. Undo/Redo with no limit on steps within
    256 MiB of history. The label table shows visibility, colour and volume (mL with known
    geometry); the view shows the colours over the image or only the ticked parts. Saving and
    reopening as a NIfTI map with a JSON label table (new format version 1,
    `OPERATIONS_SCHEMAS.md`), importable by radiomics; export of the image with only the ticked
    parts (NIfTI, or PNG for 2D images).
  - **Mask operations and comparison**: union, intersection, difference, XOR of two label maps
    (also per single value), filling of empty slices between drawn ones (distance-map
    interpolation), comparison with Dice, Jaccard, volumes, Hausdorff, HD95 and mean surface
    distance in mm; CSV export.
  - **Convert images**: DICOM, NIfTI or bitmap stacks to NIfTI (smallest lossless integer type,
    otherwise float32), 8- or 16-bit PNG slices in the on-screen window, or float TIFF slices
    with the real values. The result goes into a new folder created inside the chosen one and
    named after the image and the format (`<name>_nifti`, `<name>_png16`, numbered if it already
    exists), never scattered among the files already there.
  - **Resample**: to a chosen spacing (also isotropic) or onto the grid of a reference image by
    patient coordinates, without registration; linear, nearest-neighbour or cubic interpolation.
    Resampling onto the grid matches SimpleITK's. Linear and nearest neighbour are computed in
    blocks of slices, which provide the progress.
  - **Image arithmetic**: difference, absolute difference, sum, mean, ratio (zero denominators
    are counted and set to the chosen value), with a warning if the spacings differ.
  - **Controlled degradation**: Gaussian or Poisson noise, blur, loss of resolution, JPEG
    compression, from a single seed, to check how a metric reacts. One effect at a time: effect
    buttons at the top, intensity, Apply, list of the applied effects (each removable) and
    saving; every change recomputes the chain from the original. The provenance (new format
    version 1) is enough to reproduce the result bit for bit.
  - **Inspect and edit DICOM tags**: all the tags of a file or of a file in a series, with meta
    information, nested sequences and private tags, search, private-tag filter, copy and CSV
    export of what is shown, not anonymised (see `SECURITY.md`). Malformed headers: an element
    that cannot be decoded becomes a marked row, sequences that are too deep or too large are
    truncated, saying so. From the Tools menu of quantitative analysis and radiomics, "Inspect
    DICOM tags of the current image"; not from qualitative evaluation, so as not to give away the
    header of an image evaluated blind. In the same tree, the "New value" column edits, adds or
    deletes top-level elements of a file or of the whole series and writes copies into a new
    folder; the tags that define the pixels and group 0002 are locked, those that change
    measurements (spacing, rescale, slice geometry) ask for confirmation; new consistent UIDs per
    instance, series or study (SOPInstanceUID always follows this choice and the file meta stays
    aligned); single-valued texts (LT, ST, UT, UR) keep their backslashes; change log with hashes,
    never the previous values (new format version 1).
  - **Compare DICOM headers** (read-only): two headers element by element, with outcome
    different / only on one side / identical; it opens with both headers in full, and "Only
    differences" and "Hide UIDs" narrow the view.
  - **Check and organize DICOM folders**: groups by study and series and reports missing or
    repeated instances, mixed sizes, irregular spacing or orientation, slices without a position;
    CSV report and ordered copy with names that do not identify the patient.
  - **Decompress DICOM**: a compressed DICOM file (JPEG lossless, JPEG-LS, JPEG 2000, RLE, JPEG
    baseline) rewritten in Explicit VR Little Endian, one file per frame, in a new folder named
    after the file inside the chosen one (the dialog starts from the source folder), all at once
    or not at all (`qicom/io/dicom_decompress.py`). The pixels are decoded by the same decoders and
    in the same order as the loader, so the copies contain exactly what QICOM shows. A
    single-frame file keeps all its UIDs (only the encoding changes); a multi-frame becomes one
    instance per frame, with new SOP Instance UIDs, a single new Series Instance UID and the frame
    number as Instance Number, while study and frame of reference stay. An Enhanced object stays
    Enhanced: each copy carries its own per-frame item and the shared functional groups; the
    vectors pointed to by Frame Increment Pointer are reduced to the frame. A baseline or extended
    JPEG source without Lossy Image Compression gets "01", because decompression does not undo
    the loss.
  - **Anonymize images**: DICOM, NIfTI and bitmaps with the fail-closed rules of the exports. The
    chosen files (or a folder with its subfolders) are first shown as a tree, with the expected
    outcome for each; the copy goes into `<folder>_anonymized` next to the original (or another
    chosen location), with the same subfolders and the same file names. The names are not
    de-identified and the tool says so; alternatively an option replaces them with generic names
    (`folder_001`, `image_0001.dcm`, ...) in an `anonymized` folder, without a correspondence
    table, and the tree shows the name each item will have in the copy. Files that are not images
    are left out. Files that cannot be verified are listed before writing and written only on
    explicit choice, as not verified.
- The segmentation primitives (Otsu, hole filling, largest component, dilation, erosion, bounding
  box) moved from `qicom/radiomics/segmentation.py` to the new package `qicom/segmentation/`,
  free of Qt and SimpleITK, shared by radiomics and the tools; radiomics imports them from there
  with the same names and the same behaviour. `qicom/processing/` is another new Qt-free package;
  both are checked by the architecture map.
- Access code for the evaluations to be sent out. In the **Save** step (publishing) the author
  can enter or generate a code (at least 10 characters) and save a **protected copy for the
  evaluators**; with the code entered, the **blinded delivery** and its decoding map are protected
  too. The author's working copy stays without a code; a protected package opened for editing is
  saved protected again. Whoever takes, edits or aggregates a protected evaluation must enter the
  code, asked for right after choosing the file; an evaluator with a user does not re-enter it
  when resuming, because the key is kept in their encrypted draft. The answers of a protected
  package are saved encrypted with the same code (instead of the Excel report), and aggregation
  opens them only with the code, asked for once. Command-line aggregation reads the code from a
  file (`access_code_file`). Package, answers and map are encrypted with AES-256-GCM and a key
  derived with scrypt; the package in 1 MiB blocks, so that large packages do not sit in memory
  and a truncated one is refused. New "code-protected files" format version 1
  (`OPERATIONS_SCHEMAS.md`); limits in `SECURITY.md` (`qicom/session/protected_files.py`,
  `qicom/session/key_derivation.py`, `qicom/gui/widgets/access_code_dialog.py`).
- Evaluator users and paused evaluations, encrypted. **Take Evaluation** now asks to log in with
  ID and password (minimum 5 characters, no complexity requirements) or to create a user;
  alternatively one can continue without logging in, but then nothing is kept and the evaluation
  cannot be paused (closing before saving the report asks for confirmation, because the answers
  would be lost). After logging in, **My evaluations** lists only that user's evaluations, split
  into **Paused** (Resume, on the page left) and **Finished** (Reopen, to save the report again),
  with Remove after confirmation — it deletes the answers, never the .zip package or the
  reports — and Open another evaluation. During the evaluation, **Pause and resume later** closes
  and returns to the start screen; the status bar shows the user. Password change and deletion of
  the user (with all their evaluations) from the User menu and from the login window. The answers
  are encrypted (AES-256-GCM) at every autosave with the user's key, protected by the password
  through scrypt; the password is not stored and cannot be recovered. Clear-text drafts from
  previous versions are recovered by reopening the package and confirming they are one's own;
  they are then encrypted and the clear-text copy deleted. New formats: account, encrypted
  envelope and drafts, all version 1 (see `OPERATIONS_SCHEMAS.md`); limits in `SECURITY.md`. New
  dependency `cryptography` (Apache-2.0 or BSD-3-Clause, with `cffi` and `pycparser`), in the
  locks of all platforms (`qicom/session/evaluator_accounts.py`, `encrypted_files.py`,
  `evaluator_drafts.py`; `qicom/gui/widgets/evaluator_login_dialog.py`,
  `my_evaluations_dialog.py`).
- Interface in Spanish (`es`), Brazilian Portuguese (`pt`), Simplified Chinese (`zh-Hans`) and
  Japanese (`ja`), selectable like the other languages from the launcher, the Language menu and
  the settings. They cover all keys, including the metric cards, with placeholders checked
  against English. They are a first translation not yet reviewed by native speakers: the texts on
  intended use, de-identification and metric limits must be reviewed before a release. For
  Chinese and Japanese the theme adds the CJK fonts of the chosen language, so Japanese kanji are
  not drawn with the Chinese forms (and vice versa).
- Extended golden (`tests/performance/golden_outputs_extended.json`): pins the values of all the
  metrics beyond the 8 of the original golden, which stays unchanged. Classic 2D and 3D metrics
  and ROIs are compared with the original tolerance; the PIQ, learned and canonical NR sections,
  in float32, at rel 1e-5, and they are skipped, stating the reason, when backends or weights are
  missing.
- Canonical BRISQUE and NIQE (`brisque_canonical`, `niqe_canonical`): the BRISQUE score of PIQ
  0.8.0 with its pre-trained SVM and the NIQE distance from the original LIVE MVG model, next to
  the existing NSS distances `brisque`/`niqe`, which stay unchanged. The models are downloaded
  only on explicit request and are verified by SHA-256. The `complete` profile moved to 1.1 to
  include them (see the section on numerical results).
- Resource profiles (`qicom.runtime`: reduced, standard, workstation) chosen from the available
  RAM and CPU: they tune caches, models kept in memory and pairs in parallel, never the values.
- Memory check before allocating an image (`qicom.io.memory_preflight`): a volume that would not
  fit asks for confirmation before being decoded, instead of exhausting the RAM.
- Export with DICOM files that cannot be anonymised in a verified way: when the fail-closed
  checks block saving a qualitative evaluation or a quantitative project (for example a DICOM
  without `BurnedInAnnotation=NO`, an attribute that `dicom-anonymizer` cannot handle, a
  DICOMDIR), QICOM no longer stops without alternatives: it shows the reason and asks whether to
  export anyway. The default is still to block (Cancel is the default button). If the user
  accepts, the refused files are anonymised as far as possible — PS3.15 profile if applicable,
  then removal of private tags and blanking of every remaining identifying attribute — and marked
  `PatientIdentityRemoved=NO` with a `DeidentificationMethod` that names the override; a file
  that cannot even be read or rewritten is included unchanged. Every file exported this way is
  listed in the error log. On the code side: the `unverified` parameter of `anonymize_dicom_file`
  and `unverified_dicom` in the saves (`qicom/io/dicom_anonymize.py`,
  `qicom/gui/error_reporting.py`).
- Error report to send to the developer: from the Errors window (indicator in the status bar),
  from the Errors panel and from Help ▸ Report a Bug, one or more errors are selected (Ctrl/Shift
  + click; no selection = all) and saved to a text file with version, revision, platform and the
  latest entries of the application log with the tracebacks. Plain text, to be checked before
  sending; home path masked; no images (`qicom/session/error_report.py`).
- The qualitative window now has a persistent error log with an indicator in the status bar, like
  the quantitative one: previously, errors appeared only for a few seconds in the status bar.
- Unhandled exceptions in Qt handlers are logged with their traceback and appear in the error log
  of the open windows, instead of ending up only on a console that the bundle does not have.

- **RIQE** metric (`riqe`, Radiology Image Quality Evaluator) for CT: a NIQE-style distance from
  the `riqe-v1.0` model (doi:10.5281/zenodo.23055559), estimated on 3,792 full-dose slices from
  158 TCIA patients instead of on photographs. The model (CC BY 4.0) is included in the package
  and verified with SHA-256 before every read: no download, no network. It is computed only on
  images declared in Hounsfield units (GUI: DICOM with Modality CT or NIfTI; CLI
  `--hounsfield-units`; batch `"hounsfield_units": true`); otherwise the outcome is
  `invalid/degenerate-input`; a slice with less than 1% air (≤ −900 HU) in the field of view is
  refused as not in HU, so MR, bitmaps or a non-CT NIfTI do not get a score. As in the published
  validation, the comparison image is evaluated on the field-of-view and body masks of the
  reference. The computation reproduces the released reference code: bit-identical on the
  synthetic fixtures and on the 480 reduced-dose pairs of the TCIA test split
  (`validation/no_reference/riqe_verification.json`). Suited to the paired comparison of a
  degraded image with its source; it should not be used on its own to choose or tune a denoiser
  (`qicom/metrics/riqe.py`, `NO_REFERENCE_METHODOLOGY.md`).

- Qualitative evaluation: new first step "Setup" in the authoring path. Anonymity of the answers,
  randomisation of pages/questions/panels, counterbalancing, neutral labels, seed, hidden repeats
  and timing collection are now decided in a dedicated section, before any page is built, instead
  of being scattered between two boxes above the question list and a modal window behind a
  button. The settings are the same and packages already saved reopen without changes: only
  where they are set changes (`qicom/gui/widgets/evaluation_setup_panel.py`).

- "Markdown for AI assistant" export: a single self-contained file with the metric values next to
  the definition taken from the `descriptors/*.yaml` cards, to paste into a chat with an AI
  assistant. Available from the GUI (checkbox in the export panel, with a notice shown only once)
  and from the CLI with `--output report.md`. QICOM performs no AI processing, contacts no
  services and uploads nothing: it writes a file, and any sending is the user's choice. The
  content is decided by an explicit whitelist in `qicom/reporting/llm_export.py` — file names and
  paths, DICOM/NIfTI headers, patient/study/series identifiers, dates, institution, operator,
  hostname and user have no access to the file, and ROI names are counted rather than
  reproduced. The output is deterministic (no timestamp) and follows the interface language.
  Radiomics tables stay out of this v1: they need a summarisation strategy of their own.

- Qualitative format v4 and answers v3: deterministic plan per delivery with randomisation of
  cases/questions/panels, counterbalancing, persisted seed/order, hidden repeats and blinded
  delivery with generic names plus a separate author map. The previous formats remain readable.
- Anchored qualitative scales, general and per-page instructions, excluded calibration pages,
  conditionally mandatory comments, `not_assessable` distinct from skip/missing, forced 2-AFC,
  paired preference and timing with configurable disclosure.
- Ordinal qualitative statistics: median/IQR and distributions, Kendall's W, Krippendorff's alpha
  as primary, Gwet's AC2 as advanced, intra-rater reliability, paired evaluator×case comparison,
  two-cluster bootstrap and Holm. QC and precision produce no automatic exclusions.
- Shared scientific foundations: `StatisticalUnit`, an explicit registry of metric outcomes and a
  versioned reproducibility receipt with hashes of inputs, masks, configuration and results.
  SQLite persistence and exports keep reading previous projects.
- Versioned quantitative profiles (Quick core as the GUI default, Structural, Experimental
  perceptual, Noise/ROI QC, Complete, Phantom/task-based and Custom) and QICOM Index with declared
  profile/composition, experimental status and comparability check.
- Quantitative diagnostics: `comparison - reference` bias, residual SD, medians and percentiles,
  Wasserstein-1, declared linear/orthogonal calibration, 2D/3D phase cross-correlation without
  automatic registration, robust stack statistics and navigation to the worst slice.
- Signed difference map with a diverging LUT centred on zero, symmetric range, tissue/ROI mask and
  PNG export plus a JSON configuration of the scale.
- Traceable NPS from dedicated `noise_texture_roi` ROIs, YAML/JSON acceptance profiles without
  default thresholds, local BRISQUE/NIQE NSS model from a QA corpus, and expert tools for rise
  distance, MTF/TTF/MTF50 and NPW(E) detectability d′.
- IBSI-1 verification shared between GUI and CI: versioned manifest, official download with
  consent, pinned checksums, comparison of 104 digital-phantom features, PASS/FAIL/INCOMPLETE and
  JSON/CSV/Excel/HTML export without certification claims.
- Radiomics project v2 with subject/case/lesion/timepoint/acquisition identity, outcome registry,
  common receipt, QC of masks and features, multi-label NIfTI import/export and read
  compatibility with v1 projects.
- Physical robustness check and preprocessing sweep; table-only multi-project aggregator with
  declared independent units, cohort ICC/CCC, correlations/effective N, associations with volume
  and redundancy clusters without predictive selection.
- SQLite trending with versioned baselines, Shewhart/EWMA and separate user limits; three
  batch/headless commands with logging, cancellation/exit codes, tidy CSV/JSON, receipts and error
  isolation.
- Self-contained local HTML report with CSP, escaping and a size limit, plus a deterministic
  PHI-free demo bundle reachable from the launcher.
- Methodological documentation for radiomics and the operational schemas in
  `RADIOMICS_METHODOLOGY.md` and `OPERATIONS_SCHEMAS.md`.
- `qicom/gui/themes/gray.qss`: a third theme, gray, sitting between light and
  dark - same "Clinico" rules and teal accent as the dark theme with every
  neutral lifted into the mid grays, for when the light theme glares and the
  dark one crushes. Selectable from View ▸ Gray Theme in all three
  workbenches and from Settings ▸ Theme, and persisted like the other two;
  `Follow system` still resolves only to light or dark, since that is all the
  OS reports.
- `qicom/gui/widgets/page_overview_panel.py`: the qualitative form builder's
  `All pages` step - one row per page with its image and question counts, and
  the actions that open, duplicate, rename or delete a page already built.
- `qicom/gui/widgets/wizard_step_bar.py`: the Back/Next footer shared by the
  qualitative building steps, carrying the caption of the page being worked on.
- Qualitative evaluations can shuffle their questions: an author who ticks
  `Shuffle the question order for each evaluator` in the questions step hands
  out a package whose questions are drawn in a different order for every
  evaluator, once, and kept stable if they close it and resume. Presentation
  only - answers, reports and aggregation stay keyed to the question, and the
  package itself keeps the author's own order.
- Qualitative image boxes are named by the author: an unnamed box asks to be
  named instead of showing a default title, and the name is saved in the
  package. It is the only label anyone answering the evaluation sees - their
  copy no longer shows the file name, patient or series behind an image, so a
  comparison cannot be given away by a file called `denoised.dcm`. The
  author's own builder still shows all of it; the preview step shows what the
  evaluator will see.
- `qicom/io/nifti_anonymize.py`: NIfTI de-identification for exported
  qualitative packages - the two free-text header fields (`descrip`,
  `aux_file`), which converters routinely fill with the source series
  description or the original path, are cleared on the copy going into the
  package. A byte-level header patch: no voxel is read or rewritten.
- The qualitative builder now says what it does to the images: a standing
  notice on the images step, and a one-off dialog the first time a DICOM or
  NIfTI image is loaded, stating that the copies going into the package are
  de-identified, that the author's own files are never modified, and that
  file names are copied unchanged.
- `LICENSE` (Apache-2.0), `NOTICE`, `CITATION.cff` and this changelog.
- `qicom/about.py`: one Qt-free module holding the application's identity,
  licence, use scope, DICOM attribution, privacy statement and citation, so
  that the About dialog, the exports and the documentation cannot drift
  apart.
- Help ▸ About QICOM in all three workbenches (quantitative, qualitative,
  radiomics): version and build, authorship, licence, intended use, DICOM
  trademark attribution and NEMA non-affiliation, privacy statement, and a
  copyable citation (plain text and BibTeX).
- Windows version metadata (`CompanyName`, product, description, version
  and copyright) in the PyInstaller bundle, with an automatic check during
  packaging.
- `SHA256SUMS` manifest inside every bundle and a sidecar checksum of the CI
  archive, usable by IT for integrity verification and per-file allow-listing.

#### Release readiness

- **Build identity in the receipt.** `qicom/build_identity.py` resolves the
  exact commit that produced a result: from `qicom/_build_identity.py`
  (generated at build time), then from git in a checkout, then `unknown` —
  declared as such, never guessed, and never "clean" if unknown. The
  reproducibility receipt now carries `build_revision` and `source_dirty`
  (schema 3; schemas 1 and 2 remain readable). A version names a release;
  between two builds carrying the same number there can be any number of
  commits, and at the time of the 1.0.0 audit there were 57. See `VERSIONING.md`.
- **Release consistency gate** (`packaging/check_release_consistency.py`):
  refuses diverging versions between `qicom/__version__.py`, `pyproject.toml`
  and `CITATION.cff`, a stable version without its changelog section, and a
  stable version that leaves entries under "Changes affecting numerical
  results" in `[Unreleased]`. Run in CI and before every packaging build.
- **Generated licence inventory and SBOM** (`packaging/attribution.py`): for
  every profile actually distributed, from the environment the bundle is
  frozen from, failing when a version, origin, licence or licence text is
  missing. `check-bundle` compares what PyInstaller actually collected with
  the inventory. Inventory, CycloneDX SBOM and licences travel with the
  bundle, inside the checksum manifest.
- **Mandatory IBSI gate on tags**: a release cannot be built from a revision
  whose radiomic features diverge from the official reference. A network
  problem is classified separately only in pull requests, never on a tag.
- **Coverage gate** (`packaging/check_coverage.py`): a global non-regression
  threshold that is measured (not chosen) and a *branch* coverage target on
  the selected trust boundaries. See `coverage-floor.json`.
- **Complexity ratchet** (`packaging/check_complexity.py`): no new function
  above ruff's C901 20, no existing one getting worse. The tool is declared:
  radon, mccabe and ruff give different numbers for the same function.
- **Manifest of the installed environment per profile and platform**
  (`packaging/generate_constraints.py`): exact versions, local tags and VCS
  origins actually frozen, published next to the bundle without presenting
  them as a hashed artifact lock.
- **`ARCHITECTURE.md`: the project map, generated from the code.** Every module
  with one line on what it does (taken from the first sentence of its
  docstring: all 167 have one), the entry points, the trust boundaries, the
  version constants of the persisted formats and the build gates. Written by
  hand it would be the most drift-prone document in the repository; generated
  by `packaging/generate_architecture_map.py` and checked in CI, it cannot lie.
  Line counts are rounded to the nearest hundred because a map that changes
  every time a function grows by three lines is a map nobody regenerates.
- `packaging/DISTRIBUTION.md` collects the distribution and signing requirements. The
  specification of the quantitative AI export is implemented in every point; the AI export for
  radiomics tables remains future work, instead of being half-implemented.
- Markdown export for AI-assisted reading: a **Citation** section asks the model that an article,
  a report or a presentation written from the file cite QICOM with the same reference as About
  QICOM and `CITATION.cff`. When RIQE is among the results, the file adds the study
  (arXiv:2610.00384), the model release (doi:10.5281/zenodo.23055559) and the citations and
  acknowledgement that the model's CC BY 4.0 licence requires
  (`qicom.metrics.riqe_model.CITATIONS`). The references stay in English in every language; no
  additional data enters the file.

### Changed

- **Documentation in English.** `SECURITY.md`, this changelog, the tester guide and the Windows
  readme files, and the generated `ARCHITECTURE.md` are now in English like the rest of the
  documentation. The files that travel in the bundles are renamed accordingly:
  `packaging/LEGGIMI-TESTER.txt` → `packaging/TESTER-GUIDE.txt` and
  `packaging/windows/LEGGIMI-PRIMA.txt` → `packaging/windows/README-FIRST.txt` (at the top of
  `QICOM-windows-x64-portable.zip`). The interface languages are unchanged. The same pass
  corrected what the documents said about the current version: `SECURITY.md` and the tester
  guide counted two network accesses (or one) and now list the update check as well; the tester
  guide and the Windows readme describe updates, the downloads on qicom.ch (without the learned
  metrics) and the Linux log and data folders; the README describes the Windows installer and
  the portable zip.
- Mouse on images, in all workflows and in the utility tools: dragging with the left button
  adjusts window and level (horizontally the contrast, to the right the window widens;
  vertically the brightness, downwards the image gets darker) instead of moving the image;
  panning stays on the middle button and zoom on the right one, and the wheel scrolls through the
  slices. Every drag is a single undoable change and "Sync Levels" follows it live. With an ROI
  tool selected, even after adding the ROI, dragging, panning and zoom stay active (previously
  they were all off until you went back to Zoom / Pan): dragging over an ROI moves it, dragging
  outside adjusts the levels. They stay inactive in Annotation mode, where the left button places
  an annotation, and while erasing or adding background.
- Question editor: for the choice formats (single, multiple, drop-down menu, ranking) the options
  are written in a numbered list, one row per option, with the number of options chosen
  explicitly (from 2 to 20, default 5) instead of the "one option per line" box. A row left empty
  uses the suggested text: a quality scale (from Unacceptable to Excellent) for the choice
  formats, "Option n" for ranking and beyond seven options. The rows that the chosen format does
  not use (scale anchors and range, option list) are now hidden instead of disabled. The saved
  format does not change.
- Acronyms and standard names stay in English in all languages, as in the reference text: CT,
  MRI, CI, QC, AI, IQR, MAD, HU, MB and Z-score replace TC, RM, IRM, MRT, TDM, IC, KI, CQ, IA,
  ÉIQ, ÉAM, UH and Mo in the Italian, French and German translations. The Italian, French and
  German descriptions of Wasserstein-1 now also state the calibrated units and the absence of a
  KS p-value, like the English original.
- Review of the texts in all languages. Italian, French and German use their own language's
  capitalisation instead of English Title Case ("Nuovo progetto", "Projekt laden"). Metric names
  follow the same rule ("Errore quadratico medio"). Typos, inconsistent terms and incomplete
  sentences were corrected: for example *repeatability* is "ripetibilità", "répétabilité" and
  "Wiederholbarkeit", not "riproducibilità". The English reference text now uses only American
  spelling ("color", "analyzed", "neighbor").
- **Receipts at schema 4: strict canonical JSON.** In the previous form, the float NaN and the
  string "NaN" gave the same hash, the keys `1` and `"1"` collapsed, and a NumPy boolean became
  the string "True". Now a string equal to a special token is marked, colliding keys are refused
  and NumPy booleans stay booleans. For everything else the encoding is identical byte for byte.
  A receipt at schema 3 stays schema 3 and keeps its own hash; the identities of NSS models and
  acceptance profiles keep the previous form.
- Images that cannot be anonymised: saving a project or an evaluation no longer stops at the first
  refused image. It collects all the images that cannot be anonymised, for whatever reason,
  writes nothing, and tells the user which ones they are, why, and that they can still proceed
  without anonymisation for those images. The writers raise `ImageDeidentificationRefused` (new
  `qicom/io/deidentification.py`, with the full list in `refusals`) instead of
  `DicomDeidentificationError`; `anonymize_dicom_file` accepts a `refused` collector.
- The description of NIQE (`niqe_canonical`) warns that the model is estimated on photographs,
  that its relationship with CT quality has not been shown, and that RIQE should be used for CT.
  Text only: formula, model and values are unchanged.

- Qualitative evaluation: the New/Edit Question window is divided into three titled sections -
  "Question", "Answer format" (answer type, options, scale anchors, score range) and "Answer
  rules" - instead of a single flat list of ten rows. When the format changes, the fields that do
  not apply are disabled inside their section rather than at scattered points of the list.

- The deterministic synthetic bundle is no longer a button on the first page of the launcher,
  where it competed with the choice of workflow and nobody used it. It now lives in
  **Settings → Diagnostics**, is also offered by **Help → Report a Bug** (the moment it is really
  needed) and is available without the GUI with `qicom-cli demo <folder>`. The body of the
  report email and the new GitHub issue template explicitly ask for it to be attached.
  Generation stays atomic and never overwrites an existing folder; from the GUI the next free
  name (`-2`, `-3`, …) is chosen automatically, so the command also works the second time, and
  at the end the folder can be opened directly. The translation keys `launcher.demo_*` become
  `demo.*`.
- Defined and audited the public API deprecation policy: the three deprecated
  compatibility adapters have no QICOM runtime callers, remain covered by
  compatibility tests, and are retained until the next major release rather
  than being removed from the 1.x line.
- Qualitative form builder: the authoring steps are now a guided pass over one
  page at a time (`Images → Questions`) followed by a new `All pages` overview,
  and each building step ends in a Back/Next bar rather than relying on the
  workflow rail being clicked. The overview lists every page with how many
  images and questions it holds and is the only place a page is opened,
  duplicated, renamed or deleted; adding one returns to the same `Images` step
  instead of stacking another section on screen. The page strip that used to
  sit above the question cards is gone, and `Add` in the question list now
  reads `Add question`.
- Background (air) exclusion: the step declares itself optional in a notice
  pinned to the top of the column and is switched on with a full-width toggle
  rather than a small checkbox, so skipping it is a deliberate choice instead
  of an unnoticed one. The column's controls stack vertically and fill its
  height, and the air sample is now explicitly a single ROI - the Add button
  turns itself off once the circle exists, since a second sample would quietly
  widen what counts as air.
- Every piece of text the application displays now uses a plain hyphen `-`
  where it used an em dash or an en dash: window and section titles, the
  metric rows, the history entries, chart titles, slice ranges and the
  translated strings in all four languages. The `-`/`+` stepper buttons keep
  their U+2212 minus sign, which is sized to match the `+` beside it.
- Analysis setup: the metric checklist is laid out in two columns, and
  `Calculate` spans the full bottom edge of the page in a colour used nowhere
  else (indigo), so it is no longer one more coloured rectangle among the
  green/amber/coral effort badges above it.
- Background (air) exclusion guides with one coloured button at a time: the
  step that is actually due wears the teal accent and every other button in
  the column - including the ones already used - drops to a muted grey, so
  once the exclusion has been detected nothing in the column is coloured any
  more. Previously the toggle, `Add air ROI` and `Detect air` were all teal
  simultaneously, which said nothing about which one to press.
- Calculate reports its progress inside the Analysis setup page instead of in
  a modal dialog floating over it: the bottom edge of the page swaps the
  `Calculate` button for a progress bar, the metric being computed and a
  `Cancel` button. The run still owns the application while it lasts - the
  other workflow steps, the menus, the toolbars, the image canvas and the
  other panels are disabled until it finishes or is cancelled, which is what
  the dialog's modality used to provide. Export keeps its own modal progress
  dialog.
- Version raised from `0.1.0` to `1.0.0`: the first release published under
  semantic versioning, with a documented licence and a citation form.
- Project metadata in `pyproject.toml`: licence declared as `Apache-2.0`
  (previously `Proprietary`, which contradicted the intent to distribute
  QICOM freely), full authorship, project URLs and classifiers.

#### Release readiness

- **Development version on `main`.** `1.0.0` → `1.1.0.dev0` in all three
  sources. `main` carries the version of the release being worked towards, not
  that of the last release: as long as `[Unreleased]` contains changes that
  alter the numbers, any stable number would declare a release that does not
  describe them. **No `v1.0.0` tag has been created**: a tag asserts *this
  commit is what was distributed*, and that must be demonstrated first.
- **Hermetic suite.** Every test runs with the data/config/cache/log homes
  redirected to a temporary directory; a tripwire fails the session if the
  suite writes into the real profile. Previously, reopening a radiomics project
  in a test wrote into the user's real `recent_projects.json`.
- **Transactional qualitative authoring.** The author document has a monotonic
  revision; Close/New/Open ask Save/Discard/Cancel; saving writes an immutable
  snapshot. *Create Blinded Delivery* combined the pages, media and questions
  of the saved archive with the panel's **live** design: it now uses the two
  halves of the same snapshot, or refuses.
- **Qualitative saving off the GUI thread**: anonymisation, copying and ZIP in
  a worker with cooperative cancellation and an atomic commit; no more
  `QApplication.processEvents()` in the save path.
- **Learned policy declared and demonstrated**: the checkpoints are not
  included in any bundle (the README claimed the opposite). The frozen smoke
  test runs with empty home and cache and checks both the offline refusal and
  the explicit preparation path.
- **Real budgets** on downloads and archives: `size_bytes` in the IBSI
  manifests, verified `Content-Length` and a streaming counter always active,
  an up-front free-space check, quotas per package type (project, evaluation,
  radiomics, cohort, workbook) and a counter on the bytes actually extracted.
- **Installer bootstrap**: pip upgraded to ≥26.2 before any dependency, in CI
  and in packaging. The audited revision ran with pip 24.0 and seven known
  vulnerabilities. The environment that produces the bundle is now audited
  too.
- **Dependencies clear of security advisories**: the packaging audit found 57 known
  vulnerabilities in three packages of the locks. Pillow moves to `>=13.0,<14` (12.3.0: 39
  PYSEC-2026-43xx advisories; the `Image.fromarray(..., mode=...)` calls that Pillow 13 refuses
  had already been removed), filelock to 4.2.6 (4.1.0, via torch: 17 CVEs) and setuptools to
  84.2.1 (84.0.0: GHSA-xmj3-9gf7-h52w), with the build-system minimum raised to `>=84.1`. The lock
  installed after the bootstrap can no longer bring setuptools below that minimum:
  `generate_locks.py` constrains the resolution to the build-system range and `--check` refuses a
  lock that falls outside it. New `--upgrade-package` option to move only the pins affected by an
  advisory. PNG and TIFF files written by `Image.fromarray` are byte-for-byte identical between
  Pillow 12.3 and 13.0 in modes L, I;16, RGB and RGBA.
- **MIRP jobs and `pip check`**: `pip check` (pip 26.2.2) stopped with a `ValueError` on itk, a
  dependency of MIRP, whose 5.4.x wheels write `Tag: itk-5.4.7-cp311-abi3-…` instead of
  `Tag: cp311-abi3-…`, and the MIRP jobs could neither validate nor audit the environment.
  `packaging/repair_wheel_tags.py` removes only that prefix, when it is exactly the name and
  version of the distribution itself, and runs in the only two MIRP jobs, before the check:
  `ibsi-mirp-gate` (tags, `package.yml`) and `ibsi-mirp` (every push, `ci.yml`). Neither
  environment ends up in a bundle and `pip check` stays complete. Until this fix the script
  appeared only in the tag gate, which runs only on the push of a `v*` tag: it had never been run
  in CI, and the `ibsi-mirp` job still failed in the audit, as before the script was introduced.
- CI: Python 3.11/3.12 matrix, Actions pinned to commit SHAs with Dependabot
  updates, IBSI resources cached on the manifest hash, and the second full run
  of the suite replaced by collection only (which is what actually checked the
  `sys.path` difference).
- Benchmarks: `excel_only` and `full_export_with_ai` separated, versioned
  schema, and comparison refused between different schemas, profiles or hosts.
- `packaging/THIRD_PARTY_LICENSES.txt` no longer describes QICOM as proprietary
  (it has been Apache-2.0 since the first public commit) and no longer declares
  the learned profile out of scope, which the tags build.

### Fixed

- **The air ROI is optional again for the calculation.** With "Exclude the air" on and no green
  ROI placed, Calculate stopped with "Create the green air ROI first" and produced no
  measurement. It also happened when reopening a saved analysis with the exclusion on: the
  settings come back, but the green ROI does not. Now the calculation proceeds on the whole
  image, air included, with a warning, and the analysis is recorded as "without exclusion", so
  that saving and export describe what was actually computed. A placed green ROI that covers no
  pixel remains an error, and "Detect air" still asks for the ROI. No value that could be computed
  before changes.
- **A busy database is no longer mistaken for a damaged one.** With several QICOM windows started
  together (for example after a slow first launch, to which the user responds by relaunching),
  one found the session database locked by the other and treated it as damaged: it tried to move
  it aside and create a new one. On Windows the move failed on the open file, but the log still
  said a new one had been created; on Linux and macOS the move would have succeeded. Now only a
  corrupt or non-SQLite file is moved aside; a busy database is waited for (up to 10 seconds, a
  timeout set before every statement) and, if it stays busy, opening fails with an error without
  touching anything.
- **Starting a workflow with nothing on screen.** After a workflow was chosen in the launcher, the
  launcher closed at once and the window could take a long time to appear (on first launch
  Windows checks every file of the application): a tester believed QICOM had closed and relaunched
  it several times. Now the logo stays on screen with "Opening: Quantitative analysis..." (or the
  chosen workflow) and the wait cursor, until the window appears.
- **Errors one click away, readable bottom panels.** The error indicator in the status bar was
  red text with no border: it is now a button with an icon, an outline and the caption "N errors:
  show", and the error message that has just appeared at the bottom also opens with a click (hand
  cursor and tooltip while it stays visible). Opened from View > Panels, History, Log and Errors
  got whatever height was left: a single row in six steps out of eight. They now get a third of
  the window, the tables never go below four rows and the edge stays draggable; the Errors panel
  has "Show All" (also by double-clicking a row) to open the full list.
- **Heatmaps disappeared from the images when changing step.** The air preview and the heatmaps
  use the same layer above the images, and at every change of step away from Remove air the
  preview was removed by clearing that layer even when a map occupied it: the map was no longer
  visible, while the panel still said it was shown. Now only a preview that is actually visible is
  removed. In addition, the Heatmaps step no longer opens empty: it immediately shows the
  whole-image map for the chosen metric, a line explains what the map is, that Calculate is not
  needed and what the export saves, and a status line says what is shown, or why there is nothing
  to show (missing images, metric that cannot be computed).
- **A new project no longer overwrites the one opened before.** "Reset all" cleared the window but
  remembered the file of the project opened previously: on closing, "Save" wrote the new work
  into that file without asking anything. Now starting over forgets the file and the next save
  asks where to write. The question on closing names the project and the file "Save" will write
  to.
- **Windows larger than the screen.** The windows open maximised, but "Restore" brought them back
  to a fixed size (1280×800, 1400×900 for Tools and Radiomics): taller than a 1366×768 laptop and
  larger than almost any screen at 125-150% scaling. The restored window went off screen and the
  edge for resizing it stayed out of reach. Now the initial size is limited to 92% of the screen's
  available area, in logical pixels, and the window is centred. In the Tools window, moreover, the
  minimum width of some tools (up to about 1230 pixels) became that of the whole window, which at
  125-150% no longer fitted on screen and could not be narrowed: each tool now sits in a
  scrollable area. A test checks every tool at 1366×768 at 100%, 125% and 150%.
- **Utility tools missing in the installed app.** The catalogue imports each tool by name on first
  use (`qicom.gui.tools.converter:ConverterTool`) and no other module imports them, so PyInstaller
  did not see them: in the distributed app every tool failed with "No module named
  qicom.gui.tools.converter". The spec now collects the whole `qicom.gui.tools` package, the
  bundle smoke test imports every catalogue entry and CI refuses a bundle that reports none of
  them. If a tool is still missing, the Tools window explains it in words in place of the tool and
  keeps the detail for the bug report.
  On Linux the build ran before the Qt libraries were installed, and collecting the package, which
  imports every module to find it, silently discarded all the tools that import QtGui: the Linux
  bundle was left without them. Now the spec lists the tools from the source files, and on Linux
  the Qt libraries are installed before the build.
- **mypy with numpy 2.5.4**: the stub of `np.isscalar` moved from
  `TypeGuard[generic | complex | str | bytes | memoryview]` to `TypeIs[… | numbers.Number]`, and
  the call `np.isnan(result)` in `qicom/metrics/runner.py` no longer type-checked (`arg-type`).
  The scalar result is now converted with `np.asarray` before `np.isnan`, the same conversion the
  ufunc does by itself: no outcome changes, including the types on which `np.isnan` raises
  `TypeError` (text, bytes, `Fraction`, `Decimal`), which remain accepted. A test pins the
  outcomes. The error already existed before PR #4 was merged, hidden by the audit failing before
  the static stage; the locks pin numpy 2.5.3, while CI installs without locks and resolves 2.5.4.
- **Radiomics projects without typed tables: the text "NA" became missing again with pandas
  3.1.** `read_legacy_table` protected the known text columns with a converter, but pandas
  applies the missing-value markers ("NA", "null", "None", …) to the converter's result as well:
  the Python engine always did, the C engine since pandas 3.1. An ROI or a status called "NA"
  became missing. The text columns are now read separately without any marker, and only an empty
  field is missing; the other columns keep pandas' markers. `float_precision`, deprecated by
  pandas 3.1 (`Pandas4Warning`) because the default parser has become exact, is passed only with
  pandas < 3.1, where it is still needed to read floats bit for bit. Verified with pandas 2.3.3,
  3.0.6 (lock) and 3.1.0. The error already existed before PR #4 was merged: CI, without locks,
  installs pandas 3.1.0.
- **Python 3.11: the GUI terminated with SIGABRT with PySide6 6.12.0.** Every call to the size
  setters of `QGraphicsWidget` (`setMaximumHeight`, `setMinimumWidth`, `setPreferredHeight`, …)
  drops a reference to `None`, and pyqtgraph calls them at every resize. From Python 3.12 `None`
  is immortal and nothing happens; on 3.11 the count reaches zero and the interpreter aborts
  (`Fatal Python error: none_dealloc`). In the `test-base (3.11)` job the suite stopped after 68
  tests in `test_adaptive_layout.py` with neither traceback nor summary: pytest's capture kept
  faulthandler's output in a temporary file lost with the process, and `check_ci.py` returned the
  `returncode` −6 of the killed pytest, which `SystemExit` turns into code 250. The `gui` extra now
  excludes 6.12.0 only on Python < 3.12 (6.11.3 does not have the defect); the bundles, on 3.12,
  stay on 6.12.0 and the locks are unchanged. `generate_locks.py --check` evaluates the markers
  for the interpreter and platform of each lock. A test directly measures the reference count of
  `None` around those setters. The error already existed before PR #4 was merged.
- **Quantitative window wider than a 512 px screen in Portuguese.** The empty text of the Charts
  panel ("Ainda não há nada para exibir no gráfico: execute Calcular primeiro.") did not wrap,
  and its single-line width became the minimum width of the dock: with DejaVu Sans only, as on
  the `ubuntu-latest` runner, the window reached 529 px in the results stage. The label now
  wraps; the minimum width drops to 363 px. With Liberation Sans, the Arial substitute on many
  Linux installations, the text just fitted in 512 px, which is why the test passed locally.
  Pre-existing.
- **Qualitative evaluation: answers disappeared when scrolling through slices.** Every answer was
  recorded on the reference slice of that moment, so on a series with several slices the form
  was emptied at every change of slice and the answers reappeared only when going back to that
  slice, as if every slice were a question of its own. The design now has the setting "Answers
  apply to" (step "Evaluation settings"): "The whole series", the default for new evaluations,
  records a single answer per page, comparison and question (at `slice_index` 0) and lets the
  series scroll without touching answers or timings; "Each slice separately" keeps the previous
  behaviour. The slice indicator says which of the two applies. The `design.answer_scope` field
  travels in the package and in the blinded delivery; a design saved earlier, without the field,
  is read as per slice, so the answers already collected stay where they were
  (`OPERATIONS_SCHEMAS.md`, `qicom/session/qualitative_design.py`,
  `qicom/gui/qualitative_controller.py`).
- **Trackpad: one gesture skipped many slices.** Every wheel event moved by a whole slice; the
  trackpad (especially on macOS) sends dozens of them per gesture, plus the inertia ones after the
  fingers are lifted and a final one with no vertical movement, read as "up" and therefore one
  slice back. Now the movements add up to one wheel notch (120 units), inertia is ignored and an
  event without vertical movement moves nothing, in the workflows and in the utility tools
  (`qicom/gui/wheel_slice_steps.py`). A notched wheel behaves as before.
- **Blinded deliveries reconstructable from the evaluator's package.** The public plan contained
  the seed, the public design the master seed, and pages and questions kept the author's IDs, with
  the hidden repeats numbered after all the others: from the evaluator's ZIP alone one could
  reconstruct which panel was the reference and which pages were repeats. Now the seed mixes in a
  secret derived from a random key of the author, kept private in their profile (the same
  delivery recreated by the same author stays identical, so the evaluators' drafts and the map
  remain valid), and seed, secret and master seed live only in the author's map (version 2,
  `OPERATIONS_SCHEMAS.md`); the public plan carries `seed: 0`, pages and questions are numbered in
  presentation order, and the public design reports neither randomisation nor repeats
  (`qicom/session/evaluation_package.py`, `qicom/session/qualitative_design.py`). Aggregation
  checks the answers of each delivery against the structure that delivery presented,
  reconstructed from its map on the author's package or on the map of the loaded package, and
  reports the author's IDs (`qicom/session/evaluation_aggregation.py`). A version 2 map must be
  complete: a page, a question or a panel that it does not map is refused, because with opaque
  IDs the presented number would name another source. Version 1 maps remain readable as before.
  **Deliveries created with previous versions still expose the randomisation to whoever received
  them**: for an ongoing study, consider generating new ones.
- **Blinded deliveries and images that cannot be de-identified.** An image let through by an
  override when the author had saved their own package entered the delivery as it was: the
  unverifiable NIfTI was rescanned with the list of refusals discarded, and the DICOM was not
  de-identified at all (only stripped of the acquisition attributes). Now every image is
  de-identified again for the delivery, with UIDs of its own for each case; a refusal stops the
  delivery before any file is written and the window asks the author whether to include those
  images anyway, for this delivery only.
- **Delivery that failed halfway.** The map was written before the ZIP: if the ZIP then did not
  reach its destination, the previous delivery remained next to the new map, which names other
  images. Now the previous map is set aside and put back in place if the delivery is not written;
  without a previous map, the new one is removed. Two deliveries to the same destination, even
  simultaneous ones, write their pair one after the other (exclusive lock per user and machine,
  `qicom/file_locks.py`), and the map records the manifest hash of its own delivery
  (`delivery_manifest_sha256`).
- **Utility tools: NIfTI with the geometry of another stack.** For multi-frames the positions were
  reordered along the normal even when the loader had kept the file order (repeated positions),
  and for series the step was derived from the first and last slice even with a missing slice:
  the written affine did not describe the written pixels, and was declared known. Now the slices
  are placed from the positions the loader delivers in pixel order; a stack that no affine
  describes (irregular steps, repeated positions, frames with different orientations even if the
  positions are regular) is written with qform/sform set to 0, and a tilted stack keeps the shear
  only in the sform, which the qform cannot represent (`qicom/io/geometry.py`).
- **CI: MIRP gate in the main test job.** The job sets `QICOM_RUN_IBSI_REFERENCE=1` for the
  PyRadiomics gate but installs neither MIRP nor its cache, and the MIRP test treated it as a
  failure. The MIRP gate now has a variable of its own, `QICOM_RUN_IBSI_MIRP`, set only in the
  jobs that prepare it.
- **DICOM anonymisation: Part 10 container.** The 128-byte preamble and the File Meta group were
  copied from the original, with `PrivateInformation` and its data, into files declared
  `PatientIdentityRemoved=YES`. Now the container is rebuilt as PS3.15 requires: preamble zeroed,
  File Meta with only the elements required by PS3.10, check on the re-read copy
  (`qicom/io/dicom_anonymize.py`).
- **NIfTI anonymisation: structural recognition.** It was enough for the first four bytes to be
  348 or 540: a file without magic, with identifying text after the header, was exported as
  verified. Now magic, dimensions, datatype, voxel offset and completeness of the payload must be
  consistent; everything between header and voxels is zeroed and the bytes after the payload are
  dropped (`qicom/io/nifti_anonymize.py`).
- **Radiomics projects: tables reopened different from those saved.** CSV does not keep types: an
  ROI named `"001"` came back as the integer `1`, the text `"NA"` as a missing value, and the
  reopened table no longer had the hash recorded in the receipt. Every table is now also saved as
  typed JSON, which QICOM reads back value by value; the CSV stays for other tools. Previous
  projects are read from the CSV, keeping the known text columns as text
  (`qicom/radiomics/project.py`).
- **Radiomics cohort on stale results.** Aggregation accepted projects marked `results_stale` and
  tables that no longer matched the receipt, and computed their repeatability. Now it refuses
  them, with the reason (`qicom/radiomics/cohort.py`).
- **RIQE in the CLI and in batch.** Only the GUI passed RIQE the orientation read by the loader:
  CLI and batch evaluated a sagittal stack as axial. Now both pass it (`qicom/cli/main.py`,
  `qicom/batch.py`).
- **Qualitative evaluation: the File menu disappeared after the first save.** Locking the window
  during saving asked Qt for the menu bar, which instead of the one with the logo created a new
  empty one: File, View, Tools and Help disappeared, and the only way to leave the evaluation was
  to close QICOM. The bar now stays the real one, and the File menu item that returns to the start
  is called "Close and Return to Start", with the shortcut Ctrl+W
  (`qicom/gui/widgets/qualitative_main_window.py`).
- DICOM compressed with lossless JPEG (Process 14, SV1 and not) and JPEG-LS, the format of most CT
  and MRI exams exported from scanners and PACS: QICOM refused them with "cannot decode with its
  bundled decoders", because pydicom and Pillow on their own decode only RLE, baseline JPEG and
  JPEG 2000. `python-gdcm` is now a base dependency (locks, attribution with the licences of GDCM
  and of the libraries it embeds in `packaging/attribution/notices/python-gdcm.txt`, a bundle
  smoke test that decodes a frame). Baseline JPEG and JPEG 2000 are still decoded by Pillow even
  though pydicom would ask GDCM first, so the pixels of files that already opened do not change;
  GDCM decodes only what Pillow cannot (`qicom/io/dicom_io.py`). The message for syntaxes that
  still cannot be decoded (HTJ2K) no longer suggests installing GDCM.
- Quantitative results: the note "N requested metric outcome(s) require attention" also counted
  the metrics that the profile had not selected (status `not-requested`), so a run with seven
  metrics, all computed, reported 30 outcomes to check. Now it counts only the requested metrics
  that were not computed, and the note stays hidden when there are none.

- DICOM anonymisation in nested sequences: `dicom-anonymizer` applies the PS3.15 rules only at
  the root level, so in Enhanced multi-frames (and in every nested sequence) per-frame acquisition
  dates, source-study UIDs and derivation references remained, in files declared
  `PatientIdentityRemoved=YES`. The rules are now applied to every nested element, every remaining
  instance UID, date, time or person name is replaced (consistently within the package), the
  source AE titles in the File Meta are removed and the copy is checked against the UIDs and dates
  of the original. DICOMDIRs are recognised explicitly and are not anonymised
  (`qicom/io/dicom_anonymize.py`).
- Incomplete NIfTI anonymisation: only `descrip` and `aux_file` were zeroed. Now `intent_name`,
  `db_name`/`data_type` (NIfTI-1) and `unused_str` (NIfTI-2) are zeroed too, the header extensions
  (where converters copy the whole DICOM header, a JSON or the AFNI history) are removed, and a
  `.nii.gz` is always recompressed without the original file name or the date in the gzip header.
  A file with a NIfTI suffix that cannot be verified is no longer copied silently: it follows the
  same flow as DICOM files that cannot be anonymised. The quantitative ZIP project now also cleans
  NIfTI headers (`qicom/io/nifti_anonymize.py`).
- Bitmap metadata in packages: the standard evaluation package (and the ZIP project) copied JPG
  and PNG files with EXIF, XMP, JPEG comments and PNG text chunks, while the dialog declared
  "every image" anonymised; only the blinded delivery cleaned them. Now they are removed losslessly
  at byte level, keeping the file's name, format and compressed data (only a non-default EXIF
  orientation, if any, and the ICC profile with a neutral name remain; data appended after the end
  of the image is dropped) (`qicom/io/bitmap_anonymize.py`).
- Formula injection in CSV exports: only the XLSX files and the CLI's CSV neutralised cells
  starting with `=`, `+`, `-` or `@`. Now so do the CSV export of the qualitative aggregation
  (names and comments written by evaluators, untrusted input), the CSV of the Log panel, and the
  CSVs of radiomics, of batch and the tidy CSV. Numeric values stay numeric; the tidy JSON remains
  the exact form for the round trip.
- Loaded NIfTI files stayed memory-mapped onto the source file (`nib.load` with `mmap=True`): if a
  pipeline rewrote the file, the following metrics used the new pixels while display and
  `data_range` stayed the old ones, and a file truncated in the meantime made QICOM terminate with
  SIGBUS, without a log. Now the pixels are read into memory on loading (`qicom/io/nifti_io.py`).
- Provenance: a `qicom/_build_identity.py` left in the checkout after a local build (it is
  gitignored) took precedence over git even outside the bundle, so every result computed
  afterwards from source was attributed to the commit of that build, declared clean. Now the
  generated module applies only in the frozen bundle; from source the checkout is queried, and the
  generated module is only a fallback when there is no repository (`qicom/build_identity.py`).
- At startup the launcher closed if `recent_projects.json` had an unexpected shape (a list,
  `null`, non-UTF-8 text) or if a recent project was in a folder that was no longer readable
  (`PermissionError`); now those cases give an empty list or skip the entry, and the file is
  written with a unique temporary file.
- Per-slice diagnostics with "exclude the air" or with ROIs: slices without tissue are skipped,
  and `worst_slice`, `outlier_slices` and the "worst slice" button indicated the position in the
  shortened list instead of the volume slice (with three initial slices of air only, 6 instead of
  9). Now they report the index of the slice in the volume (`StackAggregateResult.slice_indices`).
  In addition, a single unchanged slice (infinite PSNR) made the whole calculation fail: the robust
  summary now uses the slices with a finite value. A slice on which the metric is not defined (for
  example a constant reference in the tissue) was dropped from the volume value with the outcome
  "computed" and no note at all: now the outcome says on how many slices it was computed and which
  ones were excluded. The metric values do not change.
- Resuming evaluations in progress: the local file was keyed only on study and computer, and all
  the blinded deliveries of a study share the study. A second reader on the same workstation
  resumed the first one's answers, presentation plan and submission identifier; an unreadable file
  (for example written by a newer version) was ignored and then overwritten at the first click.
  Now the file is also keyed on the delivery, QICOM asks whether the evaluation found belongs to
  whoever is answering (No starts a new one), and an unreadable or not-resumed file is kept
  aside, with a warning in the error log.
- Calculate pressed while "Open Project" reads the archive (the wait leaves the interface active)
  computed on the previous images, and the results were attributed to the project just opened.
  Now the calculation is refused with a message during opening, and every calculation started
  earlier is invalidated even once reading has finished.
- Security of inputs and downloads: an XLSX answer file (untrusted input) now goes through the
  archive inventory check before pandas (400 KB could become almost 1 GB in memory); importing a
  label map with many labels asks for confirmation before allocating one mask per label; the
  download of the learned weights immediately refuses a `Content-Length` different from the
  expected size, as already declared in SECURITY.md (the byte counter stays).
- Documentation of the network access of the IBSI verification (official IBSI resources
  downloaded on request, with SHA-256 and size pinned): About (4 languages), SECURITY.md and
  `coverage-floor.json`, where `ibsi_validation.py` becomes a trust boundary.
- Blinded delivery: DICOM files copied without changes revealed the study arm through
  manufacturer, reconstruction kernel, protocol, dose and descriptions. Now in the delivery these
  attributes are blanked (the equipment and algorithm sequences removed), the pixels stay
  unchanged; NIfTI headers are cleaned again, for packages saved by previous versions. The
  author's package does not change.
- The cards of the historical NSS distances (`brisque`, `niqe`) warn that with the default
  synthetic model adding noise can reduce the distance (a test documents it): they are not noise
  measures. Formula and direction unchanged.
- GUI and CLI warn when the `data_range` exceeds the span of the image values by more than 8 times
  (typical of 16-bit CT, where SSIM and PSNR hardly respond to differences). The default value
  does not change.
- Batch receipts: a missing input received as its "hash" the SHA-256 of the text of its path, as
  if it had been read; now it is recorded with the `missing:` prefix. The paths (package, answers,
  maps, projects, output folder) are under `configuration.locations`, excluded from the
  `configuration_hash`, and the cohort's `result_hash` no longer includes the `project_path`
  column: the same calculation run from another folder or machine has the same hashes. Existing
  receipts remain readable and unchanged.
- Persistent writes: temporary files had a fixed name (`.<name>.tmp`), so two windows saving the
  same file wrote into the same temporary file (a corrupt archive reported as saved) and a
  symbolic link planted with that name was followed. Now every write uses a unique, unpredictable
  temporary file, the data is synced to disk before the rename and, on POSIX, so is the directory
  (`qicom/atomic_files.py`, used by projects, evaluations, answers, evaluator identity,
  de-blinding maps, tidy outputs, workbooks, radiomics projects, label maps and IBSI
  verification).
- Persistence: unreadable preferences (damaged or from a newer version) were overwritten with the
  default values at the first change; now the file is kept aside before saving. A truncated NSS
  cache blocked the NSS distances for good; now it is written atomically and an unreadable cache
  is recomputed. Radiomics project tables are read back exactly (`float_precision="round_trip"`):
  previously one value in three came back a few ULPs off and the reopened table did not match the
  `result_hash` of its receipt.
- The quantitative ZIP project did not keep the receipt and the metric outcomes (they travel as
  DataFrame attributes, lost in the manifest): reopened elsewhere, it exported the numbers without
  build revision, input hashes, configuration and outcomes. Now the manifest has the optional field
  `calculation_records`, read with a default for previous projects (format unchanged, v1).
- Demo bundle: `qicom_demo.json` lists in `run_specific_files` the files that record the moment
  of their own generation; the documented comparison procedure applies to all the others.
- Damaged files: a truncated archive or one with corrupt members raised `BadZipFile`, and a
  manifest with fields of the wrong type `TypeError`/`AttributeError`, which the GUI did not
  handle: opened from the launcher, they closed QICOM without any message. Now they are input
  errors with a message, and the launcher returns to the choice of workflow if opening fails for
  any reason. An unreadable session database is kept aside (with its `-wal`/`-shm` files) and
  replaced by a new one, with a warning in the error log; the database now has a version
  (`PRAGMA user_version`) and one written by a newer version is refused instead of being used
  blindly.
- Available RAM and CPU: on Linux the "available RAM" was `MemFree` (without the reclaimable page
  cache: spurious memory warnings and an unwarranted `constrained` profile); on macOS it was
  unknown, so the check before loading a volume never triggered. Now QICOM uses `MemAvailable`
  (with the cgroup limit in containers) and `vm_stat` on macOS; the CPU count respects the
  process affinity.
- Every error shown by the interface starts with a translated sentence on what failed ("The
  project could not be opened."), followed by the technical detail. The reasons for anonymisation
  refusals, the refusals of unsafe archives and the messages about unreadable or unsupported image
  files are translated at the source, in the four languages; the other technical details stay in
  English, below the translated sentence.
- Fitting a local NSS model shows per-file progress and can be cancelled.
- Interface: the radiomics configuration panel follows language changes (its 43 texts stayed in
  the language at opening) and no longer has fixed English labels; the file filters, the metric
  names in heatmaps, ROIs and charts, the extraction step and other visible strings are
  translated; the metric search also finds the translated name; the region-growing seed shows its
  intensity; eight unused translation keys were removed.
- The English descriptions of BRISQUE, NIQE, canonical NIQE/BRISQUE and EPI came from copies in
  `en.json` that diverged from the YAML cards, which are the source of the English text: the
  copies were removed.
- Fitting a local NSS model happens off the interface thread; an invalid data range is reported
  in the user's language and every error is shown instead of escaping the dialog.
- A damaged evaluator identity file (or one of the wrong shape, such as `[]`) produced a new id,
  and every answer saved on that workstation was left orphaned; `[]` raised AttributeError. Now
  the file is set aside and, if the saved answers agree on an id, that id is taken up again.
- A blinded delivery was written before its de-blinding map: if the map failed, a delivery
  remained that nobody could de-blind any more. Now the map is committed first.
- A receipt with an identifier containing spaces (`" S1 "`) changed its hash on reloading, and
  `source_dirty: "false"` became `True`. Identifiers are now cleaned at construction and the flag
  must be a boolean.
- With two pairs in parallel the limit of one native thread applied to OpenBLAS but not to the
  OpenMP pools started by the workers, which each worked at full width: now the limit is applied
  inside every worker.
- Radiomics preparation with normalisation "none" (the default) made two useless float64 copies of
  the volume.
- The radiomics window did not receive exceptions escaping a slot, which ended up only in the log:
  now they appear in the status bar and in the bug report, together with the errors shown.
- The manifests of quantitative projects and evaluations are valid JSON (RFC 8259): non-finite
  numbers (a NaN standard deviation, the +inf PSNR of identical images) came out as `NaN`/`Infinity`
  tokens, refused by strict readers. The quantitative project moves to format 2
  (`{"$float": "Infinity"}`); format 1 remains readable.
- The version numbers of persisted formats are accepted only as exact integers: `true`, `3.0`,
  `"3"` or `3.7` passed the check (`qicom.schema_version`).
- A failed batch re-run left the results of the previous run next to a new, empty receipt and log;
  now every run removes the files written by the previous one (`qicom_batch_outputs.json`) and no
  others.
- A re-saved radiomics project received a new `created_at` every time.
- The trending database checks the version before creating tables and verifies the hash of a
  baseline on loading (additive column `canonical_json`).
- `hash_array` copied the whole array at every block when it was not contiguous (1.3 s instead of
  0.05 s on a transposed 5000×5000 volume); the hash is identical.
- An image smaller than the NSS patch was recorded as a failed calculation (`failed`); now it is an
  invalid input, like any other image too small for a metric's window.
- `qicom.runtime` declares that with two pairs in parallel the last bits of BRISQUE, NIQE,
  canonical NIQE and RIQE, and therefore the `result_hash`, can differ from a run with a single
  pair: the values match at the declared precision.
- Member names that are duplicates, or identical up to case, Unicode normalisation and trailing
  dots or spaces, make an archive be refused: on NTFS/APFS one slice silently overwrote the other.
  On Windows, device names (`CON`, `LPT1`…) and disallowed characters are refused too.
- The SHA-256 verification of a learned checkpoint stayed cached for the whole session: a file
  replaced after the first load was reloaded without a check. Now the hash is verified at every
  load.
- The file filter of the local NSS model dialog showed the translation key `file_filter.images`
  instead of a filter, and selected no file. A test now checks that every literal key passed to the
  translation exists.
- The Log panel re-read the whole file (up to 5 MB) on the interface thread every 2 seconds, even
  when hidden, and a UTF-8 character truncated by an unclean shutdown let an exception escape from
  the timer. Now it reads only the added lines, only while it is visible, and tolerates truncated or
  unreadable lines.
- Esc or closing the learned weights download dialog blocked the application for up to 60 s with a
  stalled connection. Now the dialog closes at once and the transfer stops at the next block,
  without waits on the interface thread.
- Radiomics extraction and cohort aggregation can be cancelled: the progress dialog had no Cancel
  button and a multi-ROI extraction lasting many minutes could only be interrupted by closing
  QICOM. Cancellation takes effect at the next boundary (between ROIs, or between reference and
  comparison image) and leaves the previous results unchanged.
- Memory at Calculate: some metrics reach peaks of 14-40 times an image (Wasserstein-1, NMI, 3D
  SSIM), doubled by two pairs in parallel, and a `MemoryError` cancelled the whole calculation. Now
  a `MemoryError` is the outcome of only the metric that caused it, QICOM warns when the estimated
  peak exceeds the available RAM, and it computes the pairs one at a time when two in parallel
  would not fit (`qicom.memory.estimate_calculation_peak_bytes`).
- The reasons for an anonymisation refusal no longer quote the identifying value left behind
  (previously "first: 'Surname^Name'"): they name only the attributes, because they end up in the
  error log and in the reports to be sent. For the same reason the "several DICOM series found"
  message no longer quotes the free-text description of the series (it stays visible only in the
  series choice).

- The coverage gate now computes statement coverage from line counters instead of labeling
  the combined line/branch percentage as statement coverage. Recorded floors are unchanged.

- Blinded qualitative deliveries now copy common raster images losslessly without text or
  identifying EXIF metadata. JPEG pixels are decoded once and stored in PNG; rendering metadata
  is retained. Burned-in text and arbitrary ICC profile content are outside this protection.
- Hidden-repeat schedules guarantee the requested number of intervening pages or reject an
  impossible design. Tail cases no longer silently shorten the requested spacing.

- **The learned-metric tests find the weights in CI again.** The throwaway user
  profile that every test receives also redirects `TORCH_HOME`, and from that
  point on it hid the checkpoints that the CI job downloads before the suite
  with `packaging/prepare_learned_weights.py`: LPIPS and DISTS refuse to
  compute without verified weights and never download them during an analysis,
  so they failed at every push. On a machine without the `learned` extra the
  same tests are skipped, which is why the failure was invisible locally.
  `tests/conftest.py` now links the already downloaded weights into the
  temporary profile — reads reach the real files, writes stay confined — and
  `TestPreparedLearnedCheckpointsStayReachable` guards the invariant. No change
  to the application's behaviour or to the metric values.

- **The desktop builds for macOS and Windows complete again.** Three
  independent defects, all in the packaging workflow and none in the
  application: `pip-audit` was installed *inside* the environment that
  PyInstaller then froze, so its eleven dependencies (`requests`, `rich`,
  `certifi`, `urllib3`, …) ended up in the bundle and the licence inventory
  check refused it; the PyTorch CPU index, which replaces PyPI instead of
  sitting beside it, resolved `setuptools` to 78.1.0 (PYSEC-2025-49,
  PYSEC-2026-3447), making the audit fail on Windows and Linux; and
  `setuptools`, which PyInstaller really packages because
  `torch.utils.cpp_extension` imports it, was excluded from the inventory as if
  it were a mere installer. The auditor now lives in a separate environment and
  reads the build one with `--path`, `setuptools` is inventoried with its MIT
  licence and those of the packages it vendors, and the name derived from the
  `python3.<minor>/` directory is no longer mistaken for a distribution.

- **The builds with the learned metrics complete again.** Two more
  independent defects, again both in packaging and none in the application.
  `torchvision` loads its compiled operators with `torch.ops.load_library`, on
  a path built from `torchvision.__file__`: an indirection that no static
  analysis can follow, and which the hook shipped with PyInstaller compensates
  for by naming `torchvision._C` — a name upstream no longer uses, replaced by
  `_C_stable` (and by `image_stable` for `torchvision.io`). So no library was
  collected, and in the bundle `import torchvision` died with
  `operator torchvision::nms does not exist`, because `_meta_registrations`
  unconditionally registers a fake kernel for that operator: the frozen smoke
  test failed on every learned build, macOS included.
  `packaging/qicom.spec` now collects the compiled libraries that the
  installed `torchvision` actually ships — by shape and not by name, so that
  the next rename cannot silently repeat the failure — and refuses to build a
  learned bundle if it finds none. The second defect is the `packaging<26` cap
  of the `packaging` extra, which held that distribution at 25.0: `pip check`
  examines the whole environment and not only what QICOM ships, and it broke
  on the `pipx` preinstalled on the Windows runner, which requires `>=26`. The
  cap rises to `<27`.

- **LPIPS finds its own linear weights inside the bundle again.** Once the
  `torchvision` failure was overcome, the step that checks the explicit
  preparation of the checkpoints hit the next defect: `lpips` locates the
  weights it ships in its own package with `inspect.getfile(self.__init__)`,
  that is from the file name of the code object, which for a module served
  from the PYZ archive is the relative path recorded at build time.
  `os.path.abspath` therefore resolved it against the working directory and
  LPIPS died with `FileNotFoundError` on
  `<cwd>/lpips/weights/v0.1/alex.pth`: the weights were collected correctly,
  they were being looked for elsewhere. `lpips` is now collected as source on
  disk, so the import registers it with an absolute path. Since a module
  collected this way does not appear in `a.pure`, the module inventory that
  feeds `attribution.py check-bundle` read back one package fewer and would
  have silently stopped checking its licence: now it also reads the sources
  collected as data. No change to the metric values.

- Adding the green air sample in the Background step no longer jumps the
  window to the Analyze step: creating the circle is the first of that step's
  three actions (place it, detect, correct by hand), and the user was thrown
  out of the step they had only just started.

#### Release readiness

- **Dependencies locked with hashes.** Every bundle is installed from the lock of its own profile
  and platform (`requirements/lock/`, 12 files generated by `packaging/generate_locks.py`), with
  `pip --require-hashes`: two builds of the same tag ship the same files, and a compromised release
  that falls within a version range does not enter the bundle. The IBSI gate also uses the lock of
  the Linux bundle. PyRadiomics, which pip cannot verify by hash, stays pinned to its Git commit.
  `--check` in the static gate keeps the locks consistent with `pyproject.toml`.
- **Releases and SBOM.** A tag creates a draft GitHub Release with the bundles, SHA-256 checksums
  and CycloneDX SBOM of every platform, and the notes of the CHANGELOG section; publishing remains
  the maintainer's decision.
- **Builds and attributions.** NOTICE and the About dialog pointed to `packaging/...`, a path that
  does not exist in a bundle: now they name the files where they actually are. The wheel
  redistributes the RIQE model (CC BY 4.0) together with its attribution
  (`qicom/metrics/models/RIQE-MODEL.txt`). `metric_inventory.csv`, shipped in the bundle, no
  longer refers to `paper/`, which is not distributed. Tag builds do not restore the pip cache and
  keep their artifacts for 90 days instead of 7. A local build warns that it is not verified when
  it uses the reference copy of the licences, and excludes MIRP and ITK, which no bundle profile
  includes.
- **IBSI reference sets in the `full` bundle.** `qicom/radiomics/reference_sets` was in the wheel
  but not in the PyInstaller spec: a frozen application could not run any IBSI verification. A
  test now compares package-data and spec, and the frozen smoke test loads every manifest and the
  RIQE model.
- **"Numerical results" gate that cannot be bypassed.** An entry under a `####` subheading, a list
  with `*`, `+` or numbers, or the title in upper case escaped `check_release_consistency.py`, and a
  stable version could ship with undeclared changes to the numbers.
- **Build environment.** setuptools, which ends up in the bundle, now has a cap (`<85`) and the
  receipts record the versions of PyRadiomics, PyWavelets and torch (field `backend_versions`,
  additive). Releases are now installed from hashed locks (next entry).
- **MIRP's IBSI conformance verified in CI.** The test of the six MIRP manifests (2104 rows) was
  always skipped: no job installed MIRP or prepared the resource cache. Now a dedicated CI job and
  a tag gate (`ibsi-mirp`, `ibsi-mirp-gate`) run them in a separate environment, with their own
  dependency audit. With `QICOM_RUN_IBSI_MIRP=1`, set only in those two jobs, a missing MIRP or
  cache is a failure, not a skip; the main test job, which sets `QICOM_RUN_IBSI_REFERENCE=1` for
  the PyRadiomics gate and does not install MIRP, skips it with the reason (previously it failed).
- **Tests that really verify.** The static stubs of `QMessageBox` now apply only to the module
  indicated, and a static dialog without an answer fails the test instead of blocking the suite;
  `faulthandler_timeout` points to the blocked test. The clean-up tests after a refused archive
  look at the system temporary folder, where extraction really happens. Coverage also measures
  the code run in QThreads: `evaluation_export.py` showed 13% in the full suite and 99% on its own.
- `load_package_metadata_from_zip` reported a file that is not a ZIP with a raw `BadZipFile`,
  which the aggregation dialog did not catch; now it is a readable `ValueError` as in the other
  opening paths.
- **Correct qualitative change tracking.** The asynchronous completion of the
  media restored from an archive no longer immediately makes a just-opened
  evaluation "modified"; renaming a panel, on the other hand, now increments
  the document revision and can no longer be lost on closing.
- **Fail-closed packaging.** The licence comparison ignores standard-library
  modules and non-importable data without hiding third-party extensions; the
  frozen smoke test writes its JSON to a file even with a Windows executable
  without a console; a cancelled IBSI gate does not authorise the package. On
  tags, version, ref name, annotated tag and `HEAD` commit must match. The
  inventory also includes the PyInstaller runtime, and the readable file
  reproduces the installed licence/notice texts instead of only listing their
  paths.
- **AI export limit effective even for a single huge field.** The texts
  controlled by the caller are truncated deterministically and the writer
  never returns a document beyond `MAX_REPORT_BYTES`.
- **Unknown source identity declared as such.** The About panel no longer
  translates `revision=unknown` and `source_dirty=false` into the unproven
  claim "clean".

- **Outdated documentation.** Review of all the project documents against the
  code: `OPERATIONS_SCHEMAS.md` described receipt schema 2 after schema 3 had
  shipped; the citation in the README was stuck at "Version 1.0.0" while
  `about.citation_text()` already produced `1.1.0.dev0`; the Test section of
  the README declared a single Python version after the matrix had been added;
  `requirements/packaging-base.txt` had lost the `packaging` dependency added
  to the corresponding extra. `benchmarks/README.md` ignored the schema,
  `--compare` and the separation of the two export cases, and invoked
  PowerShell in a repository developed on Linux.
  `packaging/LEGGIMI-TESTER.txt` (now `packaging/TESTER-GUIDE.txt`), which
  travels inside the bundle, explained SmartScreen and Gatekeeper but not the
  third thing a tester runs into: LPIPS and DISTS not available on first
  launch. `CITATION.cff` carried the 1.0.0 release date next to the version
  `1.1.0.dev0`, claiming a release that never happened. The historical notes
  in `benchmarks/` are now declared as such.
- **New tests against drift returning**: `tests/test_documentation_currency.py`
  (17), the `requirements/*.txt` ↔ `pyproject.toml` mirroring and the README ↔
  `about.py` consistency, which the README already claimed was checked and was
  not.

- **Metric outcomes lost through the all-pairs cache.** The value and its
  cause travelled on different channels — the value to the caller, the cause in
  a thread-local. In an N-image sweep the cache computes a single-image metric
  once and passes it to every pair: whoever reused a cached `None` had nothing
  to report, and `unavailable`, `invalid` and `failed` all ended up as "not
  applicable". Now the computation and its outcome are a single immutable
  object. **No numerical value changes.**
- **AI export truncated in words only.** Beyond 200 rows the writer dropped
  the *columns* and still emitted every row, declaring the report truncated: a
  10,000-row analysis produced a 10,000-row file that claimed to be short. Now
  the limit is on rows, with deterministic round-robin selection per metric,
  an explicit byte budget, total/emitted/omitted counts, a digest over the
  whole result and an atomic write.
- The list of recent projects is really best effort: a user profile that
  cannot be written costs the shortcut, not the opening of the project.
- `Image.fromarray(..., mode=...)`, deprecated and removed in Pillow 13, dropped
  wherever the array type already determines the mode. Byte-identical output.
- No temporary file left behind by a failed or cancelled ZIP.

### ⚠️ Changes affecting numerical results

- **Series of single-frame Enhanced DICOM files** (for example a multi-frame split into files):
  position, orientation and rescale are in the functional groups, which reading a series from a
  folder ignored. The slices were stacked by Instance Number, without positions, and the data
  range was computed without the per-frame rescale. Now reading the series also reads the
  functional groups (`qicom/io/dicom_io.py`) and the folder loads like the multi-frame it comes
  from. **For these series the slice order, the data range (and with it PSNR and SSIM) and the
  geometry can change.** Series with position and orientation in the header do not change.
- **Radiomics on multi-frame DICOM with frames out of order.** The pixels were sorted along the
  normal, but origin and spacing between slices were read in file order: frames at z = 2, 0, 3,
  1 mm gave an origin of 2 mm and a spacing of 0.333 mm instead of 0 and 1 mm. Now the loader
  delivers the positions in the same order as the pixels and the geometry uses them
  (`qicom/io/dicom_io.py`, `qicom/radiomics/io.py`). **All geometry-dependent features (shape,
  texture after resampling) change for multi-frame volumes stored out of order.** The NIfTI copy
  of a project saved earlier carries the wrong geometry and cannot be corrected on its own:
  re-import the DICOM source. A volume with missing or irregular slices (a step deviating by more
  than 1% from the mean spacing) is now refused instead of receiving a mean spacing that
  describes no slice, and so is a stack with its slices shifted within their own plane (tilted
  gantry, sheared stack) by more than 0.01 mm: projecting the positions onto the normal hid the
  shift. A multi-frame with frames in different orientations is refused too: it is not a volume.
- **RIQE in CLI and batch on non-axial stacks**: the value no longer appears (outcome
  `invalid/degenerate-input`), as already in the GUI.
- **Same configuration hash in GUI, CLI and batch.** The same calculation had a different hash in
  each: the GUI recorded the NSS reference model and the declared units, batch recorded the units
  only if HU, batch and CLI the per-role ranges even without metrics that read them. Now a single
  builder (`qicom/metrics/run_configuration.py`) adds each key only for the metrics that use it;
  the NSS model, read from an on-disk cache, is read only if BRISQUE or NIQE are requested, so an
  inaccessible cache no longer blocks an MSE-only calculation. The computed values do not change;
  **the configuration hash of CLI and batch receipts changes** (and of GUI receipts with an
  integer `data_range`, now recorded as a decimal number).
- The `complete` quantitative profile moves to version 1.2: it includes the new `riqe` metric (it
  had moved to 1.1 to include `brisque_canonical` and `niqe_canonical`). No other profile changes.
- **HaarPSI on a flat pair.** In the runner a constant pair is normalised to zeros and piq
  returned +inf, recorded as a valid HaarPSI. Now it is a degenerate input
  (`invalid/degenerate-input`) and the value does not appear.
- **RIQE only on axial slices and on NIfTI files that really are CT.** Every NIfTI was considered
  to be in HU: an MR volume was shown in HU and passed to RIQE. In addition, a NIfTI in RAS order
  was sliced sagittally along axis 0 and evaluated as if it were axial. Now a NIfTI counts as CT
  only if its values reach air (≤ −500), and RIQE refuses the slices that the file's geometry
  (NIfTI affine or DICOM ImageOrientationPatient) indicates as not axial. The new
  `slice_orientations` field enters the configuration, and therefore the hash, of analyses with
  RIQE only.
- **`evaluate_acceptance` judges the pair, not the first ROI.** The results table lists the ROIs
  before the pair, and the rule read the first row of the metric_id: it judged ROI 1 (SSIM 0.811)
  instead of the image (0.982). With several ROIs and no whole-image result the rule stays
  unevaluated and says so.
- **CV and SNR on signed intensities.** With signed values (CT in HU) an ROI in fat or air has a
  negative mean, and CV (`std/mean`) and SNR (`mean/std`) became negative: outside the declared
  domain `[0, +inf)`, and a negative CV came out "better" than any positive one. Now both use the
  absolute value of the mean. **The values of `cv` and `snr` change only for ROIs with a negative
  mean.** The receipts record the new formula versions (`roi_cv_snr`,
  `masked_spatial_support`, `stack_summary`, `canonical_no_reference_range`).

- **Slice order in bitmap stacks.** The files were sorted as strings, so `slice10` and `slice11`
  ended up between `slice1` and `slice2`: the volume came out shuffled, and two series with
  different naming conventions were paired slice by slice in a different order. Now the order is
  natural (numbers in names count as numbers). **The per-slice and volumetric results of bitmap
  stacks numbered without leading zeros and with at least 10 slices change.**

- **Default `data_range` of CLI and batch.** GUI, CLI and batch had three copies of the rule, and
  the CLI and batch one lacked the GUI's minimum of 1: two float images in [0, 0.5] gave PSNRs
  about 6 dB apart depending on the entry point. Now they all use `combined_default_data_range`.
  **The `data_range`-dependent values (PSNR, SSIM, ...) of CLI and batch change only when the joint
  span of the two images is below 1 and no `data_range` was given.** Batch receipts now also
  record the quantitative profile, like the GUI's; the CLI help describes the actual rule.

- **SSIM, EPI and HFEN with "exclude the air".** Without a mask the three metrics are native 3D on
  a stack (7×7×7 window, 3D filters); with the mask they silently became the mean of per-slice 2D
  values, a different quantity under the same ID (with a mask that excluded nothing, HFEN
  doubled). Now with the mask the same 3D map is computed and restricted to the tissue voxels
  whose whole window falls inside the mask: a mask covering everything returns exactly the value
  without mask. As without a mask, the result is one value per volume, without a per-slice
  distribution, and the 3D SSIM of a stack with fewer than 7 slices is invalid. **The values of
  `ssim`, `epi` and `hfen` of every analysis with the air excluded change.** Analyses without
  exclusion and ROIs do not change.

- **Canonical BRISQUE and NIQE normalise each image with its own range.** GUI, CLI and batch derive
  the default `data_range` from both images, and the normalisation of the canonical scores used it
  for both: a brighter partner changed the reference's score, contrary to what the methodology
  declares. Now each role uses the range of its own image (the one reported by its loader),
  recorded in the configuration under `no_reference_data_range` and in the receipt's
  normalisation. **The values of `brisque_canonical` and `niqe_canonical` change for every pair in
  which the shared range was wider than the image's.** The historical NSS distances (`brisque`,
  `niqe`), `blur` and RIQE do not change.

- **Frame order in Enhanced multi-frame DICOM.** Classic series are sorted by position along the
  normal to the plane, while the frames of an Enhanced object stayed in file (acquisition) order.
  Comparing a classic series with an Enhanced one saved in the opposite direction, slice i was
  compared with slice N-1-i without any warning. Now the frames are sorted by the positions of the
  functional groups (when they are all distinct: multi-echo or multi-phase frames that repeat a
  position stay in file order) and the overlay reports the per-frame positions, so the spacing
  consistency check also applies to Enhanced objects. **The per-slice and volumetric results of
  every Enhanced object whose frames were stored in non-increasing order change.**

- **Blinded qualitative aggregation with several deliveries.** Every blinded delivery randomises
  its own presentation (the seed derives from its `delivery_id`), but aggregation applied a single
  de-blinding map to all the answers: the scores of every reader but one were attributed to the
  wrong images, and kappa, ICC, preferences and AUC were altered as a result. Now each map applies
  only to the answers of its own delivery (`apply_deblinding_maps`); the answers of a delivery
  without a map, or of several blinded deliveries without maps, are refused. The aggregation dialog
  accepts one map per delivery and the `aggregate-qualitative` batch the `deblinding_maps` field.
  **The results of every multi-delivery aggregation with randomised panels, pages or questions
  change: it must be repeated.** Aggregations of a single delivery, or of non-blinded packages, do
  not change.

- NMI uses float64 histogram counts instead of float32 histogram densities, removing bin-width
  rounding artifacts. Its receipt identifies `nmi: count-histogram-v2`; histogram bins and the
  entropy-ratio definition are unchanged. Recompute NMI when comparing with earlier reports.
- Masked SSIM and HFEN now erode ROI/tissue masks by the square filter footprint, including
  corner pixels. Diamond erosion allowed pixels outside irregular masks to affect the score.
  Receipts identify `masked_spatial_support: kernel-footprint-v2`; masked results may change
  or become unavailable when the complete kernel no longer fits the selected region.
- Radiomics CCC accuracy is computed directly as `2 * sd_x * sd_y / denominator`, so it remains
  defined for zero correlation and positive variance. CCC itself is unchanged; identical
  constant pairs retain an undefined accuracy term.
- Gwet AC2 v2 corrects weighted chance agreement and uses equally weighted nonempty-unit
  category proportions, including singleton units. Exported results include `ac2_method`.
- Deblinding maps questions and panel roles simultaneously from the original response labels,
  preventing swaps from collapsing distinct conditions and changing their aggregates.
- Qualitative descriptive response rates explicitly identify their denominator. With an
  assignment roster, completely absent responses are counted as missing; without one, the rate
  refers to received records and unrecorded missing assignments remain unknown.
- Task-based profile v2 corrects falling-edge rise distance and implements distinct PW, NPW,
  and NPWE expressions; NPWE requires an explicit eye filter. Detectability serialization v2
  preserves legacy v1 numerical values and identifies their effective PW formula. Nonzero edge
  angles are rejected because no slanted-edge correction is implemented.

- The primary qualitative analysis moves from the legacy mean/SD and kappa aggregates to ordinal
  descriptives and Krippendorff's alpha; comparisons use evaluator×case cells, cluster bootstrap
  and Holm. The legacy sheets remain available, but the new results are not numerically comparable
  with reports that treated slices as independent observations.
- The default of the quantitative GUI moves from all metrics to the versioned Quick core profile;
  the pre-existing formulas do not change, but a new session computes a more compact set if the
  user does not select another profile.
- Stack aggregates add median, IQR, MAD, P5/P95 and Tukey outliers; `value`, `std` and the legacy
  CIs keep the previous formula.
- The new error metrics share a single residual defined as `comparison - reference`;
  `residual_std` uses the population standard deviation after removing the bias.
- The QICOM Index is now tied to the profile/version and is marked partial when a mandatory
  component is missing; indices from different profiles/versions are refused as not comparable.
- The Quick core quantitative profile moves from 1.0 to 1.1 and also includes MSE; for the same
  images the existing formulas do not change, but a new Core analysis produces one more numeric
  row.
- The default radiomics profile moves to `qicom-ibsi-core@2.0`: the primary percentage delta is now
  the symmetric one. `delta_percent` and `delta_classic_percent` keep the classic formula, so
  previous exports remain comparable using those columns.
- The new normalisations and resegmentation change intensity/mask, and therefore all features,
  only when selected explicitly; they stay disabled in the default. The `resegmentation-lower` bin
  origin changes the discretisation only if explicitly requested.
- The exported unit of `SurfaceVolumeRatio` is corrected from `mm²` to `mm⁻¹`; the numerical value
  of the feature does not change.
- The IBSI comparison rule moves from `official-rounded-difference-3-significant-digits` to
  `official-rounded-difference-3-significant-digits+absolute-near-zero-v2`
  (`reference_values.comparison_rule_version: 2` in the reference set manifest). A reference value
  equal to zero, or within `relative_zero_threshold`, has no three-significant-digit scale to
  truncate on: the previous rule fell back to `digits = 0` and truncated to zero *any* difference
  below 1.0, so `reference=0`, `calculated=0.4`, `tolerance=0` was reported as an official PASS.
  Now near zero the untruncated absolute difference is compared, so the same case is a FAIL. The
  relative error stays NA, with the reason stated explicitly in the receipt row. The official IBSI 1
  reference set (104 mandatory features) contains no zero reference values: no published verdict
  changes, but reference sets with zero references can no longer produce a silent PASS.

- Ordinal Krippendorff's alpha moves to version 2 of the formula (exported label
  `Krippendorff alpha (ordinal) v2`). Version 1 used a squared distance between uniform ranks,
  which ignores the marginal frequencies; the correct ordinal definition measures the mass of the
  categories between the two, with half the frequency of each endpoint:
  `delta(c,k) = (sum_{g=c..k} n_g - (n_c + n_k)/2)^2`. In addition, the observed disagreement is
  now weighted per unit with `1/(m_u - 1)` before averaging over the pairable values: the
  aggregate count of pairs overweighted the units with more evaluators, which is exactly the case
  created by missing answers. On unbalanced data the difference is large (asymmetric Likert
  example: 0.5272 → 0.2535; canonical matrix with missing values: 0.8515 → 0.8049). The v1 values
  are not comparable. Gwet's AC2 keeps the quadratic weights declared by its name.

- Acceptance profiles check the unit declared by the result against the rule's. Previously the
  rule's unit was written into the outcome without any check and the comparison used the bare
  number: a result in `cm` could pass a threshold written in `mm`. Now the units must match
  literally, without any implicit conversion; if they are not comparable the mandatory rule
  becomes `INCOMPLETE` and the optional one `NOT-COMPARABLE`, both with a technical reason. The
  outcome table adds the `result_unit` column next to `unit`, so the discrepancy is visible in the
  GUI and in the exports. A result that does not declare a unit is no longer compared silently.

- The percentages of multiple-choice questions are now shares of respondents and not of
  selections. Since a respondent can tick several categories, the total of the selections exceeds
  the number of respondents, and the old normalisation made each category look rarer the more
  ticking there was: two respondents, one with A+B and one with A, gave A=67% and B=33% instead of
  A=100% and B=50%. `category_distribution` now also exports `denominator` and
  `denominator_definition`. Skipped, not assessable and missing answers stay distinct states and
  are excluded from the denominator.
- New `likert_distribution`: frequencies and percentages for each category of rating questions,
  with a declared denominator. It is included in the qualitative Excel export (sheet
  "Likert Distributions") and in the qualitative batch (`qualitative_likert_distribution.csv`).

## [1.0.0] — 2026-08-19

First release published under semantic versioning. QICOM provides three
independent workflows over DICOM, NIfTI and common 2D image formats:

- **Quantitative analysis** — full-reference metrics (MSE, MAE, RMSE, NMSE,
  PSNR, SSIM, MS-SSIM, FSIM, GMSD, HaarPSI, VIF, LPIPS, DISTS), no-reference
  indices (BRISQUE, NIQE, blur, edge/detail), ROI statistics (SNR, CNR, CV,
  mean/min/max), statistical measures (PCC, NMI), difference heatmaps with
  standard medical LUTs, line profiles, background-air exclusion, per-metric
  effort levels, and Excel export.
- **Qualitative analysis** — creation, blinded administration and aggregation
  of structured image evaluations, with inter-rater spread, reliability and
  AUC comparisons.
- **Radiomics** — paired 2D/3D feature extraction on reproducible,
  hash-identified profiles, semi-automatic segmentation, delta-radiomics and
  guarded ICC/CCC repeatability statistics.

All processing runs locally; see `SECURITY.md` for the security and privacy
model.
