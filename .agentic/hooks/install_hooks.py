#!/usr/bin/env python3
"""Point git at .agentic/hooks so the pre-commit gate runs for everyone who clones."""
import os
import stat
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
hooks = root / ".agentic" / "hooks"
for h in hooks.iterdir():
    if h.suffix == "" and h.is_file():
        h.chmod(h.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        subprocess.run(["git", "update-index", "--chmod=+x", str(h.relative_to(root)).replace(os.sep, "/")],
                       cwd=root, capture_output=True)
r = subprocess.run(["git", "config", "core.hooksPath", ".agentic/hooks"], cwd=root)
if r.returncode:
    sys.exit("git config failed: is this a git repository?")
print("core.hooksPath = .agentic/hooks\npre-commit gate installed. Try: python .agentic/gate.py")
