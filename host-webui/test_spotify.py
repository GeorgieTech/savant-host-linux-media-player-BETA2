#!/usr/bin/env python3
"""Status-mapping tests for Spotify Connect. No ARM binary required."""
import os
import tempfile
import unittest

from spotify import pulse_socket, resolve_runtime_dir, session_from_status


class SessionFromStatusTests(unittest.TestCase):
    def test_none_is_idle(self):
        sess = session_from_status(None)
        self.assertFalse(sess["active"])
        self.assertEqual(sess["title"], "")

    def test_empty_body_is_idle(self):
        sess = session_from_status({})
        self.assertFalse(sess["active"])
        self.assertEqual(sess["title"], "")
        self.assertEqual(sess["artist"], "")
        self.assertEqual(sess["album"], "")

    def test_stopped_false_without_user_or_track_is_idle(self):
        # The old bug: missing stopped is false, so
        # active = not stopped and bool(track.name or not paused) became True.
        sess = session_from_status({"stopped": False, "paused": False})
        self.assertFalse(sess["active"])

    def test_stopped_true_is_idle(self):
        sess = session_from_status({
            "username": "user",
            "stopped": True,
            "paused": False,
            "track": {"name": "Song", "artist_names": ["A"], "album_name": "Al"},
        })
        self.assertFalse(sess["active"])

    def test_username_without_track_is_active(self):
        sess = session_from_status({"username": "premium-user", "stopped": False})
        self.assertTrue(sess["active"])
        self.assertEqual(sess["title"], "Spotify")
        self.assertEqual(sess["username"], "premium-user")

    def test_playing_track(self):
        sess = session_from_status({
            "username": "user",
            "stopped": False,
            "paused": False,
            "track": {
                "name": "Eulogy",
                "artist_names": ["Kyle Dixon", "Michael Stein"],
                "album_name": "Stranger Things 2",
            },
        })
        self.assertTrue(sess["active"])
        self.assertEqual(sess["title"], "Eulogy")
        self.assertEqual(sess["artist"], "Kyle Dixon, Michael Stein")
        self.assertEqual(sess["album"], "Stranger Things 2")

    def test_paused_with_track_is_active(self):
        sess = session_from_status({
            "username": "user",
            "stopped": False,
            "paused": True,
            "track": {"name": "Song", "artist_names": ["A"], "album_name": "Al"},
        })
        self.assertTrue(sess["active"])
        self.assertEqual(sess["title"], "Song")

    def test_null_track_is_idle(self):
        sess = session_from_status({"username": "", "stopped": False, "track": None})
        self.assertFalse(sess["active"])


class RuntimeDirTests(unittest.TestCase):
    def test_prefers_directory_with_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "opt")
            os.makedirs(path)
            open(os.path.join(path, "go-librespot"), "wb").close()
            self.assertEqual(resolve_runtime_dir(path), os.path.abspath(path))

    def test_missing_binary_still_returns_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope")
            resolved = resolve_runtime_dir(missing)
            self.assertTrue(resolved)


class PulseSocketTests(unittest.TestCase):
    def test_strips_unix_prefix(self):
        old = os.environ.get("PULSE_SERVER")
        os.environ["PULSE_SERVER"] = "unix:/var/run/pulse/native"
        try:
            self.assertEqual(pulse_socket(), "/var/run/pulse/native")
        finally:
            if old is None:
                os.environ.pop("PULSE_SERVER", None)
            else:
                os.environ["PULSE_SERVER"] = old


if __name__ == "__main__":
    unittest.main()
