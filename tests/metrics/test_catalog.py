"""Tests for qicom.metrics.catalog."""

from __future__ import annotations

from qicom.metrics.catalog import DESCRIPTORS_DIR, EFFORT_LEVELS, catalog, load_descriptor


def test_mse_descriptor_loads_expected_fields() -> None:
    descriptor = load_descriptor("mse")
    assert descriptor.id == "mse"
    assert descriptor.name_short == "MSE"
    assert descriptor.better_direction == "lower"
    assert descriptor.native_3d is True
    assert descriptor.effort == "low"


def test_dists_is_flagged_as_high_effort() -> None:
    # A full VGG16 CNN forward pass - the heaviest metric in the catalog
    # (see qicom.metrics.reference_structural's own measured batching-
    # ceiling comment: "DISTS dominates").
    assert load_descriptor("dists").effort == "high"


def test_every_descriptor_card_declares_a_valid_effort_level() -> None:
    for path in DESCRIPTORS_DIR.glob("*.yaml"):
        descriptor = load_descriptor(path.stem)
        assert descriptor.effort in EFFORT_LEVELS, descriptor.id


def test_reference_error_metrics_are_registered_on_import() -> None:
    import qicom.metrics.reference_error  # noqa: F401  (registration side effect)

    for metric_id in ("mse", "mae", "rmse", "nmse", "psnr"):
        assert metric_id in catalog
        assert callable(catalog.get_function(metric_id))
        assert catalog.get_descriptor(metric_id).id == metric_id


def test_register_metric_sets_docstring_from_descriptor() -> None:
    import qicom.metrics.reference_error as reference_error

    assert reference_error.mse.__doc__ is not None
    assert "Lower is better" in reference_error.mse.__doc__
    assert "Mean Squared Error" in reference_error.mse.__doc__

    assert reference_error.psnr.__doc__ is not None
    assert "Higher is better" in reference_error.psnr.__doc__
    assert "data_range" in reference_error.psnr.__doc__


def test_none_better_direction_renders_as_purely_descriptive() -> None:
    descriptor = load_descriptor("roi_mean")
    assert descriptor.better_direction == "none"
    assert "no inherent better/worse direction" in descriptor.as_docstring()


class TestAsDocstringLocalization:
    """as_docstring() is language-aware (see Translator.tr_or) so the GUI's
    (i) tooltip - which calls it fresh on every hover - follows whichever
    language is currently selected, not always English."""

    def teardown_method(self) -> None:
        from qicom.gui.i18n import translator

        translator.set_language("en")  # tests must not leak language state

    def test_docstring_is_italian_when_language_is_italian(self) -> None:
        from qicom.gui.i18n import translator

        descriptor = load_descriptor("ssim")
        translator.set_language("it")

        docstring = descriptor.as_docstring()

        assert "Indice di similarità strutturale" in docstring
        assert "Valori più alti sono migliori." in docstring
        assert "Parametri:" in docstring

    def test_docstring_falls_back_to_english_by_default(self) -> None:
        descriptor = load_descriptor("ssim")
        docstring = descriptor.as_docstring()

        assert "Structural Similarity Index" in docstring
        assert "Higher is better." in docstring
        assert "Parameters:" in docstring
