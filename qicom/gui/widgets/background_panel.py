"""The Analyze step's "don't measure the air" control.

A whole-image metric averages over every pixel, and in a typical slice
most of those pixels are the air around the patient - which is why an
unmasked MSE/SSIM is partly a statement about the background rather than
about the anatomy (see qicom.metrics.background for the full argument and
qicom.metrics.runner's `tissue_mask` for what each metric then does with
it).

Excluding the air is *optional*, and the panel says so in a banner that
never leaves the top of the column: skipping this step is a legitimate
choice (every metric is then averaged over the whole image), it is simply
one the user has to make knowingly. That is why the choice is a
full-width toggle under a plain question rather than the small checkbox
it replaced - a control the eye reaches before the tools it unlocks,
instead of one that could be walked past without ever being noticed.

The panel walks through that in three visible steps, rather than exposing
every knob at once:

1. **Create the air sample.** Exactly one green circle (DualImageView.
   add_air_roi), placed by the user on a patch of pure background - the
   Add button turns itself off once it exists, so the sample can never be
   silently widened by a second circle dropped somewhere else. It is the
   only place the air level is ever read from - never a measurement ROI
   drawn earlier for something else.
2. **Detect.** One button turns the sample into a threshold and paints
   everything that would be excluded straight onto both images, so the
   answer is seen before any metric is computed. The tolerance and the
   exact threshold stay editable next to it.
3. **Correct by hand.** A paint-style eraser puts back anything the
   threshold caught that is not background, while a brush adds missed air
   to the excluded region. Dark tissue reading as air is the normal case
   where an intensity threshold alone cannot win.

Exactly one button in the column is ever colored: the step that is
next. Once it has been pressed and is no longer the thing to do, it
drops to the muted "done" grey and the color moves on to the following
step - so a finished exclusion leaves a column of grey buttons and
nothing competing for the eye. See `_next_action`/`_refresh_guidance`
for the rule and themes/*.qss's own `[guidance=...]` rules for the two
looks.

Presentation only, exactly like every other panel here: it never touches
image data, and the canvas work (the circle, the eraser strokes, the
overlay) belongs to DualImageView. What comes back the other way is the
BackgroundEstimate, whose readout - "air level ..., threshold ..., 38% of
the image excluded" - is the whole point: a mask that quietly ate half the
anatomy and a correct one are indistinguishable from the results table.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.background_exclusion import BackgroundExclusionSettings
from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.number_format import number_format_settings
from qicom.gui.theming import theme_manager
from qicom.i18n import translator
from qicom.metrics.background import DEFAULT_TOLERANCE_FACTOR, BackgroundEstimate

MOSTLY_EXCLUDED_FRACTION = 0.9
"""Above this share of excluded pixels the readout turns into a warning:
a mask that keeps a tenth of the image is far more likely to be a
mis-detection (the circle dropped on tissue rather than air, a tolerance
dragged too wide) than a genuinely tiny patient."""

BRUSH_RADIUS_MIN = 1
BRUSH_RADIUS_MAX = 200

GUIDANCE_NEXT = "next"
"""The `guidance` QSS property value of the single button the user should
press now - the only accent-filled control in the column."""

GUIDANCE_DONE = "done"
"""Every other button: already done, optional, or not reachable yet. Muted
grey, so "nothing is colored" reads as "nothing is being asked of you
here"."""


class BackgroundPanel(QWidget):
    # The numbers changed (the checkbox, the tolerance, the threshold) -
    # MainWindow re-detects if a detection already happened.
    settings_changed = Signal()
    air_roi_requested = Signal(bool)  # True = create the green circle, False = remove it
    detect_requested = Signal()
    eraser_toggled = Signal(bool)
    brush_toggled = Signal(bool)
    brush_radius_changed = Signal(int)
    clear_erasures_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tolerance_override: float | None = None
        self._estimate: BackgroundEstimate | None = None
        self._air_roi_count = 0
        self._has_detection = False

        # Pinned to the top of the column and never hidden: whatever else
        # the panel is showing, "you may skip all of this" stays readable.
        self._optional_notice = QLabel()
        self._optional_notice.setObjectName("backgroundOptionalNotice")
        self._optional_notice.setWordWrap(True)

        self._enable_question = QLabel()
        self._enable_question.setObjectName("backgroundEnableQuestion")
        self._enable_question.setWordWrap(True)

        # A checkable button, not a checkbox: this one decision governs
        # every other control in the column, and a 13px tick box gave it
        # exactly as much visual weight as the brush radius next to it.
        # Still `_enabled_check` by name - it is the same piece of state,
        # with the same checked/toggled API.
        self._enabled_check = QPushButton()
        self._enabled_check.setObjectName("backgroundEnableToggle")
        self._enabled_check.setCheckable(True)
        self._enabled_check.setMinimumHeight(46)
        self._enabled_check.toggled.connect(self._on_enabled_toggled)

        self._step_label = QLabel()
        self._step_label.setWordWrap(True)

        # Not the shared "primary" fill any more: whether these two read
        # as the action to take is decided per state by _refresh_guidance,
        # and #primary's unconditional accent would have kept a finished
        # step looking like the next one.
        self._air_roi_button = QPushButton()
        self._air_roi_button.setObjectName("backgroundStepButton")
        self._air_roi_button.setMinimumHeight(38)
        self._air_roi_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding
        )
        self._air_roi_button.clicked.connect(self._on_air_roi_clicked)

        self._air_roi_count_label = QLabel()
        self._air_roi_count_label.setObjectName("roiCountBadge")
        self._air_roi_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._clear_air_rois_button = QPushButton()
        self._clear_air_rois_button.clicked.connect(lambda: self.air_roi_requested.emit(False))

        self._detect_button = QPushButton()
        self._detect_button.setObjectName("backgroundStepButton")
        self._detect_button.setMinimumHeight(38)
        self._detect_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding
        )
        self._detect_button.clicked.connect(self.detect_requested)

        # The "room to manoeuvre" around the sampled air level: how many
        # of the sample's own noise spreads above it a pixel has to be
        # before it counts as anatomy.
        self._tolerance_spin = QDoubleSpinBox()
        self._tolerance_spin.setRange(0.0, 50.0)
        self._tolerance_spin.setSingleStep(0.5)
        self._tolerance_spin.setDecimals(1)
        self._tolerance_spin.setValue(DEFAULT_TOLERANCE_FACTOR)
        self._tolerance_spin.valueChanged.connect(self._on_tolerance_factor_changed)

        # The tolerance is the most likely way to recover a detection that
        # initially missed some air. Keep the exact numeric input, but flank
        # it with large round controls so this important adjustment is
        # discoverable without aiming for a spin box's tiny native arrows.
        self._tolerance_minus_button = QPushButton("−")
        self._tolerance_plus_button = QPushButton("+")
        for button in (self._tolerance_minus_button, self._tolerance_plus_button):
            button.setObjectName("toleranceStepButton")
            button.setFixedSize(42, 42)
        self._tolerance_minus_button.clicked.connect(lambda: self._step_tolerance(-1))
        self._tolerance_plus_button.clicked.connect(lambda: self._step_tolerance(1))

        # An exact intensity threshold, for the user who already knows the
        # number they want (a CT reader thinking in Hounsfield units, say).
        # Editing it pins the band; the tolerance spin above releases it.
        self._threshold_label = QLabel()
        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(-1e9, 1e9)
        self._threshold_spin.setDecimals(2)
        self._threshold_spin.setKeyboardTracking(False)
        self._threshold_spin.valueChanged.connect(self._on_threshold_edited)

        self._eraser_button = QPushButton()
        self._eraser_button.setCheckable(True)
        self._eraser_button.toggled.connect(self._on_eraser_toggled)

        self._paint_button = QPushButton()
        self._paint_button.setCheckable(True)
        self._paint_button.toggled.connect(self._on_brush_toggled)

        for tool_button in (self._eraser_button, self._paint_button):
            tool_button.setMinimumHeight(38)
            tool_button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding
            )

        self._brush_label = QLabel()
        self._brush_spin = QSpinBox()
        self._brush_spin.setRange(BRUSH_RADIUS_MIN, BRUSH_RADIUS_MAX)
        self._brush_spin.setValue(12)
        self._brush_spin.valueChanged.connect(self.brush_radius_changed)
        self._brush_spin.setSuffix(" px")
        self._brush_slider = QSlider(Qt.Orientation.Horizontal)
        self._brush_slider.setRange(BRUSH_RADIUS_MIN, BRUSH_RADIUS_MAX)
        self._brush_slider.setValue(self._brush_spin.value())
        self._brush_slider.setMinimumWidth(90)
        self._brush_slider.valueChanged.connect(self._brush_spin.setValue)
        self._brush_spin.valueChanged.connect(self._brush_slider.setValue)

        self._clear_erasures_button = QPushButton()
        self._clear_erasures_button.clicked.connect(self.clear_erasures_requested)

        self._summary = QLabel()
        self._summary.setObjectName("tableNote")
        self._summary.setWordWrap(True)

        # The dedicated air-removal stage is a tall, narrow inspector to
        # the right of the images, so everything reads strictly
        # top-to-bottom - one control per row, each card filling its share
        # of the height rather than three squeezed rows of tiny controls
        # with the bottom two thirds of the column left empty.
        self._sample_card, sample_column = self._tool_card("background.samples_section")
        sample_column.addWidget(self._air_roi_button)
        sample_count_row = QHBoxLayout()
        sample_count_row.setSpacing(5)
        sample_count_row.addWidget(self._air_roi_count_label)
        sample_count_row.addWidget(self._clear_air_rois_button, 1)
        sample_column.addLayout(sample_count_row)
        sample_column.addWidget(self._detect_button)

        self._tolerance_card, tolerance_column = self._tool_card("background.tolerance_section")
        tolerance_stepper_row = QHBoxLayout()
        tolerance_stepper_row.setSpacing(5)
        tolerance_stepper_row.addWidget(self._tolerance_minus_button)
        tolerance_stepper_row.addWidget(self._tolerance_spin, 1)
        tolerance_stepper_row.addWidget(self._tolerance_plus_button)
        tolerance_column.addLayout(tolerance_stepper_row)
        tolerance_column.addWidget(self._threshold_label)
        tolerance_column.addWidget(self._threshold_spin)

        self._correction_card, correction_column = self._tool_card("background.corrections_section")
        correction_column.addWidget(self._eraser_button)
        correction_column.addWidget(self._paint_button)
        brush_row = QHBoxLayout()
        brush_row.setSpacing(5)
        brush_row.addWidget(self._brush_label)
        brush_row.addWidget(self._brush_spin, 1)
        correction_column.addLayout(brush_row)
        correction_column.addWidget(self._brush_slider)
        correction_column.addWidget(self._clear_erasures_button)

        # Takes the height the cards would have used while they are
        # hidden, so the notice and the toggle stay at the top of the
        # column instead of drifting down into the middle of it.
        self._idle_spacer = QWidget()
        self._idle_spacer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        layout.addWidget(self._optional_notice)
        layout.addWidget(self._enable_question)
        layout.addWidget(self._enabled_check)
        layout.addWidget(self._step_label)
        layout.addWidget(self._sample_card, 3)
        layout.addWidget(self._tolerance_card, 2)
        layout.addWidget(self._correction_card, 4)
        layout.addWidget(self._idle_spacer, 1)
        layout.addWidget(self._summary)

        # Every button the column offers, in the order the user meets
        # them - _refresh_guidance colors exactly one of these at a time
        # and mutes the rest.
        self._guided_buttons = (
            self._enabled_check,
            self._air_roi_button,
            self._clear_air_rois_button,
            self._detect_button,
            self._tolerance_minus_button,
            self._tolerance_plus_button,
            self._eraser_button,
            self._paint_button,
            self._clear_erasures_button,
        )

        self._refresh_state()
        translator.on_language_changed(self.retranslate_ui)
        number_format_settings.on_notation_changed(self._refresh_summary)
        theme_manager.on_theme_changed(self._refresh_tool_icons)
        self.retranslate_ui()

    @staticmethod
    def _tool_card(title_key: str) -> tuple[QFrame, QVBoxLayout]:
        """One titled card whose controls stack vertically - the caller
        adds a nested row only where two controls genuinely belong side by
        side (a −/+ stepper around its own spin box, a value next to its
        label)."""
        card = QFrame()
        card.setObjectName("backgroundToolCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 5, 8, 7)
        card_layout.setSpacing(3)
        title = QLabel()
        title.setObjectName("toolSectionTitle")
        title.setProperty("translationKey", title_key)
        card_layout.addWidget(title)
        controls = QVBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        card_layout.addLayout(controls, 1)
        return card, controls

    # -- State the rest of the app reads ----------------------------------

    def settings(self) -> BackgroundExclusionSettings:
        return BackgroundExclusionSettings(
            enabled=self._enabled_check.isChecked(),
            tolerance_factor=float(self._tolerance_spin.value()),
            tolerance=self._tolerance_override,
        )

    def set_settings(self, settings: BackgroundExclusionSettings) -> None:
        """Restore a saved analysis' choice (see AppController.
        reopen_analysis) without firing settings_changed for each control
        on the way - the caller triggers one detection afterwards."""
        widgets = (self._enabled_check, self._tolerance_spin, self._threshold_spin)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self._enabled_check.setChecked(settings.enabled)
            self._tolerance_spin.setValue(settings.tolerance_factor)
            self._tolerance_override = settings.tolerance
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._refresh_state()

    def brush_radius(self) -> int:
        return int(self._brush_spin.value())

    def is_eraser_active(self) -> bool:
        return self._eraser_button.isChecked()

    def is_brush_active(self) -> bool:
        return self._paint_button.isChecked()

    def set_eraser_active(self, active: bool) -> None:
        """Turn the eraser button off from the outside - MainWindow does
        this whenever something else takes the tool away (another toolbar
        mode, leaving the Analyze step), so the button never claims to be
        armed when the canvas is no longer erasing."""
        if self._eraser_button.isChecked() != active:
            self._eraser_button.setChecked(active)

    def set_brush_active(self, active: bool) -> None:
        if self._paint_button.isChecked() != active:
            self._paint_button.setChecked(active)

    # -- What MainWindow reports back -------------------------------------

    def set_air_roi_present(self, present: bool) -> None:
        """Whether the green circle is currently on the canvas - drives
        both the button's own label (create/remove) and which step the
        panel is inviting the user to do next."""
        self.set_air_roi_count(1 if present else 0)

    def set_air_roi_count(self, count: int) -> None:
        """Report how many green sampling circles exist on the canvas."""
        self._air_roi_count = max(0, int(count))
        present = self._air_roi_count > 0
        if not present:
            self._has_detection = False
            self._estimate = None
        self._refresh_state()

    def set_estimate(self, estimate: BackgroundEstimate | None) -> None:
        self._estimate = estimate
        self._has_detection = estimate is not None
        if estimate is not None:
            self._threshold_spin.blockSignals(True)
            self._threshold_spin.setValue(estimate.threshold)
            self._threshold_spin.blockSignals(False)
        self._refresh_state()

    def set_excluded_fraction(self, fraction: float) -> None:
        """Restate how much is excluded after a hand correction, without a
        new detection - the air level and the threshold are unchanged, only
        which pixels the mask keeps. A no-op before anything was detected,
        since there is no estimate to restate."""
        if self._estimate is None:
            return
        self._estimate = replace(self._estimate, excluded_fraction=fraction)
        self._refresh_summary()

    def set_error(self, message: str) -> None:
        """A detection that failed (the circle dropped on tissue, a
        threshold above every pixel) - shown in place of the readout, since
        the alternative is a panel that silently keeps displaying the last
        estimate that did work."""
        self._estimate = None
        self._has_detection = False
        self._refresh_state()
        self._summary.setText(translator.tr("background.detection_failed", message=message))

    # -- Internals ---------------------------------------------------------

    def _on_enabled_toggled(self) -> None:
        if not self._enabled_check.isChecked():
            self.set_eraser_active(False)
            self.set_brush_active(False)
        self._refresh_state()
        self.settings_changed.emit()

    def _on_air_roi_clicked(self) -> None:
        self.air_roi_requested.emit(True)

    def _on_eraser_toggled(self, active: bool) -> None:
        if active and self._paint_button.isChecked():
            self._paint_button.setChecked(False)
        self._refresh_state()
        self.eraser_toggled.emit(active)

    def _on_brush_toggled(self, active: bool) -> None:
        if active and self._eraser_button.isChecked():
            self._eraser_button.setChecked(False)
        self._refresh_state()
        self.brush_toggled.emit(active)

    def _step_tolerance(self, direction: int) -> None:
        self._tolerance_spin.setValue(
            self._tolerance_spin.value() + direction * self._tolerance_spin.singleStep()
        )

    def _on_tolerance_factor_changed(self) -> None:
        self._tolerance_override = None
        self.settings_changed.emit()

    def _on_threshold_edited(self, value: float) -> None:
        """An exact threshold typed in: kept as a band above the measured
        air level, so it survives being re-measured after the circle is
        moved, exactly like the tolerance factor does."""
        if self._estimate is None:
            return
        self._tolerance_override = float(value) - self._estimate.air_level
        self.settings_changed.emit()

    def _refresh_state(self) -> None:
        """One place deciding what is available and what the next step is,
        rather than each handler flipping widgets on its own."""
        enabled = self._enabled_check.isChecked()
        can_detect = enabled and self._air_roi_count > 0
        can_correct = enabled and self._has_detection

        self._step_label.setVisible(enabled)
        for card in (self._sample_card, self._tolerance_card, self._correction_card):
            card.setVisible(enabled)
        self._idle_spacer.setVisible(not enabled)
        for widget in (
            self._air_roi_button,
            self._air_roi_count_label,
            self._clear_air_rois_button,
            self._tolerance_spin,
            self._tolerance_minus_button,
            self._tolerance_plus_button,
            self._threshold_label,
            self._threshold_spin,
            self._detect_button,
            self._eraser_button,
            self._paint_button,
            self._brush_label,
            self._brush_slider,
            self._brush_spin,
            self._clear_erasures_button,
            self._summary,
        ):
            widget.setVisible(enabled)
        # One sample, and one only: the air level is read from a single
        # circle, so once it exists there is nothing left to add - offering
        # the button anyway would invite a second sample that quietly
        # widens what counts as air.
        self._air_roi_button.setEnabled(enabled and self._air_roi_count == 0)
        self._clear_air_rois_button.setEnabled(enabled and self._air_roi_count > 0)
        for widget in (
            self._tolerance_spin,
            self._tolerance_minus_button,
            self._tolerance_plus_button,
            self._threshold_label,
            self._threshold_spin,
            self._detect_button,
        ):
            widget.setEnabled(can_detect)
        for widget in (
            self._eraser_button,
            self._paint_button,
            self._brush_label,
            self._brush_slider,
            self._brush_spin,
        ):
            widget.setEnabled(can_correct)
        self._clear_erasures_button.setEnabled(can_correct)
        if not can_correct and self._eraser_button.isChecked():
            self.set_eraser_active(False)
        if not can_correct and self._paint_button.isChecked():
            self.set_brush_active(False)
        self._refresh_enable_toggle_text()
        self._refresh_air_roi_button_text()
        self._refresh_step_text()
        self._refresh_summary()
        self._refresh_guidance()

    def _next_action(self) -> QPushButton | None:
        """The one button worth pressing right now, or None once air
        removal has nothing left to ask for.

        Deliberately the same three-step order the step label narrates
        (_refresh_step_text): turn the exclusion on, place the sample,
        detect. Past that the mask exists and the remaining tools - the
        eraser, the air brush, the tolerance steppers - are corrections
        for a result that may already be right, so none of them is "the
        next step" and the column goes entirely grey."""
        if not self._enabled_check.isChecked():
            return self._enabled_check
        if self._air_roi_count == 0:
            return self._air_roi_button
        if not self._has_detection:
            return self._detect_button
        return None

    def _refresh_guidance(self) -> None:
        """Move the accent onto _next_action and mute everything else.

        QSS cannot express "the next step" on its own, so the state lands
        on each button as a `guidance` property the stylesheets select on;
        a property Qt has already resolved into a painted widget needs an
        explicit unpolish/polish to be re-read, and doing that on every
        button of every refresh would repaint the whole column for nothing
        - hence the early skip for buttons whose state has not moved."""
        next_button = self._next_action()
        for button in self._guided_buttons:
            state = GUIDANCE_NEXT if button is next_button else GUIDANCE_DONE
            if button.property("guidance") == state:
                continue
            button.setProperty("guidance", state)
            style = button.style()
            if style is not None:
                style.unpolish(button)
                style.polish(button)
            button.update()

    def _refresh_enable_toggle_text(self) -> None:
        """The toggle states what it *does*, not what it is: "Yes, exclude
        the air" while off, "Air exclusion is on" once pressed - so a
        glance at the top of the column answers "did I turn this on?"."""
        enabled = self._enabled_check.isChecked()
        key = "background.enable_on" if enabled else "background.enable_off"
        self._enabled_check.setText(translator.tr(key))

    def _refresh_air_roi_button_text(self) -> None:
        placed = self._air_roi_count > 0
        self._air_roi_button.setText(
            translator.tr("background.air_roi_placed" if placed else "background.add_air_roi")
        )
        # A disabled button with no explanation reads as broken - say why
        # it is closed and what re-opens it.
        self._air_roi_button.setToolTip(
            translator.tr(
                "background.air_roi_placed_tooltip" if placed else "background.air_roi_tooltip"
            )
        )
        self._air_roi_count_label.setText(
            translator.tr("background.air_roi_count", count=self._air_roi_count)
        )

    def _refresh_step_text(self) -> None:
        if not self._enabled_check.isChecked():
            self._step_label.clear()
            return
        if self._air_roi_count == 0:
            key = "background.step_create_roi"
        elif not self._has_detection:
            key = "background.step_place_and_detect"
        elif self._eraser_button.isChecked():
            key = "background.step_erasing"
        elif self._paint_button.isChecked():
            key = "background.step_painting"
        else:
            key = "background.step_correct"
        self._step_label.setText(translator.tr(key))

    def _refresh_summary(self) -> None:
        if not self._enabled_check.isChecked():
            self._summary.setText(translator.tr("background.disabled_hint"))
            return
        if self._estimate is None:
            self._summary.setText(translator.tr("background.no_estimate"))
            return
        estimate = self._estimate
        text = translator.tr(
            "background.summary",
            air_level=number_format_settings.format(estimate.air_level, 4),
            threshold=number_format_settings.format(estimate.threshold, 4),
            excluded=f"{100.0 * estimate.excluded_fraction:.1f}",
        )
        if estimate.excluded_fraction >= MOSTLY_EXCLUDED_FRACTION:
            text = f"{text} {translator.tr('background.mostly_excluded_warning')}"
        self._summary.setText(text)

    def retranslate_ui(self) -> None:
        self._optional_notice.setText(translator.tr("background.optional_notice"))
        self._enable_question.setText(translator.tr("background.enable_question"))
        self._refresh_enable_toggle_text()
        self._enabled_check.setToolTip(translator.tr("background.enable_tooltip"))
        self._refresh_air_roi_button_text()
        self._clear_air_rois_button.setText(translator.tr("background.clear_air_rois"))
        self._clear_air_rois_button.setToolTip(translator.tr("background.clear_air_rois_tooltip"))
        self._detect_button.setText(translator.tr("background.detect"))
        self._detect_button.setToolTip(translator.tr("background.detect_tooltip"))
        self._tolerance_spin.setToolTip(translator.tr("background.tolerance_tooltip"))
        self._threshold_label.setText(translator.tr("background.threshold"))
        self._threshold_spin.setToolTip(translator.tr("background.threshold_tooltip"))
        self._eraser_button.setText(translator.tr("background.eraser"))
        self._eraser_button.setToolTip(translator.tr("background.eraser_tooltip"))
        self._paint_button.setText(translator.tr("background.paint_air"))
        self._paint_button.setToolTip(translator.tr("background.paint_air_tooltip"))
        self._brush_label.setText(translator.tr("background.brush"))
        self._brush_spin.setToolTip(translator.tr("background.brush_tooltip"))
        self._clear_erasures_button.setText(translator.tr("background.clear_erasures"))
        self._clear_erasures_button.setToolTip(translator.tr("background.clear_erasures_tooltip"))
        for card in (self._sample_card, self._tolerance_card, self._correction_card):
            title = card.findChild(QLabel, "toolSectionTitle")
            if title is not None:
                title.setText(translator.tr(str(title.property("translationKey"))))
        self._refresh_tool_icons(theme_manager.theme)
        self._refresh_step_text()
        self._refresh_summary()

    def _refresh_tool_icons(self, theme: str) -> None:
        color = ink_color(theme)
        self._eraser_button.setIcon(paint_icon("eraser", color))
        self._paint_button.setIcon(paint_icon("brush", color))
