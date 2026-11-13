"""Print the CHANGELOG.md section for one version (used by the release workflow).
If assets/releases/<version>.png exists (roadmap banner of a major release), it is put on top.
"@name" is defused so that GitHub doesn't mention a user of that name.

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
# "@name" would mention (and notify) a GitHub user of that name: break it with an invisible zero-width space
notes = re.sub(r"(?<![\w`])@(?=[A-Za-z0-9])", "@\u200b", notes)
print(notes)
