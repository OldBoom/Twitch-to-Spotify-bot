"""Simple Start/Stop window for the song-request bot.

Runs the bot as a child process so Start/Stop is reliable and Twitch/Spotify
tokens stay on disk — authorize once, then toggle with the buttons.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

from app_paths import data_dir, is_frozen, src_dir

STATUS_STOPPED = "Stopped"
STATUS_STARTING = "Starting…"
STATUS_RUNNING = "Running"
STATUS_STOPPING = "Stopping…"

# How often the UI drains the log queue (ms).
LOG_POLL_MS = 100
# Graceful stop wait before kill (seconds).
STOP_GRACE_SECONDS = 8


def _bot_command() -> list[str]:
    if is_frozen():
        return [sys.executable, "--bot"]
    return [sys.executable, "-m", "main"]


def _bot_env() -> dict[str, str]:
    env = os.environ.copy()
    # Force UTF-8 so TwitchIO log lines with en-dashes don't crash the pipe reader.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    source = src_dir()
    if source is not None:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(source) if not existing else f"{source}{os.pathsep}{existing}"
        )
    return env


def _creationflags() -> int:
    # Avoid a flashing console window on Windows when spawning the bot.
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


class ControlApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Spotify Music Bot")
        self.root.minsize(520, 360)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._log_queue: queue.Queue[str | None] = queue.Queue()
        self._status = tk.StringVar(value=STATUS_STOPPED)
        self._data = data_dir()

        self._build_ui()
        self._refresh_token_hint()
        self.root.after(LOG_POLL_MS, self._drain_logs)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(frame)
        header.pack(fill=tk.X, padx=10, pady=6)
        ttk.Label(header, text="Song requests", font=("Segoe UI", 14, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Label(header, textvariable=self._status).pack(side=tk.RIGHT)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, padx=10, pady=6)
        self._start_btn = ttk.Button(buttons, text="Start", command=self.start_bot)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._stop_btn = ttk.Button(
            buttons, text="Stop", command=self.stop_bot, state=tk.DISABLED
        )
        self._stop_btn.pack(side=tk.LEFT)

        self._hint = ttk.Label(frame, wraplength=480, justify=tk.LEFT)
        self._hint.pack(fill=tk.X, padx=10, pady=6)

        ttk.Label(frame, text="Log").pack(anchor=tk.W, padx=10)
        self._log = scrolledtext.ScrolledText(
            frame,
            height=16,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
        )
        self._log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

    def _refresh_token_hint(self) -> None:
        twitch_tokens = self._data / ".tio.tokens.json"
        spotify_tokens = self._data / ".tokens.json"
        parts: list[str] = [
            f"Data folder: {self._data}",
            "Start turns requests on; Stop turns them off.",
        ]
        if twitch_tokens.is_file() and spotify_tokens.is_file():
            parts.append(
                "Saved Twitch and Spotify tokens found — Start should not ask "
                "you to authorize again."
            )
        else:
            missing: list[str] = []
            if not spotify_tokens.is_file():
                missing.append("Spotify (run: python -m spotify.auth)")
            if not twitch_tokens.is_file():
                missing.append(
                    "Twitch (open the OAuth link the log prints on first Start)"
                )
            parts.append("Still needed once: " + "; ".join(missing) + ".")
        self._hint.configure(text="\n".join(parts))

    def _set_running_ui(self, running: bool) -> None:
        self._start_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        self._stop_btn.configure(state=tk.NORMAL if running else tk.DISABLED)

    def _append_log(self, line: str) -> None:
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, line)
        if not line.endswith("\n"):
            self._log.insert(tk.END, "\n")
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _drain_logs(self) -> None:
        while True:
            try:
                item = self._log_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._on_process_exit()
                continue
            self._append_log(item)
        self.root.after(LOG_POLL_MS, self._drain_logs)

    def start_bot(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        env_path = self._data / ".env"
        if not env_path.is_file():
            self._append_log(f"Missing {env_path} — copy .env.example and fill it in.")
            self._status.set(STATUS_STOPPED)
            return

        self._status.set(STATUS_STARTING)
        self._set_running_ui(True)
        self._append_log("--- starting bot ---")
        try:
            self._proc = subprocess.Popen(
                _bot_command(),
                cwd=str(self._data),
                env=_bot_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_creationflags(),
            )
        except OSError as exc:
            self._append_log(f"Failed to start: {exc}")
            self._status.set(STATUS_STOPPED)
            self._set_running_ui(False)
            return

        self._status.set(STATUS_RUNNING)
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="bot-log-reader",
            daemon=True,
        )
        self._reader.start()
        self._refresh_token_hint()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._log_queue.put(None)
            return
        try:
            for line in proc.stdout:
                self._log_queue.put(line.rstrip("\r\n"))
        finally:
            self._log_queue.put(None)

    def _on_process_exit(self) -> None:
        code = self._proc.returncode if self._proc is not None else None
        self._proc = None
        self._reader = None
        self._status.set(STATUS_STOPPED)
        self._set_running_ui(False)
        self._append_log(f"--- bot exited (code {code}) ---")
        self._refresh_token_hint()

    def stop_bot(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._status.set(STATUS_STOPPING)
        self._append_log("--- stopping bot ---")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except OSError as exc:
            self._append_log(f"Stop failed: {exc}")

    def _on_close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self.stop_bot()
            # Give the reader a moment to flush exit status into the queue.
            self.root.after(200, self.root.destroy)
            return
        self.root.destroy()


def main() -> None:
    # Frozen exe may be launched with --bot to run headless (used by Start).
    if is_frozen() and "--bot" in sys.argv:
        from main import main as bot_main

        bot_main()
        return

    root = tk.Tk()
    # Prefer a crisp default look on Windows.
    try:
        root.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    ControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
