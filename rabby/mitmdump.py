#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mitmproxy==11.0.2"]
# ///
# mitmdump from mitmdump.py.lock (hash-checked); `uv run --with` would install unlocked and unchecked.
from mitmproxy.tools.main import mitmdump

mitmdump()
