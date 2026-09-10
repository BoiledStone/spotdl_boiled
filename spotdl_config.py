"""Shared public and local configuration handling for SpotDL Resolver."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
PUBLIC_CONFIG_PATH = PROJECT_DIR / "spotdl.config.json"
LOCAL_SETTINGS_PATH = PROJECT_DIR / ".spotdl-settings.json"
ENV_FILE = Path(os.getenv("BOT_ENV_FILE") or PROJECT_DIR / ".env")
VALID_THEMES = {"dark", "light"}


def _load_environment():
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)


def _read_json(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def normalize_theme(value):
    """Return a supported interface theme, with dark as the safe default."""
    theme = str(value or "").strip().lower()
    return theme if theme in VALID_THEMES else "dark"


def load_effective_settings():
    """Return public defaults, local saved settings, then environment overrides."""
    settings = {
        "playlist_url": "",
        "output_dir": ".\\downloads",
        "workers": 2,
        "theme": "dark",
    }
    settings.update(_read_json(PUBLIC_CONFIG_PATH))
    settings.update(_read_json(LOCAL_SETTINGS_PATH))

    environment_names = {
        "playlist_url": "SPOTDL_PLAYLIST_URL",
        "output_dir": "SPOTDL_OUTPUT_DIR",
        "workers": "SPOTDL_WORKERS",
    }
    for key, environment_name in environment_names.items():
        value = os.getenv(environment_name)
        if value is not None and value.strip():
            settings[key] = value.strip()
    settings["theme"] = normalize_theme(settings.get("theme"))
    return settings


def resolve_output_dir(value):
    output = Path(str(value or ".\\downloads").strip())
    return output if output.is_absolute() else PROJECT_DIR / output


def save_local_settings(playlist_url, output_dir, workers, *, theme=None):
    """Persist non-secret GUI preferences outside the tracked public config."""
    payload = {
        "playlist_url": str(playlist_url or "").strip(),
        "output_dir": str(output_dir or "").strip(),
        "workers": int(workers),
    }
    saved_theme = theme if theme is not None else _read_json(LOCAL_SETTINGS_PATH).get("theme")
    if saved_theme is not None:
        payload["theme"] = normalize_theme(saved_theme)
    temporary_path = LOCAL_SETTINGS_PATH.with_name(f".{LOCAL_SETTINGS_PATH.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(LOCAL_SETTINGS_PATH)
    except Exception:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


_load_environment()
