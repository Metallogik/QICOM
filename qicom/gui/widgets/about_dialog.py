"""Help ▸ About QICOM: what the software is, who made it, what it may and
may not be used for, and how to cite it.

Shared, unmodified, by all three workbenches (quantitative, qualitative,
radiomics) - the statements it carries are about QICOM as a whole, so a
per-window variant could only ever drift.

Every fact shown here comes from `qicom.about`, never from a literal typed
into this file: the same version, licence and citation also reach exported
files and the documentation, and one source is what keeps them identical.
The dialog contributes only the presentation and the translations.

Two deliberate exceptions to the translation rule, both in the Legal tab:
the DICOM trademark attribution and the NEMA non-affiliation statement stay
in English in every language, because a trademark notice is reproduced in
the rights holder's own wording; likewise the citation block, which has to
read the same for every reader of the paper that quotes it.
"""

from __future__ import annotations

import html

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from qicom import about
from qicom.gui.resources import app_icon
from qicom.gui.widgets.adaptive_layout import DpiIconLabel, ScreenFitDialog, make_panel_scrollable
from qicom.i18n import translator

_COPIED_FEEDBACK_MS = 1500


def _paragraphs(*blocks: str) -> str:
    return "".join(f"<p>{block}</p>" for block in blocks if block)


def _bullet_list(items_text: str) -> str:
    """Render one newline-separated locale string as an HTML list.

    The alternative - one locale key per bullet - would multiply the number
    of keys a translator has to keep aligned across every locale file for no
    gain, since these lists are always translated as a whole.
    """
    items = [line.strip() for line in items_text.splitlines() if line.strip()]
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>" if items else ""


class AboutDialog(ScreenFitDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.resize(620, 520)

        self._logo_label = DpiIconLabel(app_icon(), 56)
        self._title_label = QLabel(about.APP_TITLE)
        self._title_label.setStyleSheet("font-size: 18px; font-weight: 700;")
        self._version_label = QLabel()
        self._version_label.setStyleSheet("color: palette(mid);")
        self._designed_in_label = QLabel()
        self._designed_in_label.setStyleSheet("color: palette(mid);")

        heading_column = QVBoxLayout()
        heading_column.setSpacing(2)
        heading_column.addWidget(self._title_label)
        heading_column.addWidget(self._version_label)
        heading_column.addWidget(self._designed_in_label)

        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        header_row.addWidget(self._logo_label)
        header_row.addLayout(heading_column, 1)

        self._about_view = self._make_view()
        self._intended_use_view = self._make_view()
        self._legal_view = self._make_view()
        self._privacy_view = self._make_view()
        self._citation_view = self._make_view()

        self._copy_build_button = QPushButton()
        self._copy_build_button.clicked.connect(
            lambda: self._copy(self.build_details_text(), self._copy_build_button)
        )
        about_tab = self._make_tab(self._about_view, self._copy_build_button)

        self._copy_citation_button = QPushButton()
        self._copy_citation_button.setObjectName("primary")
        self._copy_citation_button.clicked.connect(
            lambda: self._copy(about.citation_text(), self._copy_citation_button)
        )
        self._copy_bibtex_button = QPushButton()
        self._copy_bibtex_button.clicked.connect(
            lambda: self._copy(about.citation_bibtex(), self._copy_bibtex_button)
        )
        citation_tab = self._make_tab(
            self._citation_view, self._copy_citation_button, self._copy_bibtex_button
        )

        self._tabs = QTabWidget()
        self._tabs.addTab(about_tab, "")
        self._tabs.addTab(self._intended_use_view, "")
        self._tabs.addTab(self._legal_view, "")
        self._tabs.addTab(self._privacy_view, "")
        self._tabs.addTab(citation_tab, "")

        self._button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._button_box.rejected.connect(self.reject)
        self._button_box.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addLayout(header_row)
        layout.addWidget(self._tabs, 1)
        layout.addWidget(self._button_box)

        make_panel_scrollable(self)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    # -- construction helpers ------------------------------------------

    @staticmethod
    def _make_view() -> QTextBrowser:
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        # No explicit colours anywhere in the generated HTML: the browser
        # inherits the active palette, so the same markup stays readable in
        # the light and the dark theme (see qicom.gui.theming).
        view.setStyleSheet("QTextBrowser { border: none; background: transparent; }")
        return view

    @staticmethod
    def _make_tab(view: QTextBrowser, *buttons: QPushButton) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(view, 1)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        for button in buttons:
            button_row.addWidget(button)
        layout.addLayout(button_row)
        return tab

    # -- actions --------------------------------------------------------

    @staticmethod
    def build_details_text() -> str:
        """Version and environment as one pasteable block - the first thing
        a bug report needs, and the reason this tab has a copy button at all
        (see BugReportDialog, which asks the user for exactly this).

        Untranslated on purpose: it is written to be pasted into an email to
        the author, not read in the interface.
        """
        info = about.build_info()
        return "\n".join(
            [f"{about.APP_NAME} {info['version']}"]
            + [f"{key}: {value}" for key, value in info.items() if key != "version"]
        )

    def _copy(self, text: str, button: QPushButton) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        # Feedback in the button itself rather than a modal confirmation:
        # a QMessageBox for a clipboard copy is noise, and this dialog stays
        # free of nested modals.
        original = button.text()
        button.setText(translator.tr("about.copied"))
        # `button` as the timer's context object, not a bare lambda: closing
        # the dialog within the feedback delay would otherwise leave a
        # pending call into a widget Qt has already deleted.
        QTimer.singleShot(_COPIED_FEEDBACK_MS, button, lambda: button.setText(original))

    # -- content --------------------------------------------------------

    def _about_html(self) -> str:
        info = about.build_info()
        build_kind = translator.tr(
            "about.build.packaged_bundle" if about.is_frozen() else "about.build.source_checkout"
        )
        rows = [
            (translator.tr("about.field.version"), html.escape(about.VERSION)),
            (translator.tr("about.field.build"), html.escape(build_kind)),
            (translator.tr("about.field.author"), html.escape(about.AUTHOR)),
            # No location row: the header already states it, translated
            # ("Ideato e sviluppato a Lugano, Svizzera"), and repeating
            # about.AUTHOR_LOCATION here would print the English country
            # name right under the translated one.
            (
                translator.tr("about.field.license"),
                f'<a href="{about.LICENSE_URL}">{html.escape(about.LICENSE_NAME)}</a>',
            ),
            (
                translator.tr("about.field.repository"),
                f'<a href="{about.REPOSITORY_URL}">{html.escape(about.REPOSITORY_URL)}</a>',
            ),
            (
                translator.tr("about.field.support"),
                f'<a href="mailto:{about.SUPPORT_EMAIL}">{html.escape(about.SUPPORT_EMAIL)}</a>',
            ),
            (translator.tr("about.build.python"), html.escape(info["python"])),
            (translator.tr("about.build.platform"), html.escape(info["platform"])),
            (translator.tr("about.build.machine"), html.escape(info["machine"])),
        ]
        table = "".join(
            f"<tr><td><b>{label}</b>&nbsp;&nbsp;</td><td>{value}</td></tr>" for label, value in rows
        )
        return (
            _paragraphs(html.escape(translator.tr("about.summary")))
            + f"<table>{table}</table>"
            + _paragraphs(html.escape(about.COPYRIGHT))
        )

    def _intended_use_html(self) -> str:
        return (
            f"<h3>{html.escape(translator.tr('about.intended_use.heading'))}</h3>"
            + _paragraphs(
                f"<b>{html.escape(translator.tr('about.intended_use.body'))}</b>",
                html.escape(translator.tr("about.intended_use.detail")),
            )
            + f"<h3>{html.escape(translator.tr('about.scope.does_heading'))}</h3>"
            + _bullet_list(html.escape(translator.tr("about.scope.does_items")))
            + f"<h3>{html.escape(translator.tr('about.scope.does_not_heading'))}</h3>"
            + _bullet_list(html.escape(translator.tr("about.scope.does_not_items")))
            + f"<h3>{html.escape(translator.tr('about.registration.heading'))}</h3>"
            + _paragraphs(html.escape(translator.tr("about.registration.body")))
            + f"<h3>{html.escape(translator.tr('about.ibsi.heading'))}</h3>"
            + _paragraphs(html.escape(translator.tr("about.ibsi.body")))
        )

    def _legal_html(self) -> str:
        return (
            f"<h3>{html.escape(translator.tr('about.legal.dicom_heading'))}</h3>"
            + _paragraphs(
                f"<b>{html.escape(about.DICOM_SUPPORT_STATEMENT)}</b>",
                html.escape(about.DICOM_TRADEMARK_NOTICE),
                html.escape(about.NEMA_NON_AFFILIATION),
                html.escape(about.NO_DICOM_CONFORMANCE_CLAIM),
            )
            + f"<h3>{html.escape(translator.tr('about.legal.license_heading'))}</h3>"
            + _paragraphs(
                html.escape(about.COPYRIGHT),
                translator.tr(
                    "about.legal.license_body",
                    license_name=html.escape(about.LICENSE_NAME),
                    license_url=about.LICENSE_URL,
                ),
                html.escape(translator.tr("about.legal.warranty")),
                html.escape(
                    translator.tr("about.legal.third_party", file=about.THIRD_PARTY_LICENSES_FILE)
                ),
            )
        )

    def _privacy_html(self) -> str:
        return f"<h3>{html.escape(translator.tr('about.privacy.heading'))}</h3>" + _paragraphs(
            f"<b>{html.escape(translator.tr('about.privacy.local'))}</b>",
            html.escape(translator.tr("about.privacy.network_exception")),
            html.escape(translator.tr("about.privacy.dicom_handling")),
            html.escape(translator.tr("about.privacy.no_telemetry")),
        )

    def _citation_html(self) -> str:
        return (
            _paragraphs(html.escape(translator.tr("about.citation.intro")))
            + f"<blockquote><p>{html.escape(about.citation_text())}</p></blockquote>"
            + f"<h3>{html.escape(translator.tr('about.citation.bibtex_heading'))}</h3>"
            + f"<pre>{html.escape(about.citation_bibtex())}</pre>"
            + _paragraphs(html.escape(translator.tr("about.citation.doi_pending")))
        )

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("about.title"))
        self._version_label.setText(translator.tr("about.version_line", version=about.VERSION))
        self._designed_in_label.setText(translator.tr("about.designed_in"))

        self._tabs.setTabText(0, translator.tr("about.tab.about"))
        self._tabs.setTabText(1, translator.tr("about.tab.intended_use"))
        self._tabs.setTabText(2, translator.tr("about.tab.legal"))
        self._tabs.setTabText(3, translator.tr("about.tab.privacy"))
        self._tabs.setTabText(4, translator.tr("about.tab.citation"))

        self._about_view.setHtml(self._about_html())
        self._intended_use_view.setHtml(self._intended_use_html())
        self._legal_view.setHtml(self._legal_html())
        self._privacy_view.setHtml(self._privacy_html())
        self._citation_view.setHtml(self._citation_html())

        self._copy_build_button.setText(translator.tr("about.copy_build_details"))
        self._copy_citation_button.setText(translator.tr("about.citation.copy_text"))
        self._copy_bibtex_button.setText(translator.tr("about.citation.copy_bibtex"))
