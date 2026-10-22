# Deprecation policy and audit

QICOM follows Semantic Versioning. A public compatibility API is therefore
removed only in a major release, after all of these conditions are met:

1. the replacement has shipped in at least one minor release;
2. the old API emits `DeprecationWarning` and has a documented migration;
3. QICOM's runtime has no remaining callers;
4. compatibility tests still prove the old and new paths agree before removal;
5. the removal is listed under `Removed` in the changelog.

Project/session file readers are governed separately: support for persisted
1.x data is not removed merely because a Python helper is deprecated.

## Audit - 2026-08-28

| Deprecated API | Replacement | Runtime callers | 1.x decision |
|---|---|---:|---|
| `qicom.io.dicom_io.try_load_dicom_slice` | `load_dicom_image_with_metadata` or `load_dicom_series_with_metadata` | 0 | Retain; remove no earlier than 2.0 |
| `qicom.metrics.runner.compute_per_slice_reference_metrics` | `run_analysis_metrics(...).per_slice` | 0 | Retain; remove no earlier than 2.0 |
| `RadiomicsController.load_project` | `load_radiomics_project`, then `apply_project_data` | 0 | Retain; remove no earlier than 2.0 |
| `qicom.radiomics.extraction.available_memory_bytes` | `qicom.runtime.available_memory_bytes` | 0 | Retain; remove no earlier than 2.0 |
| `qicom.radiomics.extraction.resolve_radiomics_cache_budget` | `qicom.runtime.resolve_cache_budget` | 0 | Retain; remove no earlier than 2.0 |

The compatibility-only but not deprecated APIs `run_all_metrics`,
`load_image_stack`, `load_image_folder`, and `preprocess_image_and_mask` remain
supported throughout 1.x. They are thin adapters, not dead implementations.

`tests/test_deprecation_policy.py` enforces the zero-runtime-caller result for
the explicitly deprecated function names. Existing I/O, metric and radiomics
tests continue to verify the warning and output compatibility of each adapter.

The resource helpers were promoted to the Qt-free `qicom.runtime` module in
1.1 development. Change the import path and rename the budget resolver; its
arguments, validation and one-eighth available-RAM ceiling are unchanged.
The compatibility resolver still interprets an omitted maximum as 512 MiB.
Automatic profile selection is explicit at the caller; extraction sessions
now select a profile when their own `max_cached_image_bytes` is omitted.
Both old helpers emit `DeprecationWarning`. Their replacement must ship in a
minor release before the removal policy above can be satisfied.

## Planned for 2.0 - locale files move under `qicom/i18n/`

`qicom.i18n.translator` is the importable translator, and it reads its JSON
locale files from `qicom/gui/i18n/locales/`. That is backwards: the translator
belongs to the core (the CLI, the reporting layer and the receipt all call
`translator.tr`, none of them needs Qt), while its data sits inside the GUI
package. Anything importing `qicom.i18n` therefore depends on a path inside
`qicom.gui` existing, and `pyproject.toml`'s `package-data` has to ship
`gui/i18n/locales/*.json` for a non-GUI install to be able to read a string.

The move is deliberately **not** a 1.x change. `qicom/gui/i18n/locales/` is a
packaged data path: it is named in `pyproject.toml`'s `package-data`, in
`packaging/qicom.spec`'s collected data files, and by every existing frozen
bundle. Relocating it while 1.x builds are in circulation would mean two
layouts to support with no compatibility benefit, since nothing outside QICOM
reads those files.

Plan, following the policy above:

1. **1.x (now)** - no change. `LOCALES_DIR` stays pointed at the GUI package,
   and the layout is documented here so it is a decision rather than an
   accident.
2. **The last 1.x minor** - `qicom/i18n/locales/` becomes the primary
   location, with `qicom/gui/i18n/locales/` kept as a compatibility shim that
   resolves to it. The shim emits no warning: nothing user-facing imports the
   path, so a `DeprecationWarning` would only reach QICOM's own code.
   `pyproject.toml` and `qicom.spec` ship both paths for that release.
3. **2.0** - the shim is removed, `package-data` and the PyInstaller spec name
   only `qicom/i18n/locales/`, and the removal is listed under `Removed` in the
   changelog.

Acceptance for step 2: every locale key resolves identically through both
paths, a frozen bundle built from that revision finds its locales, and
`tests/gui/test_translator.py` runs against both locations.

## NSS metric names (1.1 development)

The displayed names BRISQUE and NIQE are replaced by **NSS feature distance**
and **NSS patch-distribution distance**. These QICOM distances retain their
existing formulas and values. The historical descriptor IDs `brisque` and
`niqe` remain stable serialization keys, including in receipts and saved
projects; they do not designate the canonical published algorithms. New
result tables pair these keys with the corrected `metric_name`. Public Python
aliases `nss_feature_distance` and `nss_patch_distribution_distance` are available.
Old result files and their hashes are preserved.

EPI is displayed as **EPI (Laplacian correlation)**, preserving ID `epi`.
