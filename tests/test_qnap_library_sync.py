"""Regression tests for the persistent QNAP library index and sync entrypoints."""

import os
import json
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from flask import Flask

import qnap_share_cloud as qnap


class QnapLibrarySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patches = [
            patch.object(
                qnap,
                "ENV_LIBRARY_MANIFEST_DIR",
                os.path.join(self.temp_dir.name, "qnap_shared"),
            ),
            patch.object(
                qnap,
                "ENV_LIBRARY_INDEX_PATH",
                os.path.join(self.temp_dir.name, "qnap_shared", "library.sqlite3"),
            ),
            patch.object(
                qnap,
                "ENV_LIBRARY_STATUS_PATH",
                os.path.join(self.temp_dir.name, "qnap_shared", "status.json"),
            ),
        ]
        for path_patch in self.path_patches:
            path_patch.start()
        with qnap._env_share_lock:
            qnap._env_share_cache.clear()
            qnap._env_sync_status.update({
                "state": "idle",
                "phase": "idle",
                "loaded": 0,
                "total": 0,
                "current_offset": 0,
                "failed_offsets": [],
                "error": "",
                "completed_at": None,
            })

    def tearDown(self):
        with qnap._env_share_lock:
            qnap._env_share_cache.clear()
            qnap._env_sync_status.update({
                "state": "idle",
                "phase": "idle",
                "loaded": 0,
                "total": 0,
                "current_offset": 0,
                "failed_offsets": [],
                "error": "",
                "completed_at": None,
            })
        for path_patch in reversed(self.path_patches):
            path_patch.stop()
        self.temp_dir.cleanup()

    def test_existing_sqlite_index_restores_state_without_manifest_items(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_01",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_01",
            "folder_name": "KTV",
            "_cached_items": [{"filename": "should-not-be-persisted.mp4"}],
        }
        qnap._write_library_index_batch(
            cache_key,
            [{"filename": "song.mp4", "filesize": 123}],
            1,
            1,
            1,
            "ready",
            state,
        )

        with patch.object(
            qnap,
            "_load_env_library_manifest",
            side_effect=AssertionError("SQLite metadata should be sufficient"),
        ):
            restored = qnap._load_cached_env_library_state(cache_key)

        items, sync = qnap._read_library_index_page(cache_key, 0, 20)
        self.assertEqual(items[0]["filename"], "song.mp4")
        self.assertEqual(restored["folder_fid"], "Folder_01")
        self.assertEqual(restored["_cached_items"], [])
        self.assertEqual(restored["password"], "share-password")
        self.assertNotIn("password", sync["state_metadata"])
        self.assertNotIn("_cached_items", sync["state_metadata"])

    def test_combined_page_returns_loading_during_active_snapshot_restore(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_01",
            "share-password",
        )
        with qnap._env_share_lock:
            qnap._env_share_cache.update({
                "fetching_key": cache_key,
                "fetching_at": time.time(),
            })

        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=dict,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )
        with patch.object(
            qnap,
            "_env_share_credentials",
            return_value=(cache_key[0], cache_key[2]),
        ), patch.object(
            qnap,
            "_allowed_share_url",
            return_value=cache_key[:2],
        ), patch.object(
            qnap,
            "_load_cached_env_library_state",
            side_effect=AssertionError("Room request must not query SQLite during restore"),
        ):
            response = app.test_client().get(
                "/api/nas/library-songs/combined-page?offset=0&limit=20"
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["loading"])

    def test_concurrent_library_index_initialization_is_serialized(self):
        def open_and_check(_):
            connection = qnap._open_library_index()
            try:
                return connection.execute(
                    "SELECT COUNT(*) FROM nas_library_items"
                ).fetchone()[0]
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(open_and_check, range(16)))

        self.assertEqual(results, [0] * 16)
        connection = sqlite3.connect(qnap.ENV_LIBRARY_INDEX_PATH)
        try:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                "wal",
            )
        finally:
            connection.close()

    def test_library_index_uses_short_init_lock_wait_then_restores_query_timeout(self):
        observed_timeouts = []

        def capture_init_timeout(connection):
            observed_timeouts.append(
                connection.execute("PRAGMA busy_timeout").fetchone()[0]
            )

        with patch.object(
            qnap,
            "_initialize_library_index",
            side_effect=capture_init_timeout,
        ):
            connection = qnap._open_library_index()
            try:
                query_timeout = connection.execute(
                    "PRAGMA busy_timeout"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(
            observed_timeouts,
            [qnap.LIBRARY_INDEX_INIT_BUSY_TIMEOUT_MS],
        )
        self.assertEqual(query_timeout, qnap.LIBRARY_INDEX_BUSY_TIMEOUT_MS)

    def test_startup_resumes_from_persisted_cursor(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_02",
            "share-password",
        )
        qnap._write_library_index_batch(
            cache_key,
            [{"filename": "song.mp4"}],
            40,
            40,
            40,
            "ready",
            {
                "folder_fid": "Folder_02",
                "folder_name": "KTV",
            },
        )

        with patch.object(
            qnap,
            "_env_share_credentials",
            return_value=(
                "https://nas.example.myqnapcloud.com/share.cgi?ssid=Share_02",
                "share-password",
            ),
        ), patch.object(
            qnap,
            "_read_library_index_state",
            side_effect=AssertionError("API startup must not query SQLite"),
        ), patch.object(
            qnap,
            "_load_cached_env_library_state",
            side_effect=AssertionError("API startup must not query SQLite"),
        ), patch.object(qnap.threading, "Thread") as thread_factory:
            self.assertTrue(qnap.start_qnap_background_sync())

        self.assertEqual(qnap._env_sync_status["current_offset"], 0)
        self.assertIs(
            thread_factory.call_args.kwargs["target"],
            qnap._run_env_library_sync_safely,
        )
        args = thread_factory.call_args.kwargs["args"]
        self.assertEqual(args[0], cache_key)
        self.assertFalse(args[2])

    def test_combined_page_starts_cache_loading_in_background(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_12",
            "share-password",
        )
        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=dict,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )

        with patch.object(
            qnap,
            "_env_share_credentials",
            return_value=(cache_key[0], cache_key[2]),
        ), patch.object(
            qnap,
            "_allowed_share_url",
            return_value=cache_key[:2],
        ), patch.object(
            qnap,
            "start_qnap_background_sync",
            return_value=True,
        ) as start_sync, patch.object(
            qnap,
            "_load_cached_env_library_state",
            side_effect=AssertionError("Request handler must not query SQLite"),
        ):
            response = app.test_client().get(
                "/api/nas/library-songs/combined-page?offset=0&limit=20"
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["loading"])
        start_sync.assert_called_once_with()

    def test_worker_clears_queued_state_when_preflight_raises(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_11",
            "share-password",
        )
        with qnap._env_share_lock:
            qnap._env_share_cache.update({
                "fetching_key": cache_key,
                "fetching_at": time.time(),
            })
            qnap._env_sync_status.update({
                "state": "starting",
                "phase": "queued",
                "requests_started": 0,
            })

        with patch.object(
            qnap,
            "_read_library_index_state",
            side_effect=sqlite3.OperationalError(
                "database is locked share-password"
            ),
        ), patch.object(
            qnap,
            "_schedule_next_env_sync",
        ) as schedule_next, self.assertLogs(qnap.logger, level="ERROR") as logs:
            qnap._run_env_library_sync_safely(cache_key)

        self.assertEqual(qnap._env_sync_status["state"], "error")
        self.assertEqual(qnap._env_sync_status["phase"], "failed")
        self.assertEqual(
            qnap._env_sync_status["error_type"],
            "OperationalError",
        )
        self.assertEqual(
            qnap._env_sync_status["error"],
            "database is locked [redacted]",
        )
        self.assertIsNone(qnap._env_share_cache.get("fetching_key"))
        self.assertEqual(qnap._env_share_cache["error_key"], cache_key)
        self.assertEqual(
            str(qnap._env_share_cache["error"]),
            "database is locked [redacted]",
        )
        self.assertIn("[redacted]", "\n".join(logs.output))
        self.assertNotIn("share-password", "\n".join(logs.output))
        schedule_next.assert_called_once_with(None)

    def test_worker_reports_cache_loading_phase_before_preflight(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_13",
            "share-password",
        )
        with qnap._env_share_lock:
            qnap._env_share_cache.update({
                "fetching_key": cache_key,
                "fetching_at": time.time(),
            })

        observed_phases = []

        def load_cached_state(_cache_key):
            observed_phases.append(qnap._env_sync_status["phase"])
            return None

        with patch.object(
            qnap,
            "_load_cached_env_library_state",
            side_effect=load_cached_state,
        ), patch.object(
            qnap,
            "_run_env_library_sync",
        ):
            qnap._run_env_library_sync_safely(cache_key)

        self.assertEqual(observed_phases, ["loading_cache"])

    def test_sync_reports_sqlite_state_check_phase_before_read(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_16",
            "share-password",
        )
        with patch.object(
            qnap,
            "_read_library_index_state",
            side_effect=RuntimeError("stop after phase check"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after phase check"):
                qnap._run_env_library_sync(cache_key)

        self.assertEqual(
            qnap._env_sync_status["phase"],
            "checking_cache_state",
        )

    def test_sync_reports_gas_snapshot_phase_before_request(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_17",
            "share-password",
        )
        with patch.object(qnap, "_read_library_index_state", return_value=None), \
                patch.object(qnap, "_gas_library_configured", return_value=True), \
                patch.object(
                    qnap,
                    "_restore_gas_library_snapshot",
                    side_effect=RuntimeError("stop after phase check"),
                ):
            with self.assertRaisesRegex(RuntimeError, "stop after phase check"):
                qnap._run_env_library_sync(cache_key)

        self.assertEqual(
            qnap._env_sync_status["phase"],
            "loading_gas_snapshot",
        )

    def test_fresh_google_sheets_snapshot_restores_without_qnap_scan(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_03",
            "share-password",
        )
        snapshot_state = {
            "folder_fid": "Folder_03",
            "folder_name": "KTV",
        }
        first_page = {
            "found": True,
            "snapshot_id": "snapshot-1",
            "total": 2,
            "updated_at": time.time(),
            "state": snapshot_state,
            "items": [{
                "item_key": "cached-song",
                "item": {
                    "filename": "Cached-Singer-國語-0001-Solo.mp4",
                    "filesize": 123,
                    "isfolder": 0,
                },
            }],
        }
        second_page = {
            **first_page,
            "items": [{
                "item_key": "cached-song-2",
                "item": {
                    "filename": "Cached2-Singer-國語-0002-Solo.mp4",
                    "filesize": 456,
                    "isfolder": 0,
                },
            }],
        }

        with patch.object(
            qnap,
            "_gas_library_configured",
            return_value=True,
        ), patch.object(
            qnap,
            "_gas_library_request",
            side_effect=[first_page, second_page],
        ), patch.object(
            qnap,
            "GAS_LIBRARY_PAGE_SIZE",
            1,
        ), patch.object(
            qnap,
            "_share_folder_items",
            side_effect=AssertionError("A fresh snapshot should avoid NAS access"),
        ), patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            side_effect=AssertionError("A fresh snapshot should avoid NAS access"),
        ), patch.object(qnap, "_schedule_next_env_sync"):
            qnap._run_env_library_sync(cache_key)

        items, sync = qnap._read_library_index_page(cache_key, 0, 20)
        self.assertEqual(
            {item["filename"] for item in items},
            {
                first_page["items"][0]["item"]["filename"],
                second_page["items"][0]["item"]["filename"],
            },
        )
        self.assertEqual(
            {item["resource_id"] for item in items},
            {"cached-song", "cached-song-2"},
        )
        self.assertEqual(sync["state"], "ready")
        self.assertEqual(qnap._env_sync_status["state"], "ready")

    def test_persistent_snapshot_uploads_bounded_pages_and_safe_item_fields(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_13",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_13",
            "folder_name": "KTV",
        }
        qnap._write_library_index_batch(
            cache_key,
            [
                {
                    "id": "one",
                    "filename": "One.mp4",
                    "filesize": 100,
                    "isfolder": 0,
                    "download_url": "https://example.test/temporary",
                },
                {"id": "two", "filename": "Two.mp4", "filesize": 200},
                {"id": "three", "filename": "Three.mp4", "isfolder": 0},
            ],
            3,
            3,
            3,
            "ready",
            state,
        )
        calls = []

        def gas_request(payload):
            calls.append(payload)
            if payload["action"] == "nas_library_begin":
                return {"snapshot_id": payload["snapshot_id"]}
            if payload["action"] == "nas_library_append":
                return {"appended": len(payload["items"])}
            return {"snapshot_id": payload["snapshot_id"]}

        with patch.object(
            qnap,
            "_gas_library_configured",
            return_value=True,
        ), patch.object(
            qnap,
            "_gas_library_request",
            side_effect=gas_request,
        ), patch.object(qnap, "GAS_LIBRARY_BATCH_SIZE", 2):
            self.assertTrue(qnap._persist_gas_library_snapshot(cache_key, state))

        append_calls = [
            call for call in calls if call["action"] == "nas_library_append"
        ]
        self.assertEqual([len(call["items"]) for call in append_calls], [2, 1])
        first_item = append_calls[0]["items"][0]
        self.assertEqual(first_item["item_key"], "one")
        self.assertNotIn("download_url", first_item["item"])
        self.assertEqual(calls[-1]["action"], "nas_library_commit")
        self.assertNotIn("password", calls[-1]["state"])

    def test_smartshare_refresh_bypasses_snapshot_index_for_temporary_urls(self):
        """SmartShare 快照不保存短效下載網址，要求重新整理時必須重新向 NAS 查詢。"""
        cache_key = (
            qnap.SMARTSHARE_AJAX_URL,
            "Share_17",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "/",
            "_from_env": True,
            "_cached_items": [],
        }
        qnap._write_library_index_batch(
            cache_key,
            [{"filename": "Song.mp4", "filesize": 100}],
            1,
            1,
            1,
            "ready",
            state,
        )

        with patch.object(
            qnap,
            "_list_page",
            return_value={
                "datas": [{
                    "filename": "Song.mp4",
                    "filesize": 100,
                    "download_url": "https://nas.example.myqnapcloud.com/temp",
                }],
                "total": 1,
            },
        ) as list_page:
            items = qnap._all_items(state, refresh=True)

        self.assertEqual(items[0]["download_url"], "https://nas.example.myqnapcloud.com/temp")
        list_page.assert_called_once()

    def test_completed_initial_scan_publishes_google_sheets_snapshot(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_14",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_14",
            "folder_name": "KTV",
        }
        gas_calls = []

        def gas_request(payload):
            gas_calls.append(payload)
            if payload["action"] == "nas_library_load":
                return {"found": False}
            if payload["action"] == "nas_library_begin":
                return {"snapshot_id": payload["snapshot_id"]}
            if payload["action"] == "nas_library_append":
                return {"appended": len(payload["items"])}
            return {"snapshot_id": payload["snapshot_id"]}

        with patch.object(
            qnap,
            "_gas_library_configured",
            return_value=True,
        ), patch.object(
            qnap,
            "_gas_library_request",
            side_effect=gas_request,
        ), patch.object(
            qnap,
            "_share_folder_items",
            return_value=(dict(state), []),
        ), patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            return_value={
                "datas": [{"id": "song-14", "filename": "Song14.mp4"}],
                "total": 1,
            },
        ), patch.object(qnap, "_schedule_next_env_sync"), patch.object(
            qnap.time,
            "sleep",
        ):
            qnap._run_env_library_sync(cache_key)

        self.assertEqual(
            [call["action"] for call in gas_calls],
            [
                "nas_library_load",
                "nas_library_begin",
                "nas_library_append",
                "nas_library_commit",
            ],
        )
        self.assertFalse(gas_calls[-1]["state"]["snapshot_partial"])
        self.assertEqual(qnap._env_sync_status["state"], "ready")

    def test_partial_smartshare_scan_publishes_indexed_items_to_google_sheets(self):
        """部分掃描仍保存已成功索引的項目，並標記快照待補掃。"""
        cache_key = (
            qnap.SMARTSHARE_AJAX_URL,
            "Share_15",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "/",
            "link_path": "/",
            "folder_name": "KTV",
        }
        gas_calls = []

        def gas_request(payload):
            gas_calls.append(payload)
            if payload["action"] == "nas_library_load":
                return {"found": False}
            if payload["action"] == "nas_library_begin":
                return {"snapshot_id": payload["snapshot_id"]}
            if payload["action"] == "nas_library_append":
                return {"appended": len(payload["items"])}
            return {"snapshot_id": payload["snapshot_id"]}

        def list_page(_state, offset, _limit, _timeout):
            if offset == 0:
                raise qnap.QnapShareError("simulated failed NAS item")
            return {
                "datas": [{
                    "id": "available-song",
                    "filename": "Available-Singer-國語-0015-Solo.mp4",
                }],
                "total": 2,
            }

        with patch.object(
            qnap,
            "_gas_library_configured",
            return_value=True,
        ), patch.object(
            qnap,
            "_gas_library_request",
            side_effect=gas_request,
        ), patch.object(
            qnap,
            "_share_folder_items",
            return_value=(dict(state), []),
        ), patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            side_effect=list_page,
        ), patch.object(
            qnap,
            "_recover_failed_offsets",
            side_effect=lambda _key, _state, failures, _notify=None: failures,
        ), patch.object(
            qnap,
            "SYNC_PAGE_RETRIES",
            0,
        ), patch.object(
            qnap,
            "_schedule_next_env_sync",
        ), patch.object(qnap.time, "sleep"):
            qnap._run_env_library_sync(cache_key)

        self.assertEqual(
            [call["action"] for call in gas_calls],
            [
                "nas_library_load",
                "nas_library_begin",
                "nas_library_append",
                "nas_library_commit",
            ],
        )
        self.assertTrue(gas_calls[-1]["state"]["snapshot_partial"])
        self.assertEqual(
            [item["filename"] for item in qnap._read_library_index_items(cache_key)],
            ["Available-Singer-國語-0015-Solo.mp4"],
        )
        self.assertEqual(len(qnap._env_sync_status["failed_offsets"]), 1)

    def test_fresh_partial_smartshare_snapshot_is_rescanned_then_completed(self):
        """即使部分快照仍在有效期內，也先載入快取再重掃 SmartShare。"""
        cache_key = (
            qnap.SMARTSHARE_AJAX_URL,
            "Share_16",
            "share-password",
        )
        snapshot_state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "folder_fid": "/",
            "link_path": "/",
            "folder_name": "KTV",
            "snapshot_partial": True,
        }
        first_page = {
            "found": True,
            "snapshot_id": "partial-snapshot",
            "total": 1,
            "updated_at": time.time(),
            "state": snapshot_state,
            "items": [{
                "item_key": "old-song",
                "item": {
                    "filename": "Old-Singer-國語-0016-Solo.mp4",
                    "filesize": 123,
                },
            }],
        }
        gas_calls = []

        def gas_request(payload):
            gas_calls.append(payload)
            if payload["action"] == "nas_library_load":
                return first_page
            if payload["action"] == "nas_library_begin":
                return {"snapshot_id": payload["snapshot_id"]}
            if payload["action"] == "nas_library_append":
                return {"appended": len(payload["items"])}
            return {"snapshot_id": payload["snapshot_id"]}

        state = {
            **snapshot_state,
            "password": cache_key[2],
        }
        with patch.object(
            qnap,
            "_gas_library_configured",
            return_value=True,
        ), patch.object(
            qnap,
            "_gas_library_request",
            side_effect=gas_request,
        ), patch.object(
            qnap,
            "_share_folder_items",
            side_effect=AssertionError("Restored state should be used for rescan"),
        ), patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            return_value={
                "datas": [{
                    "id": "current-song",
                    "filename": "Current-Singer-國語-0016-Solo.mp4",
                }],
                "total": 1,
            },
        ), patch.object(
            qnap,
            "_schedule_next_env_sync",
        ), patch.object(qnap.time, "sleep"):
            qnap._run_env_library_sync(cache_key)

        self.assertEqual(
            [call["action"] for call in gas_calls],
            [
                "nas_library_load",
                "nas_library_begin",
                "nas_library_append",
                "nas_library_commit",
            ],
        )
        self.assertFalse(gas_calls[-1]["state"]["snapshot_partial"])
        self.assertEqual(
            [item["filename"] for item in qnap._read_library_index_items(cache_key)],
            ["Current-Singer-國語-0016-Solo.mp4"],
        )

    def test_background_sync_appends_from_saved_cursor(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_04",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_04",
            "folder_name": "KTV",
        }
        qnap._write_library_index_batch(
            cache_key,
            [{"filename": "existing.mp4"}],
            1,
            1,
            1,
            "ready",
            state,
        )
        requested_offsets = []

        def list_page(_state, offset, _limit, _timeout):
            requested_offsets.append(offset)
            return {"datas": [{"filename": "new.mp4"}], "total": 2}

        with patch.object(
            qnap,
            "_share_folder_items",
            return_value=(dict(state), []),
        ), patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            side_effect=list_page,
        ), patch.object(qnap, "_schedule_next_env_sync"), patch.object(
            qnap.time,
            "sleep",
        ):
            qnap._run_env_library_sync(cache_key)

        self.assertEqual(requested_offsets, [1])
        items, _sync = qnap._read_library_index_page(cache_key, 0, 20)
        self.assertEqual(
            {item["filename"] for item in items},
            {"existing.mp4", "new.mp4"},
        )

    def test_successful_full_rescan_prunes_removed_index_items(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_05",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_05",
            "folder_name": "KTV",
        }
        qnap._write_library_index_batch(
            cache_key,
            [{"filename": "removed.mp4"}],
            1,
            1,
            1,
            "ready",
            state,
        )

        with patch.object(
            qnap,
            "_share_folder_items",
            return_value=(dict(state), []),
        ), patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            return_value={"datas": [{"filename": "current.mp4"}], "total": 1},
        ), patch.object(qnap, "_schedule_next_env_sync"), patch.object(
            qnap.time,
            "sleep",
        ):
            qnap._run_env_library_sync(cache_key, force_full_rescan=True)

        items, _sync = qnap._read_library_index_page(cache_key, 0, 20)
        self.assertEqual([item["filename"] for item in items], ["current.mp4"])

    def test_page_api_returns_bounded_index_metadata_and_sync_status(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_06",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_06",
            "folder_name": "KTV",
            "_from_env": True,
            "_cached_items": [],
        }
        qnap._write_library_index_batch(
            cache_key,
            [
                {"filename": "one.mp4", "filesize": 100, "download_url": "https://example.test/one"},
                {"filename": "one.lrc", "filesize": 10, "download_url": "https://example.test/one-lyrics"},
                {"filename": "two.mp4", "filesize": 200, "download_url": "https://example.test/two"},
                {"filename": "three.mp4", "filesize": 300, "download_url": "https://example.test/three"},
            ],
            4,
            4,
            4,
            "ready",
            state,
        )
        with qnap._env_share_lock:
            qnap._env_sync_status.update({
                "state": "ready",
                "phase": "complete",
                "loaded": 4,
                "total": 4,
                "partial_error": "One resource remains unavailable.",
            })

        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )

        with patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            side_effect=AssertionError("Indexed pages must not request QNAP"),
        ), patch.object(
            qnap,
            "_download_file",
            side_effect=AssertionError("Index reads must not download media"),
        ):
            client = app.test_client()
            response = client.get(
                "/api/nas/library-songs/page?offset=0&limit=2"
            )
            next_page_response = client.get(
                "/api/nas/library-songs/page?offset=2&limit=2"
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([song["name"] for song in payload["songs"]], ["one.mp4", "two.mp4"])
        self.assertTrue(payload["songs"][0]["has_lyrics"])
        self.assertTrue(all("download_url" not in song for song in payload["songs"]))
        self.assertEqual(payload["next_offset"], 2)
        self.assertEqual(payload["total_count"], 3)
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["sync_state"], "ready")
        self.assertEqual(payload["sync_loaded"], 4)
        self.assertEqual(payload["partial_error"], "One resource remains unavailable.")
        next_page_payload = next_page_response.get_json()
        self.assertEqual(next_page_response.status_code, 200)
        self.assertEqual([song["name"] for song in next_page_payload["songs"]], ["three.mp4"])
        self.assertEqual(next_page_payload["next_offset"], 3)
        self.assertEqual(next_page_payload["total_count"], 3)
        self.assertFalse(next_page_payload["has_more"])

    def test_combined_page_reports_sqlite_errors_as_service_unavailable(self):
        state = {
            "base_url": "https://nas.example.myqnapcloud.com/share.cgi",
            "ssid": "Share_12",
            "password": "share-password",
            "_from_env": True,
            "_cached_items": [],
        }
        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )

        with patch.object(
            qnap,
            "_ensure_room_nas_state",
            return_value="ready",
        ), patch.object(
            qnap,
            "_read_library_index_state",
            side_effect=sqlite3.OperationalError("database is locked"),
        ), self.assertLogs(qnap.logger, level="ERROR"):
            response = app.test_client().get(
                "/api/nas/library-songs/combined-page?offset=0&limit=20"
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error_type"], "OperationalError")

    def test_search_matches_full_index_and_filters_song_metadata(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_07",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_07",
            "folder_name": "KTV",
            "_from_env": True,
            "_cached_items": [],
        }
        qnap._write_library_index_batch(
            cache_key,
            [
                {"filename": "Shining-Alice-國語-0101-Solo.mp4"},
                {"filename": "Moon-Alice-台語-0102-Duet.mp4"},
                {"filename": "Night-Bob-國語-0103-Solo.mp4"},
                {"filename": "Night-Bob-國語-0103-Solo.lrc"},
            ],
            4,
            4,
            4,
            "ready",
            state,
        )
        with qnap._env_share_lock:
            qnap._env_sync_status.update({
                "state": "ready",
                "phase": "complete",
                "loaded": 4,
                "total": 4,
                "partial_error": "One resource remains unavailable.",
            })

        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )

        with patch.object(
            qnap,
            "start_qnap_background_sync",
            return_value=True,
        ), patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            side_effect=AssertionError("Search must query the local full index"),
        ), patch.object(
            qnap,
            "_download_file",
            side_effect=AssertionError("Search must not download media"),
        ):
            response = app.test_client().get(
                "/api/nas/library-songs/search"
                "?q=Night&singer=Bob&language=%E5%9C%8B%E8%AA%9E"
                "&category=Solo&sort=title&page=1&limit=1"
            )
            multi_category_response = app.test_client().get(
                "/api/nas/library-songs/search?category=Solo&category=Duet&limit=20"
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["limit"], 1)
        self.assertFalse(payload["has_more"])
        self.assertEqual(payload["sync_state"], "ready")
        self.assertEqual(
            payload["partial_error"],
            "One resource remains unavailable.",
        )
        self.assertEqual(len(payload["songs"]), 1)
        self.assertEqual(payload["songs"][0]["name"], "Night-Bob-國語-0103-Solo.mp4")
        self.assertTrue(payload["songs"][0]["has_lyrics"])
        multi_category_payload = multi_category_response.get_json()
        self.assertEqual(multi_category_response.status_code, 200)
        self.assertEqual(multi_category_payload["total_count"], 3)

    def test_large_library_search_and_page_are_bounded_at_supported_scales(self):
        state_ref = {"state": None}
        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state_ref["state"],
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
            get_local_songs=lambda: ["LocalOnly-Singer-台-0030-Local.mp4"],
            get_play_counts=lambda: {},
        )
        client = app.test_client()
        measurements = []

        with patch.object(qnap, "start_qnap_background_sync", return_value=True), patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            side_effect=AssertionError("Indexed queries must not contact QNAP"),
        ), patch.object(
            qnap,
            "_download_file",
            side_effect=AssertionError("Index and search queries must not download media"),
        ):
            for size in (800, 8_000, 80_000):
                cache_key = (
                    "https://nas.example.myqnapcloud.com/share.cgi",
                    f"Scale_{size}",
                    "share-password",
                )
                state = {
                    "base_url": cache_key[0],
                    "ssid": cache_key[1],
                    "password": cache_key[2],
                    "folder_fid": f"Folder_{size}",
                    "folder_name": "KTV",
                    "_from_env": True,
                    "_cached_items": [],
                }
                items = [
                    {
                        "filename": (
                            f"Tune{index:06d}-Singer{index % 50}-國語-"
                            f"{index:06d}-Solo.mp4"
                        ),
                        "filesize": 100,
                    }
                    for index in range(size - 1)
                ]
                items.append({
                    "filename": f"Needle-{size}-Singer-國語-{size:06d}-Solo.mp4",
                    "filesize": 100,
                })
                qnap._write_library_index_batch(
                    cache_key,
                    items,
                    size,
                    size,
                    size,
                    "ready",
                    state,
                )
                state_ref["state"] = state
                with qnap._env_share_lock:
                    qnap._env_share_cache.update({
                        "key": cache_key,
                        "state": state,
                        "fetching_key": None,
                    })
                    qnap._env_sync_status.update({
                        "state": "ready",
                        "phase": "complete",
                        "loaded": size,
                        "total": size,
                        "partial_error": "",
                    })

                started = time.perf_counter()
                search = client.get(
                    f"/api/nas/library-songs/search?q=Needle-{size}&limit=20"
                )
                search_elapsed = time.perf_counter() - started
                search_payload = search.get_json()
                self.assertEqual(search.status_code, 200)
                self.assertEqual(search_payload["total_count"], 1)
                self.assertEqual(len(search_payload["songs"]), 1)
                self.assertEqual(search_payload["songs"][0]["name"], items[-1]["filename"])

                started = time.perf_counter()
                page = client.get(
                    f"/api/nas/library-songs/page?offset={size - 20}&limit=20"
                )
                page_elapsed = time.perf_counter() - started
                page_payload = page.get_json()
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page_payload["total_count"], size)
                self.assertEqual(len(page_payload["songs"]), 20)
                self.assertEqual(page_payload["next_offset"], size)
                self.assertFalse(page_payload["has_more"])
                self.assertLess(len(page.data), 20_000)

                started = time.perf_counter()
                combined_page = client.get(
                    "/api/nas/library-songs/combined-page"
                    f"?offset={size - 19}&limit=20&sort=newest"
                )
                combined_elapsed = time.perf_counter() - started
                combined_payload = combined_page.get_json()
                self.assertEqual(combined_page.status_code, 200)
                self.assertEqual(combined_payload["total_count"], size + 1)
                self.assertEqual(len(combined_payload["songs"]), 20)
                self.assertEqual(combined_payload["next_offset"], size + 1)
                self.assertFalse(combined_payload["has_more"])
                self.assertLess(len(combined_page.data), 20_000)
                measurements.append(
                    f"{size}: search={search_elapsed:.3f}s, "
                    f"last_page={page_elapsed:.3f}s, "
                    f"combined_last_page={combined_elapsed:.3f}s, "
                    f"response={len(combined_page.data)} bytes"
                )

        print("NAS synthetic index measurements: " + "; ".join(measurements))

    def test_sync_uses_serial_bounded_pages_without_downloading_media(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_10",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_10",
            "folder_name": "KTV",
        }
        active_requests = 0
        maximum_active_requests = 0
        request_lock = threading.Lock()
        calls = []

        def list_page(_state, offset, limit, _timeout):
            nonlocal active_requests, maximum_active_requests
            with request_lock:
                active_requests += 1
                maximum_active_requests = max(
                    maximum_active_requests,
                    active_requests,
                )
            calls.append((offset, limit))
            time.sleep(0.002)
            page = [
                {"filename": f"Song{index:03d}-Singer-國語-{index:03d}-Solo.mp4"}
                for index in range(offset, min(offset + limit, 45))
            ]
            with request_lock:
                active_requests -= 1
            return {"datas": page, "total": 45}

        with patch.object(
            qnap,
            "_share_folder_items",
            return_value=(dict(state), []),
        ), patch.object(qnap, "_list_page", side_effect=list_page), patch.object(
            qnap,
            "_schedule_next_env_sync",
        ), patch.object(qnap.time, "sleep"), patch.object(
            qnap,
            "_download_file",
            side_effect=AssertionError("Background indexing must not download media"),
        ) as download:
            qnap._run_env_library_sync(cache_key)

        self.assertEqual(calls, [(0, qnap.SYNC_PAGE_SIZE), (20, qnap.SYNC_PAGE_SIZE), (40, qnap.SYNC_PAGE_SIZE)])
        self.assertEqual(maximum_active_requests, 1)
        self.assertEqual(qnap._env_sync_status["requests_started"], 3)
        self.assertEqual(qnap._env_sync_status["requests_completed"], 3)
        self.assertEqual(qnap._env_sync_status["active_requests"], 0)
        download.assert_not_called()

    def test_download_progress_is_reported_only_after_explicit_download_request(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_11",
            "share-password",
        )
        filename = "Explicit-Singer-國語-0111-Solo.mp4"
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_11",
            "folder_name": "KTV",
            "_from_env": True,
            "_cached_items": [],
        }
        qnap._write_library_index_batch(
            cache_key,
            [{
                "filename": filename,
                "filesize": 10,
                "library_offset": 825,
            }],
            1,
            1,
            1,
            "ready",
            state,
        )
        progress_reported = threading.Event()
        allow_download_to_finish = threading.Event()

        def write_download(_state, _filename, target_path, _size, **kwargs):
            kwargs["progress_callback"](5, 10)
            progress_reported.set()
            self.assertTrue(allow_download_to_finish.wait(timeout=5))
            with open(target_path, "wb") as downloaded:
                downloaded.write(b"1234567890")

        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )
        with patch.object(qnap, "_prepare_cache_space"), patch.object(
            qnap,
            "_download_file",
            side_effect=write_download,
        ) as download:
            client = app.test_client()
            response = client.post(
                "/api/nas/library-songs/download",
                json={"file_id": qnap._qnap_file_id(state, filename)},
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["job_id"]
            try:
                self.assertTrue(progress_reported.wait(timeout=5))
                duplicate_response = client.post(
                    "/api/nas/library-songs/download",
                    json={"file_id": qnap._qnap_file_id(state, filename)},
                )
                self.assertEqual(duplicate_response.status_code, 202)
                self.assertEqual(duplicate_response.get_json()["job_id"], job_id)
                status_response = client.get(
                    f"/api/nas/library-songs/download-status?job_id={job_id}"
                )
                status = status_response.get_json()
                self.assertEqual(status_response.status_code, 200)
                self.assertEqual(status["phase"], "downloading")
                self.assertEqual(status["stage"], "downloading_song")
                self.assertEqual(status["progress"], 50)
            finally:
                allow_download_to_finish.set()

            for _attempt in range(50):
                status = client.get(
                    f"/api/nas/library-songs/download-status?job_id={job_id}"
                ).get_json()
                if status.get("status") in {"completed", "error"}:
                    break
                time.sleep(0.01)

        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["progress"], 100)
        download.assert_called_once()

    def test_smartshare_download_refreshes_snapshot_item_before_download(self):
        """從快照下載 SmartShare 歌曲前，先向 NAS 取得短效下載網址。"""
        cache_key = (
            qnap.SMARTSHARE_AJAX_URL,
            "Share_18",
            "share-password",
        )
        filename = "Snapshot-Singer-國語-0118-Song.mp4"
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "/",
            "folder_name": "KTV",
            "_from_env": True,
            "_cached_items": [],
        }
        snapshot_record = qnap._gas_library_item_record(
            {
                "filename": filename,
                "filesize": 10,
                "library_offset": 825,
            },
            0,
        )
        self.assertEqual(snapshot_record["item"]["library_offset"], 825)
        qnap._write_library_index_batch(
            cache_key,
            [{
                "filename": filename,
                "filesize": 10,
                "library_offset": 825,
            }],
            1,
            1,
            1,
            "ready",
            state,
        )
        refreshed_url = "https://nas.example.myqnapcloud.com/temporary-song"
        refresh_started = threading.Event()
        allow_refresh_to_finish = threading.Event()
        refreshed = threading.Event()

        def read_download_page(*_args, **_kwargs):
            refresh_started.set()
            if not allow_refresh_to_finish.wait(timeout=5):
                raise TimeoutError("Timed out waiting to release the SmartShare refresh.")
            return {
                "datas": [{
                    "filename": filename,
                    "filesize": 10,
                    "download_url": refreshed_url,
                }],
                "total": 1,
            }

        def write_download(_state, _filename, target_path, _size, **kwargs):
            self.assertEqual(kwargs["download_url"], refreshed_url)
            with open(target_path, "wb") as downloaded:
                downloaded.write(b"1234567890")
            refreshed.set()

        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )
        with patch.object(
            qnap,
            "_list_page",
            side_effect=read_download_page,
        ) as list_page, patch.object(qnap, "_prepare_cache_space"), patch.object(
            qnap,
            "_download_file",
            side_effect=write_download,
        ) as download:
            client = app.test_client()
            response = client.post(
                "/api/nas/library-songs/download",
                json={"file_id": qnap._qnap_file_id(state, filename)},
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["job_id"]
            try:
                self.assertTrue(refresh_started.wait(timeout=5))
                status = client.get(
                    f"/api/nas/library-songs/download-status?job_id={job_id}"
                ).get_json()
                self.assertEqual(status["phase"], "locating")
                self.assertEqual(status["stage"], "refreshing_smartshare_download_urls")
            finally:
                allow_refresh_to_finish.set()
            self.assertTrue(refreshed.wait(timeout=5))
            for _attempt in range(50):
                status = client.get(
                    f"/api/nas/library-songs/download-status?job_id={job_id}"
                ).get_json()
                if status.get("status") in {"completed", "error"}:
                    break
                time.sleep(0.01)

        self.assertEqual(status["status"], "completed")
        list_page.assert_called_once_with(
            state,
            800,
            qnap.SMARTSHARE_PAGE_SIZE,
            qnap.PAGE_REQUEST_TIMEOUT_SECONDS,
        )
        download.assert_called_once()

    def test_download_fails_clearly_if_nas_initialization_is_still_loading(self):
        state = {}
        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )
        with patch.object(qnap, "_ensure_room_nas_state", return_value="loading") as ensure_state:
            response = app.test_client().post(
                "/api/nas/library-songs/download",
                json={"file_id": "qnap_" + "a" * 64},
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["job_id"]

            for _attempt in range(50):
                status = app.test_client().get(
                    f"/api/nas/library-songs/download-status?job_id={job_id}"
                ).get_json()
                if status.get("status") in {"completed", "error"}:
                    break
                time.sleep(0.01)

        self.assertEqual(status["status"], "error")
        self.assertIn("NAS 歌庫仍在初始化", status["error"])
        ensure_state.assert_called_once_with(state, wait_timeout=1.5)

    def test_paired_lyrics_download_failure_is_identified(self):
        """確認配對歌詞失敗原因會回報，且不保留未完成的歌曲檔案。"""
        song_filename = "Singer-國語-0130-Song.mp4"
        lyric_filename = "Singer-國語-0130-Song.lrc"
        state = {
            "base_url": "https://nas.example.myqnapcloud.com/share.cgi",
            "ssid": "Share_13",
            "password": "share-password",
            "folder_fid": "Folder_13",
            "folder_name": "KTV",
            "_from_env": True,
            "_cached_items": [
                {"filename": song_filename, "filesize": 10},
                {"filename": lyric_filename, "filesize": 4},
            ],
            "_cached_at": time.time(),
        }
        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )

        def download_file(_state, filename, target_path, _size, **kwargs):
            if filename == lyric_filename:
                raise qnap.QnapShareError("QNAP 歌詞連線逾時")
            with open(target_path, "wb") as downloaded:
                downloaded.write(b"1234567890")
            kwargs["progress_callback"](10, 10)
            return 10

        with patch.object(qnap, "_prepare_cache_space"), patch.object(
            qnap,
            "_download_file",
            side_effect=download_file,
        ):
            client = app.test_client()
            response = client.post(
                "/api/nas/library-songs/download",
                json={"file_id": qnap._qnap_file_id(state, song_filename)},
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["job_id"]

            for _attempt in range(50):
                status = client.get(
                    f"/api/nas/library-songs/download-status?job_id={job_id}"
                ).get_json()
                if status.get("status") in {"completed", "error"}:
                    break
                time.sleep(0.01)

        self.assertEqual(status["status"], "error")
        self.assertIn("配對歌詞下載失敗", status["error"])
        self.assertIn("QNAP 歌詞連線逾時", status["error"])
        self.assertFalse(os.path.exists(
            os.path.join(self.temp_dir.name, qnap._qnap_file_id(state, song_filename) + ".mp4")
        ))

    def test_different_song_downloads_run_concurrently_and_preserve_metadata(self):
        state = {
            "base_url": "https://nas.example.myqnapcloud.com/share.cgi",
            "ssid": "Share_12",
            "password": "share-password",
            "folder_fid": "Folder_12",
            "folder_name": "KTV",
            "_from_env": True,
            "_cached_items": [
                {"filename": "Singer-國語-0121-SongA.mp4", "filesize": 10},
                {"filename": "Singer-國語-0122-SongB.mp4", "filesize": 10},
            ],
            "_cached_at": time.time(),
        }
        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
        )
        downloads_started = threading.Event()
        allow_downloads_to_finish = threading.Event()
        started_count = 0
        started_lock = threading.Lock()

        def write_download(_state, _filename, target_path, _size, **kwargs):
            nonlocal started_count
            with started_lock:
                started_count += 1
                if started_count == 2:
                    downloads_started.set()
            kwargs["progress_callback"](5, 10)
            if not allow_downloads_to_finish.wait(timeout=5):
                raise AssertionError("Concurrent downloads were blocked.")
            with open(target_path, "wb") as downloaded:
                downloaded.write(b"1234567890")

        with patch.object(qnap, "_prepare_cache_space"), patch.object(
            qnap,
            "_download_file",
            side_effect=write_download,
        ):
            client = app.test_client()
            file_ids = [
                qnap._qnap_file_id(state, item["filename"])
                for item in state["_cached_items"]
            ]
            job_ids = []
            try:
                for file_id in file_ids:
                    response = client.post(
                        "/api/nas/library-songs/download",
                        json={"file_id": file_id},
                    )
                    self.assertEqual(response.status_code, 202)
                    job_ids.append(response.get_json()["job_id"])
                self.assertTrue(downloads_started.wait(timeout=5))
            finally:
                allow_downloads_to_finish.set()

            for job_id in job_ids:
                for _attempt in range(50):
                    status = client.get(
                        f"/api/nas/library-songs/download-status?job_id={job_id}"
                    ).get_json()
                    if status.get("status") in {"completed", "error"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(status["status"], "completed")

        metadata = qnap.drive_core._load_download_metadata(self.temp_dir.name)
        self.assertEqual(set(metadata), set(file_ids))

    def test_legacy_index_is_backfilled_for_search(self):
        os.makedirs(os.path.dirname(qnap.ENV_LIBRARY_INDEX_PATH), exist_ok=True)
        connection = sqlite3.connect(qnap.ENV_LIBRARY_INDEX_PATH)
        connection.execute(
            """
            CREATE TABLE nas_library_items (
                share_key TEXT NOT NULL,
                item_key TEXT NOT NULL,
                filename TEXT NOT NULL,
                item_json TEXT NOT NULL,
                synced_at REAL NOT NULL,
                PRIMARY KEY (share_key, item_key)
            )
            """
        )
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_08",
            "share-password",
        )
        connection.execute(
            """
            INSERT INTO nas_library_items
                (share_key, item_key, filename, item_json, synced_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                qnap._library_share_key(cache_key),
                "legacy-song",
                "Legacy-Singer-國語-9-Group.mp4",
                json.dumps({"filename": "Legacy-Singer-國語-9-Group.mp4"}),
                1.0,
            ),
        )
        connection.commit()
        connection.close()

        items, total, _sync = qnap._read_library_index_search(
            cache_key,
            keyword="Legacy",
        )

        self.assertEqual(total, 1)
        self.assertEqual(items[0]["filename"], "Legacy-Singer-國語-9-Group.mp4")

    def test_combined_library_pages_keep_sources_and_sort_before_paging(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_12",
            "share-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_12",
            "_from_env": True,
        }
        nas_songs = [
            {"filename": "Shared-Singer-國-0020-NAS.mp4", "filesize": 100},
            {"filename": "NasOnly-Singer-台-0010-NAS.mp4", "filesize": 100},
            {"filename": "NasOnly-Singer-台-0010-NAS.vtt", "filesize": 20},
        ]
        qnap._write_library_index_batch(
            cache_key,
            nas_songs,
            3,
            3,
            3,
            "ready",
            state,
        )
        shared_filename = "Shared-Singer-國-0020-NAS.mp4"
        shared_file_id = qnap._qnap_file_id(state, shared_filename)
        shared_saved_as = shared_file_id + ".mp4"
        with open(os.path.join(self.temp_dir.name, shared_saved_as), "wb") as media:
            media.write(b"cached NAS media")
        qnap.drive_core._save_download_metadata(self.temp_dir.name, {
            shared_file_id: {
                "file_id": shared_file_id,
                "saved_as": shared_saved_as,
                "public_filename": shared_filename,
                "name": shared_filename,
            },
        })

        page, total, facets = qnap._read_library_index_combined_page(
            cache_key,
            state,
            self.temp_dir.name,
            [
                "Shared-Singer-國-0020-NAS.mp4",
                "LocalOnly-Singer-台-0030-Local.mp4",
            ],
            {
                "NASONLY-SINGER-台-0010-NAS.MP4": 80,
                "LocalOnly-Singer-台-0030-Local.mp4": 20,
            },
            sort="newest",
            offset=0,
            limit=2,
        )

        self.assertEqual(total, 3)
        self.assertEqual(
            [(song.get("filename") or song["name"], song["source"]) for song in page],
            [
                ("LocalOnly-Singer-台-0030-Local.mp4", "local"),
                ("Shared-Singer-國-0020-NAS.mp4", "local"),
            ],
        )
        self.assertTrue(page[1]["downloaded_from_nas"])
        next_page, _next_total, _next_facets = qnap._read_library_index_combined_page(
            cache_key,
            state,
            self.temp_dir.name,
            [
                "Shared-Singer-國-0020-NAS.mp4",
                "LocalOnly-Singer-台-0030-Local.mp4",
            ],
            {},
            sort="newest",
            offset=2,
            limit=2,
        )
        self.assertEqual(
            [(song.get("filename") or song["name"], song["source"]) for song in next_page],
            [
                ("NasOnly-Singer-台-0010-NAS.mp4", "nas"),
            ],
        )
        self.assertTrue(next_page[0]["has_lyrics"])
        popular_page, _popular_total, _popular_facets = qnap._read_library_index_combined_page(
            cache_key,
            state,
            self.temp_dir.name,
            [
                "Shared-Singer-國-0020-NAS.mp4",
                "LocalOnly-Singer-台-0030-Local.mp4",
            ],
            {
                "Shared-Singer-國-0020-NAS.mp4": 2,
                "NasOnly-Singer-台-0010-NAS.mp4": 1,
                "LocalOnly-Singer-台-0030-Local.mp4": 20,
            },
            sort="popular",
            offset=0,
            limit=1,
        )
        self.assertEqual(
            popular_page[0].get("filename") or popular_page[0]["name"],
            "LocalOnly-Singer-台-0030-Local.mp4",
        )
        self.assertIn("Local", facets["category"])

        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=lambda: state,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, _token: False,
            get_local_songs=lambda: [
                "Shared-Singer-國-0020-NAS.mp4",
                "LocalOnly-Singer-台-0030-Local.mp4",
            ],
            get_play_counts=lambda: {
                "NASONLY-SINGER-台-0010-NAS.MP4": 80,
                "LocalOnly-Singer-台-0030-Local.mp4": 20,
            },
        )
        response = app.test_client().get(
            "/api/nas/library-songs/combined-page?limit=2&sort=popular"
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["total_count"], 3)
        self.assertEqual(
            [(song["name"], song["source"]) for song in payload["songs"]],
            [
                ("NasOnly-Singer-台-0010-NAS.mp4", "nas"),
                ("LocalOnly-Singer-台-0030-Local.mp4", "local"),
            ],
        )
        lyric_response = app.test_client().get(
            "/api/nas/library-songs/combined-page?q=NasOnly-Singer&limit=20"
        )
        self.assertEqual(lyric_response.status_code, 200)
        lyric_payload = lyric_response.get_json()
        self.assertEqual(len(lyric_payload["songs"]), 1)
        self.assertTrue(lyric_payload["songs"][0]["has_lyrics"])

    def test_failed_sync_offset_records_details_and_uses_bounded_backoff(self):
        cache_key = (
            "https://nas.example.myqnapcloud.com/share.cgi",
            "Share_09",
            "private-password",
        )
        state = {
            "base_url": cache_key[0],
            "ssid": cache_key[1],
            "password": cache_key[2],
            "folder_fid": "Folder_09",
            "folder_name": "KTV",
        }
        request_offsets = []

        def list_page(_state, offset, _limit, _timeout):
            request_offsets.append(offset)
            if offset == 0:
                raise qnap.QnapShareError("request failed private-password")
            return {"datas": [], "total": 1}

        with patch.object(
            qnap,
            "_share_folder_items",
            return_value=(dict(state), []),
        ), patch.object(
            qnap,
            "_list_page_with_hard_timeout",
            side_effect=list_page,
        ), patch.object(qnap, "_schedule_next_env_sync"), patch.object(
            qnap.time,
            "sleep",
        ) as sleep, patch.object(qnap, "SYNC_RECOVERY_ROUNDS", 0):
            qnap._run_env_library_sync(cache_key)

        failures = qnap._env_sync_status["failed_offsets"]
        self.assertEqual(request_offsets, [0, 0, 0, 1])
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list[:2]],
            [1, 2],
        )
        self.assertEqual(len(failures), 1)
        failure = failures[0]
        self.assertEqual(failure["resource"], "Folder_09")
        self.assertEqual(failure["cursor"], 0)
        self.assertEqual(failure["offset"], 0)
        self.assertEqual(failure["error_type"], "QnapShareError")
        self.assertEqual(failure["attempts"], qnap.SYNC_PAGE_RETRIES + 1)
        self.assertNotIn("private-password", failure["error"])
        self.assertIsInstance(failure["failed_at"], float)
        self.assertEqual(qnap._env_sync_status["state"], "ready")

    def test_manual_full_rescan_endpoint_requires_owner_and_starts_full_scan(self):
        app = Flask(__name__)
        app.secret_key = "test"
        qnap.register_qnap_share_routes(
            app,
            get_state=dict,
            get_download_dir=lambda: self.temp_dir.name,
            is_room_owner=lambda _room_code, token: token == "owner-token",
        )
        client = app.test_client()

        with patch.object(
            qnap,
            "_env_share_credentials",
            return_value=(
                "https://nas.example.myqnapcloud.com/share.cgi?ssid=Share_03",
                "share-password",
            ),
        ), patch.object(qnap, "start_qnap_background_sync", return_value=True) as start:
            denied = client.post("/api/nas/library-sync-rescan")
            accepted = client.post(
                "/api/nas/library-sync-rescan",
                headers={"X-KTV-Room-Owner": "owner-token"},
            )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(accepted.status_code, 202)
        start.assert_called_once_with(force_full_rescan=True)


if __name__ == "__main__":
    unittest.main()
