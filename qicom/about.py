"""Single source of truth for everything QICOM says about itself.

The About dialog, the export headers, CITATION.cff, the README and docs/ all
describe the same software. Keeping its name, version, authorship, licence,
use scope and the DICOM trademark wording in one importable, Qt-free module
is what stops those copies from drifting apart - a version string that is
right in the About box and stale in an exported workbook is worse than no
version string at all.

Two kinds of text live here, deliberately:

* Wording that must NOT be translated: the trademark attribution, the
  non-affiliation statement and the citation block. A trademark notice is
  reproduced verbatim in the rights holder's own wording, and a citation has
  to read identically whatever language the user happens to have selected.
* One-sentence English summaries (INTENDED_USE_SUMMARY, PRIVACY_SUMMARY),
  for the consumers that have no translator at hand: exported files, the
  README, generated documentation. The GUI's fuller, translated paragraphs
  live in the locale files under the `about.*` keys - the locale files, not
  this module, are what AboutDialog reads.

Imports only Qt-free QICOM identity modules, and no Qt: the reporting layer
must be able to stamp a workbook with this metadata in a worker thread, with
no GUI in the process at all.
"""

from __future__ import annotations

import platform
import sys

from qicom.__version__ import __version__
from qicom.build_identity import UNKNOWN_REVISION, build_identity

# -- Identity ----------------------------------------------------------

APP_NAME = "QICOM"
APP_FULL_NAME = "Quality Image COMparator"
APP_TITLE = f"{APP_NAME} - {APP_FULL_NAME}"
VERSION = __version__

# Fixed, not derived from the clock: a citation and a copyright line must
# read the same today and in five years, on any machine, including one
# whose date is wrong.
RELEASE_YEAR = 2026

AUTHOR = "Fabio Mattiussi"
AUTHOR_GIVEN_NAME = "Fabio"
AUTHOR_FAMILY_NAME = "Mattiussi"
AUTHOR_CITY = "Lugano"
AUTHOR_COUNTRY = "Switzerland"
AUTHOR_LOCATION = f"{AUTHOR_CITY}, {AUTHOR_COUNTRY}"
DESIGNED_IN = f"Designed and developed in {AUTHOR_LOCATION}."

# QICOM is an independent project with no institutional affiliation. Kept
# as an explicit None rather than an empty string so CITATION.cff, the
# About dialog and any future affiliation line all fail loudly if someone
# assumes an institution exists.
AFFILIATION: str | None = None

SUPPORT_EMAIL = "support.qicom@gmail.com"
REPOSITORY_URL = "https://github.com/Metallogik/QICOM"

# -- Licensing ---------------------------------------------------------

LICENSE_ID = "Apache-2.0"
LICENSE_NAME = "Apache License, Version 2.0"
LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0"
COPYRIGHT = f"Copyright {RELEASE_YEAR} {AUTHOR}"
# Named by file, not by path: it sits next to NOTICE in a desktop bundle and in
# packaging/ in the source tree, and a source-tree path pointed at nothing there.
THIRD_PARTY_LICENSES_FILE = "THIRD_PARTY_LICENSES.txt"

# -- DICOM attribution -------------------------------------------------
#
# Reproduced in NEMA's own wording and never translated. The three
# statements are separate on purpose: supporting a format, attributing a
# trademark, and claiming conformance to a standard are three different
# claims, and only the first two are ones QICOM makes.

DICOM_SUPPORT_STATEMENT = "QICOM supports DICOM® medical images."

DICOM_TRADEMARK_NOTICE = (
    "DICOM® is the registered trademark of the National Electrical "
    "Manufacturers Association (NEMA) for its standards publications relating "
    "to digital communications of medical information."
)

NEMA_NON_AFFILIATION = (
    "QICOM is an independent software project and is not affiliated with, "
    "sponsored by, or endorsed by NEMA or the DICOM Standards Committee."
)

NO_DICOM_CONFORMANCE_CLAIM = (
    "QICOM reads DICOM files; it implements no DICOM network or storage "
    "service, claims no conformance to the DICOM Standard, and publishes no "
    "DICOM Conformance Statement."
)

# -- Use scope ---------------------------------------------------------

INTENDED_USE_SUMMARY = (
    "QICOM is intended for research and quality-assessment use only. It is not "
    "a medical device, and it is not intended for clinical diagnosis or for any "
    "decision concerning the care of a patient."
)

# Named separately from the disclaimer because it is a technical limitation,
# not a legal one: qicom.io.consistency describes each mismatch it can see,
# but nothing in QICOM registers two images onto each other.
NO_REGISTRATION_NOTICE = (
    "QICOM compares images element by element and never registers them. Every "
    "reference-based metric (MSE, MAE, RMSE, PSNR, SSIM and the others) "
    "therefore assumes that the two inputs already correspond spatially; QICOM "
    "reports the mismatches it can detect from the DICOM headers, but it "
    "cannot detect a misalignment the headers do not describe."
)

IBSI_STATUS = (
    "The radiomics module targets the IBSI reference definitions and applies "
    "the IBSI convention where PyRadiomics differs from it, but it is not IBSI "
    "certified: IBSI operates no certification scheme. See the validation "
    "documentation for the benchmark results feature by feature."
)

# -- Privacy -----------------------------------------------------------

PRIVACY_SUMMARY = (
    "QICOM performs all image processing locally on the machine it runs on. It "
    "transmits no image data, embeds no telemetry and makes no network "
    "connection during an analysis."
)

# Stated next to the claim rather than omitted from it: a privacy statement
# with one declared exception is verifiable, an absolute one is not.
PRIVACY_NETWORK_EXCEPTION = (
    "QICOM opens exactly one kind of network connection, and only when the "
    "user explicitly starts it: the download of the optional learned-metric "
    "checkpoints, each verified against a known SHA-256. No analysis ever "
    "triggers it."
)


def build_info() -> dict[str, str]:
    """Runtime facts worth quoting in a bug report or an export header.

    Deliberately a plain str->str mapping, in display order: it is rendered
    as-is into the About dialog and (from the reproducibility batch on)
    written into export headers, where a stable, flat shape is what keeps
    two files comparable.

    `revision`/`source` come from qicom.build_identity: the version names a
    release, the revision names the code. A bug report quoting only the
    former cannot be traced back to what actually ran, which is the same gap
    the reproducibility receipt closes for results.
    """
    identity = build_identity()
    source = (
        "unknown"
        if identity.revision == UNKNOWN_REVISION
        else ("modified" if identity.source_dirty else "clean")
    )
    return {
        "version": VERSION,
        "revision": identity.short_revision,
        "source": source,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine() or "unknown",
        "build": "packaged bundle" if is_frozen() else "source checkout",
    }


def is_frozen() -> bool:
    """True inside a PyInstaller bundle (see packaging/qicom.spec)."""
    return bool(getattr(sys, "frozen", False))


def citation_text() -> str:
    """The plain-text citation, in the order a reference list expects it."""
    return (
        f"{AUTHOR_FAMILY_NAME}, {AUTHOR_GIVEN_NAME[0]}. ({RELEASE_YEAR}). "
        f"{APP_TITLE} (Version {VERSION}) [Computer software]. {REPOSITORY_URL}"
    )


def citation_bibtex() -> str:
    """The same citation as a BibTeX @software entry.

    Braces around QICOM and COM keep BibTeX styles from lower-casing them,
    which is exactly the kind of silent damage an acronym-based name invites.
    """
    return (
        "@software{qicom,\n"
        f"  author  = {{{AUTHOR_FAMILY_NAME}, {AUTHOR_GIVEN_NAME}}},\n"
        "  title   = {{QICOM} --- Quality Image {COM}parator},\n"
        f"  version = {{{VERSION}}},\n"
        f"  year    = {{{RELEASE_YEAR}}},\n"
        f"  url     = {{{REPOSITORY_URL}}},\n"
        f"  license = {{{LICENSE_ID}}}\n"
        "}"
    )
