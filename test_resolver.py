import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import spotdl_config
from spotdl_gui import build_resolver_command


MODULE_PATH = Path(__file__).with_name("download_missing_autonomous_v2.py")
SPEC = importlib.util.spec_from_file_location("spotdl_resolver", MODULE_PATH)
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


class ResolverParsingTests(unittest.TestCase):
    def test_accepts_spotify_url_and_uri(self):
        self.assertEqual(
            resolver.normalize_spotify_target(
                "https://open.spotify.com/playlist/0rFIvkUL9MgfkyVU50zC42?si=test"
            ),
            ("playlist", "0rFIvkUL9MgfkyVU50zC42"),
        )
        self.assertEqual(
            resolver.normalize_spotify_target("spotify:playlist:0rFIvkUL9MgfkyVU50zC42"),
            ("playlist", "0rFIvkUL9MgfkyVU50zC42"),
        )

    def test_extracts_markdown_spotify_link(self):
        value = "[ma playlist](https://open.spotify.com/playlist/abc123)"
        self.assertEqual(
            resolver.sanitize_spotify_input(value),
            "https://open.spotify.com/playlist/abc123",
        )

    def test_normalizes_unicode_filename_safely(self):
        self.assertEqual(
            resolver.normalize_filename("Beyoncé: Live/Track?"),
            "Beyonce Live Track",
        )

    def test_bounds_long_audio_filename_components(self):
        value = resolver.normalize_filename("x" * 300)
        self.assertLessEqual(value, "x" * resolver.MAX_FILENAME_COMPONENT_LENGTH)
        self.assertEqual(len(value), resolver.MAX_FILENAME_COMPONENT_LENGTH)
        self.assertEqual(
            resolver.audio_output_stem("", ""),
            "Artiste inconnu - Titre inconnu",
        )

    def test_parses_duration_variants(self):
        self.assertEqual(resolver.coerce_duration_seconds("3:05"), 185)
        self.assertEqual(resolver.coerce_duration_seconds("1:02:03"), 3723)
        self.assertEqual(resolver.coerce_duration_seconds(185.4), 185)
        self.assertIsNone(resolver.coerce_duration_seconds(float("nan")))

    def test_rejects_low_confidence_candidate(self):
        candidate = {
            "title": "Unrelated upload",
            "artist": "Other Artist",
            "duration": 240,
        }
        self.assertFalse(
            resolver.is_fallback_candidate_acceptable(
                candidate, 100, "Artist", "Track", 180
            )
        )

    def test_rejects_unrequested_title_variant_even_with_high_score(self):
        candidate = {
            "title": "Mega Man 3 - Top Man Stage METALIZED",
            "artist": "Capcom Sound Team",
            "duration": 109,
        }
        self.assertFalse(
            resolver.is_fallback_candidate_acceptable(
                candidate, 177, "Capcom Sound Team", "Top Man Stage", 109
            )
        )

    def test_gui_builds_argument_list_without_shell_interpolation(self):
        command = build_resolver_command(
            "python.exe",
            "download_missing_autonomous_v2.py",
            "https://open.spotify.com/playlist/example",
            ".\\downloads",
            3,
        )
        self.assertEqual(
            command,
            [
                "python.exe",
                "download_missing_autonomous_v2.py",
                "--playlist",
                "https://open.spotify.com/playlist/example",
                "--output",
                ".\\downloads",
                "--workers",
                "3",
            ],
        )

    def test_thread_pool_switch_targets_windows(self):
        with patch.object(resolver.os, "name", "nt"):
            self.assertTrue(resolver.use_thread_pool())

        with patch.object(resolver.os, "name", "posix"):
            self.assertFalse(resolver.use_thread_pool())

    def test_candidate_enrichment_is_cached_during_ranking(self):
        candidate = {"id": "cached-video", "title": "Track", "duration": 180}
        cache = {}
        with patch.object(resolver, "enrich_entry", wraps=resolver.enrich_entry) as enrich:
            resolver.pick_ranked([candidate], None, "Track", 180, "Track", enriched_cache=cache)
            resolver.pick_ranked([candidate], None, "Track", 180, "Track", enriched_cache=cache)
        self.assertEqual(enrich.call_count, 1)

    def test_local_settings_save_contains_only_public_preferences(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            settings_path = Path(temporary_dir) / ".spotdl-settings.json"
            with patch.object(spotdl_config, "LOCAL_SETTINGS_PATH", settings_path):
                spotdl_config.save_local_settings(
                    "spotify:playlist:test", "D:/download", 2, theme="light"
                )
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {
            "playlist_url": "spotify:playlist:test",
            "output_dir": "D:/download",
            "workers": 2,
            "theme": "light",
        })


if __name__ == "__main__":
    unittest.main()
