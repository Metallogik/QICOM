# QICOM documentation

**User manual** — how to use QICOM, step by step, with pictures:

| Language | Manual |
|---|---|
| English | [en/USER_MANUAL.md](en/USER_MANUAL.md) |
| Italiano | [it/USER_MANUAL.md](it/USER_MANUAL.md) |
| Français | [fr/USER_MANUAL.md](fr/USER_MANUAL.md) |
| Deutsch | [de/USER_MANUAL.md](de/USER_MANUAL.md) |

English is the reference: a change goes into `en/` first, then into the other three.

## Website and PDF

```bash
python documentation/build_manual.py
```

writes, under `build/manual/` (not tracked by Git):

- `site/` — one HTML page per language, the images and the PDF, ready to upload as they are;
- `QICOM-User-Manual.pdf` — one file with all four languages, cover and language index first;
- `qicom-manual-site.zip` — the `site/` folder in one archive.

It needs `markdown-it-py` and, for the PDF, Google Chrome or Chromium (`CHROME=/path/to/chrome`
if it is not on `PATH`). `--no-pdf` builds the pages only.

## Images

`images/screens/<lang>/` are real screenshots of the application in that language;
`images/diagrams/<lang>/` are the drawings (SVG). Both use a synthetic phantom and invented
DICOM data: no patient images.
