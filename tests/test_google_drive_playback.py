"""Regression tests for exposing indexed Drive downloads through KTV playback routes."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import main
from google_drive_prototype import _save_download_metadata


class GoogleDrivePlaybackTests(unittest.TestCase):
    """Ensure remote clients retain the existing filename-based playback contract."""

    def test_cloud_local_list_keeps_shared_qnap_downloads(self):
        """Shared QNAP downloads remain in the cloud's merged local song list."""
        with tempfile.TemporaryDirectory() as songs_dir, tempfile.TemporaryDirectory() as download_dir:
            saved_as = "qnap-cache-song.mp4"
            public_filename = "雲端歌曲-歌手-國-0002.mp4"
            with open(os.path.join(download_dir, saved_as), "wb") as media:
                media.write(b"complete QNAP media")
            _save_download_metadata(download_dir, {
                "qnap_file_id": {
                    "file_id": "qnap_file_id",
                    "saved_as": saved_as,
                    "public_filename": public_filename,
                    "name": public_filename,
                },
            })

            with patch.object(main, "SONGS_DIR", songs_dir), patch.object(
                main, "QNAP_SHARED_DOWNLOAD_DIR", download_dir
            ), patch.object(main, "IANAUTOKTV_CLOUD_MODE", True):
                self.assertIn(public_filename, main._get_local_song_list())

    def test_api_list_and_song_route_use_indexed_drive_download(self):
        """A completed indexed download appears in the list and streams locally."""
        with tempfile.TemporaryDirectory() as download_dir:
            saved_as = "hashed-drive-song.mp4"
            public_filename = "雲端歌曲-歌手-國-0001.mp4"
            with open(os.path.join(download_dir, saved_as), "wb") as media:
                media.write(b"complete local Drive media")
            _save_download_metadata(download_dir, {
                "drive-file-id": {
                    "file_id": "drive-file-id",
                    "saved_as": saved_as,
                    "public_filename": public_filename,
                    "name": public_filename,
                },
            })

            original_download_dir = main.DRIVE_DOWNLOAD_DIR
            main.DRIVE_DOWNLOAD_DIR = download_dir
            try:
                client = main.app.test_client()
                songs = json.loads(client.get("/api/list").get_data(as_text=True))
                response = client.get("/songs/{0}".format(public_filename))
                response_data = response.data
                response.close()
            finally:
                main.DRIVE_DOWNLOAD_DIR = original_download_dir

        self.assertIn(public_filename, songs)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response_data, b"complete local Drive media")


if __name__ == "__main__":
    unittest.main()
