# QICOM security

QICOM is a local desktop tool for research and quality control, not a medical device.
The security model treats as untrusted the image files, the project/evaluation archives,
the answer files and any text that can end up in a report.

## Implemented guarantees

- Fail-closed ZIP import against traversal, absolute paths, symbolic links, zip bombs and archives
  beyond the configured limits. Members that are duplicates, or identical up to case, Unicode
  normalisation and trailing dots or spaces, which on NTFS/APFS would end up as the same file, are
  also refused; on Windows, so are device names (`CON`, `LPT1`…) and disallowed characters. The limits are per package type (project, evaluation,
  radiomics, cohort, workbook), because a cohort table and a multi-series project do not carry
  the same risk, and a shared ceiling would in practice leave the small one with no budget. The
  counter acts on the bytes **actually extracted**, not on the sizes declared in the archive,
  and is preceded by a free-space check with a margin: a refusal leaves no partial files
  behind.
- Structural validation of images, manifests and answers before computation or aggregation.
  Answers accepted for aggregation can arrive as QICOM JSON, XLSX or CSV: all three formats are
  treated as untrusted, with a limit on file size and number of records, and are checked
  against the question package before they enter the statistics.
- DICOM anonymisation by default in portable exports, with consistent UIDs, and blocking of
  clinical files that do not explicitly declare the absence of identifying annotations in the
  pixels. Only minimal Secondary Captures without direct identifying attributes are accepted
  when the declaration is missing, to support JPG images converted to DICOM. The rules of the
  PS3.15 profile are also applied to every item of nested sequences (functional groups of
  Enhanced multi-frames, derivation chains, references to the source study); every remaining
  instance UID, date, time or person name is replaced, and the exported copy is checked against
  the UIDs and dates of the original before declaring `PatientIdentityRemoved=YES`.
  DICOMDIRs are not anonymised: their records can name patients in their paths.
  The Part 10 container is rebuilt, not inherited (PS3.15): the 128-byte preamble is zeroed and
  the File Meta group keeps only the elements required by PS3.10 (SOP class and instance,
  transfer syntax, implementation identity); `PrivateInformation`, AE titles and source
  addresses do not survive. The re-read copy is checked for this too.
- A file with a NIfTI suffix is recognised only if its structure is consistent: single-file
  magic, dimensions, a datatype with a known voxel size, a voxel offset beyond the extender, and
  the payload present in full. Everything between header and voxels is zeroed, and bytes after
  the payload are dropped; a file that fails the check is refused like any other file that
  cannot be verified.
- Blinded deliveries: every image is de-identified again for the delivery, and an override that
  the author accepted when saving their own package does not carry over: refused images stop
  the delivery before any file is written, until the author explicitly chooses, for that
  delivery, to include them. The evaluator's package contains nothing that allows the
  randomisation to be reconstructed: the seed mixes in a secret derived from a random key of the
  author, kept private in their profile (`qualitative_blinding_key.json`), and seed, secret and
  master seed live only in the map; pages and questions are numbered in presentation order, and
  the public design reports neither randomisation nor hidden repeats. Deliveries created before
  this version contain the seed and the master seed: whoever received them can reconstruct which
  panel was which and which pages were repeats (see `CHANGELOG.md`).
- Copies of JPG and PNG images in packages lose EXIF, XMP, comments and text chunks without any
  loss of quality (same compressed data, same name and format); text burned into the pixels
  remains beyond the reach of any metadata removal.
- Atomic writes for archives/JSON and SQLite transactions for analyses, results, ROIs and notes.
- Protection against formula injection in Excel and CSV exports (text cells starting with `=`,
  `+`, `-` or `@` get a leading apostrophe) and valid/unique sheet names.
- Only two network accesses, both started explicitly by the user and never by an analysis: the
  download of the learned weights (below) and the download of the official IBSI resources for
  the IBSI verification (raw.githubusercontent.com and ibsi.radiomics.hevs.ch), with SHA-256 and
  size pinned in the manifest and confirmation before the download.
- No implicit download of learned weights during an analysis; admitted checkpoints are verified
  by SHA-256 **and** pinned size. Every transfer is bounded in two independent ways:
  `Content-Length` when the server declares it, and a counter on the bytes received — a server
  willing to send the wrong file is willing to declare the wrong length for it. An overrun, a
  wrong digest or an interruption deletes the temporary file and leaves the already verified copy
  intact. A checkpoint's hash is re-checked every time the model is loaded, not once per session.
- Every result carries in its receipt the exact commit it was produced from (`build_revision`)
  and whether that checkout was modified (`source_dirty`): a version names a release, it does not
  identify the software that computed the numbers.

## Operational limits

- Quantitative ZIPs keep the original file names. NIfTI headers are cleaned as in qualitative
  packages (text fields, extensions, name and date in the gzip header), but file names and the
  free text of manifests need checking before sharing.
- `BurnedInAnnotation=NO` is a statement by the DICOM producer, not an OCR check of the pixels.
  Before sharing real data, the organisation's privacy procedure is still required.
- In minimal Secondary Captures accepted without `BurnedInAnnotation`, QICOM records in the
  de-identification method that the pixel content was not verified. The absence of demographic
  tags does not guarantee that the original JPG contains no visible identifying text.
- When an image cannot be anonymised, for whatever reason, the export writes nothing: QICOM
  collects all the affected images and tells the user which ones they are and why, and that they
  can still proceed without anonymisation for those images (Cancel remains the default button).
  Only this explicit choice exports them: anonymised as far as possible but **not verified**, with
  `PatientIdentityRemoved=NO` and a `DeidentificationMethod` that names the override, and listed
  in the error log; a file that QICOM cannot read or rewrite is written out unchanged.
  Responsibility for sharing passes to the user. The reasons shown and logged name the
  attributes, never the identifying values.
- The error report (Errors window, Help ▸ Report a Bug) is a text file written only on request and
  never sent by QICOM. It contains the selected error messages, the build data and the tail of the
  application log; the messages can mention file names, so read it before sending it.
- Image files are decoded by third-party native libraries. Update the dependencies regularly, run
  `pip-audit`, and rebuild the executables after security updates. CI runs `pip-audit` on both
  the test environment and the one that produces the bundle, and upgrades the installer before
  any dependency: pip is what downloads and runs the build backends, so pinning the versions of
  what it installs does not help if the installer itself is vulnerable.
- Lossless JPEG and JPEG-LS in DICOM are decoded by GDCM (`python-gdcm`), which embeds its own
  copies, older than Pillow's, of libjpeg 6b, CharLS and OpenJPEG 2.3. QICOM hands it only the
  syntaxes that Pillow does not decode: baseline JPEG and JPEG 2000 stay with Pillow
  (`qicom/io/dicom_io.py`). The Linux wheel also includes OpenSSL 1.1.1k (RHEL 8 build, a branch no
  longer supported upstream; on macOS it is OpenSSL 3.5, on Windows it is absent), linked into the
  GDCM core and therefore loaded with it, even though QICOM uses GDCM only to decode pixels and
  opens no connections. `pip-audit` does not see these native libraries: a security advisory on
  GDCM or on the libraries it embeds must be followed by hand, by updating `python-gdcm` and
  regenerating the locks.
- CI artifacts are not signed. A public distribution must add Authenticode on Windows and Apple
  signing/notarisation on macOS, using credentials stored in the CI system — see
  [packaging/DISTRIBUTION.md](packaging/DISTRIBUTION.md) for the exact sequence and for why
  signing must happen **before** the checksums are computed.
- Every bundle comes with the licence inventory (including the PyInstaller runtime, with the
  licence/notice texts reproduced), a CycloneDX SBOM and the manifest of the versions and VCS
  origins actually installed on the build platform, all covered by the same `SHA256SUMS`. The
  environment manifest is not a hashed wheel lock; the build fails if it contains a distribution
  that the inventory does not describe.
- Every CI bundle contains `SHA256SUMS` for its regular files and is published together with the
  SHA-256 checksum of the archive. These values identify the build and allow it to be compared
  with the approved one, but they do not authenticate the publisher: until a trusted signature is
  in place, they must be obtained or confirmed through a trusted company channel.
- Evaluator users (Take Evaluation): each user's paused and finished evaluations are encrypted
  with AES-256-GCM, at every autosave and not only when pausing, with a random key belonging to
  the user. That key is stored encrypted with a key derived from the password (scrypt, N=2^17,
  r=8, p=1); the password is not stored. Every file is bound to its user and to its name: copied
  into someone else's folder, or swapped, it does not open. Inside QICOM a user sees, resumes and
  removes only their own evaluations.
  - Limits: encryption prevents reading or undetected tampering, not deletion. Anyone who shares
    the operating-system account can delete other users' files from the data folder; separate
    operating-system accounts are needed against this.
  - The minimum password length is 5 characters, with no complexity requirements, by design.
    Anyone who copies the files to another computer can try passwords without the delay of the
    login window: scrypt makes every attempt expensive, but a short password remains guessable.
  - No password recovery: forgetting it makes that user's paused evaluations unreadable (the
    package can be taken again from the start).
  - What stays in clear text: the user IDs, the report and the answer file that the evaluator
    saves for the author, and the package images extracted temporarily during the evaluation. The
    key stays in memory while the evaluation window is open.
  - Without logging in ("Continue without logging in") nothing is kept on disk: no pausing or
    resuming, and closing before saving the report loses the answers, after a confirmation.
  - Clear-text drafts from previous versions are not assigned to anyone automatically: they are
    recovered only by whoever, after logging in, reopens the same package and confirms they are
    theirs. They are then encrypted under that user and the clear-text copy is deleted; on SSDs,
    deleting a file does not guarantee that the previous blocks disappear from the disk.
- Access code (optional, Save step): the copy to send to evaluators, the blinded delivery and its
  decoding map are encrypted (AES-256-GCM, key derived from the code with scrypt) and can be
  opened, edited and aggregated only with the code, inside QICOM and outside it. Answers saved for
  a protected package are encrypted with the same code.
  - The code is the same for all evaluators: it determines who can open the evaluation, not who
    answered, and anyone who knows it can pass it on.
  - Minimum 10 characters; QICOM suggests a generated one of 16 (about 79 bits). Anyone who has
    the file can try codes on their own computer without limit: a short or predictable code
    remains guessable.
  - No recovery: once the code is lost, the protected files can no longer be opened (the author's
    working copy remains without a code).
  - It should be communicated over a different channel from the file.
  - During evaluation or aggregation the package is decrypted into a temporary folder, deleted on
    close; a sudden shutdown can leave it on disk.
- Utility tools. They read the sources and never modify them: every result is a copy, and a set
  of files appears in the destination folder all at once or not at all; the folder must be new or
  empty, real (not a link) and different from the sources' folder. Folder scans do not follow
  links.
  - Tag inspection and comparison show the header as it is, patient data included, and the CSV
    export of the table **is not anonymised**. The cells of every CSV are protected against
    formula injection.
  - The tag editor writes copies into a new folder; the tags that define the pixels and group
    0002 cannot be edited, and those that change measurements ask for confirmation. The change
    log contains the new values and the hashes, never the previous values. An edited copy remains
    as identifiable as the source, except for what the user changed.
  - The anonymiser applies the same fail-closed rules as the exports (above): a file that cannot
    be verified stops everything before anything is written, and only the user's explicit choice
    writes it as not verified. The copy keeps the folder tree and file names of the original, in
    `<folder>_anonymized`: the names **are not de-identified**, and a folder or file that carries
    a patient's name carries it in the copy too. The tool says so before proceeding; the names
    must be checked before sharing the copy. For exports whose names identify the patient, an
    option replaces folders and files with generic names (`folder_001`, `image_0001.dcm`, ...) in
    an `anonymized` folder, without a correspondence table.
  - The organised copy of DICOM files names the folders by study order, series number and
    modality, never by patient, but the files **are not anonymised**.
  - DICOM decompression rewrites the full header into every copy, patient data included: the
    copies **are not anonymised**, and the folder is named after the source file. The pixels are
    decoded by the same decoders as the loader (Pillow, then GDCM), and the GDCM security limits
    described above apply here too.
  - Conversions, resamplings, degradations, arithmetic results, masks and segmentations carry
    only pixels and geometry: no header field and no text from the source. The conversion
    creates its folder named after the source image (`<name>_<format>`).
- The local database and the projects are not encrypted at rest. Use disk encryption and
  operating-system access controls when they contain sensitive data.

## Reporting

Do not include clinical images, personal data, keys or credentials in a report. Provide a minimal
reproduction with synthetic data and the versions of QICOM and of the operating system.
