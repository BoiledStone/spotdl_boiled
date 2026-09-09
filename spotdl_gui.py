"""Small Windows-friendly graphical launcher for the SpotDL resolver."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


PROJECT_DIR = Path(__file__).resolve().parent
RESOLVER_SCRIPT = PROJECT_DIR / "download_missing_autonomous_v2.py"
DOTENV_PATH = Path(os.getenv("BOT_ENV_FILE") or PROJECT_DIR / ".env")


def build_resolver_command(python_executable, script_path, playlist, output, workers):
    """Build the resolver command without shell interpolation."""
    command = [str(python_executable), str(script_path)]
    if playlist and playlist.strip():
        command.extend(["--playlist", playlist.strip()])
    if output and output.strip():
        command.extend(["--output", output.strip()])
    command.extend(["--workers", str(workers)])
    return command


def read_dotenv_value(name, path=None):
    """Read one simple dotenv value without importing or exposing the full file."""
    dotenv_path = Path(path or DOTENV_PATH)
    try:
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ""

    prefix = f"{name}="
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return ""


class ResolverApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SpotDL Resolver")
        self.root.geometry("760x560")
        self.root.minsize(660, 460)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.playlist = tk.StringVar(value=os.getenv("SPOTDL_PLAYLIST_URL") or read_dotenv_value("SPOTDL_PLAYLIST_URL"))
        configured_output = os.getenv("SPOTDL_OUTPUT_DIR") or read_dotenv_value("SPOTDL_OUTPUT_DIR")
        self.output = tk.StringVar(value=configured_output or str(PROJECT_DIR / "downloads"))
        self.workers = tk.IntVar(value=self._configured_workers())
        self.status = tk.StringVar(value="Prêt")
        self.queue = queue.Queue()
        self.process = None
        self.reader_thread = None
        self.stop_requested = False

        self._configure_style()
        self._build_ui()
        self.root.after(100, self._drain_queue)

    def _configured_workers(self):
        raw = os.getenv("SPOTDL_WORKERS") or read_dotenv_value("SPOTDL_WORKERS")
        try:
            return min(5, max(1, int(raw)))
        except (TypeError, ValueError):
            return 2

    def _configure_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Muted.TLabel", foreground="#5f6368")
        style.configure("Action.TButton", padding=(12, 7))

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(4, weight=1)

        ttk.Label(outer, text="SpotDL Resolver", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            outer,
            text="Télécharge les pistes manquantes d’une playlist Spotify vers un dossier local.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 18))

        ttk.Label(outer, text="Playlist Spotify").grid(row=2, column=0, sticky="w", pady=6)
        playlist_entry = ttk.Entry(outer, textvariable=self.playlist)
        playlist_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=6)
        playlist_entry.focus_set()

        ttk.Label(outer, text="Dossier de sortie").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(outer, textvariable=self.output).grid(row=3, column=1, sticky="ew", pady=6)
        self.browse_button = ttk.Button(outer, text="Parcourir…", command=self.choose_output)
        self.browse_button.grid(row=3, column=2, sticky="e", padx=(8, 0), pady=6)

        ttk.Label(outer, text="Workers").grid(row=4, column=0, sticky="nw", pady=(12, 6))
        workers = ttk.Spinbox(outer, from_=1, to=5, textvariable=self.workers, width=5)
        workers.grid(row=4, column=1, sticky="nw", pady=(12, 6))

        log_frame = ttk.LabelFrame(outer, text="Journal", padding=8)
        log_frame.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        outer.rowconfigure(5, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="word", height=15, state="disabled", undo=False)
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Label(actions, textvariable=self.status, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.open_button = ttk.Button(actions, text="Ouvrir le dossier", command=self.open_output)
        self.open_button.grid(row=0, column=1, padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="Arrêter", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=2, padx=(8, 0))
        self.start_button = ttk.Button(
            actions, text="Démarrer", style="Action.TButton", command=self.start
        )
        self.start_button.grid(row=0, column=3, padx=(8, 0))

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running):
        state = "disabled" if running else "normal"
        self.start_button.configure(state=state)
        self.browse_button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")

    def choose_output(self):
        current_output = self._resolve_output_path(self.output.get())
        selected = filedialog.askdirectory(
            parent=self.root,
            initialdir=str(current_output if current_output.is_dir() else PROJECT_DIR),
            title="Choisir le dossier de sortie",
        )
        if selected:
            self.output.set(selected)

    @staticmethod
    def _resolve_output_path(value):
        output = Path(value.strip() if value else "downloads")
        return output if output.is_absolute() else PROJECT_DIR / output

    def open_output(self):
        output = self._resolve_output_path(self.output.get())
        try:
            output.mkdir(parents=True, exist_ok=True)
            os.startfile(output)  # noqa: S606 - Windows shell association is intentional.
        except OSError as exc:
            messagebox.showerror("Dossier inaccessible", str(exc))

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        if not RESOLVER_SCRIPT.is_file():
            messagebox.showerror("Fichier manquant", f"Resolver introuvable :\n{RESOLVER_SCRIPT}")
            return
        try:
            worker_count = int(self.workers.get())
        except (TypeError, ValueError):
            messagebox.showerror("Workers invalides", "Choisis une valeur entre 1 et 5.")
            return
        if not 1 <= worker_count <= 5:
            messagebox.showerror("Workers invalides", "Choisis une valeur entre 1 et 5.")
            return
        if not self.playlist.get().strip() and not read_dotenv_value("SPOTDL_PLAYLIST_URL"):
            messagebox.showwarning("Playlist manquante", "Renseigne une URL Spotify ou configure .env.")
            return

        command = build_resolver_command(
            sys.executable,
            RESOLVER_SCRIPT,
            self.playlist.get(),
            self.output.get(),
            worker_count,
        )
        self.stop_requested = False
        self._append_log("\n$ " + subprocess.list2cmdline(command) + "\n\n")
        self.status.set("Exécution en cours…")
        self._set_running(True)
        try:
            self.process = subprocess.Popen(
                command,
                cwd=PROJECT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self.process = None
            self._set_running(False)
            self.status.set("Erreur de démarrage")
            messagebox.showerror("Démarrage impossible", str(exc))
            return

        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()

    def _read_output(self):
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.queue.put(("output", line))
        self.queue.put(("finished", process.wait()))

    def _drain_queue(self):
        try:
            while True:
                event, value = self.queue.get_nowait()
                if event == "output":
                    self._append_log(value)
                elif event == "finished":
                    self._process_finished(value)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _process_finished(self, code):
        self.process = None
        self._set_running(False)
        if self.stop_requested:
            self.status.set("Arrêté")
        elif code == 0:
            self.status.set("Terminé avec succès")
        else:
            self.status.set(f"Terminé avec le code {code}")
        self._append_log(f"\n[GUI] Fin du resolver, code {code}.\n")

    def stop(self):
        process = self.process
        if process is None or process.poll() is not None:
            return
        self.stop_requested = True
        self.status.set("Arrêt en cours…")
        try:
            process.terminate()
        except OSError:
            return
        self._append_log("\n[GUI] Arrêt demandé.\n")

    def close(self):
        if self.process is not None and self.process.poll() is None:
            if not messagebox.askyesno("Quitter", "Une exécution est en cours. L’arrêter et fermer ?"):
                return
            self.stop()
            self.root.after(300, self._close_when_stopped)
            return
        self.root.destroy()

    def _close_when_stopped(self):
        if self.process is not None and self.process.poll() is None:
            self.root.after(300, self._close_when_stopped)
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    ResolverApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
