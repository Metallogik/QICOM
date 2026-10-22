"""Fail-closed DICOM de-identification for exported QICOM packages.

The DICOM PS3.15 Basic Application Level Confidentiality Profile is too
large and frequently updated to reproduce safely with a hand-maintained tag
list.  QICOM therefore delegates the attribute rules to ``dicom-anonymizer``
and adds the application-level controls the profile itself cannot provide:

* the library applies its rules to the top-level data set only, so QICOM
  applies the same rules to every item of every nested sequence (functional
  groups, derivation and source-image references, ...), and then replaces any
  instance UID, date, time or person name still left anywhere in the tree;
* private attributes are always removed;
* UID replacements are consistent inside one package but never reused across
  independently exported packages;
* images whose pixels may contain burned-in identity are rejected, and so are
  DICOM directories, whose records can name patients in file paths;
* the Part 10 container is rebuilt rather than inherited: the 128-byte
  preamble is zeroed and the File Meta group keeps only the elements PS3.10
  requires (anything else there, private information included, travels
  outside the data set the profile reaches);
* the copy is verified for leftover identity values, instance UIDs, dates
  of the original and container data, then replaced atomically; and
* ``PatientIdentityRemoved=YES`` is written only after every check succeeds.

A refusal never exports anything by itself.  Package writers collect every
refused file (``refused``) and stop before committing, so the user is told
once which images cannot be de-identified and that the export can still
proceed without de-identification for them.  Only that explicit choice
passes an ``unverified`` list: each refused file is then de-identified on a
best-effort basis (:func:`_best_effort_deidentify`), marked
``PatientIdentityRemoved=NO`` and reported back, so the decision is visible
in the file, to the user and to whoever receives the package.

Refusal messages name attributes, never the identifying values they held:
they reach the error log and, from there, error reports sent outside.

This protects the copy being exported.  The source DICOM is never modified.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from functools import cache
from pathlib import Path

import pydicom
from dicomanonymizer import anonymize_dataset
from dicomanonymizer import dictionary as _library_uid_dictionary
from dicomanonymizer.simpledicomanonymizer import delete as _profile_delete
from dicomanonymizer.simpledicomanonymizer import get_UID as _profile_replacement_uid
from dicomanonymizer.simpledicomanonymizer import initialize_actions as _profile_rules
from dicomanonymizer.simpledicomanonymizer import keep as _profile_keep
from dicomanonymizer.simpledicomanonymizer import (
    replace_date_time_element as _replace_date_time,
)
from pydicom.dataelem import DataElement
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.errors import InvalidDicomError
from pydicom.multival import MultiValue
from pydicom.tag import BaseTag, Tag
from pydicom.uid import (
    UID,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    MediaStorageDirectoryStorage,
    SecondaryCaptureImageStorage,
)
from pydicom.valuerep import PersonName

from qicom.i18n import translator
from qicom.io.deidentification import ImageDeidentificationError, UnverifiedImageFile

_ANONYMIZER_LOCK = threading.Lock()
_DEIDENTIFICATION_METHOD = "DICOM PS3.15 Basic Profile; dicom-anonymizer 2023"
_DEIDENTIFICATION_METHOD_UNVERIFIED_PIXELS = (
    "DICOM PS3.15 Basic; dicom-anonymizer; pixels unverified"
)
# At most 64 characters: DeidentificationMethod is VR LO.
_DEIDENTIFICATION_METHOD_BEST_EFFORT = "QICOM best-effort, NOT verified (user override)"

# A converted JPEG commonly arrives as a very small Secondary Capture object
# without BurnedInAnnotation.  Treating that wrapper more strictly than the
# original JPG made otherwise anonymous synthetic/non-clinical images
# impossible to use.  The exception below is deliberately narrow: it is only
# available to Secondary Capture objects that carry none of these populated,
# direct identity attributes.  Other DICOM image classes continue to require
# an explicit BurnedInAnnotation=NO declaration.
_DIRECT_IDENTITY_KEYWORDS = (
    "PatientName",
    "PatientID",
    "IssuerOfPatientID",
    "PatientBirthDate",
    "PatientAddress",
    "OtherPatientIDs",
    "OtherPatientNames",
    "AccessionNumber",
    "InstitutionName",
    "InstitutionAddress",
    "ReferringPhysicianName",
    "PerformingPhysicianName",
    "OperatorsName",
)


class DicomDeidentificationError(ImageDeidentificationError):
    """The file is DICOM, but QICOM cannot safely export it as de-identified."""


#: One DICOM file the checks refused (see :func:`anonymize_dicom_file`'s
#: ``refused`` and ``unverified``). Kept under its historical name; the record
#: is the format-independent :class:`~qicom.io.deidentification.UnverifiedImageFile`.
UnverifiedDicomFile = UnverifiedImageFile

# UIDs that name a class, a syntax or a coding scheme rather than an instance:
# they identify nobody and are required to interpret the file, so the
# instance-UID replacement below must leave them alone. Standard UIDs under
# the DICOM root are kept whatever attribute holds them.
_CLASS_UID_KEYWORDS = frozenset(
    {
        "SOPClassUID",
        "MediaStorageSOPClassUID",
        "ReferencedSOPClassUID",
        "ReferencedSOPClassUIDInFile",
        "RelatedGeneralSOPClassUID",
        "OriginalSpecializedSOPClassUID",
        "SOPClassesInStudy",
        "TransferSyntaxUID",
        "ReferencedTransferSyntaxUIDInFile",
        "ImplementationClassUID",
        "CodingSchemeUID",
        "MappingResourceUID",
        "ContextGroupExtensionCreatorUID",
    }
)
_DICOM_STANDARD_UID_ROOT = "1.2.840.10008."
# File Meta attributes naming the station or network node a file came from.
_FILE_META_SOURCE_KEYWORDS = (
    "SourceApplicationEntityTitle",
    "SendingApplicationEntityTitle",
    "ReceivingApplicationEntityTitle",
    "SourcePresentationAddress",
    "SendingPresentationAddress",
    "ReceivingPresentationAddress",
)
_DATE_TIME_VRS = frozenset({"DA", "DT", "TM"})
_DATE_VRS = frozenset({"DA", "DT"})
#: How many attribute names a refusal message lists before summarising.
_MESSAGE_KEYWORD_LIMIT = 5


_PART10_PREAMBLE_BYTES = 128
_PART10_MAGIC = b"DICM"
# The whole File Meta group a de-identified copy may carry: the three Type 1
# elements naming the object and its encoding, plus the three pydicom fills in
# for the writer. Everything else - source and sending AE titles, presentation
# addresses, PrivateInformation and its creator - is dropped by rebuilding the
# group, never by listing what to remove.
_ALLOWED_FILE_META_KEYWORDS = frozenset(
    {
        "FileMetaInformationGroupLength",
        "FileMetaInformationVersion",
        "MediaStorageSOPClassUID",
        "MediaStorageSOPInstanceUID",
        "TransferSyntaxUID",
        "ImplementationClassUID",
        "ImplementationVersionName",
    }
)

# Attributes a DICOM dataset carries and effectively nothing else does.
# Used only to decide whether a file *without* the Part 10 preamble is a
# bare DICOM dataset (many PACS exports, older archives and CD writers
# produce these) rather than an unrelated file that pydicom's force=True
# reader happened not to choke on. Two independent matches are required,
# so a coincidental tag-shaped byte pattern in a non-DICOM file is not
# enough.
_DICOM_EVIDENCE_KEYWORDS = (
    "SOPClassUID",
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "StudyDate",
    "Modality",
    "PatientName",
    "PatientID",
    "PixelData",
    "Rows",
    "Columns",
)
_MINIMUM_DICOM_EVIDENCE = 2


def _summarize(exc: BaseException) -> str:
    """One-line "type: message" for a user-facing error message - pydicom
    chains its parse failures with an embedded traceback in the message
    text, which is useful in a log and unreadable in a dialog."""
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__


def _has_part10_magic(path: Path) -> bool:
    """Whether ``path`` starts with the DICOM PS3.10 preamble + 'DICM'."""
    try:
        with path.open("rb") as handle:
            header = handle.read(_PART10_PREAMBLE_BYTES + len(_PART10_MAGIC))
    except OSError:
        return False
    return header[_PART10_PREAMBLE_BYTES:] == _PART10_MAGIC


def _looks_like_dicom_dataset(dataset: Dataset) -> bool:
    matches = sum(1 for keyword in _DICOM_EVIDENCE_KEYWORDS if keyword in dataset)
    return matches >= _MINIMUM_DICOM_EVIDENCE


def read_dicom_dataset(path: Path, *, stop_before_pixels: bool = False) -> Dataset | None:
    """Parse ``path`` as DICOM, or return ``None`` if it is not DICOM.

    Deliberately more permissive than a plain :func:`pydicom.dcmread`: a
    dataset written without the PS3.10 preamble and File Meta Information
    (legal-ish, common from PACS exports and older archives) is still real
    DICOM carrying real PHI, and used to be classified as "not DICOM" and
    copied verbatim into packages QICOM told the user were de-identified.

    Raises :class:`DicomDeidentificationError` - never returns ``None`` -
    for a file that *is* recognisably DICOM but cannot be parsed
    (truncated, corrupt, an unknown VR). Failing closed matters more than
    failing gracefully here: the alternative is shipping an unreadable but
    perfectly quotable patient name to whoever receives the package.
    """
    try:
        return pydicom.dcmread(path, stop_before_pixels=stop_before_pixels)
    except InvalidDicomError:
        pass  # no preamble/DICM magic - may still be a bare dataset, see below
    except OSError:
        raise
    except Exception as exc:  # noqa: BLE001 - any parse failure is handled below
        if _has_part10_magic(path):
            raise DicomDeidentificationError(
                translator.tr("deidentification.reason.dicom_unparsable", error=_summarize(exc))
            ) from exc
        return None

    try:
        dataset = pydicom.dcmread(path, force=True, stop_before_pixels=stop_before_pixels)
    except OSError:
        raise
    except Exception:  # noqa: BLE001 - unreadable *and* unmarked: not DICOM
        return None
    return dataset if _looks_like_dicom_dataset(dataset) else None


def is_dicom_file(path: Path) -> bool:
    """Return whether ``path`` parses as DICOM, regardless of its suffix.

    Includes datasets stored without the PS3.10 preamble - see
    :func:`read_dicom_dataset`.
    """
    try:
        return read_dicom_dataset(path, stop_before_pixels=True) is not None
    except (OSError, DicomDeidentificationError):
        return False


def _is_identity_free_secondary_capture(dataset: Dataset) -> bool:
    sop_class_uid = str(
        dataset.get(
            "SOPClassUID",
            dataset.file_meta.get("MediaStorageSOPClassUID", ""),
        )
    )
    has_direct_identity = any(
        str(dataset.get(keyword, "")).strip() for keyword in _DIRECT_IDENTITY_KEYWORDS
    )
    return sop_class_uid == str(SecondaryCaptureImageStorage) and not has_direct_identity


def _assert_clean_pixel_claim(dataset: Dataset, *, allow_missing_burned_in: bool = False) -> None:
    """Reject pixel-bearing objects that do not explicitly claim clean pixels.

    The Basic Profile only addresses attributes.  Absence of
    BurnedInAnnotation is therefore not evidence that the pixels are safe.
    QICOM has no validated OCR/redaction pipeline and must fail closed.
    """
    if "PixelData" not in dataset:
        return
    burned_in = str(dataset.get("BurnedInAnnotation", "")).strip().upper()
    if burned_in not in {"", "NO"} or (not burned_in and not allow_missing_burned_in):
        raise DicomDeidentificationError(translator.tr("deidentification.reason.burned_in"))
    recognizable = str(dataset.get("RecognizableVisualFeatures", "")).strip().upper()
    if recognizable == "YES":
        raise DicomDeidentificationError(translator.tr("deidentification.reason.recognizable"))


# Values that identify nobody because they are what a de-identifier writes
# *in place of* an identifier. They must be excluded from the residual
# check below, or re-exporting an already de-identified package - opening
# an evaluation, editing it and saving it again does exactly that - would
# flag the previous pass's own replacement text ("ANONYMIZED", the
# placeholder birth date "00010101", ...) as leaked identity and block the
# save. Compared case-insensitively.
_STATIC_DEIDENTIFIED_PLACEHOLDERS = frozenset(
    {"anonymized", "anonymised", "anonymous", "anon", "none", "removed"}
)


@cache
def _deidentified_placeholders() -> frozenset[str]:
    """Every value the profile itself writes into an identity attribute.

    Determined by running the profile once on a probe data set carrying a
    distinctive value in each identifying attribute and collecting what
    comes back, rather than hard-coding a list that would silently rot the
    day `dicom-anonymizer` changes a replacement token - and rot in the
    direction of blocking legitimate re-exports.
    """
    probe = Dataset()
    for keyword in _DIRECT_IDENTITY_KEYWORDS:
        try:
            # <= 16 characters: several of these attributes are VR SH,
            # and a longer probe value only produces a pydicom warning.
            setattr(probe, keyword, "19000101" if "Date" in keyword else "QICOM PROBE")
        except (ValueError, TypeError):  # pragma: no cover - defensive
            continue
    with _ANONYMIZER_LOCK:
        _library_uid_dictionary.clear()
        try:
            anonymize_dataset(probe, delete_private_tags=True)
        except Exception:  # noqa: BLE001 - a failed probe just means no extra tokens
            return _STATIC_DEIDENTIFIED_PLACEHOLDERS
        finally:
            _library_uid_dictionary.clear()
    produced = {
        str(element.value).strip().casefold()
        for element in probe.iterall()
        if element.keyword in _DIRECT_IDENTITY_KEYWORDS and str(element.value).strip()
    }
    return _STATIC_DEIDENTIFIED_PLACEHOLDERS | frozenset(produced)


def _element_texts(element: DataElement) -> list[str]:
    """The element's value(s) as stripped text, one entry per value."""
    raw = element.value
    items = raw if isinstance(raw, (list, tuple, MultiValue)) else [raw]
    return [str(item).strip() for item in items if item is not None]


def _identity_values(dataset: Dataset) -> set[str]:
    """Every identifying value present anywhere in the data set tree.

    `iterall()` walks nested sequences too. The attribute rules reach them
    only because QICOM applies them item by item (see
    :func:`_anonymize_nested_items`); this check is what proves it did.
    """
    values: set[str] = set()
    for element in dataset.iterall():
        if element.keyword not in _DIRECT_IDENTITY_KEYWORDS:
            continue
        for text in _element_texts(element):
            if text and text.casefold() not in _deidentified_placeholders():
                values.add(text)
    return values


def _describe_keywords(keywords: set[str]) -> str:
    listed = sorted(keywords)
    shown = ", ".join(listed[:_MESSAGE_KEYWORD_LIMIT])
    if len(listed) > _MESSAGE_KEYWORD_LIMIT:
        shown += f" and {len(listed) - _MESSAGE_KEYWORD_LIMIT} more"
    return shown


def _assert_identity_values_are_gone(dataset: Dataset, original_values: set[str]) -> None:
    """Fail closed if any value the original file identified a person by
    is still somewhere in the de-identified copy.

    Deliberately compares only *identifying attributes* against the values
    they actually held before, so a replacement value the profile itself
    installed can never trip it, and no unrelated element can either. The
    message names the attributes, never the values: it is shown to the
    user and kept in the error log.
    """
    if not original_values:
        return
    residual_keywords: set[str] = set()
    for element in dataset.iterall():
        if element.keyword not in _DIRECT_IDENTITY_KEYWORDS:
            continue
        if any(text in original_values for text in _element_texts(element)):
            residual_keywords.add(element.keyword)
    if residual_keywords:
        raise DicomDeidentificationError(
            translator.tr(
                "deidentification.reason.identifying_values",
                keywords=_describe_keywords(residual_keywords),
            )
        )


def _is_standard_uid(value: str) -> bool:
    return value.startswith(_DICOM_STANDARD_UID_ROOT)


def _is_instance_uid_element(element: DataElement) -> bool:
    return element.VR == "UI" and element.keyword not in _CLASS_UID_KEYWORDS


def _all_elements(dataset: Dataset) -> list[DataElement]:
    """Every element of the tree, the File Meta group included."""
    elements = list(dataset.iterall())
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is not None:
        elements.extend(file_meta.iterall())
    return elements


def _original_instance_uids(dataset: Dataset) -> set[str]:
    """Every instance UID of the original file, at any depth."""
    return {
        text
        for element in _all_elements(dataset)
        if _is_instance_uid_element(element)
        for text in _element_texts(element)
        if text and not _is_standard_uid(text)
    }


@cache
def _date_time_placeholders() -> frozenset[str]:
    """The dummy dates and times the profile writes, read from the profile
    itself for the same reason as :func:`_deidentified_placeholders`."""
    produced: set[str] = set()
    for vr, value in (("DA", "20000101"), ("DT", "20000101000000"), ("TM", "000000")):
        element = DataElement(Tag(0x0008, 0x0020), vr, value)
        _replace_date_time(element)
        produced.add(str(element.value).strip().casefold())
    return frozenset(produced)


def _original_dates(dataset: Dataset) -> set[str]:
    """Every date and date-time of the original file, at any depth, other
    than the placeholders a previous de-identification wrote - re-exporting
    an already de-identified package must not trip over its own dummies."""
    return {
        text
        for element in dataset.iterall()
        if element.VR in _DATE_VRS
        for text in _element_texts(element)
        if text
        and text.casefold() not in _deidentified_placeholders()
        and text.casefold() not in _date_time_placeholders()
    }


def _assert_original_identifiers_are_gone(
    dataset: Dataset, original_uids: set[str], original_dates: set[str]
) -> None:
    """Fail closed if an instance UID or a date of the original survived.

    Instance UIDs let anyone with access to the source archive look the
    patient up; dates of an examination are identifying on their own. Both
    are replaced everywhere, so finding one means a rule did not run.
    """
    residual_keywords: set[str] = set()
    for element in _all_elements(dataset):
        if _is_instance_uid_element(element):
            leaked = original_uids
        elif element.VR in _DATE_VRS:
            leaked = original_dates
        else:
            continue
        if any(text in leaked for text in _element_texts(element)):
            residual_keywords.add(element.keyword or str(element.tag))
    if residual_keywords:
        raise DicomDeidentificationError(
            translator.tr(
                "deidentification.reason.uids_dates", keywords=_describe_keywords(residual_keywords)
            )
        )


def _replace_instance_uids(element: DataElement, replacements: set[str]) -> None:
    """Map each instance UID through the package mapping, exactly once.

    ``replacements`` holds every UID the mapping has produced so far, so a
    value already replaced - by the profile, or by an earlier visit of this
    pass - is never mapped a second time, which would break the references
    between files of one package.
    """
    raw = element.value
    values = list(raw) if isinstance(raw, (list, tuple, MultiValue)) else [raw]
    changed = False
    for index, value in enumerate(values):
        text = str(value).strip() if value is not None else ""
        if not text or text in replacements or _is_standard_uid(text):
            continue
        replacement = _profile_replacement_uid(text)
        replacements.add(replacement)
        values[index] = replacement
        changed = True
    if changed:
        element.value = values if len(values) > 1 else values[0]


@cache
def _profile_actions() -> dict[BaseTag, Callable[[Dataset, tuple[int, int]], None]]:
    """The Basic Profile's per-attribute rules, keyed by tag - the same rules
    `dicom-anonymizer` applies to the top-level data set. Repeating-group
    rules are left out: the library already walks those through every
    nested sequence itself."""
    return {
        Tag(tag[0], tag[1]): action for tag, action in _profile_rules().items() if len(tag) == 2
    }


def _anonymize_nested_items(dataset: Dataset, replacements: set[str]) -> None:
    for element in [element for element in dataset if element.VR == "SQ"]:
        for item in element.value or ():
            _anonymize_item(item, replacements)


def _anonymize_item(item: Dataset, replacements: set[str]) -> None:
    """Apply the profile's rules to one sequence item, then to its own items.

    A sequence attribute the profile deletes is deleted with everything in
    it. Any other rule for a sequence is not applied wholesale - the
    library's version empties every UID inside, including references that
    must stay consistent - the item's own attributes get their own rules
    instead, one level down.
    """
    actions = _profile_actions()
    for tag in list(item.keys()):
        action = actions.get(tag)
        if action is None or action is _profile_keep or tag not in item:
            continue
        if action is _profile_delete:
            del item[tag]
            continue
        element = item[tag]
        if element.VR == "SQ":
            continue
        if element.VR == "UI":
            _replace_instance_uids(element, replacements)
            continue
        action(item, (tag.group, tag.element))
    item.remove_private_tags()
    _anonymize_nested_items(item, replacements)


def _scrub_remaining_identifiers(dataset: Dataset, replacements: set[str]) -> None:
    """The safety net after the profile: wherever an attribute the profile
    does not list still holds an instance UID, a date, a time or a person
    name, it is replaced as the profile would replace one it lists."""
    for element in dataset.iterall():
        if element.value is None or element.value == "":
            continue
        if _is_instance_uid_element(element):
            _replace_instance_uids(element, replacements)
        elif element.VR in _DATE_TIME_VRS:
            _replace_date_time(element)
        elif element.VR == "PN":
            element.value = ""
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is None:
        return
    for keyword in _FILE_META_SOURCE_KEYWORDS:
        if keyword in file_meta:
            delattr(file_meta, keyword)
    for element in file_meta.iterall():
        if _is_instance_uid_element(element):
            _replace_instance_uids(element, replacements)


def _is_directory_object(dataset: Dataset) -> bool:
    file_meta = getattr(dataset, "file_meta", None)
    media_class = file_meta.get("MediaStorageSOPClassUID", "") if file_meta is not None else ""
    return str(media_class) == MediaStorageDirectoryStorage or "DirectoryRecordSequence" in dataset


def _assert_no_private_attributes(dataset: Dataset) -> None:
    residual = [element.tag for element in dataset.iterall() if element.tag.is_private]
    if residual:
        preview = ", ".join(str(tag) for tag in residual[:5])
        raise DicomDeidentificationError(
            translator.tr("deidentification.reason.private", preview=preview)
        )


def _rebuild_container(dataset: Dataset, transfer_syntax: UID) -> None:
    """Give ``dataset`` a fresh File Meta group and a zeroed preamble.

    PS3.15 asks for the File Meta Information to be replaced, not cleaned:
    the preamble is application-defined bytes and the group can hold
    elements (PrivateInformation, AE titles) no attribute rule reaches.  The
    Media Storage UIDs follow the data set, whose instance UID the profile
    has already replaced.
    """
    previous = getattr(dataset, "file_meta", None)
    meta = FileMetaDataset()
    sop_class = dataset.get("SOPClassUID") or (
        previous.get("MediaStorageSOPClassUID") if previous is not None else None
    )
    sop_instance = dataset.get("SOPInstanceUID") or (
        previous.get("MediaStorageSOPInstanceUID") if previous is not None else None
    )
    if sop_class:
        meta.MediaStorageSOPClassUID = sop_class
    if sop_instance:
        meta.MediaStorageSOPInstanceUID = sop_instance
    meta.TransferSyntaxUID = transfer_syntax
    dataset.file_meta = meta
    dataset.preamble = b"\0" * _PART10_PREAMBLE_BYTES


def _assert_safe_container(dataset: Dataset) -> None:
    """Fail closed if the written copy kept a non-zero preamble or a File
    Meta element outside :data:`_ALLOWED_FILE_META_KEYWORDS`."""
    preamble = getattr(dataset, "preamble", None)
    residual = [] if not preamble or not any(preamble) else ["preamble"]
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is not None:
        residual.extend(
            element.keyword or str(element.tag)
            for element in file_meta
            if element.keyword not in _ALLOWED_FILE_META_KEYWORDS
        )
    if residual:
        raise DicomDeidentificationError(
            translator.tr(
                "deidentification.reason.dicom_container",
                keywords=_describe_keywords(set(residual)),
            )
        )


def _write_deidentified(dataset: Dataset, destination: Path, source: Path) -> None:
    """Serialize the de-identified dataset in a rebuilt container.

    A file that arrived as a Part 10 object is written back as one, with a
    new File Meta group and a zeroed preamble (:func:`_rebuild_container`).
    A bare dataset (no preamble, no File Meta group - see
    :func:`read_dicom_dataset`) is upgraded to Part 10 when its SOP Common
    UIDs supply the required File Meta elements, and otherwise written
    back in the same bare form it arrived in: inventing a SOP Class for it
    would be a fabrication, and refusing to write it would block the
    export of a file whose PHI has just been removed successfully.

    The transfer syntax always follows how the dataset was actually
    decoded, so pixel bytes are re-encoded exactly as they were read.
    Big-endian sources without File Meta Information are refused rather
    than silently byte-swapped into a little-endian syntax.
    """
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is not None and "TransferSyntaxUID" in file_meta:
        _rebuild_container(dataset, file_meta.TransferSyntaxUID)
        dataset.save_as(destination, enforce_file_format=True)
        return

    implicit_vr, little_endian = dataset.original_encoding
    if little_endian is False:
        raise DicomDeidentificationError(translator.tr("deidentification.reason.big_endian"))
    if "SOPClassUID" in dataset and "SOPInstanceUID" in dataset:
        _rebuild_container(
            dataset, ImplicitVRLittleEndian if implicit_vr else ExplicitVRLittleEndian
        )
        dataset.save_as(destination, enforce_file_format=True)
        return

    dataset.file_meta = FileMetaDataset()
    dataset.preamble = None
    dataset.save_as(
        destination,
        implicit_vr=bool(implicit_vr),
        little_endian=True,
        enforce_file_format=False,
    )


def anonymize_dicom_file(
    path: Path,
    *,
    uid_mapping: dict[str, str] | None = None,
    unverified: list[UnverifiedDicomFile] | None = None,
    refused: list[UnverifiedDicomFile] | None = None,
) -> bool:
    """De-identify the DICOM copy at ``path`` in place, atomically.

    ``False`` means the file is not DICOM and was left untouched.  A DICOM
    that cannot be proven safe raises :class:`DicomDeidentificationError` -
    including one QICOM can recognise but not parse or not rewrite, which
    is never passed through as "not DICOM".  Datasets stored without the
    PS3.10 preamble are de-identified like any other and written back in
    their own container format (see :func:`read_dicom_dataset` and
    :func:`_write_deidentified`).

    Passing the same ``uid_mapping`` to every file in one package preserves
    internal references while keeping different exports unlinkable.

    ``refused`` lets a package writer collect every refusal of one export
    instead of stopping at the first: the refused file is recorded and left
    exactly as copied, **not de-identified**, and the writer must then stop
    before committing anything (see
    :func:`qicom.io.deidentification.raise_if_refused`).

    ``unverified`` must stay ``None`` unless the user has explicitly accepted
    exporting files QICOM cannot prove de-identified.  When it is a list, a
    file that would raise :class:`DicomDeidentificationError` is instead
    de-identified on a best-effort basis and recorded in it as an
    :class:`UnverifiedDicomFile`; ``OSError`` still propagates.
    """
    try:
        return _anonymize_dicom_file_strictly(path, uid_mapping)
    except DicomDeidentificationError as exc:
        if unverified is not None:
            unverified.append(
                _best_effort_deidentify(path, uid_mapping if uid_mapping is not None else {}, exc)
            )
            return True
        if refused is not None:
            refused.append(UnverifiedImageFile(path.name, str(exc), attributes_scrubbed=False))
            return True
        raise


def _anonymize_dicom_file_strictly(path: Path, uid_mapping: dict[str, str] | None) -> bool:
    dataset = read_dicom_dataset(path)
    if dataset is None:
        return False

    try:
        return _anonymize_dataset_in_place(dataset, path, uid_mapping)
    except (DicomDeidentificationError, OSError):
        raise
    except Exception as exc:  # noqa: BLE001 - never downgrade a failure to "left as-is"
        raise DicomDeidentificationError(
            translator.tr("deidentification.reason.failed", error=_summarize(exc))
        ) from exc


def _apply_basic_profile(dataset: Dataset, mapping: dict[str, str]) -> None:
    """The Basic Profile on the top-level data set (``dicom-anonymizer``),
    the same rules on every nested sequence item, then the safety net for
    UIDs, dates, times and names no rule lists."""
    # dicom-anonymizer currently stores UID replacements in a process-global
    # dictionary.  Isolate that implementation detail behind a lock and copy
    # the values into the package-scoped mapping supplied by our caller.
    with _ANONYMIZER_LOCK:
        _library_uid_dictionary.clear()
        _library_uid_dictionary.update(mapping)
        try:
            anonymize_dataset(dataset, delete_private_tags=True)
            replacements = set(_library_uid_dictionary.values())
            _anonymize_nested_items(dataset, replacements)
            _scrub_remaining_identifiers(dataset, replacements)
        finally:
            mapping.update(_library_uid_dictionary)
            _library_uid_dictionary.clear()


def _scrub_with_mapping(dataset: Dataset, mapping: dict[str, str]) -> None:
    """:func:`_scrub_remaining_identifiers` on its own, for the best-effort
    path where the profile itself may have failed half-way."""
    with _ANONYMIZER_LOCK:
        _library_uid_dictionary.clear()
        _library_uid_dictionary.update(mapping)
        try:
            _scrub_remaining_identifiers(dataset, set(_library_uid_dictionary.values()))
        finally:
            mapping.update(_library_uid_dictionary)
            _library_uid_dictionary.clear()


def _anonymize_dataset_in_place(
    dataset: Dataset, path: Path, uid_mapping: dict[str, str] | None
) -> bool:
    if _is_directory_object(dataset):
        raise DicomDeidentificationError(translator.tr("deidentification.reason.dicomdir"))
    original_identity_values = _identity_values(dataset)
    original_uids = _original_instance_uids(dataset)
    original_dates = _original_dates(dataset)
    pixel_claim_was_missing = not str(dataset.get("BurnedInAnnotation", "")).strip()
    allow_missing_burned_in = pixel_claim_was_missing and _is_identity_free_secondary_capture(
        dataset
    )
    _assert_clean_pixel_claim(dataset, allow_missing_burned_in=allow_missing_burned_in)
    _apply_basic_profile(dataset, uid_mapping if uid_mapping is not None else {})

    _assert_no_private_attributes(dataset)
    _assert_identity_values_are_gone(dataset, original_identity_values)
    _assert_original_identifiers_are_gone(dataset, original_uids, original_dates)
    dataset.PatientIdentityRemoved = "YES"
    dataset.DeidentificationMethod = (
        _DEIDENTIFICATION_METHOD_UNVERIFIED_PIXELS
        if pixel_claim_was_missing
        else _DEIDENTIFICATION_METHOD
    )

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
        _write_deidentified(dataset, temp_path, path)
        # Validate the actual serialized file, not only the in-memory object.
        verified = read_dicom_dataset(temp_path)
        if verified is None:
            raise DicomDeidentificationError(
                translator.tr("deidentification.reason.dicom_read_back")
            )
        _assert_clean_pixel_claim(verified, allow_missing_burned_in=allow_missing_burned_in)
        _assert_safe_container(verified)
        _assert_no_private_attributes(verified)
        _assert_identity_values_are_gone(verified, original_identity_values)
        _assert_original_identifiers_are_gone(verified, original_uids, original_dates)
        if str(verified.get("PatientIdentityRemoved", "")).upper() != "YES":
            raise DicomDeidentificationError(translator.tr("deidentification.reason.declaration"))
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return True


def _blank_identity(dataset: Dataset, original_values: set[str]) -> None:
    """Empty every direct identity attribute, and every other text element
    holding a value one of them held, anywhere in the data set tree.

    The fallback for a file the Basic Profile could not process (or left
    identity in, as with a DICOMDIR): far less than the profile, but it
    reaches the attributes a person is actually named by.
    """
    for element in dataset.iterall():
        if element.VR == "SQ" or element.value is None:
            continue
        raw = element.value
        items = raw if isinstance(raw, (list, tuple, MultiValue)) else [raw]
        texts = {str(item).strip() for item in items if isinstance(item, (str, PersonName))}
        if element.keyword in _DIRECT_IDENTITY_KEYWORDS or texts & original_values:
            element.value = ""


def _best_effort_deidentify(
    path: Path, mapping: dict[str, str], refusal: DicomDeidentificationError
) -> UnverifiedDicomFile:
    """What the user accepted when they chose to export despite ``refusal``:
    everything QICOM can still remove is removed, nothing is claimed.

    The Basic Profile is attempted, private attributes are dropped, any
    identifying value left is blanked and instance UIDs, dates, times and
    person names are replaced wherever they still are; the copy is then declared
    ``PatientIdentityRemoved=NO`` with a method naming the override.  A file
    that cannot be parsed or rewritten at all stays exactly as copied, and
    the returned record says so.
    """
    reason = str(refusal)
    try:
        dataset = read_dicom_dataset(path)
    except DicomDeidentificationError:
        dataset = None
    if dataset is None:
        return UnverifiedDicomFile(path.name, reason, attributes_scrubbed=False)

    temp_path: Path | None = None
    try:
        original_identity_values = _identity_values(dataset)
        try:
            _apply_basic_profile(dataset, mapping)
        except Exception:  # noqa: BLE001 - the manual scrub below still runs
            pass
        dataset.remove_private_tags()
        _blank_identity(dataset, original_identity_values)
        try:
            _scrub_with_mapping(dataset, mapping)
        except Exception:  # noqa: BLE001 - best effort: what was removed stays removed
            pass
        dataset.PatientIdentityRemoved = "NO"
        dataset.DeidentificationMethod = _DEIDENTIFICATION_METHOD_BEST_EFFORT
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
        _write_deidentified(dataset, temp_path, path)
        os.replace(temp_path, path)
        temp_path = None
    except OSError:
        raise
    except Exception:  # noqa: BLE001 - the user accepted an unmodified copy for this case
        return UnverifiedDicomFile(path.name, reason, attributes_scrubbed=False)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return UnverifiedDicomFile(path.name, reason, attributes_scrubbed=True)


#: Attributes that identify nobody but can tell an evaluator which arm of a
#: study an image belongs to: the scanner and its software, the protocol, the
#: reconstruction kernel or algorithm, the dose and tube settings, free-text
#: descriptions. A blinded delivery empties them (sequences are removed); the
#: author's own package keeps them.
_ARM_REVEALING_KEYWORDS = (
    "Manufacturer",
    "ManufacturerModelName",
    "SoftwareVersions",
    "DeviceSerialNumber",
    "StationName",
    "InstitutionName",
    "InstitutionalDepartmentName",
    "StudyDescription",
    "SeriesDescription",
    "ProtocolName",
    "PerformedProcedureStepDescription",
    "RequestedProcedureDescription",
    "ImageComments",
    "DerivationDescription",
    "ConvolutionKernel",
    "FilterType",
    "ReconstructionMethod",
    "ScanOptions",
    "KVP",
    "XRayTubeCurrent",
    "Exposure",
    "ExposureTime",
    "ExposureInuAs",
    "CTDIvol",
    "SpiralPitchFactor",
)
_ARM_REVEALING_SEQUENCES = (
    "ContributingEquipmentSequence",
    "ReconstructionAlgorithmSequence",
    "AcquisitionContextSequence",
)


def blind_acquisition_attributes(path: Path) -> bool:
    """Empty, in the DICOM copy at `path`, every attribute that could reveal
    how the image was acquired or reconstructed - see _ARM_REVEALING_KEYWORDS.

    A blinding measure, not a privacy one: the copy has already been
    de-identified. False when the file is not DICOM, cannot be parsed or has
    none of these attributes; the copy is then left as it is.
    """
    try:
        dataset = read_dicom_dataset(path)
    except DicomDeidentificationError:
        return False
    if dataset is None:
        return False
    changed = False
    for element in list(dataset.iterall()):
        if element.keyword in _ARM_REVEALING_KEYWORDS and element.value not in (None, ""):
            element.value = None
            changed = True
    for keyword in _ARM_REVEALING_SEQUENCES:
        for owner in [
            dataset,
            *(item for e in dataset.iterall() if e.VR == "SQ" for item in e.value),
        ]:
            if keyword in owner:
                delattr(owner, keyword)
                changed = True
    if not changed:
        return False
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
        _write_deidentified(dataset, temp_path, path)
        os.replace(temp_path, path)
        temp_path = None
    except DicomDeidentificationError:
        return False
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return True
