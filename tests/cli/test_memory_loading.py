"""CLI users can confirm a metadata warning without changing scientific outputs."""

from __future__ import annotations

import io
import json

import nibabel as nib
import numpy as np
import pandas as pd
import pytest

from qicom import runtime
from qicom.cli import main as cli


@pytest.fixture
def inputs(tmp_path):
    paths = [tmp_path / f"image-{i}.nii.gz" for i in range(2)]
    for index, path in enumerate(paths):
        pixels = np.random.default_rng(index).random((8, 8, 8)).astype(np.float32)
        nib.save(nib.Nifti1Image(pixels, np.eye(4)), path)
    return paths


@pytest.mark.parametrize("override", [False, True])
def test_noninteractive_cli_refuses_before_decode_or_accepts_explicit_override(
    tmp_path, monkeypatch, capsys, inputs, override
):
    original_metrics = cli.run_all_metrics
    monkeypatch.setattr(
        cli,
        "run_all_metrics",
        lambda *args, **kwargs: original_metrics(
            *args, **kwargs, selected_metric_ids=frozenset({"mse", "ssim"})
        ),
    )
    expected_path = tmp_path / "expected.csv"
    args = ["qicom-cli", "confronta", *map(str, inputs), "--output", str(expected_path)]
    monkeypatch.setattr("sys.argv", args)
    assert cli.main() == 0
    expected = pd.read_csv(expected_path)
    capsys.readouterr()

    decoded = []
    original_array = nib.arrayproxy.ArrayProxy.__array__

    def decode(self, *args, **kwargs):
        decoded.append(True)
        return original_array(self, *args, **kwargs)

    monkeypatch.setattr(nib.arrayproxy.ArrayProxy, "__array__", decode)
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    monkeypatch.setattr("sys.stdin", io.StringIO())
    output = tmp_path / "actual.csv"
    monkeypatch.setattr(
        "sys.argv", [*args[:-1], str(output), *(["--allow-large-images"] if override else [])]
    )
    assert cli.main() == (0 if override else 1)
    message = capsys.readouterr().err
    assert "lower bound, not a peak" in message
    assert "Continuing may still run out of memory" in message
    if override:
        assert len(decoded) == 2
        pd.testing.assert_frame_equal(pd.read_csv(output), expected, check_exact=True)
    else:
        assert not decoded
        assert not output.exists()
        assert "--allow-large-images" in message


@pytest.mark.parametrize(
    ("answer", "approved"), [("yes\nyes\n", True), ("n\n", False), ("", False)]
)
def test_interactive_cli_requires_an_affirmative_answer(monkeypatch, inputs, answer, approved):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Terminal(answer))
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    monkeypatch.setattr("sys.argv", ["qicom-cli", "confronta", *map(str, inputs)])
    original_metrics = cli.run_all_metrics
    monkeypatch.setattr(
        cli,
        "run_all_metrics",
        lambda *args, **kwargs: original_metrics(
            *args, **kwargs, selected_metric_ids=frozenset({"mse"})
        ),
    )
    assert cli.main() == (0 if approved else 1)


@pytest.mark.parametrize("override", [False, True])
def test_quantitative_batch_uses_the_same_memory_policy(
    tmp_path, monkeypatch, capsys, inputs, override
):
    configuration = tmp_path / "batch.json"
    output = tmp_path / "results"
    configuration.write_text(
        json.dumps(
            {
                "output_directory": str(output),
                "profile": "core",
                "jobs": [
                    {"id": "case-1", "reference": str(inputs[0]), "comparison": str(inputs[1])}
                ],
            }
        )
    )
    monkeypatch.setattr("sys.stdin", io.StringIO())
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    monkeypatch.setattr(
        "sys.argv",
        [
            "qicom-cli",
            "batch-quantitative",
            str(configuration),
            *(["--allow-large-images"] if override else []),
        ],
    )
    decoded = []
    original_array = nib.arrayproxy.ArrayProxy.__array__

    def decode(self, *args, **kwargs):
        decoded.append(True)
        return original_array(self, *args, **kwargs)

    monkeypatch.setattr(nib.arrayproxy.ArrayProxy, "__array__", decode)
    assert cli.main() == (0 if override else 1)
    assert "memory warning" in capsys.readouterr().err
    assert len(decoded) == (2 if override else 0)
    assert (output / "quantitative_results.csv").exists() is override
    log = pd.read_csv(output / "batch_log.csv")
    assert log.status.tolist() == ["completed" if override else "failed"]
