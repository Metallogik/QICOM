"""Share one recorded reference model between the two NSS distances."""

from __future__ import annotations

from collections.abc import Callable

from qicom.events import CallbackRegistry
from qicom.metrics.nss_reference import NssReferenceModel, default_reference_model


class NssModelSettings:
    def __init__(self) -> None:
        self._model: NssReferenceModel | None = None
        self._listeners = CallbackRegistry()

    @property
    def model(self) -> NssReferenceModel:
        return self._model if self._model is not None else default_reference_model()

    @property
    def uses_synthetic_default(self) -> bool:
        return self._model is None

    def set_model(self, model: NssReferenceModel | None) -> None:
        self._model = model
        self._listeners.notify()

    def on_changed(self, callback: Callable[[], None]) -> None:
        self._listeners.add(callback)


nss_model_settings = NssModelSettings()

__all__ = ["NssModelSettings", "nss_model_settings"]
