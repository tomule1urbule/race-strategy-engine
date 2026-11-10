"""Assemble the static site (frontend + Python engine + sample courses) into ``_site/``.

python scripts/build_site.py && python -m http.server 8000 -d _site
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "_site"
ENGINE_FILES = ["__init__.py", "models.py", "utils.py", "gpx_analyzer.py", "race_engine.py", "web.py"]
FRONTEND_FILES = ["index.html", "app.js", "style.css", "icon.svg", "manifest.webmanifest"]


def main() -> None:
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "py" / "race_strategy").mkdir(parents=True)
    (SITE / "samples").mkdir()
    for name in FRONTEND_FILES:
        shutil.copy2(ROOT / "frontend" / name, SITE / name)
    for name in ENGINE_FILES:
        shutil.copy2(ROOT / "backend" / "race_strategy" / name, SITE / "py" / "race_strategy" / name)
    (SITE / "py" / "files.json").write_text(json.dumps(ENGINE_FILES))
    for gpx in sorted((ROOT / "data" / "sample_courses").glob("*.gpx")):
        shutil.copy2(gpx, SITE / "samples" / gpx.name)
    (SITE / ".nojekyll").write_text("")
    print(f"Built {sum(1 for _ in SITE.rglob('*') if _.is_file())} files into {SITE}")


if __name__ == "__main__":
    main()
