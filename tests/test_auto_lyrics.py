import json
import os
import tempfile
import unittest
from unittest.mock import patch

import main


class AutomaticLyricsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_songs_dir = main.SONGS_DIR
        self.original_subtitle_mode = main.subtitle_mode
        main.SONGS_DIR = self.temporary_directory.name
        main.subtitle_mode = 0
        main._AUTO_LYRICS_ATTEMPTS.clear()
        self.song_filename = '倒帶-蔡依林-國-0728-ian.mp4'
        with open(os.path.join(main.SONGS_DIR, self.song_filename), 'wb') as song_file:
            song_file.write(b'media')
        self.client = main.app.test_client()

    def tearDown(self):
        main.SONGS_DIR = self.original_songs_dir
        main.subtitle_mode = self.original_subtitle_mode
        main._AUTO_LYRICS_ATTEMPTS.clear()
        self.temporary_directory.cleanup()

    @patch.object(main, 'broadcast_current_song')
    @patch.object(main.socketio, 'emit')
    @patch.object(main, 'get_media_duration', return_value=180.0)
    @patch.object(main, 'lyrics_provider_request')
    def test_saves_high_confidence_synced_lyrics(
        self, lyrics_request, _duration, emit, _broadcast,
    ):
        self.assertEqual(main.split_song_filename(self.song_filename), ('倒帶', '蔡依林'))
        lyrics_request.return_value = [{
            'id': 42,
            'trackName': '倒帶',
            'artistName': '蔡依林',
            'duration': 181,
            'syncedLyrics': '[00:01.00]愛你怎麼能了',
        }]

        response = self.client.post(
            '/api/lyrics/auto',
            json={'song': self.song_filename},
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.data)
        self.assertTrue(payload['found'])
        self.assertEqual(main.subtitle_mode, 1)
        subtitle_path = os.path.join(
            main.SONGS_DIR,
            os.path.splitext(self.song_filename)[0] + '.vtt',
        )
        with open(subtitle_path, encoding='utf-8') as subtitle_file:
            self.assertIn('WEBVTT', subtitle_file.read())
        subtitle_response = self.client.get(
            f'/subtitles/{os.path.splitext(self.song_filename)[0]}.vtt',
        )
        self.assertEqual(subtitle_response.status_code, 200)
        subtitle_text = subtitle_response.get_data(as_text=True)
        subtitle_response.close()
        self.assertIn('WEBVTT', subtitle_text)
        self.assertEqual(emit.call_args_list[0].args[0], 'subtitle_state')

    @patch.object(main, 'broadcast_current_song')
    @patch.object(main.socketio, 'emit')
    @patch.object(main, 'get_media_duration', return_value=180.0)
    @patch.object(main, 'lyrics_provider_request')
    @patch.object(main, 'netease_search_lyrics')
    def test_uses_netease_when_lrclib_has_no_match(
        self, netease_search, lyrics_request, _duration, _emit, _broadcast,
    ):
        lyrics_request.side_effect = [
            [],
            {'lrc': {'lyric': '[00:01.00]網易雲同步歌詞'}},
        ]
        netease_search.return_value = [{
            'id': 1024,
            'trackName': '倒帶',
            'artistName': '蔡依林',
            'duration': 180,
            'syncedLyrics': '',
        }]

        response = self.client.post(
            '/api/lyrics/auto',
            json={'song': self.song_filename},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.data)['found'])
        self.assertEqual(lyrics_request.call_args_list[1].args[1], '/api/song/lyric')

    @patch.object(main, 'get_media_duration', return_value=180.0)
    @patch.object(main, 'lyrics_provider_request')
    @patch.object(main, 'netease_search_lyrics', return_value=[])
    def test_does_not_save_unmatched_lyrics(self, _netease, lyrics_request, _duration):
        lyrics_request.return_value = [{
            'id': 43,
            'trackName': '倒帶',
            'artistName': '不同歌手',
            'duration': 180,
            'syncedLyrics': '[00:01.00]不應顯示',
        }]

        response = self.client.post(
            '/api/lyrics/auto',
            json={'song': self.song_filename},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(json.loads(response.data)['found'])
        self.assertFalse(os.path.exists(os.path.join(
            main.SONGS_DIR,
            os.path.splitext(self.song_filename)[0] + '.vtt',
        )))

    @patch.object(main, 'lyrics_provider_request')
    def test_keeps_existing_subtitles_and_skips_provider_search(self, lyrics_request):
        subtitle_path = os.path.join(
            main.SONGS_DIR,
            os.path.splitext(self.song_filename)[0] + '.vtt',
        )
        with open(subtitle_path, 'w', encoding='utf-8') as subtitle_file:
            subtitle_file.write('WEBVTT\n\n')

        response = self.client.post(
            '/api/lyrics/auto',
            json={'song': self.song_filename},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.data)['reason'], 'existing')
        lyrics_request.assert_not_called()

    @patch.object(main, 'IANAUTOKTV_CLOUD_MODE', True)
    @patch.object(main, '_song_has_subtitle', return_value=True)
    @patch.object(main, '_active_cloud_room')
    @patch.object(main.socketio, 'emit')
    def test_cloud_subtitle_state_uses_room_preferences(
        self, emit, active_room, _has_subtitle,
    ):
        room = {'state': {'subtitle_mode': 0, 'subtitle_font_size': 130}}
        active_room.return_value = room

        main._broadcast_auto_subtitle_state(self.song_filename)

        self.assertEqual(room['state']['subtitle_mode'], 1)
        self.assertEqual(emit.call_args_list[0].args[1]['font_size'], 130)


if __name__ == '__main__':
    unittest.main()
