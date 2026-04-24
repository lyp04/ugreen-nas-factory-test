from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["UGREEN_DISABLE_FORM_ENTRY"] = "1"

if __package__ in {None, ""}:
    package_root = Path(__file__).resolve().parent.parent
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    from src.gui import main
else:
    from .gui import main


if __name__ == "__main__":
    main()
