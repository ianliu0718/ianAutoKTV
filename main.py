import sys
import os
import copy
import contextvars
import secrets
import base64
import glob
import queue
import random
import re
import math

# ==========================================
# 【終極修復】修正 Bad file descriptor 崩潰問題
# ==========================================
system_log_queue = queue.Queue()

class GUIWriter:
    def __init__(self):
        # 【關鍵】開啟系統底層的空裝置 (devnull)，取得真實合法的檔案描述符
        self.null_file = open(os.devnull, 'w')

    def write(self, data):
        # 攔截所有 print 和系統報錯，丟進佇列中
        if data and data.strip():
            system_log_queue.put(data.strip())

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        # 【關鍵】回傳真實合法的空裝置描述符，徹底騙過 Flask 的 click 模組！
        return self.null_file.fileno()

if getattr(sys, 'frozen', False):
    # 打包成 EXE 後，強制把所有輸出導向我們的攔截器
    sys_writer = GUIWriter()
    sys.stdout = sys_writer
    sys.stderr = sys_writer

# ==========================================
# 正常 Import 區
# ==========================================
IANAUTOKTV_CLOUD_MODE = os.environ.get('IANAUTOKTV_CLOUD_MODE') == '1'
try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:
    # Render's Linux runtime does not need the desktop UI. Keep the GUI classes
    # importable so the existing Flask application can be loaded as a WSGI app.
    class _HeadlessTk:
        pass

    class _HeadlessTkModule:
        Tk = _HeadlessTk

    class _HeadlessMessageBox:
        @staticmethod
        def showerror(*args, **kwargs):
            raise RuntimeError('The ianAutoKTV desktop UI is unavailable in cloud mode.')

        @staticmethod
        def showwarning(*args, **kwargs):
            raise RuntimeError('The ianAutoKTV desktop UI is unavailable in cloud mode.')

    tk = _HeadlessTkModule()
    messagebox = _HeadlessMessageBox()
# ... 下面的 import 保留原樣 ...
import subprocess
import shutil
import threading
import socket
import json
import time
import webbrowser
import ipaddress
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timedelta, timezone
from flask import Flask, Response, redirect, render_template, request, send_from_directory, session
from flask_socketio import SocketIO, emit as _socket_emit, join_room, leave_room
from google_drive_prototype import (
    get_downloaded_song_path,
    get_downloaded_song_records,
    get_downloaded_subtitle_path,
    register_google_drive_prototype,
)
import google_drive_prototype as drive_core
import multiprocessing

# Windows worker 必須沿用目前虛擬環境，避免子程序誤以系統 Python 再啟動一份伺服器。
multiprocessing.set_executable(sys.executable)

# ==========================================
# 設定區
# ==========================================
APP_VERSION = "v1.1.2.3"

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable) 
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STICKERS_DIR = os.path.join(BASE_DIR, "static", "stickers")
STICKER_EXTENSIONS = {'.gif', '.webp', '.png'}
STICKER_COOLDOWN_SECONDS = 2
sticker_last_sent_at = {}
sticker_cooldown_lock = threading.Lock()
FFMPEG_DIR = os.path.join(BASE_DIR, "ffmpeg", "bin")
YT_DLP_PATH = os.path.join(BASE_DIR, "yt-dlp.exe")
# 待播備註獨立保存於專案目錄，避免重啟 server 後遺失。
SONG_NOTES_FILE = os.path.join(BASE_DIR, "song_notes.json")
SONG_PLAY_COUNTS_FILE = os.path.join(BASE_DIR, "song_play_counts.json")

def get_ytdlp_command():
    if os.path.exists(YT_DLP_PATH):
        return [YT_DLP_PATH]
    if not getattr(sys, 'frozen', False) and shutil.which("py"):
        return ["py", "-3.10", "-m", "yt_dlp"]
    return [sys.executable, "-m", "yt_dlp"]

def get_ffmpeg_location():
    if os.path.isdir(FFMPEG_DIR):
        return FFMPEG_DIR
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return os.path.dirname(ffmpeg_path)
    winget_ffmpeg = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\**\ffmpeg.exe"
    ), recursive=True)
    return os.path.dirname(winget_ffmpeg[0]) if winget_ffmpeg else None

def get_ffprobe_path(ffmpeg_path):
    """Return the FFprobe executable beside FFmpeg or from PATH."""
    sibling_path = os.path.join(os.path.dirname(ffmpeg_path), 'ffprobe.exe')
    return sibling_path if os.path.exists(sibling_path) else shutil.which('ffprobe')

def get_media_duration(song_path):
    """Return a media file's duration in seconds for plain lyric timing."""
    ffmpeg_path = os.path.join(FFMPEG_DIR, 'ffmpeg.exe') if os.path.isdir(FFMPEG_DIR) else shutil.which('ffmpeg')
    ffprobe_path = get_ffprobe_path(ffmpeg_path) if ffmpeg_path else None
    if not ffprobe_path or not os.path.exists(song_path):
        return None
    result = subprocess.run(
        [ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', song_path],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None

def get_audio_channel_count(filename):
    """Return the first audio stream channel count for a song, or zero when unavailable."""
    song_path = _resolve_song_path(filename)
    ffmpeg_path = os.path.join(FFMPEG_DIR, 'ffmpeg.exe') if os.path.isdir(FFMPEG_DIR) else shutil.which('ffmpeg')
    ffprobe_path = get_ffprobe_path(ffmpeg_path) if ffmpeg_path else None
    if not ffprobe_path or not os.path.exists(song_path):
        return 0
    result = subprocess.run(
        [ffprobe_path, '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=channels', '-of', 'default=noprint_wrappers=1:nokey=1', song_path],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    try:
        return int(result.stdout.strip())
    except (TypeError, ValueError):
        return 0

def get_audio_channel_layout(filename):
    """Return the first audio stream channel layout for a song."""
    song_path = _resolve_song_path(filename)
    ffmpeg_path = os.path.join(FFMPEG_DIR, 'ffmpeg.exe') if os.path.isdir(FFMPEG_DIR) else shutil.which('ffmpeg')
    ffprobe_path = get_ffprobe_path(ffmpeg_path) if ffmpeg_path else None
    if not ffprobe_path or not os.path.exists(song_path):
        return ''
    result = subprocess.run(
        [ffprobe_path, '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=channel_layout', '-of', 'default=noprint_wrappers=1:nokey=1', song_path],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    return result.stdout.strip()

def get_audio_channel_count_from_path(song_path, ffprobe_path):
    """Return the first audio stream channel count for an arbitrary media path."""
    if not ffprobe_path or not os.path.exists(song_path):
        return 0
    result = subprocess.run(
        [ffprobe_path, '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=channels', '-of', 'default=noprint_wrappers=1:nokey=1', song_path],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    try:
        return int(result.stdout.strip())
    except (TypeError, ValueError):
        return 0

VOCAL_STEM_LAYOUT_TAG = 'handler_name'
VOCAL_STEM_LAYOUT_VERSION = 'vocal-stem-v1'
C4_PEAK_EXCEPTION_TAG = 'ktv_peak_exception'
C4_PEAK_EXCEPTION_LIMIT_TAG = 'ktv_peak_exception_limit_dbtp'
C4_PEAK_EXCEPTION_MEASURED_TAG = 'ktv_peak_exception_measured_dbtp'
C4_LRA_EXCEPTION_TAG = 'ktv_lra_exception'
C4_LRA_EXCEPTION_LIMIT_TAG = 'ktv_lra_exception_limit_lu'
C4_LRA_EXCEPTION_MEASURED_TAG = 'ktv_lra_exception_measured_lu'
# This MP4 tag lists out-of-range measurements; it never overrides structural or measurement failures.
AUDIO_EXCEPTIONS_TAG = 'ktv_audio_exceptions'
# Keep the -14 LUFS target and its accepted measurement tolerance shared by all validated modes.
AUDIO_LOUDNESS_TARGET_LUFS = -14.0
AUDIO_LOUDNESS_TOLERANCE_LU = 1.0


def _get_audio_exception_metadata(ffmpeg_path, song_path):
    """Read and validate per-metric special-song markers from MP4 metadata."""
    ffprobe_path = get_ffprobe_path(ffmpeg_path)
    if not ffprobe_path or not os.path.exists(song_path):
        return False, [], False
    try:
        result = subprocess.run(
            [ffprobe_path, '-v', 'error', '-show_entries',
             f'format_tags={AUDIO_EXCEPTIONS_TAG}', '-of', 'json', song_path],
            check=True, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        tags = json.loads(result.stdout).get('format', {}).get('tags', {})
        raw_exceptions = tags.get(AUDIO_EXCEPTIONS_TAG)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return False, [], False
    if not raw_exceptions:
        return False, [], True
    try:
        exceptions = json.loads(raw_exceptions)
        if not isinstance(exceptions, list) or not exceptions:
            raise ValueError('Audio exception metadata must be a non-empty list')
        valid_modes = {'guide', 'instrumental'}
        valid_metrics = {'loudness', 'true_peak', 'loudness_range'}
        seen = set()
        for exception in exceptions:
            if not isinstance(exception, dict):
                raise ValueError('Audio exception entry must be an object')
            mode = exception.get('mode')
            metric = exception.get('metric')
            raw_measured = exception.get('measured')
            if isinstance(raw_measured, bool) or not isinstance(raw_measured, (int, float)):
                raise ValueError('Audio exception measurement must be numeric')
            measured = float(raw_measured)
            if (mode not in valid_modes or metric not in valid_metrics
                    or (mode, metric) in seen or not math.isfinite(measured)):
                raise ValueError('Audio exception entry is invalid')
            if metric == 'loudness_range' and mode != 'instrumental':
                raise ValueError('Only c4 can have a loudness-range exception')
            expected_entry = _audio_exception_entry(mode, metric, measured)
            legacy_loudness_standard = metric == 'loudness' and exception.get('standard') == '-14 ±2 LUFS'
            if (exception.get('unit') != expected_entry['unit']
                    or (exception.get('standard') != expected_entry['standard']
                        and not legacy_loudness_standard)):
                raise ValueError('Audio exception label is invalid')
            exception['standard'] = expected_entry['standard']
            seen.add((mode, metric))
        return True, exceptions, True
    except (TypeError, ValueError, json.JSONDecodeError):
        return True, [], False


def _audio_exception_entry(mode, metric, measured):
    """Build a UI-safe, self-describing audio range exception record."""
    standards = {
        'loudness': ('LUFS', f'-14 ±{AUDIO_LOUDNESS_TOLERANCE_LU:g} LUFS'),
        'true_peak': ('dBTP', '≤ -1 dBTP'),
        'loudness_range': ('LU', '≤ 8 LU'),
    }
    unit, standard = standards[metric]
    return {
        'mode': mode,
        'metric': metric,
        'measured': round(measured, 3),
        'unit': unit,
        'standard': standard,
    }


def _write_audio_exception_metadata(ffmpeg_path, song_path, exceptions):
    """Store all out-of-range measurements with a metadata-only MP4 remux."""
    temporary_path = song_path + '.audio_exception.mp4'
    serialized = json.dumps(exceptions, ensure_ascii=False, separators=(',', ':'))
    try:
        result = subprocess.run(
            [
                ffmpeg_path, '-y', '-i', song_path, '-map', '0', '-map_metadata', '0', '-c', 'copy',
                '-movflags', '+faststart+use_metadata_tags',
                '-metadata', f'{AUDIO_EXCEPTIONS_TAG}={serialized}', temporary_path,
            ],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode != 0 or not os.path.exists(temporary_path):
            detail = (result.stderr or result.stdout or 'FFmpeg 未提供錯誤訊息').strip()[-2000:]
            raise RuntimeError(f'寫入特殊歌曲音訊標記失敗：{detail}')
        os.replace(temporary_path, song_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def _get_c4_peak_exception_metadata(ffmpeg_path, song_path):
    """Read an automatic c4 peak exception marker, retry ceiling, and measured peak."""
    ffprobe_path = get_ffprobe_path(ffmpeg_path)
    if not ffprobe_path or not os.path.exists(song_path):
        return False, None, None
    try:
        result = subprocess.run(
            [ffprobe_path, '-v', 'error', '-show_entries',
             f'format_tags={C4_PEAK_EXCEPTION_TAG},{C4_PEAK_EXCEPTION_LIMIT_TAG},{C4_PEAK_EXCEPTION_MEASURED_TAG}',
             '-of', 'json', song_path],
            check=True, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        tags = json.loads(result.stdout).get('format', {}).get('tags', {})
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return False, None, None
    if str(tags.get(C4_PEAK_EXCEPTION_TAG, '')).lower() != 'true':
        return False, None, None
    try:
        limit = float(tags.get(C4_PEAK_EXCEPTION_LIMIT_TAG))
        measured = float(tags.get(C4_PEAK_EXCEPTION_MEASURED_TAG))
        if not math.isfinite(limit) or not math.isfinite(measured):
            raise ValueError('TP exception metadata must be finite')
        return True, limit, measured
    except (TypeError, ValueError):
        return True, None, None


def _get_c4_lra_exception_metadata(ffmpeg_path, song_path):
    """Read an approved c4 LRA exception marker, limit, and measured range."""
    ffprobe_path = get_ffprobe_path(ffmpeg_path)
    if not ffprobe_path or not os.path.exists(song_path):
        return False, None, None
    try:
        result = subprocess.run(
            [ffprobe_path, '-v', 'error', '-show_entries',
             f'format_tags={C4_LRA_EXCEPTION_TAG},{C4_LRA_EXCEPTION_LIMIT_TAG},{C4_LRA_EXCEPTION_MEASURED_TAG}',
             '-of', 'json', song_path],
            check=True, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        tags = json.loads(result.stdout).get('format', {}).get('tags', {})
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return False, None, None
    if str(tags.get(C4_LRA_EXCEPTION_TAG, '')).lower() != 'true':
        return False, None, None
    try:
        limit = float(tags.get(C4_LRA_EXCEPTION_LIMIT_TAG))
        measured = float(tags.get(C4_LRA_EXCEPTION_MEASURED_TAG))
        if not math.isfinite(limit) or not math.isfinite(measured):
            raise ValueError('LRA exception metadata must be finite')
        return True, limit, measured
    except (TypeError, ValueError):
        return True, None, None


def _write_c4_audio_exception_metadata(
        ffmpeg_path, song_path, peak_limit=None, peak_measured=None, lra_limit=None, lra_measured=None,
        peak_marked=True, lra_marked=True):
    """Store approved c4 exception measurements by remuxing without AAC re-encoding."""
    temporary_path = song_path + '.audio_exception.mp4'
    metadata = []
    if peak_limit is not None and peak_measured is not None:
        metadata.extend([
            '-metadata', f'{C4_PEAK_EXCEPTION_TAG}={str(peak_marked).lower()}',
            '-metadata', f'{C4_PEAK_EXCEPTION_LIMIT_TAG}={peak_limit:.3f}',
            '-metadata', f'{C4_PEAK_EXCEPTION_MEASURED_TAG}={peak_measured:.3f}',
        ])
    if lra_limit is not None and lra_measured is not None:
        metadata.extend([
            '-metadata', f'{C4_LRA_EXCEPTION_TAG}={str(lra_marked).lower()}',
            '-metadata', f'{C4_LRA_EXCEPTION_LIMIT_TAG}={lra_limit:.3f}',
            '-metadata', f'{C4_LRA_EXCEPTION_MEASURED_TAG}={lra_measured:.3f}',
        ])
    try:
        result = subprocess.run(
            [
                ffmpeg_path, '-y', '-i', song_path, '-map', '0', '-map_metadata', '0', '-c', 'copy',
                '-movflags', '+faststart+use_metadata_tags',
                *metadata, temporary_path,
            ],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode != 0 or not os.path.exists(temporary_path):
            detail = (result.stderr or result.stdout or 'FFmpeg 未提供錯誤訊息').strip()[-2000:]
            raise RuntimeError(f'寫入伴奏音訊例外標籤失敗：{detail}')
        os.replace(temporary_path, song_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def _has_separated_vocal_track(ffmpeg_path, song_path):
    """Return whether the first audio stream uses the vocal-stem layout."""
    metadata = _get_vocal_layout_metadata(ffmpeg_path, song_path)
    return bool(metadata and metadata.startswith(VOCAL_STEM_LAYOUT_VERSION))


def _get_vocal_layout_metadata(ffmpeg_path, song_path):
    """Return the audio handler metadata used to identify the separated-vocal layout."""
    ffprobe_path = get_ffprobe_path(ffmpeg_path)
    if not ffprobe_path or not os.path.exists(song_path):
        return ''
    try:
        result = subprocess.run(
            [ffprobe_path, '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', f'stream_tags={VOCAL_STEM_LAYOUT_TAG}',
             '-of', 'default=noprint_wrappers=1:nokey=1', song_path],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return result.stdout.strip()


def _mode_pan_filter(mode, channels):
    """Return the FFmpeg pan filter for one playback mode."""
    if mode not in {'original', 'guide', 'instrumental'}:
        mode = 'original'
    if channels >= 6:
        mode_channels = {'original': ('c0', 'c1'), 'guide': ('c2', 'c2'), 'instrumental': ('c4', 'c4')}[mode]
        return f'pan=stereo|c0={mode_channels[0]}|c1={mode_channels[1]}'
    elif mode == 'instrumental':
        return 'pan=stereo|c0=c1|c1=c1'
    elif mode == 'guide':
        return 'pan=stereo|c0=c0|c1=c1'
    return 'pan=stereo|c0=c0|c1=c0'

def _measure_audio_metrics(ffmpeg_path, song_path, mode, channels=6):
    """Measure integrated loudness and true peak for one selected playback mode."""
    null_device = 'NUL' if os.name == 'nt' else '/dev/null'
    is_vocal_stem = mode == 'guide' and _has_separated_vocal_track(ffmpeg_path, song_path)
    if is_vocal_stem:
        command = [
            ffmpeg_path, '-v', 'info', '-i', song_path, '-vn', '-sn', '-dn',
            '-filter_complex',
            '[0:a]pan=mono|c0=c2,volume=0.5[vocals];'
            '[0:a]pan=mono|c0=c4[accompaniment];'
            '[vocals][accompaniment]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,'
            'pan=stereo|c0=c0|c1=c0,ebur128=peak=true:framelog=verbose[metrics]',
            '-map', '[metrics]', '-f', 'null', null_device,
        ]
    else:
        command = [
            ffmpeg_path, '-v', 'info', '-i', song_path, '-vn', '-sn', '-dn',
            '-af', f'{_mode_pan_filter(mode, channels)},ebur128=peak=true:framelog=verbose',
            '-f', 'null', null_device,
        ]
    try:
        result = subprocess.run(
            command,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    loudness_match = re.search(r'^\s*I:\s*(-?\d+(?:\.\d+)?)\s+LUFS', result.stderr, re.MULTILINE)
    peak_matches = re.findall(r'^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s+dBFS', result.stderr, re.MULTILINE)
    loudness = float(loudness_match.group(1)) if loudness_match else None
    peak = float(peak_matches[-1]) if peak_matches else None
    return loudness, peak

def _measure_audio_loudness_range(ffmpeg_path, song_path, mode='instrumental', channels=6):
    """量測指定播放模式的 EBU R128 響度範圍（LRA）。"""
    null_device = 'NUL' if os.name == 'nt' else '/dev/null'
    command = [
        ffmpeg_path, '-v', 'info', '-i', song_path, '-vn', '-sn', '-dn',
        '-af', f'{_mode_pan_filter(mode, channels)},ebur128=peak=true:framelog=verbose',
        '-f', 'null', null_device,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lra_match = re.search(r'^\s*LRA:\s*(\d+(?:\.\d+)?)\s+LU', result.stderr, re.MULTILINE)
    return float(lra_match.group(1)) if lra_match else None

def _measure_audio_loudness(ffmpeg_path, song_path, mode, channels=6):
    """Measure one audio file's selected mode without changing the file."""
    loudness, _ = _measure_audio_metrics(ffmpeg_path, song_path, mode, channels)
    return loudness

def _measure_stereo_source_metrics(ffmpeg_path, source_path):
    """Measure a stereo source without applying legacy six-channel mode mapping."""
    null_device = 'NUL' if os.name == 'nt' else '/dev/null'
    command = [
        ffmpeg_path, '-v', 'info', '-i', source_path, '-vn', '-sn', '-dn',
        '-af', 'pan=stereo|c0=c0|c1=c1,ebur128=peak=true:framelog=verbose',
        '-f', 'null', null_device,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    loudness_match = re.search(r'^\s*I:\s*(-?\d+(?:\.\d+)?)\s+LUFS', result.stderr, re.MULTILINE)
    peak_matches = re.findall(r'^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s+dBFS', result.stderr, re.MULTILINE)
    return (
        float(loudness_match.group(1)) if loudness_match else None,
        float(peak_matches[-1]) if peak_matches else None,
    )

def get_audio_loudness(filename, mode='original'):
    """Return the selected mode's integrated loudness in LUFS, cached until the song changes."""
    song_path = _resolve_song_path(filename)
    if not song_path or not os.path.exists(song_path):
        return None
    cache_key = (song_path, os.path.getmtime(song_path), os.path.getsize(song_path), mode)
    cached_value = audio_loudness_cache.get((song_path, mode))
    if cached_value and cached_value[0] == cache_key:
        return cached_value[1]
    ffmpeg_path = os.path.join(FFMPEG_DIR, 'ffmpeg.exe') if os.path.isdir(FFMPEG_DIR) else shutil.which('ffmpeg')
    if not ffmpeg_path:
        return None
    loudness = _measure_audio_loudness(ffmpeg_path, song_path, mode, get_audio_channel_count(filename))
    loudness = round(loudness, 1) if loudness is not None else None
    audio_loudness_cache[(song_path, mode)] = (cache_key, loudness)
    return loudness

ffmpeg_location = get_ffmpeg_location()
if ffmpeg_location:
    os.environ["PATH"] += os.pathsep + ffmpeg_location
os.environ["PATH"] += os.pathsep + BASE_DIR

SONGS_DIR = os.path.join(BASE_DIR, "ktv_songs")
DRIVE_DOWNLOAD_DIR = os.path.join(BASE_DIR, "drive_downloads")
EFFECTS_DIR = os.path.join(BASE_DIR, "static", "sounds")
TEMP_BASE_DIR = os.path.join(BASE_DIR, "temp_processing") 
SUBTITLE_EXTENSIONS = {"srt", "lrc", "vtt"}
audio_loudness_cache = {}

LYRICS_PROVIDERS = {
    'lrclib': {
        'name': 'LRCLIB（同步歌詞）',
        'base_url': 'https://lrclib.net/api',
    },
    'netease': {
        'name': 'NetEase Cloud Music（同步歌詞）',
        'base_url': 'https://music.163.com',
    },
    'lyrics_ovh': {
        'name': 'lyrics.ovh（精確純文字查詢）',
        'base_url': 'https://api.lyrics.ovh',
    },
}
LYRICS_USER_AGENT = f'ianAutoKTV/{APP_VERSION} (local KTV lyrics downloader)'

if not os.path.exists(SONGS_DIR): os.makedirs(SONGS_DIR)
if not os.path.exists(EFFECTS_DIR): os.makedirs(EFFECTS_DIR)
if not os.path.exists(TEMP_BASE_DIR): os.makedirs(TEMP_BASE_DIR)

# ==========================================
# Flask + SocketIO 伺服器
# ==========================================
app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=os.path.join(BASE_DIR, 'static'))
app.config['SECRET_KEY'] = 'ktv_secret'
app.config['GOOGLE_DRIVE_DEPLOY_PATH'] = os.path.join(BASE_DIR, 'google_drive_deploy.json')
app.config['GOOGLE_DRIVE_ACCOUNT_PATH'] = os.path.join(BASE_DIR, 'google_drive_account.bin')
app.config['DRIVE_PROTOTYPE_DOWNLOAD_DIR'] = DRIVE_DOWNLOAD_DIR
_socketio_origins = [
    origin.strip()
    for origin in os.environ.get('SOCKETIO_CORS_ALLOWED_ORIGINS', '').split(',')
    if origin.strip()
]
socketio = SocketIO(
    app,
    cors_allowed_origins=_socketio_origins or None,
    async_mode="threading",
)

_CURRENT_CLOUD_ROOM = contextvars.ContextVar('ianautoktv_cloud_room', default=None)
_socketio_emit_original = socketio.emit


def emit(event, data=None, **kwargs):
    """Keep existing broadcast calls inside the caller's cloud room."""
    if IANAUTOKTV_CLOUD_MODE and kwargs.pop('broadcast', False):
        room_code = _CURRENT_CLOUD_ROOM.get()
        if not room_code:
            return None
        kwargs.setdefault('to', room_code)
    return _socket_emit(event, data, **kwargs)


def _emit_to_current_cloud_room(event, *args, **kwargs):
    if IANAUTOKTV_CLOUD_MODE and not any(key in kwargs for key in ('to', 'room', 'skip_sid')):
        room_code = _CURRENT_CLOUD_ROOM.get()
        if not room_code:
            return None
        kwargs['to'] = room_code
    return _socketio_emit_original(event, *args, **kwargs)


socketio.emit = _emit_to_current_cloud_room
if not IANAUTOKTV_CLOUD_MODE:
    # The current prototype relies on a single Windows DPAPI-protected token.
    # Do not expose it as a multi-user cloud integration.
    register_google_drive_prototype(app)

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

LOCAL_IP = get_local_ip()
PORT = int(os.environ.get('PORT', '5000'))
TLS_CERT_PATH = os.path.join(BASE_DIR, "ktv-local.crt")
TLS_KEY_PATH = os.path.join(BASE_DIR, "ktv-local.key")
SERVER_ERROR_LOG_PATH = os.path.join(BASE_DIR, "server-error.log")
FIREWALL_RULE_NAME = "OpenKTV HTTPS 5000"
PRIVATE_FIREWALL_RULE_NAME = "OpenKTV HTTPS 5000 Private"
PROGRAM_FIREWALL_RULE_NAME = "OpenKTV Server Private Application"

def _certificate_is_current():
    """確認既有憑證仍有效、私鑰配對，且 SAN 包含目前 LAN IP。"""
    if not (os.path.exists(TLS_CERT_PATH) and os.path.exists(TLS_KEY_PATH)):
        return False
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        certificate = x509.load_pem_x509_certificate(open(TLS_CERT_PATH, 'rb').read())
        private_key = serialization.load_pem_private_key(open(TLS_KEY_PATH, 'rb').read(), password=None)
        now = datetime.now(timezone.utc)
        not_before = getattr(certificate, 'not_valid_before_utc', certificate.not_valid_before)
        not_after = getattr(certificate, 'not_valid_after_utc', certificate.not_valid_after)
        if not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=timezone.utc)
        if not (not_before <= now < not_after):
            return False
        certificate_key = certificate.public_key().public_numbers()
        private_key_public = private_key.public_key().public_numbers()
        if certificate_key != private_key_public:
            return False
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return any(
            isinstance(name, x509.IPAddress) and str(name.value) == LOCAL_IP
            for name in san
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def ensure_tls_certificate():
    """Create or refresh a self-signed certificate for localhost and the LAN IP."""
    if _certificate_is_current():
        return TLS_CERT_PATH, TLS_KEY_PATH

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ianAutoKTV Local"),
        x509.NameAttribute(NameOID.COMMON_NAME, LOCAL_IP),
    ])
    san_names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        san_names.append(x509.IPAddress(ipaddress.ip_address(LOCAL_IP)))
    except ValueError:
        san_names.append(x509.DNSName(LOCAL_IP))
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_names), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    temporary_key_path = TLS_KEY_PATH + '.tmp'
    temporary_cert_path = TLS_CERT_PATH + '.tmp'
    with open(temporary_key_path, "wb") as key_file:
        key_file.write(private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    with open(temporary_cert_path, "wb") as cert_file:
        cert_file.write(certificate.public_bytes(serialization.Encoding.PEM))
    os.replace(temporary_key_path, TLS_KEY_PATH)
    os.replace(temporary_cert_path, TLS_CERT_PATH)
    return TLS_CERT_PATH, TLS_KEY_PATH

def broadcast_log(msg):
    # 用 print 就會自動被我們的 GUIWriter 抓走並顯示在介面上
    print(msg)
    socketio.emit('admin_log', {'msg': msg})


_server_instance_lock_file = None


def _try_lock_server_instance(lock_file):
    """Try to own the first byte of this project's Windows instance lock."""
    import msvcrt

    lock_file.seek(0)
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _wait_for_server_instance_lock(lock_file, timeout):
    """Wait for the previous project instance to release its process lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _try_lock_server_instance(lock_file):
            return True
        time.sleep(0.1)
    return False


def _request_server_window_close(process_id):
    """Send WM_CLOSE to top-level windows owned by the previous KTV process."""
    import ctypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    enum_callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)

    def close_window(window_handle, _):
        window_process_id = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(window_handle, ctypes.byref(window_process_id))
        if window_process_id.value == process_id:
            user32.PostMessageW(window_handle, 0x0010, 0, 0)
        return True

    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.EnumWindows.argtypes = [enum_callback_type, ctypes.c_ssize_t]
    callback = enum_callback_type(close_window)
    user32.EnumWindows(callback, 0)


def _run_taskkill(process_id, force=False):
    """Stop only the process recorded in this project's instance lock."""
    command = ['taskkill.exe', '/PID', str(process_id), '/T']
    if force:
        command.append('/F')
    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f'無法關閉舊 KTV Server（PID {process_id}）：{error}')


def _get_server_port_process():
    """Identify the process listening on the KTV port without stopping it."""
    encoded_base_dir = base64.b64encode(os.path.abspath(BASE_DIR).encode('utf-8')).decode('ascii')
    powershell_check = (
        f"$basePath = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_base_dir}')); "
        f"$connection = Get-NetTCPConnection -State Listen -LocalPort {PORT} "
        '-ErrorAction SilentlyContinue | Select-Object -First 1; '
        'if ($connection) { '
        '$process = Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)"; '
        'if ($process) { '
        '$name = [string]$process.Name; $commandLine = [string]$process.CommandLine; '
        '$executablePath = [string]$process.ExecutablePath; '
        "$pythonDirectory = [IO.Path]::GetFullPath((Join-Path $basePath '.venv\\Scripts')) + [IO.Path]::DirectorySeparatorChar; "
        "$mainScript = Join-Path $basePath 'main.py'; "
        "$isPythonMain = ($name -match '^(?i:pythonw?\\.exe)$') -and ($commandLine -match '(?i)main\\.py') "
        "-and (($executablePath -and $executablePath.StartsWith($pythonDirectory, [StringComparison]::OrdinalIgnoreCase)) "
        "-or ($commandLine.IndexOf($mainScript, [StringComparison]::OrdinalIgnoreCase) -ge 0)); "
        "$isKtvServer = ($name -ieq 'ianAutoKTV_Server.exe') -or $isPythonMain; "
        '[pscustomobject]@{ ProcessId = $process.ProcessId; Name = $name; IsKtvServer = $isKtvServer } '
        '| ConvertTo-Json -Compress } }'
    )
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', powershell_check],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=8,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    output = result.stdout.lstrip('\ufeff').strip()
    if result.returncode != 0 or not output:
        return None
    try:
        process_info = json.loads(output)
        return process_info if isinstance(process_info, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _wait_for_windows_process_exit(process_id, timeout):
    """Wait for a process handle to become signaled without polling process lists."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process_handle = kernel32.OpenProcess(0x00100000, False, process_id)
    if not process_handle:
        return ctypes.get_last_error() == 87
    try:
        wait_result = kernel32.WaitForSingleObject(process_handle, max(0, int(timeout * 1000)))
        return wait_result == 0
    finally:
        kernel32.CloseHandle(process_handle)


def _close_legacy_server_process(process_info):
    """Gracefully close a recognized older KTV process, then force it only on timeout."""
    process_id = int(process_info['ProcessId'])
    print(f'偵測到 5000 埠上的舊版 KTV Server（PID {process_id}），正在正常關閉。')
    _request_server_window_close(process_id)
    if _wait_for_windows_process_exit(process_id, 5):
        return
    _run_taskkill(process_id)
    if _wait_for_windows_process_exit(process_id, 2):
        return
    _run_taskkill(process_id, force=True)
    if not _wait_for_windows_process_exit(process_id, 5):
        raise RuntimeError(f'無法關閉舊版 KTV Server（PID {process_id}），新的 Server 未啟動。')


def ensure_single_server_instance():
    """Replace a previous server from this project directory before startup."""
    global _server_instance_lock_file
    if os.name != 'nt':
        return

    import ctypes
    import hashlib
    from ctypes import wintypes

    project_path = os.path.normcase(os.path.abspath(BASE_DIR))
    project_key = hashlib.sha256(project_path.encode('utf-8')).hexdigest()[:16]
    mutex_name = f'Local\\ianAutoKTV-startup-{project_key}'
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    startup_mutex = kernel32.CreateMutexW(None, False, mutex_name)
    if not startup_mutex:
        raise OSError(ctypes.get_last_error(), '無法建立 KTV Server 啟動鎖。')

    mutex_acquired = False
    lock_file = None
    lock_acquired = False
    try:
        wait_result = kernel32.WaitForSingleObject(startup_mutex, 0xFFFFFFFF)
        if wait_result not in (0, 0x80):
            raise OSError(ctypes.get_last_error(), '無法取得 KTV Server 啟動鎖。')
        mutex_acquired = True

        lock_path = os.path.join(BASE_DIR, '.ianAutoKTV.instance.lock')
        if not os.path.exists(lock_path):
            with open(lock_path, 'wb') as new_lock_file:
                new_lock_file.write(b'\0' + b'0\n')
        lock_file = open(lock_path, 'r+b')
        if os.fstat(lock_file.fileno()).st_size < 3:
            lock_file.seek(0)
            lock_file.write(b'\0' + b'0\n')
            lock_file.flush()

        lock_acquired = _try_lock_server_instance(lock_file)
        if not lock_acquired:
            # Byte 0 is exclusively locked; keep the readable owner PID after it.
            lock_file.seek(1)
            try:
                previous_process_id = int(lock_file.readline().decode('ascii').strip())
            except (UnicodeDecodeError, ValueError) as error:
                raise RuntimeError('同專案鎖檔無法辨識舊 Server，為避免誤關其他程序，已停止啟動。') from error
            if previous_process_id <= 0 or previous_process_id == os.getpid():
                raise RuntimeError('同專案鎖檔中的舊 Server PID 無效，已停止啟動。')

            print(f'偵測到同專案舊 KTV Server（PID {previous_process_id}），正在正常關閉。')
            _request_server_window_close(previous_process_id)
            lock_acquired = _wait_for_server_instance_lock(lock_file, 5)
            if not lock_acquired:
                _run_taskkill(previous_process_id)
                lock_acquired = _wait_for_server_instance_lock(lock_file, 2)
            if not lock_acquired:
                _run_taskkill(previous_process_id, force=True)
                lock_acquired = _wait_for_server_instance_lock(lock_file, 5)
            if not lock_acquired:
                raise RuntimeError('無法關閉同專案舊 KTV Server，新的 Server 未啟動。')

        if _is_server_port_listening():
            port_process = _get_server_port_process()
            if not port_process:
                raise RuntimeError(f'無法辨識佔用 TCP {PORT} 的程序，為避免誤關其他程式，新的 Server 未啟動。')
            if not port_process.get('IsKtvServer'):
                raise RuntimeError(
                    f'TCP {PORT} 已由非本專案程序 {port_process.get("Name", "未知程序")} '
                    f'(PID {port_process.get("ProcessId", "未知")}) 佔用；未關閉該程序。'
                )
            port_process_id = int(port_process['ProcessId'])
            if port_process_id != os.getpid():
                _close_legacy_server_process(port_process)

        lock_file.seek(1)
        lock_file.write(f'{os.getpid():<12}\n'.encode('ascii'))
        lock_file.flush()
        _server_instance_lock_file = lock_file
        lock_file = None
    finally:
        if lock_file is not None:
            lock_file.close()
        if mutex_acquired:
            kernel32.ReleaseMutex(startup_mutex)
        kernel32.CloseHandle(startup_mutex)


def _is_server_port_listening():
    """檢查本機 HTTPS 服務是否已在 5000 port 監聽。"""
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1):
            return True
    except OSError:
        return False


def _is_lan_port_listening():
    """確認服務可透過目前 LAN IP 接受 TCP 連線。"""
    try:
        with socket.create_connection((LOCAL_IP, PORT), timeout=1):
            return True
    except OSError:
        return False


def _has_firewall_rule():
    """檢查 Windows 規則是否真的啟用並允許入站 TCP 5000。"""
    if os.name != 'nt':
        return True
    try:
        netsh_result = subprocess.run(
            ['netsh.exe', 'advfirewall', 'firewall', 'show', 'rule', f'name={PRIVATE_FIREWALL_RULE_NAME}'],
            capture_output=True, text=True, encoding='mbcs', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
        )
        if netsh_result.returncode == 0 and '5000' in netsh_result.stdout:
            return True
        powershell_check = (
            "$matched = Get-NetFirewallRule -Enabled True -Direction Inbound -Action Allow "
            "-ErrorAction SilentlyContinue | ForEach-Object { "
            "$rule = $_; $port = $rule | Get-NetFirewallPortFilter; "
            "if ($port.Protocol -eq 'TCP' -and $port.LocalPort -eq '5000' "
            "-and $rule.Profile.ToString() -match 'Private|Any') { $rule } }; "
            "if ($matched) { exit 0 } else { exit 1 }"
        )
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', powershell_check],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _firewall_rule_details():
    """取得防火牆規則與 TCP 埠設定，供 GUI 診斷使用。"""
    if os.name != 'nt':
        return '非 Windows 作業系統'
    try:
        netsh_result = subprocess.run(
            ['netsh.exe', 'advfirewall', 'firewall', 'show', 'rule', f'name={PRIVATE_FIREWALL_RULE_NAME}'],
            capture_output=True, text=True, encoding='mbcs', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15,
        )
        powershell_details = (
            "$matches = Get-NetFirewallRule -Enabled True -Direction Inbound -Action Allow "
            "-ErrorAction SilentlyContinue | ForEach-Object { "
            "$rule = $_; $port = $rule | Get-NetFirewallPortFilter; "
            "if ($port.Protocol -eq 'TCP' -and $port.LocalPort -eq '5000') { "
            "[pscustomobject]@{Name=$rule.DisplayName;Profile=$rule.Profile;"
            "Enabled=$rule.Enabled;Direction=$rule.Direction;Action=$rule.Action;"
            "Protocol=$port.Protocol;LocalPort=$port.LocalPort} } }; "
            "if ($matches) { $matches | ConvertTo-Json -Compress } else { 'RULE_NOT_FOUND' }"
        )
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', powershell_details],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15,
        )
        output = result.stdout.strip() or result.stderr.strip() or '<無輸出>'
        netsh_output = netsh_result.stdout.strip() or netsh_result.stderr.strip() or '<無輸出>'
        return (
            f'netsh 回傳碼={netsh_result.returncode}; '
            f'netsh={netsh_output}; PowerShell回傳碼={result.returncode}; PowerShell={output}'
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'查詢例外：{error}'


def _log_lan_access(message):
    print(f"[區網連線][{datetime.now().strftime('%H:%M:%S')}] {message}")


def allow_lan_firewall_access():
    """以系統管理員權限建立區網 HTTPS 入站規則。"""
    if os.name != 'nt':
        _log_lan_access('目前作業系統不是 Windows，無法建立 Windows 防火牆規則。')
        return False, '此功能只支援 Windows。'
    try:
        _log_lan_access(f'開始設定入站 TCP {PORT}，規則名稱：{FIREWALL_RULE_NAME} / {PRIVATE_FIREWALL_RULE_NAME}')
        _log_lan_access(f'目前服務網址：https://{LOCAL_IP}:{PORT}/remote')
        import base64
        firewall_log_path = os.path.join(BASE_DIR, 'firewall-rule-setup.log')
        escaped_log_path = firewall_log_path.replace("'", "''")
        program_path = os.path.abspath(sys.executable)
        escaped_program_path = program_path.replace('"', '\\"')
        _log_lan_access(f'應用程式規則目標：{program_path}')
        firewall_script = (
            f"$log = '{escaped_log_path}'; "
            "'--- firewall setup ---' | Set-Content -Path $log -Encoding UTF8; "
            f"& netsh.exe advfirewall firewall delete rule name=\"{FIREWALL_RULE_NAME}\" *> $log; "
            "$deleteExitCode = $LASTEXITCODE; "
            f"& netsh.exe advfirewall firewall delete rule name=\"{PRIVATE_FIREWALL_RULE_NAME}\" *> $log; "
            "$deletePrivateExitCode = $LASTEXITCODE; "
            f"& netsh.exe advfirewall firewall delete rule name=\"{PROGRAM_FIREWALL_RULE_NAME}\" *> $log; "
            "$deleteProgramExitCode = $LASTEXITCODE; "
            f"& netsh.exe advfirewall firewall add rule name=\"{FIREWALL_RULE_NAME}\" "
            "dir=in action=allow protocol=TCP localport=5000 profile=any remoteip=any *> $log; "
            "$addExitCode = $LASTEXITCODE; "
            f"& netsh.exe advfirewall firewall add rule name=\"{PRIVATE_FIREWALL_RULE_NAME}\" "
            "dir=in action=allow protocol=TCP localport=5000 profile=private remoteip=any *> $log; "
            "$addPrivateExitCode = $LASTEXITCODE; "
            f"& netsh.exe advfirewall firewall add rule name=\"{PROGRAM_FIREWALL_RULE_NAME}\" "
            f"dir=in action=allow program=\"{escaped_program_path}\" protocol=TCP localport=5000 "
            "profile=private remoteip=any enable=yes *> $log; "
            "$addProgramExitCode = $LASTEXITCODE; "
            "('netsh delete exit code: ' + $deleteExitCode) | Add-Content -Path $log -Encoding UTF8; "
            "('netsh add exit code: ' + $addExitCode) | Add-Content -Path $log -Encoding UTF8; "
            "('netsh private add exit code: ' + $addPrivateExitCode) | Add-Content -Path $log -Encoding UTF8; "
            "('netsh program add exit code: ' + $addProgramExitCode) | Add-Content -Path $log -Encoding UTF8; "
            'if ($addExitCode -ne 0 -or $addPrivateExitCode -ne 0 -or $addProgramExitCode -ne 0) { exit 1 }; exit 0'
        )
        encoded_script = base64.b64encode(firewall_script.encode('utf-16le')).decode('ascii')
        elevated_command = (
            "$process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru "
            f"-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{encoded_script}'); "
            'exit $process.ExitCode'
        )
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', elevated_command],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=45,
        )
        _log_lan_access(f'UAC 提權程序結束，回傳碼：{result.returncode}')
        if result.stdout.strip():
            _log_lan_access(f'系統輸出：{result.stdout.strip()}')
        if result.stderr.strip():
            _log_lan_access(f'系統錯誤：{result.stderr.strip()}')
        if result.returncode != 0:
            _log_lan_access('防火牆指令未成功完成，可能是取消 UAC 或未使用系統管理員權限。')
            return False, '防火牆規則建立失敗或未允許 UAC，請確認已按下「是」。'
        _log_lan_access('開始重新查詢防火牆規則。')
        firewall_ready = _has_firewall_rule()
        _log_lan_access(f'防火牆規則驗證結果：{"成功" if firewall_ready else "失敗"}')
        if firewall_ready:
            _log_lan_access(f'已確認允許入站 TCP {PORT}。')
            return True, '防火牆規則已建立並確認允許 TCP 5000。'
        _log_lan_access(f'規則詳細資料：{_firewall_rule_details()}')
        if os.path.exists(firewall_log_path):
            with open(firewall_log_path, 'r', encoding='utf-8', errors='replace') as log_file:
                setup_log = log_file.read().strip()
            if setup_log:
                _log_lan_access(f'netsh 設定記錄：{setup_log}')
        _log_lan_access('找不到符合條件的啟用中 Inbound Allow TCP 5000 規則。')
        return False, '系統管理員程序已結束，但尚未確認防火牆規則，請檢查 Windows 防火牆。'
    except subprocess.TimeoutExpired:
        _log_lan_access('等待 UAC 或防火牆設定逾時（45 秒）。')
        return False, '等待 Windows 防火牆設定逾時，請確認 UAC 視窗仍未被取消。'
    except OSError as error:
        _log_lan_access(f'執行防火牆設定時發生 OSError：{error!r}')
        return False, f'建立防火牆規則失敗：{error}'

# ------------------------------------------
# Flask 路由
# ------------------------------------------
@app.route('/player')
def page_player():
    room_code = session.get('room_code', '') if IANAUTOKTV_CLOUD_MODE else ''
    return render_template(
        'player.html',
        room_code=room_code,
        room_join_url=f'{request.url_root.rstrip("/")}/join/{room_code}' if room_code else '',
    )

@app.route('/remote')
def page_remote():
    return render_template(
        'remote.html',
        cloud_mode=IANAUTOKTV_CLOUD_MODE,
    )

@app.route('/admin')
def page_admin(): return render_template('admin.html')

@app.route('/combo')
def page_combo():
    if IANAUTOKTV_CLOUD_MODE and session.get('room_role') != 'player':
        return redirect('/remote')
    room_code = session.get('room_code', '') if IANAUTOKTV_CLOUD_MODE else ''
    return render_template(
        'combo.html',
        room_code=room_code,
        room_join_url=f'{request.url_root.rstrip("/")}/join/{room_code}' if room_code else f'{request.url_root.rstrip("/")}/remote',
    )

@app.route('/soundtouch-prototype')
def page_soundtouch_prototype():
    """Serve the isolated SoundTouch KEY validation page."""
    return send_from_directory(BASE_DIR, 'soundtouch-prototype.html')

@app.route('/')
def page_index():
    if IANAUTOKTV_CLOUD_MODE:
        return render_template('room_entry.html', error=request.args.get('error', ''))
    return render_template('remote.html')

@app.route('/rooms')
def page_rooms():
    """Keep manual room creation and joining available from the all-in-one start page."""
    return render_template('room_entry.html', error=request.args.get('error', ''))


def _room_drive_download_dir():
    """Return this room's isolated temporary Drive cache directory in cloud mode."""
    if not IANAUTOKTV_CLOUD_MODE:
        return ''
    room_code = session.get('room_code', '')
    if not re.fullmatch(r'\d{8}', room_code):
        return ''
    return os.path.join(DRIVE_DOWNLOAD_DIR, 'rooms', room_code)

@app.route('/songs/<path:filename>')
def serve_song(filename):
    local_path = os.path.join(SONGS_DIR, os.path.basename(filename))
    if os.path.isfile(local_path):
        return send_from_directory(SONGS_DIR, os.path.basename(filename))
    room_drive_dir = _room_drive_download_dir()
    room_drive_path = get_downloaded_song_path(room_drive_dir, filename) if room_drive_dir else None
    if room_drive_path:
        return send_from_directory(room_drive_dir, os.path.basename(room_drive_path))
    drive_path = None if IANAUTOKTV_CLOUD_MODE else get_downloaded_song_path(DRIVE_DOWNLOAD_DIR, filename)
    if drive_path:
        return send_from_directory(DRIVE_DOWNLOAD_DIR, os.path.basename(drive_path))
    return send_from_directory(SONGS_DIR, filename)


def _resolve_song_path(filename):
    """Resolve local songs first, then indexed Drive downloads for playback metadata."""
    safe_filename = os.path.basename(str(filename))
    local_path = os.path.join(SONGS_DIR, safe_filename)
    if os.path.isfile(local_path):
        return local_path
    room_drive_dir = _room_drive_download_dir()
    room_drive_path = get_downloaded_song_path(room_drive_dir, safe_filename) if room_drive_dir else None
    if room_drive_path:
        return room_drive_path
    return None if IANAUTOKTV_CLOUD_MODE else get_downloaded_song_path(DRIVE_DOWNLOAD_DIR, safe_filename)

@app.route('/effects/<path:filename>')
def serve_effect(filename):
    return send_from_directory(EFFECTS_DIR, filename)

def _song_has_subtitle(filename):
    """Returns True only when the current song actually has a matching VTT file."""
    if not filename:
        return False
    subtitle_path = os.path.join(SONGS_DIR, os.path.splitext(filename)[0] + '.vtt')
    if os.path.exists(subtitle_path):
        return True
    drive_subtitle_path = None if IANAUTOKTV_CLOUD_MODE else get_downloaded_subtitle_path(
        DRIVE_DOWNLOAD_DIR, os.path.splitext(filename)[0] + '.vtt',
    )
    room_drive_dir = _room_drive_download_dir()
    if room_drive_dir:
        drive_subtitle_path = get_downloaded_subtitle_path(
            room_drive_dir,
            os.path.splitext(filename)[0] + '.vtt',
        ) or drive_subtitle_path
    drive_subtitle_extension = os.path.splitext(drive_subtitle_path)[1].lower().lstrip('.') if drive_subtitle_path else ''
    return drive_subtitle_extension in SUBTITLE_EXTENSIONS


def _song_uses_plain_lyrics(filename):
    """Return whether a song's VTT contains the marker used for untimed lyrics."""
    if not filename:
        return False
    subtitle_path = os.path.join(SONGS_DIR, os.path.splitext(filename)[0] + '.vtt')
    if not os.path.exists(subtitle_path):
        subtitle_path = None if IANAUTOKTV_CLOUD_MODE else get_downloaded_subtitle_path(
            DRIVE_DOWNLOAD_DIR, os.path.splitext(filename)[0] + '.vtt',
        )
        room_drive_dir = _room_drive_download_dir()
        if room_drive_dir:
            subtitle_path = get_downloaded_subtitle_path(
                room_drive_dir,
                os.path.splitext(filename)[0] + '.vtt',
            ) or subtitle_path
    if not subtitle_path:
        return False
    try:
        with open(subtitle_path, 'r', encoding='utf-8-sig') as subtitle_file:
            return any(line.strip().startswith('PLAIN_LYRICS_') for line in subtitle_file)
    except (OSError, UnicodeError):
        return False


def _effective_subtitle_mode(filename):
    """Return the shared subtitle mode when the song has a subtitle file."""
    if not _song_has_subtitle(filename):
        return 0
    return subtitle_mode


def _play_video_payload(filename):
    """Build a playback event payload with server-confirmed audio metadata and per-song subtitle state."""
    effective_mode = _effective_subtitle_mode(filename)
    guide_audio_state = _guide_audio_control_state(filename)
    return {
        'filename': filename,
        'title': filename,
        'plain_lyrics': _song_uses_plain_lyrics(filename),
        'audio_channels': get_audio_channel_count(filename),
        'audio_channel_layout': get_audio_channel_layout(filename),
        **guide_audio_state,
        'audio_loudness_lufs': get_audio_loudness(filename, 'original'),
        'track_mode': current_track_mode,
        'visible': effective_mode > 0,
        'subtitle_mode': effective_mode,
        'font_size': subtitle_font_size,
    }

@app.route('/subtitles/<path:filename>')
def serve_subtitle(filename):
    """Serve a stored WebVTT subtitle file for a song."""
    room_drive_dir = _room_drive_download_dir()
    subtitle_path = get_downloaded_subtitle_path(room_drive_dir, filename) if room_drive_dir else None
    if not subtitle_path and not IANAUTOKTV_CLOUD_MODE:
        subtitle_path = get_downloaded_subtitle_path(DRIVE_DOWNLOAD_DIR, filename)
    subtitle_extension = os.path.splitext(subtitle_path)[1].lower().lstrip('.') if subtitle_path else ''
    if subtitle_path and subtitle_extension in SUBTITLE_EXTENSIONS:
        try:
            with open(subtitle_path, 'r', encoding='utf-8-sig') as subtitle_file:
                content = convert_to_webvtt(subtitle_file.read(), subtitle_extension)
        except (OSError, UnicodeError, ValueError):
            return '', 404
        return Response(content, content_type='text/vtt; charset=utf-8')
    return send_from_directory(SONGS_DIR, filename, mimetype='text/vtt; charset=utf-8')

@app.route('/api/list')
def get_song_list():
    songs = [f for f in os.listdir(SONGS_DIR) if f.lower().endswith('.mp4')]
    local_names = {filename.casefold() for filename in songs}
    # Keep the existing filename-based client contract while adding indexed Drive files.
    if not IANAUTOKTV_CLOUD_MODE:
        for record in get_downloaded_song_records(DRIVE_DOWNLOAD_DIR):
            filename = record["filename"]
            if filename.casefold() not in local_names:
                songs.append(filename)
                local_names.add(filename.casefold())
    room_drive_dir = _room_drive_download_dir()
    if room_drive_dir:
        for record in get_downloaded_song_records(room_drive_dir):
            filename = record['filename']
            if filename.casefold() not in local_names:
                songs.append(filename)
                local_names.add(filename.casefold())
    return json.dumps(songs) 

@app.route('/api/songs/rename', methods=['POST'])
def batch_rename_songs():
    """Rename valid song rows while collecting row-specific failures."""
    global is_processing
    if is_processing:
        return json.dumps({'success': False, 'error': '目前已有其他製作或轉檔工作進行中，請稍候'}), 409
    is_processing = True
    socketio.emit('task_status', {'status': 'busy'})
    try:
        data = request.get_json(silent=True) or {}
        mappings = data.get('mappings')
        if not isinstance(mappings, list) or not mappings:
            return json.dumps({'success': False, 'error': '請至少輸入一筆歌名對照'}), 400

        parsed_mappings = []
        failures = []
        invalid_filename_characters = set('<>:"/\\|?*')
        for index, mapping in enumerate(mappings, start=1):
            if not isinstance(mapping, dict):
                failures.append({'line': index, 'old': '', 'new': '', 'error': '格式錯誤，需為舊歌名與新歌名'})
                continue
            old_stem = str(mapping.get('old', '')).strip()
            new_stem = str(mapping.get('new', '')).strip()
            if old_stem.lower().endswith('.mp4'):
                old_stem = old_stem[:-4]
            if new_stem.lower().endswith('.mp4'):
                new_stem = new_stem[:-4]
            if (not old_stem or not new_stem or old_stem in {'.', '..'} or new_stem in {'.', '..'}
                    or any(character in invalid_filename_characters for character in old_stem + new_stem)):
                failures.append({'line': index, 'old': old_stem, 'new': new_stem, 'error': '歌名空白或包含檔名不允許的字元'})
                continue
            parsed_mappings.append({'line': index, 'old': old_stem, 'new': new_stem})

        old_counts = {}
        new_counts = {}
        for mapping in parsed_mappings:
            old_counts[mapping['old'].casefold()] = old_counts.get(mapping['old'].casefold(), 0) + 1
            new_counts[mapping['new'].casefold()] = new_counts.get(mapping['new'].casefold(), 0) + 1
        renamed_songs = []
        queued_songs = {str(filename).casefold() for filename in playlist_queue}

        for mapping in parsed_mappings:
            line_number = mapping['line']
            old_stem = mapping['old']
            new_stem = mapping['new']
            if old_stem.casefold() == new_stem.casefold():
                failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': '新舊歌名相同'})
                continue
            if old_counts[old_stem.casefold()] > 1:
                failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': '舊歌名在批次中重複'})
                continue
            if new_counts[new_stem.casefold()] > 1:
                failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': '新歌名在批次中重複'})
                continue
            if f'{old_stem}.mp4'.casefold() in queued_songs:
                failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': '歌曲正在播放或待播，請先移出待播清單'})
                continue

            files_by_name = {filename.casefold(): filename for filename in os.listdir(SONGS_DIR)}
            old_song = files_by_name.get(f'{old_stem}.mp4'.casefold())
            if not old_song:
                failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': f'找不到來源歌曲：{old_stem}.mp4'})
                continue
            new_song = f'{new_stem}.mp4'
            if new_song.casefold() in files_by_name:
                failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': f'目的歌曲已存在：{new_song}'})
                continue
            if new_song in song_notes and new_song != old_song:
                failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': f'目的歌曲已有備註：{new_song}'})
                continue

            planned_files = []
            source_stem = os.path.splitext(old_song)[0]
            for filename in os.listdir(SONGS_DIR):
                filename_stem, extension = os.path.splitext(filename)
                if filename_stem.casefold() != source_stem.casefold() or extension.lower() not in {'.mp4', '.vtt', '.srt', '.lrc'}:
                    continue
                target_filename = new_stem + extension
                if target_filename.casefold() in files_by_name:
                    planned_files = []
                    failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': f'目的檔案已存在：{target_filename}'})
                    break
                planned_files.append((filename, target_filename))
            if not planned_files:
                if not any(failure['line'] == line_number for failure in failures):
                    failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': f'找不到來源歌曲：{old_stem}.mp4'})
                continue

            staged_files = []
            notes_snapshot = dict(song_notes) if old_song in song_notes else None
            try:
                for index, (source_filename, target_filename) in enumerate(planned_files):
                    source_path = os.path.join(SONGS_DIR, source_filename)
                    target_path = os.path.join(SONGS_DIR, target_filename)
                    temporary_path = os.path.join(SONGS_DIR, f'.batch-rename-{time.time_ns()}-{index}.tmp')
                    record = {'source': source_path, 'target': target_path, 'temporary': temporary_path, 'committed': False}
                    staged_files.append(record)
                    os.replace(source_path, temporary_path)
                for record in staged_files:
                    os.rename(record['temporary'], record['target'])
                    record['committed'] = True
                if old_song in song_notes:
                    song_notes[new_song] = song_notes.pop(old_song)
                    _save_song_notes()
            except (OSError, RuntimeError, ValueError, TypeError) as error:
                rollback_errors = []
                for record in reversed(staged_files):
                    current_path = record['target'] if record['committed'] else record['temporary']
                    if os.path.exists(current_path):
                        try:
                            os.replace(current_path, record['source'])
                        except OSError as rollback_error:
                            rollback_errors.append(str(rollback_error))
                if notes_snapshot is not None:
                    song_notes.clear()
                    song_notes.update(notes_snapshot)
                    try:
                        _save_song_notes()
                    except OSError as rollback_error:
                        rollback_errors.append(str(rollback_error))
                detail = f'此列改名失敗，已回復：{error}'
                if rollback_errors:
                    detail += f'；回復時另有錯誤：{"、".join(rollback_errors)}'
                failures.append({'line': line_number, 'old': old_stem, 'new': new_stem, 'error': detail})
                continue

            renamed_songs.append({
                'line': line_number,
                'old': old_stem,
                'new': new_stem,
                'lyrics': [os.path.splitext(source)[1] for source, _ in planned_files
                           if os.path.splitext(source)[1].lower() != '.mp4'],
            })

        if renamed_songs:
            socketio.emit('refresh_list')
        failures.sort(key=lambda failure: failure['line'])
        return json.dumps({
            'success': not failures,
            'total': len(mappings),
            'renamed': renamed_songs,
            'failures': failures,
        }, ensure_ascii=False)
    finally:
        is_processing = False
        socketio.emit('task_status', {'status': 'idle'})

song_audio_exception_cache = {}
song_audio_exception_cache_lock = threading.Lock()


@app.route('/api/song-audio-exceptions')
def get_song_audio_exceptions():
    """Return exception metadata only for explicitly requested songs.

    Do not scan the whole song library as a side effect of opening a page.  The
    caller must provide one or more ``filename`` query parameters when it
    actually needs the warning labels.
    """
    ffmpeg_dir = get_ffmpeg_location()
    ffmpeg_path = os.path.join(ffmpeg_dir, 'ffmpeg.exe') if ffmpeg_dir else shutil.which('ffmpeg')
    exceptions = {}
    requested_filenames = {
        os.path.basename(filename).strip()
        for filename in request.args.getlist('filename')
        if os.path.basename(filename).strip().lower().endswith('.mp4')
    }
    if not ffmpeg_path or not requested_filenames:
        return json.dumps(exceptions, ensure_ascii=False)
    song_stats = {}
    for filename in requested_filenames:
        song_path = os.path.join(SONGS_DIR, filename)
        if not os.path.isfile(song_path):
            continue
        if filename.lower().endswith('.mp4'):
            stat = os.stat(song_path)
            song_stats[filename] = (stat.st_mtime_ns, stat.st_size)
    with song_audio_exception_cache_lock:
        for removed_song in set(song_audio_exception_cache) - set(song_stats):
            song_audio_exception_cache.pop(removed_song, None)
        for filename, stat_key in song_stats.items():
            cached = song_audio_exception_cache.get(filename)
            if cached is None or cached[0] != stat_key:
                has_peak_exception, peak_limit, peak_measured = _get_c4_peak_exception_metadata(
                    ffmpeg_path, os.path.join(SONGS_DIR, filename),
                )
                has_lra_exception, lra_limit, lra_measured = _get_c4_lra_exception_metadata(
                    ffmpeg_path, os.path.join(SONGS_DIR, filename),
                )
                has_audio_exception, audio_exceptions, valid_audio_metadata = _get_audio_exception_metadata(
                    ffmpeg_path, os.path.join(SONGS_DIR, filename),
                )
                exception = {
                    'audio_exceptions': audio_exceptions,
                    'metadata_error': has_audio_exception and not valid_audio_metadata,
                    'c4_true_peak_dbtp': peak_measured,
                    'automatic_limit_dbtp': peak_limit,
                    'c4_lra_lu': lra_measured,
                    'approved_lra_limit_lu': lra_limit,
                } if has_audio_exception or has_peak_exception or has_lra_exception else None
                song_audio_exception_cache[filename] = (stat_key, exception)
            exception = song_audio_exception_cache[filename][1]
            if exception:
                exceptions[filename] = exception
    return json.dumps(exceptions, ensure_ascii=False)

@app.route('/api/song-notes')
def get_song_notes():
    """提供後台排序歌曲用的獨立備註資料，不修改歌曲本身結構。"""
    return json.dumps(song_notes, ensure_ascii=False)

@app.route('/api/subtitles')
def get_subtitle_list():
    """Return MP4 filenames that have a matching WebVTT subtitle file."""
    subtitles = {
        os.path.splitext(filename)[0] + '.mp4'
        for filename in os.listdir(SONGS_DIR)
        if filename.lower().endswith('.vtt')
    }
    if not IANAUTOKTV_CLOUD_MODE:
        subtitles.update(
            record['filename']
            for record in get_downloaded_song_records(DRIVE_DOWNLOAD_DIR)
            if get_downloaded_subtitle_path(
                DRIVE_DOWNLOAD_DIR,
                os.path.splitext(record['filename'])[0] + '.vtt',
            )
        )
    room_drive_dir = _room_drive_download_dir()
    if room_drive_dir:
        subtitles.update(
            record['filename']
            for record in get_downloaded_song_records(room_drive_dir)
            if get_downloaded_subtitle_path(
                room_drive_dir,
                os.path.splitext(record['filename'])[0] + '.vtt',
            )
        )
    return json.dumps(sorted(subtitles), ensure_ascii=False)

@app.route('/api/stickers')
def get_sticker_list():
    """Return only supported sticker files stored in the project asset directory."""
    if not os.path.isdir(STICKERS_DIR):
        return json.dumps([], ensure_ascii=False)
    filenames = [
        filename for filename in os.listdir(STICKERS_DIR)
        if os.path.splitext(filename)[1].lower() in STICKER_EXTENSIONS
        and os.path.isfile(os.path.join(STICKERS_DIR, filename))
    ]
    return json.dumps(sorted(filenames, key=str.casefold), ensure_ascii=False)

def parse_vtt_timestamp(timestamp):
    """Convert a WebVTT timestamp into seconds."""
    match = re.match(r'^(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})$', timestamp.strip())
    if not match:
        raise ValueError('VTT 時間格式錯誤')
    hours, minutes, seconds, milliseconds = match.groups()
    return (int(hours or 0) * 3600) + (int(minutes) * 60) + int(seconds) + int(milliseconds) / 1000

def parse_vtt_cues(content):
    """Parse simple WebVTT cues used by the KTV lyric editor."""
    lines = content.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    cues = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if '-->' not in line:
            index += 1
            continue
        start_text, end_text = [part.strip().split(' ', 1)[0] for part in line.split('-->', 1)]
        try:
            start = parse_vtt_timestamp(start_text)
            end = parse_vtt_timestamp(end_text)
        except ValueError:
            index += 1
            continue
        index += 1
        text_lines = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(re.sub(r'<[^>]*>', '', lines[index].strip()))
            index += 1
        text = '\n'.join(text_lines).strip()
        if text and end > start:
            cues.append({'start': start, 'end': end, 'text': text})
        index += 1
    return cues

@app.route('/api/subtitles/manual')
def get_manual_subtitle():
    """Return an existing song's VTT cues for editing in the admin tool."""
    song_filename = os.path.basename(request.args.get('song', '').strip())
    song_path = os.path.join(SONGS_DIR, song_filename)
    subtitle_path = os.path.join(SONGS_DIR, os.path.splitext(song_filename)[0] + '.vtt')
    if not song_filename.lower().endswith('.mp4') or not os.path.exists(song_path):
        return json.dumps({'success': False, 'error': '請選擇有效的歌曲'}), 400
    if not os.path.exists(subtitle_path):
        return json.dumps({'success': True, 'exists': False, 'plain_lyrics': False, 'cues': []}, ensure_ascii=False)
    try:
        with open(subtitle_path, 'r', encoding='utf-8-sig') as subtitle_file:
            cues = parse_vtt_cues(subtitle_file.read())
        return json.dumps({
            'success': True,
            'exists': True,
            'plain_lyrics': _song_uses_plain_lyrics(song_filename),
            'cues': cues,
        }, ensure_ascii=False)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return json.dumps({'success': False, 'error': f'既有歌詞讀取失敗：{error}'}, ensure_ascii=False), 400

def split_song_filename(song_filename):
    """Extract the title and artist from title-artist-language-number filenames."""
    stem = os.path.splitext(os.path.basename(song_filename))[0]
    parts = stem.rsplit('-', 3)
    if len(parts) == 4:
        return parts[0].strip(), parts[1].strip()
    return stem.strip(), ''

def lyrics_provider_request(provider_id, path, query=None):
    """Request JSON from a registered lyrics provider."""
    provider = LYRICS_PROVIDERS.get(provider_id)
    if not provider:
        raise ValueError('不支援的歌詞伺服器')
    url = f"{provider['base_url']}{path}"
    if query:
        url = f'{url}?{urlencode(query)}'
    request = Request(url, headers={'User-Agent': LYRICS_USER_AGENT, 'Accept': 'application/json'})
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        try:
            detail = error.read().decode('utf-8', errors='replace')
        except OSError:
            detail = ''
        raise RuntimeError(f'歌詞伺服器回應 HTTP {error.code}：{detail[-500:]}') from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f'無法連線歌詞伺服器：{error}') from error


def netease_search_lyrics(query):
    """搜尋 NetEase 歌曲並補上同步 LRC，轉成共用搜尋結果格式。"""
    response = lyrics_provider_request('netease', '/api/search/get/web', {
        's': query,
        'type': 1,
        'offset': 0,
        'total': 'true',
        'limit': 10,
    })
    songs = response.get('result', {}).get('songs', []) if isinstance(response, dict) else []
    records = []
    for song in songs[:10]:
        song_id = song.get('id')
        if not song_id:
            continue
        try:
            lyric_response = lyrics_provider_request('netease', '/api/song/lyric', {
                'id': song_id,
                'lv': 1,
                'kv': 1,
                'tv': -1,
            })
        except RuntimeError:
            # 單首歌詞查詢失敗時仍保留其他搜尋結果。
            lyric_response = {}
        synced_lyrics = lyric_response.get('lrc', {}).get('lyric', '') if isinstance(lyric_response, dict) else ''
        artists = song.get('artists') or []
        albums = song.get('album') or {}
        records.append({
            'id': song_id,
            'trackName': song.get('name', ''),
            'artistName': '、'.join(artist.get('name', '') for artist in artists),
            'albumName': albums.get('name', ''),
            'duration': (song.get('duration') or 0) / 1000,
            'syncedLyrics': synced_lyrics,
        })
    return records


def lyrics_ovh_get_lyrics(title, artist):
    """取得 lyrics.ovh 純文字歌詞；此來源沒有時間碼。"""
    path = f"/v1/{quote(artist or 'unknown', safe='')}/{quote(title, safe='')}"
    return lyrics_provider_request('lyrics_ovh', path)

@app.route('/api/lyrics/providers')
def get_lyrics_providers():
    """Return the lyrics providers currently available to the admin UI."""
    return json.dumps([
        {'id': provider_id, 'name': provider['name']}
        for provider_id, provider in LYRICS_PROVIDERS.items()
    ], ensure_ascii=False)

@app.route('/api/lyrics/search')
def search_lyrics():
    """Search a lyrics provider using an optional local-song filename and query override."""
    provider_id = request.args.get('provider', 'lrclib').strip().lower()
    song_filename = os.path.basename(request.args.get('song', '').strip())
    query_override = request.args.get('query', '').strip()
    if not song_filename.lower().endswith('.mp4') or not os.path.exists(os.path.join(SONGS_DIR, song_filename)):
        return json.dumps({'success': False, 'error': '請選擇有效的本地歌曲'}), 400
    title, artist = split_song_filename(song_filename)
    if not query_override and not title:
        return json.dumps({'success': False, 'error': '找不到歌名，請自行輸入搜尋關鍵字'}), 400
    try:
        query = {'q': query_override} if query_override else {'track_name': title, 'artist_name': artist}
        if provider_id == 'netease':
            records = netease_search_lyrics(query_override or f'{title} {artist}'.strip())
        elif provider_id == 'lyrics_ovh':
            try:
                plain_lyrics = lyrics_ovh_get_lyrics(query_override or title, artist)
            except RuntimeError as error:
                if 'HTTP 404' in str(error):
                    return json.dumps({'success': False, 'error': 'lyrics.ovh 找不到這組歌手與歌名的精確歌詞，請改用 NetEase 或 LRCLIB 搜尋。'}, ensure_ascii=False), 404
                raise
            records = [{
                'id': f'{artist}|{title}',
                'trackName': title,
                'artistName': artist,
                'albumName': '',
                'duration': None,
                'plainLyrics': plain_lyrics.get('lyrics', '') if isinstance(plain_lyrics, dict) else '',
            }]
        else:
            records = lyrics_provider_request(provider_id, '/search', query)
        results = []
        for record in records if isinstance(records, list) else []:
            results.append({
                'id': record.get('id'),
                'trackName': record.get('trackName') or record.get('name', ''),
                'artistName': record.get('artistName', ''),
                'albumName': record.get('albumName', ''),
                'duration': record.get('duration'),
                'hasSyncedLyrics': bool((record.get('syncedLyrics') or '').strip()),
                'plainLyrics': (record.get('plainLyrics') or '').strip(),
                'preview': (record.get('syncedLyrics') or record.get('plainLyrics') or '').strip()[:240],
            })
        return json.dumps({
            'success': True,
            'song': song_filename,
            'suggested_title': title,
            'suggested_artist': artist,
            'query': query_override or f'{title} {artist}'.strip(),
            'results': results,
        }, ensure_ascii=False)
    except (RuntimeError, ValueError) as error:
        return json.dumps({'success': False, 'error': str(error)}, ensure_ascii=False), 502

@app.route('/api/lyrics/download', methods=['POST'])
def download_lyrics():
    """Fetch selected synchronized lyrics and save them as the song's WebVTT file."""
    global is_processing
    data = request.get_json(silent=True) or {}
    provider_id = str(data.get('provider', 'lrclib')).strip().lower()
    song_filename = os.path.basename(str(data.get('song', '')).strip())
    record_id = data.get('id')
    overwrite = bool(data.get('overwrite', False))
    song_path = os.path.join(SONGS_DIR, song_filename)
    if is_processing:
        return json.dumps({'success': False, 'error': '目前已有其他製作或轉檔工作進行中，請稍候'}), 409
    if not song_filename.lower().endswith('.mp4') or not os.path.exists(song_path):
        return json.dumps({'success': False, 'error': '請選擇有效的本地歌曲'}), 400
    if provider_id != 'lyrics_ovh':
        try:
            record_id = int(record_id)
        except (TypeError, ValueError):
            return json.dumps({'success': False, 'error': '請選擇有效的歌詞搜尋結果'}), 400
    output_name = os.path.splitext(song_filename)[0] + '.vtt'
    output_path = os.path.join(SONGS_DIR, output_name)
    if os.path.exists(output_path) and not overwrite:
        return json.dumps({'success': False, 'requires_overwrite': True, 'filename': output_name, 'error': '此歌曲已有歌詞，是否覆蓋？'}), 409
    try:
        if provider_id == 'netease':
            record = lyrics_provider_request('netease', '/api/song/lyric', {'id': record_id, 'lv': 1, 'kv': 1, 'tv': -1})
            record = {
                'syncedLyrics': record.get('lrc', {}).get('lyric', '') if isinstance(record, dict) else '',
                'trackName': '',
                'artistName': '',
            }
        elif provider_id == 'lyrics_ovh':
            artist, title = str(record_id).split('|', 1) if '|' in str(record_id) else ('', str(record_id))
            record = lyrics_ovh_get_lyrics(title, artist)
            record = {
                'plainLyrics': record.get('lyrics', '') if isinstance(record, dict) else '',
                'trackName': title,
                'artistName': artist,
            }
        else:
            record = lyrics_provider_request(provider_id, f'/get/{record_id}')
        synced_lyrics = str(record.get('syncedLyrics') or '').strip()
        plain_lyrics = str(record.get('plainLyrics') or '').strip()
        if not synced_lyrics and not plain_lyrics:
            return json.dumps({'success': False, 'error': '此搜尋結果沒有可用歌詞'}), 400
        output_name = save_subtitle(song_filename, synced_lyrics or plain_lyrics, 'lrc' if synced_lyrics else 'plain')
        socketio.emit('refresh_list')
        return json.dumps({'success': True, 'filename': output_name, 'trackName': record.get('trackName', ''), 'artistName': record.get('artistName', '')}, ensure_ascii=False)
    except (RuntimeError, ValueError, OSError) as error:
        return json.dumps({'success': False, 'error': f'歌詞下載失敗：{error}'}, ensure_ascii=False), 502

@app.route('/api/subtitles/upload', methods=['POST'])
def upload_subtitle():
    """Convert subtitle file or text and save it beside its matching song."""
    song_filename = os.path.basename(request.form.get('song', ''))
    subtitle_file = request.files.get('subtitle')
    if not song_filename.lower().endswith('.mp4') or not os.path.exists(os.path.join(SONGS_DIR, song_filename)):
        return json.dumps({'success': False, 'error': '請選擇有效的歌曲'}), 400
    subtitle_text = request.form.get('subtitle_content', '').strip()
    if subtitle_file and subtitle_file.filename and subtitle_text:
        return json.dumps({'success': False, 'error': '字幕檔案與文字內容請擇一輸入'}), 400
    if subtitle_file and subtitle_file.filename:
        subtitle_extension = os.path.splitext(subtitle_file.filename)[1].lower().lstrip('.')
        if subtitle_extension not in SUBTITLE_EXTENSIONS:
            return json.dumps({'success': False, 'error': '只支援 .srt、.lrc 或 .vtt 字幕檔'}), 400
    elif subtitle_text:
        subtitle_extension = str(request.form.get('subtitle_extension', '')).lower().lstrip('.')
    else:
        return json.dumps({'success': False, 'error': '請選擇字幕檔或輸入字幕文字'}), 400
    try:
        content = subtitle_file.read().decode('utf-8-sig') if subtitle_file else subtitle_text
        subtitle_extension = detect_subtitle_format(content, subtitle_extension)
        output_name = save_subtitle(song_filename, content, subtitle_extension)
        socketio.emit('refresh_list')
        return json.dumps({'success': True, 'filename': output_name}, ensure_ascii=False)
    except (UnicodeDecodeError, OSError, ValueError) as error:
        return json.dumps({'success': False, 'error': f'字幕檔處理失敗：{error}'}), 400

@app.route('/api/subtitles/manual', methods=['POST'])
def save_manual_subtitle():
    """Validate timed lyric cues and save them as the selected song's VTT file."""
    data = request.get_json(silent=True) or {}
    song_filename = os.path.basename(str(data.get('song', '')))
    cues = data.get('cues', [])
    song_path = os.path.join(SONGS_DIR, song_filename)
    if not song_filename.lower().endswith('.mp4') or not os.path.exists(song_path):
        return json.dumps({'success': False, 'error': '請選擇有效的歌曲'}), 400
    if not isinstance(cues, list) or not cues:
        return json.dumps({'success': False, 'error': '請至少建立一句歌詞'}), 400
    normalized_cues = []
    for cue_index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict):
            return json.dumps({'success': False, 'error': f'第 {cue_index} 筆歌詞資料格式錯誤'}, ensure_ascii=False), 400
        text = str(cue.get('text', '')).strip()
        try:
            start = float(cue.get('start'))
            end = float(cue.get('end'))
        except (TypeError, ValueError):
            return json.dumps({'success': False, 'error': f'第 {cue_index} 筆歌詞時間格式錯誤'}, ensure_ascii=False), 400
        if not math.isfinite(start) or not math.isfinite(end):
            return json.dumps({'success': False, 'error': f'第 {cue_index} 筆歌詞時間必須是有限數值'}, ensure_ascii=False), 400
        if not text or start < 0 or end <= start:
            return json.dumps({'success': False, 'error': f'第 {cue_index} 筆歌詞內容或時間範圍無效'}, ensure_ascii=False), 400
        normalized_cues.append((start, end, text))
    normalized_cues.sort(key=lambda cue: cue[0])
    for index in range(1, len(normalized_cues)):
        if normalized_cues[index][0] < normalized_cues[index - 1][1]:
            return json.dumps({'success': False, 'error': '歌詞時間不可重疊'}), 400
    output_name = os.path.splitext(song_filename)[0] + '.vtt'
    output_path = os.path.join(SONGS_DIR, output_name)
    try:
        with open(output_path, 'w', encoding='utf-8', newline='\n') as output_file:
            output_file.write('WEBVTT\n\n')
            for start, end, text in normalized_cues:
                output_file.write(f'{format_vtt_time(round(start * 1000))} --> {format_vtt_time(round(end * 1000))}\n{text}\n\n')
        socketio.emit('refresh_list')
        return json.dumps({'success': True, 'filename': output_name}, ensure_ascii=False)
    except OSError as error:
        return json.dumps({'success': False, 'error': f'歌詞儲存失敗：{error}'}), 400

@app.route('/api/videos/optimize', methods=['POST'])
def optimize_video():
    """Convert an uploaded MP4 to low-load H.264 and replace its song file."""
    global is_processing
    if is_processing:
        return json.dumps({'success': False, 'error': '目前已有其他製作或轉檔工作進行中，請稍候'}), 409
    video_file = request.files.get('video')
    if not video_file or not video_file.filename:
        broadcast_log('❌ 影片最佳化失敗：未選擇 MP4 檔案。')
        return json.dumps({'error': '請選擇要轉檔的 MP4 影片'}), 400
    filename = os.path.basename(video_file.filename)
    if not filename.lower().endswith('.mp4'):
        broadcast_log(f'❌ 影片最佳化失敗：檔案不是 MP4（{filename}）。')
        return json.dumps({'error': '影片檔必須是 .mp4'}), 400

    job_dir = os.path.join(TEMP_BASE_DIR, f'video_optimize_{time.time_ns()}')
    source_path = os.path.join(job_dir, filename)
    output_path = os.path.join(job_dir, f'{os.path.splitext(filename)[0]}.optimized.mp4')
    final_path = os.path.join(SONGS_DIR, filename)
    os.makedirs(job_dir, exist_ok=True)
    is_processing = True
    socketio.emit('task_status', {'status': 'busy'})
    try:
        broadcast_log(f'=== 開始影片效能最佳化：{filename} ===')
        video_file.save(source_path)
        broadcast_log(f'📥 已接收影片：{filename}（{os.path.getsize(source_path):,} bytes）')
        ffmpeg_dir = get_ffmpeg_location()
        ffmpeg_path = os.path.join(ffmpeg_dir, 'ffmpeg.exe') if ffmpeg_dir else shutil.which('ffmpeg')
        if not ffmpeg_path:
            broadcast_log('❌ 影片最佳化失敗：找不到 FFmpeg。')
            return json.dumps({'error': '找不到 FFmpeg'}), 500
        broadcast_log(f"🔧 使用 FFmpeg：{ffmpeg_path}")
        broadcast_log('⏳ 正在重新編碼為 H.264 / 最高 720p / 30fps，請稍候...')
        _optimize_downloaded_video(ffmpeg_path, source_path, output_path)
        broadcast_log(f'✅ FFmpeg 轉檔完成：輸出 {os.path.getsize(output_path):,} bytes。')
        shutil.move(output_path, final_path)
        broadcast_log(f'✅ 已取代原始影片：{filename}')
        socketio.emit('refresh_list')
        broadcast_log(f'=== 影片效能最佳化成功：{filename} ===')
        return json.dumps({'success': True, 'filename': filename}, ensure_ascii=False)
    except (OSError, ValueError) as error:
        broadcast_log(f'❌ 影片最佳化發生例外：{error}')
        return json.dumps({'success': False, 'error': f'影片轉檔失敗：{error}'}, ensure_ascii=False), 400
    finally:
        is_processing = False
        socketio.emit('task_status', {'status': 'idle'})
        shutil.rmtree(job_dir, ignore_errors=True)

def _balance_six_channel_loudness(ffmpeg_path, source_path, output_path):
    """Correct each playback mode on its selected channels before rebuilding 5.1."""
    mode_levels = {
        mode: _measure_audio_metrics(ffmpeg_path, source_path, mode)
        for mode in ('original', 'guide', 'instrumental')
    }
    if any(level[0] is None or not math.isfinite(level[0])
           or (mode != 'original' and (level[1] is None or not math.isfinite(level[1])))
           for mode, level in mode_levels.items()):
        raise RuntimeError('FFmpeg 無法量測六聲道輸出的模式音量或 True Peak')
    has_vocal_stem = _has_separated_vocal_track(ffmpeg_path, source_path)
    has_peak_exception, peak_exception_limit, peak_exception_measured = _get_c4_peak_exception_metadata(ffmpeg_path, source_path)
    if has_peak_exception and (peak_exception_limit is None or peak_exception_measured is None):
        raise RuntimeError('既有歌曲的伴奏 True Peak 例外標籤缺少有效上限')
    has_lra_exception, lra_exception_limit, lra_exception_measured = _get_c4_lra_exception_metadata(
        ffmpeg_path, source_path,
    )
    if has_lra_exception and (lra_exception_limit is None or lra_exception_measured is None):
        raise RuntimeError('既有歌曲的伴奏 LRA 例外標籤缺少有效上限')

    def gain_factor(mode, peak_target=-2.5):
        loudness, peak = mode_levels[mode]
        gain_db = -14.0 - loudness
        if peak is not None:
            gain_db = min(gain_db, peak_target - peak)
        return 10 ** (gain_db / 20.0)

    original_factor = gain_factor('original', peak_target=-1.0)
    instrumental_factor = gain_factor('instrumental')
    guide_expression = 'c2'
    if not has_vocal_stem:
        guide_expression = f'{gain_factor("guide"):.6f}*c2'
    audio_filter = (
        f'[0:a]pan=5.1|c0={original_factor:.6f}*c0|c1={original_factor:.6f}*c1|'
        f'c2={guide_expression}|c3=c3|c4={instrumental_factor:.6f}*c4|'
        f'c5={instrumental_factor:.6f}*c5,'
        'aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=5.1[audio]'
    )
    command = [
        ffmpeg_path, '-y', '-i', source_path, '-filter_complex', audio_filter,
        '-map', '0:v:0', '-map', '[audio]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '384k',
        '-movflags', '+faststart+use_metadata_tags',
        '-metadata', f'{AUDIO_EXCEPTIONS_TAG}=',
    ]
    if has_vocal_stem:
        command.extend(['-metadata:s:a:0', f'{VOCAL_STEM_LAYOUT_TAG}={VOCAL_STEM_LAYOUT_VERSION}'])
    if has_peak_exception:
        command.extend([
            '-metadata', f'{C4_PEAK_EXCEPTION_TAG}=true',
            '-metadata', f'{C4_PEAK_EXCEPTION_LIMIT_TAG}={peak_exception_limit:.3f}',
            '-metadata', f'{C4_PEAK_EXCEPTION_MEASURED_TAG}={peak_exception_measured:.3f}',
        ])
    command.append(output_path)
    try:
        subprocess.run(
            command,
            check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=600, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('六聲道音量平衡逾時（超過 600 秒）') from error
    if has_peak_exception or has_lra_exception:
        _, balanced_peak = _measure_audio_metrics(ffmpeg_path, output_path, 'instrumental')
        balanced_lra = _measure_audio_loudness_range(ffmpeg_path, output_path)
        if (has_peak_exception and balanced_peak is None) or (has_lra_exception and balanced_lra is None):
            raise RuntimeError('既有歌曲平衡後無法量測 c4 例外指標')
        _write_c4_audio_exception_metadata(
            ffmpeg_path, output_path,
            peak_limit=peak_exception_limit if has_peak_exception else None,
            peak_measured=balanced_peak if has_peak_exception else None,
            peak_marked=balanced_peak > -1.0 if has_peak_exception else True,
            lra_limit=lra_exception_limit if has_lra_exception else None,
            lra_measured=balanced_lra if has_lra_exception else None,
            lra_marked=(
                balanced_lra > 8.0 and balanced_lra <= lra_exception_limit
                if has_lra_exception else True
            ),
        )

def _normalize_accompaniment_source(ffmpeg_path, source_path, output_path):
    """Normalize and compensate PCM accompaniment before the single final AAC encode."""
    base_output = output_path + '.base.wav'
    try:
        result = subprocess.run(
            [
                ffmpeg_path, '-y', '-i', source_path,
                '-af', 'pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1,acompressor=threshold=-24dB:ratio=8:attack=5:release=100:makeup=1,loudnorm=I=-14:TP=-3:LRA=7',
                '-ar', '44100', '-ac', '2', '-c:a', 'pcm_s16le', base_output,
            ],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode != 0 or not os.path.exists(base_output):
            detail = (result.stderr or result.stdout or 'FFmpeg 未提供錯誤訊息').strip()[-2000:]
            raise RuntimeError(f'伴奏 pre-AAC 響度處理失敗：{detail}')
        ffmpeg_metrics = _measure_stereo_source_metrics(ffmpeg_path, base_output)
        if ffmpeg_metrics[0] is None:
            raise RuntimeError('伴奏 pre-AAC 響度量測失敗')
        # 為 AAC 編碼保留峰值餘裕，補償後的 PCM 不得超過 -3 dBTP。
        requested_compensation = max(0.0, -14.0 - ffmpeg_metrics[0])
        peak_headroom = max(0.0, -3.0 - ffmpeg_metrics[1]) if ffmpeg_metrics[1] is not None else 0.0
        compensation = min(requested_compensation, peak_headroom)
        if compensation < 0.05:
            os.replace(base_output, output_path)
            return
        result = subprocess.run(
            [
                ffmpeg_path, '-y', '-i', base_output,
                '-af', f'volume={compensation:.3f}dB',
                '-ar', '44100', '-ac', '2', '-c:a', 'pcm_s16le', output_path,
            ],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode != 0 or not os.path.exists(output_path):
            detail = (result.stderr or result.stdout or 'FFmpeg 未提供錯誤訊息').strip()[-2000:]
            raise RuntimeError(f'伴奏 PCM 補償失敗：{detail}')
    finally:
        if os.path.exists(base_output):
            os.remove(base_output)

def _create_six_channel_mp4(ffmpeg_path, ffprobe_path, source_path, vocal_path, accompaniment_path, output_path, normalize_volume=True):
    """Create one MP4 using the six-channel mix topology."""
    # Store isolated vocals in c2 so playback can change vocal gain without changing c4 accompaniment.
    original_loudnorm = ''
    # c4 is mono here and duplicated to stereo for playback, so pre-compensate the measured LUFS and AAC peak.
    mode_loudnorm = ''
    prepared_accompaniment_path = accompaniment_path
    temporary_accompaniment_path = None
    if normalize_volume:
        original_loudness, original_peak = _measure_stereo_source_metrics(ffmpeg_path, source_path)
        if original_loudness is None or original_peak is None:
            raise RuntimeError('無法在唯一一次 AAC 輸出前量測原聲或伴奏響度')
        original_gain = min(-14.0 - original_loudness, -2.5 - original_peak)
        original_loudnorm = f'volume={original_gain:.3f}dB,'
        temporary_accompaniment_path = output_path + '.normalized_accompaniment.wav'
        _normalize_accompaniment_source(ffmpeg_path, accompaniment_path, temporary_accompaniment_path)
        prepared_accompaniment_path = temporary_accompaniment_path
    # 明確分流已校正原聲，確保左右聲道都保留相同的響度增益。
    audio_filter = (
        f'[0:a]pan=stereo|c0=FL|c1=FR,{original_loudnorm}aresample=async=1,'
        'aformat=sample_fmts=fltp:sample_rates=44100[original];'
        '[original]asplit=2[original_left_source][original_right_source];'
        '[original_left_source]pan=mono|c0=FL[original_l];'
        '[original_right_source]pan=mono|c0=FR[original_r];'
        '[1:a]pan=mono|c0=0.5*FL+0.5*FR,'
        'aformat=sample_fmts=fltp:sample_rates=44100[vocals];'
        f'[2:a]pan=mono|c0=0.5*FL+0.5*FR,{mode_loudnorm}aresample=async=1,'
        'aformat=sample_fmts=fltp:sample_rates=44100[instrumental];'
        '[vocals]asplit=2[vocal_channel][unused_vocal_source];'
        '[unused_vocal_source]volume=0[unused_vocal];'
        '[instrumental]asplit=2[instrumental_channel][unused_instrumental_source];'
        '[unused_instrumental_source]volume=0[unused_instrumental];'
        '[original_l][original_r][vocal_channel][unused_vocal][instrumental_channel][unused_instrumental]'
        'join=inputs=6:channel_layout=5.1:map=0.0-FL|1.0-FR|2.0-FC|3.0-LFE|4.0-BL|5.0-BR,'
        'aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=5.1[audio]'
    )
    command = [
        ffmpeg_path, '-y', '-i', source_path, '-i', vocal_path, '-i', prepared_accompaniment_path,
        '-filter_complex', audio_filter,
        '-map', '0:v:0', '-map', '[audio]',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '384k', '-movflags', '+faststart',
        '-shortest',
        '-metadata:s:a:0', 'title=原聲、人聲、伴奏（六聲道）',
        '-metadata:s:a:0', f'{VOCAL_STEM_LAYOUT_TAG}={VOCAL_STEM_LAYOUT_VERSION}', output_path,
    ]
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    finally:
        if temporary_accompaniment_path and os.path.exists(temporary_accompaniment_path):
            os.remove(temporary_accompaniment_path)
    if result.returncode != 0:
        detail = result.stderr.strip()[-3000:] if result.stderr else 'FFmpeg 未提供錯誤訊息'
        raise RuntimeError(f'六聲道合成失敗（return code {result.returncode}）：{detail}')
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError('六聲道合成失敗：FFmpeg 未產生有效輸出檔案')
    probe_command = [
        ffprobe_path, '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=channels', '-of', 'default=noprint_wrappers=1:nokey=1', output_path,
    ]
    probe_result = subprocess.run(
        probe_command, check=True, capture_output=True, text=True,
        encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    if probe_result.stdout.strip() != '6':
        raise RuntimeError(f'FFprobe 驗證失敗：輸出音訊聲道數為 {probe_result.stdout.strip() or "未知"}，預期 6')

def _rebalance_encoded_six_channel_audio(ffmpeg_path, song_path):
    """Rebalance an encoded six-channel file using the shared channel rules."""
    temporary_path = song_path + '.rebalance.mp4'
    try:
        _balance_six_channel_loudness(ffmpeg_path, song_path, temporary_path)
        os.replace(temporary_path, song_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)

def _collect_audio_range_exceptions(ffmpeg_path, ffprobe_path, song_path):
    """Measure range deviations while keeping channel and measurement failures fatal."""
    channel_count = get_audio_channel_count_from_path(song_path, ffprobe_path)
    if channel_count != 6:
        raise RuntimeError(f'最終音訊驗證失敗：聲道數為 {channel_count or "未知"}，預期 6')
    has_vocal_stem = _has_separated_vocal_track(ffmpeg_path, song_path)
    exceptions = []
    for mode in ('original', 'guide', 'instrumental'):
        if mode == 'guide' and has_vocal_stem:
            continue
        loudness, peak = _measure_audio_metrics(ffmpeg_path, song_path, mode)
        if loudness is None or not math.isfinite(loudness):
            raise RuntimeError(f'最終音訊驗證失敗：{mode} LUFS 無法量測')
        if mode == 'original':
            continue
        if peak is None or not math.isfinite(peak):
            raise RuntimeError(f'最終音訊驗證失敗：{mode} True Peak 無法量測')
        if abs(loudness - AUDIO_LOUDNESS_TARGET_LUFS) > AUDIO_LOUDNESS_TOLERANCE_LU:
            exceptions.append(_audio_exception_entry(mode, 'loudness', loudness))
        if peak > -1.0:
            exceptions.append(_audio_exception_entry(mode, 'true_peak', peak))
        if mode == 'instrumental':
            loudness_range = _measure_audio_loudness_range(ffmpeg_path, song_path, mode)
            if loudness_range is None or not math.isfinite(loudness_range):
                raise RuntimeError('最終音訊驗證失敗：instrumental LRA 無法量測')
            if loudness_range > 8.0:
                exceptions.append(_audio_exception_entry(mode, 'loudness_range', loudness_range))
    return exceptions


def _audio_exception_key(exception):
    return exception['mode'], exception['metric']


def _format_audio_exception(exception):
    mode_names = {'guide': '導唱 c2', 'instrumental': '伴奏 c4'}
    metric_names = {
        'loudness': f"{exception['measured']:.1f} LUFS（標準 {exception['standard']}）",
        'true_peak': f"{exception['measured']:+.1f} dBTP（標準 {exception['standard']}）",
        'loudness_range': f"{exception['measured']:.1f} LU（標準 {exception['standard']}）",
    }
    return f"{mode_names[exception['mode']]} {metric_names[exception['metric']]}"


def _validate_six_channel_audio(ffmpeg_path, ffprobe_path, song_path):
    """Accept measured range deviations only when matching special-song metadata exists."""
    current_exceptions = _collect_audio_range_exceptions(ffmpeg_path, ffprobe_path, song_path)
    has_audio_metadata, stored_exceptions, valid_audio_metadata = _get_audio_exception_metadata(
        ffmpeg_path, song_path,
    )
    has_peak_exception, stored_peak_limit, stored_peak_measurement = _get_c4_peak_exception_metadata(
        ffmpeg_path, song_path,
    )
    has_lra_exception, stored_lra_limit, stored_lra_measurement = _get_c4_lra_exception_metadata(
        ffmpeg_path, song_path,
    )
    warnings = []
    if has_audio_metadata and not valid_audio_metadata:
        warnings.append('特殊歌曲音訊標記格式無效')
    if has_peak_exception and (stored_peak_limit is None or stored_peak_measurement is None):
        warnings.append('instrumental TP 例外標籤缺少有效資料')
    if has_lra_exception and (stored_lra_limit is None or stored_lra_measurement is None):
        warnings.append('instrumental LRA 例外標籤缺少有效資料')

    stored_by_key = {
        _audio_exception_key(exception): exception for exception in stored_exceptions
    } if valid_audio_metadata else {}
    current_by_key = {
        _audio_exception_key(exception): exception for exception in current_exceptions
    }
    for key, stored in stored_by_key.items():
        current = current_by_key.get(key)
        if current is None or abs(current['measured'] - stored['measured']) > 0.05:
            warnings.append(f"特殊歌曲標記實測值不符：{stored['mode']} {stored['metric']}")

    for key, current in current_by_key.items():
        stored = stored_by_key.get(key)
        if stored and abs(current['measured'] - stored['measured']) <= 0.05:
            continue
        legacy_valid = False
        if key == ('instrumental', 'true_peak') and has_peak_exception:
            legacy_valid = (
                stored_peak_limit is not None and stored_peak_measurement is not None
                and current['measured'] <= stored_peak_limit
                and abs(current['measured'] - stored_peak_measurement) <= 0.05
            )
        elif key == ('instrumental', 'loudness_range') and has_lra_exception:
            legacy_valid = (
                stored_lra_limit is not None and stored_lra_measurement is not None
                and current['measured'] <= stored_lra_limit
                and abs(current['measured'] - stored_lra_measurement) <= 0.05
            )
        if not legacy_valid:
            warnings.append(_format_audio_exception(current))

    if warnings:
        raise RuntimeError('最終音訊響度驗證失敗：' + '、'.join(warnings))


def _validate_six_channel_audio_with_exceptions(ffmpeg_path, ffprobe_path, song_path):
    """Validate a song and mark every measurable range deviation as a special-song exception."""
    try:
        _validate_six_channel_audio(ffmpeg_path, ffprobe_path, song_path)
        return []
    except RuntimeError as strict_error:
        try:
            exceptions = _collect_audio_range_exceptions(ffmpeg_path, ffprobe_path, song_path)
        except RuntimeError:
            raise strict_error
        if not exceptions:
            raise strict_error
        _write_audio_exception_metadata(ffmpeg_path, song_path, exceptions)
        _validate_six_channel_audio(ffmpeg_path, ffprobe_path, song_path)
        return exceptions

def _optimize_downloaded_video(ffmpeg_path, source_path, output_path):
    """Convert and validate a downloaded video for reliable legacy-PC playback."""
    command = [
        ffmpeg_path, '-y', '-fflags', '+genpts', '-err_detect', 'ignore_err', '-i', source_path,
        '-map', '0:v:0', '-map', '0:a?',
        '-vf', "scale=w=1280:h=720:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30",
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
        '-profile:v', 'main', '-level', '3.1', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-ar', '44100', '-ac', '2',
        '-af', 'aresample=async=1:first_pts=0',
        '-avoid_negative_ts', 'make_zero', '-movflags', '+faststart', output_path,
    ]
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'FFmpeg 未提供錯誤訊息').strip()[-4000:]
        raise RuntimeError(f'FFmpeg 轉檔失敗（return code {result.returncode}）：{detail}')
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError('下載影片播放相容化失敗：輸出檔案不存在或為空')
    validation_command = [
        ffmpeg_path, '-v', 'error', '-xerror', '-i', output_path,
        '-map', '0:v:0', '-map', '0:a:0', '-f', 'null',
        'NUL' if os.name == 'nt' else '/dev/null',
    ]
    validation_result = subprocess.run(
        validation_command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    if validation_result.returncode != 0:
        detail = (validation_result.stderr or validation_result.stdout or 'FFmpeg 驗證未提供錯誤訊息').strip()[-4000:]
        raise RuntimeError(f'轉檔後驗證失敗（return code {validation_result.returncode}）：{detail}')

@app.route('/api/videos/ai-vocal-remove', methods=['POST'])
def ai_vocal_remove_video():
    """Separate an uploaded MP4 into one six-channel KTV audio stream."""
    global is_processing
    if is_processing:
        return json.dumps({'error': '目前已有其他製作或轉檔工作進行中，請稍候'}), 409
    video_file = request.files.get('video')
    if not video_file or not video_file.filename:
        return json.dumps({'error': '請選擇要處理的 MP4 影片'}), 400
    filename = os.path.basename(video_file.filename)
    if not filename.lower().endswith('.mp4'):
        return json.dumps({'error': '影片檔必須是 .mp4'}), 400

    job_dir = os.path.join(TEMP_BASE_DIR, f'ai_vocal_remove_{time.time_ns()}')
    source_path = os.path.join(job_dir, 'input.mp4')
    optimized_source_path = os.path.join(job_dir, 'input_optimized.mp4')
    output_path = os.path.join(job_dir, 'output.mp4')
    final_path = os.path.join(SONGS_DIR, filename)
    os.makedirs(job_dir, exist_ok=True)
    is_processing = True
    socketio.emit('task_status', {'status': 'busy'})
    try:
        video_file.save(source_path)
        broadcast_log(f'=== 開始 AI 去人聲：{filename} ===')
        ffmpeg_dir = get_ffmpeg_location()
        ffmpeg_path = os.path.join(ffmpeg_dir, 'ffmpeg.exe') if ffmpeg_dir else shutil.which('ffmpeg')
        if not ffmpeg_path:
            raise RuntimeError('找不到 FFmpeg')
        broadcast_log('⏳ 先轉換為 H.264 / 最高 720p / 30fps，降低播放負擔...')
        _optimize_downloaded_video(ffmpeg_path, source_path, optimized_source_path)
        shutil.move(optimized_source_path, source_path)
        os.environ['IANAUTOKTV_WORKER'] = '1'
        p = multiprocessing.Process(target=_run_spleeter_process, args=(source_path, job_dir))
        p.start()
        p.join()
        os.environ.pop('IANAUTOKTV_WORKER', None)
        if p.exitcode != 0:
            raise RuntimeError('Spleeter 分離失敗')
        stem_dir = _resolve_spleeter_stem_dir(job_dir, source_path)
        vocal_path = os.path.join(stem_dir, 'vocals.wav')
        accompaniment_path = os.path.join(stem_dir, 'accompaniment.wav')
        if not os.path.exists(vocal_path):
            raise RuntimeError('找不到 Spleeter 產生的 vocals.wav')
        if not os.path.exists(accompaniment_path):
            _build_spleeter_accompaniment(ffmpeg_path, stem_dir, accompaniment_path)
        ffprobe_path = get_ffprobe_path(ffmpeg_path)
        if not ffprobe_path:
            raise RuntimeError('找不到 FFprobe，無法驗證六聲道輸出')
        _create_six_channel_mp4(
            ffmpeg_path, ffprobe_path, source_path, vocal_path, accompaniment_path,
            output_path, normalize_volume=True,
        )
        audio_exceptions = _validate_six_channel_audio_with_exceptions(
            ffmpeg_path, ffprobe_path, output_path,
        )
        shutil.move(output_path, final_path)
        for exception in audio_exceptions:
            broadcast_log(f'⚠️ 已標記特殊歌曲超標項目：{_format_audio_exception(exception)}')
        broadcast_log(f'✅ AI 去人聲完成：{filename}（六聲道：原聲 / 導唱 / 伴奏）')
        socketio.emit('refresh_list')
        return json.dumps({'success': True, 'filename': filename}, ensure_ascii=False)
    except (OSError, RuntimeError) as error:
        broadcast_log(f'❌ AI 去人聲失敗：{error}')
        return json.dumps({'error': str(error)}, ensure_ascii=False), 400
    finally:
        is_processing = False
        socketio.emit('task_status', {'status': 'idle'})
        shutil.rmtree(job_dir, ignore_errors=True)

@app.route('/api/videos/normalize-audio', methods=['POST'])
def normalize_video_audio():
    """Normalize an uploaded MP4 audio track to the shared KTV loudness target."""
    global is_processing
    if is_processing:
        return json.dumps({'error': '目前已有其他製作或轉檔工作進行中，請稍候'}), 409
    video_file = request.files.get('video')
    if not video_file or not video_file.filename:
        return json.dumps({'error': '請選擇要處理的 MP4 影片'}), 400
    filename = os.path.basename(video_file.filename)
    if not filename.lower().endswith('.mp4'):
        return json.dumps({'error': '影片檔必須是 .mp4'}), 400

    job_dir = os.path.join(TEMP_BASE_DIR, f'normalize_audio_{time.time_ns()}')
    source_path = os.path.join(job_dir, 'input.mp4')
    output_path = os.path.join(job_dir, 'output.mp4')
    final_path = os.path.join(SONGS_DIR, filename)
    os.makedirs(job_dir, exist_ok=True)
    is_processing = True
    socketio.emit('task_status', {'status': 'busy'})
    try:
        video_file.save(source_path)
        broadcast_log(f'=== 開始平衡音量：{filename} ===')
        ffmpeg_dir = get_ffmpeg_location()
        ffmpeg_path = os.path.join(ffmpeg_dir, 'ffmpeg.exe') if ffmpeg_dir else shutil.which('ffmpeg')
        if not ffmpeg_path:
            raise RuntimeError('找不到 FFmpeg')
        ffprobe_path = get_ffprobe_path(ffmpeg_path)
        if get_audio_channel_count_from_path(source_path, ffprobe_path) >= 6:
            balanced_source = source_path
            balanced_path = os.path.join(job_dir, 'balanced.mp4')
            # 平衡流程已包含動態處理與峰值保護；只允許一次 AAC 輸出，避免重編碼再次推高峰值。
            _balance_six_channel_loudness(ffmpeg_path, balanced_source, balanced_path)
            balanced_source = balanced_path
            _validate_six_channel_audio_with_exceptions(ffmpeg_path, ffprobe_path, balanced_source)
            shutil.move(balanced_source, final_path)
            broadcast_log(f'✅ 六聲道音量平衡完成：{filename}')
            socketio.emit('refresh_list')
            return json.dumps({'success': True, 'filename': filename}, ensure_ascii=False)
        command = [
            ffmpeg_path, '-y', '-i', source_path,
            '-map', '0:v:0', '-map', '0:a?',
            '-c:v', 'copy', '-af', 'loudnorm=I=-14:TP=-1:LRA=11',
            '-c:a', 'aac', '-movflags', '+faststart', output_path,
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=600,
            encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode != 0 or not os.path.exists(output_path):
            detail = (result.stderr or result.stdout or 'FFmpeg 未提供錯誤訊息').strip()[-2000:]
            raise RuntimeError(f'FFmpeg 音量平衡失敗：{detail}')
        validation = subprocess.run(
            [ffmpeg_path, '-v', 'error', '-xerror', '-i', output_path,
             '-map', '0:v:0', '-map', '0:a:0', '-f', 'null',
             'NUL' if os.name == 'nt' else '/dev/null'],
            check=False, capture_output=True, text=True, timeout=600,
            encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if validation.returncode != 0:
            raise RuntimeError('FFmpeg 完整解碼驗證失敗')
        shutil.move(output_path, final_path)
        broadcast_log(f'✅ 音量平衡完成：{filename}')
        socketio.emit('refresh_list')
        return json.dumps({'success': True, 'filename': filename}, ensure_ascii=False)
    except (OSError, RuntimeError) as error:
        broadcast_log(f'❌ 音量平衡失敗：{error}')
        return json.dumps({'error': str(error)}, ensure_ascii=False), 400
    finally:
        is_processing = False
        socketio.emit('task_status', {'status': 'idle'})
        shutil.rmtree(job_dir, ignore_errors=True)

@app.route('/api/videos/normalize-audio-batch', methods=['POST'])
def normalize_videos_audio_batch():
    """Balance a selected folder of MP4 songs sequentially with the same single-file rules."""
    global is_processing
    if is_processing:
        return json.dumps({'error': '目前已有其他製作或轉檔工作進行中，請稍候'}), 409
    video_files = [file for file in request.files.getlist('videos') if file and file.filename]
    video_files = [file for file in video_files if os.path.basename(file.filename).lower().endswith('.mp4')]
    if not video_files:
        return json.dumps({'error': '請選擇含有 MP4 的歌曲資料夾'}), 400

    ffmpeg_dir = get_ffmpeg_location()
    ffmpeg_path = os.path.join(ffmpeg_dir, 'ffmpeg.exe') if ffmpeg_dir else shutil.which('ffmpeg')
    ffprobe_path = get_ffprobe_path(ffmpeg_path) if ffmpeg_path else None
    if not ffmpeg_path or not ffprobe_path:
        return json.dumps({'error': '找不到 FFmpeg 或 FFprobe'}), 500

    job_dir = os.path.join(TEMP_BASE_DIR, f'normalize_audio_batch_{time.time_ns()}')
    os.makedirs(job_dir, exist_ok=True)
    is_processing = True
    socketio.emit('task_status', {'status': 'busy', 'batch': True, 'total': len(video_files)})
    successes = []
    failures = []
    try:
        broadcast_log(f'=== 開始批次平衡音量：共 {len(video_files)} 首 ===')
        for index, video_file in enumerate(video_files, start=1):
            filename = os.path.basename(video_file.filename)
            source_path = os.path.join(job_dir, f'{index}_input.mp4')
            output_path = os.path.join(job_dir, f'{index}_balanced.mp4')
            final_path = os.path.join(SONGS_DIR, filename)
            try:
                video_file.save(source_path)
                broadcast_log(f'=== 批次音量平衡 {index}/{len(video_files)}：{filename} ===')
                if get_audio_channel_count_from_path(source_path, ffprobe_path) >= 6:
                    _balance_six_channel_loudness(ffmpeg_path, source_path, output_path)
                    _validate_six_channel_audio_with_exceptions(ffmpeg_path, ffprobe_path, output_path)
                else:
                    result = subprocess.run(
                        [ffmpeg_path, '-y', '-i', source_path, '-map', '0:v:0', '-map', '0:a?',
                         '-c:v', 'copy', '-af', 'loudnorm=I=-14:TP=-1:LRA=11',
                         '-c:a', 'aac', '-movflags', '+faststart', output_path],
                        check=False, capture_output=True, text=True, timeout=600,
                        encoding='utf-8', errors='replace',
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                    )
                    if result.returncode != 0 or not os.path.exists(output_path):
                        detail = (result.stderr or result.stdout or 'FFmpeg 未提供錯誤訊息').strip()[-2000:]
                        raise RuntimeError(f'FFmpeg 音量平衡失敗：{detail}')
                    validation = subprocess.run(
                        [ffmpeg_path, '-v', 'error', '-xerror', '-i', output_path,
                         '-map', '0:v:0', '-map', '0:a:0', '-f', 'null',
                         'NUL' if os.name == 'nt' else '/dev/null'],
                        check=False, capture_output=True, text=True, timeout=600,
                        encoding='utf-8', errors='replace',
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                    )
                    if validation.returncode != 0:
                        raise RuntimeError('FFmpeg 完整解碼驗證失敗')
                os.replace(output_path, final_path)
                successes.append(filename)
                broadcast_log(f'✅ 批次音量平衡完成：{filename}')
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                failures.append({'filename': filename, 'error': str(error)})
                broadcast_log(f'❌ 批次音量平衡失敗：{filename}：{error}')
        socketio.emit('refresh_list')
        broadcast_log(f'=== 批次音量平衡完成：成功 {len(successes)} 首，失敗 {len(failures)} 首 ===')
        return json.dumps({
            'success': not failures,
            'success_count': len(successes),
            'failure_count': len(failures),
            'successes': successes,
            'failures': failures,
        }, ensure_ascii=False)
    finally:
        is_processing = False
        socketio.emit('task_status', {'status': 'idle', 'batch': True})
        shutil.rmtree(job_dir, ignore_errors=True)

def save_subtitle(song_filename, content, subtitle_extension):
    """Convert subtitle text and save it as the matching song's WebVTT file."""
    song_path = os.path.join(SONGS_DIR, os.path.basename(song_filename))
    converted_content = convert_to_webvtt(content, subtitle_extension, get_media_duration(song_path))
    output_name = os.path.splitext(song_filename)[0] + '.vtt'
    with open(os.path.join(SONGS_DIR, output_name), 'w', encoding='utf-8', newline='\n') as output_file:
        output_file.write(converted_content)
    return output_name

def convert_to_webvtt(content, subtitle_extension, duration=None):
    """Convert timed or plain lyrics into browser-compatible WebVTT text."""
    if subtitle_extension == 'srt':
        return srt_to_webvtt(content)
    if subtitle_extension == 'lrc':
        return lrc_to_webvtt(content)
    if subtitle_extension == 'plain':
        return plain_lyrics_to_webvtt(content, duration)
    if not content.lstrip().startswith('WEBVTT'):
        return 'WEBVTT\n\n' + content
    return content

def detect_subtitle_format(content, extension=''):
    """Detect a subtitle format from content, using the extension only as fallback."""
    normalized = content.strip()
    if re.match(r'^WEBVTT(?:\s|$)', normalized, re.IGNORECASE):
        return 'vtt'
    if re.search(r'^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]', normalized, re.MULTILINE):
        return 'lrc'
    if re.search(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->', normalized):
        return 'srt'
    if extension in SUBTITLE_EXTENSIONS:
        return extension
    if normalized:
        return 'plain'
    raise ValueError('歌詞內容不可為空白。')

def srt_to_webvtt(content):
    """Convert SRT timestamp separators to the WebVTT format."""
    lines = content.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    converted = ['WEBVTT', '']
    for line in lines:
        if re.match(r'^\s*\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}', line):
            line = line.replace(',', '.')
        converted.append(line)
    return '\n'.join(converted)

def lrc_to_webvtt(content):
    """Convert LRC minute-second tags into one WebVTT cue per timestamp."""
    lines = content.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    converted = ['WEBVTT', '']
    timestamp_pattern = re.compile(r'\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\](.*)')
    cues = []
    for line in lines:
        match = timestamp_pattern.match(line.strip())
        if not match:
            continue
        minutes, seconds, fraction, text = match.groups()
        milliseconds = int((fraction or '0').ljust(3, '0')[:3])
        start_ms = (int(minutes) * 60 + int(seconds)) * 1000 + milliseconds
        cues.append((start_ms, text.strip()))
    cues.sort(key=lambda cue: cue[0])
    for index, (start_ms, text) in enumerate(cues):
        end_ms = cues[index + 1][0] if index + 1 < len(cues) else start_ms + 4000
        if end_ms <= start_ms:
            end_ms = start_ms + 1000
        converted.extend([
            f'{format_vtt_time(start_ms)} --> {format_vtt_time(end_ms)}',
            text,
            '',
        ])
    if not cues:
        raise ValueError('找不到有效的 LRC 時間標記')
    return '\n'.join(converted)

def plain_lyrics_to_webvtt(content, duration):
    """Create one automatically timed WebVTT cue for each plain lyric line."""
    if not duration or duration <= 0:
        raise ValueError('無法取得歌曲長度，無法自動安排普通歌詞時間')
    lines = [line.strip() for line in content.replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip()]
    if not lines:
        raise ValueError('歌詞內容不可為空白')
    lead_in = min(15, duration) if duration > 30 else 0
    lyric_duration = max(0.1, duration - 30) if duration > 30 else duration
    converted = ['WEBVTT', '']
    line_duration = round(lyric_duration / len(lines), 3)
    first_cue_end = lead_in + line_duration
    duration_ms = round(duration * 1000)
    for index, line in enumerate(lines):
        if index == 0:
            start_ms = 0
        else:
            start_ms = round((first_cue_end + (index - 1) * line_duration) * 1000)
        if index == len(lines) - 1:
            end_ms = duration_ms
        else:
            end_ms = round((first_cue_end + index * line_duration) * 1000)
        end_ms = min(end_ms, duration_ms)
        converted.extend([
            f'PLAIN_LYRICS_{index}_{index}',
            f'{format_vtt_time(start_ms)} --> {format_vtt_time(end_ms)}',
            line,
            '',
        ])
    return '\n'.join(converted)

def format_vtt_time(milliseconds):
    """Format milliseconds as a WebVTT HH:MM:SS.mmm timestamp."""
    total_seconds, millis = divmod(max(0, milliseconds), 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}'

# ------------------------------------------
# SocketIO 事件處理 & 待播清單
# ------------------------------------------
playlist_queue = []
# 待播歌曲備註獨立儲存，不把備註寫入歌曲或 queue 項目本身。
def _load_song_notes():
    """從 JSON 檔載入備註；檔案不存在或格式錯誤時使用空資料。"""
    try:
        with open(SONG_NOTES_FILE, 'r', encoding='utf-8') as notes_file:
            notes = json.load(notes_file)
        return notes if isinstance(notes, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _normalize_song_notes(notes):
    """已有歌詞的歌曲不再保留「想加入歌詞」選項，等同將該選項設為 False。"""
    changed = False
    for filename, note in notes.items():
        if not isinstance(note, dict) or not _song_has_subtitle(filename):
            continue
        selected_options = note.get('selected_options', [])
        if not isinstance(selected_options, list) or '想加入歌詞' not in selected_options:
            continue
        note['selected_options'] = [option for option in selected_options if option != '想加入歌詞']
        changed = True
    return changed


def _save_song_notes():
    """以暫存檔取代方式保存備註，避免寫檔中斷留下不完整 JSON。"""
    temporary_file = SONG_NOTES_FILE + '.tmp'
    with open(temporary_file, 'w', encoding='utf-8') as notes_file:
        json.dump(song_notes, notes_file, ensure_ascii=False, indent=2)
    os.replace(temporary_file, SONG_NOTES_FILE)


def _load_song_play_counts():
    """載入歌曲播放開始次數；檔案不存在或格式錯誤時從空計數開始。"""
    try:
        with open(SONG_PLAY_COUNTS_FILE, 'r', encoding='utf-8') as counts_file:
            counts = json.load(counts_file)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(counts, dict):
        return {}
    return {
        filename: count
        for filename, count in counts.items()
        if isinstance(filename, str) and os.path.basename(filename) == filename
        and isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }


def _save_song_play_counts():
    """以暫存檔原子取代播放次數資料，避免中斷時留下不完整 JSON。"""
    temporary_file = SONG_PLAY_COUNTS_FILE + '.tmp'
    with open(temporary_file, 'w', encoding='utf-8') as counts_file:
        json.dump(song_play_counts, counts_file, ensure_ascii=False, indent=2)
    os.replace(temporary_file, SONG_PLAY_COUNTS_FILE)


def _record_song_play_start(filename):
    """在歌曲開始播放時累加次數、保存並同步更新給點歌頁。"""
    safe_filename = os.path.basename(str(filename).strip())
    if not safe_filename or safe_filename != str(filename).strip():
        return
    song_play_counts[safe_filename] = song_play_counts.get(safe_filename, 0) + 1
    try:
        _save_song_play_counts()
    except OSError as error:
        print(f'保存歌曲播放次數失敗：{error}')
    emit('song_play_counts', dict(song_play_counts), broadcast=True)


song_notes = _load_song_notes()
if _normalize_song_notes(song_notes):
    _save_song_notes()
song_play_counts = _load_song_play_counts()
cloud_drive_state = {}


def get_room_google_drive_user_state():
    """Return Google Drive OAuth state isolated to this room and browser session."""
    global cloud_drive_state
    user_id = session.get('google_drive_user_id')
    if not isinstance(user_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', user_id):
        user_id = secrets.token_urlsafe(32)
        session['google_drive_user_id'] = user_id
    if not isinstance(cloud_drive_state, dict):
        cloud_drive_state = {}
    # Discard the old room-wide token shape; it must never become another user's account.
    if 'refresh_token' in cloud_drive_state:
        cloud_drive_state = {}
    state = cloud_drive_state.get(user_id)
    if not isinstance(state, dict):
        state = {}
        cloud_drive_state[user_id] = state
    return state
subtitle_mode = 0
subtitle_font_size = 100
subtitle_style_mode = 0
qr_visible = True
random_play_enabled = False
random_play_explicitly_enabled = False
playback_rate = 1.0
current_pitch = 0
current_track_mode = 'original'
current_vocal_level = 50
track_mode_request_id = 0
seek_offset = 0.0
seek_correction_enabled = False
last_user_action_time = 0.0
engine_debug_enabled = False

# Cloud room playback state is held in memory. Each room receives an isolated
# snapshot of the existing single-room KTV state; song media remains external.
CLOUD_ROOM_STATE_FIELDS = (
    'playlist_queue',
    'song_notes',
    'song_play_counts',
    'cloud_drive_state',
    'subtitle_mode',
    'subtitle_font_size',
    'subtitle_style_mode',
    'qr_visible',
    'random_play_enabled',
    'random_play_explicitly_enabled',
    'playback_rate',
    'current_pitch',
    'current_track_mode',
    'current_vocal_level',
    'track_mode_request_id',
    'seek_offset',
    'seek_correction_enabled',
    'last_user_action_time',
    'engine_debug_enabled',
)
CLOUD_ROOM_IDLE_TTL_SECONDS = 6 * 60 * 60
_cloud_rooms = {}
_cloud_rooms_lock = threading.RLock()
_cloud_socket_rooms = {}
_cloud_runtime_lock = threading.RLock()


def _snapshot_cloud_room_state():
    return {
        name: copy.deepcopy(globals()[name])
        for name in CLOUD_ROOM_STATE_FIELDS
    }


def _prune_cloud_rooms(now=None):
    now = time.time() if now is None else now
    expired = [
        code for code, room in _cloud_rooms.items()
        if not room['members'] and now - room['last_active'] > CLOUD_ROOM_IDLE_TTL_SECONDS
    ]
    for code in expired:
        _cloud_rooms.pop(code, None)


def create_cloud_room():
    """Create an isolated eight-digit numeric KTV room."""
    if not IANAUTOKTV_CLOUD_MODE:
        raise RuntimeError('Cloud rooms are available only in cloud mode.')
    with _cloud_rooms_lock:
        _prune_cloud_rooms()
        while True:
            code = str(secrets.randbelow(90_000_000) + 10_000_000)
            if code not in _cloud_rooms:
                break
        now = time.time()
        with _cloud_runtime_lock:
            initial_state = _snapshot_cloud_room_state()
        _cloud_rooms[code] = {
            'state': initial_state,
            'members': set(),
            'player_sid': None,
            'player_sids': set(),
            'created_at': now,
            'last_active': now,
        }
        return code


def get_cloud_room(code):
    if not isinstance(code, str) or not re.fullmatch(r'\d{8}', code):
        return None
    with _cloud_rooms_lock:
        _prune_cloud_rooms()
        room = _cloud_rooms.get(code)
        if room:
            room['last_active'] = time.time()
        return room


def close_cloud_room(code):
    with _cloud_rooms_lock:
        return _cloud_rooms.pop(str(code), None)


def push_cloud_room_state(code):
    """Load one room's globals for a synchronous Flask request."""
    room = get_cloud_room(code)
    if not room:
        return None
    _cloud_runtime_lock.acquire()
    previous = {name: globals()[name] for name in CLOUD_ROOM_STATE_FIELDS}
    for name, value in room['state'].items():
        globals()[name] = value
    token = _CURRENT_CLOUD_ROOM.set(code)
    return code, room, previous, token


def pop_cloud_room_state(context):
    """Save one room's globals and restore the process defaults."""
    if not context:
        return
    code, room, previous, token = context
    try:
        room['state'] = _snapshot_cloud_room_state()
        room['last_active'] = time.time()
        for name, value in previous.items():
            globals()[name] = value
        _CURRENT_CLOUD_ROOM.reset(token)
    finally:
        _cloud_runtime_lock.release()


def _wrap_cloud_room_handler(handler, event_name):
    from functools import wraps

    @wraps(handler)
    def scoped_handler(*args, **kwargs):
        if not IANAUTOKTV_CLOUD_MODE:
            return handler(*args, **kwargs)

        sid = request.sid
        if event_name == 'connect':
            room_code = session.get('room_code')
            session_role = session.get('room_role')
            socket_role = request.args.get('client_type', '').strip().lower() or session_role
            room = get_cloud_room(room_code)
            if (not room or session_role not in {'player', 'remote'}
                    or socket_role not in {'player', 'remote'}
                    or (socket_role == 'player' and session_role != 'player')):
                return False
            join_room(room_code)
            with _cloud_rooms_lock:
                room['members'].add(sid)
                room['last_active'] = time.time()
                if socket_role == 'player':
                    room['player_sid'] = sid
                    room.setdefault('player_sids', set()).add(sid)
                _cloud_socket_rooms[sid] = room_code
        else:
            room_code = _cloud_socket_rooms.get(sid) or session.get('room_code')
            room = get_cloud_room(room_code)
            if not room:
                return False
            player_sids = room.get('player_sids')
            if player_sids is None:
                player_sids = {room.get('player_sid')}
            if event_name == 'song_ended' and sid not in player_sids:
                return False

        context = push_cloud_room_state(room_code)
        if not context:
            return False
        try:
            return handler(*args, **kwargs)
        finally:
            pop_cloud_room_state(context)
            if event_name == 'disconnect':
                with _cloud_rooms_lock:
                    code = _cloud_socket_rooms.pop(sid, None)
                    disconnected_room = _cloud_rooms.get(code) if code else None
                    if disconnected_room:
                        disconnected_room['members'].discard(sid)
                        disconnected_room.get('player_sids', set()).discard(sid)
                        if disconnected_room['player_sid'] == sid:
                            disconnected_room['player_sid'] = next(
                                iter(disconnected_room.get('player_sids', set())), None,
                            )
                        disconnected_room['last_active'] = time.time()
                if code:
                    leave_room(code)

    return scoped_handler


_socketio_on_original = socketio.on


def _room_aware_socketio_on(event, namespace=None):
    decorator = _socketio_on_original(event, namespace=namespace)

    def register(handler):
        return decorator(_wrap_cloud_room_handler(handler, event))

    return register


socketio.on = _room_aware_socketio_on


def _guide_audio_control_state(filename):
    """Return synchronized guide-vocal controls and the song-specific guide mix gain."""
    ffmpeg_path = os.path.join(FFMPEG_DIR, 'ffmpeg.exe') if os.path.isdir(FFMPEG_DIR) else shutil.which('ffmpeg')
    song_path = _resolve_song_path(filename) if filename else ''
    metadata = _get_vocal_layout_metadata(ffmpeg_path, song_path)
    supports_vocal_control = metadata.startswith(VOCAL_STEM_LAYOUT_VERSION)
    return {
        'guide_vocal_control': supports_vocal_control,
        'vocal_level': current_vocal_level,
    }


def _format_queue_label(filename):
    """Return a compact title without extension for queue/history display."""
    if not filename:
        return ''
    return os.path.splitext(os.path.basename(filename))[0]


def _build_engine_status_history():
    """Build compact playback queue text for the status strip without A/B/C labels."""
    if not playlist_queue:
        return '播放清單：無'
    visible_items = [_format_queue_label(name) for name in playlist_queue[:3]]
    if len(playlist_queue) > 3:
        visible_items.append('...')
    return '播放清單：' + ' | '.join(visible_items)


def broadcast_engine_status_state():
    """Sync the single engine-debug toggle to all clients."""
    socketio.emit('engine_status_config', {
        'debug': engine_debug_enabled,
    })


"""
判斷是否允許在空閒狀態啟動隨機播放。
@returns {boolean} 當待播清單為空且近期沒有使用者手動點歌時，才允許自動補播。
"""
def can_start_random_song():
    """Return True only when the queue is empty and no recent user action should block auto-fill."""
    if not random_play_enabled or playlist_queue:
        return False
    return (time.monotonic() - last_user_action_time) >= 1.5


"""
以隨機方式補一首歌曲進入播放佇列。
@returns {boolean} 若成功補播則為 true，否則為 false。
"""
def start_random_song():
    """Append and start one random song when the playback queue is empty."""
    if not random_play_explicitly_enabled or not can_start_random_song():
        return False
    songs = [filename for filename in os.listdir(SONGS_DIR) if filename.lower().endswith('.mp4')]
    if not songs:
        return False
    filename = random.choice(songs)
    playlist_queue.append(filename)
    emit('update_queue', playlist_queue, broadcast=True)
    emit('queue_song_added', {'filename': filename}, broadcast=True)
    _record_song_play_start(filename)
    emit('play_video', _play_video_payload(filename), broadcast=True)
    broadcast_current_song()
    return True

def broadcast_current_song():
    """Broadcast the current song and its per-song subtitle presentation state to all clients."""
    filename = playlist_queue[0] if playlist_queue else ''
    effective_mode = _effective_subtitle_mode(filename)
    socketio.emit('current_song', {
        'filename': filename,
        'visible': effective_mode > 0,
        'plain_lyrics': _song_uses_plain_lyrics(filename),
        'subtitle_mode': effective_mode,
        'font_size': subtitle_font_size,
        'seek_offset': seek_offset,
        **_guide_audio_control_state(filename),
    })

@socketio.on('connect')
def handle_connect():
    """Send the current queue to each newly connected client."""
    current_filename = playlist_queue[0] if playlist_queue else ''
    emit('update_queue', playlist_queue)
    # 新連線先同步目前所有歌曲備註，讓遙控器與播放端畫面一致。
    emit('song_notes', song_notes)
    emit('song_play_counts', song_play_counts)
    effective_mode = _effective_subtitle_mode(current_filename)
    emit('current_song', {
        'filename': current_filename,
        'visible': effective_mode > 0,
        'plain_lyrics': _song_uses_plain_lyrics(current_filename),
        'subtitle_mode': effective_mode,
        'font_size': subtitle_font_size,
        'seek_offset': seek_offset,
        **_guide_audio_control_state(current_filename),
    })
    emit('subtitle_style_mode', {'mode': subtitle_style_mode})
    emit('qr_visibility', {'visible': qr_visible})
    emit('random_play', {'enabled': random_play_enabled})
    emit('seek_correction', {'enabled': seek_correction_enabled})
    emit('engine_status_config', {'debug': engine_debug_enabled})
    emit('apply_effect', {'playback_rate': playback_rate, 'pitch': current_pitch})
    emit('set_audio', {
        'mode': current_track_mode,
        **_guide_audio_control_state(current_filename),
    })
    emit('set_vocal_level', {'level': current_vocal_level})

@socketio.on('set_engine_debug')
def handle_set_engine_debug(data):
    """Toggle verbose engine diagnostics for the status strip; when off, show compact playback history."""
    global engine_debug_enabled
    engine_debug_enabled = bool(data.get('enabled')) if isinstance(data, dict) else False
    print(f'音訊引擎除錯資訊：{"開啟" if engine_debug_enabled else "關閉"}')
    broadcast_engine_status_state()

@socketio.on('request_engine_status_config')
def handle_request_engine_status_config():
    """Return the current engine-debug setting to a client after connection or reload."""
    emit('engine_status_config', {'debug': engine_debug_enabled})

@socketio.on('set_seek_correction')
def handle_set_seek_correction(data):
    """Update and broadcast whether single-video playback-position correction is enabled."""
    global seek_correction_enabled, seek_offset
    seek_correction_enabled = bool(data.get('enabled')) if isinstance(data, dict) else False
    if not seek_correction_enabled:
        seek_offset = 0.0
    socketio.emit('seek_correction', {'enabled': seek_correction_enabled})
    if not seek_correction_enabled:
        socketio.emit('seek_video', {'seconds': 0, 'offset': 0})

@socketio.on('set_qr_visibility')
def handle_qr_visibility(data):
    """Update and broadcast whether playback screens show the remote QR Code."""
    global qr_visible
    qr_visible = bool(data.get('visible')) if isinstance(data, dict) else True
    emit('qr_visibility', {'visible': qr_visible}, broadcast=True)

@socketio.on('set_random_play')
def handle_random_play(data):
    """Update and broadcast whether idle playback should choose random songs."""
    global random_play_enabled, random_play_explicitly_enabled
    # 啟用/停用「無點歌時隨機播歌」不是使用者手動點歌行為，
    # 因此不能更新 last_user_action_time，否則空隊列時會被 1.5 秒冷卻鎖住。
    random_play_enabled = bool(data.get('enabled')) if isinstance(data, dict) else False
    random_play_explicitly_enabled = random_play_enabled
    print(f"隨機播放設定：{'開啟' if random_play_enabled else '關閉'}")
    emit('random_play', {'enabled': random_play_enabled}, broadcast=True)
    if random_play_enabled and not playlist_queue and can_start_random_song():
        start_random_song()

"""
使用者手動點歌時，標記近期操作時間並避免隨機播放在同一時段插隊。
@param {object} data 點歌事件內容，包含 filename 欄位。
"""
@socketio.on('add_to_queue')
def handle_add_queue(data):
    """Append a user-selected song while blocking random fill for a short cooldown period."""
    global seek_offset, last_user_action_time
    filename = data['filename']
    last_user_action_time = time.monotonic()
    playlist_queue.append(filename)

    # 廣播更新所有設備上的歌單畫面
    emit('update_queue', playlist_queue, broadcast=True)
    emit('queue_song_added', {'filename': filename}, broadcast=True)

    # 如果清單裡面只有剛點的這首歌，代表目前沒有歌在播，立刻開始播放
    if len(playlist_queue) == 1:
        seek_offset = 0.0
        _record_song_play_start(filename)
        emit('play_video', _play_video_payload(filename), broadcast=True)
        broadcast_current_song()

@socketio.on('replay_current_song')
def handle_replay_current_song():
    """Insert the current song after itself, then cut to replay it immediately while preserving the active key."""
    if not playlist_queue:
        return
    filename = playlist_queue[0]
    playlist_queue.insert(1, filename)
    emit('queue_song_added', {'filename': filename}, broadcast=True)
    handle_song_ended(reset_pitch=False)

@socketio.on('set_subtitle_mode')
def handle_set_subtitle_mode(data):
    """Set the shared subtitle display mode for the song currently playing."""
    global subtitle_mode
    filename = os.path.basename(data.get('filename', '')) if isinstance(data, dict) else ''
    if not playlist_queue or filename != playlist_queue[0]:
        return
    if not _song_has_subtitle(filename):
        return
    try:
        requested_mode = int(data.get('mode')) if isinstance(data, dict) else -1
    except (TypeError, ValueError):
        return
    if requested_mode not in range(5):
        return
    subtitle_mode = requested_mode
    plain_lyrics = _song_uses_plain_lyrics(filename)
    effective_mode = _effective_subtitle_mode(filename)
    emit('subtitle_state', {
        'filename': filename,
        'visible': effective_mode > 0,
        'plain_lyrics': plain_lyrics,
        'subtitle_mode': effective_mode,
        'font_size': subtitle_font_size,
    }, broadcast=True)
    broadcast_current_song()

@socketio.on('set_subtitle_font_size')
def handle_set_subtitle_font_size(data):
    """Update and broadcast the shared subtitle font size in percent."""
    global subtitle_font_size
    try:
        requested_size = int(data.get('font_size', 120)) if isinstance(data, dict) else 120
    except (TypeError, ValueError):
        return
    subtitle_font_size = max(80, min(200, requested_size))
    subtitle_font_size = round(subtitle_font_size / 10) * 10
    subtitle_font_size = max(80, min(200, subtitle_font_size))
    current_filename = playlist_queue[0] if playlist_queue else ''
    effective_mode = _effective_subtitle_mode(current_filename)
    emit('subtitle_state', {
        'filename': current_filename,
        'visible': effective_mode > 0,
        'subtitle_mode': effective_mode,
        'font_size': subtitle_font_size,
    }, broadcast=True)
    broadcast_current_song()


@socketio.on('set_subtitle_style_mode')
def handle_set_subtitle_style_mode(data):
    """Update the shared subtitle background/shadow mode and broadcast it."""
    global subtitle_style_mode
    try:
        requested_mode = int(data.get('mode')) if isinstance(data, dict) else -1
    except (TypeError, ValueError):
        return
    if requested_mode not in range(2):
        return
    subtitle_style_mode = requested_mode
    emit('subtitle_style_mode', {'mode': subtitle_style_mode}, broadcast=True)

@socketio.on('remove_from_queue')
def handle_remove_from_queue(data):
    """Remove a queued song by index while protecting the currently playing song."""
    try:
        queue_index = int(data.get('index', -1))
    except (AttributeError, TypeError, ValueError):
        return
    if queue_index <= 0 or queue_index >= len(playlist_queue):
        return
    playlist_queue.pop(queue_index)
    emit('update_queue', playlist_queue, broadcast=True)

@socketio.on('song_note_submit')
def handle_song_note_submit(data):
    """Validate and save one note for a song currently present in the queue."""
    if not isinstance(data, dict):
        return
    song_filename = os.path.basename(str(data.get('song_filename', '')).strip())
    if not song_filename or song_filename not in playlist_queue:
        return

    allowed_options = {'想加入歌詞', '歌詞錯誤待修改'}
    selected_options = data.get('selected_options', [])
    if not isinstance(selected_options, list):
        selected_options = []
    selected_options = [option for option in selected_options if option in allowed_options]
    # 已有字幕的歌曲強制清除「想加入歌詞」，避免舊版 client 寫回錯誤狀態。
    if _song_has_subtitle(song_filename):
        selected_options = [option for option in selected_options if option != '想加入歌詞']

    allowed_keys = {'原 Key'}
    key_value = str(data.get('key_value', '原 Key')).strip()
    if key_value not in allowed_keys:
        try:
            key_number = max(-12, min(12, int(key_value)))
            key_value = '原 Key' if key_number == 0 else f'{key_number:+d}'
        except (TypeError, ValueError):
            key_value = '原 Key'
    if key_value not in allowed_keys and not re.fullmatch(r'[+-]\d+', key_value):
        key_value = '原 Key'

    custom_text = str(data.get('custom_text', '')).strip()[:500]
    note = {
        'song_filename': song_filename,
        'selected_options': selected_options,
        'key_value': key_value,
        'custom_text': custom_text,
        'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
    }
    # 備註以檔名為 key，獨立保存到 JSON，不改動既有歌曲資料結構。
    song_notes[song_filename] = note
    try:
        _save_song_notes()
    except OSError:
        return
    emit('song_note_updated', note, broadcast=True)

@socketio.on('song_ended')
def handle_song_ended(data=None, reset_pitch=True):
    """Advance the queue while preventing random idle fill from racing user-selected songs.
    Replay intentionally preserves the active pitch so the user keeps the same KEY when restarting the same song.
    """
    global seek_offset, last_user_action_time, current_pitch
    ended_filename = os.path.basename(str(data.get('filename', '')).strip()) if isinstance(data, dict) else ''
    if ended_filename and (not playlist_queue or ended_filename != playlist_queue[0]):
        return
    if len(playlist_queue) > 0:
        # 移除剛剛唱完的那首歌
        playlist_queue.pop(0)
        seek_offset = 0.0
        if reset_pitch:
            # 每首歌結束後只重設升降 KEY，其他播放設定維持原狀。
            current_pitch = 0
            socketio.emit('apply_effect', {'pitch': current_pitch})
        emit('update_queue', playlist_queue, broadcast=True)

        # 檢查是否還有下一首
        if len(playlist_queue) > 0:
            next_song = playlist_queue[0]
            _record_song_play_start(next_song)
            emit('play_video', _play_video_payload(next_song), broadcast=True)
            broadcast_current_song()
        else:
            print(
                f"歌曲播放結束：待播清單已空，隨機播放={'開啟' if random_play_enabled else '關閉'}，"
                f'明確啟用旗標={random_play_explicitly_enabled}'
            )
            if random_play_explicitly_enabled and can_start_random_song():
                if start_random_song():
                    return
            # 沒歌了，停止畫面並回到待機狀態
            emit('stop_video', broadcast=True)
            # 再同步一次最終空佇列，避免控制頁面保留上一首的「播放中」標籤。
            emit('update_queue', playlist_queue, broadcast=True)
            broadcast_current_song()

@socketio.on('control')
def handle_control(action):
    global current_pitch
    if action == 'cut':
        # 防呆：切歌後若隊列空了，仍要檢查「無點播時隨機播歌」設定，
        # 否則使用者勾選自動補播時，會因為漏判而直接停住不播下一首。
        if not playlist_queue:
            if random_play_explicitly_enabled and can_start_random_song():
                start_random_song()
            return
        current_filename = playlist_queue[0]
        emit('stop_video', {'filename': current_filename}, broadcast=True)

        if len(playlist_queue) > 1:
            playlist_queue.pop(0)
            current_pitch = 0
            socketio.emit('apply_effect', {'pitch': current_pitch})
            emit('update_queue', playlist_queue, broadcast=True)
            next_song = playlist_queue[0]
            _record_song_play_start(next_song)
            emit('play_video', _play_video_payload(next_song), broadcast=True)
            broadcast_current_song()
            return

        playlist_queue.pop(0)
        current_pitch = 0
        socketio.emit('apply_effect', {'pitch': current_pitch})
        emit('update_queue', playlist_queue, broadcast=True)
        if random_play_explicitly_enabled and can_start_random_song():
            if start_random_song():
                return
        broadcast_current_song()
        return
    else:
        # 其他指令 (例如 pause) 照常發送
        emit('command', action, broadcast=True)

@socketio.on('danmaku_submit')
def handle_danmaku_submit(data):
    """Validate and broadcast one temporary danmaku message to all playback screens."""
    if not isinstance(data, dict):
        return
    text = str(data.get('text', '')).strip()[:100]
    if not text:
        return
    # 彈幕是即時氣氛訊息，不寫入歌曲或備註檔案。
    emit('danmaku_show', {'text': text}, broadcast=True)

@socketio.on('sound_effect')
def handle_sound_effect(data):
    """Broadcast a supported short audience-reaction sound effect."""
    if not isinstance(data, dict):
        return
    effect = str(data.get('effect', '')).strip().lower()
    if effect not in {'clap'}:
        return
    emit('sound_effect', {'effect': effect}, broadcast=True)

@socketio.on('photo_submit')
def handle_photo_submit(data):
    """Validate and broadcast one temporary camera photo to playback screens."""
    if not isinstance(data, dict):
        return {'success': False, 'error': '照片資料格式錯誤'}
    image_data = str(data.get('data', '')).strip()
    match = re.fullmatch(r'data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)', image_data)
    if not match:
        return {'success': False, 'error': '照片格式不支援'}
    try:
        decoded = base64.b64decode(match.group(2), validate=True)
    except (ValueError, base64.binascii.Error):
        return {'success': False, 'error': '照片資料無效'}
    if not decoded or len(decoded) > 5 * 1024 * 1024:
        return {'success': False, 'error': '照片大小不可超過 5 MB'}
    emit('photo_show', {'data': image_data}, broadcast=True)
    return {'success': True}

@socketio.on('sticker_submit')
def handle_sticker_submit(data):
    """Broadcast one allowlisted static sticker filename to playback screens."""
    if not isinstance(data, dict):
        return {'success': False, 'error': '貼圖資料格式錯誤'}
    filename = str(data.get('filename', '')).strip()
    if not filename or filename != os.path.basename(filename):
        return {'success': False, 'error': '貼圖名稱無效'}
    extension = os.path.splitext(filename)[1].lower()
    sticker_path = os.path.join(STICKERS_DIR, filename)
    if extension not in STICKER_EXTENSIONS or not os.path.isfile(sticker_path):
        return {'success': False, 'error': '找不到支援的貼圖'}
    if os.path.getsize(sticker_path) > 2 * 1024 * 1024:
        return {'success': False, 'error': '貼圖大小不可超過 2 MB'}
    now = time.monotonic()
    with sticker_cooldown_lock:
        last_sent_at = sticker_last_sent_at.get(request.sid)
        remaining = STICKER_COOLDOWN_SECONDS - (now - last_sent_at) if last_sent_at is not None else 0
        if remaining > 0:
            return {
                'success': False,
                'error': '每 2 秒只能傳送一張貼圖。',
                'retry_after': remaining,
            }
        sticker_last_sent_at[request.sid] = now
    emit('sticker_show', {'filename': filename}, broadcast=True)
    return {'success': True}

@socketio.on('disconnect')
def handle_disconnect():
    with sticker_cooldown_lock:
        sticker_last_sent_at.pop(request.sid, None)

@socketio.on('seek_video')
def handle_seek_video(data):
    """Broadcast a bounded video-delay adjustment using the same behavior as the stable v1.0.6.8 flow."""
    global seek_offset
    try:
        seconds = float(data.get('seconds', 0)) if isinstance(data, dict) else 0
    except (TypeError, ValueError):
        return
    if seconds not in {-0.5, -0.1, 0, 0.1, 0.5}:
        return
    # 這裡不再用伺服器端的狀態旗標硬擋使用者指令，因為畫面進度修正的有效性
    # 已由前端與伺服器共同控制；維持 v1.0.6.8 的行為可避免在開啟功能後
    # 按下 [0.5>>] / [0.1>] 等按鈕完全無反應。
    seek_offset = 0.0 if seconds == 0 else round(seek_offset + seconds, 1)
    emit('seek_video', {'seconds': seconds, 'offset': seek_offset}, broadcast=True)

@socketio.on('seek_song')
def handle_seek_song(data):
    """驗證固定 10 秒的歌曲跳轉，並同步廣播目前播放曲目。"""
    if not isinstance(data, dict):
        return
    try:
        seconds = float(data.get('seconds'))
    except (TypeError, ValueError):
        return
    if seconds not in {-10.0, 10.0} or not playlist_queue or seek_correction_enabled:
        return
    emit('seek_song', {'seconds': seconds, 'filename': playlist_queue[0]}, broadcast=True)

# ------------------------------------------
# 音效與升降 KEY 控制（單一入口，避免重複事件註冊造成按鍵無反應）
# ------------------------------------------
@socketio.on('control_effect')
def handle_effect(data):
    """Normalize and broadcast audio control updates for volume, pitch, and playback rate."""
    global playback_rate, current_pitch
    if not isinstance(data, dict):
        return

    # 只接受已定義的播放速度範圍，避免非法值破壞播放邏輯。
    if 'playback_rate' in data:
        try:
            requested_rate = float(data['playback_rate'])
        except (TypeError, ValueError):
            return
        if requested_rate not in {0.75, 1.0, 1.25}:
            return
        playback_rate = requested_rate

    normalized_data = dict(data)
    if 'pitch' in data:
        try:
            requested_pitch = int(data['pitch'])
        except (TypeError, ValueError):
            return
        if requested_pitch < -12 or requested_pitch > 12:
            return
        current_pitch = requested_pitch
        normalized_data['pitch'] = current_pitch

    # 升降 KEY / 音量 / 速度都以同一個事件廣播，讓遙控器與播放器同步。
    emit('apply_effect', normalized_data, broadcast=True)

@socketio.on('change_track')
def handle_track(mode):
    """Switch the playback mode immediately and keep it active for all subsequent songs until changed again."""
    global current_track_mode, track_mode_request_id
    if mode not in {'original', 'guide', 'instrumental'}:
        return
    track_mode_request_id += 1
    request_id = track_mode_request_id
    current_track_mode = mode
    filename = playlist_queue[0] if playlist_queue else ''
    emit('set_audio', {
        'mode': mode,
        **_guide_audio_control_state(filename),
        'audio_loudness_lufs': None,
        'request_id': request_id,
    }, broadcast=True)
    if not filename:
        return

    def update_loudness():
        """Send the selected mode's LUFS only when the same mode request is still the newest."""
        loudness = get_audio_loudness(filename, mode)
        if not playlist_queue or playlist_queue[0] != filename:
            return
        if track_mode_request_id != request_id:
            return
        socketio.emit('set_audio', {
            'mode': mode,
            **_guide_audio_control_state(filename),
            'audio_loudness_lufs': loudness,
            'request_id': request_id,
        })

    socketio.start_background_task(update_loudness)


@socketio.on('change_vocal_level')
def handle_change_vocal_level(data):
    """Set the separated guide-vocal gain and synchronize all connected controls."""
    global current_vocal_level
    raw_level = data.get('level') if isinstance(data, dict) else data
    try:
        level = int(round(float(raw_level)))
    except (TypeError, ValueError):
        return
    current_vocal_level = max(0, min(100, level))
    emit('set_vocal_level', {'level': current_vocal_level}, broadcast=True)

is_processing = False


# ==========================================
# Spleeter 獨立進程處理函式
# ==========================================
def _resolve_spleeter_stem_dir(output_dir, input_path):
    """Return the actual Spleeter output directory for either 2-stem or legacy 4-stem separation."""
    input_name = os.path.splitext(os.path.basename(input_path))[0]
    candidates = [
        os.path.join(output_dir, input_name),
        output_dir,
    ]
    for candidate in candidates:
        vocals_path = os.path.join(candidate, 'vocals.wav')
        accompaniment_path = os.path.join(candidate, 'accompaniment.wav')
        if os.path.exists(vocals_path):
            return candidate
        if os.path.exists(accompaniment_path):
            return candidate
    return os.path.join(output_dir, input_name)


def _run_spleeter_process(input_path, output_dir):
    """
    這個函式會在一個完全獨立的 Python 進程中執行。
    結束時作業系統會強制清空此進程佔用的 TensorFlow 記憶體。
    """
    try:
        from spleeter.separator import Separator
        # 使用 2stems 直接生成 vocals/accompaniment，避免 4stems 合成伴奏時把原始人聲殘留混回伴奏。
        separator = Separator('spleeter:2stems')
        separator.separate_to_file(input_path, output_dir)
    except Exception:
        import traceback
        with open(os.path.join(output_dir, "spleeter_error.log"), "w", encoding="utf-8") as error_file:
            error_file.write(traceback.format_exc())
        raise

def _build_spleeter_accompaniment(ffmpeg_path, stem_dir, output_path):
    """Combine non-vocal Spleeter stems into the only accompaniment source."""
    stem_paths = [os.path.join(stem_dir, name) for name in ('drums.wav', 'bass.wav', 'other.wav')]
    if any(not os.path.exists(path) for path in stem_paths):
        raise RuntimeError('Spleeter 4-stems 分離失敗：缺少 drums、bass 或 other 音軌')
    filter_complex = (
        '[0:a][1:a][2:a]amix=inputs=3:duration=longest:dropout_transition=0:normalize=1,'
        'aresample=async=1,aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=stereo[accompaniment]'
    )
    result = subprocess.run(
        [ffmpeg_path, '-y', '-i', stem_paths[0], '-i', stem_paths[1], '-i', stem_paths[2],
         '-filter_complex', filter_complex, '-map', '[accompaniment]',
         '-map_metadata', '-1', '-c:a', 'pcm_s16le', output_path],
        check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=600,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    if result.returncode != 0 or not os.path.exists(output_path):
        detail = (result.stderr or result.stdout or 'FFmpeg 未提供錯誤訊息').strip()[-2000:]
        raise RuntimeError(f'伴奏合成失敗：{detail}')




@socketio.on('start_download')
def handle_start_download(data):
    global is_processing
    if IANAUTOKTV_CLOUD_MODE:
        return {'error': '雲端模式不提供新增或轉檔歌曲功能。'}
    if is_processing:
        broadcast_log("⚠️ 系統正在處理其他歌曲，請稍候。")
        return
    
    url = data.get('url')
    title = data.get('title')
    ai_engine = data.get('ai_engine', 'spleeter')
    normalize_volume = True
    if ai_engine not in ('spleeter', 'mdxnet'):
        broadcast_log(f"❌ 不支援的 AI 去人聲引擎：{ai_engine}")
        return
    if ai_engine == 'mdxnet':
        broadcast_log("❌ MDX-Net 尚未安裝，請先使用 Spleeter，或完成 ToDo.md 的 MDX-Net 測試階段。")
        return
    
    def run_process():
        global is_processing
        is_processing = True
        socketio.emit('task_status', {'status': 'busy'})
        
        processor = KTVProcessor(log_cb=broadcast_log)
        output_filename = processor.process_song(url, title, ai_engine, normalize_volume)
        
        if output_filename:
            socketio.emit('refresh_list')
        
        is_processing = False
        socketio.emit('task_status', {'status': 'idle'})

    broadcast_log("=== 開始新任務 ===")
    threading.Thread(target=run_process, daemon=True).start()

@socketio.on('start_batch_download')
def handle_start_batch_download(data):
    """Validate and process a batch of URL/title download jobs sequentially."""
    global is_processing
    if IANAUTOKTV_CLOUD_MODE:
        return {'error': '雲端模式不提供新增或轉檔歌曲功能。'}
    if is_processing:
        broadcast_log("⚠️ 系統正在處理其他歌曲，請稍候。")
        return
    jobs = data.get('jobs', []) if isinstance(data, dict) else []
    ai_engine = data.get('ai_engine', 'spleeter') if isinstance(data, dict) else 'spleeter'
    normalize_volume = True
    if ai_engine not in ('spleeter', 'mdxnet'):
        broadcast_log(f"❌ 不支援的 AI 去人聲引擎：{ai_engine}")
        return
    if ai_engine == 'mdxnet':
        broadcast_log("❌ MDX-Net 尚未安裝，請先使用 Spleeter，或完成 ToDo.md 的 MDX-Net 測試階段。")
        return
    valid_jobs = [
        {'url': str(job.get('url', '')).strip(), 'title': str(job.get('title', '')).strip()}
        for job in jobs if isinstance(job, dict)
    ]
    invalid_jobs = [index + 1 for index, job in enumerate(valid_jobs) if not job['url'] or not job['title']]
    if not valid_jobs or invalid_jobs:
        detail = f"第 {', '.join(map(str, invalid_jobs))} 筆缺少網址或歌名。" if invalid_jobs else "批次清單不可為空。"
        broadcast_log(f"❌ 批量新增格式錯誤：{detail}")
        return

    def run_batch_process():
        global is_processing
        is_processing = True
        socketio.emit('task_status', {'status': 'busy', 'batch': True, 'total': len(valid_jobs)})
        success_count = 0
        failures = []
        batch_started_at = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        try:
            processor = KTVProcessor(log_cb=broadcast_log)
            for index, job in enumerate(valid_jobs, start=1):
                broadcast_log(f"=== 批量任務 {index}/{len(valid_jobs)}：{job['title']} ===")
                output_filename = processor.process_song(job['url'], job['title'], ai_engine, normalize_volume)
                if output_filename:
                    success_count += 1
                    socketio.emit('refresh_list')
                else:
                    broadcast_log(f"⚠️ 批量任務 {index}/{len(valid_jobs)} 失敗，繼續處理下一首。")
                    failures.append({
                        'index': index,
                        'title': job['title'],
                        'url': job['url'],
                        'step': processor.last_failure.get('step', '未知步驟') if processor.last_failure else '未知步驟',
                        'error': processor.last_failure.get('error', '未提供錯誤詳情') if processor.last_failure else '未提供錯誤詳情',
                    })
            if failures:
                error_path = os.path.join(BASE_DIR, f'batch_{batch_started_at}.error')
                with open(error_path, 'w', encoding='utf-8', newline='\n') as error_file:
                    error_file.write(f'ianAutoKTV 批量新增失敗清單\n建立時間：{datetime.now().isoformat(timespec="seconds")}\n')
                    error_file.write(f'失敗數量：{len(failures)}\n\n')
                    for failure in failures:
                        error_file.write(f"[{failure['index']}] {failure['title']}\n")
                        error_file.write(f"可重試：{failure['url']} | {failure['title']}\n")
                        error_file.write(f"失敗步驟：{failure['step']}\n")
                        error_file.write(f"錯誤：{failure['error']}\n\n")
                broadcast_log(f'📄 失敗清單已儲存：{os.path.basename(error_path)}')
            broadcast_log(f"✅ 批量新增完成：成功 {success_count} 首，失敗 {len(valid_jobs) - success_count} 首。")
        finally:
            is_processing = False
            socketio.emit('task_status', {'status': 'idle', 'batch': True})

    broadcast_log(f"=== 開始批量新增：共 {len(valid_jobs)} 首 ===")
    threading.Thread(target=run_batch_process, daemon=True).start()

@socketio.on('update_ytdlp')
def handle_update_ytdlp():
    if IANAUTOKTV_CLOUD_MODE:
        return {'error': '雲端模式不提供 yt-dlp 更新功能。'}
    def run_update():
        socketio.emit('task_status', {'status': 'busy'})
        broadcast_log("開始更新 yt-dlp 核心...")
        try:
            cmd = get_ytdlp_command() + ["-U"]
            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            broadcast_log(result.stdout)
            if result.stderr: broadcast_log(result.stderr)
            broadcast_log("✅ yt-dlp 更新程序結束。")
        except Exception as e:
            broadcast_log(f"❌ 更新失敗: {str(e)}")
        finally:
            socketio.emit('task_status', {'status': 'idle'})

    threading.Thread(target=run_update, daemon=True).start()

def run_server_thread():
    try:
        print("準備啟動 Flask 伺服器...")
        
        # 【關鍵防護】強制關閉 Flask 雞婆的啟動橫幅 (Banner) 與日誌，從根本拔除報錯源頭
        import logging
        from flask import cli
        cli.show_server_banner = lambda *args, **kwargs: None  # 暴力閹割橫幅印出功能
        logging.getLogger('werkzeug').setLevel(logging.ERROR)  # 只允許印出重大錯誤
        
        cert_path, key_path = ensure_tls_certificate()
        print(f"HTTPS server starting on https://{LOCAL_IP}:{PORT}")
        socketio.run(app, host='0.0.0.0', port=PORT, debug=False,
                 allow_unsafe_werkzeug=True, ssl_context=(cert_path, key_path))
    except Exception as e:
        import traceback
        error_text = f"伺服器啟動失敗: {e}\n{traceback.format_exc()}"
        try:
            with open(SERVER_ERROR_LOG_PATH, 'a', encoding='utf-8') as error_file:
                error_file.write(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}]\n{error_text}")
        except OSError:
            pass
        print(error_text)

# ==========================================
# 核心處理類別
# ==========================================
class KTVProcessor:
    def __init__(self, log_cb):
        self.log = log_cb
        self.last_failure = None

    def sanitize_filename(self, name):
        return "".join([c for c in name if c not in r'\/:*?"<>|'])

    def process_song(self, url, manual_title, ai_engine='spleeter', normalize_volume=True):
        job_temp_dir = None
        current_step = '初始化'
        self.last_failure = None
        try:
            safe_title = self.sanitize_filename(manual_title)
            self.log(f"目標歌曲：{safe_title}")

            job_id = str(int(time.time()))
            job_temp_dir = os.path.join(TEMP_BASE_DIR, job_id)
            os.makedirs(job_temp_dir, exist_ok=True)

            temp_input = os.path.join(job_temp_dir, "input.mp4")
            temp_optimized = os.path.join(job_temp_dir, "input_optimized.mp4")
            temp_output = os.path.join(job_temp_dir, "output.mp4")

            current_step = '步驟 1/5 下載影片'
            self.log("步驟 1/5: 下載影片...")
            ffmpeg_location = get_ffmpeg_location()
            ffmpeg_path = os.path.join(ffmpeg_location, 'ffmpeg.exe') if ffmpeg_location else shutil.which('ffmpeg')
            if not ffmpeg_path:
                raise Exception("找不到 FFmpeg")

            format_candidates = [
                ('720p AVC 影像 + m4a 音訊', 'bestvideo[vcodec^=avc1][height<=720]+bestaudio[ext=m4a]'),
                ('720p AVC HLS 影像 + m4a 音訊', 'bestvideo[vcodec^=avc1][height<=720][protocol^=m3u8]+bestaudio[ext=m4a]'),
                ('480p AVC 影像 + m4a 音訊', 'bestvideo[vcodec^=avc1][height<=480]+bestaudio[ext=m4a]'),
                ('480p AVC HLS 影像 + m4a 音訊', 'bestvideo[vcodec^=avc1][height<=480][protocol^=m3u8]+bestaudio[ext=m4a]'),
                ('720p VP9 HLS 影像 + m4a 音訊', 'bestvideo[vcodec^=vp09][height<=720][protocol^=m3u8]+bestaudio[ext=m4a]'),
                ('720p AV1 影像 + m4a 音訊', 'bestvideo[vcodec^=av01][height<=720]+bestaudio[ext=m4a]'),
                ('720p MP4 progressive（影像與音訊合一）', 'best[ext=mp4][height<=720]'),
                ('其他 720p 分離影像與音訊格式', 'bestvideo[height<=720]+bestaudio'),
            ]
            download_errors = []
            selected_format_name = ''
            for format_index, (format_name, format_selector) in enumerate(format_candidates, start=1):
                current_step = f'步驟 1/5 下載影片（格式嘗試 {format_index}/{len(format_candidates)}）'
                self.log(f"步驟 1/5: 嘗試格式 {format_index}/{len(format_candidates)}：{format_name}")
                self.log(f"🔎 yt-dlp 格式選擇器：{format_selector}")
                cmd_dl = get_ytdlp_command() + ([
                    "--ffmpeg-location", ffmpeg_location
                ] if ffmpeg_location else []) + [
                    "--force-overwrites", "--no-playlist",
                    "--retries", "10", "--fragment-retries", "10",
                    "--extractor-retries", "3", "--file-access-retries", "3",
                    "--retry-sleep", "fragment:exp=1:10",
                    "--concurrent-fragments", "1", "--abort-on-unavailable-fragments",
                    "-f", format_selector, "-o", temp_input, url,
                ]
                download_result = subprocess.run(
                    cmd_dl, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    encoding='utf-8', errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                if download_result.returncode != 0:
                    detail = (download_result.stderr or download_result.stdout or 'yt-dlp 未提供錯誤訊息').strip()[-1200:]
                    self.log(f"⚠️ 格式失敗：{format_name}（return code {download_result.returncode}），準備換下一種格式。")
                    download_errors.append(f'格式 {format_name}（{format_selector}）：下載失敗（{download_result.returncode}）：{detail}')
                    for stale_path in (temp_input, temp_optimized):
                        if os.path.exists(stale_path):
                            os.remove(stale_path)
                    self.log(f"🧹 已清除 {format_name} 的下載半成品。")
                    continue
                try:
                    self.log(f"✅ 下載完成：{format_name}，開始轉檔與完整性驗證。")
                    current_step = f'步驟 2/5 轉換影片（格式嘗試 {format_index}/{len(format_candidates)}）'
                    self.log(f"步驟 2/5: 使用 {format_name} 轉換為 H.264 / 最高 720p / 30fps，降低播放負擔...")
                    _optimize_downloaded_video(ffmpeg_path, temp_input, temp_optimized)
                    selected_format_name = format_name
                    self.log(f"✅ 格式驗證成功：{format_name}，後續使用此影片進行 AI 去人聲。")
                    break
                except Exception as conversion_error:
                    self.log(f"⚠️ 格式驗證失敗：{format_name}，清除半成品並換下一種格式。")
                    download_errors.append(f'格式 {format_name}（{format_selector}）：{conversion_error}')
                    for stale_path in (temp_input, temp_optimized):
                        if os.path.exists(stale_path):
                            os.remove(stale_path)
                    self.log(f"🧹 已清除 {format_name} 的下載與轉檔半成品。")
            else:
                raise RuntimeError('所有下載格式均失敗：\n' + '\n'.join(download_errors)[-6000:])
            shutil.move(temp_optimized, temp_input)

            engine_names = {'spleeter': 'Spleeter', 'mdxnet': 'MDX-Net'}
            engine_name = engine_names.get(ai_engine)
            if engine_name is None:
                raise ValueError(f"不支援的 AI 去人聲引擎：{ai_engine}")
            current_step = f'步驟 3/5 AI 去人聲（{engine_name}）'
            self.log(f"步驟 3/5: 使用 {selected_format_name} 的有效影片進行 AI 去人聲 ({engine_name})... (這需要一點時間)")
            
            # 【終極修復】PyInstaller 打包後沒有 spleeter.exe 可用 subprocess 呼叫。
            # 改用 multiprocessing 開啟獨立 Python 子進程執行 API。
            # 效果與 CLI 完全相同：進程結束後，OS 會強制回收 TensorFlow 記憶體！
            import multiprocessing
            os.environ['IANAUTOKTV_WORKER'] = '1'
            p = multiprocessing.Process(target=_run_spleeter_process, args=(temp_input, job_temp_dir))
            p.start()
            p.join() # 等待進程執行完畢
            os.environ.pop('IANAUTOKTV_WORKER', None)
            
            if p.exitcode != 0:
                error_log = os.path.join(job_temp_dir, "spleeter_error.log")
                spleeter_detail = ''
                if os.path.exists(error_log):
                    with open(error_log, encoding="utf-8") as error_file:
                        spleeter_detail = error_file.read().strip()
                    self.log(spleeter_detail)
                detail = spleeter_detail[-4000:] if spleeter_detail else '未產生 Spleeter 錯誤日誌'
                raise Exception(f"Spleeter 分離失敗（Exit code: {p.exitcode}）：{detail}")
            
            stem_dir = _resolve_spleeter_stem_dir(job_temp_dir, temp_input)
            voc_path = os.path.join(stem_dir, "vocals.wav")
            acc_path = os.path.join(stem_dir, "accompaniment.wav")

            if not os.path.exists(voc_path):
                raise Exception("Spleeter 分離失敗，找不到 vocals.wav")
            if not os.path.exists(acc_path):
                _build_spleeter_accompaniment(ffmpeg_path, stem_dir, acc_path)

            current_step = '步驟 4/5 合成六聲道'
            self.log("步驟 4/5: 合成六聲道（原聲 / 導唱 / 伴奏）...")
            ffprobe_path = get_ffprobe_path(ffmpeg_path) if ffmpeg_path else None
            if not ffmpeg_path or not ffprobe_path:
                raise Exception("找不到 FFmpeg 或 FFprobe")
            _create_six_channel_mp4(
                ffmpeg_path, ffprobe_path, temp_input, voc_path, acc_path,
                temp_output, normalize_volume,
            )
            audio_exceptions = _validate_six_channel_audio_with_exceptions(
                ffmpeg_path, ffprobe_path, temp_output,
            )
            for exception in audio_exceptions:
                self.log(f'⚠️ 已標記特殊歌曲超標項目：{_format_audio_exception(exception)}')

            current_step = '步驟 5/5 儲存檔案'
            self.log(f"步驟 5/5: 儲存為 {safe_title}.mp4")
            final = os.path.join(SONGS_DIR, f"{safe_title}.mp4")

            # 重新批量新增相同歌名時，應優先覆蓋舊版本，避免 UI 還在播放舊的、未修正的 c2/c4 輸出。
            if os.path.exists(final):
                os.remove(final)

            shutil.move(temp_output, final)

            self.log("✅ 製作完成！已自動同步至歌單（六聲道：原聲 / 導唱 / 伴奏）。")
            return os.path.basename(final)

        except Exception as e:
            error_text = str(e)
            self.last_failure = {'step': current_step, 'error': error_text}
            self.log(f"❌ 執行失敗：{error_text}")
            return None
        finally:
            if job_temp_dir and os.path.exists(job_temp_dir):
                try:
                    shutil.rmtree(job_temp_dir, ignore_errors=True)
                except:
                    pass 

# ==========================================
# 本機 GUI 
# ==========================================
class ServerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"ianAutoKTV {APP_VERSION}")
        self.geometry("450x570") # 保留區網連線診斷與操作區
        self.configure(bg="#f4f4f9")
        
        tk.Label(self, text=f"🎤 KTV 系統運作中 {APP_VERSION}", font=("Microsoft JhengHei", 20, "bold"), fg="#4CAF50", bg="#f4f4f9").pack(pady=10)
        
        info_frame = tk.Frame(self, bg="white", bd=1, relief="solid")
        info_frame.pack(fill="x", padx=20, pady=5)
        
        self.create_clickable_link(info_frame, "📺 播放端 (電視用)", f"https://{LOCAL_IP}:{PORT}/player", "blue")
        self.create_clickable_link(info_frame, "📱 遙控端 (手機用)", f"https://{LOCAL_IP}:{PORT}/remote", "#d32f2f")
        self.create_clickable_link(info_frame, "🕹️ 一體機 (單機用)", f"https://{LOCAL_IP}:{PORT}/combo", "#9C27B0")
        self.create_clickable_link(info_frame, "⚙️ 管理端 (加歌用)", f"https://{LOCAL_IP}:{PORT}/admin", "#F57C00")
        # OAuth／Picker 僅允許 KTV 主機本機操作，因此使用 loopback 位址開啟 Google Drive 歌庫頁。
        self.create_clickable_link(info_frame, "☁ Google Drive 歌庫", f"https://127.0.0.1:{PORT}/drive-prototype", "#1565C0")

        stat_frame = tk.Frame(self, bg="#f4f4f9")
        stat_frame.pack(fill="x", padx=20, pady=5)
        
        self.lbl_count = tk.Label(stat_frame, text="總歌曲數: 載入中...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_count.pack(anchor="w")
        
        self.lbl_size = tk.Label(stat_frame, text="佔用空間: 載入中...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_size.pack(anchor="w", pady=5)

        self.lbl_server_status = tk.Label(
            stat_frame,
            text="區網連線狀態: 尚未檢查，請按下「允許區網連線」。",
            font=("Microsoft JhengHei", 11, "bold"),
            bg="#f4f4f9",
            justify="left",
            wraplength=400,
        )
        self.lbl_server_status.pack(anchor="w", pady=3)
        self.lan_access_button = tk.Button(
            stat_frame,
            text="允許區網連線",
            command=self.enable_lan_access,
            font=("Microsoft JhengHei", 10, "bold"),
            bg="#1976D2",
            fg="white",
            activebackground="#1565C0",
        )
        self.lan_access_button.pack(anchor="w", pady=4)

        self.seek_correction_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            self,
            text="啟用畫面進度調整（低效能電腦建議關閉）",
            variable=self.seek_correction_var,
            command=self.toggle_seek_correction,
            font=("Microsoft JhengHei", 10),
            bg="#f4f4f9",
            activebackground="#f4f4f9",
        ).pack(anchor="w", padx=20, pady=2)

        self.engine_debug_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            self,
            text="啟用音訊引擎除錯資訊",
            variable=self.engine_debug_var,
            command=self.toggle_engine_debug,
            font=("Microsoft JhengHei", 10),
            bg="#f4f4f9",
            activebackground="#f4f4f9",
        ).pack(anchor="w", padx=20, pady=2)

        # 增加一個實體的 GUI 日誌框，用來接聽攔截到的錯誤訊息
        self.log_txt = tk.Text(self, height=8, state="disabled", bg="#222", fg="#0f0", font=("Consolas", 9))
        self.log_txt.pack(fill="both", expand=True, padx=20, pady=10)
        self.update_stats()
        
        # 啟動背景佇列監聽器
        self.check_log_queue()

    def toggle_seek_correction(self):
        """Apply the server-side video progress correction setting."""
        global seek_correction_enabled
        # 依伺服器目前狀態反轉，避免 Tk Checkbutton 回呼讀到尚未更新的舊值。
        enabled = not seek_correction_enabled
        self.seek_correction_var.set(enabled)
        handle_set_seek_correction({'enabled': enabled})

    def toggle_engine_debug(self):
        """Apply the single server-side toggle: debug on shows detailed engine info; off shows playback history."""
        global engine_debug_enabled
        # 以伺服器目前狀態反轉，避免 Tk Checkbutton 回呼讀到尚未更新的舊值。
        enabled = not engine_debug_enabled
        self.engine_debug_var.set(enabled)
        handle_set_engine_debug({'enabled': enabled})

    def enable_lan_access(self):
        """請求 UAC 提權建立 Windows 防火牆入站規則。"""
        _log_lan_access('使用者按下「允許區網連線」。')
        self.lan_access_button.config(state="disabled", text="設定區網連線中...")
        self.lbl_server_status.config(text="正在設定防火牆並檢查區網連線，請稍候。", fg="#1976D2")
        consent = messagebox.askyesno(
            '需要系統管理員權限',
            '允許區網連線需要修改 Windows 防火牆規則。\n\n'
            '按下「是」後，Windows 會再顯示系統管理員權限確認視窗。\n'
            '請在 Windows 視窗中按「是」，程式才能允許其他裝置連線。',
            parent=self,
        )
        if not consent:
            _log_lan_access('使用者取消權限確認，未執行防火牆設定。')
            self.lbl_server_status.config(text='已取消區網連線設定。', fg="#C62828")
            self.lan_access_button.config(state="normal", text="允許區網連線")
            return
        _log_lan_access('使用者同意權限確認，準備呼叫 Windows 系統管理員程序。')

        def configure_lan_access():
            success, message = allow_lan_firewall_access()
            port_ready = _is_server_port_listening()
            lan_port_ready = _is_lan_port_listening() if port_ready else False
            firewall_ready = _has_firewall_rule() if lan_port_ready else False
            self.after(0, lambda: self._finish_lan_access(
                success and port_ready and lan_port_ready and firewall_ready,
                message,
                port_ready,
                lan_port_ready,
                firewall_ready,
            ))

        threading.Thread(target=configure_lan_access, daemon=True).start()

    def _finish_lan_access(self, success, message, port_ready, lan_port_ready, firewall_ready):
        """更新按鈕觸發的區網設定與驗證結果。"""
        _log_lan_access(
            f'區網連線設定完成：{"成功" if success else "失敗"}；'
            f'port={port_ready}, LAN={lan_port_ready}, firewall={firewall_ready}；{message}'
        )
        if success:
            status_text = f"✅ 區網連線已確認：{LOCAL_IP}:{PORT}"
            status_color = "#2E7D32"
        elif not port_ready:
            status_text = "❌ 伺服器尚未在 5000 port 監聽。"
            status_color = "#C62828"
        elif not lan_port_ready:
            status_text = f"❌ 無法透過區網介面連線：{LOCAL_IP}:{PORT}"
            status_color = "#C62828"
        else:
            status_text = f"⚠️ 防火牆設定未確認：{message}"
            status_color = "#C62828"
        self.lbl_server_status.config(text=status_text, fg=status_color)
        self.lan_access_button.config(state="normal", text="允許區網連線")

    def create_clickable_link(self, parent, text_prefix, url, color):
        frame = tk.Frame(parent, bg="white")
        frame.pack(pady=2, anchor="w", padx=10)
        tk.Label(frame, text=f"{text_prefix}: ", font=("Consolas", 11), bg="white").pack(side="left")
        link_lbl = tk.Label(frame, text=url, font=("Consolas", 11, "underline"), fg=color, bg="white", cursor="hand2")
        link_lbl.pack(side="left")
        link_lbl.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

    def update_stats(self):
        try:
            songs = [f for f in os.listdir(SONGS_DIR) if f.endswith('.mp4')]
            count = len(songs)
            total_size = sum(os.path.getsize(os.path.join(SONGS_DIR, f)) for f in songs)
            size_mb = total_size / (1024 * 1024)
            
            self.lbl_count.config(text=f"🎵 總歌曲數: {count} 首")
            self.lbl_size.config(text=f"💾 佔用空間: {size_mb:.2f} MB")
        except Exception as e:
            pass
        self.after(5000, self.update_stats)

    def check_log_queue(self):
        """每 100 毫秒檢查一次佇列，把背景的文字寫進 GUI 日誌框"""
        try:
            while not system_log_queue.empty():
                msg = system_log_queue.get_nowait()
                self.log_txt.config(state="normal")
                self.log_txt.insert("end", msg + "\n")
                self.log_txt.see("end")
                self.log_txt.config(state="disabled")
        except Exception:
            pass
        self.after(100, self.check_log_queue)


class StartupWindow(tk.Tk):
    """顯示伺服器與主畫面初始化期間的啟動畫面。"""
    def __init__(self):
        super().__init__()
        self.title(f"ianAutoKTV {APP_VERSION}")
        self.geometry("430x190")
        self.resizable(False, False)
        self.configure(bg="#f4f4f9")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        left = (self.winfo_screenwidth() - self.winfo_width()) // 2
        top = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{left}+{top}")

        tk.Label(
            self,
            text="ianAutoKTV",
            font=("Microsoft JhengHei", 22, "bold"),
            fg="#1976D2",
            bg="#f4f4f9",
        ).pack(pady=(24, 8))
        tk.Label(
            self,
            text="開啟 ianAutoKTV 中...",
            font=("Microsoft JhengHei", 14, "bold"),
            fg="#333333",
            bg="#f4f4f9",
        ).pack()
        self.status_label = tk.Label(
            self,
            text="正在準備 Flask 伺服器，請耐心等待。",
            font=("Microsoft JhengHei", 10),
            fg="#666666",
            bg="#f4f4f9",
        )
        self.status_label.pack(pady=(8, 20))

    def set_status(self, text):
        self.status_label.config(text=text)
        self.update_idletasks()

if __name__ == "__main__":
    multiprocessing.freeze_support()

if __name__ == "__main__" and os.environ.get('IANAUTOKTV_WORKER') != '1':
    startup_window = StartupWindow()
    startup_window.set_status("正在檢查是否已有其他 KTV Server...")
    # 先顯示等待畫面，再執行可能需要等待舊程序退出的防重檢查。
    startup_window.update()
    if not startup_window.winfo_exists():
        raise SystemExit(0)

    try:
        ensure_single_server_instance()
    except Exception as error:
        messagebox.showerror('啟動失敗', f'單一 Server 啟動保護失敗：{error}', parent=startup_window)
        startup_window.destroy()
        raise SystemExit(1)

    if get_ffmpeg_location() is None:
        startup_window.set_status("找不到 FFmpeg，程式無法啟動。")
        messagebox.showerror(
            "啟動失敗",
            "找不到 FFmpeg\n請將 ffmpeg 資料夾放在程式同一目錄",
            parent=startup_window,
        )
        startup_window.destroy()
    else:
        def launch_application():
            startup_window.set_status("正在啟動 Flask 伺服器，請耐心等待。")
            server_thread = threading.Thread(target=run_server_thread, daemon=True)
            server_thread.start()
            startup_window.update()
            app = ServerApp()
            startup_window.destroy()
            app.mainloop()

        startup_window.after(100, launch_application)
        startup_window.mainloop()
