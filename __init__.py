# Plugin entry point — forwards into src/ layout
#
# hermes plugins install clones the whole repo including src/.
# The Hermes plugin loader imports this as hermes_plugins.hermes_chess
# with the plugin directory on the module search path.
# We add src/ to sys.path so the actual module resolves.
import sys
from pathlib import Path

_plugin_dir = Path(__file__).resolve().parent
_src = str(_plugin_dir / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

# Now import the real module from src/hermes_chess/
from hermes_chess import *  # noqa: E402, F401, F403
