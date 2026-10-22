from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


def _run_generator(source: Path, output: Path) -> None:
    script = Path(__file__).parents[1] / "packaging" / "generate_sha256sums.py"
    subprocess.run(
        [sys.executable, str(script), str(source), "--output", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_directory_manifest_is_sorted_and_excludes_itself(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "z.txt").write_bytes(b"last")
    nested = bundle / "nested"
    nested.mkdir()
    (nested / "a.txt").write_bytes(b"first")
    manifest = bundle / "SHA256SUMS"

    _run_generator(bundle, manifest)
    first_result = manifest.read_text(encoding="utf-8")
    _run_generator(bundle, manifest)

    assert manifest.read_text(encoding="utf-8") == first_result
    assert first_result.splitlines() == [
        f"{hashlib.sha256(b'first').hexdigest()}  nested/a.txt",
        f"{hashlib.sha256(b'last').hexdigest()}  z.txt",
    ]


def test_single_file_manifest_uses_the_filename(tmp_path: Path) -> None:
    archive = tmp_path / "QICOM-windows-x64.zip"
    archive.write_bytes(b"archive contents")
    manifest = tmp_path / "QICOM-windows-x64.zip.sha256"

    _run_generator(archive, manifest)

    assert manifest.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(b'archive contents').hexdigest()}  {archive.name}\n"
    )


def test_generator_refuses_to_overwrite_its_source(tmp_path: Path) -> None:
    archive = tmp_path / "QICOM.zip"
    archive.write_bytes(b"keep me")

    script = Path(__file__).parents[1] / "packaging" / "generate_sha256sums.py"
    result = subprocess.run(
        [sys.executable, str(script), str(archive), "--output", str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "source and output must be different" in result.stderr
    assert archive.read_bytes() == b"keep me"
