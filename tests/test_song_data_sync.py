"""Tests for Google Sheets persistence and local JSON migration."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import main


class SongDataSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patches = [
            patch.object(main, "SONG_DATA_SYNC_CONFIG_FILE", os.path.join(self.temp_dir.name, "song_data_sync.json")),
            patch.object(main, "SONG_NOTES_FILE", os.path.join(self.temp_dir.name, "song_notes.json")),
            patch.object(main, "SONG_PLAY_COUNTS_FILE", os.path.join(self.temp_dir.name, "song_play_counts.json")),
        ]
        for path_patch in self.path_patches:
            path_patch.start()
        self.original_notes = main.song_notes
        self.original_counts = main.song_play_counts
        self.original_ready = main._song_data_sync_ready
        self.original_needs_merge = main._song_data_sync_needs_merge
        main.song_notes = {}
        main.song_play_counts = {}
        main._song_data_sync_ready = False
        main._song_data_sync_needs_merge = False

    def tearDown(self):
        main.song_notes = self.original_notes
        main.song_play_counts = self.original_counts
        main._song_data_sync_ready = self.original_ready
        main._song_data_sync_needs_merge = self.original_needs_merge
        for path_patch in reversed(self.path_patches):
            path_patch.stop()
        self.temp_dir.cleanup()

    def _write_config(self):
        with open(main.SONG_DATA_SYNC_CONFIG_FILE, "w", encoding="utf-8") as config_file:
            json.dump({
                "web_app_url": "https://script.google.com/macros/s/test/exec",
                "token": "test-token",
            }, config_file)

    def _write_local_json(self):
        with open(main.SONG_NOTES_FILE, "w", encoding="utf-8") as notes_file:
            json.dump({
                "Local.mp4": {
                    "selected_options": ["需要歌詞"],
                    "key_value": "原 Key",
                    "custom_text": "local note",
                    "updated_at": "2026-11-03T10:00:00+00:00",
                },
            }, notes_file)
        with open(main.SONG_PLAY_COUNTS_FILE, "w", encoding="utf-8") as counts_file:
            json.dump({"Local.mp4": 2, "Shared.mp4": 4}, counts_file)

    def test_reads_sync_settings_from_external_json(self):
        self._write_config()
        self.assertEqual(
            main._gas_song_data_configured(),
            ("https://script.google.com/macros/s/test/exec", "test-token"),
        )

    def test_migration_merges_cloud_and_local_then_deletes_json_after_success(self):
        self._write_config()
        self._write_local_json()
        calls = []

        def fake_post(payload):
            calls.append(payload)
            if payload["action"] == "load":
                return {
                    "ok": True,
                    "data": {
                        "song_notes": {
                            "Local.mp4": {
                                "selected_options": ["需要修正"],
                                "key_value": "原 Key",
                                "custom_text": "remote note",
                                "updated_at": "2026-11-04T10:00:00+00:00",
                            },
                            "Remote.mp4": {
                                "selected_options": [],
                                "key_value": "原 Key",
                                "custom_text": "remote note",
                                "updated_at": "2026-11-02T10:00:00+00:00",
                            },
                        },
                        "song_play_counts": {"Shared.mp4": 10, "Remote.mp4": 5},
                    },
                }
            return {"ok": True}

        with patch.object(main, "_post_song_data_to_gas", side_effect=fake_post), patch.object(
            main.socketio, "emit"
        ) as socket_emit:
            self.assertTrue(main._restore_song_data_from_gas())

        self.assertEqual([call["action"] for call in calls], ["load", "sync"])
        uploaded = calls[1]
        self.assertEqual(uploaded["song_notes"]["Local.mp4"]["custom_text"], "remote note\nlocal note")
        self.assertEqual(
            uploaded["song_notes"]["Local.mp4"]["selected_options"],
            ["需要修正", "需要歌詞"],
        )
        self.assertEqual(uploaded["song_notes"]["Remote.mp4"]["custom_text"], "remote note")
        self.assertEqual(uploaded["song_play_counts"], {
            "Local.mp4": 2,
            "Shared.mp4": 14,
            "Remote.mp4": 5,
        })
        self.assertFalse(os.path.exists(main.SONG_NOTES_FILE))
        self.assertFalse(os.path.exists(main.SONG_PLAY_COUNTS_FILE))
        socket_emit.assert_called_once_with("song_play_counts", main.song_play_counts)

    def test_keeps_json_files_when_cloud_write_fails(self):
        self._write_config()
        self._write_local_json()

        def fail_sync(payload):
            if payload["action"] == "load":
                return {
                    "ok": True,
                    "data": {"song_notes": {}, "song_play_counts": {}},
                }
            raise RuntimeError("simulated write failure")

        with patch.object(main, "_post_song_data_to_gas", side_effect=fail_sync):
            self.assertFalse(main._restore_song_data_from_gas())

        self.assertTrue(os.path.isfile(main.SONG_NOTES_FILE))
        self.assertTrue(os.path.isfile(main.SONG_PLAY_COUNTS_FILE))

    def test_song_list_read_schedules_migration_when_json_exists(self):
        self._write_config()
        self._write_local_json()
        with patch.object(main, "_schedule_song_data_sync_retry") as schedule:
            main._sync_pending_song_json_on_song_read()
        schedule.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
