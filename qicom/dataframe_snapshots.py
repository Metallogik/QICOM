"""Cheap, isolated pandas snapshots for background save/export jobs."""

from __future__ import annotations

import pandas as pd

# pandas 3 always uses Copy-on-Write.  On supported pandas 2 releases it
# remains opt-in; enabling it once lets a shallow snapshot detach only the
# blocks either side later changes instead of eagerly duplicating a complete
# results table on the GUI thread.
if int(pd.__version__.split(".", 1)[0]) < 3:
    pd.options.mode.copy_on_write = True


def dataframe_snapshot(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Return an O(1) Copy-on-Write snapshot isolated from later edits."""
    return dataframe.copy(deep=False)
