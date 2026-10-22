"""`qicom-cli` entry point: minimal command-line access to the core, no GUI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from qicom.batch import (
    load_batch_configuration,
    run_qualitative_aggregation_batch,
    run_quantitative_batch,
    run_radiomics_cohort_batch,
)
from qicom.io.loader import (
    WIDE_DATA_RANGE_FACTOR,
    combined_default_data_range,
    load_image_with_metadata,
    oversized_data_range,
)
from qicom.io.memory_preflight import ImageAllocationWarning, image_memory_policy
from qicom.metrics.run_configuration import quantitative_configuration
from qicom.metrics.runner import run_all_metrics
from qicom.metrics.statistics import has_confidence_intervals
from qicom.reporting.excel_safety import safe_excel_dataframe
from qicom.reporting.llm_export import AnalysisContext, write_llm_report

# The GUI carries this caveat in a label, a tooltip and an extra workbook
# sheet (see qicom.metrics.statistics, results_table.ci_note); a CSV has
# nowhere to put it, so it goes to stderr instead - printed whenever the
# results actually contain an interval, never as unconditional noise.
# Hardcoded English like every other message here, rather than routed
# through the translator, whose locales are a GUI resource.
_CONFIDENCE_INTERVAL_NOTE = (
    "qicom-cli: ci_lower/ci_upper are a 95% Student-t interval on the mean across the "
    "stack's slices. Slices of one volume are spatially correlated, not independent "
    "observations, so the interval is narrower (more optimistic) than a genuine "
    "independent-sample interval - descriptive of variation along the stack, not a "
    "formal statistical inference."
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qicom-cli",
        description="QICOM command-line interface - core metrics validation without a GUI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    confronta = subparsers.add_parser(
        "confronta",
        help="Compare a reference and a comparison image/volume and report all applicable metrics.",
    )
    confronta.add_argument("reference", type=Path, help="Path to the reference image/volume.")
    confronta.add_argument("comparison", type=Path, help="Path to the comparison image/volume.")
    _add_memory_override(confronta)
    confronta.add_argument(
        "--data-range",
        type=float,
        default=None,
        help="Data range (max possible value minus min possible value) for metrics that "
        "need it (PSNR, SSIM, ...). If omitted, it is inferred from both images - the "
        "widest of their representable ranges and of their joint observed span, at least "
        "1 - exactly as the GUI does, and printed explicitly - never assumed silently.",
    )
    confronta.add_argument(
        "--hounsfield-units",
        action="store_true",
        help="Declare both inputs as CT pixel values in Hounsfield units. RIQE is computed "
        "only with this declaration; both images are scored on the reference's field of "
        "view and body mask.",
    )
    confronta.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Also write the results table to this file (.xlsx for Excel, .md for a "
        "self-contained Markdown report meant to be pasted into an AI chat assistant, "
        "otherwise CSV). The .md report carries each metric's definition and no file "
        "names, image headers or patient identifiers; QICOM performs no AI processing "
        "and uploads nothing.",
    )
    demo = subparsers.add_parser(
        "demo",
        help="Write the deterministic, PHI-free synthetic demo / bug-report bundle.",
    )
    demo.add_argument(
        "destination",
        type=Path,
        help="Directory to create. It must not exist yet: the manifest's per-file SHA-256 "
        "hashes only describe what this run wrote, so an existing directory is never "
        "reused or overwritten.",
    )

    for command, help_text in (
        (
            "batch-quantitative",
            "Run a versioned quantitative JSON batch with tidy output and receipts.",
        ),
        (
            "aggregate-qualitative",
            "Aggregate a qualitative package and answer files without the GUI.",
        ),
        (
            "batch-radiomics-cohort",
            "Aggregate radiomics project tables and cohort statistics without loading images.",
        ),
    ):
        batch_parser = subparsers.add_parser(command, help=help_text)
        batch_parser.add_argument("configuration", type=Path, help="Path to JSON configuration.")
        if command == "batch-quantitative":
            _add_memory_override(batch_parser)
    return parser


def _add_memory_override(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-large-images",
        action="store_true",
        help="Continue past pre-allocation memory warnings. Estimates are lower bounds, "
        "not peaks; this does not guarantee that loading or analysis will avoid "
        "running out of memory.",
    )


def _confirm_image_allocation(warning: ImageAllocationWarning, *, allow: bool) -> bool:
    print(f"qicom-cli: memory warning: {warning.message()}", file=sys.stderr, flush=True)
    if allow:
        return True
    if sys.stdin.isatty():
        print("qicom-cli: continue loading? [y/N] ", file=sys.stderr, end="", flush=True)
        try:
            return input().strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False
    print(
        "qicom-cli: no interactive confirmation is available; use --allow-large-images "
        "to proceed explicitly.",
        file=sys.stderr,
        flush=True,
    )
    return False


def _run_demo(args: argparse.Namespace) -> int:
    """The CLI half of Settings > Diagnostics / Help > Report a Bug.

    Same bundle, same fixed seed: whoever receives a bug report runs this
    on their own machine and diffs qicom_demo.json against the reporter's,
    which is the whole point of the artifact - no patient data has to
    travel for the problem to be reproduced.
    """
    from qicom.demo import DEMO_SEED, create_demo_bundle

    try:
        destination = create_demo_bundle(args.destination)
    except FileExistsError:
        print(
            f"qicom-cli: error: {args.destination} already exists; "
            "choose a path that does not exist yet.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as exc:
        print(f"qicom-cli: error: could not create the demo bundle: {exc}", file=sys.stderr)
        return 1
    print(
        f"qicom-cli: synthetic demo written to {destination} (seed {DEMO_SEED}, no PHI). "
        "Attach the whole folder to a bug report; qicom_demo.json carries the QICOM "
        "version, the platform and a SHA-256 per file.",
        file=sys.stderr,
    )
    return 0


def _run_batch(args: argparse.Namespace) -> int:
    runners = {
        "batch-quantitative": run_quantitative_batch,
        "aggregate-qualitative": run_qualitative_aggregation_batch,
        "batch-radiomics-cohort": run_radiomics_cohort_batch,
    }
    try:
        configuration = load_batch_configuration(args.configuration)
        summary = runners[args.command](
            configuration,
            progress=lambda done, total, message: print(
                f"qicom-cli: [{done}/{total}] {message}", file=sys.stderr
            ),
        )
    except KeyboardInterrupt:
        print("qicom-cli: cancelled by user", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError) as exc:
        print(f"qicom-cli: error: {exc}", file=sys.stderr)
        return 2
    print(
        f"qicom-cli: {summary.status}; completed={summary.completed}, failed={summary.failed}",
        file=sys.stderr,
    )
    return summary.exit_code


def _run_confronta(args: argparse.Namespace) -> int:
    try:
        loaded1 = load_image_with_metadata(args.reference)
        loaded2 = load_image_with_metadata(args.comparison)
        img1 = loaded1.pixels
        img2 = loaded2.pixels
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"qicom-cli: error: {exc}", file=sys.stderr)
        return 1

    data_range = args.data_range
    if data_range is None:
        # The rule the GUI and the batch runner use too: the same images get
        # the same default range whichever way they are analysed.
        data_range = combined_default_data_range((loaded1, loaded2))
        print(
            f"qicom-cli: --data-range not given, inferred {data_range!r} as the physical range "
            f"from both inputs ({loaded1.source_dtype}, {loaded2.source_dtype}).",
            file=sys.stderr,
        )

    wide = oversized_data_range(float(data_range), (loaded1, loaded2))
    if wide is not None:
        print(
            f"qicom-cli: warning: data range {wide[0]:g} is more than "
            f"{WIDE_DATA_RANGE_FACTOR:g} times the images' observed span ({wide[1]:g}); "
            "SSIM and PSNR will barely respond to differences. Pass --data-range closer to "
            "the span if that suits the comparison.",
            file=sys.stderr,
        )

    try:
        # The configuration the GUI and a batch job build for the same run.
        config = quantitative_configuration(
            float(data_range),
            role_data_ranges=(float(loaded1.data_range), float(loaded2.data_range)),
            hounsfield_units=(args.hounsfield_units, args.hounsfield_units),
            slice_orientations=(loaded1.slice_orientation, loaded2.slice_orientation),
        )
        results = run_all_metrics(img1, img2, config=config)
    except ValueError as exc:
        print(f"qicom-cli: error: {exc}", file=sys.stderr)
        return 1

    if has_confidence_intervals(results):
        print(_CONFIDENCE_INTERVAL_NOTE, file=sys.stderr)

    if args.output is not None:
        exported_results = safe_excel_dataframe(results)
        try:
            if args.output.suffix.lower() == ".xlsx":
                exported_results.to_excel(args.output, index=False, engine="openpyxl")
            elif args.output.suffix.lower() == ".md":
                write_llm_report(
                    results,
                    args.output,
                    context=AnalysisContext(
                        data_range=float(data_range),
                        dimensionality="3D" if img1.ndim >= 3 else "2D",
                        n_slices=int(img1.shape[0]) if img1.ndim >= 3 else None,
                    ),
                )
            else:
                exported_results.to_csv(args.output, index=False)
        except (OSError, ValueError) as exc:
            # An unwritable directory, a name the filesystem rejects, a
            # missing Excel engine: a diagnostic line and a non-zero exit,
            # not a traceback - the metrics themselves already succeeded.
            print(f"qicom-cli: error: could not write {args.output}: {exc}", file=sys.stderr)
            return 1
        print(f"qicom-cli: results written to {args.output}", file=sys.stderr)

    display = results.assign(
        value=results["value"].round(4),
        std=results["std"].round(4),
        ci_lower=results["ci_lower"].round(4),
        ci_upper=results["ci_upper"].round(4),
    )
    with pd.option_context("display.max_rows", None, "display.width", 120):
        print(display.to_string(index=False))

    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    with image_memory_policy(
        lambda warning: _confirm_image_allocation(
            warning, allow=getattr(args, "allow_large_images", False)
        )
    ):
        if args.command == "confronta":
            return _run_confronta(args)
        if args.command == "demo":
            return _run_demo(args)
        if args.command in {
            "batch-quantitative",
            "aggregate-qualitative",
            "batch-radiomics-cohort",
        }:
            return _run_batch(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
