"""Tests for qicom.about - the single source of QICOM's own metadata.

Most of these assert consistency between this module and the repository
files that repeat the same facts (pyproject.toml, CITATION.cff, LICENSE):
that duplication is unavoidable - a build backend cannot import the package
it is building - so it is checked instead.
"""

from __future__ import annotations

import re
import runpy
import tomllib
from pathlib import Path

from qicom import about
from qicom.__version__ import __version__
from qicom.build_identity import UNKNOWN_REVISION, BuildIdentity, build_identity

REPO_ROOT = Path(__file__).parents[1]
_GATE = runpy.run_path(str(REPO_ROOT / "packaging" / "check_release_consistency.py"))
is_development_version = _GATE["is_development_version"]


class TestIdentity:
    def test_version_matches_the_package_version(self) -> None:
        assert about.VERSION == __version__

    def test_version_matches_pyproject(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert pyproject["project"]["version"] == about.VERSION

    def test_license_matches_pyproject(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert pyproject["project"]["license"] == about.LICENSE_ID

    def test_author_and_support_email_match_pyproject(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        author = pyproject["project"]["authors"][0]
        assert author["name"] == about.AUTHOR
        assert author["email"] == about.SUPPORT_EMAIL

    def test_author_name_parts_compose_the_full_name(self) -> None:
        assert about.AUTHOR == f"{about.AUTHOR_GIVEN_NAME} {about.AUTHOR_FAMILY_NAME}"

    def test_qicom_is_an_independent_project_with_no_affiliation(self) -> None:
        assert about.AFFILIATION is None

    def test_designed_in_names_the_author_location(self) -> None:
        assert about.AUTHOR_LOCATION in about.DESIGNED_IN


class TestRepositoryFiles:
    def test_license_file_is_the_declared_license(self) -> None:
        text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        assert "Apache License" in text
        assert "Version 2.0" in text
        assert about.COPYRIGHT in text

    def test_notice_carries_the_dicom_attribution_verbatim(self) -> None:
        text = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
        assert about.DICOM_TRADEMARK_NOTICE in _unwrapped(text)
        assert about.NEMA_NON_AFFILIATION in _unwrapped(text)
        assert about.INTENDED_USE_SUMMARY in _unwrapped(text)

    def test_citation_cff_agrees_with_the_module(self) -> None:
        text = (REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8")
        assert f"version: {about.VERSION}" in text
        assert f"license: {about.LICENSE_ID}" in text
        assert about.AUTHOR_FAMILY_NAME in text
        assert about.REPOSITORY_URL in text

    def test_packaged_builds_ship_the_licence_files(self) -> None:
        """Apache-2.0 requires LICENSE and NOTICE to travel with every
        distributed copy, and the About dialog names them."""
        spec = (REPO_ROOT / "packaging" / "qicom.spec").read_text(encoding="utf-8")
        assert '"LICENSE"' in spec
        assert '"NOTICE"' in spec

    def test_macos_bundle_declares_the_same_copyright(self) -> None:
        spec = (REPO_ROOT / "packaging" / "qicom.spec").read_text(encoding="utf-8")
        assert about.COPYRIGHT in spec

    def test_changelog_documents_the_current_version(self) -> None:
        """A released version has its own section; a development version's
        work lives under [Unreleased] until the release commit moves it.
        packaging/check_release_consistency.py enforces the rest of that
        rule - in particular that a stable version may not leave changes to
        the numbers sitting unreleased."""
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        if is_development_version(about.VERSION):
            assert "## [Unreleased]" in text
        else:
            assert f"[{about.VERSION}]" in text
        # The section that must never be silently dropped: a scientific
        # tool has to say when the same inputs stop producing the same
        # numbers (see the changelog's own preamble).
        assert "Changes affecting numerical results" in text


class TestStatements:
    def test_dicom_trademark_notice_attributes_the_mark_to_nema(self) -> None:
        assert "DICOM®" in about.DICOM_TRADEMARK_NOTICE
        assert "National Electrical Manufacturers Association" in about.DICOM_TRADEMARK_NOTICE

    def test_non_affiliation_statement_names_nema_and_the_committee(self) -> None:
        assert "not affiliated" in about.NEMA_NON_AFFILIATION
        assert "NEMA" in about.NEMA_NON_AFFILIATION
        assert "DICOM Standards Committee" in about.NEMA_NON_AFFILIATION

    def test_intended_use_states_that_qicom_is_not_a_medical_device(self) -> None:
        assert "not a medical device" in about.INTENDED_USE_SUMMARY

    def test_no_registration_notice_names_the_metrics_it_concerns(self) -> None:
        assert "never registers" in about.NO_REGISTRATION_NOTICE
        for metric in ("MSE", "RMSE", "PSNR", "SSIM"):
            assert metric in about.NO_REGISTRATION_NOTICE

    def test_ibsi_status_does_not_claim_certification(self) -> None:
        assert "not IBSI" in about.IBSI_STATUS

    def test_privacy_summary_and_its_one_declared_exception_stay_together(self) -> None:
        """A privacy claim is only verifiable next to its exception - the
        learned-weights download is the one network access in QICOM (see
        qicom.metrics.learned_weights), and it must not disappear from the
        statement."""
        assert "no network connection during an analysis" in about.PRIVACY_SUMMARY
        assert "SHA-256" in about.PRIVACY_NETWORK_EXCEPTION


class TestBuildInfo:
    def test_build_info_reports_the_version_and_the_environment(self) -> None:
        info = about.build_info()
        assert info["version"] == about.VERSION
        assert re.fullmatch(r"\d+\.\d+\.\d+", info["python"])
        assert info["platform"]
        assert info["machine"]

    def test_build_kind_reflects_whether_the_app_is_frozen(self) -> None:
        expected = "packaged bundle" if about.is_frozen() else "source checkout"
        assert about.build_info()["build"] == expected

    def test_is_frozen_is_false_when_running_from_source(self) -> None:
        assert about.is_frozen() is False


class TestCitation:
    def test_citation_text_carries_author_year_version_and_url(self) -> None:
        citation = about.citation_text()
        assert about.AUTHOR_FAMILY_NAME in citation
        assert str(about.RELEASE_YEAR) in citation
        assert about.VERSION in citation
        assert about.REPOSITORY_URL in citation

    def test_bibtex_entry_is_balanced_and_protects_the_acronym(self) -> None:
        bibtex = about.citation_bibtex()
        assert bibtex.startswith("@software{qicom,")
        assert bibtex.rstrip().endswith("}")
        assert bibtex.count("{") == bibtex.count("}")
        # Without the braces a BibTeX style is free to lower-case the name.
        assert "{QICOM}" in bibtex

    def test_release_year_is_a_constant_not_the_current_clock(self) -> None:
        """Citations and copyright lines must read identically on every
        machine, including one whose date is wrong."""
        assert isinstance(about.RELEASE_YEAR, int)
        assert str(about.RELEASE_YEAR) in about.COPYRIGHT


def _unwrapped(text: str) -> str:
    """Collapse hard-wrapped file text so a sentence can be matched whole."""
    return re.sub(r"\s+", " ", text)


class TestBuildIdentityIsVisible:
    """A version alone does not identify the source that produced a result
    (see qicom.build_identity). Wherever QICOM states its version to a user
    or to a file, it states the revision too - About, bug reports, export
    headers and the receipt all read the same mapping."""

    def test_build_info_carries_the_revision_and_the_dirty_flag(self) -> None:
        identity = build_identity()
        info = about.build_info()

        assert info["revision"] == identity.short_revision
        expected_source = (
            "unknown"
            if identity.revision == UNKNOWN_REVISION
            else ("modified" if identity.source_dirty else "clean")
        )
        assert info["source"] == expected_source

    def test_an_unknown_revision_is_never_described_as_clean(self, monkeypatch) -> None:
        monkeypatch.setattr(
            about,
            "build_identity",
            lambda: BuildIdentity(
                version=about.VERSION,
                revision=UNKNOWN_REVISION,
                source_dirty=False,
            ),
        )

        assert about.build_info()["source"] == "unknown"

    def test_build_info_stays_a_flat_string_mapping(self) -> None:
        """Export headers depend on the shape, not just the contents."""
        assert all(isinstance(value, str) for value in about.build_info().values())


class TestReadmeAgreesWithTheModule:
    """The README states that qicom/about.py is the single source shared with
    pyproject.toml, CITATION.cff *and the README itself*, and that
    tests/test_about.py verifies the agreement. It did not check the README:
    the citation block there still said "Version 1.0.0" after the version was
    bumped, and nothing noticed."""

    @staticmethod
    def _readme() -> str:
        return (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_citation_block_carries_the_current_version(self) -> None:
        assert f"(Version {about.VERSION})" in self._readme()

    def test_the_citation_block_matches_what_the_application_shows(self) -> None:
        """Normalised for the dash: the README uses an em dash in prose where
        citation_text() uses a hyphen, and that difference is cosmetic."""
        rendered = about.citation_text().replace(" - ", " — ")
        readme = " ".join(self._readme().split())

        assert " ".join(rendered.split()) in readme

    def test_the_readme_names_the_declared_licence(self) -> None:
        assert about.LICENSE_ID in self._readme()

    def test_the_readme_does_not_claim_a_release_that_has_no_tag(self) -> None:
        """A retroactive v1.0.0 must be demonstrated, not asserted - see
        VERSIONING.md."""
        readme = self._readme()

        assert "v1.0.0" not in readme
