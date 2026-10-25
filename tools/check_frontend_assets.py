"""Fail CI when index.html references a local asset that is missing from the repo.

The frontend has no build step, so a dangling src/href would only surface as a
broken script or stylesheet inside the packaged app.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
INDEX = FRONTEND / "index.html"

EXTERNAL = ("http://", "https://", "//", "data:", "#", "mailto:")


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    missing = []
    for ref in refs:
        ref = ref.strip()
        if not ref or ref.startswith(EXTERNAL):
            continue
        target = FRONTEND / ref.split("?", 1)[0].split("#", 1)[0].lstrip("./")
        if not target.is_file():
            missing.append(ref)
    if missing:
        print("Missing frontend assets referenced by index.html:")
        for ref in sorted(set(missing)):
            print("  -", ref)
        return 1
    print(f"OK: all {len(set(refs))} local references in index.html resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
