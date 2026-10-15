"""Assemble a Hugging Face Space folder from the files tracked in git (never .env or notes/).

    python deploy/huggingface/make_space.py ../blast-radius-space

The folder gets the Space Dockerfile and README at its root; push it to the Space's git repo.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def main(out: str) -> None:
    dest = Path(out).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.split()
    for rel in files:
        # Spaces reject binary files pushed without git-lfs; the screenshot is only for the GitHub README.
        if rel.startswith("docs/images/") or rel in ("Dockerfile", "README.md"):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    shutil.copy2(HERE / "Dockerfile", dest / "Dockerfile")
    # The image copies README.md for the package metadata; the Space card replaces it.
    shutil.copy2(HERE / "SPACE_README.md", dest / "README.md")
    print(f"Space folder ready: {dest} ({len(files)} tracked files)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT.parent / "blast-radius-space"))
