"""One coloured button at a time: the step the user should take next.

A column of controls tells a newcomer nothing about the order to use them
in. The air-removal column of the quantitative workflow (BackgroundPanel)
solved that by colouring exactly the button that is due and muting every
other one; the image tools follow the same rule through this helper, so the
look - the themes' ``QPushButton[guidance=...]`` rules - and the behaviour
stay one and the same.

A guided button must not also be named ``primary``: the themes' ID selector
outranks the attribute selector and would keep it coloured when it is done.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtWidgets import QPushButton

GUIDANCE_NEXT = "next"
GUIDANCE_DONE = "done"


def set_guidance(button: QPushButton, state: str) -> None:
    """Mark `button`; a property Qt has already resolved into a painted
    widget is only re-read after an unpolish/polish, so an unchanged state
    is skipped rather than repainted."""
    if button.property("guidance") == state:
        return
    button.setProperty("guidance", state)
    style = button.style()
    if style is not None:
        style.unpolish(button)
        style.polish(button)
    button.update()


def guide(buttons: Iterable[QPushButton], *next_buttons: QPushButton | None) -> None:
    """Colour `next_buttons` - usually one, two when either will do, such as
    opening a file or a folder - and mute every other one of `buttons`."""
    due = [button for button in next_buttons if button is not None]
    for button in buttons:
        set_guidance(
            button, GUIDANCE_NEXT if any(button is item for item in due) else GUIDANCE_DONE
        )


def is_next(button: QPushButton) -> bool:
    return button.property("guidance") == GUIDANCE_NEXT


__all__ = ["GUIDANCE_DONE", "GUIDANCE_NEXT", "guide", "is_next", "set_guidance"]
