"""Documentation that states a number the code owns must state the right one.

Prose drifts silently: nothing fails when a document keeps describing the
schema, the version or the formula it described last year. Every assertion
here corresponds to a real drift found in this repository - a README citation
frozen at 1.0.0 after the version moved, a schemas reference still describing
receipt schema 2 after schema 3 shipped, a CI section naming one interpreter
after the matrix gained a second.

Only claims with a machine-checkable counterpart belong here. Prose that
explains *why* something is done is not testable and is not tested.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qicom import about
from qicom.provenance import PERSISTENCE_SCHEMA_VERSION
from qicom.radiomics.config import RadiomicsConfig
from qicom.session.evaluation_statistics import ORDINAL_ALPHA_FORMULA_VERSION

REPO_ROOT = Path(__file__).parents[1]


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


class TestSchemaReference:
    """OPERATIONS_SCHEMAS.md is what a reader consults to interpret a receipt
    they were handed."""

    def test_it_names_the_receipt_schema_that_ships(self) -> None:
        assert f"reproducibility receipt — schema {PERSISTENCE_SCHEMA_VERSION}" in _read(
            "OPERATIONS_SCHEMAS.md"
        )

    def test_it_lists_the_fields_the_current_schema_added(self) -> None:
        text = _read("OPERATIONS_SCHEMAS.md")

        assert "build_revision" in text
        assert "source_dirty" in text

    def test_it_says_which_older_schemas_still_load(self) -> None:
        text = _read("OPERATIONS_SCHEMAS.md")

        assert "schemas 1 and 2 remain readable" in text


class TestMethodologyDocuments:
    def test_the_qualitative_formula_version_matches_the_code(self) -> None:
        """The exported method label carries this number; a document naming a
        different one would describe results nobody can reproduce."""
        assert f"formula version (currently v{ORDINAL_ALPHA_FORMULA_VERSION})" in _read(
            "QUALITATIVE_METHODOLOGY.md"
        )

    def test_the_radiomics_receipt_description_mentions_the_build_revision(self) -> None:
        assert "build revision" in _read("RADIOMICS_METHODOLOGY.md")

    def test_the_default_radiomics_profile_id_is_the_one_documented(self) -> None:
        """The document names the profile by id; the version deliberately is
        not asserted here - RADIOMICS_METHODOLOGY.md describes the profile
        mechanism, not a particular version of it, and CHANGELOG.md is where a
        version bump is recorded."""
        assert RadiomicsConfig().profile_id in _read("RADIOMICS_METHODOLOGY.md")


class TestReadme:
    def test_the_ci_section_names_every_python_the_matrix_runs(self) -> None:
        workflow = _read(".github/workflows/ci.yml")
        readme = _read("README.md")

        for version in ("3.11", "3.12"):
            assert f'"{version}"' in workflow, f"the matrix lost {version}"
            assert version in readme, f"the README does not mention {version}"

    def test_the_readme_points_at_the_documents_that_exist(self) -> None:
        """Every repository-local link in the README must resolve - a dangling
        one is how a deleted document goes unnoticed."""
        import re

        readme = _read("README.md")
        missing = []
        for target in re.findall(r"\]\(([^)#:]+\.(?:md|txt|toml|cff))\)", readme):
            if not (REPO_ROOT / target).exists():
                missing.append(target)

        assert missing == [], missing

    @pytest.mark.parametrize(
        "document",
        ["VERSIONING.md", "packaging/DISTRIBUTION.md", "DEPRECATIONS.md"],
    )
    def test_the_readme_reaches_the_reference_documents(self, document: str) -> None:
        assert document in _read("README.md"), document


class TestNoDocumentClaimsAnUnprovenRelease:
    """A tag asserts *this commit is what was distributed*. There is no
    v1.0.0 tag, and no document may imply otherwise until the commit that was
    actually handed out is demonstrated (see VERSIONING.md)."""

    @pytest.mark.parametrize(
        "document",
        ["README.md", "SECURITY.md", "OPERATIONS_SCHEMAS.md", "packaging/DISTRIBUTION.md"],
    )
    def test_no_user_facing_document_names_a_v1_tag(self, document: str) -> None:
        assert "v1.0.0" not in _read(document)

    def test_the_citation_everywhere_is_the_current_version(self) -> None:
        assert f"(Version {about.VERSION})" in _read("README.md")
        assert f"version: {about.VERSION}" in _read("CITATION.cff")


class TestCitationFile:
    """CITATION.cff is read by citation tooling, so a wrong field there
    propagates into other people's bibliographies."""

    @staticmethod
    def _citation() -> dict:
        import yaml

        return yaml.safe_load((REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8"))

    def test_it_declares_the_current_version(self) -> None:
        assert self._citation()["version"] == about.VERSION

    def test_a_development_version_claims_no_release_date(self) -> None:
        """It carried 1.0.0's changelog date next to a 1.1.0.dev0 version -
        asserting a release of that version that never happened."""
        import runpy

        gate = runpy.run_path(str(REPO_ROOT / "packaging" / "check_release_consistency.py"))
        citation = self._citation()

        if gate["is_development_version"](about.VERSION):
            assert "date-released" not in citation
        else:
            assert citation.get("date-released"), "a released version needs its date"

    def test_it_declares_the_same_licence_as_everything_else(self) -> None:
        assert self._citation()["license"] == about.LICENSE_ID
