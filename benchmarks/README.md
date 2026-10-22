# QICOM performance baseline

`performance_baseline.py` measures the main application workloads with generated,
deterministic data only. It never reads clinical files.

Run the small diagnostic profile:

```bash
python -m benchmarks.performance_baseline --profile smoke --repeats 1
```

Run the acceptance-sized baseline (the ROI case uses `200 x 512 x 512`, the
bitmap stack uses 64 synthetic `512 x 512` slices, and the DICOM case writes
128 synthetic `512 x 512` slices):

```bash
python -m benchmarks.performance_baseline --profile baseline --repeats 3
```

Use repeated `--case` options to isolate workloads. JSON reports contain
timing observations, sampled process working set, Python allocation peaks,
output digests, and structural call counts. Timings are intentionally not CI
thresholds; deterministic outputs and optimization contracts are checked in
`tests/performance`.

The memory cases are `calculate_2d`, `calculate_3d`, `bitmap_stack`,
`dicom_load`, and `roi_analysis`. The last case records both the actual bytes
retained by unique ROI representations and whether the role/region mappings
share one rasterization.

The two export cases are separate on purpose. `export_results` writes the
workbook and, by default, the Markdown AI report as well, so one number
covered two pieces of work with different costs and different reasons to
change - a regression in either moved it and neither could be attributed.
`excel_only` is the workbook writer; the difference between it and
`full_export_with_ai` is what the Markdown export costs.

## Comparing against a stored baseline

```bash
python -m benchmarks.performance_baseline --profile baseline --repeats 3 \
    --output baseline.json
# ... later, on the same host ...
python -m benchmarks.performance_baseline --profile baseline --repeats 3 \
    --compare baseline.json
```

`--compare` **refuses** rather than guesses. A baseline written under a
different benchmark schema, a different profile, or a different host/Python/
NumPy/pandas/CPU count is not comparable, and reading its differences as a
regression - or as an all-clear - is worse than having no baseline: a faster
machine hides a real regression, a slower one invents a fake alarm. Every
report carries `schema_version` and an `environment_key` summarising exactly
the fields that have to match.

A median more than 20% slower than the baseline is reported as a regression
and exits non-zero. Cases whose median is under 50 ms are printed but cannot
fail the comparison on their own: at that scale a percentage is scheduler
jitter, not code.

Re-record a baseline only on the same host and profile, and never to make a
red comparison green - that is how a regression becomes the new normal.

Measure launcher startup and the idle memory of the three workbench windows
in fresh processes:

```bash
python -m benchmarks.startup_baseline --repeats 5
```

Use repeated `--case` options to isolate `launcher_import`,
`launcher_construct`, `quantitative_idle`, `qualitative_idle`, or
`radiomics_idle`. The child processes use an offscreen Qt platform and an
in-memory quantitative session, so the benchmark neither shows windows nor
touches application data.
