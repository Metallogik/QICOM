# Distribution requirements

What has to be true before a QICOM bundle is handed to anyone, and which parts
of it the repository can enforce on its own.

## Compatibility boundary

Reviewed on 2026-09-16. QICOM targets the desktop platforms below; support means
the stated checks for the stated environment, not every older OS on which Python
or Qt can start. Resource profiles can reduce retained memory and concurrency;
they cannot lower the OS, ABI or instruction-set requirements of a dependency.

The source baseline is **Python >=3.11** and, for the GUI,
**PySide6-Essentials >=6.6,<7** (Qt 6). These are dependency constraints, not a
claim that every permitted combination has been tested. Python 3.11 and 3.12 are
in the CI matrix; full learned-metric tests and all bundle jobs use Python 3.12.
Newer Python versions are not covered by that matrix. A standalone bundle
includes its interpreter; the recipient does not need to install Python.

### Desktop targets and their limits

| Target | Minimum/boundary that can be stated | What QICOM verifies |
| --- | --- | --- |
| Windows x86-64 | Qt 6.6 and 6.11 list Windows 10 **1809 or later** and Windows 11. This is the GUI dependency floor; the chosen Python and native libraries must also support the OS. Windows 7/8/8.1 and 32-bit Windows are outside the desktop target. | The bundle job uses `windows-latest` x64. It smoke-tests the frozen executable on that runner, not on Windows 10 and 11 desktops separately. Those desktop versions remain expected compatibility, not independently tested QICOM environments. |
| macOS Apple Silicon (`arm64`) | The QICOM bundle target is **macOS 14 arm64**, matching `macos-14`. Older macOS is not a verified bundle target: Qt 6.6 supported macOS 11–14, but the PySide6-Essentials 6.11.2 wheel already requires macOS 13+. Other native wheels may raise this floor. | A native arm64 build and frozen smoke test are configured on macOS 14. Compatibility with newer macOS is expected but requires a check on the recipient's OS. No Intel or universal QICOM bundle is configured. |
| Linux x86-64 | The local verified source environment is **Ubuntu 24.04, glibc 2.39**. Packaging uses `ubuntu-latest`; no older-distribution minimum for its resulting bundle is certified. Qt's wheel floor alone is insufficient to determine the bundle floor. | Linux runs the source suite and has a native bundle/smoke job. Other distributions, older glibc, Wayland-specific behavior and actual display drivers are not covered merely by an offscreen pass. |

The Qt floors come from the versioned
[Qt 6.6 platform table](https://doc.qt.io/archives/qt-6.6/supported-platforms.html),
the [Qt 6.11 platform table](https://doc.qt.io/qt-6.11/supported-platforms.html),
and the [PySide6-Essentials 6.11.2 wheel tags](https://pypi.org/project/PySide6-Essentials/6.11.2/#files).
They describe Qt/PySide, not the complete QICOM dependency closure. For example,
that PySide release publishes `manylinux_2_34_x86_64` and
`macosx_13_0_universal2` wheels; neither certifies an entire QICOM bundle for
glibc 2.34 or macOS 13, nor makes QICOM's arm64 bundle run on an Intel Mac.
Even a Qt minor update can change the OS floor: the Qt 6.11 table identifies
Qt 6.12 as the last series planned to support Windows 10. The `<7` constraint
therefore does not promise Windows 10 support for every future allowed version.

`ubuntu-latest` and `windows-latest` are moving labels. At this review the
[GitHub runner image table](https://github.com/actions/runner-images#available-images)
maps them to Ubuntu 24.04 x64 and Windows Server 2025 x64. Record the actual image
version from each run's setup log; a Windows Server smoke result is not a
Windows 10/11 desktop test. `macos-14` fixes the OS major version and architecture,
but its installed toolchain and dependency resolution can still change.

PyInstaller does not include glibc in a Linux bundle. A bundle built on a newer
distribution can therefore fail on an older one even when its Python packages
have compatible wheel tags. Supporting an older Linux target requires building
and testing the complete dependency set on that target, including Qt runtime
libraries and graphics drivers. The corresponding native-library constraint
also applies to macOS. See
[PyInstaller's platform notes](https://pyinstaller.org/en/stable/usage.html#platform-specific-notes).

Linux ARM, Windows ARM, Intel macOS, emulation layers, mobile systems and 32-bit
targets have no QICOM build/test matrix here. Upstream Qt support for one of them
does not establish QICOM support. There is no Qt 5 fallback: reaching an OS below
the Qt 6 baseline would be a separate platform port and dependency validation,
not a runtime-profile setting.

### Dependency constraints by profile

The authoritative ranges are in [pyproject.toml](../pyproject.toml), with install
entry points in [requirements/](../requirements/). A resolver must satisfy both
these ranges and each distribution's Python, OS and architecture requirements.
Native libraries must actually load; a successful resolution or `pip check`
alone is not an execution test.

| Component/profile | Declared constraints relevant to compatibility |
| --- | --- |
| Core | `numpy>=1.26,<3`, `scipy>=1.11,<2`, `scikit-image>=0.22,<1`, `pandas>=2.1,<4`, `Pillow>=12.3,<13`, `python-gdcm>=3.0.10,<4`; their native wheels and numerical runtimes also constrain the platform. python-gdcm (the lossless-JPEG and JPEG-LS DICOM decoder, about 16 MB of native libraries) publishes wheels for Linux x86_64/aarch64 (manylinux_2_28), macOS and Windows x64; its wheel carries only its own Apache-2.0 licence, so GDCM's and those of the libraries it compiles in are a reviewed entry in `packaging/attribution_overrides.json`. The remaining I/O/reporting requirements are in `pyproject.toml`. |
| GUI; packaged `base` | `PySide6-Essentials>=6.6,<7`, `pyqtgraph>=0.13,<1`. Essentials supplies the Qt modules used by QICOM; Addons is not required. |
| Radiomics; packaged `full` | `SimpleITK>=2.3,<3`, `PyWavelets>=1.4,<2`, and PyRadiomics at upstream commit `6a761c4eb01905cfcb1139b0b158547c55edd4df`. Its compiled extension must be built for the selected interpreter/architecture; the source install requires a suitable build toolchain. |
| Optional `learned` | `torch>=2.2,<3`, `torchvision>=0.17,<1`, `lpips==0.1.4`, `piq==0.8.0`. Torch and torchvision must be a compatible pair for the platform. The packaging workflow installs CPU wheels on Windows/Linux; macOS uses its normal PyPI wheels. QICOM does not require a CUDA GPU. |
| Optional MIRP | `mirp==2.7.0`; explicitly installed and outside the default bundle profiles and the standard CI jobs. Its transitive requirements are audited separately, in the `ibsi-mirp` CI job and the `ibsi-mirp-gate` tag gate, which also verify its six IBSI 1 manifests. |

These lower bounds are not a tested lock assembled from all minimum versions.
For example, Python 3.11 may resolve different NumPy/SciPy versions from Python
3.12. The full profile includes the radiomics backend; learned libraries are an
independent packaging switch. This differs from the CI job named `test-base`,
which includes GUI and radiomics but omits learned libraries.

For a specific bundle, use the generated `base`, `base+learned`, `full` or
`full+learned` installed-environment manifest beside it, not this range table.
`generate_constraints.py` records versions and VCS origins without resolving
again. Its environment tag is not an OS-minimum certification; keep the runner
image, native build requirements and smoke result with the same revision.
Changing dependency versions requires renewed validation before repeating a
compatibility claim. No dependency is downgraded automatically to fit an old OS.

### Build and verification matrix

These are the jobs implemented in [ci.yml](../.github/workflows/ci.yml) and
[package.yml](../.github/workflows/package.yml). A configured job is not evidence
that a particular run completed successfully.

| Job | Platform / Python | Checks performed by the job |
| --- | --- | --- |
| `test-base` | `ubuntu-latest` x64 / 3.11 and 3.12 | Installed GUI/radiomics environment, dependency audit and source suite without learned libraries |
| `test` | `ubuntu-latest` x64 / 3.12 | Full source suite, offscreen GUI tests, static gates, branch coverage, learned checkpoints and IBSI reference checks |
| `package` — `QICOM-windows-x64` | `windows-latest` x64 / 3.12 | Native frozen build, attribution check, smoke checks and Windows executable metadata |
| `package` — `QICOM-macos-arm64` | `macos-14` arm64 / 3.12 | Native frozen build, attribution check and smoke checks |
| `package` — `QICOM-linux-x64` | `ubuntu-latest` x64 / 3.12 | Native frozen build, attribution check and smoke checks |
| `ibsi-gate` | `ubuntu-latest` x64 / 3.12 | Tag-only reference gate; manual package runs skip this job |

Tag packaging selects all three platforms, full profile and learned libraries.
Manual runs select a platform/profile; the default platform selection is macOS
plus Windows. Upload occurs after the checks, and artifacts are retained for
seven days. A Windows or macOS bundle job does **not** run the complete pytest
suite. The frozen smoke imports the controller/backends, constructs a workbook
and a Qt/pyqtgraph image widget, and checks build identity. When learned libraries
are included, it checks LPIPS model initialization with an empty cache and after
explicit checkpoint preparation. Its `computed` status means the model loaded;
the probe does not evaluate an image pair. It is not a numerical regression suite,
a manual review of all workflows or a physical mixed-DPI monitor test.

Verification snapshot for this review: the unchanged application code from the
hardware-adaptation work passed **3433 tests, with 7 skips**, on Ubuntu 24.04.5
x86-64, Python 3.12.3 and Qt/PySide6 6.11.2. GUI DPI checks used virtual screens.
This is local source-test evidence, not a newly produced frozen bundle. No
successful Windows/macOS workflow-run receipt was available to this review, so
the table above does not attest a current cross-platform release.

To call a delivered artifact tested, retain its revision, profile, actual runner
image/architecture, dependency manifest, successful build/smoke logs and archive
checksum. Compatibility with an additional OS version remains expected until
that artifact is exercised there. Raising or lowering a support floor requires
updating this document and [the tester guide](LEGGIMI-TESTER.txt) with that evidence.

## What the build already proves

| Property | Enforced by |
| --- | --- |
| The bundle knows which commit produced it | `packaging/generate_build_identity.py`, asserted on the frozen smoke output |
| The version is consistent and describes its own changes | `packaging/check_release_consistency.py` (`--require-tag` on tags) |
| Every redistributed component, including the PyInstaller runtime, is inventoried with version, origin, licence and reproduced notice text | `packaging/attribution.py generate --include-build-runtime` |
| The bundle contains nothing the inventory does not describe | `packaging/attribution.py check-bundle` |
| The bundle was built from a fixed dependency set: every distribution is installed from its profile and platform's lock, with a SHA-256 checked by pip | `requirements/lock/`, `packaging/generate_locks.py --install` (`--require-hashes`); `--check` in the static CI gate keeps the locks consistent with `pyproject.toml` |
| The radiomics features match the official IBSI reference, on the dependency set of the Linux bundle | the `ibsi-gate` job, installed from the Linux `full` lock and fail-closed on tags (`ibsi-mirp-gate` verifies the optional MIRP backend in its own environment) |
| The dependency versions and VCS origins each bundle was built with are recorded | `packaging/generate_constraints.py` |
| Every file's integrity can be checked after download | `packaging/generate_sha256sums.py` |
| Every bundle's dependency closure is published as an SBOM | CycloneDX from `pip-audit`, uploaded beside the bundle and attached to the release |
| A tag's bundles, checksums and SBOMs outlive the artifact retention | the `release` job creates a draft GitHub Release; the maintainer publishes it |
| Learned-model initialization follows the documented cache policy | the empty-home and prepared-checkpoint frozen smoke |

Two limits remain. PyRadiomics is installed from a Git commit, which pip cannot
hash: it is pinned by that commit (a content hash of the repository) and
installed without dependencies after the lock, and its compiled extension is
built from source on each runner. The IBSI gate verifies the Linux lock in a
source install, not the frozen bundles themselves; the Windows and macOS locks
resolve the same versions where wheels exist, but are not separately verified
against IBSI. Receipts record the PyRadiomics, PyWavelets and torch versions, so
a result still names the backend versions that produced it.

Regenerate the locks with `python packaging/generate_locks.py` after changing a
dependency (existing pins are kept), or with `--upgrade` to move every pin to the
newest allowed version; review the diff like any other change.

## What it cannot prove: publisher identity

**SHA256SUMS is an integrity check, not a signature.** It tells a user that
the file they downloaded is the file that was uploaded. It says nothing about
who uploaded it, and it cannot: an attacker who replaces the archive can
replace the checksum next to it.

Establishing *who* published a bundle needs a publicly trusted identity, which
is a purchase and an identity verification rather than a code change:

### Windows - Authenticode

- An OV or EV code-signing certificate from a CA in the Microsoft Trusted Root
  Program. EV certificates are held on hardware tokens or an HSM.
- `signtool sign /fd SHA256 /tr <timestamp url> /td SHA256` over `qicom.exe`
  and every bundled DLL, before the archive is created.
- Timestamping is not optional: without it every signature expires with the
  certificate.
- Until this exists, SmartScreen shows an unknown-publisher warning, and the
  reputation that suppresses it accrues per signing certificate.

### macOS - Developer ID and notarization

- An Apple Developer ID Application certificate.
- `codesign --deep --options runtime --timestamp` over `QICOM.app`, then
  `notarytool submit --wait`, then `stapler staple`.
- Without notarization, Gatekeeper refuses to open the app on a default macOS
  configuration; the workaround a user has to be talked through is exactly the
  habit that makes malware distribution work.

### Linux

- No platform-level signing requirement. If a distribution package is ever
  produced, it is signed with the repository's own key.

## What this repository deliberately does not do

It does not generate, store or improvise certificates or credentials, and it
does not put secrets in the packaging workflow. Signing is a step the
distribution owner adds once a real identity exists; the sequence is:

1. build and verify (everything in the table above);
2. **sign** — the step that is missing;
3. checksum the signed artifacts;
4. publish.

Signing before step 3 matters: a checksum taken before signing describes a
file nobody will ever download.

## Statements that must not appear

QICOM is not a medical device and makes no claim of clinical validation or
regulatory conformity. No release note, store listing or bundled document may
say or imply otherwise. `qicom/about.py`'s `INTENDED_USE_SUMMARY` is the
wording to reuse, and `tests/test_about.py` asserts it stays.
