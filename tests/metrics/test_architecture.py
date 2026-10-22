from __future__ import annotations

import subprocess
import sys


def test_metric_core_import_does_not_import_the_gui_package() -> None:
    code = "import sys; import qicom.metrics.runner; assert 'qicom.gui' not in sys.modules"

    subprocess.run([sys.executable, "-c", code], check=True)
