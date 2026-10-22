"""Tests for packaging/attribution.py - what a bundle really redistributes.

The hand-maintained inventory it replaces got three things wrong at once: it
called QICOM proprietary (it is Apache-2.0), it declared the `learned` extra
out of scope while the release tags built exactly that, and nothing compared
it against a bundle. These tests pin the mechanism that makes each of those
states detectable rather than plausible.

The module is loaded by path rather than imported: `packaging` is also a
third-party distribution name, and shadowing it inside the test process would
be a worse problem than the convenience is worth.
"""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
ATTRIBUTION_PATH = REPO_ROOT / "packaging" / "attribution.py"
attribution = runpy.run_path(str(ATTRIBUTION_PATH))

DistributionRecord = attribution["DistributionRecord"]
Inventory = attribution["Inventory"]
PROFILE_EXTRAS = attribution["PROFILE_EXTRAS"]
BUNDLE_BUILD_RUNTIME_DISTRIBUTIONS = attribution["BUNDLE_BUILD_RUNTIME_DISTRIBUTIONS"]
build_inventory = attribution["build_inventory"]
check_bundle = attribution["check_bundle"]
load_overrides = attribution["load_overrides"]
missing_notice_texts = attribution["missing_notice_texts"]
attribution_main = attribution["main"]
render_sbom = attribution["render_sbom"]
resolve_closure = attribution["resolve_closure"]


def _record(name: str, **overrides: object) -> object:
    defaults = {
        "name": name,
        "version": "1.0",
        "license": "MIT",
        "origin": "https://example.invalid",
        "top_level": (name,),
        "notice_files": ("LICENSE",),
    }
    defaults.update(overrides)
    return DistributionRecord(**defaults)


class TestQicomDeclaresApache:
    def test_the_reference_licence_file_no_longer_calls_qicom_proprietary(self) -> None:
        text = (REPO_ROOT / "packaging" / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")
        header = text.split("REFERENCE COPIES OF THE LICENSE TEXTS FOLLOW", 1)[0]

        assert "Apache License, Version 2.0" in header
        # The word survives only inside the reproduced GPL/LGPL texts, which
        # are below the header and are not QICOM talking about itself.
        assert "QICOM itself (this application's own source code) is proprietary" not in text

    def test_the_sbom_declares_apache_for_qicom_itself(self) -> None:
        sbom = render_sbom(Inventory(profile="full", distributions=[]))

        component = sbom["metadata"]["component"]
        assert component["name"] == "QICOM"
        assert component["licenses"] == [{"license": {"id": "Apache-2.0"}}]

    def test_every_profile_the_workflow_can_build_is_known(self) -> None:
        """A tag build is full+learned; a profile the generator does not know
        cannot be inventoried, and so must not be buildable."""
        assert {"base", "full", "base+learned", "full+learned"} <= set(PROFILE_EXTRAS)
        assert "learned" in PROFILE_EXTRAS["full+learned"]


class TestClosure:
    def test_a_profile_resolves_the_extras_it_is_built_with(self) -> None:
        names = set(resolve_closure("full"))

        assert {"numpy", "pandas", "pydicom", "pyside6-essentials"} <= names
        assert {"simpleitk", "pyradiomics", "pywavelets"} <= names

    def test_the_base_profile_leaves_radiomics_out(self) -> None:
        assert "pyradiomics" not in set(resolve_closure("base"))
        assert "simpleitk" not in set(resolve_closure("base"))

    def test_development_tooling_never_enters_a_distributed_profile(self) -> None:
        """pytest, black, ruff, mypy and pip-audit are installed in this very
        environment and must not appear in an inventory of what is shipped."""
        names = set(resolve_closure("full"))

        assert names.isdisjoint({"pytest", "black", "ruff", "mypy", "pip-audit", "pyinstaller"})

    def test_qicom_itself_is_not_a_third_party_distribution(self) -> None:
        assert "qicom" not in set(resolve_closure("full"))

    def test_an_unknown_profile_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown profile"):
            resolve_closure("full+quantum")


class TestInventoryCompleteness:
    def test_the_full_profile_is_complete_in_this_environment(self) -> None:
        inventory = build_inventory("full")

        incomplete = {
            record.name: record.missing_fields()
            for record in inventory.distributions
            if record.missing_fields()
        }
        assert incomplete == {}
        assert missing_notice_texts(inventory) == []

    def test_a_distribution_without_a_licence_text_is_incomplete(self) -> None:
        assert _record("x", notice_files=()).missing_fields() == ("notice",)

    def test_a_distribution_without_an_origin_is_incomplete(self) -> None:
        assert _record("x", origin="").missing_fields() == ("origin",)

    def test_a_distribution_without_a_version_or_licence_is_incomplete(self) -> None:
        assert _record("x", version="", license="").missing_fields() == ("version", "license")

    def test_reviewed_overrides_supply_what_a_wheel_does_not(self) -> None:
        """PySide6's wheels ship no licence file at all - the LGPLv3 text
        QICOM relies on has to come from a reviewed, checked-in copy."""
        overrides = load_overrides()

        assert "pyside6-essentials" in overrides
        assert "shiboken6" in overrides
        assert "python-gdcm" in overrides
        for entry in overrides.values():
            assert entry["reason"], "an override must say why the metadata is insufficient"
            assert entry["notice_files"]

    def test_the_gdcm_notice_carries_what_the_bundled_libraries_require(self) -> None:
        """python-gdcm's wheel has only its own Apache-2.0 text; GDCM's licence,
        and the IJG acknowledgement libjpeg requires of an executable-only copy,
        have to come from the reviewed notice."""
        (notice,) = load_overrides()["python-gdcm"]["notice_files"]
        text = (REPO_ROOT / "packaging" / "attribution" / notice).read_text(encoding="utf-8")

        assert "Copyright (c) 2006-2016 Mathieu Malaterre" in text
        assert "based in part\non the work of the Independent JPEG Group" in text
        for component in ("CharLS", "OpenJPEG", "Expat", "zlib", "socket++", "OpenSSL"):
            assert component in text, component

    def test_an_override_is_marked_as_a_human_judgement_in_the_inventory(self) -> None:
        inventory = build_inventory("full")
        pyside = next(
            record for record in inventory.distributions if record.name == "PySide6_Essentials"
        )

        assert pyside.attribution_source == "reviewed-override"
        assert pyside.license == "LGPL-3.0-only"

    def test_an_override_pointing_at_a_missing_text_is_reported(self, tmp_path: Path) -> None:
        inventory = Inventory(
            profile="full",
            distributions=[
                _record(
                    "ghost",
                    notice_files=("notices/absent.txt",),
                    attribution_source="reviewed-override",
                ),
            ],
        )

        assert missing_notice_texts(inventory, notices_root=tmp_path) == [
            "ghost: referenced notice 'notices/absent.txt' does not exist"
        ]

    def test_the_redistributed_build_runtime_is_added_without_its_tooling_closure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # pytest is available in this test environment and, like PyInstaller,
        # is not a base runtime dependency. Substituting it exercises the
        # explicit one-distribution path without requiring PyInstaller here.
        monkeypatch.setitem(
            build_inventory.__globals__,
            "BUNDLE_BUILD_RUNTIME_DISTRIBUTIONS",
            ("pytest",),
        )

        inventory = build_inventory("base", include_build_runtime=True)
        names = {record.name.casefold() for record in inventory.distributions}

        assert "pytest" in names
        assert "pluggy" not in names, "build-tool dependencies are not automatically bundled"

    def test_a_missing_redistributed_build_runtime_fails_generation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            build_inventory.__globals__,
            "BUNDLE_BUILD_RUNTIME_DISTRIBUTIONS",
            ("qicom-definitely-missing-build-runtime",),
        )

        with pytest.raises(RuntimeError, match="packaging extra"):
            build_inventory("base", include_build_runtime=True)


class TestBundleComparison:
    @staticmethod
    def _inventory() -> object:
        return Inventory(
            profile="full",
            distributions=[_record("numpy"), _record("pandas")],
        )

    def test_a_bundle_matching_its_inventory_passes(self) -> None:
        modules = {"profile": "full", "top_level": ["numpy", "pandas", "qicom"]}

        assert check_bundle(self._inventory(), modules) == []

    def test_a_bundled_distribution_absent_from_the_inventory_fails(self) -> None:
        """The finding this exists for: a build that quietly picks up torch
        while shipping an inventory that says it does not."""
        modules = {"profile": "full", "top_level": ["numpy", "pandas", "torch", "lpips"]}

        problems = check_bundle(self._inventory(), modules)

        assert len(problems) == 1
        assert "lpips, torch" in problems[0]

    def test_an_inventory_for_another_profile_fails(self) -> None:
        modules = {"profile": "full+learned", "top_level": ["numpy", "pandas"]}

        problems = check_bundle(self._inventory(), modules)

        assert any("built as 'full+learned'" in problem for problem in problems)

    def test_an_incomplete_inventory_entry_fails_the_bundle_too(self) -> None:
        inventory = Inventory(profile="full", distributions=[_record("numpy", license="")])
        modules = {"profile": "full", "top_level": ["numpy"]}

        problems = check_bundle(inventory, modules)

        assert any("has no license" in problem for problem in problems)

    def test_a_declared_redistributed_build_runtime_must_be_inventoried(self) -> None:
        modules = {
            "profile": "full",
            "top_level": ["numpy", "pandas", "pyimod01_archive"],
            "build_runtime": ["pyinstaller"],
        }

        problems = check_bundle(self._inventory(), modules)

        assert any("build runtime" in problem and "pyinstaller" in problem for problem in problems)

    def test_standard_library_modules_are_not_treated_as_distributions(self) -> None:
        modules = {
            "profile": "full",
            "top_level": ["argparse", "json", "pathlib", "sitecustomize", "numpy", "pandas"],
        }

        assert check_bundle(self._inventory(), modules) == []


class TestCommandLine:
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ATTRIBUTION_PATH), *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def _run_generate(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        # This test venv intentionally lacks the packaging extra. Production
        # generation includes PyInstaller by default; these application-only
        # fixtures exercise rendering/checking without claiming to describe a
        # distributable frozen bundle.
        return self._run("generate", "--no-include-build-runtime", *arguments)

    def test_generation_includes_the_build_runtime_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        included: list[bool] = []

        def fake_inventory(
            profile: str,
            overrides: dict[str, dict] | None = None,
            *,
            include_build_runtime: bool = False,
        ) -> object:
            del overrides
            included.append(include_build_runtime)
            return Inventory(profile=profile, distributions=[])

        monkeypatch.setitem(attribution_main.__globals__, "build_inventory", fake_inventory)

        assert (
            attribution_main(["generate", "--profile", "base", "--output-dir", str(tmp_path)]) == 0
        )
        assert included == [True]

    def test_generate_writes_all_three_artifacts(self, tmp_path: Path) -> None:
        result = self._run_generate("--profile", "full", "--output-dir", str(tmp_path))

        assert result.returncode == 0, result.stderr
        inventory = json.loads((tmp_path / "THIRD_PARTY_INVENTORY.json").read_text("utf-8"))
        sbom = json.loads((tmp_path / "qicom-sbom.cdx.json").read_text("utf-8"))
        licences = (tmp_path / "THIRD_PARTY_LICENSES.txt").read_text("utf-8")

        assert inventory["profile"] == "full"
        assert sbom["bomFormat"] == "CycloneDX"
        assert len(sbom["components"]) == len(inventory["distributions"])
        assert "Apache License, Version 2.0" in licences

    def test_generation_is_reproducible(self, tmp_path: Path) -> None:
        """An SBOM published next to a checksum has to be re-derivable; a
        timestamp or a random serial would make every run differ."""
        first, second = tmp_path / "a", tmp_path / "b"
        assert self._run_generate("--profile", "full", "--output-dir", str(first)).returncode == 0
        assert self._run_generate("--profile", "full", "--output-dir", str(second)).returncode == 0

        for name in (
            "THIRD_PARTY_INVENTORY.json",
            "qicom-sbom.cdx.json",
            "THIRD_PARTY_LICENSES.txt",
        ):
            assert (first / name).read_bytes() == (second / name).read_bytes(), name

    def test_the_generated_licences_reproduce_the_reviewed_texts(self, tmp_path: Path) -> None:
        assert (
            self._run_generate("--profile", "full", "--output-dir", str(tmp_path)).returncode == 0
        )

        licences = (tmp_path / "THIRD_PARTY_LICENSES.txt").read_text("utf-8")

        assert "GNU LESSER GENERAL PUBLIC LICENSE" in licences
        assert "GNU GENERAL PUBLIC LICENSE" in licences

    def test_the_generated_licences_reproduce_installed_wheel_texts(self, tmp_path: Path) -> None:
        assert (
            self._run_generate("--profile", "base", "--output-dir", str(tmp_path)).returncode == 0
        )

        licences = (tmp_path / "THIRD_PARTY_LICENSES.txt").read_text("utf-8")

        assert "packaging " in licences
        assert "Apache License" in licences
        assert "packaging-" in licences

    def test_check_bundle_fails_on_an_unmapped_distribution(self, tmp_path: Path) -> None:
        assert (
            self._run_generate("--profile", "full", "--output-dir", str(tmp_path)).returncode == 0
        )
        modules = tmp_path / "bundle-modules.json"
        modules.write_text(
            json.dumps({"schema_version": 1, "profile": "full", "top_level": ["torch"]}),
            encoding="utf-8",
        )

        result = self._run(
            "check-bundle",
            "--inventory",
            str(tmp_path / "THIRD_PARTY_INVENTORY.json"),
            "--bundle-modules",
            str(modules),
        )

        assert result.returncode == 1
        assert "torch" in result.stderr

    def test_check_bundle_passes_for_the_distributions_it_inventoried(self, tmp_path: Path) -> None:
        assert (
            self._run_generate("--profile", "full", "--output-dir", str(tmp_path)).returncode == 0
        )
        inventory = json.loads((tmp_path / "THIRD_PARTY_INVENTORY.json").read_text("utf-8"))
        top_level = sorted(
            {name for record in inventory["distributions"] for name in record["top_level"]}
            | {"qicom"}
        )
        modules = tmp_path / "bundle-modules.json"
        modules.write_text(
            json.dumps({"schema_version": 1, "profile": "full", "top_level": top_level}),
            encoding="utf-8",
        )

        result = self._run(
            "check-bundle",
            "--inventory",
            str(tmp_path / "THIRD_PARTY_INVENTORY.json"),
            "--bundle-modules",
            str(modules),
        )

        assert result.returncode == 0, result.stderr


class TestWorkflowWiring:
    """The gate only closes the finding if the build actually runs it."""

    def test_the_packaging_workflow_generates_and_checks_before_uploading(self) -> None:
        """Ordering within the `package` job: the IBSI release gate above it
        uploads its own receipt, so the bundle upload has to be located by the
        artifact it actually publishes."""
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")
        generate_at = workflow.index("attribution.py generate")
        check_at = workflow.index("attribution.py check-bundle")
        upload_at = workflow.index("${{ matrix.artifact }}.*")

        assert generate_at < check_at < upload_at

    def test_the_inventory_and_sbom_are_published_with_the_checksums(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")
        staged = workflow.index("THIRD_PARTY_INVENTORY.json")
        checksums = workflow.index("generate_sha256sums.py")

        assert staged < checksums, "attribution must be inside the checksum manifest"
        assert "qicom-sbom.cdx.json" in workflow

    def test_the_learned_profile_is_selected_when_learned_metrics_are_bundled(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")

        assert 'profile="${profile}+learned"' in workflow

    def test_the_spec_records_what_it_collected(self) -> None:
        spec = (REPO_ROOT / "packaging" / "qicom.spec").read_text("utf-8")
        collector = spec.split("def _collected_top_levels", 1)[1].split("# Recorded", 1)[0]

        assert "_collected_top_levels" in spec
        assert "bundle-modules-" in spec
        assert "list(a.datas)" not in collector
        assert 'entry[2] == "EXTENSION"' in collector
        assert "sys.stdlib_module_names" in collector
        assert '"build_runtime": ["pyinstaller"]' in spec

    def test_the_profile_specific_generated_licence_file_is_bundled(self) -> None:
        spec = (REPO_ROOT / "packaging" / "qicom.spec").read_text("utf-8")

        assert "QICOM_ATTRIBUTION_PROFILE" in spec
        assert "_generated_licence" in spec


class TestLearnedWeightPolicyIsConsistent:
    """README, spec, workflow and behaviour used to disagree: the README said
    checkpoints were embedded in learned bundles, the spec said and
    implemented the opposite, and CI pre-downloaded them into the runner's
    cache so the frozen smoke never met the state a user's first run does."""

    def test_the_readme_no_longer_claims_bundled_checkpoints(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text("utf-8")

        assert "Nei bundle learned i checkpoint verificati vengono incorporati" not in readme
        assert "I checkpoint non sono inclusi in nessun bundle" in readme

    def test_the_spec_states_it_does_not_require_the_checkpoints(self) -> None:
        spec = (REPO_ROOT / "packaging" / "qicom.spec").read_text("utf-8")

        assert "does NOT require the checkpoints to be present" in spec

    def test_the_packaging_workflow_no_longer_pre_fetches_them(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")

        assert "Pre-fetch and verify learned-metric checkpoints" not in workflow
        assert "No checkpoint pre-fetch here, deliberately" in workflow

    def test_the_frozen_smoke_runs_against_an_empty_cache(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")

        assert 'TORCH_HOME="$PWD/smoke-home/.cache/torch"' in workflow
        assert "unavailable-offline" in workflow

    def test_the_workflow_also_proves_the_explicit_preparation_path(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")
        offline_at = workflow.index("unavailable-offline")
        prepared_at = workflow.index("Verify the explicit checkpoint preparation path")

        assert offline_at < prepared_at
        assert "prepare_learned_weights.py" in workflow[prepared_at:]

    def test_the_ci_job_still_prepares_weights_for_the_learned_tests(self) -> None:
        """Removing the packaging prefetch must not disable the learned
        metric tests, which need real weights to run at all."""
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")

        assert "prepare_learned_weights.py" in ci


class TestDependencyProvenance:
    """A version string does not identify a distribution installed from a
    repository: PyRadiomics reports "3.0.1a1", which matches no PyPI release,
    and pip-audit skips it for exactly that reason."""

    def test_a_vcs_install_records_its_repository_and_commit(self) -> None:
        inventory = build_inventory("full")
        pyradiomics = next(
            record for record in inventory.distributions if record.name == "pyradiomics"
        )

        assert pyradiomics.vcs_url.endswith("pyradiomics.git")
        assert len(pyradiomics.vcs_commit) == 40

    def test_the_recorded_commit_is_the_one_pyproject_pins(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
        inventory = build_inventory("full")
        pyradiomics = next(
            record for record in inventory.distributions if record.name == "pyradiomics"
        )

        assert f"@{pyradiomics.vcs_commit}" in pyproject

    def test_the_purl_names_the_commit_not_the_meaningless_version(self) -> None:
        inventory = build_inventory("full")
        pyradiomics = next(
            record for record in inventory.distributions if record.name == "pyradiomics"
        )

        assert pyradiomics.purl.startswith("pkg:github/AIM-Harvard/pyradiomics@")
        assert pyradiomics.version not in pyradiomics.purl

    def test_a_pypi_install_keeps_its_pypi_purl(self) -> None:
        inventory = build_inventory("full")
        numpy = next(record for record in inventory.distributions if record.name == "numpy")

        assert numpy.purl.startswith("pkg:pypi/numpy@")
        assert numpy.vcs_commit == ""

    def test_the_sbom_carries_the_vcs_reference(self) -> None:
        sbom = render_sbom(build_inventory("full"))
        component = next(item for item in sbom["components"] if item["name"] == "pyradiomics")

        references = {reference["type"] for reference in component["externalReferences"]}
        assert "vcs" in references


class TestInstallerBootstrap:
    """pip 24.0 carried seven known vulnerabilities at the audit. The
    installer fetches and executes build backends, so pinning what it installs
    does not help if it is itself the problem."""

    def test_the_manifest_uses_the_shared_installer_minimum(self) -> None:
        generator = runpy.run_path(str(REPO_ROOT / "packaging" / "generate_constraints.py"))
        bootstrap = runpy.run_path(str(REPO_ROOT / "packaging" / "bootstrap_environment.py"))

        assert generator["MINIMUM_PIP"] == bootstrap["MINIMUM_PIP"] == "26.2"

    def test_the_constraints_generator_knows_every_distributed_profile(self) -> None:
        generator = runpy.run_path(str(REPO_ROOT / "packaging" / "generate_constraints.py"))

        assert set(generator["PROFILE_EXTRAS"]) == set(PROFILE_EXTRAS)


class TestInstalledEnvironmentManifest:
    """The bundle evidence must describe the environment that was frozen.

    A second ``pip download`` resolution can silently choose another index or
    lose a local version tag, so it is not evidence about the actual bundle.
    """

    @staticmethod
    def _generator() -> dict[str, object]:
        return runpy.run_path(str(REPO_ROOT / "packaging" / "generate_constraints.py"))

    def test_registry_versions_keep_local_build_tags(self) -> None:
        constraint_line = self._generator()["_constraint_line"]

        assert constraint_line(_record("torch", version="2.9.0+cpu")) == "torch==2.9.0+cpu"

    def test_vcs_dependencies_keep_the_repository_and_commit(self) -> None:
        constraint_line = self._generator()["_constraint_line"]
        record = _record(
            "pyradiomics",
            version="3.0.1a1",
            vcs_url="https://github.com/AIM-Harvard/pyradiomics.git",
            vcs_commit="a" * 40,
        )

        assert constraint_line(record) == (
            "pyradiomics @ " "git+https://github.com/AIM-Harvard/pyradiomics.git@" + "a" * 40
        )

    def test_manifest_is_explicitly_not_an_artifact_hash_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        generator = self._generator()
        writer = generator["write_environment_manifest"]
        monkeypatch.setitem(
            writer.__globals__,
            "_installed_inventory",
            lambda _profile: [_record("numpy", version="2.5.2")],
        )
        text_path, json_path = writer("base", tmp_path)

        text_manifest = text_path.read_text(encoding="utf-8")
        machine_manifest = json.loads(json_path.read_text(encoding="utf-8"))
        assert "not an artifact-hash lock" in text_manifest
        assert "--hash=" not in text_manifest
        assert machine_manifest["kind"] == "installed-environment-manifest"
        assert machine_manifest["distributions"]


class TestWorkflowFailureSemantics:
    def test_a_cancelled_ibsi_gate_cannot_release_a_bundle(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")

        assert "needs.ibsi-gate.result != 'failure'" not in workflow
        assert "needs.ibsi-gate.result == 'success'" in workflow

    def test_headless_windows_smoke_writes_json_to_a_file(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")

        assert "--smoke-output smoke.json" in workflow
        assert "tee smoke.json" not in workflow

    def test_optional_ibsi_receipt_is_only_uploaded_when_it_exists(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")

        assert "hashFiles('build/ibsi-ci/**') != ''" in workflow

    def test_release_checkout_preserves_annotated_tag_objects(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")

        assert "fetch-depth: 0" in workflow
        assert '--require-tag --expected-tag "$GITHUB_REF_NAME"' in workflow

    def test_packaging_inventory_includes_the_redistributed_build_runtime(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")

        assert "--include-build-runtime" in workflow
        assert BUNDLE_BUILD_RUNTIME_DISTRIBUTIONS == ("pyinstaller",)
