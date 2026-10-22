# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for QICOM's desktop GUI.

Produces a one-folder (onedir) bundle - onedir starts faster than onefile
(no self-extraction step) and keeps the shipped data files inspectable,
which matters for a scientific/medical tool where it should be obvious
exactly which metric set/locale files were bundled.

Set ``QICOM_PACKAGE_PROFILE=base`` for the lightweight quantitative/
qualitative GUI, or ``QICOM_PACKAGE_PROFILE=full`` (the default) to include
the dedicated radiomics backend. PyRadiomics contains compiled feature-
matrix extensions and runtime schema data, so the full profile names only
those runtime-discovered modules and schemas explicitly.

By default this EXCLUDES the heavy learned-metric backends (torch,
torchvision, lpips, piq) - matching pyproject.toml's `gui` extra, not
`learned`. Those libraries are only imported lazily inside the specific
metric functions that need them (qicom/metrics/reference_structural.py),
so excluding them here only removes MS-SSIM/FSIM/GMSD/VIF/LPIPS/DISTS from
the packaged app - every other metric (including BRISQUE/NIQE, a from-
scratch numpy/scipy reimplementation - see qicom/metrics/nss.py - needing
no `learned` extra at all), plus loading/ROI/session/export, is unaffected.
Calling an excluded metric raises ImportError naming the missing library,
exactly as it does in an unpackaged dev install missing that extra (see
qicom/metrics/catalog.py).

Set QICOM_INCLUDE_LEARNED_METRICS=1 before running `pyinstaller` to bundle
them instead - roughly 120 MB more, most of it torch's own binaries.
Requires `pip install -e ".[gui,learned]"` first either way; this spec does
not install anything itself, only packages what's already importable in the
active environment.

Model *weights* are never bundled, in either mode - the policy is on-demand
with explicit consent. The two metrics that need them (LPIPS, DISTS) would
add ~760 MB, more than the rest of the application combined, so they are
downloaded once, when the user asks, into a per-user directory and verified
against a pinned SHA-256; see qicom/metrics/learned_weights.py and
packaging/runtime_torch_home.py. The other five torch-backed metrics
(MS-SSIM, FSIM, GMSD, VIF, HaarPSI) are classical algorithms with no
weights and work as soon as the application is installed.

This spec therefore does NOT require the checkpoints to be present, and does
not check for them: a learned bundle built on a machine that has never
downloaded one is correct and complete. The packaging workflow proves the
consequence on the frozen bundle - run with an empty home, LPIPS reports
"unavailable-offline" with no network access; run again after an explicit
`packaging/prepare_learned_weights.py`, it computes.

Usage (from the repository root):
    QICOM_PACKAGE_PROFILE=base pyinstaller packaging/qicom.spec
    QICOM_PACKAGE_PROFILE=full pyinstaller packaging/qicom.spec
Output lands in dist/qicom-base/ or dist/qicom-full/. On macOS the selected
tree is additionally wrapped as dist/QICOM.app.

PyInstaller does not cross-compile: run this on each target OS (Windows,
macOS, Linux) to produce that OS's build - a Linux build only runs on
Linux, etc.
"""

import json
import os
import re
import runpy
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

REPO_ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is injected by PyInstaller

_LEARNED_LIBS = ["torch", "torchvision", "lpips", "piq"]
_RADIOMICS_HIDDENIMPORTS = [
    "radiomics.base",
    "radiomics.featureextractor",
    "radiomics.firstorder",
    "radiomics.generalinfo",
    "radiomics.glcm",
    "radiomics.gldm",
    "radiomics.glrlm",
    "radiomics.glszm",
    "radiomics.imageoperations",
    "radiomics.ngtdm",
    "radiomics.schemas.schemaFuncs",
    "radiomics.shape",
    "radiomics.shape2D",
]
# Optional libraries present only because the development environment has
# them installed. QICOM does not import them at runtime; excluding them keeps
# the desktop bundle substantially smaller and avoids shipping test tooling.
_UNUSED_OPTIONAL_LIBS = [
    "IPython",
    "_pytest",
    # Links against libcublas/libcublasLt, which PyInstaller then collects:
    # on a development machine that happens to have bitsandbytes installed
    # this dragged ~570 MB of CUDA runtime into a CPU-only build. Neither
    # QICOM nor any declared dependency imports it.
    "bitsandbytes",
    # OpenCV, ~190 MB with its bundled libraries. Not a dependency of QICOM
    # or of pyradiomics (which needs numpy/SimpleITK/PyWavelets/pykwalify/six).
    "cv2",
    # The optional MIRP backend and its ITK stack: outside every bundle profile
    # (packaging/DISTRIBUTION.md), yet a local `full` build collected them
    # whenever the development environment had the `mirp` extra installed.
    "itk",
    "llvmlite",
    "matplotlib",
    "mirp",
    "numba",
    "pyarrow",
    "pytest",
    "tkinter",
]
_include_learned = os.environ.get("QICOM_INCLUDE_LEARNED_METRICS") == "1"
_package_profile = os.environ.get("QICOM_PACKAGE_PROFILE", "full").strip().lower()
if _package_profile not in {"base", "full"}:
    raise ValueError("QICOM_PACKAGE_PROFILE must be 'base' or 'full'")
_include_radiomics = _package_profile == "full"

try:
    from importlib.metadata import PackageNotFoundError, version as _distribution_version

    _APP_VERSION = _distribution_version("qicom")
except PackageNotFoundError:
    # A direct local PyInstaller invocation may precede `pip install -e .`.
    # Preserve the real version instead of emitting misleading 0.0.0 metadata.
    _APP_VERSION = str(
        runpy.run_path(str(REPO_ROOT / "qicom" / "__version__.py"))["__version__"]
    )


def _windows_version_tuple(version):
    """Turn a PEP 440-style release into Windows' four 16-bit integers."""
    match = re.match(r"(?:\d+!)?(\d+(?:\.\d+){0,3})", version)
    if match is None:
        raise ValueError(f"QICOM version has no numeric release component: {version!r}")
    release = [int(component) for component in match.group(1).split(".")]
    release.extend([0] * (4 - len(release)))
    if any(component > 0xFFFF for component in release):
        raise ValueError(f"QICOM version cannot be represented by Windows: {version!r}")
    return tuple(release)


# Authenticode is deliberately a separate release step: it requires a
# publicly trusted publisher identity. This resource is free, is embedded by
# PyInstaller before any future signing step, and makes the executable's owner
# and purpose explicit in Explorer, inventory systems and EDR consoles.
_WINDOWS_VERSION_INFO = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    _numeric_version = _windows_version_tuple(_APP_VERSION)
    _WINDOWS_VERSION_INFO = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_numeric_version,
            prodvers=_numeric_version,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",  # English (US), Unicode
                        [
                            StringStruct("CompanyName", "Fabio Mattiussi"),
                            StringStruct(
                                "FileDescription",
                                "QICOM medical-image quality assessment application",
                            ),
                            StringStruct("FileVersion", _APP_VERSION),
                            StringStruct("InternalName", "qicom"),
                            StringStruct(
                                "LegalCopyright", "Copyright 2026 Fabio Mattiussi"
                            ),
                            StringStruct("OriginalFilename", "qicom.exe"),
                            StringStruct("ProductName", "QICOM - Quality Image COMparator"),
                            StringStruct("ProductVersion", _APP_VERSION),
                            StringStruct(
                                "Comments",
                                "Research and quality-assessment software; not a medical device.",
                            ),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

_expected_attribution_profile = (
    f"{_package_profile}+learned" if _include_learned else _package_profile
)
_generated_licence = REPO_ROOT / "build" / "attribution" / "THIRD_PARTY_LICENSES.txt"
_licence_source = (
    _generated_licence
    if os.environ.get("QICOM_ATTRIBUTION_PROFILE") == _expected_attribution_profile
    and _generated_licence.is_file()
    else REPO_ROOT / "packaging" / "THIRD_PARTY_LICENSES.txt"
)
if _licence_source != _generated_licence:
    # Said out loud: a local build used to fall back without a word to the
    # checked-in reference copy, which does not describe this environment, and
    # nothing compared the bundle with an inventory (attribution.py check-bundle).
    print(
        "qicom.spec: WARNING - not a verified release build. The licence file is the "
        "checked-in reference copy, not an inventory of this environment, and the bundle is "
        "not checked against one. For a distributable build run packaging/attribution.py "
        f"generate and set QICOM_ATTRIBUTION_PROFILE={_expected_attribution_profile}."
    )

datas = [
    (str(REPO_ROOT / "qicom" / "gui" / "i18n" / "locales"), "qicom/gui/i18n/locales"),
    (str(REPO_ROOT / "qicom" / "gui" / "themes"), "qicom/gui/themes"),
    (str(REPO_ROOT / "qicom" / "metrics" / "descriptors"), "qicom/metrics/descriptors"),
    # The RIQE model is bundled (CC BY 4.0) and verified by SHA-256 before each load.
    (str(REPO_ROOT / "qicom" / "metrics" / "models"), "qicom/metrics/models"),
    (str(REPO_ROOT / "qicom" / "gui" / "resources" / "icons"), "qicom/gui/resources/icons"),
    # The workflow-generated file matches the selected profile and contains
    # the licence texts copied from the exact distributions being frozen.
    # A direct developer build falls back to the checked-in reference copy.
    (str(_licence_source), "."),
    # Apache-2.0 requires both files to travel with every distributed
    # copy - the About dialog names them, so a bundle without them
    # would point at documents the user cannot open.
    (str(REPO_ROOT / "LICENSE"), "."),
    (str(REPO_ROOT / "NOTICE"), "."),
    # Canonical scores cite the methods and the separately acquired model.
    (str(REPO_ROOT / "NO_REFERENCE_METHODOLOGY.md"), "."),
    (str(REPO_ROOT / "metric_inventory.csv"), "."),
    (str(REPO_ROOT / "packaging" / "notices"), "notices"),
]
_APP_ICON = str(REPO_ROOT / "qicom" / "gui" / "resources" / "icons" / "qicom_logo.png")
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = []
#: Import name -> how PyInstaller should collect it, for the rare package that
#: cannot be served out of the PYZ archive. See the lpips entry below.
module_collection_mode: dict[str, str] = {}

# Written by packaging/generate_build_identity.py just before this build, and
# read at runtime by qicom.build_identity. Named explicitly rather than left to
# the module graph: it is imported inside a try/except (it legitimately does
# not exist in a development checkout), and a bundle that silently dropped it
# would be unable to say which commit produced it.
if (REPO_ROOT / "qicom" / "_build_identity.py").exists():
    hiddenimports.append("qicom._build_identity")
else:
    print(
        "qicom.spec: no qicom/_build_identity.py - this bundle will not be able to "
        "report the commit it was built from. Run "
        "'python packaging/generate_build_identity.py' first."
    )
# pydicom loads its decoder plugins by import path, and its GDCM plugin imports
# gdcm inside a try. PyInstaller's pydicom hook follows that today; named here
# as well because a bundle without GDCM would refuse lossless JPEG - most CT
# and MRI exports - with nothing else failing. The smoke test decodes a frame.
hiddenimports += ["pydicom.pixels.decoders.gdcm", "gdcm"]
excludes: list[str] = list(_UNUSED_OPTIONAL_LIBS) + [
    "PySide6.QtTest",
    "openpyxl.tests",
    "PIL.AvifImagePlugin",
    "pywt.tests",
    "radiomics.scripts",
]

# pandas' openpyxl writer and PyWavelets' compiled CWT extension are covered
# by maintained PyInstaller hooks. Do not collect either whole distribution:
# doing so copied tests, docs and multi-megabyte MATLAB fixtures into the app.
if _include_radiomics:
    hiddenimports += _RADIOMICS_HIDDENIMPORTS
    datas += collect_data_files(
        "radiomics",
        includes=["schemas/*.yaml"],
    )
    # The IBSI 1 manifests the verification window loads; without them a
    # frozen build could not verify anything (tests/test_packaging_profiles.py
    # compares this list with the package data in pyproject.toml).
    datas.append(
        (
            str(REPO_ROOT / "qicom" / "radiomics" / "reference_sets"),
            "qicom/radiomics/reference_sets",
        )
    )
else:
    excludes += [
        "SimpleITK",
        "pykwalify",
        "pywt",
        "radiomics",
        "ruamel.yaml",
        "qicom.gui.radiomics_controller",
        "qicom.gui.radiomics_main_window",
        "qicom.radiomics",
    ]

if _include_learned:
    # torch and torchvision have first-party PyInstaller hooks; LPIPS and PIQ
    # do not, so their dynamically addressed Python modules and non-test
    # package data need an explicit nudge. Collecting all of torch instead
    # would duplicate docs/tests and can pull unrelated accelerator assets
    # from a development environment. What those hooks leave undone - and
    # what one of them now gets wrong - is handled after this loop.
    for lib in ("lpips", "piq"):
        hiddenimports += collect_submodules(
            lib,
            filter=lambda name: ".test" not in name and ".tests" not in name,
        )
        datas += collect_data_files(
            lib,
            excludes=["**/test/**", "**/tests/**"],
        )

    # torchvision is the exception: its hook still nudges `torchvision._C`, a
    # name upstream stopped using. The compiled operators now live in
    # `_C_stable` (and `image_stable` for torchvision.io), loaded through
    # torch.ops.load_library on a path built from torchvision.__file__ - an
    # indirection no static analysis can follow, so under the stale name
    # nothing was collected at all. `import torchvision` then failed in the
    # frozen application with "operator torchvision::nms does not exist":
    # torchvision._meta_registrations registers a fake kernel for
    # torchvision::nms unconditionally, and that operator exists only once the
    # library has been loaded. Match by shape rather than by name, so the next
    # upstream rename cannot repeat this silently.
    _torchvision_libraries = collect_dynamic_libs(
        "torchvision",
        search_patterns=["*.so", "*.pyd", "*.dll", "*.dylib"],
    )
    if not _torchvision_libraries:
        raise RuntimeError(
            "qicom.spec: no compiled libraries found in the installed torchvision. "
            "A learned bundle without them cannot import torchvision at all, so "
            "refuse to build one that would only fail on the user's machine."
        )
    binaries += _torchvision_libraries

    # lpips finds its own bundled linear weights through
    # inspect.getfile(self.__init__) - the code object's file name, which for
    # a module served out of the PYZ archive is the relative path recorded at
    # build time ("lpips/lpips.py"). os.path.abspath then resolved it against
    # the current working directory, and LPIPS died with FileNotFoundError on
    # <cwd>/lpips/weights/v0.1/alex.pth: the weights were collected correctly
    # all along, they were simply looked for somewhere else. Collected as
    # source on disk instead, lpips is imported by CPython's own machinery,
    # which records an absolute file name - which is what inspect.getfile
    # then returns.
    module_collection_mode["lpips"] = "py"
else:
    excludes += _LEARNED_LIBS

a = Analysis(  # noqa: F821 - Analysis/PYZ/EXE/COLLECT are injected by PyInstaller
    [str(REPO_ROOT / "qicom" / "gui" / "app.py")],
    pathex=[str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    hookspath=[],
    hooksconfig={},
    # Always installed, not just for learned builds: the hook decides where
    # on-demand model weights are downloaded to, and that has to be settled
    # before any application code runs.
    runtime_hooks=[str(REPO_ROOT / "packaging" / "runtime_torch_home.py")],
    noarchive=False,
    module_collection_mode=module_collection_mode,
)

# QtGui's generic plugin scan includes a PDF image reader and the virtual
# keyboard even though QICOM uses neither PDF rendering nor QML. Their native
# dependency chains add QtPdf and QtQuick/QML. Filter only these known plugin
# roots after dependency analysis; ordinary image formats, SVG export,
# OpenGL plotting and the software-rendering fallback remain bundled.
_UNUSED_QT_PATH_PARTS = (
    "pyside6/plugins/imageformats/qpdf",
    "pyside6/plugins/platforminputcontexts/qtvirtualkeyboardplugin",
    "pyside6/qt6pdf",
    "pyside6/qt6qml",
    "pyside6/qt6quick",
    "pyside6/qt6virtualkeyboard",
)


def _needed_bundle_entry(entry):
    destination = str(entry[0]).replace("\\", "/").lower()
    return not any(part in destination for part in _UNUSED_QT_PATH_PARTS)


a.binaries = [entry for entry in a.binaries if _needed_bundle_entry(entry)]
a.datas = [entry for entry in a.datas if _needed_bundle_entry(entry)]


def _collected_top_levels():
    """Top-level import names PyInstaller decided to bundle.

    Taken from the analysis result rather than by walking `dist/` afterwards:
    pure-Python modules end up inside the PYZ archive, where a directory scan
    cannot see them at all, so a filesystem-based inventory would silently
    under-report exactly the packages most likely to need attribution.
    """
    names = set()
    # `a.datas` contains destinations such as LICENSE and translation files,
    # not import namespaces. `a.binaries` also contains system DLLs/shared
    # libraries; only EXTENSION entries represent importable Python modules.
    #
    # A module collected as source rather than into the PYZ (see
    # module_collection_mode above) is absent from `a.pure` and reaches the
    # bundle as a data file, so the source files in `a.datas` have to be read
    # back as the import namespaces they are - otherwise switching a package
    # to that mode would quietly retire it from this inventory, which is
    # exactly the kind of silent gap this function exists to close.
    module_entries = (
        list(a.pure)
        + [entry for entry in a.binaries if len(entry) >= 3 and entry[2] == "EXTENSION"]
        + [entry for entry in a.datas if str(entry[0]).endswith((".py", ".pyc"))]
    )
    for entry in module_entries:
        destination = str(entry[0]).replace("\\", "/")
        head = destination.split("/")[0].split(".")[0]
        if head and not head.startswith("_") and head not in sys.stdlib_module_names:
            names.add(head)
    return names


# Recorded for packaging/attribution.py check-bundle, which fails the build if
# the bundle contains a distribution the generated licence inventory does not
# describe. Written next to the build, not into the bundle: it is evidence
# about the build, not a file users need.
_BUNDLE_MODULES_PATH = REPO_ROOT / "build" / "attribution" / f"bundle-modules-{_package_profile}.json"
_BUNDLE_MODULES_PATH.parent.mkdir(parents=True, exist_ok=True)
_BUNDLE_MODULES_PATH.write_text(
    json.dumps(
        {
            "schema_version": 1,
            "profile": (
                f"{_package_profile}+learned" if _include_learned else _package_profile
            ),
            "build_runtime": ["pyinstaller"],
            "top_level": sorted(_collected_top_levels()),
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
print(f"qicom.spec: recorded bundled modules -> {_BUNDLE_MODULES_PATH}")
pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="qicom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_APP_ICON,
    version=_WINDOWS_VERSION_INFO,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=f"qicom-{_package_profile}",
)

if sys.platform == "darwin":
    # Without this, the macOS build is a bare Unix executable: double-clicking
    # dist/qicom-<profile>/qicom in Finder opens a Terminal window alongside the GUI, and
    # the app gets a generic icon and the wrong name in the Dock and menu bar.
    # BUNDLE re-wraps the same COLLECT tree as dist/QICOM.app, which is what a
    # tester can actually drag to /Applications and launch normally.
    app = BUNDLE(  # noqa: F821
        coll,
        name="QICOM.app",
        icon=_APP_ICON,
        bundle_identifier="com.metallogik.qicom",
        version=_APP_VERSION,
        info_plist={
            "CFBundleShortVersionString": _APP_VERSION,
            "NSHighResolutionCapable": True,
            # QICOM ships light, gray and dark QSS themes and follows the
            # user's choice inside the app; letting macOS force the legacy Aqua
            # appearance would fight the dark theme's own palette.
            "NSRequiresAquaSystemAppearance": False,
            # Shown in the Finder "Get Info" panel; kept identical to
            # qicom.about.COPYRIGHT and to the LICENSE/NOTICE files.
            "NSHumanReadableCopyright": "Copyright 2026 Fabio Mattiussi. "
            "Licensed under the Apache License, Version 2.0.",
        },
    )
