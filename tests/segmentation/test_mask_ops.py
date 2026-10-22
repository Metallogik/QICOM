"""Tests for qicom.segmentation.mask_ops."""

from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from qicom.segmentation.mask_ops import (
    ANY_LABEL,
    DIFFERENCE,
    INTERSECTION,
    UNION,
    XOR,
    combine_masks,
    combine_volumes,
    compare_masks,
    compare_volumes,
    interpolate_slices,
    load_label_volume,
    save_mask,
)


def _write(path: Path, data: np.ndarray, zooms: tuple[float, ...] = (1.0, 1.0, 2.0)) -> Path:
    affine = np.diag([*zooms, 1.0][:4]) if len(zooms) == 3 else np.diag([*zooms, 1.0, 1.0])
    nib.save(nib.Nifti1Image(data, affine), str(path))
    return path


def _cube(start: int, size: int, shape: tuple[int, int, int] = (20, 20, 10)) -> np.ndarray:
    mask = np.zeros(shape, dtype=np.uint8)
    mask[start : start + size, start : start + size, 2:8] = 1
    return mask


class TestCombine:
    def test_boolean_operations(self) -> None:
        a = np.array([1, 1, 0, 0], bool)
        b = np.array([1, 0, 1, 0], bool)

        assert combine_masks(UNION, a, b).tolist() == [True, True, True, False]
        assert combine_masks(INTERSECTION, a, b).tolist() == [True, False, False, False]
        assert combine_masks(DIFFERENCE, a, b).tolist() == [False, True, False, False]
        assert combine_masks(XOR, a, b).tolist() == [False, True, True, False]
        with pytest.raises(ValueError):
            combine_masks("nand", a, b)
        with pytest.raises(ValueError):
            combine_masks(UNION, a, b[:2])

    def test_labels_are_chosen_per_map(self, tmp_path: Path) -> None:
        data = np.zeros((4, 4, 2), np.uint8)
        data[0] = 1
        data[1] = 2
        volume = load_label_volume(_write(tmp_path / "multi.nii.gz", data))

        assert volume.labels == (1, 2)
        result = combine_volumes(UNION, volume, 1, volume, 2)
        assert result[:2].all() and not result[2:].any()
        assert volume.mask(ANY_LABEL).sum() == 16

    def test_two_grids_are_refused(self, tmp_path: Path) -> None:
        a = load_label_volume(_write(tmp_path / "a.nii.gz", _cube(2, 5)))
        b = load_label_volume(_write(tmp_path / "b.nii.gz", _cube(2, 5), zooms=(1.0, 1.0, 3.0)))

        with pytest.raises(ValueError, match="not on the same grid"):
            combine_volumes(UNION, a, ANY_LABEL, b, ANY_LABEL)

    def test_a_saved_result_lands_on_the_same_grid(self, tmp_path: Path) -> None:
        a = load_label_volume(_write(tmp_path / "a.nii.gz", _cube(2, 5)))

        written = save_mask(tmp_path / "out", a.mask(), a)

        image = nib.load(str(written))
        assert np.allclose(image.affine, nib.load(str(a.path)).affine)
        assert np.array_equal(np.asarray(image.dataobj), a.data.astype(np.uint8))


class TestLoading:
    def test_non_label_files_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="whole-number"):
            load_label_volume(_write(tmp_path / "f.nii.gz", np.full((3, 3, 3), 0.5, np.float32)))
        (tmp_path / "x.nii.gz").write_bytes(b"nope")
        with pytest.raises(ValueError, match="not a readable"):
            load_label_volume(tmp_path / "x.nii.gz")
        four_d = tmp_path / "4d.nii.gz"
        nib.save(nib.Nifti1Image(np.zeros((2, 2, 2, 2), np.uint8), np.eye(4)), str(four_d))
        with pytest.raises(ValueError, match="2D or 3D"):
            load_label_volume(four_d)


class TestInterpolation:
    def test_empty_slices_between_drawn_ones_are_filled_smoothly(self) -> None:
        mask = np.zeros((20, 20, 9), bool)
        mask[5:9, 5:9, 0] = True  # a small square
        mask[3:15, 3:15, 8] = True  # a larger one

        result = interpolate_slices(mask, axis=2, spacing=(1.0, 1.0, 2.0))

        areas = [int(result[:, :, index].sum()) for index in range(9)]
        assert areas[0] == 16 and areas[8] == 144
        assert all(earlier <= later for earlier, later in zip(areas, areas[1:], strict=False))
        assert 16 < areas[4] < 144

    def test_neighbouring_and_single_slices_are_left_alone(self) -> None:
        mask = np.zeros((5, 5, 3), bool)
        mask[1:3, 1:3, 0] = True
        mask[1:3, 1:3, 1] = True

        assert np.array_equal(interpolate_slices(mask, 2, (1.0, 1.0, 1.0)), mask)
        with pytest.raises(ValueError, match="3D"):
            interpolate_slices(mask[:, :, 0], 2, (1.0, 1.0))


class TestComparison:
    def test_identical_masks(self) -> None:
        mask = _cube(2, 6).astype(bool)

        result = compare_masks(mask, mask, (1.0, 1.0, 2.0))

        assert result.dice == 1.0 and result.jaccard == 1.0
        assert result.hausdorff_mm == 0.0 and result.mean_surface_mm == 0.0
        assert result.volume_a_ml == pytest.approx(6 * 6 * 6 * 2.0 / 1000.0)
        assert result.volume_difference_percent == 0.0

    def test_a_shifted_mask_is_measured_in_millimetres(self) -> None:
        a = _cube(2, 6).astype(bool)
        b = _cube(4, 6).astype(bool)

        result = compare_masks(a, b, (1.0, 1.0, 2.0))

        overlap = 4 * 4 * 6
        assert result.dice == pytest.approx(2 * overlap / (2 * 6 * 6 * 6))
        assert result.hausdorff_mm == pytest.approx(np.sqrt(8.0), rel=1e-6)
        assert result.hausdorff95_mm is not None and result.hausdorff95_mm <= result.hausdorff_mm

    def test_empty_masks_have_no_distances(self) -> None:
        empty = np.zeros((4, 4, 4), bool)

        both_empty = compare_masks(empty, empty, (1.0, 1.0, 1.0))
        one_empty = compare_masks(_cube(0, 2, (4, 4, 4)).astype(bool), empty, (1.0, 1.0, 1.0))

        assert both_empty.dice is None and both_empty.jaccard is None
        assert one_empty.dice == 0.0 and one_empty.hausdorff_mm is None
        assert one_empty.volume_difference_percent == -100.0
        with pytest.raises(ValueError):
            compare_masks(empty, empty[0], (1.0, 1.0))

    def test_volumes_use_the_file_spacing(self, tmp_path: Path) -> None:
        a = load_label_volume(_write(tmp_path / "a.nii.gz", _cube(2, 5), zooms=(0.5, 0.5, 3.0)))

        result = compare_volumes(a, ANY_LABEL, a, ANY_LABEL)

        assert result.volume_a_ml == pytest.approx(5 * 5 * 6 * 0.75 / 1000.0)


def test_comparing_and_filling_report_progress() -> None:
    a = np.zeros((6, 8, 8), bool)
    a[0, 2:6, 2:6] = a[5, 3:5, 3:5] = True
    seen: list[tuple[int, int]] = []

    compare_masks(a, a, (1.0, 1.0, 1.0), progress_callback=lambda *p: seen.append(p))
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]

    seen.clear()
    interpolate_slices(a, 0, (2.0, 1.0, 1.0), progress_callback=lambda *p: seen.append(p))
    assert seen == [(0, 1), (1, 1)]
