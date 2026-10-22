# Qualitative evaluation methodology

QICOM's qualitative workflow is intended for research and quality-control studies. It is not a
medical-device workflow and its statistical output is not a substitute for a prespecified study
protocol reviewed by a qualified statistician.

## Design and blinding

Every choice in this section is made in the authoring flow's first step, "Evaluation settings",
before any page exists: it describes what the study is, not what one page asks.

The author project stores stable case, question, and source-panel identifiers. A separate delivery
plan derives a 64-bit seed from the test ID, delivery ID, author seed and a secret derived from a
random key kept private in the author's profile, so the same delivery re-created by the same author
is the same delivery. It can randomize cases, questions, and panels, or counterbalance panel
positions.
The seed, the secret and the author seed are recorded only in the author's deblinding map, which
therefore reproduces the plan; the evaluator package carries the order actually shown, never what
generated it, so the identifiers it travels with cannot rebuild which panel is which. The shown
order is saved in the evaluator package and response file so a resumed session uses exactly the
same order. A package without a delivery plan is ordered per evaluator: per evaluator account when
the evaluator logged in, per computer otherwise, so two readers sharing one computer get two orders.

“Create blinded evaluator delivery” writes two different files:

- a public ZIP with generic case/panel/file names, neutral A/B/C labels, no author answers, no
  creator-machine ID, and no source-role map;
- an author-only JSON deblinding map relating presented pages, questions, and panels to their source
  identifiers.

Never send the deblinding map to evaluators. Load it only in the aggregation dialog. Pages and
questions of a delivery are numbered in presentation order, hidden repeats included: repeats are
placed at the configured minimum distance and disclosed only by that map. Calibration/example pages and repeat copies are excluded from the
primary inter-rater analysis.

The minimum repeat distance counts intervening presentations. If a sampled source near the end
cannot satisfy it, the planner moves sampled first presentations earlier, retains their relative
order, and appends the corresponding repeats in source order. No sampled source is dropped. A
requested distance greater than or equal to the number of original pages is impossible and is
rejected. Previously persisted presentation plans remain unchanged on resume.

Blinded bitmap copies remove text metadata and EXIF fields other than orientation. Decoded pixel
values are saved losslessly: JPEG images become PNG; floating-point and other unsupported PNG
modes and multiframe images use TIFF. Palettes, transparency, color profiles, pixel density, and
orientation are retained to preserve image interpretation. This process does not inspect retained
ICC profile payloads or remove labels burned into image pixels. Author source files are untouched.

## Response model

Numeric scales may define text anchors for any score. A low-score threshold may make a comment
mandatory. Missing (no record), explicit skip, and “not assessable” are distinct states; the last
may require a reason. Strict 2-AFC permits A or B only, while paired preference permits A, B, or
“No difference”; “not assessable” remains a separate response state.

Time per item is optional and author-configurable. If timing disclosure is disabled, elapsed values
are removed from the returned response file. Timing, straightlining, missingness, and leave-one-out
influence are descriptive review flags only: QICOM performs no automatic exclusion and no default
evaluator centering.

## Descriptive and reliability statistics

Likert-style ratings are reported with median, Q1, Q3, IQR, category counts/percentages, top-box,
bottom-box, divergent-response counts, and response accounting (answered, skipped, not assessable,
missing), plus per-category frequencies and percentages. Categorical/preference questions use
counts and percentages; rankings use Kendall's W; open text is exported verbatim and can be
filtered. Every percentage is a share of the answered responses for that question, never of the
selections: a multiple-choice item lets one respondent tick several categories, so its percentages
deliberately do not sum to 100%. The denominator and its definition are exported next to the
percentage. Skipped, not-assessable, and missing responses are excluded from both numerator and
denominator and stay reported as distinct states.

Response completion is a different denominator from category frequency. The core
`descriptive_by_question(..., expected_assignments=...)` API accepts an explicit assignment table,
with one evaluator/page/panel/question/slice row per expected response. It counts absent responses
against that table, including readers who returned no row, and supports incomplete study designs.
Without this table, the export states `received_records; unrecorded_assignments_unknown`, reports
the response rate among received records, and leaves the unrecorded missing count undefined.
Readers observed on another question are never assumed assigned to every question. The current
GUI exporter uses the received-records denominator unless an assignment table is supplied through
the core API; a protocol's assignment roster must be retained separately.

The primary ordinal agreement coefficient is Krippendorff's alpha, which accepts incomplete rating
matrices. It uses Krippendorff's ordinal difference function, not a rank distance: the distance
between two categories is the mass of the categories lying between them, taking half of each
endpoint's own marginal frequency,

    delta(c, k) = ( sum_{g=c..k} n_g - (n_c + n_k) / 2 ) ** 2

so two adjacent categories that both attracted many ratings count as further apart than two
adjacent categories that were barely used. Each unit's observed disagreement is weighted by
1/(m_u - 1) before averaging over the pairable values, so units rated by fewer evaluators are not
under-weighted when ratings are missing. Results carry the formula version (currently v2) in the
exported method label; v1 used a uniform squared rank distance and pooled pair counts, and its
values are not comparable. Gwet AC2 with quadratic ordinal weights is labelled advanced and keeps
the quadratic weights its name promises. AC2 formula v2 uses category proportions averaged equally
across nonempty units, including singleton units; observed agreement uses pairable units only.
For a q-category agreement matrix W, its chance term is
`sum(W) * sum(p * (1 - p)) / (q * (q - 1))`, following the
[coefficient author's irrCAC implementation](https://github.com/cran/irrCAC/blob/master/R/agree.coeff3.raw.r).
The original observed-category weights are held fixed in nonempty-unit percentile-bootstrap
resamples. AC2 v1 used an incorrect chance expression and pooled marginals; its values are not
numerically comparable. The AC2 method label includes v2 in new reports, while legacy stored
results remain readable without rewriting their values. Effective unit/rater/rating counts, missing counts,
confidence intervals, and method limitations are exported. Legacy weighted Cohen/Fleiss kappa and
ICC(A,1) absolute agreement remain visible for continuity. Hidden repeats are
analysed separately within evaluator using quadratic weighted kappa, mean absolute difference, and
mean signed difference.

## Paired comparisons and uncertainty

Slices are averaged inside each evaluator-by-case cell; they are never treated as independent
subjects. By default an evaluator answers once for the whole series, so a cell holds one answer
per question; the author can instead ask for every slice to be answered on its own ("Answers
apply to" in the evaluation settings). Paired results report median A−B, probability of superiority, and the paired Wilcoxon
test. Confidence intervals independently resample evaluator and case clusters. Families of
Wilcoxon tests include both raw p-values and Holm-adjusted p-values.

Precision output reports confidence-interval width, usable/complete-case counts, and loss due to
missingness. A narrow interval does not repair a weak sampling design, correlated cases, a small
reader panel, or post-hoc endpoint selection.

## What a blinded delivery removes

Beyond neutral file names, case and panel labels, a blinded delivery rebuilds bitmaps from their
pixels, scrubs NIfTI headers (text fields, extensions, gzip file name), de-identifies every DICOM
again - with UIDs of its own for each case, so a hidden repeat does not share its first
presentation's - and empties the DICOM attributes that could reveal how an image was acquired or
reconstructed: manufacturer, model,
software, station, protocol and series/study descriptions, convolution kernel and filter,
reconstruction method, tube voltage and current, exposure, CTDIvol and pitch, with the
contributing-equipment and reconstruction-algorithm sequences removed. Pixel data are never
altered, so differences an evaluator can see in the image itself (noise texture, sharpness)
remain - which is what is being rated.
