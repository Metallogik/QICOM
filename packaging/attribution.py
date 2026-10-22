"""Third-party attribution: what a distributed QICOM bundle actually contains.

Apache-2.0 requires the licence and notices of everything redistributed to
travel with the copy. Before this, `packaging/THIRD_PARTY_LICENSES.txt` was a
hand-maintained file generated once from a `.[gui]` environment: it described
QICOM itself as proprietary (it is Apache-2.0), and it stated that the
`learned` extra was out of scope while the release tags built exactly that -
a full + learned bundle - and shipped the same inventory with it.

This module replaces the hand-maintenance with three mechanical steps:

`generate`
    Resolve the dependency closure of one distributed profile from the
    *installed* metadata, and write a machine-readable inventory, a CycloneDX
    SBOM and the human-readable licence file. Fails when a distribution has
    no version, no origin, no licence, or no licence text to reproduce - the
    four things attribution needs and the four things a hand-maintained file
    silently loses.

`check-bundle`
    Compare what PyInstaller actually collected (recorded by qicom.spec, see
    write_bundle_modules) against that inventory. A distribution in the
    bundle and absent from the inventory fails the build.

Nothing here declares legal compliance. It produces the technical evidence -
complete, reproducible and checkable - and names the cases a human has to
review.

Reproducibility: no network, no ordering that depends on the filesystem, and
no timestamp in the inventory. Two runs against the same environment produce
byte-identical output.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_SCHEMA_VERSION = 1

#: Profile name -> the extras a bundle of that profile is built with. These
#: are the combinations the packaging workflow actually produces; anything
#: else has to be added here before it can be built and published.
PROFILE_EXTRAS: dict[str, tuple[str, ...]] = {
    "base": ("gui",),
    "base+learned": ("gui", "learned"),
    "full": ("gui", "radiomics"),
    "full+learned": ("gui", "radiomics", "learned"),
}

#: Never part of a bundle: QICOM itself (its licence is LICENSE/NOTICE, not a
#: third-party notice) and installers that merely happen to live in the venv.
#:
#: setuptools is deliberately *not* on this list. It looks like an installer,
#: but PyInstaller collects it - torch.utils.cpp_extension imports it, so the
#: analysis follows it into dist/qicom/_internal/setuptools. Something that
#: ships has to carry its licence, whatever its role in the venv was, and the
#: bundle check below is what noticed the discrepancy.
_EXCLUDED = {canonicalize_name(name) for name in ("qicom", "pip", "wheel")}

# PyInstaller is a build dependency, but its bootloader and runtime modules
# are redistributed inside every frozen application. Its helper dependencies
# are not automatically bundled, so only this distribution is added instead
# of walking the whole ``packaging`` extra closure.
BUNDLE_BUILD_RUNTIME_DISTRIBUTIONS: tuple[str, ...] = ("pyinstaller",)

#: Names that cannot be mapped through installed ``top_level.txt``: the
#: standard library, QICOM itself, and PyInstaller runtime aliases. The
#: PyInstaller distribution is inventoried separately; these aliases are
#: exempt only from top-level-name matching.
_NON_DISTRIBUTION_TOP_LEVELS = set(sys.stdlib_module_names) | {
    "qicom",
    "pyimod01_archive",
    "pyimod02_importers",
    "pyimod03_ctypes",
    "pyimod04_pywin32",
    "pyiboot01_bootstrap",
    "pyi_rth_inspect",
    # Debian/Ubuntu provide this standard-library customization beside the
    # stdlib rather than listing it in sys.stdlib_module_names.
    "sitecustomize",
    # Not a distribution at all: PyInstaller collects part of the interpreter
    # under a "python3.<minor>/" destination, and qicom.spec derives a
    # top-level name by cutting at the first dot - which turns that directory
    # into a bare "python3". The name varies with the platform's layout
    # ("python3" on macOS, "python3.12" on Linux), so both spellings are
    # exempt rather than only the one a given runner happens to produce.
    "python3",
    *(f"python3.{minor}" for minor in range(8, 30)),
}

_LICENSE_FILE_HINTS = ("LICEN", "COPYING", "NOTICE", "AUTHORS")


@dataclass(frozen=True)
class DistributionRecord:
    """One redistributed distribution, as the inventory records it."""

    name: str
    version: str
    license: str
    origin: str
    top_level: tuple[str, ...] = ()
    notice_files: tuple[str, ...] = ()
    #: For a distribution installed from a repository rather than a released
    #: wheel: the VCS URL and the exact commit (PEP 610 direct_url.json).
    #: PyRadiomics is installed this way, pinned to a commit in pyproject.toml,
    #: and a version string alone ("3.0.1a1") identifies nothing that can be
    #: fetched again - the same reason qicom.build_identity exists for QICOM.
    vcs_url: str = ""
    vcs_commit: str = ""
    #: "metadata" when everything above was read from the installed wheel;
    #: "reviewed-override" when a human supplied it (see
    #: packaging/attribution_overrides.json) because the wheel did not.
    attribution_source: str = "metadata"

    @property
    def purl(self) -> str:
        """A package URL that actually identifies what was installed.

        For a VCS install the PyPI version is meaningless, so the purl names
        the repository and the commit instead - the form a reader can fetch.
        """
        if self.vcs_url and self.vcs_commit:
            repository = self.vcs_url.removeprefix("https://").removesuffix(".git")
            return f"pkg:github/{repository.removeprefix('github.com/')}@{self.vcs_commit}"
        return f"pkg:pypi/{canonicalize_name(self.name)}@{self.version}"

    def missing_fields(self) -> tuple[str, ...]:
        """The attribution facts this record cannot supply."""
        missing = [
            field_name
            for field_name, value in (
                ("version", self.version),
                ("license", self.license),
                ("origin", self.origin),
            )
            if not value
        ]
        if not self.notice_files:
            missing.append("notice")
        return tuple(missing)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "license": self.license,
            "origin": self.origin,
            "purl": self.purl,
            "top_level": list(self.top_level),
            "notice_files": list(self.notice_files),
            "attribution_source": self.attribution_source,
            "vcs_url": self.vcs_url,
            "vcs_commit": self.vcs_commit,
        }


@dataclass
class Inventory:
    profile: str
    distributions: list[DistributionRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "profile": self.profile,
            "distributions": [record.to_dict() for record in self.distributions],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> Inventory:
        return cls(
            profile=str(payload["profile"]),
            distributions=[
                DistributionRecord(
                    name=str(item["name"]),
                    version=str(item.get("version", "")),
                    license=str(item.get("license", "")),
                    origin=str(item.get("origin", "")),
                    top_level=tuple(str(value) for value in item.get("top_level", ())),
                    notice_files=tuple(str(value) for value in item.get("notice_files", ())),
                    attribution_source=str(item.get("attribution_source", "metadata")),
                    vcs_url=str(item.get("vcs_url", "")),
                    vcs_commit=str(item.get("vcs_commit", "")),
                )
                for item in payload.get("distributions", [])
            ],
        )

    def top_level_owners(self) -> dict[str, str]:
        """Top-level module name -> the distribution that provides it."""
        owners: dict[str, str] = {}
        for record in self.distributions:
            for name in record.top_level:
                owners[name] = record.name
        return owners


# -- Reading the installed environment -------------------------------------


def _license_expression(meta: metadata.PackageMetadata) -> str:
    """The SPDX expression if the distribution states one, else the licence
    named by its classifiers, else its free-text License field."""
    expression = meta.get("License-Expression")
    if expression:
        return str(expression).strip()
    classifiers = [
        value.split("::")[-1].strip()
        for value in meta.get_all("Classifier", [])
        if value.startswith("License ::")
    ]
    if classifiers:
        return " OR ".join(sorted(set(classifiers)))
    free_text = str(meta.get("License", "") or "").strip()
    # Some distributions paste the whole licence into this field.
    return free_text.splitlines()[0].strip() if free_text else ""


def _origin(meta: metadata.PackageMetadata) -> str:
    """Where the distribution comes from - a homepage or repository URL."""
    home_page = str(meta.get("Home-page", "") or "").strip()
    if home_page:
        return home_page
    urls = {}
    for entry in meta.get_all("Project-URL", []):
        label, _, url = str(entry).partition(",")
        urls[label.strip().casefold()] = url.strip()
    for label in ("homepage", "home", "source", "repository", "source code", "documentation"):
        if urls.get(label):
            return urls[label]
    return next(iter(urls.values()), "")


def _notice_files(distribution: metadata.Distribution) -> tuple[str, ...]:
    """Paths, inside the distribution, of the licence texts it ships."""
    declared = [str(value) for value in distribution.metadata.get_all("License-File", [])]
    if declared:
        # Metadata paths are relative to ``.dist-info/licenses/`` in newer
        # wheels and to ``.dist-info/`` in some older ones, while
        # Distribution.locate_file() expects the installed RECORD path.
        installed_paths = [str(path) for path in (distribution.files or [])]
        resolved: list[str] = []
        for relative in declared:
            suffix = f"/{relative}"
            matches = [
                path
                for path in installed_paths
                if path == relative or path.replace("\\", "/").endswith(suffix)
            ]
            resolved.extend(matches or [relative])
        return tuple(sorted(set(resolved)))
    found = [
        str(path)
        for path in (distribution.files or [])
        if any(hint in path.name.upper() for hint in _LICENSE_FILE_HINTS)
        and path.suffix.lower() not in {".py", ".pyc", ".so", ".dll", ".dylib"}
    ]
    return tuple(sorted(found))


def _vcs_provenance(distribution: metadata.Distribution) -> tuple[str, str]:
    """(url, commit) for a distribution installed from a repository.

    PEP 610 records this in direct_url.json when pip installs from a VCS.
    Without it, an SBOM entry for PyRadiomics says "3.0.1a1" - a version that
    matches no PyPI release and cannot be fetched again by anyone auditing the
    build. pip-audit skips it for the same reason.
    """
    payload = distribution.read_text("direct_url.json")
    if not payload:
        return "", ""
    try:
        direct_url = json.loads(payload)
    except json.JSONDecodeError:
        return "", ""
    vcs_info = direct_url.get("vcs_info")
    if not isinstance(vcs_info, dict):
        return "", ""
    return str(direct_url.get("url", "")), str(vcs_info.get("commit_id", ""))


def _top_level(distribution: metadata.Distribution) -> tuple[str, ...]:
    """Importable top-level names, so a bundle's modules can be traced back."""
    text = distribution.read_text("top_level.txt")
    if text:
        return tuple(sorted({line.strip() for line in text.splitlines() if line.strip()}))
    names: set[str] = set()
    for path in distribution.files or []:
        parts = path.parts
        if not parts or parts[0].endswith((".dist-info", ".egg-info", ".data")):
            continue
        first = parts[0]
        if first.endswith(".py"):
            names.add(first[:-3])
        elif "." not in first:
            names.add(first)
        elif first.endswith((".so", ".pyd")):
            names.add(first.split(".", 1)[0])
    return tuple(sorted(names))


def _installed() -> dict[str, metadata.Distribution]:
    """Canonical name -> distribution, deduplicated (an editable install can
    be visible through both a .dist-info and a legacy .egg-info)."""
    found: dict[str, metadata.Distribution] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if not name:
            continue
        found.setdefault(canonicalize_name(name), distribution)
    return found


def resolve_closure(
    profile: str, installed: dict[str, metadata.Distribution] | None = None
) -> list[str]:
    """Canonical names of everything `qicom[<profile extras>]` pulls in.

    Walks Requires-Dist from the profile's own extras, evaluating environment
    markers for the running interpreter and platform - which is why this runs
    in the same environment PyInstaller is about to freeze, and on the same
    runner. Development and packaging tooling never enters the closure: it is
    not in any extra a bundle is built with.
    """
    if profile not in PROFILE_EXTRAS:
        raise ValueError(f"unknown profile {profile!r}; expected one of {sorted(PROFILE_EXTRAS)}")
    environment = installed if installed is not None else _installed()
    seen: set[str] = set()
    queue: list[tuple[str, tuple[str, ...]]] = [("qicom", PROFILE_EXTRAS[profile])]
    while queue:
        name, extras = queue.pop()
        canonical = canonicalize_name(name)
        distribution = environment.get(canonical)
        if distribution is None:
            continue
        if canonical not in _EXCLUDED:
            seen.add(canonical)
        for raw in distribution.metadata.get_all("Requires-Dist", []):
            requirement = Requirement(str(raw))
            if requirement.marker is not None and not any(
                requirement.marker.evaluate({"extra": extra}) for extra in ("", *extras)
            ):
                continue
            queue.append((requirement.name, tuple(sorted(requirement.extras))))
    return sorted(seen)


OVERRIDES_PATH = REPO_ROOT / "packaging" / "attribution_overrides.json"
NOTICES_ROOT = REPO_ROOT / "packaging" / "attribution"


def load_overrides(path: Path | None = None) -> dict[str, dict]:
    """Reviewed manual entries, keyed by canonical distribution name.

    Some wheels ship no licence file and no usable licence field - PySide6's
    do not - so the text Apache-2.0 requires to travel with the bundle has to
    come from somewhere a human vouched for. Reading it from a checked-in
    file, with the reason recorded next to it, keeps that judgement visible
    instead of hidden in a generated artefact.
    """
    source = path or OVERRIDES_PATH
    if not source.exists():
        return {}
    payload = json.loads(source.read_text(encoding="utf-8"))
    return {
        canonicalize_name(name): dict(entry)
        for name, entry in dict(payload.get("overrides", {})).items()
    }


def _apply_override(record: DistributionRecord, override: dict) -> DistributionRecord:
    from dataclasses import replace

    return replace(
        record,
        license=str(override.get("license") or record.license),
        origin=str(override.get("origin") or record.origin),
        notice_files=tuple(str(value) for value in override.get("notice_files", ()))
        or record.notice_files,
        attribution_source="reviewed-override",
    )


def build_inventory(
    profile: str,
    overrides: dict[str, dict] | None = None,
    *,
    include_build_runtime: bool = False,
) -> Inventory:
    """The inventory for one distributed profile, read from this environment."""
    installed = _installed()
    reviewed = load_overrides() if overrides is None else overrides
    records = []
    names = set(resolve_closure(profile, installed))
    if include_build_runtime:
        required = {canonicalize_name(name) for name in BUNDLE_BUILD_RUNTIME_DISTRIBUTIONS}
        missing = sorted(required - installed.keys())
        if missing:
            raise RuntimeError(
                "cannot inventory redistributed build runtime(s) "
                f"{', '.join(missing)}; install QICOM's packaging extra first"
            )
        names.update(required)
    for canonical in sorted(names):
        distribution = installed[canonical]
        meta = distribution.metadata
        vcs_url, vcs_commit = _vcs_provenance(distribution)
        record = DistributionRecord(
            name=str(meta["Name"]),
            version=str(meta["Version"] or ""),
            license=_license_expression(meta),
            origin=_origin(meta),
            top_level=_top_level(distribution),
            notice_files=_notice_files(distribution),
            vcs_url=vcs_url,
            vcs_commit=vcs_commit,
        )
        override = reviewed.get(canonical)
        if override is not None:
            record = _apply_override(record, override)
        records.append(record)
    records.sort(key=lambda record: canonicalize_name(record.name))
    return Inventory(profile=profile, distributions=records)


def _notice_path(
    record: DistributionRecord,
    relative: str,
    installed: dict[str, metadata.Distribution],
    notices_root: Path,
) -> Path | None:
    if record.attribution_source == "reviewed-override":
        return notices_root / relative
    distribution = installed.get(canonicalize_name(record.name))
    return None if distribution is None else Path(distribution.locate_file(relative))


def missing_notice_texts(inventory: Inventory, notices_root: Path | None = None) -> list[str]:
    """Inventoried licence/notice files that cannot actually be reproduced."""
    root = notices_root or NOTICES_ROOT
    installed = _installed()
    missing = []
    for record in inventory.distributions:
        for relative in record.notice_files:
            path = _notice_path(record, relative, installed, root)
            if path is None or not path.is_file():
                missing.append(f"{record.name}: referenced notice {relative!r} does not exist")
    return missing


# -- Rendering --------------------------------------------------------------


def render_sbom(inventory: Inventory) -> dict[str, object]:
    """A CycloneDX 1.5 document. Deliberately without a timestamp or a random
    serial number: an SBOM published next to a checksum has to be
    reproducible from the same environment, and neither field carries
    information a build could not get from the build record itself."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "QICOM",
                "version": _qicom_version(),
                "licenses": [{"license": {"id": "Apache-2.0"}}],
                "purl": f"pkg:pypi/qicom@{_qicom_version()}",
            },
            "properties": [{"name": "qicom:profile", "value": inventory.profile}],
        },
        "components": [
            {
                "type": "library",
                "name": record.name,
                "version": record.version,
                "purl": record.purl,
                "licenses": [{"license": {"name": record.license}}],
                "externalReferences": (
                    ([{"type": "website", "url": record.origin}] if record.origin else [])
                    + (
                        [{"type": "vcs", "url": f"{record.vcs_url}@{record.vcs_commit}"}]
                        if record.vcs_commit
                        else []
                    )
                ),
            }
            for record in inventory.distributions
        ],
    }


def _qicom_version() -> str:
    import runpy

    return str(runpy.run_path(str(REPO_ROOT / "qicom" / "__version__.py"))["__version__"])


_LICENSE_HEADER = """\
================================================================================
QICOM - THIRD-PARTY LICENSES AND ATTRIBUTION
================================================================================

Profile: {profile}
QICOM {version}

QICOM's own source code is licensed under the Apache License, Version 2.0.
Its full text is in LICENSE, and the notices Apache-2.0 requires to travel
with it are in NOTICE. Both files ship inside every bundle. The licences
below are those of the third-party libraries QICOM redistributes; they do not
apply to QICOM itself, and QICOM's licence does not apply to them.

This file is generated - do not edit it by hand. Regenerate it from the same
environment a bundle is built from:

    python packaging/attribution.py generate --profile {profile} \\
        --include-build-runtime --output-dir packaging/attribution

That command also writes THIRD_PARTY_INVENTORY.json (machine-readable, one
entry per distribution with its version, origin, licence and licence-file
paths) and qicom-sbom.cdx.json (CycloneDX 1.5). The human-readable file also
reproduces those licence/notice texts. All three are published
alongside the bundle checksums, and
`python packaging/attribution.py check-bundle` fails the build if what
PyInstaller collected is not what this inventory describes.

Distributions in this profile: {count}

================================================================================
INVENTORY
================================================================================

"""


def render_license_file(inventory: Inventory) -> str:
    """The human-readable attribution file, in a stable order."""
    lines = [
        _LICENSE_HEADER.format(
            profile=inventory.profile,
            version=_qicom_version(),
            count=len(inventory.distributions),
        )
    ]
    for record in inventory.distributions:
        reviewed = " (reviewed entry)" if record.attribution_source != "metadata" else ""
        lines.append(
            f"{record.name} {record.version}{reviewed}\n"
            f"    Licence: {record.license}\n"
            f"    Origin:  {record.origin}\n"
            f"    Notices: {', '.join(record.notice_files)}\n"
            + (
                f"    Source:  {record.vcs_url} @ {record.vcs_commit}\n"
                if record.vcs_commit
                else ""
            )
        )

    if inventory.distributions:
        lines.append(
            "\n"
            "================================================================================\n"
            "REPRODUCED LICENCE TEXTS\n"
            "================================================================================\n"
            "\n"
            "The texts below are copied from each installed distribution's declared\n"
            "licence/notice files. Reviewed overrides come from the checked-in files\n"
            "under packaging/attribution/notices/; attribution_overrides.json records\n"
            "why the installed metadata was insufficient in those cases.\n"
        )
    installed = _installed()
    emitted: set[Path] = set()
    for record in inventory.distributions:
        for relative in record.notice_files:
            path = _notice_path(record, relative, installed, NOTICES_ROOT)
            if path is None or not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in emitted:
                continue
            emitted.add(resolved)
            lines.append(
                "\n"
                "--------------------------------------------------------------------------------\n"
                f"{record.name} {record.version} - {relative}\n"
                "--------------------------------------------------------------------------------\n\n"
            )
            data = path.read_bytes()
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                # Licence texts in older wheels are occasionally Latin-1.
                # That codec is total, so no character is silently replaced.
                text = data.decode("latin-1")
            lines.append(text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n")
    return "".join(lines)


# -- What the bundle actually contains --------------------------------------


def write_bundle_modules(path: Path, profile: str, top_levels: Iterable[str]) -> None:
    """Record the top-level names PyInstaller collected. Called from
    qicom.spec, which is the only place that knows the real answer."""
    payload = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "profile": profile,
        "top_level": sorted({str(name) for name in top_levels}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_bundle(inventory: Inventory, bundle_modules: dict) -> list[str]:
    """Every attribution problem in this build, as printable lines.

    Four kinds, all of which used to be invisible: a distribution in the
    bundle that the inventory does not describe; a redistributed build
    runtime omitted from the inventory; an inventory entry missing one of the
    four facts attribution needs; and an inventory built for a different
    profile than the bundle.
    """
    problems: list[str] = []

    bundle_profile = str(bundle_modules.get("profile", ""))
    if bundle_profile != inventory.profile:
        problems.append(
            f"inventory describes profile {inventory.profile!r} but the bundle was built "
            f"as {bundle_profile!r}"
        )

    owners = inventory.top_level_owners()
    unmapped = sorted(
        name
        for name in bundle_modules.get("top_level", [])
        if name not in owners and name not in _NON_DISTRIBUTION_TOP_LEVELS
    )
    if unmapped:
        problems.append(
            "bundled top-level modules belong to no inventoried distribution: "
            + ", ".join(unmapped)
        )

    inventoried = {canonicalize_name(record.name) for record in inventory.distributions}
    required_build_runtime = {
        canonicalize_name(str(name)) for name in bundle_modules.get("build_runtime", [])
    }
    missing_build_runtime = sorted(required_build_runtime - inventoried)
    if missing_build_runtime:
        problems.append(
            "redistributed build runtime absent from the inventory: "
            + ", ".join(missing_build_runtime)
        )

    for record in inventory.distributions:
        missing = record.missing_fields()
        if missing:
            problems.append(
                f"{record.name} {record.version or '<no version>'} has no "
                + ", ".join(missing)
                + " - a redistributed component must carry all four"
            )
    return problems


# -- CLI --------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _generate(arguments: argparse.Namespace) -> int:
    try:
        inventory = build_inventory(
            arguments.profile,
            include_build_runtime=arguments.include_build_runtime,
        )
    except RuntimeError as exc:
        print(f"Attribution generation failed: {exc}", file=sys.stderr)
        return 1
    output = arguments.output_dir
    _write_json(output / "THIRD_PARTY_INVENTORY.json", inventory.to_dict())
    _write_json(output / "qicom-sbom.cdx.json", render_sbom(inventory))
    (output / "THIRD_PARTY_LICENSES.txt").write_text(
        render_license_file(inventory), encoding="utf-8"
    )

    incomplete = [
        f"{record.name}: missing {', '.join(record.missing_fields())}"
        for record in inventory.distributions
        if record.missing_fields()
    ] + missing_notice_texts(inventory)
    if incomplete and not arguments.allow_incomplete:
        print(
            f"Attribution incomplete for profile {arguments.profile!r} - "
            "these need a reviewed manual entry before the bundle is distributed:",
            file=sys.stderr,
        )
        for line in incomplete:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print(
        f"Attribution for {arguments.profile}: {len(inventory.distributions)} "
        f"distributions -> {output}"
    )
    return 0


def _check_bundle(arguments: argparse.Namespace) -> int:
    inventory = Inventory.from_dict(json.loads(arguments.inventory.read_text(encoding="utf-8")))
    bundle_modules = json.loads(arguments.bundle_modules.read_text(encoding="utf-8"))
    problems = check_bundle(inventory, bundle_modules)
    if problems:
        print("Bundle attribution check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"Bundle attribution OK: {len(bundle_modules.get('top_level', []))} bundled "
        f"top-level modules map to {len(inventory.distributions)} inventoried distributions."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    generate = subcommands.add_parser("generate", help="Write the inventory, SBOM and licences.")
    generate.add_argument("--profile", required=True, choices=sorted(PROFILE_EXTRAS))
    generate.add_argument("--output-dir", type=Path, default=REPO_ROOT / "packaging/attribution")
    generate.add_argument(
        "--include-build-runtime",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include PyInstaller, whose bootloader/runtime is redistributed in a frozen app.",
    )
    generate.add_argument(
        "--allow-incomplete",
        action="store_true",
        help=(
            "Write the files even when a distribution is missing a version, origin, "
            "licence or notice. For inspecting a broken environment only - a build "
            "that distributes the result would be shipping unattributed code."
        ),
    )
    generate.set_defaults(handler=_generate)

    check = subcommands.add_parser(
        "check-bundle", help="Compare a frozen bundle against its inventory."
    )
    check.add_argument("--inventory", type=Path, required=True)
    check.add_argument("--bundle-modules", type=Path, required=True)
    check.set_defaults(handler=_check_bundle)

    arguments = parser.parse_args(argv)
    handler = arguments.handler
    assert callable(handler)
    return int(handler(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
