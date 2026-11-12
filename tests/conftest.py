import sys
from pathlib import Path

# The harness modules import each other as top-level modules (run as scripts from the e2e dir).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
