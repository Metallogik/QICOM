"""Build the user manual for the website: one HTML page per language and one PDF with all of them.

The Markdown files in ``documentation/<lang>/`` stay the single source. This script renders them
into a static folder that can be uploaded as it is, and prints the same pages into one PDF with
Chrome, so the website and the download never disagree.

Usage::

    python documentation/build_manual.py            # writes build/manual/
    python documentation/build_manual.py --no-pdf   # HTML only

Requires ``markdown-it-py`` (``pip install markdown-it-py``) and, for the PDF, Google Chrome or
Chromium (set ``CHROME`` to its path if it is not on ``PATH``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

DOC_DIR = Path(__file__).resolve().parent
REPO_DIR = DOC_DIR.parent
REPO_URL = "https://github.com/Metallogik/QICOM/blob/main/"
PDF_NAME = "QICOM-User-Manual.pdf"
LANGUAGES = ("en", "it", "fr", "de")

LABELS = {
    "en": {
        "name": "English",
        "title": "QICOM — User Manual",
        "download": "Download PDF (EN · IT · FR · DE)",
        "disclaimer": (
            "For research and quality-assessment use only - not intended for clinical diagnosis."
        ),
        "contents": "Contents",
    },
    "it": {
        "name": "Italiano",
        "title": "QICOM — Manuale utente",
        "download": "Scarica il PDF (EN · IT · FR · DE)",
        "disclaimer": (
            "Solo per uso di ricerca e controllo qualità - non destinato alla diagnosi clinica."
        ),
        "contents": "Indice",
    },
    "fr": {
        "name": "Français",
        "title": "QICOM — Manuel d'utilisation",
        "download": "Télécharger le PDF (EN · IT · FR · DE)",
        "disclaimer": (
            "Réservé à la recherche et au contrôle qualité - non destiné au diagnostic clinique."
        ),
        "contents": "Sommaire",
    },
    "de": {
        "name": "Deutsch",
        "title": "QICOM — Benutzerhandbuch",
        "download": "PDF herunterladen (EN · IT · FR · DE)",
        "disclaimer": (
            "Nur für Forschung und Qualitätsbewertung - nicht für die klinische Diagnose bestimmt."
        ),
        "contents": "Inhalt",
    },
}

CSS = """
:root { --ink:#1f2328; --muted:#59636e; --line:#d0d7de; --accent:#0f8b7d; --warn:#fff4e5; }
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
       color: var(--ink); line-height: 1.55; background: #fff; }
.bar { position: sticky; top: 0; z-index: 5; background: #fff; border-bottom: 1px solid var(--line);
       display: flex; gap: 12px; align-items: center; flex-wrap: wrap; padding: 10px 20px; }
.bar .langs a { margin-right: 10px; color: var(--muted); text-decoration: none; font-weight: 600; }
.bar .langs a.on { color: var(--accent); border-bottom: 2px solid var(--accent); }
.bar .pdf { margin-left: auto; background: var(--accent); color: #fff; text-decoration: none;
            padding: 8px 14px; border-radius: 8px; font-weight: 700; }
.note { background: var(--warn); border: 1px solid #f0c36d; border-radius: 8px; padding: 8px 14px;
        margin: 16px 0; font-weight: 600; }
main { max-width: 1000px; margin: 0 auto; padding: 8px 20px 60px; }
img { max-width: 100%; height: auto; }
main img { display: block; margin: 12px auto; border-radius: 6px; }
p[align="center"] { display: flex; gap: 2%; justify-content: center; flex-wrap: wrap; }
p[align="center"] img { display: inline-block; margin: 6px 0; }
table { border-collapse: collapse; margin: 12px 0; width: 100%; font-size: 0.95em; }
th, td { border: 1px solid var(--line); padding: 6px 10px; vertical-align: top; text-align: left; }
th { background: #f6f8fa; }
blockquote { margin: 12px 0; padding: 4px 16px; border-left: 4px solid var(--accent);
             background: #f6f8fa; color: var(--ink); }
code { background: #eff1f3; padding: 1px 5px; border-radius: 4px; font-size: 0.92em; }
h1 { font-size: 2em; margin-top: 0.6em; }
h2 { margin-top: 2em; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
h3 { margin-top: 1.6em; }
a { color: var(--accent); }
.cover { text-align: center; padding-top: 60px; }
.cover img { width: 120px; margin: 0 auto 20px; }
.cover h1 { font-size: 2.2em; margin: 0.2em 0; }
.cover .titles { font-size: 1.15em; color: var(--muted); line-height: 1.8; margin: 18px 0 30px; }
.cover .meta { color: var(--muted); margin-top: 30px; }
.cover .note { text-align: left; max-width: 640px; margin: 10px auto; }
.index { max-width: 640px; margin: 0 auto; }
.index a { display: block; font-size: 1.3em; padding: 14px 18px; margin: 10px 0;
           border: 2px solid var(--line); border-radius: 10px; text-decoration: none; }
@media print {
  @page { size: A4; margin: 14mm 12mm; }
  body { font-size: 10.5pt; }
  .bar { display: none; }
  main { max-width: none; padding: 0; }
  .page { break-before: page; }
  h2, h3 { break-after: avoid; }
  img, table, blockquote, p[align="center"] { break-inside: avoid; }
  main img { max-height: 120mm; width: auto; max-width: 100%; }
  p[align="center"] { flex-wrap: nowrap; }
  p[align="center"] img { max-height: 95mm; }
  img[width="32%"] { max-width: 32%; }
  img[width="40%"] { max-width: 40%; }
  img[width="49%"] { max-width: 49%; }
  img[width="55%"] { max-width: 55%; }
  a { color: var(--ink); text-decoration: none; }
}
"""


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading, so links written for GitHub work here too."""
    text = re.sub(r"<[^>]+>", "", html.unescape(heading)).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def render_markdown(lang: str, image_prefix: str, anchor_prefix: str = "") -> str:
    """One manual as an HTML fragment, with its links made to work outside the repository."""
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        sys.exit("markdown-it-py is required: pip install markdown-it-py")

    source = (DOC_DIR / lang / "USER_MANUAL.md").read_text(encoding="utf-8")
    body = MarkdownIt("commonmark", {"html": True}).enable("table").render(source)
    body = body.replace('"../images/', f'"{image_prefix}images/')
    body = re.sub(r'href="\.\./\.\./([^"#]+)"', lambda m: f'href="{REPO_URL}{m.group(1)}"', body)

    def add_id(match: re.Match[str]) -> str:
        level, inner = match.group(1), match.group(2)
        return f'<h{level} id="{anchor_prefix}{slug(inner)}">{inner}</h{level}>'

    body = re.sub(r"<h([1-3])>(.*?)</h\1>", add_id, body)
    if anchor_prefix:
        body = body.replace('href="#', f'href="#{anchor_prefix}')
    return body


def language_bar(current: str, root: str) -> str:
    links = " ".join(
        f'<a href="{root}{code}/" class="{"on" if code == current else ""}">'
        f'{LABELS[code]["name"]}</a>'
        for code in LANGUAGES
    )
    return (
        f'<nav class="bar"><span class="langs">{links}</span>'
        f'<a class="pdf" href="{root}{PDF_NAME}" download>{LABELS[current]["download"]}</a></nav>'
    )


def page(title: str, lang: str, content: str, bar: str = "") -> str:
    return (
        f'<!DOCTYPE html>\n<html lang="{lang}"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{bar}<main>{content}</main></body></html>\n"
    )


def version() -> str:
    sys.path.insert(0, str(REPO_DIR))
    try:
        from qicom.__version__ import __version__
    except ImportError:
        return "unknown"
    return str(__version__)


def build_site(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(DOC_DIR / "images", out / "images")
    for lang in LANGUAGES:
        labels = LABELS[lang]
        content = f'<div class="note">{html.escape(labels["disclaimer"])}</div>' + render_markdown(
            lang, "../"
        )
        (out / lang).mkdir(parents=True)
        (out / lang / "index.html").write_text(
            page(labels["title"], lang, content, language_bar(lang, "../")), encoding="utf-8"
        )
    choices = "".join(
        f'<a href="{code}/">{LABELS[code]["name"]} — {html.escape(LABELS[code]["title"])}</a>'
        for code in LANGUAGES
    )
    index = (
        '<div class="cover"><h1>QICOM</h1></div><div class="index">'
        f"{choices}"
        f'<a href="{PDF_NAME}" download>PDF — EN · IT · FR · DE</a></div>'
    )
    (out / "index.html").write_text(page("QICOM — User Manual", "en", index), encoding="utf-8")


def build_print_html(out: Path) -> Path:
    """Cover, a language index, then each manual from a new page, anchors kept apart.

    Written next to the site, whose images it reuses, so it is never part of the upload.
    """
    out.mkdir(parents=True, exist_ok=True)
    logo = REPO_DIR / "qicom" / "gui" / "resources" / "icons" / "qicom_logo.png"
    shutil.copy(logo, out / "qicom_logo.png")
    titles = "<br>".join(html.escape(LABELS[code]["title"]) for code in LANGUAGES)
    notes = "".join(
        f'<div class="note">{html.escape(LABELS[code]["disclaimer"])}</div>' for code in LANGUAGES
    )
    cover = (
        '<section class="cover"><img src="qicom_logo.png" alt="QICOM">'
        f'<h1>QICOM</h1><div class="titles">{titles}</div>{notes}'
        f'<div class="meta">QICOM {html.escape(version())} · {dt.date.today().isoformat()}'
        "<br>https://github.com/Metallogik/QICOM</div></section>"
    )
    index = '<section class="page index"><h2>EN · IT · FR · DE</h2>' + "".join(
        f'<a href="#{code}-top">{LABELS[code]["name"]} — {html.escape(LABELS[code]["title"])}</a>'
        for code in LANGUAGES
    )
    index += "</section>"
    manuals = "".join(
        f'<section class="page" id="{code}-top" lang="{code}">'
        f"{render_markdown(code, '../site/', anchor_prefix=f'{code}-')}</section>"
        for code in LANGUAGES
    )
    target = out / "print.html"
    target.write_text(page("QICOM — User Manual", "en", cover + index + manuals), encoding="utf-8")
    return target


def find_chrome() -> str:
    candidates = [os.environ.get("CHROME", "")]
    candidates += ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and (shutil.which(candidate) or Path(candidate).is_file()):
            return shutil.which(candidate) or candidate
    sys.exit("Chrome or Chromium not found: set CHROME, or use --no-pdf")


def build_pdf(print_html: Path, target: Path) -> None:
    command = [
        find_chrome(),
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        "--generate-pdf-document-outline",
        f"--print-to-pdf={target}",
        print_html.as_uri(),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=600)
    if not target.is_file() or target.stat().st_size == 0:
        sys.exit("Chrome did not write the PDF")


def zip_site(site: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(site.rglob("*")):
            if path.is_file():
                archive.write(path, Path("manual") / path.relative_to(site))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO_DIR / "build" / "manual")
    parser.add_argument("--no-pdf", action="store_true", help="build the HTML pages only")
    args = parser.parse_args()

    site = args.out / "site"
    build_site(site)
    if not args.no_pdf:
        print_html = build_print_html(args.out / "print")
        build_pdf(print_html, site / PDF_NAME)
        shutil.copy(site / PDF_NAME, args.out / PDF_NAME)
    zip_site(site, args.out / "qicom-manual-site.zip")
    print(f"Site: {site}")
    if not args.no_pdf:
        size = (args.out / PDF_NAME).stat().st_size / 1e6
        print(f"PDF:  {args.out / PDF_NAME} ({size:.1f} MB)")
    print(f"Zip:  {args.out / 'qicom-manual-site.zip'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
