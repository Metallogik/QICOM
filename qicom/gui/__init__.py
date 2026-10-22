"""PySide6 + pyqtgraph desktop GUI. Presentation and interaction only - no calculation logic."""

from __future__ import annotations

import pyqtgraph as pg

# pyqtgraph's default ("col-major") axis order treats a 2D array's *first*
# axis as the ImageItem's local x - so a DICOM/PNG/... array's row axis
# (its real top-to-bottom direction) is drawn left-to-right on screen, and
# its column axis (real left-to-right) is drawn top-to-bottom. That's a
# transpose, not a rotation (determinant -1, a mirror) - no sequence of
# 90-degree rotate/flip operations can ever undo it, only mask a symptom
# of it (fixing top/bottom always leaves left/right swapped, or vice
# versa). "row-major" matches every array's natural (row, col) layout
# instead, so ImageItem-local (x, y) becomes (col, row) - the same
# convention every other image library (PIL, matplotlib, skimage) already
# uses, and what qicom.gui.roi_geometry/qicom.gui.line_profile are written
# against.
#
# This must run before any pg.ImageItem is constructed anywhere in the app
# (ImageItem reads and caches this once, in its own __init__) - since
# every qicom.gui.* submodule's body only runs after this package's own
# __init__ has, setting it here (rather than in main()) is what makes it
# apply consistently both for the real app and for tests that construct
# widgets directly without ever calling qicom.gui.app.main().
pg.setConfigOptions(imageAxisOrder="row-major")
