"""Print the CHANGELOG.md section for one version (used by the release workflow).
If assets/releases/<version>.png exists (roadmap banner of a major release), it is put on top.

Usage: python scripts/release_notes.py 2.7.0
"""
import re
import sys
from pathlib import Path

version = sys.argv[1].lstrip("vV")
root = Path(__file__).resolve().parent.parent
text = root.joinpath("CHANGELOG.md").read_text("utf-8")
m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |^\[[^\]]+\]: |\Z)", text, re.S | re.M)
if not m:
    sys.exit(f"No CHANGELOG.md entry for {version}")
notes = m.group(1).strip()
if root.joinpath("assets", "releases", f"{version}.png").exists():
    banner = f"https://raw.githubusercontent.com/LooneyN/chronolist/main/assets/releases/{version}.png"
    notes = f"![Roadmap]({banner})\n\n{notes}"
print(notes)
