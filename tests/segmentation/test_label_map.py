"""Tests for qicom.segmentation: primitives, the label-map editor and its files."""

from __future__ import annotations

import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from qicom.io.geometry import unknown_geometry, write_nifti
from qicom.segmentation import label_map as label_map_module
from qicom.segmentation.label_files import (
    LABEL_MAP_FORMAT_VERSION,
    MAX_SIDECAR_BYTES,
    load_label_map,
    save_label_map,
    sidecar_path,
)
from qicom.segmentation.label_map import (
    MAX_LABELS,
    PRESETS,
    RANGE_SIGMA,
    UNASSIGNED,
    Label,
    LabelMapEditor,
    SelectionRequest,
    masked_image,
    preset,
    range_from_samples,
    select_pixels,
)
from qicom.segmentation.primitives import (
    close_mask,
    keep_components_touching,
    median_smoothed,
    open_mask,
    remove_small_components,
)


def _phantom() -> np.ndarray:
    """Air around an elliptical body, a bone disc inside, and a second disc of
    the same density outside the body, which a connected selection must skip."""
    _z, y, x = np.mgrid[0:4, 0:40, 0:40]
    volume = np.full((4, 40, 40), -1000.0, dtype=np.float32)
    volume[((y - 20) ** 2 / 15**2 + (x - 16) ** 2 / 12**2) <= 1] = 40.0
    volume[((y - 20) ** 2 + (x - 16) ** 2) <= 16] = 700.0
    volume[((y - 5) ** 2 + (x - 35) ** 2) <= 4] = 700.0
    return volume


class TestPrimitives:
    def test_components_touching_the_seeds_are_kept(self) -> None:
        mask = np.zeros((6, 6), dtype=bool)
        mask[0:2, 0:2] = True
        mask[4:6, 4:6] = True
        seeds = np.zeros_like(mask)
        seeds[0, 0] = True

        result = keep_components_touching(mask, seeds)

        assert result[0:2, 0:2].all() and not result[4:6, 4:6].any()

    def test_seeds_outside_the_mask_select_nothing(self) -> None:
        mask = np.zeros((3, 3), dtype=bool)
        mask[0, 0] = True
        seeds = np.zeros_like(mask)
        seeds[2, 2] = True

        assert not keep_components_touching(mask, seeds).any()

    def test_seeds_must_match_the_mask(self) -> None:
        with pytest.raises(ValueError):
            keep_components_touching(np.zeros((3, 3), bool), np.zeros((2, 2), bool))

    def test_small_components_are_dropped(self) -> None:
        mask = np.zeros((6, 6), dtype=bool)
        mask[0, 0] = True
        mask[3:6, 3:6] = True

        result = remove_small_components(mask, 4)

        assert not result[0, 0] and result[3:6, 3:6].all()
        assert remove_small_components(mask, 1).sum() == mask.sum()
        assert not remove_small_components(np.zeros((2, 2), bool), 3).any()

    def test_opening_removes_a_speck_and_closing_fills_a_gap(self) -> None:
        mask = np.zeros((9, 9), dtype=bool)
        mask[2:7, 2:7] = True
        mask[0, 8] = True
        gap = mask.copy()
        gap[4, 4] = False

        assert not open_mask(mask)[0, 8]
        assert close_mask(gap)[4, 4]

    def test_median_smoothing_stays_within_a_slice(self) -> None:
        volume = np.zeros((3, 5, 5), dtype=np.float32)
        volume[1] = 100.0

        smoothed = median_smoothed(volume, 3)

        assert np.array_equal(smoothed, volume)
        assert median_smoothed(volume, 0) is not None


class TestRanges:
    def test_percentiles_ignore_the_extremes(self) -> None:
        samples = np.concatenate([np.full(98, 10.0), [-1000.0, 1000.0]])

        result = range_from_samples(samples, lower_percentile=2, upper_percentile=98)

        assert result.low == pytest.approx(10.0) and result.high == pytest.approx(10.0)

    def test_sigma_range_is_mean_plus_minus_k_sd(self) -> None:
        samples = np.array([9.0, 11.0])

        result = range_from_samples(samples, method=RANGE_SIGMA, sigmas=2.0)

        assert (result.low, result.high) == pytest.approx((8.0, 12.0))

    def test_an_empty_sample_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no usable pixel"):
            range_from_samples(np.array([np.nan]))

    def test_inverted_percentiles_are_refused(self) -> None:
        with pytest.raises(ValueError):
            range_from_samples(np.array([1.0, 2.0]), lower_percentile=90, upper_percentile=10)

    def test_an_unknown_method_is_refused(self) -> None:
        with pytest.raises(ValueError):
            range_from_samples(np.array([1.0]), method="median")

    def test_every_preset_is_reachable_and_ct_ranges_are_ordered(self) -> None:
        for item in PRESETS:
            assert preset(item.preset_id) is item
            if item.ct_range is not None:
                assert item.ct_range[0] < item.ct_range[1]
        with pytest.raises(KeyError):
            preset("liver")


class TestSelection:
    def test_anywhere_takes_both_discs_connected_takes_one(self) -> None:
        volume = _phantom()
        seeds = np.zeros(volume.shape, dtype=bool)
        seeds[1, 19:22, 15:18] = True

        anywhere = select_pixels(volume, SelectionRequest(600.0, 800.0))
        connected = select_pixels(volume, SelectionRequest(600.0, 800.0, seeds=seeds))

        assert anywhere[:, 5, 35].all()
        assert not connected[:, 5, 35].any()
        assert connected[:, 20, 16].all()

    def test_one_slice_scope_leaves_the_others_untouched(self) -> None:
        volume = _phantom()

        result = select_pixels(volume, SelectionRequest(600.0, 800.0, slice_index=2))

        assert result[2].any() and not result[[0, 1, 3]].any()

    def test_a_slice_outside_the_volume_is_refused(self) -> None:
        with pytest.raises(ValueError):
            select_pixels(_phantom(), SelectionRequest(0.0, 1.0, slice_index=9))

    def test_a_reversed_range_is_reordered(self) -> None:
        volume = _phantom()

        assert np.array_equal(
            select_pixels(volume, SelectionRequest(800.0, 600.0)),
            select_pixels(volume, SelectionRequest(600.0, 800.0)),
        )

    def test_refinements_apply(self) -> None:
        volume = _phantom()
        volume[0, 0, 0] = 700.0  # an isolated speck

        result = select_pixels(
            volume,
            SelectionRequest(
                600.0, 800.0, minimum_voxels=5, fill_holes=True, opening=1, closing=1, smoothing=3
            ),
        )

        assert not result[0, 0, 0]
        assert result[:, 20, 16].all()

    def test_a_non_finite_range_and_a_wrong_shape_are_refused(self) -> None:
        with pytest.raises(ValueError):
            select_pixels(_phantom(), SelectionRequest(float("nan"), 1.0))
        with pytest.raises(ValueError):
            select_pixels(_phantom(), SelectionRequest(0.0, 1.0, seeds=np.zeros((2, 2), bool)))
        with pytest.raises(ValueError):
            select_pixels(np.zeros(4), SelectionRequest(0.0, 1.0))

    def test_a_2d_image_works_with_seeds(self) -> None:
        image = _phantom()[0]
        seeds = np.zeros(image.shape, dtype=bool)
        seeds[20, 16] = True

        result = select_pixels(image, SelectionRequest(600.0, 800.0, seeds=seeds, fill_holes=True))

        assert result[20, 16] and not result[5, 35]


class TestEditor:
    def _editor(self) -> tuple[LabelMapEditor, np.ndarray]:
        volume = _phantom()
        return LabelMapEditor(volume.shape), volume

    def test_assign_takes_only_unassigned_pixels_unless_overwriting(self) -> None:
        editor, volume = self._editor()
        body = editor.add_label("Body")
        bone = editor.add_label("Bone")
        editor.assign(volume > -500, body.value)

        step = editor.assign(volume > 500, bone.value)
        assert step.changed == 0
        step = editor.assign(volume > 500, bone.value, overwrite=True)

        assert step.changed == int((volume > 500).sum())
        counts = editor.counts()
        assert counts[bone.value] + counts[body.value] + counts[UNASSIGNED] == volume.size

    def test_undo_and_redo_restore_the_map_exactly(self) -> None:
        editor, volume = self._editor()
        air = editor.add_label("Air")
        bone = editor.add_label("Bone")
        editor.assign(volume < -500, air.value)
        before = editor.data.copy()
        editor.assign(volume > 500, bone.value)
        after = editor.data.copy()

        assert editor.undo() is not None
        assert np.array_equal(editor.data, before)
        assert editor.can_redo
        assert editor.redo() is not None
        assert np.array_equal(editor.data, after)
        assert editor.redo() is None

    def test_a_new_step_clears_redo(self) -> None:
        editor, volume = self._editor()
        air = editor.add_label("Air")
        editor.assign(volume < -500, air.value)
        editor.undo()

        editor.assign(volume < -900, air.value)

        assert not editor.can_redo

    def test_erase_returns_pixels_to_unassigned(self) -> None:
        editor, volume = self._editor()
        air = editor.add_label("Air")
        editor.assign(volume < -500, air.value)
        corner = np.zeros(volume.shape, dtype=bool)
        corner[:, :2, :2] = True

        step = editor.erase(corner, air.value)

        assert step.changed == corner.sum()
        assert (editor.data[corner] == UNASSIGNED).all()

    def test_removing_a_label_is_undoable(self) -> None:
        editor, volume = self._editor()
        air = editor.add_label("Air")
        editor.assign(volume < -500, air.value)

        editor.remove_label(air.value)
        assert editor.labels == () and not editor.data.any()
        editor.undo()

        assert editor.labels == (air,)
        assert (editor.data == air.value).sum() == (volume < -500).sum()
        editor.redo()
        assert editor.labels == ()

    def test_fill_unassigned(self) -> None:
        editor, volume = self._editor()
        rest = editor.add_label("Rest")

        editor.fill_unassigned(rest.value)

        assert (editor.data == rest.value).all()

    def test_dense_and_sparse_steps_are_both_undoable(self) -> None:
        editor, volume = self._editor()
        label = editor.add_label("X")
        sparse = np.zeros(volume.shape, dtype=bool)
        sparse[0, 0, 0] = True
        editor.assign(sparse, label.value)
        editor.assign(np.ones(volume.shape, dtype=bool), label.value)

        editor.undo()
        assert editor.data.sum() == label.value
        editor.undo()
        assert not editor.data.any()

    def test_history_is_trimmed_to_its_byte_budget(self) -> None:
        volume = _phantom()
        editor = LabelMapEditor(volume.shape, history_byte_budget=1)
        first, second = editor.add_label("X"), editor.add_label("Y")
        everything = np.ones(volume.shape, dtype=bool)
        for label in (first, second, first):
            editor.assign(everything, label.value, overwrite=True)

        assert len(editor.history) == 1
        assert editor.undo() is not None
        assert (editor.data == second.value).all()

    def test_a_step_that_changes_nothing_leaves_no_history(self) -> None:
        editor, volume = self._editor()
        label = editor.add_label("X")
        editor.assign(volume > 500, label.value)
        editor.undo()

        step = editor.assign(np.zeros(volume.shape, dtype=bool), label.value)

        assert step.changed == 0 and editor.history == () and editor.can_redo

    def test_the_map_is_read_only_from_outside(self) -> None:
        editor, _volume = self._editor()

        with pytest.raises(ValueError):
            editor.data[0, 0, 0] = 1

    def test_label_table_rules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        editor, _volume = self._editor()
        label = editor.add_label("  Soft   tissue ")

        assert label.name == "Soft tissue"
        assert editor.rename_label(label.value, "Muscle").name == "Muscle"
        assert editor.recolor_label(label.value, "#123456").color == "#123456"
        with pytest.raises(ValueError):
            editor.add_label("   ")
        with pytest.raises(ValueError):
            editor.add_label("x" * 65)
        with pytest.raises(ValueError):
            editor.recolor_label(label.value, "red")
        monkeypatch.setattr(label_map_module, "MAX_LABELS", 1)
        with pytest.raises(ValueError, match="at most 1 labels"):
            editor.add_label("Second")

    def test_a_map_with_undefined_values_is_refused(self) -> None:
        data = np.zeros((2, 2), dtype=np.uint8)
        data[0, 0] = 3

        with pytest.raises(ValueError, match="no label defines: 3"):
            LabelMapEditor((2, 2), data=data)
        with pytest.raises(ValueError):
            LabelMapEditor((3, 3), data=data)
        with pytest.raises(ValueError):
            LabelMapEditor((2, 2), labels=[Label(1, "a", "#000000"), Label(1, "b", "#000000")])
        with pytest.raises(ValueError):
            LabelMapEditor((2, 2), labels=[Label(MAX_LABELS + 1, "a", "#000000")])

    def test_masks_and_masked_images(self) -> None:
        editor, volume = self._editor()
        air = editor.add_label("Air")
        editor.assign(volume < -500, air.value)

        keep = editor.mask_of({UNASSIGNED})
        result = masked_image(volume, keep, -2000.0)

        assert (result[volume < -500] == -2000.0).all()
        assert (result[volume > -500] == volume[volume > -500]).all()
        with pytest.raises(ValueError):
            masked_image(volume, keep[0], 0.0)
        with pytest.raises(ValueError):
            editor.assign(np.ones((2, 2), bool), air.value)


class TestLabelFiles:
    def test_a_saved_map_reopens_with_its_labels(self, tmp_path: Path) -> None:
        volume = _phantom()
        editor = LabelMapEditor(volume.shape)
        air = editor.add_label("Air", "#4cc9f0", preset_id="air")
        bone = editor.add_label("Bone")
        editor.assign(volume < -500, air.value, parameters={"low": -1100.0, "connected": False})
        editor.assign(volume > 500, bone.value)
        geometry = unknown_geometry(volume.shape)

        nifti, sidecar = save_label_map(tmp_path / "seg", editor, geometry, source_format="nifti")
        loaded = load_label_map(nifti, geometry, volume.shape)

        assert nifti.name == "seg.nii.gz" and sidecar == sidecar_path(nifti)
        assert loaded.had_sidecar
        assert loaded.labels == editor.labels
        assert np.array_equal(loaded.data, editor.data)
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["format_version"] == LABEL_MAP_FORMAT_VERSION
        assert [step["kind"] for step in payload["steps"]] == ["assign", "assign"]
        assert payload["steps"][0]["parameters"]["low"] == -1100.0

    def test_a_map_without_sidecar_gets_generic_names(self, tmp_path: Path) -> None:
        data = np.zeros((3, 4), dtype=np.uint8)
        data[0, 0], data[1, 1] = 2, 5
        geometry = unknown_geometry(data.shape)
        nifti = write_nifti(tmp_path / "foreign", data, geometry)

        loaded = load_label_map(nifti, geometry, data.shape)

        assert not loaded.had_sidecar
        assert [label.name for label in loaded.labels] == ["Label 2", "Label 5"]
        LabelMapEditor(data.shape, labels=loaded.labels, data=loaded.data)

    def test_a_float_map_of_whole_numbers_is_accepted(self, tmp_path: Path) -> None:
        data = np.zeros((3, 4), dtype=np.float32)
        data[0, 0] = 1.0
        geometry = unknown_geometry(data.shape)
        nifti = write_nifti(tmp_path / "f", data, geometry)

        assert load_label_map(nifti, geometry, data.shape).data[0, 0] == 1

    @pytest.mark.parametrize(
        "data",
        [
            np.full((3, 4), 0.5, np.float32),
            np.full((3, 4), 300, np.int32),
            np.full((3, 4), -1, np.int16),
        ],
    )
    def test_maps_that_are_not_labels_are_refused(self, tmp_path: Path, data: np.ndarray) -> None:
        geometry = unknown_geometry(data.shape)
        nifti = write_nifti(tmp_path / "bad", data, geometry)

        with pytest.raises(ValueError):
            load_label_map(nifti, geometry, data.shape)

    def test_another_shape_is_refused(self, tmp_path: Path) -> None:
        geometry = unknown_geometry((3, 4))
        nifti = write_nifti(tmp_path / "small", np.zeros((3, 4), np.uint8), geometry)

        with pytest.raises(ValueError, match="does not have the size"):
            load_label_map(nifti, geometry, (5, 5))

    def test_an_unreadable_file_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.nii.gz"
        path.write_bytes(b"not nifti")

        with pytest.raises(ValueError, match="not a readable NIfTI label map"):
            load_label_map(path, unknown_geometry((2, 2)), (2, 2))

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p.update(format="something-else"),
            lambda p: p.update(format_version=True),
            lambda p: p.update(format_version=2),
            lambda p: p.update(labels="all"),
            lambda p: p["labels"].append(dict(p["labels"][0])),
            lambda p: p["labels"][0].update(value=0),
            lambda p: p["labels"][0].update(value=True),
            lambda p: p["labels"][0].update(name=""),
            lambda p: p["labels"][0].update(color="blue"),
            lambda p: p["labels"][0].update(preset=7),
            lambda p: p["labels"].__setitem__(0, "Air"),
        ],
    )
    def test_a_damaged_sidecar_is_refused(self, tmp_path: Path, mutate: object) -> None:
        editor = LabelMapEditor((2, 2))
        editor.add_label("Air")
        geometry = unknown_geometry((2, 2))
        nifti, sidecar = save_label_map(tmp_path / "seg", editor, geometry)
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        mutate(payload)  # type: ignore[operator]
        sidecar.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError):
            load_label_map(nifti, geometry, (2, 2))

    def test_an_oversized_or_unparsable_sidecar_is_refused(self, tmp_path: Path) -> None:
        editor = LabelMapEditor((2, 2))
        editor.add_label("Air")
        geometry = unknown_geometry((2, 2))
        nifti, sidecar = save_label_map(tmp_path / "seg", editor, geometry)

        sidecar.write_text("{", encoding="utf-8")
        with pytest.raises(ValueError):
            load_label_map(nifti, geometry, (2, 2))
        sidecar.write_bytes(b" " * (MAX_SIDECAR_BYTES + 1))
        with pytest.raises(ValueError):
            load_label_map(nifti, geometry, (2, 2))

    def test_the_sidecar_carries_no_source_name(self, tmp_path: Path) -> None:
        editor = LabelMapEditor((2, 2))
        editor.add_label("Air")

        _nifti, sidecar = save_label_map(tmp_path / "seg", editor, unknown_geometry((2, 2)))

        assert "Doe" not in sidecar.read_text(encoding="utf-8")
        assert set(json.loads(sidecar.read_text(encoding="utf-8"))) == {
            "created_by",
            "format",
            "format_version",
            "geometry_known",
            "labels",
            "layout",
            "nifti_sha256",
            "shape",
            "source_format",
            "steps",
        }
        assert nib.load(str(_nifti)).get_data_dtype() == np.uint8


def test_a_selection_counts_its_stages() -> None:
    image = np.zeros((3, 10, 10), np.float32)
    image[:, 3:7, 3:7] = 100.0
    seeds = np.zeros(image.shape, bool)
    seeds[1, 5, 5] = True
    seen: list[tuple[int, int]] = []

    select_pixels(
        image,
        SelectionRequest(low=50, high=150, seeds=seeds, fill_holes=True, opening=1),
        progress_callback=lambda *p: seen.append(p),
    )

    assert seen == [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]
