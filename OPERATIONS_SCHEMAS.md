# Operational formats and schemas

All formats are local and versioned. Unknown future major schema values fail closed instead of
being guessed. Older supported values are migrated to explicit empty/default fields; an absent
statistical identity is “unknown”, never one independent observation per slice or ROI.

A version is an exact integer (`qicom.schema_version`): `true`, `3.0`, `"3"` or `3.7` are not
version 3 and are refused, where they used to pass a membership test or `int()`.

### Fields this version does not know

QICOM does not preserve what it does not understand. A field added by a newer QICOM, or by hand,
is ignored when preferences, receipts, answers files and project manifests are read, and is
therefore absent from the file the next time this version saves it. Two formats refuse unknown
fields outright instead, because silently dropping one would change a result:
`QualitativeDesign` and `RadiomicsConfig`. A file that must survive a round trip through an
older QICOM should not rely on fields that version does not know.

## Shared reproducibility receipt — schema 4

The JSON-compatible receipt in `qicom.provenance` contains:

- `qicom_version`, `calculation_schema_version`, `receipt_schema_version`;
- `build_revision` and `source_dirty` — the exact commit the numbers came from, and whether
  the checkout it was built from had uncommitted changes (see `qicom/build_identity.py` and
  [VERSIONING.md](VERSIONING.md)). A version names a release; between two builds carrying the
  same one there can be any number of commits, so the version alone does not identify the
  software that produced a result;
- `formula_versions`, platform and backend versions (`backend_versions` names numpy, scipy,
  pandas, scikit-image, nibabel, SimpleITK and, read from the installed metadata without
  importing them, PyRadiomics `pyradiomics`, PyWavelets `pywt` and `torch`; a backend that is not
  installed is absent);
- `input_hashes`, `mask_hashes`, full configuration and `configuration_hash`;
- requested metrics and their outcome registry;
- preflight warnings and diagnostics;
- statistical unit (`subject_id`, `case_id`, `acquisition_id`, `timepoint`, `lesion_id`,
  `roi_id`, `reader_id`);
- timestamp and optional result hash.

Receipt schemas 1 and 2 remain readable and are normalized to schema 3 in memory. They predate
`build_revision`, so they restore it as `"unknown"` rather than being attributed to whichever
build happens to be reading them. Schema 4 changes no field: it hashes with the strict
canonical JSON described below. A schema-3 receipt stays schema 3 when it is read back, and
its `receipt_hash` keeps the legacy rules it was computed with, so it never changes.

### Hash scope and numerical equivalence

The schema 4 hash semantics, preserved when testing resource profiles:

| Field | Exact content covered |
| --- | --- |
| `input_hashes`, `mask_hashes` | Each input or mask; array hashes include dtype, shape and C-order bytes. An input that did not exist when the receipt was built is recorded as `missing:` followed by a hash of its path, never as a content hash |
| `configuration_hash` | The supplied configuration's canonical JSON, independently of results, except its `locations` entry (file and folder paths of a batch run, recorded but not hashed) |
| `result_hash` | The result table's ordered columns and ordered rows, including numeric values and descriptor metadata; `null` when no results were supplied |
| `receipt_hash` | The entire receipt's canonical JSON, including `result_hash`, timestamp, platform, backend versions and build identity |

The canonical JSON is strict from schema 4 (`qicom.provenance.canonical_json`). The legacy form
was not injective: a float NaN and the string `"NaN"` serialized alike (so did the infinities and
`"+Infinity"`/`"-Infinity"`), mapping keys `1` and `"1"` collapsed into one, and a NumPy boolean
became the string `"True"`. The strict form writes a string equal to one of those tokens, or
starting with a NUL character, with a NUL in front; refuses a mapping whose keys collide once
converted to text; and writes a NumPy boolean as a JSON boolean. For every value without such a
string, key collision or NumPy boolean the two forms are byte-identical, so a schema-4 hash
equals the schema-3 hash of the same content except in those cases.

The legacy form is kept, deliberately, where a hash was already persisted with it: schema-3
receipts (above), NSS model identities (verified when a saved model is loaded) and acceptance
profile hashes. Radiomics configuration hashes, trend baselines, qualitative designs and the
IBSI manifest use their own canonical JSON with different parameters
(`qicom/radiomics/config.py`, `qicom/trending.py`, `qicom/session/qualitative_design.py`,
`qicom/radiomics/ibsi_validation.py`): they are persisted identities that projects and cohorts
compare, so they are not migrated, and their hashes are not comparable with the receipt's.

`receipt_hash` is a computed property, not an additional serialized field. These digests establish exact content integrity, not equivalence within a
numerical tolerance. A last-bit floating-point difference can change `result_hash` and thus
`receipt_hash` even when the scientific results satisfy the existing golden tolerances.
Separate executions can already have different receipt hashes because of their timestamps
or environments, even if the output values are identical.

Resource-profile equivalence therefore compares numerical results using the existing
tolerances, while requiring exact input/mask identities, scientific configuration, requested
metrics, descriptor identifiers, output structure/order, outcome records and schema/formula
versions. Each execution keeps its own exact result hash and receipt. Thread caps and cache
budgets remain execution controls outside the scientific configuration; they do not silently
select metrics or transform inputs. Automatic resource profiles add no rounding or
normalization of stored results for hashing and do not change any receipt schema.

Canonical BRISQUE/NIQE add IDs `brisque_canonical` and `niqe_canonical` without
remapping the legacy NSS IDs `brisque`/`niqe`. Existing result columns, project
version and receipt schemas remain unchanged. Their model identities and per-image
normalization are recorded in the existing configuration dictionary under
`canonical_no_reference`, only when requested; each role's own data range, used for
that normalization, is recorded under `no_reference_data_range`. Existing outcome `scope` values also
identify `reference` and `comparison` independently. Historical receipts are not
rewritten. See `NO_REFERENCE_METHODOLOGY.md` for the model/download contract.

RIQE adds the ID `riqe` in the same way, with no schema change. When it is requested,
the configuration may carry `intensity_units` (`{"reference": "HU" | "unspecified",
"comparison": ...}`), and `canonical_no_reference.models.riqe` records the bundled
model's identity and SHA-256, the HU mapping, masks and patch parameters and each role's
declared unit. The configuration also carries `slice_orientations` (`{"reference": "axial" |
"non-axial" | "unknown", "comparison": ...}`, from the NIfTI affine or the DICOM
ImageOrientationPatient): RIQE refuses a role whose slices along axis 0 are not axial.
The GUI, the CLI and batch jobs build the configuration through one function
(`qicom/metrics/run_configuration.py`), so one calculation has one configuration hash:
`data_range` always; the quantitative profile unless it is `custom@1.0`; `reference_model` with
BRISQUE or NIQE; `intensity_units` and `slice_orientations` with RIQE; the per-role data ranges
with the canonical no-reference scores.
`canonical_no_reference` holds only `models` when RIQE is the sole
canonical score requested; with BRISQUE or NIQE requested, the existing `normalization`
and `spatial_support` entries are unchanged. The profile `complete` moves to version
1.2 because its metric set now includes `riqe`; saved selections keep their sorted IDs.

## Desktop preferences — version 2

`qicom/gui/preferences.py` stores per-account settings in `preferences.json`, separately from
projects and scientific receipts. Three resource preferences distinguish automatic from manual:

| Field | Automatic | Explicit manual value |
| --- | --- | --- |
| `radiomics_cache_mib` | `null` | Integer 0–4096 MiB; zero disables the prepared-image cache |
| `keep_learned_models` | `null` | `true` to retain, `false` to release after analysis |
| `pair_workers` | `null` | 1 or 2 simultaneous eligible quantitative pairs |

An absent version is read as version 1. Migration preserves every valid, present v1 cache and
model-retention value as manual, including the former defaults 512 and `true`: a v1 file cannot
tell whether these were chosen by its user. Absent or invalid resource values become automatic;
the new `pair_workers` field starts automatic. Saving writes version 2; unknown or malformed
versions are logged and fall back to default preferences. Reset restores all three to `null`.
Round-trip tests cover old files, explicit zero/false, and automatic values across hardware changes.

Automatic values are resolved from `qicom.runtime` without writing detected values to disk:

| Profile | Available RAM / logical CPUs | Cache maximum | Retain learned models | Pair workers |
| --- | --- | ---: | --- | ---: |
| `constrained` | RAM <4 GiB **or** CPUs ≤2, both known | 128 MiB | No | 1 |
| `standard` | Other detected hardware, or either detection unknown | 512 MiB | Yes | 2 |
| `workstation` | RAM ≥32 GiB **and** CPUs ≥8 | 1024 MiB | Yes | 2 |

These conservative thresholds leave room for the foreground application; they are not estimates
of peak memory or protection against OOM. CPU detection counts the logical CPUs the process may
use (its affinity, or a container's cpuset). Available RAM is `MemAvailable` on Linux (bounded by
a cgroup v2 limit in a container; `MemFree` used to leave out the reclaimable page cache), free +
inactive + speculative pages from `vm_stat` on macOS (unknown before) and `GlobalMemoryStatusEx`
on Windows. The independent cache ceiling of **one eighth of available RAM** is promoted unchanged
from radiomics. That ceiling still limits the effective cache even when a
manual maximum is larger, without overwriting the saved preference. If RAM is unknown the
configured maximum applies.

The settings dialog displays “Automatic” with the currently detected default or an explicit
value. Cache and pair budgets are resolved once when starting work; model retention is applied
on preference activation, reset or a change to that control. Core radiomics sessions also resolve
an automatic cache when their explicit byte budget is absent, including CLI/batch callers.
The existing two-pair ceiling and sequential path for learned metrics remain. Parallel classic
pairs reuse the existing `threadpoolctl` scope. No new global numerical thread limiter is added.
Metrics, slices, ROIs, dtypes and qualitative image presentation are independent of these profiles.

## Quantitative project ZIP — version 2

Manifest: `qicom_project.json`. Besides panels, ROIs, annotations, lines and the result tables,
the optional `calculation_records` maps each result table's label to its reproducibility
receipt and metric outcomes, which travel as DataFrame attributes and would otherwise be lost.
Projects saved without it remain readable and simply have no receipt to restore; readers that
predate it ignore it.

Version 2 is strict JSON (RFC 8259): a non-finite number — a NaN standard deviation, the +inf
PSNR of identical images — is written as `{"$float": "NaN" | "Infinity" | "-Infinity"}` instead
of a bare `NaN`/`Infinity` token, which strict readers reject. Version 1 manifests, with bare
tokens, still load and are saved again as version 2. A QICOM that only understands version 1
refuses a version 2 manifest rather than reading those cells as objects. The qualitative
evaluation manifest is strict JSON too; a non-finite timing is written as unknown (`null`).

## Radiomics project ZIP — version 2

Manifest: `qicom_radiomics_project.json`.

- `images/`: de-identified/derived reference and comparison NIfTI;
- `masks/`: binary NIfTI masks in each image grid;
- `tables/`: features, delta, descriptive pairwise agreement, correlation, provenance, mask QC,
  feature QC, robustness, preprocessing sweep, validation status and metric outcomes;
- manifest: configuration, study identity, ROI records, notes, stale-result state and common
  reproducibility receipt.

Version 1 projects remain readable. Missing v2 identity/tables/receipt become explicit empty
values. Saving writes version 2. Cohort loading opens only the bounded manifest and feature/delta
members, validates archive paths and sizes, and does not decode the images.

Every table is also written as typed JSON (`tables/<name>.json`, listed in the optional manifest
key `typed_tables`): `{"qicom_typed_table_version": 1` (the integer: `true`, `1.0` or `"1"` are
refused)`, "columns": [...], "dtypes": {column:
dtype}, "rows": [[...], ...]}`, with dtypes limited to NumPy numbers and booleans, `str` and
`object`, and cells to text, numbers, booleans, `null` and the `NaN`/`Infinity` tokens. QICOM
reads the typed copy: CSV keeps no types, and a ROI named `"001"` came back as `1`, the text
`"NA"` as missing, so a reopened table no longer matched its receipt's `result_hash`. The CSV
stays for other tools. A table the format cannot carry exactly is written as CSV only. A project
without `typed_tables` (or a table missing from it) is read from its CSV, with the known text
columns (identifiers, names, labels, hashes) kept as text and only an empty field read as
missing. Readers that predate the key ignore it; no version change.

Cohort aggregation refuses a project whose manifest has `results_stale: true`, and one whose
receipt records a `result_hash` the feature table no longer has (strict or, for receipts up to
schema 3, legacy canonical hash).

## Qualitative package and answers

- evaluation package: format version 4;
- answers: format version 3.

The readers for v1/v2/v3 packages and prior answer formats remain covered by compatibility tests.

`design.answer_scope` says what one answer covers on a multi-slice image: `"series"` (one answer
per page, comparison and question, recorded at `slice_index` 0, so scrolling the series keeps it)
or `"slice"` (one answer per slice of the reference series, at that slice's index). A new design
is `"series"`. A stored design without the field (every package written before it existed, and a
`null` design) is migrated explicitly to `"slice"`, which is how its answers were given. The
manifest version does not move: `QualitativeDesign` refuses fields it does not know, so a QICOM
that predates `answer_scope` refuses such a package instead of reading series answers as
first-slice answers.

The current delivery stores the deterministic presentation plan, actual shown order, aliases,
repeat metadata, answer status and optional disclosed timing. The de-blinding map is a separate
author artifact and is absent from the evaluator package.

A blinded delivery's public plan carries `seed: 0` (withheld), page IDs `1..N` and question IDs
`1..M` in presentation order, and a public `design` with only general instructions, neutral
labels, advanced statistics, timing and answer scope; `randomize_questions` is `false`.

De-blinding map — version 2: as version 1, plus `master_seed`, `delivery_secret` (the secret
mixed into this delivery's seed, an HMAC of test and delivery ID under the author's private key
`qualitative_blinding_key.json` in the user data directory) and, per page, `analysis_page_id`
(the author's page ID, or the author plan's ID of a hidden repeat). Presented IDs are opaque, so
aggregation rewrites `page_id` to `analysis_page_id` and takes `presentation_seed` from the map.
A version 2 map must be complete and one-to-one: a presented page, question or panel it does not
map, an ID listed twice or two pages with one analysis ID refuse the map, since an opaque ID left
unmapped names some other source. Version 1 maps, whose presented IDs were the author's, stay
readable and keep what they leave out as it is; a QICOM that understands only version 1 refuses a
version 2 map rather than leaving opaque IDs in the results. When a delivery's ZIP cannot be
committed, the map that was at the destination is put back (or the new one removed); two exports
to one destination commit their pairs in turn under an exclusive lock, and the map records
`delivery_manifest_sha256`, the SHA-256 of the evaluator manifest it was written with. Answers
from another delivery are checked against that delivery as its map describes it, built on the
author's package or on the loaded package's own map.

## Files protected by an access code — version 1

`qicom/session/protected_files.py`. An author may protect the copy of an evaluation sent to
evaluators, a blinded delivery and its de-blinding map with an access code of at least 10
characters; the answers an evaluator saves for such a package are protected with the same code.
The author's own working copy is never protected; a protected package opened for editing is saved
protected again with the same key.

The code is turned into a 256-bit key with scrypt (as for evaluator accounts, parameters stored
with the file). Every protected file has a header: `qicom_protected_version`, `kind`
(`evaluation-package`, `answers` or `deblinding-map`), `kdf`, and `check`, an empty plaintext
sealed with context `check/<kind>`, which tells a wrong code apart from a damaged file.

- **Package**: still a `.zip`, holding exactly `qicom_protected.json` (the header plus
  `chunk_bytes` = 1 MiB and a 7-byte `nonce_prefix`) and `payload.bin`, stored uncompressed: the
  ordinary package zip encrypted in 1 MiB chunks, each AES-256-GCM with nonce = prefix ‖ 32-bit
  chunk index ‖ last-chunk flag, and associated data the SHA-256 of the canonical header. A
  truncated, reordered or extended payload fails to decrypt. The payload is bounded by the
  evaluation package limits and free disk space is checked before decrypting; the decrypted
  package then goes through every check an unprotected one does.
- **Answers and de-blinding maps**: JSON, the header plus `envelope`, the original JSON sealed
  with context `<kind>/content`. Readers accept the plain and the protected form alike.
- **Headless aggregation** reads the code from the text file named by `access_code_file`, never
  from the configuration itself.

A version-1 reader refuses a higher version. Older QICOM versions cannot open a protected file;
unprotected files are unchanged.

## Evaluator accounts — version 1

`qicom/session/evaluator_accounts.py` keeps `evaluators/accounts.json` in the per-account data
directory: one record per evaluator who logged in on this computer to take qualitative
evaluations. A record holds `account_id` (32 hex digits, also the name of the evaluator's folder),
`username` (unique ignoring case), `created_at`, `kdf` and `key`. `kdf` is scrypt with its `n`,
`r`, `p` and a 16-byte `salt`; `key` is the evaluator's random 256-bit key, sealed (see below)
under the key scrypt derives from the password, with context `account-key/<account_id>`. No
password and no hash of one is stored: a password is right when it opens the key. Changing it
re-seals the key and touches nothing else.

`n` is read from each record, so a later cost change leaves existing accounts readable; it is
bounded (`2 ≤ n ≤ 2^20`, power of two; `r ≤ 16`; `p ≤ 4`) so an edited file cannot make a login
allocate gigabytes. A damaged file, or one with a higher version, is refused and never
overwritten — every account's key is in it.

## Encrypted file envelope — version 1

`qicom/session/encrypted_files.py`. A JSON object: `qicom_encrypted_version`, `algorithm`
(`AES-256-GCM`), base64 `nonce` (12 random bytes per write) and base64 `ciphertext` (with its
tag). The associated data is `qicom/<version>/<context>`, where the context names the owner and
the file, so a file moved to another account or swapped for another file of the same account
fails like a damaged one. Files are at most 64 MiB, written atomically and private to the OS
account.

## Evaluator drafts — version 1

`qicom/session/evaluator_drafts.py`. One encrypted file per evaluation in
`evaluators/<account_id>/`, named by the first 32 hex digits of SHA-256 over the test ID and the
delivery ID plus `.qdraft`, so the folder does not name the studies. Context:
`draft/<account_id>/<file name>`. The payload holds `qicom_draft_version`, `summary`
(`package_path`, `name`, `answered`, `page_position`, `page_count`, `page_id` — the page to reopen
on, by id since presentation order may differ from package order — `saved_at` and `completed_at`,
null until the report is saved) and `answers`, the answers-file content (version 3) unchanged.
It is rewritten at every autosave. A draft the account's key cannot open is listed as unreadable
and can only be removed; one met while resuming is set aside, never overwritten.

Before accounts, unfinished answers were kept in clear in `qualitative_answers/`, keyed by this
computer's identity. Those files remain readable: a logged-in evaluator who opens the same package
is asked whether one is theirs, and a confirmed one is moved into their account, encrypted, before
the clear copy is deleted. A plaintext `pending_evaluations.json` written by an unreleased
development build is deleted at the next login.

## Tidy export — version 1

CSV-long and JSON use the same columns:

`schema_version, module, analysis_id, subject_id, case_id, acquisition_id, timepoint_id,
lesion_id, reader_id, image_role, measure_key, value, unit, status, provenance_json`.

The JSON envelope contains `qicom_tidy_schema_version`, creation time, metadata, rows and a
canonical SHA-256 of those rows. Loading verifies the hash. Non-finite numeric values serialize as
JSON null. The CSV is the flat interchange form; the JSON is the integrity-checked round-trip form.
Text cells of the CSV that start with `=`, `+`, `-` or `@` carry a leading apostrophe so that a
spreadsheet cannot execute them; numeric cells and the JSON are unchanged.

## Trending SQLite — version 1

`TrendStore` owns a local SQLite database with a version table, analysis runs and versioned
baselines. A run stores module, metric, value/unit, protocol, profile/config hashes, time and the
original project path. Baselines are immutable versions identified by their own hash. The
schema version is checked before any table is created, and a baseline stores the exact JSON its
hash covers (`canonical_json`, an additive column): loading one whose statistics no longer match
that hash is refused. Baselines recorded before the column existed load unverified.

Trend queries report absolute/percent change, statistical Shewhart limits, optional EWMA and
separately labelled user acceptance limits. Acceptance limits are never inferred from the
statistical limits. The stored project path supports drill-down to the original local project.

## Utility Tools outputs

Every Utility Tools output that is more than one file appears complete or not at all: it is built
in a hidden staging folder next to the destination and renamed into place
(`qicom.io.output_folders`). The destination must be new or empty, a real folder and not the
folder of the sources. NIfTI written by a tool carries only the voxel-to-patient mapping
(`qicom.io.geometry`): for a DICOM source the data axes are column, row, slice, as dcm2niix and
SimpleITK write them; a NIfTI source keeps its own layout and affine; an image without geometry
gets unit spacing and qform/sform code 0. No header field of the source is copied.

Two tools name their output folder themselves, in the place the user picked: the converter
creates `<source>_<format>` (`_nifti`, `_png8`, `_png16`, `_tiff`), holding `<source>.nii.gz` or
`<source>_0001.png` onwards; the anonymizer creates `<folder>_anonymized` beside the original by
default, with the original subfolders and file names, or with generic names as an option:
`anonymized/folder_001/image_0001.dcm`, folders and images numbered in listing order
(`qicom.io.folder_deidentification.target_paths`). Either name gets `_2`, `_3`, … when it is
taken (`qicom.io.output_folders.new_folder_in`). Neither writes a mapping or a log.

### Label map table — version 1

A segmentation is `<name>.nii.gz` (uint8, 0 = unassigned) and `<name>.nii.gz.json`
(`LABEL_MAP_FORMAT_VERSION`, `qicom.segmentation.label_files`):

| Field | Content |
| --- | --- |
| `format`, `format_version` | `"qicom-label-map"`, exactly `1` |
| `labels` | `value` (1–255, unique), `name` (≤64 characters), `color` (`#rrggbb`), `preset` |
| `shape`, `layout`, `geometry_known`, `source_format` | QICOM-order shape and how the NIfTI was laid out |
| `nifti_sha256` | the label map file as written |
| `steps` | each undoable step still in the history: kind, label, changed voxels, parameters |
| `created_by` | the QICOM version |

Reading refuses a sidecar over 1 MiB, another `format`, a version that is not exactly 1, a value
outside 1–255 or defined twice, an empty or overlong name, a colour of another form, and a map
whose values are not all defined or whose shape is not the open image's. A label map without a
sidecar opens with its values named `Label 1`, `Label 2`, … No source file name, path or header
value is written.

### DICOM edit log — version 1

`qicom-dicom-edits.json`, next to the edited copies (`EDIT_LOG_FORMAT_VERSION`,
`qicom.io.dicom_edit`): the UID policy, each edit (tag, keyword, action, new value), the keywords
that change measurements, and for each file the source and copy names with the SHA-256 of both.
The old value of an edited element is never written: it may be the identity the edit removed.
QICOM writes this log and does not read it back.

### Degradation provenance — version 1

`<name>.json` next to a degraded image (`DEGRADATION_FORMAT_VERSION`,
`qicom.processing.degradation`): the seed, the steps in order with their parameters, the source
format and shape, and the SHA-256 of the source and result pixels (float32, C order). Running the
same steps with the same seed on the same pixels reproduces `result_pixels_sha256`. QICOM writes
this record and does not read it back.

## Headless batch JSON

Every command accepts one JSON object with `output_directory`. A run first removes the files the
previous run in that folder wrote, as listed in its `qicom_batch_outputs.json`, then records its
own: a re-run that fails half-way no longer leaves the earlier results beside a new, empty
receipt and log. Other files in the folder are never touched.

### Image allocation warnings

Image loaders inspect metadata before allocating or decoding pixels. The common DICOM,
NIfTI and bitmap loaders, including folders and explicit stacks, share
`qicom.io.memory_preflight`. Spatial NIfTI images, imported labelmaps and radiomics project
masks use the same policy after `SimpleITK.ImageFileReader.ReadImageInformation()`.
This check also applies when loaders are called by CLI, batch or a project reader.

The warning threshold is **half the currently available RAM**. The estimate counts a lower
bound on additional arrays for this load: the float32 destination for common images, the
destination plus one-slice workspace for stacks, and the spatial image plus its NumPy copy
for radiomics. The available-RAM measurement already reflects earlier resident images.
Decoder buffers, GUI/framework costs, metric temporaries and later analysis allocations
are excluded. The number is therefore **a lower bound, not a peak**. Accepting the warning
does not guarantee avoidance of OOM. Unknown available RAM does not trigger a warning and
must not be interpreted as an assurance that there is enough memory.

In the GUI, the worker waits with only metadata loaded while the GUI thread offers Yes/Cancel.
Cancel leaves the previous panel image and its source identity intact. A stack is considered
as a whole; approval is not repeated for its smaller nested slice reads, and is not reused
for a separate image load. No approval or memory estimate is persisted in preferences,
projects or scientific receipts.

`qicom-cli confronta` and `batch-quantitative` print the estimate to stderr before decoding.
An interactive terminal can answer yes; a non-interactive invocation declines a warned load
unless the user supplies `--allow-large-images`. This flag still prints the warning and changes
only permission to load. A declined batch job is recorded as failed under the existing batch
isolation policy; subsequent jobs continue. The other batch commands read derived tables,
not image volumes, and need no image-allocation override.

```bash
qicom-cli confronta reference.nii.gz comparison.nii.gz --allow-large-images
qicom-cli batch-quantitative jobs.json --allow-large-images
```

Library callers retain their existing loading API: by default they receive `ImageMemoryWarning`
and continue. To decide programmatically before decoding, wrap the call in
`image_memory_policy(handler)`; the handler receives the shape, format, lower-bound bytes and
available bytes, and returns a boolean. False raises `ImageLoadCancelled` (a `ValueError`).
Policies are context-local and restored even after failure. None of these choices resamples,
reduces bit depth, skips slices or alters selected metrics. Large volumes still remain resident.

Quantitative:

```json
{
  "output_directory": "results",
  "profile": "core",
  "jobs": [
    {"id": "case-1", "reference": "ref.nii.gz", "comparison": "cmp.nii.gz",
     "data_range": 4095}
  ]
}
```

Run with `qicom-cli batch-quantitative config.json`.

Qualitative aggregation:

```json
{
  "output_directory": "results",
  "package": "delivery-reader-a.zip",
  "answers": ["reader-a.json", "reader-b.json"],
  "deblinding_maps": ["reader-a.deblinding.json", "reader-b.deblinding.json"]
}
```

Run with `qicom-cli aggregate-qualitative config.json`. `deblinding_maps` is optional and takes
one author map per blinded delivery: each map is applied only to the answers of the delivery it
names, because every delivery randomises its own presentation. Once any map is given, answers of
a delivery without its map are refused; answers from several blinded deliveries are refused
without maps. Answers from a single delivery may be aggregated without its map (a blinded
analysis on the presented labels).

Radiomics cohort:

```json
{
  "output_directory": "results",
  "projects": ["subject-1.zip", "subject-2.zip", "subject-3.zip"],
  "independent_unit": "subject",
  "icc_design": "random",
  "icc_definition": "agreement",
  "icc_unit": "single",
  "image_role": "reference"
}
```

Run with `qicom-cli batch-radiomics-cohort config.json`. The same workflow is available from the
radiomics Tools menu. Exit codes are 0 for success, 1 for completed-with-isolated-failures, 2 for
invalid configuration/I/O at the CLI boundary, and 130 for cancellation/interrupt. Batch outputs
include progress, CSV log, tidy CSV/JSON and a reproducibility receipt; one failed quantitative
job or qualitative answer file does not erase successful jobs.

## Self-contained HTML — versioned inputs, bounded output

HTML reports are generated locally from tables and receipts, escape user-controlled cells, embed
only explicitly selected local images as data URLs, contain no network references, set a
restrictive content-security policy and enforce a configurable byte limit (20 MiB by default).

## Synthetic demo bundle — version 1

`QICOM-synthetic-demo` is written atomically (a scratch directory next to the target, then
`os.replace`). It uses seed `20260517`, contains no PHI, and includes deterministic quantitative
images/results/receipt, qualitative example answers and summary, a real PyRadiomics project, tidy
JSON and a local HTML report. It is a software demo and bug-reproduction artifact, not clinical or
IBSI validation.

Three entry points, one implementation (`qicom.demo.create_demo_bundle`):

| Entry point | Where |
| --- | --- |
| Settings > Diagnostics | anywhere in the GUI |
| Help > Report a Bug | the moment the bundle is actually needed |
| `qicom-cli demo <dir>` | headless, and for whoever regenerates a reported bundle |

`create_demo_bundle` refuses an existing destination — the manifest's per-file SHA-256 only
describes what that run wrote — so the GUI picks the next free name (`-2`, `-3`, …) in
`qicom.gui.demo_bundle` rather than weakening the guarantee. Reproducing a report means running
`qicom-cli demo` locally and diffing `qicom_demo.json`: a hash mismatch on the same
`qicom_version` points at the environment, not at the reporter's data - for every file except
those listed in `run_specific_files` (the receipt, the radiomics project and the tidy JSON),
which record the time of their own run and therefore differ on every generation.

## Optional MIRP radiomics profiles

`RadiomicsConfig.backend` defaults to `pyradiomics`; `ibsi_preset` defaults to
`none`. Missing fields in earlier projects resolve to those defaults. Default
fields are omitted during serialization, preserving existing canonical profile
hashes. A MIRP profile includes `backend: mirp`; an immutable IBSI profile also
includes its preset identifier (`digital-phantom` or `ct-a` through `ct-e`).
MIRP records use distinct feature keys and provider metadata. Editing a reference
profile in the GUI, or duplicating verification as an experiment, clears its
reference preset. Saved original receipts are not rewritten.
