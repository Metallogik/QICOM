from __future__ import annotations

import pandas as pd

from qicom.dataframe_snapshots import dataframe_snapshot


def test_snapshot_isolated_from_edits_on_either_side() -> None:
    original = pd.DataFrame({"score": [1, 2], "label": ["a", "b"]})

    snapshot = dataframe_snapshot(original)
    original.loc[0, "score"] = 9
    snapshot.loc[1, "label"] = "changed"

    assert snapshot.loc[0, "score"] == 1
    assert original.loc[1, "label"] == "b"
