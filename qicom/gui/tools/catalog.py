"""One list of the utility tools, read by the launcher and by the Tools window alike.

A tool is added here once and appears in both places, in the same order and
with the same words. Each entry names its widget by import path rather than
by class: the launcher reads this list on every start and must not import a
tool - nor what the tool imports - before the user has picked one.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    title_key: str
    description_key: str
    icon: str
    widget: str  # "package.module:ClassName", imported on first use

    def create_widget(self) -> QWidget:
        module_name, _separator, class_name = self.widget.partition(":")
        widget_class = getattr(importlib.import_module(module_name), class_name)
        widget = widget_class()
        if not isinstance(widget, QWidget):
            raise TypeError(f"{self.widget} is not a QWidget")
        return widget


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        tool_id="segmenter",
        title_key="tools.segmenter.title",
        description_key="tools.segmenter.description",
        icon="brush",
        widget="qicom.gui.tools.segmenter:SegmenterTool",
    ),
    ToolSpec(
        tool_id="masks",
        title_key="tools.masks.title",
        description_key="tools.masks.description",
        icon="roi_panel",
        widget="qicom.gui.tools.mask_tool:MaskTool",
    ),
    ToolSpec(
        tool_id="converter",
        title_key="tools.converter.title",
        description_key="tools.converter.description",
        icon="export",
        widget="qicom.gui.tools.converter:ConverterTool",
    ),
    ToolSpec(
        tool_id="resampling",
        title_key="tools.resampling.title",
        description_key="tools.resampling.description",
        icon="sync",
        widget="qicom.gui.tools.resampling_tool:ResamplingTool",
    ),
    ToolSpec(
        tool_id="arithmetic",
        title_key="tools.arithmetic.title",
        description_key="tools.arithmetic.description",
        icon="calculate",
        widget="qicom.gui.tools.arithmetic_tool:ArithmeticTool",
    ),
    ToolSpec(
        tool_id="degradation",
        title_key="tools.degradation.title",
        description_key="tools.degradation.description",
        icon="tune",
        widget="qicom.gui.tools.degradation_tool:DegradationTool",
    ),
    ToolSpec(
        tool_id="dicom_editor",
        title_key="tools.editor.title",
        description_key="tools.editor.description",
        icon="tag",
        widget="qicom.gui.tools.dicom_editor:DicomTagEditor",
    ),
    ToolSpec(
        tool_id="dicom_compare",
        title_key="tools.dicom_compare.title",
        description_key="tools.dicom_compare.description",
        icon="compare",
        widget="qicom.gui.tools.dicom_tags:DicomHeaderComparison",
    ),
    ToolSpec(
        tool_id="dicom_organizer",
        title_key="tools.organizer.title",
        description_key="tools.organizer.description",
        icon="folder",
        widget="qicom.gui.tools.dicom_organizer:DicomOrganizerTool",
    ),
    ToolSpec(
        tool_id="dicom_decompressor",
        title_key="tools.decompressor.title",
        description_key="tools.decompressor.description",
        icon="decompress",
        widget="qicom.gui.tools.dicom_decompressor:DicomDecompressorTool",
    ),
    ToolSpec(
        tool_id="anonymizer",
        title_key="tools.anonymizer.title",
        description_key="tools.anonymizer.description",
        icon="eye_off",
        widget="qicom.gui.tools.anonymizer:AnonymizerTool",
    ),
)


def tool_spec(tool_id: str) -> ToolSpec:
    for spec in TOOLS:
        if spec.tool_id == tool_id:
            return spec
    raise KeyError(tool_id)


__all__ = ["TOOLS", "ToolSpec", "tool_spec"]
