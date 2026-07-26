# Plugin entry point — forwards into src/ layout
#
# hermes plugins install clones the whole repo including src/, but
# the plugin loader imports from the root directory.  We add src/ to
# sys.path so all imports from src/hermes_chess/ resolve correctly.
import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from hermes_chess import *  # noqa: E402, F401, F403
