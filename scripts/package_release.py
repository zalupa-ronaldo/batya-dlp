"""Run after the packaged-EXE smoke test passes."""

import hashlib
import shutil
from pathlib import Path

root = Path(__file__).resolve().parent.parent
archive = Path(shutil.make_archive(str(root / "dist/BatyaDLP-Windows-x64"), "zip", root / "dist", "BatyaDLP"))
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(root / "dist/SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
print(f"{archive.name}: {archive.stat().st_size / 1024 / 1024:.1f} MiB; SHA256 {digest}")
