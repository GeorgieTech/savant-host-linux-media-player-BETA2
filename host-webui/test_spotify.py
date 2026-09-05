#!/usr/bin/env python3
"""Level-1 tests for Spotify Connect status mapping (no ARM binary)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spotify import Spotify, resolve_runtime_dir, session_from_status


class SessionFromStatusTests(unittest.TestCase):
    def test_empty_and_204_are_idle(self):
        for payload in (None, {}, []):
            active, title, artist, album = session_from_status(payload)
            self.assertFalse(active, payload)
            self.assertEqual((title, artist, album), ("", "", ""))

    def test_stopped_session_is_idle(self):
        active, title, _, _ = session_from_status(
            {"stopped": True, "paused": False, "username": "u", "track": {"name": "X"}}
        )
        self.assertFalse(active)
        self.assertEqual(title, "")

    def test_playing_track_is_active(self):
        active, title, artist, album = session_from_status(
            {
                "stopped": False,
                "paused": False,
                "username": "u",
                "track": {
                    "name": "Eulogy",
                    "artist_names": ["Kyle Dixon", "Michael Stein"],
                    "album_name": "Stranger Things 2",
                },
            }
        )
        self.assertTrue(active)
        self.assertEqual(title, "Eulogy")
        self.assertEqual(artist, "Kyle Dixon, Michael Stein")
        self.assertEqual(album, "Stranger Things 2")

    def test_paused_with_track_still_owns_speaker(self):
        active, title, _, _ = session_from_status(
            {"stopped": False, "paused": True, "username": "u", "track": {"name": "Eulogy"}}
        )
        self.assertTrue(active)
        self.assertEqual(title, "Eulogy")

    def test_old_bug_empty_not_stopped_is_not_active(self):
        # Previous code: active = not stopped and bool(name or not paused)
        # with stopped missing/false and no track → True. That locked the UI.
        active, _, _, _ = session_from_status({"stopped": False, "paused": False})
        self.assertFalse(active)

    def test_username_without_track_name_is_active(self):
        active, title, _, _ = session_from_status(
            {"stopped": False, "paused": False, "username": "premium-user", "track": {}}
        )
        self.assertTrue(active)
        self.assertEqual(title, "")


class ResolveAndSnapshotTests(unittest.TestCase):
    def test_resolve_falls_back_to_repo_tree(self):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify")
        self.assertEqual(resolve_runtime_dir("/no/such/opt/spotify"), here)

    def test_snapshot_idle_when_binary_present_but_not_started(self):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify")
        with tempfile.TemporaryDirectory() as tmp:
            sp = Spotify(here, tmp, name="Gigawatt")
            snap = sp.snapshot()
            self.assertTrue(snap["available"])
            self.assertFalse(snap["enabled"])
            self.assertFalse(snap["active"])
            self.assertEqual(snap["title"], "")
            conf = os.path.join(tmp, "spotify", "config.yml")
            self.assertTrue(os.path.isfile(conf))
            with open(conf) as fh:
                text = fh.read()
            self.assertIn("audio_backend: pulseaudio", text)
            self.assertIn("device_name: Gigawatt", text)

    def test_apply_status_clears_stale_now_playing(self):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify")
        with tempfile.TemporaryDirectory() as tmp:
            sp = Spotify(here, tmp)
            with sp.lock:
                sp._apply_status_locked(
                    {"stopped": False, "username": "u", "track": {"name": "A", "artist_names": ["B"]}}
                )
                self.assertTrue(sp.active)
                self.assertEqual(sp.title, "A")
                sp._apply_status_locked(None)
                self.assertFalse(sp.active)
                self.assertEqual(sp.title, "")


if __name__ == "__main__":
    unittest.main()
