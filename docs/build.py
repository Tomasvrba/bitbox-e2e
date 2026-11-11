#!/usr/bin/env python3
"""Build docs/showcase.html (self-contained, images inlined) and, with --pdf, showcase.pdf.

  docs/build.py [--pdf]

Screenshots in docs/assets/ come from real runs (see the slide captions); replace them with
fresh report output to update the deck. PDF export uses a Playwright Chromium if one is installed.
"""

import base64
import glob
import os
import re
import subprocess
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent


def inline(m):
    data = (DOCS / "assets" / m.group(1)).read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode()


def chromium():
    for pattern in ("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",):
        found = sorted(glob.glob(os.path.expanduser(pattern)))
        if found:
            return found[-1]
    sys.exit("no Playwright Chromium found (npx playwright install chromium)")


def main():
    src = (DOCS / "showcase.src.html").read_text()
    out = DOCS / "showcase.html"
    out.write_text(re.sub(r"\{\{([\w.-]+\.png)\}\}", inline, src))
    print(out)
    if "--pdf" in sys.argv:
        pdf = DOCS / "showcase.pdf"
        subprocess.run([chromium(), "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--no-pdf-header-footer", f"--print-to-pdf={pdf}", out.as_uri()],
                       check=True, stderr=subprocess.DEVNULL)
        print(pdf)


if __name__ == "__main__":
    main()
