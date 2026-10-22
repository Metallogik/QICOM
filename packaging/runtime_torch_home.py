"""Point the frozen application's Torch cache at a writable per-user directory.

PyInstaller runs this before any application code. Without it, torch would
default to ~/.cache/torch on the end user's machine - which works, but hides
several hundred megabytes of QICOM's data in a directory the user has no
reason to associate with QICOM, and which an uninstall would never clean up.

QICOM's learned metrics (LPIPS, DISTS) need pretrained weights that are
downloaded on demand rather than bundled - see
qicom.metrics.learned_weights for why. That download has to land somewhere
writable that survives an application update, so it cannot go inside the
bundle: sys._MEIPASS is replaced wholesale when the user installs a new
version, and on macOS lives inside a signed .app the application should not
be writing into at all.

The layout under TORCH_HOME stays exactly torch's own (hub/checkpoints), so
qicom.metrics.learned_weights.checkpoints_dir() and torch.hub.get_dir()
agree without either having to know about this hook.
"""

from __future__ import annotations

import os
import sys

if getattr(sys, "frozen", False) and not os.environ.get("TORCH_HOME"):
    # platformdirs is a base dependency and always bundled. appauthor=False
    # matches every other QICOM user directory (session database, logs,
    # recent projects), so all of the application's per-user state stays
    # under one root the user can inspect or delete as a unit.
    import platformdirs

    os.environ["TORCH_HOME"] = os.path.join(
        platformdirs.user_data_dir("qicom", appauthor=False), "torch"
    )
