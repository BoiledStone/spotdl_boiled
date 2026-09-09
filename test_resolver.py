import importlib.util
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
