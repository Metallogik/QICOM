# QICOM task-based tools: formulas, domain and compatibility

These expert Python APIs support technical phantom and research/QC analyses.
QICOM is not a medical device. These tools do not establish clinical thresholds
or predict an individual patient's outcome. They are separate from the generic
metric runner and currently have no dedicated GUI calculation workflow.

## Line-profile transition distance

`rise_distance_10_90(profile, sample_spacing=...)` estimates the starting and
ending plateaus as medians of the first/last 10% of samples (at least two).
For plateaus `a` and `b`, the normalized transition is `(profile-a)/(b-a)`.
The signed denominator handles rising and falling edges identically: both run
from normalized zero to one. A second inversion for falling edges was erroneous
and has been removed.

The first upward crossings of normalized 0.1 and 0.9 are linearly interpolated.
`distance` is their positive separation in the units of `sample_spacing`;
`position_10` and `position_90` refer to **progress through the transition**, not
the lower and upper absolute intensity levels. `direction` records the original
polarity. Profiles require at least eight finite samples, positive finite spacing,
nonzero plateau contrast and ordered crossings. This procedure is intended for
a single sufficiently sampled transition; it is not an edge detector or a
general denoising/monotonicity certification procedure.

## Axis-aligned edge transfer estimate

`edge_transfer_function` requires a finite ROI of at least 16×16 pixels, known
positive pixel spacing and an explicit `edge_is_suitable=True` declaration.
The edge must run parallel to the row-index direction (vertically in the array),
with the intensity transition along columns. Only `edge_angle_degrees=0` is
accepted. Previously nonzero angles were recorded without affecting the
calculation; they are now rejected rather than implying an angle correction.

The estimator averages rows to form an edge-spread profile, applies a Gaussian
filter with sigma 0.5 pixel, differentiates with `numpy.gradient`, applies a Hann
window and takes the magnitude of the real FFT. The transfer curve is normalized
by its DC component. The first 0.5 crossing is linearly interpolated for MTF50;
absence of a crossing returns NaN. The measured transfer therefore includes the
effects of the derivative, smoothing, window and finite sampling.

`quality_status` compares plateau contrast-to-noise with the caller's threshold
(default 10); PASS confirms only that CNR check. It does not validate straightness,
stationarity or suitability. No slanted-edge projection, oversampling, fitting of
edge angle, derivative-response correction or circular-edge TTF is implemented.
An anatomical edge cannot characterize the imaging system generally.

## Detectability formula version 2

Let `f` be the supplied strictly increasing frequency axis, `W` the declared task
function, `T` the transfer function, `N>0` the NPS and `S=|W T|²`. QICOM evaluates
trapezoidal integrals over exactly the supplied one-dimensional frequency domain:

| Observer | Detectability d′ |
| --- | --- |
| PW | `sqrt(∫ S/N df)` |
| NPW | `∫ S df / sqrt(∫ S N df)` |
| NPWE | `∫ S E² df / sqrt(∫ S N E⁴ df)` |

NPW uses a signal-matched template without noise prewhitening. NPWE applies an
explicit eye-filter amplitude response `E` to both image and template. The NPWE
argument `eye_filter` must match the frequency axis, be finite and nonnegative,
and contain nonzero sensitivity. Missing eye filters raise `ValueError`; QICOM
does not invent viewing geometry or a default eye response. Passing an eye filter
to NPW/PW is also rejected. A unity eye response recovers NPW exactly.

These expressions follow Justin Solomon and Ehsan Samei, *Correlation between
human detection accuracy and observer model-based image quality metrics in
computed tomography*, J. Med. Imaging 3(3), 035506 (2016), equations 6 and 10,
restricted here to a declared 1D domain.
[Primary article](https://doi.org/10.1117/1.JMI.3.3.035506),
[author institution record](https://scholars.duke.edu/publication/1147529).

No radial `2πf` integration measure, automatic negative-frequency factor, internal
noise or display/viewing conversion is supplied. In particular, passing a radial
average of a 2D NPS does not by itself implement a 2D observer. The caller must
provide compatible task, transfer and noise units and spectral conventions.
These values are mathematical observer SNRs for the configured task, not human
performance measurements. A zero signal produces zero detectability.

Eye amplitudes are normalized by their maximum internally. This common gain
cancels from d′, avoids fourth-power overflow and defines the normalized
`eye_filter_hash` recorded in the result. Save the input arrays and viewing model
separately when reporting an experiment: a hash does not reconstruct them.

## Historical results and API migration

The previous implementation evaluated `sqrt(∫S/N df)` for both labels NPW and
NPWE. That is the PW formula. New calculations carry `formula_version=2`,
`integration_measure="df"` and, for NPWE, the normalized eye-filter hash.
`PHANTOM_TASK_PROFILE` version changes from 1.0 to 2.0; existing tool IDs remain
unchanged.

`DetectabilityResult.from_dict()` accepts an old five-field dictionary and assigns
`formula_version=1`. It preserves the original numeric value, observer label,
frequency limits and methodology string. Its `effective_observer` property
returns PW, so reading old data does not reinterpret it as a corrected NPW/NPWE
measurement. Serializing and reopening that result retains version 1. New version
2 records also round-trip, while unknown formula versions or integration
measures are rejected. No global project schema or stored result is rewritten.

To reproduce the historical numeric calculation, call `observer="PW"` explicitly.
To calculate NPWE with version 2, supply the intended `eye_filter`; old calls
without that required scientific input now fail explicitly. Existing default NPW
calls remain syntactically valid and use the corrected formula identified by v2.

Regression tests cover polarity/crossing coordinates, unequal colored-noise
weights that distinguish PW and NPW, nontrivial NPWE amplitudes, eye-gain
invariance, unity-eye equivalence, invalid inputs, unsupported angles and
version 1/version 2 JSON round-trips.
