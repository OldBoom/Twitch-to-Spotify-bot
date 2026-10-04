"""Build a standalone Windows exe for the Start/Stop control app.

Usage (from the project root, with the venv active):

    python scripts/build_exe.py

Produces ``release/SpotifyMusicBot.exe``. Puts ``.env``, ``.tokens.json``,
``.tio.tokens.json``, and ``blocklist.json`` next to that exe.

Token files are special: the running bot refreshes them inside ``release/``, so
a naive root→release copy would wipe a working login with a stale project-root
copy. For those files this script keeps whichever side is newer and syncs it
both ways.
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
# OAuth sessions — never clobber a fresher copy written by a running bot.
TOKEN_FILES = frozenset({".tokens.json", ".tio.tokens.json"})


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
        _sync_data_file(name)

    print()
    print(f"Ready: {target}")
    print("Desktop shortcut can point at that file; keep the release/ folder intact.")
    return 0


def _sync_data_file(name: str) -> None:
    root_path = ROOT / name
    release_path = RELEASE / name

    if name in TOKEN_FILES:
        _sync_tokens(name, root_path, release_path)
        return

    if root_path.is_file():
        shutil.copy2(root_path, release_path)
        print(f"Copied {name} -> release/")
    else:
        print(f"Skip {name} (not in project root)")


def _sync_tokens(name: str, root_path: Path, release_path: Path) -> None:
    """Keep the newer OAuth session and mirror it to the other side."""
    root_ok = root_path.is_file()
    release_ok = release_path.is_file()

    if root_ok and release_ok:
        if release_path.stat().st_mtime >= root_path.stat().st_mtime:
            shutil.copy2(release_path, root_path)
            print(f"Kept newer release/{name}; synced -> project root")
        else:
            shutil.copy2(root_path, release_path)
            print(f"Copied newer {name} -> release/")
        return

    if root_ok:
        shutil.copy2(root_path, release_path)
        print(f"Copied {name} -> release/")
        return

    if release_ok:
        shutil.copy2(release_path, root_path)
        print(f"Synced release/{name} -> project root")
        return

    print(f"Skip {name} (missing on both sides — authorize after first Start)")


if __name__ == "__main__":
    raise SystemExit(main())
