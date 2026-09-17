"""Build a standalone Windows exe for the Start/Stop control app.

Usage (from the project root, with the venv active):

    python scripts/build_exe.py

Produces ``release/SpotifyMusicBot.exe``. Put ``.env``, ``.tokens.json``,
``.tio.tokens.json``, and ``blocklist.json`` next to that exe (this script
copies them from the project root when present).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
RELEASE = ROOT / "release"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
EXE_NAME = "SpotifyMusicBot"
DATA_FILES = (".env", ".tokens.json", ".tio.tokens.json", "blocklist.json")


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyinstaller>=6.0,<7"]
        )

    RELEASE.mkdir(exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        f"--name={EXE_NAME}",
        f"--paths={SRC}",
        "--collect-all=certifi",
        "--collect-all=twitchio",
        "--hidden-import=main",
        "--hidden-import=control_app",
        "--hidden-import=config",
        "--hidden-import=app_paths",
        "--hidden-import=matching",
        "--hidden-import=playback",
        "--hidden-import=requests_service",
        "--hidden-import=rate_limit",
        "--hidden-import=chat_format",
        "--hidden-import=parsing",
        "--hidden-import=spotify.auth",
        "--hidden-import=spotify.client",
        "--hidden-import=twitch.bot",
        "--hidden-import=twitch.lookup",
        str(SRC / "control_app.py"),
    ]
    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)

    built = DIST / f"{EXE_NAME}.exe"
    if not built.is_file():
        print(f"Expected build output missing: {built}", file=sys.stderr)
        return 1

    target = RELEASE / f"{EXE_NAME}.exe"
    shutil.copy2(built, target)
    print(f"Copied exe -> {target}")

    for name in DATA_FILES:
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, RELEASE / name)
            print(f"Copied {name} -> release/")
        else:
            print(f"Skip {name} (not in project root)")

    print()
    print(f"Ready: {target}")
    print("Desktop shortcut can point at that file; keep the release/ folder intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
