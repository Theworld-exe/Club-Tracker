"""Standalone Desktop Application Launcher for Club Tracker.

Starts the FastAPI analytical engine in a managed background thread and opens
a native desktop application window via Microsoft Edge WebView2 (pywebview).
Cleanly stops all background services when the window is closed.
"""

from __future__ import annotations

import argparse
import ctypes
import io
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

# Base directory resolution (handles source execution and PyInstaller bundles)
if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    APP_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_DIR = BUNDLE_DIR

sys.path.insert(0, str(BUNDLE_DIR))
sys.path.insert(0, str(APP_DIR))

# Ensure persistent desktop log file
LOG_FILE = APP_DIR / "desktop.log"


class SafeLogWriter:
    """Safe stream wrapper that writes to a persistent log file and supports isatty()."""

    def __init__(self, log_path: Path, fallback_stream=None) -> None:
        self.log_path = log_path
        self.fallback = fallback_stream

    def write(self, text: str) -> int:
        if not text:
            return 0
        try:
            with open(self.log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except Exception:
            pass

        if self.fallback is not None:
            try:
                return self.fallback.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        if self.fallback is not None:
            try:
                self.fallback.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False


# In Windows GUI mode (pythonw.exe or PyInstaller --windowed), sys.stdout/stderr are None.
# Uvicorn and other libraries crash if sys.stdout is None or missing isatty().
if sys.stdout is None or not hasattr(sys.stdout, "isatty"):
    sys.stdout = SafeLogWriter(LOG_FILE)
else:
    sys.stdout = SafeLogWriter(LOG_FILE, fallback_stream=sys.stdout)

if sys.stderr is None or not hasattr(sys.stderr, "isatty"):
    sys.stderr = SafeLogWriter(LOG_FILE)
else:
    sys.stderr = SafeLogWriter(LOG_FILE, fallback_stream=sys.stderr)

if sys.stdin is None:
    sys.stdin = io.StringIO()


def log_msg(msg: str) -> None:
    """Write timestamped message to desktop log and stdout."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{stamp}] {msg}\n"
    sys.stdout.write(formatted)
    sys.stdout.flush()


def show_native_alert(title: str, message: str) -> None:
    """Show a native Windows alert dialog box."""
    log_msg(f"[ALERT] {title}: {message}")
    try:
        MB_ICONERROR = 0x00000010
        ctypes.windll.user32.MessageBoxW(0, str(message), str(title), MB_ICONERROR)
    except Exception:
        pass


def find_free_port(preferred: int = 8000, host: str = "127.0.0.1") -> int:
    """Find an available TCP port, preferring 8000 and falling back to 8001..8099."""
    for port in [preferred, *range(8001, 8100)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class UvicornServerThread(threading.Thread):
    """Runs Uvicorn in a daemon thread and supports clean termination on window exit."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(name="uvicorn-worker", daemon=True)
        self.host = host
        self.port = port
        self.server = None
        self.startup_error: str | None = None

    def run(self) -> None:
        try:
            log_msg(f"Initializing Uvicorn on http://{self.host}:{self.port}...")
            import uvicorn

            from main import app

            # log_config=None avoids Uvicorn trying to access terminal color formatters
            config = uvicorn.Config(
                app=app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False,
                workers=1,
                loop="asyncio",
                log_config=None,
            )
            self.server = uvicorn.Server(config)
            log_msg("Uvicorn server running.")
            self.server.run()
            log_msg("Uvicorn server stopped cleanly.")
        except Exception as exc:
            self.startup_error = f"{exc}\n{traceback.format_exc()}"
            log_msg(f"ERROR inside UvicornServerThread: {self.startup_error}")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True


def wait_for_server(url: str, server_thread: UvicornServerThread, timeout_sec: float = 12.0) -> bool:
    """Poll server status endpoint until FastAPI is ready or reports startup failure."""
    probe_url = f"{url.rstrip('/')}/api/settings/status"
    start = time.monotonic()
    while (time.monotonic() - start) < timeout_sec:
        if server_thread.startup_error:
            log_msg("Early abort: server thread encountered an error.")
            return False

        try:
            with urllib.request.urlopen(probe_url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def launch_fallback_browser(app_url: str) -> None:
    """Fallback if pywebview fails: launch standalone Edge app mode or browser."""
    log_msg(f"Launching in desktop fallback browser at {app_url}...")
    edge_paths = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    ]
    edge_exe = next((p for p in edge_paths if os.path.exists(p)), None)
    if edge_exe:
        import subprocess

        cmd = [edge_exe, f"--app={app_url}", "--window-size=1340,880"]
        try:
            proc = subprocess.Popen(cmd)
            proc.wait()
            return
        except Exception as exc:
            log_msg(f"Edge app mode fallback failed: {exc}")

    webbrowser.open(app_url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main() -> None:
    log_msg("============================================================")
    log_msg("  Club Tracker — Standalone Desktop Application Startup")
    log_msg("============================================================")

    parser = argparse.ArgumentParser(description="Club Tracker Desktop Application")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host to bind server to")
    parser.add_argument("--port", type=int, default=8000, help="Preferred port (defaults to 8000)")
    parser.add_argument("--browser", action="store_true", help="Launch in default browser instead of native window")
    parser.add_argument("--debug", action="store_true", help="Enable webview developer tools")
    args = parser.parse_args()

    port = find_free_port(preferred=args.port, host=args.host)
    app_url = f"http://{args.host}:{port}"
    log_msg(f"[1/2] Starting backend on {app_url} ...")

    server_thread = UvicornServerThread(host=args.host, port=port)
    server_thread.start()

    if not wait_for_server(app_url, server_thread=server_thread, timeout_sec=12.0):
        err = server_thread.startup_error or "Server failed to respond within 12 seconds."
        log_msg(f"FATAL ERROR: {err}")
        server_thread.stop()
        show_native_alert(
            "Club Tracker — Startup Failed",
            f"The application backend could not start on {app_url}.\n\n"
            f"Details:\n{err[:400]}\n\n"
            f"Check log file at:\n{LOG_FILE}",
        )
        sys.exit(1)

    log_msg("[2/2] Backend ready! Opening native desktop window...")

    if args.browser:
        try:
            launch_fallback_browser(app_url)
        finally:
            server_thread.stop()
            server_thread.join(timeout=3.0)
        return

    try:
        log_msg("Importing webview module...")
        import webview
        log_msg(f"webview module loaded: {webview}")

        log_msg("Creating native application window...")
        window = webview.create_window(
            title="Club Tracker — University Timetable & Availability Analyzer",
            url=app_url,
            width=1340,
            height=880,
            min_size=(980, 640),
            background_color="#09090b",
            text_select=True,
            confirm_close=False,
        )
        log_msg(f"Window object initialized: {window}. Calling webview.start()...")

        webview.start(debug=args.debug)
        log_msg("webview.start() finished normally.")
    except BaseException as exc:
        log_msg(f"Native webview engine error ({type(exc).__name__}: {exc}). Attempting browser app fallback...")
        try:
            launch_fallback_browser(app_url)
        except Exception as fb_exc:
            log_msg(f"Fallback browser launch failed: {fb_exc}")
            show_native_alert(
                "Club Tracker — Display Error",
                f"Could not open the desktop window or browser.\n\nError: {exc}",
            )
    finally:
        log_msg("Closing background backend server...")
        server_thread.stop()
        server_thread.join(timeout=3.0)
        log_msg("Desktop application closed successfully.")


if __name__ == "__main__":
    main()
