"""Print the CHANGELOG.md section for one version (used by the release workflow).

Usage: python scripts/release_notes.py 2.7.0
"""
import re
import sys
from pathlib import Path

version = sys.argv[1].lstrip("vV")
text = Path(__file__).resolve().parent.parent.joinpath("CHANGELOG.md").read_text("utf-8")
m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |^\[[^\]]+\]: |\Z)", text, re.S | re.M)
if not m:
    sys.exit(f"No CHANGELOG.md entry for {version}")
print(m.group(1).strip())
