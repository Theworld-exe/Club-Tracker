"""Build script to compile Club Tracker into a standalone Windows desktop executable.

Usage:
    python build_desktop.py          # Builds standalone folder (dist/ClubTracker/ClubTracker.exe)
    python build_desktop.py --onefile # Builds single-file executable (dist/ClubTracker.exe)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def build_executable(onefile: bool = False, include_env: bool = False) -> None:
    print("============================================================")
    print("  Building Club Tracker Standalone Windows Desktop App")
    print("============================================================")

    static_dir = BASE_DIR / "static"
    if not static_dir.exists():
        print("ERROR: 'static' directory not found.")
        sys.exit(1)

    venv_candidates = [
        BASE_DIR / ".venv" / "Scripts" / "pyinstaller.exe",
        BASE_DIR / ".venv" / "bin" / "pyinstaller",
    ]
    pyinstaller_exe = next((p for p in venv_candidates if p.exists()), None)
    executable = str(pyinstaller_exe) if pyinstaller_exe else "pyinstaller"

    # Use a relative --add-data path with os.pathsep so ClubTracker.spec stays portable
    add_data_arg = f"static{os.pathsep}static"

    cmd = [
        executable,
        "--name=ClubTracker",
        "--noconfirm",
        "--clean",
        "--windowed",  # Windows GUI mode without command console
        f"--add-data={add_data_arg}",
        "--collect-all=webview",
        "--collect-all=pythonnet",
        "--collect-all=clr_loader",
        "--collect-all=uvicorn",
        "--hidden-import=uvicorn.logging",
        "--hidden-import=uvicorn.loops",
        "--hidden-import=uvicorn.loops.auto",
        "--hidden-import=uvicorn.protocols",
        "--hidden-import=uvicorn.protocols.http",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.lifespans",
        "--hidden-import=uvicorn.lifespans.on",
        "--hidden-import=webview.platforms.winforms",
        "--hidden-import=google.genai",
        "--hidden-import=pydantic",
        "--hidden-import=sqlite3",
        "--hidden-import=ctypes",
        "desktop.py",
    ]

    if onefile:
        cmd.append("--onefile")
        print("[Mode] Building Single-File Executable (--onefile)...")
    else:
        cmd.append("--onedir")
        print("[Mode] Building Standalone App Directory (--onedir, recommended for instant startup)...")

    print(f"[Command] {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=str(BASE_DIR))
    if result.returncode != 0:
        print("\nERROR: PyInstaller build failed.")
        sys.exit(result.returncode)

    out_target = BASE_DIR / "dist" / ("ClubTracker.exe" if onefile else "ClubTracker/ClubTracker.exe")
    if not onefile:
        dist_dir = BASE_DIR / "dist" / "ClubTracker"
        # Copy WebView2 binaries alongside executable for seamless loader discovery
        webview2_src = dist_dir / "_internal" / "webview" / "lib" / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll"
        if webview2_src.exists():
            shutil.copy2(webview2_src, dist_dir / "WebView2Loader.dll")
        # Copy .env.example template into dist so releases never accidentally leak local secrets
        env_example_src = BASE_DIR / ".env.example"
        if env_example_src.exists():
            shutil.copy2(env_example_src, dist_dir / ".env.example")
        if include_env:
            env_src = BASE_DIR / ".env"
            if env_src.exists():
                print(
                    "WARNING: Copying local .env into dist/ClubTracker/.env "
                    "(--include-env). Do NOT distribute this folder publicly."
                )
                shutil.copy2(env_src, dist_dir / ".env")

    print("============================================================")
    print("  BUILD SUCCESSFUL!")
    print(f"  Executable created at: {out_target}")
    print("============================================================")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Club Tracker Desktop App")
    parser.add_argument("--onefile", action="store_true", help="Build a single standalone .exe file")
    parser.add_argument(
        "--include-env",
        action="store_true",
        help="Copy local .env into dist/ClubTracker/ (for private local builds only; never publish)",
    )
    args = parser.parse_args()
    build_executable(onefile=args.onefile, include_env=args.include_env)

