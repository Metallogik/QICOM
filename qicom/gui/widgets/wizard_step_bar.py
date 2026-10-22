"""The Back/Next footer the qualitative form builder is driven from.

Building an evaluation is a sequence - load the images for a page, write
that page's questions, then look at every page together - and each of
those steps is a different set of panels in the same window. The rail
across the top can jump between them at any time, but it is navigation
chrome: it does not tell someone opening the builder for the first time
what the *next* thing to do is. This bar does, in the one place the eye
ends up when a step is finished: the bottom of it.

Presentation only, like every other widget here. It knows its own
translation keys and emits back_requested/next_requested;
QualitativeMainWindow decides which stage each of those actually means.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qicom.i18n import translator


class WizardStepBar(QWidget):
    back_requested = Signal()
    next_requested = Signal()

    def __init__(
        self,
        *,
        title_key: str,
        hint_key: str,
        next_key: str,
        back_key: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title_key = title_key
        self._hint_key = hint_key
        self._next_key = next_key
        self._back_key = back_key

        self._title = QLabel()
        self._title.setObjectName("panelSectionTitle")
        self._hint = QLabel()
        self._hint.setObjectName("panelSectionHint")
        self._hint.setWordWrap(True)
        # Which page this step is currently working on - set by the window
        # from the controller, since a builder with several pages otherwise
        # gives no clue which one's images are on screen.
        self._context = QLabel()
        self._context.setObjectName("wizardStepContext")
        self._context.setVisible(False)
        # A standing statement about what this step does to the user's
        # data - not a hint about what to do next (that is _hint) and not
        # a transient status message. The images step uses it to say that
        # every DICOM/NIfTI file loaded there will be de-identified on the
        # way into the package, which is something the author has to know
        # while they are still choosing the files, not once the package is
        # already written.
        self._notice = QLabel()
        self._notice.setObjectName("wizardStepNotice")
        self._notice.setWordWrap(True)
        self._notice.setVisible(False)

        self._back_button = QPushButton()
        self._back_button.clicked.connect(self.back_requested)
        self._back_button.setVisible(back_key is not None)

        self._next_button = QPushButton()
        self._next_button.setObjectName("primary")
        self._next_button.setMinimumHeight(44)
        self._next_button.setMinimumWidth(220)
        self._next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._next_button.clicked.connect(self.next_requested)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self._back_button)
        buttons.addStretch(1)
        buttons.addWidget(self._next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 16)
        layout.setSpacing(6)
        # In its own row with a stretch beside it so the chip hugs its
        # text instead of painting a band across the whole bar.
        context_row = QHBoxLayout()
        context_row.addWidget(self._context)
        context_row.addStretch(1)

        layout.addWidget(self._title)
        layout.addLayout(context_row)
        layout.addWidget(self._hint)
        layout.addWidget(self._notice)
        layout.addLayout(buttons)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def set_context(self, text: str) -> None:
        self._context.setText(text)
        self._context.setVisible(bool(text))

    def set_notice(self, text: str) -> None:
        """See _notice's own comment - empty text hides the row entirely,
        so a step that has nothing to declare looks exactly as it did
        before this existed."""
        self._notice.setText(text)
        self._notice.setVisible(bool(text))

    def set_next_enabled(self, enabled: bool) -> None:
        self._next_button.setEnabled(enabled)

    def retranslate_ui(self) -> None:
        self._title.setText(translator.tr(self._title_key))
        self._hint.setText(translator.tr(self._hint_key))
        self._next_button.setText(translator.tr(self._next_key))
        if self._back_key is not None:
            self._back_button.setText(translator.tr(self._back_key))


__all__ = ["WizardStepBar"]
