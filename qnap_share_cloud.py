"""Read-only QNAP File Station share links for cloud KTV rooms."""

import hashlib
import errno
import json
import logging
import os
import re
import shutil
import ssl
import sqlite3
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager

from flask import jsonify, request, session

import google_drive_prototype as drive_core


logger = logging.getLogger(__name__)


SSID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SMARTSHARE_AJAX_URL = "https://www.myqnapcloud.com/ajax/share"
SMARTSHARE_PAGE_SIZE = 100
ITEMS_CACHE_TTL_SECONDS = 600
MAX_PASSWORD_LENGTH = 256
MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_MEDIA_BYTES = 1024 * 1024 * 1024
LIST_PAGE_SIZE = 500
SYNC_PAGE_SIZE = 20
REQUEST_TIMEOUT_SECONDS = 25
PAGE_REQUEST_TIMEOUT_SECONDS = 12
DOWNLOAD_TIMEOUT_SECONDS = 180
ENV_FETCH_TIMEOUT_SECONDS = 180
SYNC_PAGE_RETRIES = 2
SYNC_PAGE_DELAY_SECONDS = 0.3
SYNC_RETRY_BASE_DELAY_SECONDS = 1
SYNC_RETRY_MAX_DELAY_SECONDS = 8
SYNC_RECOVERY_ROUNDS = 2
SYNC_RECOVERY_PAGE_SIZE = 20
SYNC_RECOVERY_DELAY_SECONDS = 5
LIBRARY_INDEX_INIT_BUSY_TIMEOUT_MS = 1000
LIBRARY_INDEX_BUSY_TIMEOUT_MS = 30000
LIBRARY_INDEX_INIT_LOCK_TIMEOUT_SECONDS = 10
LIBRARY_INDEX_INIT_TIMEOUT_SECONDS = 60
DEFAULT_LIBRARY_SYNC_INTERVAL_SECONDS = 6 * 60 * 60
GAS_LIBRARY_PAGE_SIZE = 5000
GAS_LIBRARY_BATCH_SIZE = 2000
GAS_LIBRARY_REQUEST_TIMEOUT_SECONDS = 25
GAS_LIBRARY_RESPONSE_LIMIT_BYTES = 8 * 1024 * 1024
MAX_LIBRARY_RESOURCE_COUNT = 1_000_000
QNAP_CACHE_RESERVE_BYTES = 128 * 1024 * 1024
LYRIC_EXTENSIONS = (".vtt", ".srt", ".lrc")

# _ensure_room_nas_state may start a fetch while it is inspecting the cache
# under this condition. A re-entrant lock keeps that operation atomic without
# deadlocking when the fetch starter checks the same condition.
_env_share_lock = threading.Condition(threading.RLock())
_qnap_request_lock = threading.Lock()
_library_index_init_lock = threading.Lock()
_initialized_library_index_paths = set()
_env_share_cache = {}
_env_sync_timer = None
_env_sync_status = {
    "state": "idle",
    "phase": "idle",
    "loaded": 0,
    "total": 0,
    "error": "",
    "error_type": "",
    "started_at": None,
    "completed_at": None,
    "last_activity_at": None,
    "current_offset": 0,
    "request_started_at": None,
    "request_finished_at": None,
    "failed_offsets": [],
    "partial_error": "",
    "requests_started": 0,
    "requests_completed": 0,
    "requests_failed": 0,
    "active_requests": 0,
    "cache_stage": "",
}
ENV_LIBRARY_MANIFEST_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "drive_downloads",
    "qnap_shared",
)
ENV_LIBRARY_STATUS_PATH = os.path.join(
    ENV_LIBRARY_MANIFEST_DIR,
    "library-sync-status.json",
)
ENV_LIBRARY_INDEX_PATH = os.path.join(
    ENV_LIBRARY_MANIFEST_DIR,
    "library-index.sqlite3",
)


class QnapShareError(Exception):
    """A safe error that can be shown to a room member."""


def _set_library_cache_stage(stage):
    with _env_share_lock:
        if _env_sync_status.get("phase") != "loading_cache":
            return
        _env_sync_status["cache_stage"] = stage
        _env_sync_status["last_activity_at"] = time.time()
        _env_share_lock.notify_all()


def _open_library_index():
    _set_library_cache_stage("opening_database")
    os.makedirs(ENV_LIBRARY_MANIFEST_DIR, exist_ok=True)
    index_path = os.path.abspath(ENV_LIBRARY_INDEX_PATH)
    _set_library_cache_stage("connecting")
    connection = sqlite3.connect(
        index_path,
        timeout=LIBRARY_INDEX_INIT_BUSY_TIMEOUT_MS / 1000.0,
    )
    try:
        connection.execute(
            f"PRAGMA busy_timeout={LIBRARY_INDEX_INIT_BUSY_TIMEOUT_MS}"
        )
        if index_path in _initialized_library_index_paths:
            connection.row_factory = sqlite3.Row
        else:
            _set_library_cache_stage("waiting_initialization_lock")
            logger.warning(
                "QNAP SQLite cache initialization is waiting for its initialization lock."
            )
            acquired = _library_index_init_lock.acquire(
                timeout=LIBRARY_INDEX_INIT_LOCK_TIMEOUT_SECONDS
            )
            if not acquired:
                message = (
                    "Timed out waiting for the QNAP SQLite cache initialization lock "
                    f"after {LIBRARY_INDEX_INIT_LOCK_TIMEOUT_SECONDS} seconds."
                )
                logger.error(message)
                raise sqlite3.OperationalError(message)
            try:
                if index_path not in _initialized_library_index_paths:
                    _set_library_cache_stage("initializing_schema")
                    logger.warning("QNAP SQLite cache initialization started.")
                    _initialize_library_index(connection)
                    _initialized_library_index_paths.add(index_path)
                    _set_library_cache_stage("cache_ready")
                    logger.warning("QNAP SQLite cache initialization completed.")
                else:
                    connection.row_factory = sqlite3.Row
            finally:
                _library_index_init_lock.release()
        connection.execute(
            f"PRAGMA busy_timeout={LIBRARY_INDEX_BUSY_TIMEOUT_MS}"
        )
        return connection
    except Exception:
        connection.close()
        raise


def _enable_library_index_wal(connection):
    deadline = time.monotonic() + 30
    delay = 0.05
    while True:
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() == "wal":
                return
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() == "wal":
                return
            raise sqlite3.OperationalError(
                f"Unable to enable WAL journal mode (current mode: {mode})."
            )
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if ("locked" not in message and "busy" not in message) or time.monotonic() >= deadline:
                raise
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
            delay = min(delay * 2, 1)


def _initialize_library_index(connection):
    deadline = time.monotonic() + LIBRARY_INDEX_INIT_TIMEOUT_SECONDS
    timeout_message = (
        "QNAP SQLite cache initialization exceeded its "
        f"{LIBRARY_INDEX_INIT_TIMEOUT_SECONDS}-second deadline."
    )

    def check_deadline():
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError(timeout_message)

    connection.set_progress_handler(
        lambda: int(time.monotonic() >= deadline),
        1000,
    )
    try:
        initialized_connection = _initialize_library_index_schema(
            connection,
            check_deadline,
        )
        check_deadline()
        return initialized_connection
    except sqlite3.OperationalError as error:
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError(timeout_message) from error
        raise
    finally:
        connection.set_progress_handler(None, 0)


def _initialize_library_index_schema(connection, check_deadline):
    connection.row_factory = sqlite3.Row
    check_deadline()
    _set_library_cache_stage("enabling_wal")
    logger.warning("QNAP SQLite initialization phase=enable_wal.")
    _enable_library_index_wal(connection)
    check_deadline()
    _set_library_cache_stage("creating_core_schema")
    logger.warning("QNAP SQLite initialization phase=wal_ready.")
    connection.execute("PRAGMA synchronous=NORMAL")
    logger.warning("QNAP SQLite initialization phase=create_core_schema.")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS nas_library_items (
            share_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            filename TEXT NOT NULL,
            item_json TEXT NOT NULL,
            synced_at REAL NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            singer TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            song_stem TEXT NOT NULL DEFAULT '',
            extension TEXT NOT NULL DEFAULT '',
            title_length INTEGER NOT NULL DEFAULT 0,
            code INTEGER,
            is_song INTEGER NOT NULL DEFAULT 0,
            metadata_version INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (share_key, item_key)
        );
        CREATE INDEX IF NOT EXISTS idx_nas_library_filename
            ON nas_library_items (share_key, filename);
        CREATE TABLE IF NOT EXISTS nas_library_sync (
            share_key TEXT PRIMARY KEY,
            total INTEGER NOT NULL DEFAULT 0,
            loaded INTEGER NOT NULL DEFAULT 0,
            cursor INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'idle',
            updated_at REAL NOT NULL,
            state_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    sync_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(nas_library_sync)")
    }
    if "state_json" not in sync_columns:
        connection.execute(
            "ALTER TABLE nas_library_sync ADD COLUMN state_json TEXT NOT NULL DEFAULT '{}'"
        )
    item_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(nas_library_items)")
    }
    item_column_definitions = {
        "title": "TEXT NOT NULL DEFAULT ''",
        "singer": "TEXT NOT NULL DEFAULT ''",
        "language": "TEXT NOT NULL DEFAULT ''",
        "category": "TEXT NOT NULL DEFAULT ''",
        "song_stem": "TEXT NOT NULL DEFAULT ''",
        "extension": "TEXT NOT NULL DEFAULT ''",
        "title_length": "INTEGER NOT NULL DEFAULT 0",
        "code": "INTEGER",
        "is_song": "INTEGER NOT NULL DEFAULT 0",
        "metadata_version": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in item_column_definitions.items():
        if column not in item_columns:
            connection.execute(
                f"ALTER TABLE nas_library_items ADD COLUMN {column} {definition}"
            )
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_nas_library_singer
            ON nas_library_items (share_key, is_song, singer);
        CREATE INDEX IF NOT EXISTS idx_nas_library_language
            ON nas_library_items (share_key, is_song, language);
        CREATE INDEX IF NOT EXISTS idx_nas_library_category
            ON nas_library_items (share_key, is_song, category);
        CREATE INDEX IF NOT EXISTS idx_nas_library_stem
            ON nas_library_items (share_key, song_stem, extension);
        CREATE INDEX IF NOT EXISTS idx_nas_library_code
            ON nas_library_items (share_key, is_song, code);
        """
    )
    check_deadline()
    _set_library_cache_stage("creating_search_index")
    logger.warning("QNAP SQLite initialization phase=create_search_index.")
    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS nas_library_search
        USING fts5(share_key UNINDEXED, item_key UNINDEXED, filename,
                   tokenize='trigram')
        """
    )
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS nas_library_items_search_insert
        AFTER INSERT ON nas_library_items BEGIN
            INSERT INTO nas_library_search(rowid, share_key, item_key, filename)
            VALUES (new.rowid, new.share_key, new.item_key, new.filename);
        END;
        CREATE TRIGGER IF NOT EXISTS nas_library_items_search_update
        AFTER UPDATE ON nas_library_items BEGIN
            DELETE FROM nas_library_search WHERE rowid = old.rowid;
            INSERT INTO nas_library_search(rowid, share_key, item_key, filename)
            VALUES (new.rowid, new.share_key, new.item_key, new.filename);
        END;
        CREATE TRIGGER IF NOT EXISTS nas_library_items_search_delete
        AFTER DELETE ON nas_library_items BEGIN
            DELETE FROM nas_library_search WHERE rowid = old.rowid;
        END;
        """
    )
    check_deadline()
    _set_library_cache_stage("backfilling_metadata")
    logger.warning("QNAP SQLite initialization phase=backfill_metadata.")
    last_rowid = 0
    while True:
        check_deadline()
        legacy_rows = connection.execute(
            """
            SELECT rowid, filename, item_json
            FROM nas_library_items
            WHERE rowid > ? AND metadata_version < 1
            ORDER BY rowid
            LIMIT 500
            """,
            (last_rowid,),
        ).fetchall()
        if not legacy_rows:
            break
        for row in legacy_rows:
            check_deadline()
            try:
                item = json.loads(row["item_json"])
            except (TypeError, ValueError):
                item = {"filename": row["filename"]}
            fields = _library_search_fields(item)
            connection.execute(
                """
                UPDATE nas_library_items
                SET title=?, singer=?, language=?, category=?, song_stem=?,
                    extension=?, title_length=?, code=?, is_song=?,
                    metadata_version=1
                WHERE rowid=?
                """,
                (*fields, row["rowid"]),
            )
            last_rowid = row["rowid"]
    check_deadline()
    connection.commit()
    _set_library_cache_stage("cache_ready")
    logger.warning("QNAP SQLite initialization phase=ready.")
    return connection


@contextmanager
def _library_index():
    connection = _open_library_index()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _library_share_key(cache_key):
    base_url, ssid, _password = cache_key
    return f"{base_url}|{ssid}"


def _library_item_key(item, fallback_offset):
    for key in ("resource_id", "id", "fid", "filename", "name"):
        value = item.get(key) if isinstance(item, dict) else None
        if value not in (None, ""):
            return str(value)
    return f"offset:{fallback_offset}"


def _gas_library_configured():
    return bool(
        os.environ.get("KTV_SHEETS_SYNC_URL", "").strip()
        and os.environ.get("KTV_SHEETS_SYNC_TOKEN", "").strip()
    )


def _gas_library_request(payload):
    endpoint = os.environ.get("KTV_SHEETS_SYNC_URL", "").strip()
    token = os.environ.get("KTV_SHEETS_SYNC_TOKEN", "").strip()
    if not endpoint or not token:
        raise QnapShareError("Google 試算表 NAS 快照尚未設定。")
    request_payload = dict(payload)
    request_payload["token"] = token
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=GAS_LIBRARY_REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = response.read(GAS_LIBRARY_RESPONSE_LIMIT_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise QnapShareError("目前無法連線至 Google 試算表 NAS 快照服務。") from None
    if len(body) > GAS_LIBRARY_RESPONSE_LIMIT_BYTES:
        raise QnapShareError("Google 試算表 NAS 快照回應過大。")
    try:
        result = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise QnapShareError("Google 試算表 NAS 快照服務未回傳有效資料。") from None
    if not isinstance(result, dict) or not result.get("ok"):
        raise QnapShareError("Google 試算表 NAS 快照服務拒絕此請求。")
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _restore_gas_library_snapshot(cache_key):
    if not _gas_library_configured():
        return None
    base_url, ssid, password = cache_key

    share_key = _library_share_key(cache_key)
    first_page = _gas_library_request({
        "action": "nas_library_load",
        "share_key": share_key,
        "offset": 0,
        "limit": GAS_LIBRARY_PAGE_SIZE,
    })
    if not first_page.get("found"):
        return None
    try:
        total = int(first_page.get("total", 0))
        updated_at = float(first_page.get("updated_at", 0))
    except (TypeError, ValueError):
        raise QnapShareError("Google 試算表 NAS 快照中繼資料無效。") from None
    state_metadata = first_page.get("state")
    if (
        total < 0
        or total > MAX_LIBRARY_RESOURCE_COUNT
        or not isinstance(state_metadata, dict)
        or not state_metadata.get("folder_fid")
    ):
        raise QnapShareError("Google 試算表 NAS 快照格式無效。")
    snapshot_partial = state_metadata.get("snapshot_partial", False)
    if not isinstance(snapshot_partial, bool):
        raise QnapShareError("Google 試算表 NAS 快照狀態無效。")

    state = dict(state_metadata)
    state.update({
        "base_url": base_url,
        "ssid": ssid,
        "password": password,
        "_from_env": True,
        "_cached_items": [],
        "_cached_at": updated_at,
        "snapshot_partial": snapshot_partial,
    })
    offset = 0
    page = first_page
    while offset < total:
        records = page.get("items")
        if not isinstance(records, list) or not records:
            raise QnapShareError("Google 試算表 NAS 快照分頁不完整。")
        items = []
        for record in records:
            if not isinstance(record, dict):
                raise QnapShareError("Google 試算表 NAS 快照項目格式無效。")
            item_key = record.get("item_key")
            item = record.get("item")
            filename = item.get("filename") if isinstance(item, dict) else None
            if (
                not isinstance(item_key, str)
                or not item_key
                or len(item_key) > 512
                or not isinstance(filename, str)
                or not filename
                or len(filename) > 255
                or os.path.basename(filename.replace("\\", "/")) != filename
            ):
                raise QnapShareError("Google 試算表 NAS 快照包含無效項目。")
            cached_item = dict(item)
            cached_item["resource_id"] = item_key
            # 舊快照沒有來源位置時，依快照順序補回可用的分頁位置。
            if (
                _is_smartshare_url(base_url)
                and (
                    not isinstance(cached_item.get("library_offset"), int)
                    or isinstance(cached_item.get("library_offset"), bool)
                    or cached_item["library_offset"] < 0
                )
            ):
                cached_item["library_offset"] = offset + len(items)
            items.append(cached_item)

        next_offset = offset + len(items)
        if next_offset > total:
            raise QnapShareError("Google 試算表 NAS 快照分頁超出總筆數。")
        _write_library_index_batch(
            cache_key,
            items,
            next_offset,
            total,
            next_offset,
            "ready",
            state,
        )
        _update_env_sync_status(
            state="starting",
            phase="restoring",
            loaded=next_offset,
            total=total,
            current_offset=next_offset,
            last_activity_at=time.time(),
        )
        offset = next_offset
        if offset < total:
            page = _gas_library_request({
                "action": "nas_library_load",
                "share_key": share_key,
                "offset": offset,
                "limit": GAS_LIBRARY_PAGE_SIZE,
            })
            try:
                page_total = int(page.get("total", -1))
            except (TypeError, ValueError):
                page_total = -1
            if page.get("snapshot_id") != first_page.get("snapshot_id") or page_total != total:
                raise QnapShareError("Google 試算表 NAS 快照在讀取期間發生變更。")

    if total == 0:
        _write_library_index_batch(
            cache_key, [], 0, 0, 0, "ready", state
        )
    return {
        "state": state,
        "total": total,
        "updated_at": updated_at,
        "partial": snapshot_partial,
    }


def _gas_library_item_record(item, fallback_offset, item_key=None):
    item_key = item_key or _library_item_key(item, fallback_offset)
    snapshot_item = {
        key: item[key]
        for key in (
            "filename",
            "filesize",
            "isfolder",
            "resource_id",
            "id",
            "fid",
            "library_offset",
        )
        if key in item
    }
    return {"item_key": item_key, "item": snapshot_item}


def _begin_gas_library_snapshot(cache_key, snapshot_id):
    data = _gas_library_request({
        "action": "nas_library_begin",
        "share_key": _library_share_key(cache_key),
        "snapshot_id": snapshot_id,
    })
    if data.get("snapshot_id") != snapshot_id:
        raise QnapShareError("Google 試算表未能開始 NAS 快照。")


def _append_gas_library_snapshot(cache_key, snapshot_id, records):
    if not records:
        return
    data = _gas_library_request({
        "action": "nas_library_append",
        "share_key": _library_share_key(cache_key),
        "snapshot_id": snapshot_id,
        "items": records,
    })
    if int(data.get("appended", -1)) != len(records):
        raise QnapShareError("Google 試算表未能完整保存 NAS 快照分頁。")


def _commit_gas_library_snapshot(cache_key, snapshot_id, total, state):
    data = _gas_library_request({
        "action": "nas_library_commit",
        "share_key": _library_share_key(cache_key),
        "snapshot_id": snapshot_id,
        "total": int(total),
        "state": _library_state_metadata(state),
    })
    if data.get("snapshot_id") != snapshot_id:
        raise QnapShareError("Google 試算表未能完成 NAS 快照。")


def _persist_gas_library_snapshot(cache_key, state):
    if not _gas_library_configured():
        return False
    snapshot_id = uuid.uuid4().hex
    _begin_gas_library_snapshot(cache_key, snapshot_id)
    sync = _read_library_index_state(cache_key)
    total = int(sync.get("item_count") or 0) if sync else 0
    offset = 0
    while offset < total:
        records = _read_library_index_snapshot_page(
            cache_key,
            offset,
            GAS_LIBRARY_BATCH_SIZE,
        )
        if not records:
            raise QnapShareError("NAS SQLite 索引在保存快照時不完整。")
        _append_gas_library_snapshot(
            cache_key,
            snapshot_id,
            [
                _gas_library_item_record(
                    record["item"],
                    offset + index,
                    item_key=record["item_key"],
                )
                for index, record in enumerate(records)
            ],
        )
        offset += len(records)
    _commit_gas_library_snapshot(
        cache_key,
        snapshot_id,
        total,
        state,
    )
    return True


def _library_search_fields(item):
    filename = str(item.get("filename") or item.get("name") or "")
    extension = os.path.splitext(filename)[1].lower()
    stem = os.path.splitext(filename)[0]
    parts = stem.split("-")
    title = parts[0] if parts else stem
    singer = (parts[1] if len(parts) > 1 else "") or "未辨識"
    language = (parts[2] if len(parts) > 2 else "") or "未辨識"
    category = "-".join(parts[4:]) if len(parts) > 4 else "未標記"
    try:
        code = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
    except (TypeError, ValueError):
        code = None
    try:
        is_folder = int(item.get("isfolder", 0) or 0) != 0
    except (TypeError, ValueError):
        is_folder = True
    is_song = int(extension == ".mp4" and not is_folder)
    return (
        title,
        singer,
        language,
        category,
        stem.casefold(),
        extension,
        len(title),
        code,
        is_song,
    )


def _read_library_index_combined_page(
    cache_key,
    state,
    download_dir,
    local_songs,
    play_counts,
    keyword="",
    singer="",
    language="",
    categories=None,
    title_length=None,
    sort="newest",
    offset=0,
    limit=20,
):
    share_key = _library_share_key(cache_key)
    categories = [value for value in (categories or []) if value]
    normalized_play_counts = {
        str(filename).casefold(): int(count)
        for filename, count in (play_counts or {}).items()
        if isinstance(filename, str)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
    }
    normalized_local_songs = []
    seen_local_songs = set()
    for filename in local_songs or []:
        if not isinstance(filename, str):
            continue
        filename = os.path.basename(filename.strip())
        if not filename or os.path.splitext(filename)[1].lower() != ".mp4":
            continue
        key = filename.casefold()
        if key in seen_local_songs:
            continue
        seen_local_songs.add(key)
        fields = _library_search_fields({"filename": filename})
        normalized_local_songs.append((
            filename,
            *fields[:5],
            fields[6],
            fields[7],
            normalized_play_counts.get(key, 0),
        ))

    with _library_index() as connection:
        connection.execute(
            """
            CREATE TEMP TABLE combined_local_songs (
                filename TEXT NOT NULL,
                title TEXT NOT NULL,
                singer TEXT NOT NULL,
                language TEXT NOT NULL,
                category TEXT NOT NULL,
                song_stem TEXT NOT NULL,
                title_length INTEGER NOT NULL,
                code INTEGER,
                play_count INTEGER NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO combined_local_songs
                (filename, title, singer, language, category, song_stem,
                 title_length, code, play_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized_local_songs,
        )
        connection.execute(
            """
            CREATE INDEX combined_local_songs_filename_nocase
            ON combined_local_songs(filename COLLATE NOCASE)
            """
        )
        connection.execute(
            """
            CREATE TEMP TABLE combined_play_counts (
                filename TEXT PRIMARY KEY COLLATE NOCASE,
                play_count INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )
        connection.executemany(
            "INSERT INTO combined_play_counts (filename, play_count) VALUES (?, ?)",
            [
                (str(filename).casefold(), int(count))
                for filename, count in normalized_play_counts.items()
            ],
        )
        library_cte = """
            WITH library AS (
                SELECT 'nas' AS source, i.item_key AS source_id,
                       i.filename, i.item_json, i.song_stem, i.title, i.singer,
                       i.language, i.category, i.title_length, i.code,
                       COALESCE(p.play_count, 0) AS play_count,
                       i.rowid AS index_rowid
                FROM nas_library_items AS i
                LEFT JOIN combined_play_counts AS p
                    ON p.filename = i.filename
                WHERE i.share_key = ? AND i.is_song = 1
                    AND NOT EXISTS (
                        SELECT 1
                        FROM combined_local_songs AS l
                        WHERE l.filename = i.filename COLLATE NOCASE
                    )
                UNION ALL
                SELECT 'local', l.filename, l.filename, NULL, NULL,
                       l.title, l.singer, l.language, l.category,
                       l.title_length, l.code, l.play_count, NULL
                FROM combined_local_songs AS l
            )
        """

        def filter_sql(skip_field=None):
            conditions = []
            parameters = []
            for field, value in (
                ("singer", singer),
                ("language", language),
            ):
                if value and skip_field != field:
                    conditions.append(f"{field} = ?")
                    parameters.append(value)
            if categories and skip_field != "category":
                placeholders = ",".join("?" for _ in categories)
                conditions.append(f"category IN ({placeholders})")
                parameters.extend(categories)
            if title_length is not None and skip_field != "title_length":
                conditions.append("title_length = ?")
                parameters.append(int(title_length))
            if keyword:
                if len(keyword) >= 3:
                    conditions.append(
                        "("
                        "source = 'local' AND instr(lower(filename), lower(?)) > 0"
                        ") OR ("
                        "source = 'nas' AND index_rowid IN ("
                        "SELECT rowid FROM nas_library_search "
                        "WHERE share_key = ? AND nas_library_search MATCH ?"
                        "))"
                    )
                    parameters.extend((
                        keyword,
                        share_key,
                        '"' + keyword.replace('"', '""') + '"',
                    ))
                else:
                    conditions.append("instr(lower(filename), lower(?)) > 0")
                    parameters.append(keyword)
            return (" WHERE " + " AND ".join(f"({item})" for item in conditions)) if conditions else "", parameters

        order_by = {
            "newest": (
                "CASE WHEN code IS NULL THEN 0 ELSE 1 END DESC, "
                "code DESC, filename COLLATE NOCASE ASC, source ASC, source_id ASC"
            ),
            "popular": (
                "play_count DESC, CASE WHEN code IS NULL THEN 0 ELSE 1 END DESC, "
                "code DESC, filename COLLATE NOCASE ASC, source ASC, source_id ASC"
            ),
            "cold": (
                "play_count ASC, CASE WHEN code IS NULL THEN 0 ELSE 1 END DESC, "
                "code DESC, filename COLLATE NOCASE ASC, source ASC, source_id ASC"
            ),
        }.get(sort)
        if order_by is None:
            raise ValueError("合併曲庫排序方式無效。")

        where_sql, filter_parameters = filter_sql()
        total = connection.execute(
            library_cte + "SELECT COUNT(*) FROM library" + where_sql,
            [share_key, *filter_parameters],
        ).fetchone()[0]
        rows = connection.execute(
            library_cte
            + "SELECT source, source_id, filename, item_json, song_stem FROM library"
            + where_sql
            + f" ORDER BY {order_by} LIMIT ? OFFSET ?",
            [share_key, *filter_parameters, int(limit), int(offset)],
        ).fetchall()
        nas_stems = list(dict.fromkeys(
            row["song_stem"] for row in rows
            if row["source"] == "nas" and row["song_stem"]
        ))
        lyric_items = []
        if nas_stems:
            placeholders = ",".join("?" for _ in nas_stems)
            lyric_items = connection.execute(
                f"""
                SELECT item_json
                FROM nas_library_items
                WHERE share_key = ?
                    AND is_song = 0
                    AND extension IN ('.lrc', '.srt', '.vtt')
                    AND song_stem IN ({placeholders})
                """,
                [share_key, *nas_stems],
            ).fetchall()
        facets = {}
        for field in ("title_length", "singer", "language", "category"):
            facet_where, facet_parameters = filter_sql(field)
            facets[field] = [
                row[0]
                for row in connection.execute(
                    library_cte
                    + f"SELECT DISTINCT {field} FROM library"
                    + facet_where
                    + f" ORDER BY {field} COLLATE NOCASE",
                    [share_key, *facet_parameters],
                ).fetchall()
                if row[0] not in (None, "")
            ]

    nas_rows = [row for row in rows if row["source"] == "nas"]
    nas_items = [json.loads(row["item_json"]) for row in nas_rows]
    nas_items.extend(json.loads(row["item_json"]) for row in lyric_items)
    nas_songs, _lyrics = _songs_from_items(
        state,
        nas_items,
        download_dir,
    )
    nas_by_filename = {
        song["name"].casefold(): song
        for song in nas_songs
    }
    downloaded_qnap_filenames = {
        record["filename"].casefold()
        for record in drive_core.get_downloaded_song_records(download_dir)
        if record.get("file_id", "").startswith("qnap_")
    }
    songs = []
    for row in rows:
        if row["source"] == "local":
            songs.append({
                "id": f"local:{row['source_id']}",
                "name": row["filename"],
                "filename": row["filename"],
                "source": "local",
                "downloaded": True,
                # Preserve the shared NAS status when deduplication renders it as a local row.
                "downloaded_from_nas": row["filename"].casefold() in downloaded_qnap_filenames,
            })
        else:
            song = dict(nas_by_filename[row["filename"].casefold()])
            song["source"] = "nas"
            songs.append(song)
    return songs, int(total), facets


def _library_state_metadata(state):
    if not isinstance(state, dict):
        return {}
    return {
        key: value
        for key, value in state.items()
        if key not in {
            "password",
            "_from_env",
            "_cached_items",
            "_cached_at",
        }
    }


def _write_library_index_batch(
    cache_key, items, loaded, total, cursor, state, library_state=None
):
    share_key = _library_share_key(cache_key)
    now = time.time()
    state_json = json.dumps(
        _library_state_metadata(library_state),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with _library_index() as connection:
        connection.executemany(
            """
            INSERT INTO nas_library_items
                (share_key, item_key, filename, item_json, synced_at,
                 title, singer, language, category, song_stem, extension,
                 title_length, code, is_song, metadata_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(share_key, item_key) DO UPDATE SET
                filename=excluded.filename,
                item_json=excluded.item_json,
                synced_at=excluded.synced_at,
                title=excluded.title,
                singer=excluded.singer,
                language=excluded.language,
                category=excluded.category,
                song_stem=excluded.song_stem,
                extension=excluded.extension,
                title_length=excluded.title_length,
                code=excluded.code,
                is_song=excluded.is_song,
                metadata_version=excluded.metadata_version
            """,
            [
                (
                    share_key,
                    _library_item_key(item, index),
                    str(item.get("filename") or item.get("name") or ""),
                    json.dumps(item, ensure_ascii=False),
                    now,
                    *_library_search_fields(item),
                )
                for index, item in enumerate(items)
                if isinstance(item, dict)
            ],
        )
        connection.execute(
            """
            INSERT INTO nas_library_sync
                (share_key, total, loaded, cursor, state, updated_at, state_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(share_key) DO UPDATE SET
                total=excluded.total,
                loaded=excluded.loaded,
                cursor=excluded.cursor,
                state=excluded.state,
                updated_at=excluded.updated_at,
                state_json=CASE
                    WHEN excluded.state_json = '{}' THEN nas_library_sync.state_json
                    ELSE excluded.state_json
                END
            """,
            (
                share_key,
                int(total),
                int(loaded),
                int(cursor),
                state,
                now,
                state_json,
            ),
        )


def _read_library_index_page(cache_key, offset, limit):
    share_key = _library_share_key(cache_key)
    with _library_index() as connection:
        rows = connection.execute(
            """
            SELECT item_json
            FROM nas_library_items
            WHERE share_key = ?
            ORDER BY rowid
            LIMIT ? OFFSET ?
            """,
            (share_key, int(limit), int(offset)),
        ).fetchall()
        sync = connection.execute(
            """
            SELECT total, loaded, cursor, state, updated_at, state_json
            FROM nas_library_sync
            WHERE share_key = ?
            """,
            (share_key,),
        ).fetchone()
    sync_state = dict(sync) if sync else None
    if sync_state:
        try:
            sync_state["state_metadata"] = json.loads(sync_state.pop("state_json") or "{}")
        except (TypeError, ValueError):
            sync_state["state_metadata"] = {}
    return (
        [json.loads(row["item_json"]) for row in rows],
        sync_state,
    )


def _read_library_index_song_page(cache_key, offset, limit):
    share_key = _library_share_key(cache_key)
    with _library_index() as connection:
        total = connection.execute(
            """
            SELECT COUNT(*)
            FROM nas_library_items
            WHERE share_key = ? AND is_song = 1
            """,
            (share_key,),
        ).fetchone()[0]
        rows = connection.execute(
            """
            SELECT item_json, song_stem
            FROM nas_library_items
            WHERE share_key = ? AND is_song = 1
            ORDER BY rowid
            LIMIT ? OFFSET ?
            """,
            (share_key, int(limit), int(offset)),
        ).fetchall()
        stems = list(dict.fromkeys(row["song_stem"] for row in rows))
        lyric_items = []
        if stems:
            placeholders = ",".join("?" for _ in stems)
            lyric_items = connection.execute(
                f"""
                SELECT item_json
                FROM nas_library_items
                WHERE share_key = ?
                    AND is_song = 0
                    AND extension IN ('.lrc', '.srt', '.vtt')
                    AND song_stem IN ({placeholders})
                """,
                [share_key, *stems],
            ).fetchall()
        sync = connection.execute(
            """
            SELECT total, loaded, cursor, state, updated_at, state_json
            FROM nas_library_sync
            WHERE share_key = ?
            """,
            (share_key,),
        ).fetchone()
    sync_state = dict(sync) if sync else None
    if sync_state:
        try:
            sync_state["state_metadata"] = json.loads(sync_state.pop("state_json") or "{}")
        except (TypeError, ValueError):
            sync_state["state_metadata"] = {}
    items = [json.loads(row["item_json"]) for row in rows]
    items.extend(json.loads(row["item_json"]) for row in lyric_items)
    return items, int(total), sync_state


def _read_library_index_search(
    cache_key,
    keyword="",
    singer="",
    language="",
    category="",
    title_length=None,
    sort="newest",
    offset=0,
    limit=20,
):
    share_key = _library_share_key(cache_key)
    conditions = ["i.share_key = ?", "i.is_song = 1"]
    parameters = [share_key]
    if singer:
        conditions.append("i.singer = ?")
        parameters.append(singer)
    if language:
        conditions.append("i.language = ?")
        parameters.append(language)
    categories = (
        [category]
        if isinstance(category, str)
        else list(category or [])
    )
    categories = [value for value in categories if value]
    if categories:
        placeholders = ",".join("?" for _ in categories)
        conditions.append(f"i.category IN ({placeholders})")
        parameters.extend(categories)
    if title_length is not None:
        conditions.append("i.title_length = ?")
        parameters.append(int(title_length))
    if keyword:
        if len(keyword) >= 3:
            conditions.append(
                "i.rowid IN (SELECT rowid FROM nas_library_search "
                "WHERE nas_library_search MATCH ?)"
            )
            parameters.append('"' + keyword.replace('"', '""') + '"')
        else:
            conditions.append("instr(lower(i.filename), lower(?)) > 0")
            parameters.append(keyword)
    order_by = {
        "newest": (
            "CASE WHEN i.code IS NULL THEN 0 ELSE 1 END DESC, "
            "i.code DESC, i.filename COLLATE NOCASE ASC"
        ),
        "oldest": (
            "CASE WHEN i.code IS NULL THEN 0 ELSE 1 END DESC, "
            "i.code ASC, i.filename COLLATE NOCASE ASC"
        ),
        "title": "i.title COLLATE NOCASE ASC, i.singer COLLATE NOCASE ASC, i.filename",
        "singer": "i.singer COLLATE NOCASE ASC, i.title COLLATE NOCASE ASC, i.filename",
        "language": "i.language COLLATE NOCASE ASC, i.singer COLLATE NOCASE ASC, i.title COLLATE NOCASE ASC",
    }.get(sort)
    if order_by is None:
        raise ValueError("NAS 歌曲搜尋排序方式無效。")
    where_sql = " AND ".join(conditions)
    with _library_index() as connection:
        total = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM nas_library_items AS i
            WHERE {where_sql}
            """,
            parameters,
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT i.item_json, i.song_stem
            FROM nas_library_items AS i
            WHERE {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            [*parameters, int(limit), int(offset)],
        ).fetchall()
        stems = list(dict.fromkeys(row["song_stem"] for row in rows))
        lyric_items = []
        if stems:
            placeholders = ",".join("?" for _ in stems)
            lyric_items = connection.execute(
                f"""
                SELECT item_json
                FROM nas_library_items
                WHERE share_key = ?
                    AND is_song = 0
                    AND extension IN ('.lrc', '.srt', '.vtt')
                    AND song_stem IN ({placeholders})
                """,
                [share_key, *stems],
            ).fetchall()
        sync = connection.execute(
            """
            SELECT total, loaded, cursor, state, updated_at
            FROM nas_library_sync
            WHERE share_key = ?
            """,
            (share_key,),
        ).fetchone()
    return (
        [json.loads(row["item_json"]) for row in rows]
        + [json.loads(row["item_json"]) for row in lyric_items],
        int(total),
        dict(sync) if sync else None,
    )


def _read_library_index_state(cache_key):
    share_key = _library_share_key(cache_key)
    with _library_index() as connection:
        row = connection.execute(
            """
            SELECT total, loaded, cursor, state, updated_at, state_json,
                   (SELECT COUNT(*) FROM nas_library_items WHERE share_key = ?) AS item_count
            FROM nas_library_sync
            WHERE share_key = ?
            """,
            (share_key, share_key),
        ).fetchone()
    if not row:
        return None
    sync = dict(row)
    try:
        sync["state_metadata"] = json.loads(sync.pop("state_json") or "{}")
    except (TypeError, ValueError):
        sync["state_metadata"] = {}
    return sync


def _read_library_index_items(cache_key):
    share_key = _library_share_key(cache_key)
    with _library_index() as connection:
        rows = connection.execute(
            """
            SELECT item_json
            FROM nas_library_items
            WHERE share_key = ?
            ORDER BY rowid
            """,
            (share_key,),
        ).fetchall()
    return [json.loads(row["item_json"]) for row in rows]


def _read_library_index_snapshot_page(cache_key, offset, limit):
    share_key = _library_share_key(cache_key)
    with _library_index() as connection:
        rows = connection.execute(
            """
            SELECT item_key, item_json
            FROM nas_library_items
            WHERE share_key = ?
            ORDER BY rowid
            LIMIT ? OFFSET ?
            """,
            (share_key, int(limit), int(offset)),
        ).fetchall()
    return [
        {"item_key": row["item_key"], "item": json.loads(row["item_json"])}
        for row in rows
    ]


def _load_cached_env_library_state(cache_key):
    base_url, ssid, password = cache_key
    sync = _read_library_index_state(cache_key)
    if sync and sync.get("state_metadata"):
        state = dict(sync["state_metadata"])
        state.update({
            "base_url": base_url,
            "ssid": ssid,
            "password": password,
            "_from_env": True,
            "_cached_items": [],
            "_cached_at": sync["updated_at"],
        })
        return state

    manifest_state = _load_env_library_manifest(cache_key)
    if manifest_state is None:
        return None
    manifest_state.update({
        "base_url": base_url,
        "ssid": ssid,
        "password": password,
        "_from_env": True,
    })
    if sync and sync["item_count"]:
        manifest_state["_cached_items"] = []
        manifest_state["_cached_at"] = sync["updated_at"]
    return manifest_state


def _prune_library_index(cache_key, synced_after):
    share_key = _library_share_key(cache_key)
    with _library_index() as connection:
        connection.execute(
            "DELETE FROM nas_library_items WHERE share_key = ? AND synced_at < ?",
            (share_key, float(synced_after)),
        )


def _cache_limit_bytes():
    raw_value = os.environ.get("QNAP_SHARED_CACHE_MAX_BYTES", "0").strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _cache_usage(download_dir):
    total = 0
    records = drive_core.get_downloaded_song_records(download_dir)
    for record in records:
        for filename in (record.get("saved_as"), record.get("subtitle_saved_as")):
            if not isinstance(filename, str):
                continue
            path = os.path.join(download_dir, filename)
            try:
                total += os.path.getsize(path)
            except OSError:
                continue
    return total, records


def _prepare_cache_space(download_dir, required_bytes, reserved_bytes=0):
    """Evict the least recently used indexed NAS songs before a new download."""
    if required_bytes < 0 or reserved_bytes < 0:
        raise QnapShareError("歌曲大小無效，無法準備 Render 儲存空間。")
    os.makedirs(download_dir, exist_ok=True)
    usage, records = _cache_usage(download_dir)
    disk = shutil.disk_usage(download_dir)
    limit = _cache_limit_bytes()
    reserve = QNAP_CACHE_RESERVE_BYTES

    def enough():
        return (
            (not limit or usage + required_bytes + reserved_bytes <= limit)
            and disk.free >= required_bytes + reserved_bytes + reserve
        )

    if not enough():
        metadata = drive_core._load_download_metadata(download_dir)
        candidates = []
        for record in records:
            paths = [
                os.path.join(download_dir, name)
                for name in (record.get("saved_as"), record.get("subtitle_saved_as"))
                if isinstance(name, str)
            ]
            existing_paths = [path for path in paths if os.path.isfile(path)]
            if not existing_paths:
                continue
            last_used = max(os.path.getmtime(path) for path in existing_paths)
            candidates.append((last_used, record, existing_paths))
        candidates.sort(key=lambda item: item[0])

        for _last_used, record, paths in candidates:
            for path in paths:
                try:
                    usage -= os.path.getsize(path)
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
            metadata.pop(record.get("file_id"), None)
            disk = shutil.disk_usage(download_dir)
            if enough():
                drive_core._save_download_metadata(download_dir, metadata)
                return

        if metadata != drive_core._load_download_metadata(download_dir):
            drive_core._save_download_metadata(download_dir, metadata)
    if not enough():
        raise QnapShareError(
            "Render 儲存空間不足，已清理最舊歌曲但仍無法下載；請稍後重試或清理公共 NAS 快取。"
        )


def _storage_status(download_dir):
    os.makedirs(download_dir, exist_ok=True)
    usage, _records = _cache_usage(download_dir)
    disk = shutil.disk_usage(download_dir)
    limit = _cache_limit_bytes()
    return {
        "cache_type": "qnap_shared",
        "cache_path": os.path.relpath(download_dir, os.getcwd()),
        "used_bytes": usage,
        "limit_bytes": limit,
        "free_disk_bytes": disk.free,
        "total_disk_bytes": disk.total,
        "available_for_download_bytes": max(0, disk.free - QNAP_CACHE_RESERVE_BYTES),
    }


def _env_share_credentials():
    share_url = (
        os.environ.get("QNAP_SHARE_URL")
        or os.environ.get("NAS_SHARE_URL")
        or ""
    ).strip()
    password = (
        os.environ.get("QNAP_SHARE_PASSWORD")
        or os.environ.get("NAS_SHARE_PASSWORD")
        or ""
    )
    return share_url, password


def _env_library_manifest_path(cache_key):
    """Return a non-secret path for the last successful NAS library index."""
    base_url, ssid, _password = cache_key
    digest = hashlib.sha256(f"{base_url}\n{ssid}".encode("utf-8")).hexdigest()
    return os.path.join(ENV_LIBRARY_MANIFEST_DIR, f"library-{digest}.json")


def _load_env_library_manifest(cache_key):
    path = _env_library_manifest_path(cache_key)
    try:
        with open(path, "r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), list):
        return None
    state = manifest.get("state")
    if not isinstance(state, dict):
        return None
    state["_cached_items"] = manifest["items"]
    state["_cached_at"] = float(manifest.get("cached_at") or 0)
    return state


def _save_env_library_manifest(cache_key, state):
    manifest_state = {
        key: value
        for key, value in state.items()
        if key not in {"password", "_from_env"}
    }
    path = _env_library_manifest_path(cache_key)
    temporary_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temporary_path, "w", encoding="utf-8") as manifest_file:
            json.dump(
                {
                    "state": manifest_state,
                    "items": [],
                    "cached_at": state.get("_cached_at", 0),
                },
                manifest_file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        os.replace(temporary_path, path)
    except OSError:
        try:
            os.remove(temporary_path)
        except OSError:
            pass


def _is_smartshare_url(base_url):
    return base_url == SMARTSHARE_AJAX_URL


def _allowed_share_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("請輸入有效的 QNAP 分享連結。")
    parsed = urllib.parse.urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not (host == "myqnapcloud.com" or host.endswith(".myqnapcloud.com"))
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("請使用 HTTPS 的 QNAP myQNAPcloud 分享網址（支援 SmartShare 與 share.cgi）。")

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0].lower() in ("smartshare", "share"):
        composed_id = path_parts[1]
        if not SSID_PATTERN.fullmatch(composed_id):
            raise ValueError("SmartShare 分享網址中找不到有效的分享代碼。")
        return SMARTSHARE_AJAX_URL, composed_id

    if parsed.path.rstrip("/").lower() != "/share.cgi" or host in ("myqnapcloud.com", "www.myqnapcloud.com"):
        raise ValueError("請使用 HTTPS 的 QNAP myQNAPcloud 分享網址（支援 SmartShare 與 share.cgi）。")
    query = urllib.parse.parse_qs(parsed.query)
    ssid = (query.get("ssid") or [""])[0]
    if not SSID_PATTERN.fullmatch(ssid):
        raise ValueError("分享網址中找不到有效的分享代碼。")
    origin = "https://{0}".format(host)
    return origin + "/share.cgi", ssid


def _validate_remote_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not (host == "myqnapcloud.com" or host.endswith(".myqnapcloud.com"))
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise QnapShareError("QNAP 分享服務回傳了不支援的連線位址。")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute_url = urllib.parse.urljoin(req.full_url, newurl)
        _validate_remote_url(absolute_url)
        return super().redirect_request(req, fp, code, msg, headers, absolute_url)


def _ssl_context():
    context = ssl.create_default_context()
    # QNAP CloudLink relay servers (e.g. *.myqnapcloud.com) chain to the 2012
    # TWCA Global Root CA, which omits the X.509 v3 SubjectKeyIdentifier
    # extension required when Python 3.13+ enables VERIFY_X509_STRICT.
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def _opener():
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_context()),
        _SafeRedirectHandler(),
    )


def _request_url(base_url, params):
    return base_url + "?" + urllib.parse.urlencode(params, doseq=True)


def _smartshare_error_from_code(code):
    if code in (1122002, 1122003):
        return QnapShareError("QNAP 分享密碼錯誤，請確認密碼後重試。")
    if code in (1122001, 1000003):
        return QnapShareError("QNAP 分享連結不存在或已過期。")
    return QnapShareError("QNAP 拒絕讀取分享清單，請確認網址、密碼與分享期限。")


def _normalize_smartshare_item(item):
    return {
        "filename": item.get("file_name"),
        "filesize": item.get("file_size"),
        "isfolder": 1 if item.get("file_type") == "directory" else 0,
        "download_url": item.get("download_url") or "",
    }


def _read_smartshare_json(base_url, body, timeout=REQUEST_TIMEOUT_SECONDS):
    _validate_remote_url(base_url)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base_url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ianAutoKTV/1.0",
        },
        method="POST",
    )
    try:
        with _opener().open(req, timeout=timeout) as response:
            payload = response.read(MAX_LIST_BYTES + 1)
    except urllib.error.HTTPError as error:
        try:
            err_body = json.loads(error.read(65536).decode("utf-8-sig"))
        except Exception:
            err_body = None
        if isinstance(err_body, dict) and "code" in err_body:
            raise _smartshare_error_from_code(err_body.get("code")) from None
        raise QnapShareError("QNAP 拒絕讀取分享清單，請確認網址、密碼與分享期限。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise QnapShareError("目前無法連線至 QNAP 分享服務，請稍後再試。") from None
    if len(payload) > MAX_LIST_BYTES:
        raise QnapShareError("QNAP 歌曲清單過大，請縮小分享資料夾內容後重試。")
    try:
        result = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise QnapShareError("QNAP 未回傳有效歌單；請確認分享網址、密碼與分享連結有效。") from None
    if not isinstance(result, dict):
        raise QnapShareError("QNAP 未回傳有效歌單；請確認分享網址、密碼與分享連結有效。")
    if result.get("code") != 0:
        raise _smartshare_error_from_code(result.get("code"))
    inner = result.get("result")
    if not isinstance(inner, dict) or not isinstance(inner.get("resources", []), list):
        raise QnapShareError("QNAP 未回傳有效歌單；請確認分享網址、密碼與分享連結有效。")
    return inner


def _read_json_response(base_url, params, timeout=REQUEST_TIMEOUT_SECONDS):
    url = _request_url(base_url, params)
    _validate_remote_url(url)
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "ianAutoKTV/1.0"},
    )
    try:
        with _opener().open(req, timeout=timeout) as response:
            payload = response.read(MAX_LIST_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise QnapShareError("QNAP 拒絕讀取分享清單，請確認網址、密碼與分享期限。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise QnapShareError("目前無法連線至 QNAP 分享服務，請稍後再試。") from None
    if len(payload) > MAX_LIST_BYTES:
        raise QnapShareError("QNAP 歌曲清單過大，請縮小分享資料夾內容後重試。")
    try:
        result = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise QnapShareError("QNAP 未回傳有效歌單；請確認分享網址、密碼與分享連結有效。") from None
    if not isinstance(result, dict) or not isinstance(result.get("datas", []), list):
        raise QnapShareError("QNAP 未回傳有效歌單；請確認分享網址、密碼與分享連結有效。")
    return result


def _list_page(state, start, limit=None, timeout=REQUEST_TIMEOUT_SECONDS):
    page_limit = limit or LIST_PAGE_SIZE
    if _is_smartshare_url(state.get("base_url")):
        inner = _read_smartshare_json(state["base_url"], {
            "composed_link_id": state["ssid"],
            "link_path": state.get("link_path", "/"),
            "limit": min(page_limit, SMARTSHARE_PAGE_SIZE),
            "offset": start,
            "sort_type": 7,
            "reverse": False,
            "access_code": state["password"],
        }, timeout=timeout)
        resources = [
            _normalize_smartshare_item(item)
            for item in inner.get("resources", [])
            if isinstance(item, dict)
        ]
        return {
            "total": inner.get("resource_total", len(resources)),
            "datas": resources,
        }
    params = {
        "ssid": state["ssid"],
        "fid": state.get("folder_fid", state["ssid"]),
        "func": "get_list",
        "pwd": state["password"],
        "sort": "filename",
        "dir": "ASC",
        "start": start,
        "limit": min(page_limit, LIST_PAGE_SIZE),
    }
    return _read_json_response(state["base_url"], params, timeout=timeout)


def _run_env_share_fetch(cache_key):
    base_url, ssid, password = cache_key
    try:
        resolved_state, _items = _share_folder_items(base_url, ssid, password, load_items=False)
        resolved_state["_from_env"] = True
        _save_env_library_manifest(cache_key, resolved_state)
    except Exception as exc:
        with _env_share_lock:
            if _env_share_cache.get("fetching_key") == cache_key:
                _env_share_cache["fetching_key"] = None
                _env_share_cache["error"] = exc
                _env_share_cache["error_key"] = cache_key
                _env_share_cache["error_at"] = time.time()
            _env_share_lock.notify_all()
        raise
    with _env_share_lock:
        if _env_share_cache.get("fetching_key") != cache_key:
            return None
        _env_share_cache["key"] = cache_key
        _env_share_cache["state"] = resolved_state
        if _env_share_cache.get("fetching_key") == cache_key:
            _env_share_cache["fetching_key"] = None
        _env_share_cache.pop("error", None)
        _env_share_lock.notify_all()
    return resolved_state


def _update_env_sync_status(**values):
    with _env_share_lock:
        _env_sync_status.update(values)
        try:
            os.makedirs(ENV_LIBRARY_MANIFEST_DIR, exist_ok=True)
            temporary_path = f"{ENV_LIBRARY_STATUS_PATH}.tmp"
            with open(temporary_path, "w", encoding="utf-8") as status_file:
                json.dump(_env_sync_status, status_file, ensure_ascii=False)
            os.replace(temporary_path, ENV_LIBRARY_STATUS_PATH)
        except OSError:
            logger.exception("Unable to persist QNAP sync status.")
        _env_share_lock.notify_all()


def _load_persisted_env_sync_status():
    try:
        with open(ENV_LIBRARY_STATUS_PATH, "r", encoding="utf-8") as status_file:
            payload = json.load(status_file)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        key: payload[key]
        for key in _env_sync_status
        if key in payload
    }


def _safe_sync_error(exc, password):
    message = str(exc).strip() or exc.__class__.__name__
    if password:
        message = message.replace(password, "[redacted]")
    return message


def _library_sync_failure(cache_key, state, offset, error, attempts=1, round_number=None):
    _base_url, ssid, password = cache_key
    failure = {
        "resource": (
            state.get("link_path")
            or state.get("folder_fid")
            or f"share:{ssid}"
        ),
        "cursor": int(offset),
        "offset": int(offset),
        "error_type": error.__class__.__name__,
        "error": _safe_sync_error(error, password),
        "failed_at": time.time(),
        "attempts": int(attempts),
    }
    if round_number is not None:
        failure["recovery_round"] = int(round_number)
    return failure


def _list_page_with_hard_timeout(state, start, limit, timeout):
    """Run one serialized request; urllib timeout prevents idle connections."""
    with _qnap_request_lock:
        with _env_share_lock:
            _env_sync_status["requests_started"] += 1
            _env_sync_status["active_requests"] = 1
        try:
            result = _list_page(state, start, limit, timeout)
        except Exception:
            with _env_share_lock:
                _env_sync_status["requests_failed"] += 1
                _env_sync_status["active_requests"] = 0
            raise
        with _env_share_lock:
            _env_sync_status["requests_completed"] += 1
            _env_sync_status["active_requests"] = 0
        return result


def _recover_failed_offsets(cache_key, state, failed_offsets, notify_update=None):
    """Retry failed pages with a wider page size after the main scan."""
    remaining = list(failed_offsets)
    _, ssid, _password = cache_key
    for recovery_round in range(1, SYNC_RECOVERY_ROUNDS + 1):
        if not remaining:
            break
        time.sleep(SYNC_RECOVERY_DELAY_SECONDS)
        next_remaining = []
        for failure in remaining:
            offset = int(failure["offset"])
            loaded = int(_env_sync_status.get("loaded") or 0)
            try:
                _update_env_sync_status(
                    phase="recovery",
                    current_offset=offset,
                    last_activity_at=time.time(),
                )
                result = _list_page_with_hard_timeout(
                    state,
                    offset,
                    SYNC_RECOVERY_PAGE_SIZE,
                    REQUEST_TIMEOUT_SECONDS,
                )
                page = [
                    item for item in result.get("datas", [])
                    if isinstance(item, dict)
                ]
                if not page:
                    raise QnapShareError("QNAP 未回傳此位置的資源。")
                if _is_smartshare_url(state.get("base_url")):
                    for index, item in enumerate(page):
                        # 保存來源分頁位置，下載短效網址時才能只刷新這一頁。
                        item["library_offset"] = offset + index
                state["_cached_at"] = time.time()
                loaded = int(_env_sync_status.get("loaded") or 0) + len(page)
                _write_library_index_batch(
                    cache_key,
                    page,
                    loaded,
                    int(_env_sync_status.get("total") or 0),
                    int(_env_sync_status.get("current_offset") or 0),
                    "loading",
                    state,
                )
                _save_env_library_manifest(cache_key, state)
                with _env_share_lock:
                    _env_share_cache["state"] = state
                if notify_update:
                    try:
                        notify_update()
                    except Exception:
                        logger.exception(
                            "QNAP recovery notification failed at offset %d.",
                            offset,
                        )
            except Exception as error:
                next_remaining.append(
                    _library_sync_failure(
                        cache_key,
                        state,
                        offset,
                        error,
                        round_number=recovery_round,
                    )
                )
            _update_env_sync_status(
                state="loading",
                phase="recovery",
                loaded=loaded,
                current_offset=offset,
                failed_offsets=list(next_remaining) + [
                    item for item in remaining
                    if item["offset"] != offset
                ],
                last_activity_at=time.time(),
            )
        remaining = next_remaining
        logger.info(
            "QNAP sync recovery round %d for share %s: %d failures remain.",
            recovery_round,
            ssid,
            len(remaining),
        )
    return remaining


def _run_env_failed_offset_recovery(cache_key):
    try:
        with _env_share_lock:
            state = _env_share_cache.get("state")
            failed_offsets = list(_env_sync_status.get("failed_offsets") or [])
            if not isinstance(state, dict) or not failed_offsets:
                return
            _env_share_cache["fetching_key"] = cache_key
            _env_share_cache["fetching_at"] = time.time()
        remaining = _recover_failed_offsets(cache_key, state, failed_offsets)
        if state.get("snapshot_partial"):
            scan_started_at = _env_sync_status.get("started_at")
            snapshot_partial = True
            if not remaining and scan_started_at:
                _prune_library_index(cache_key, scan_started_at)
                snapshot_partial = False
            state["snapshot_partial"] = snapshot_partial
            sync = _read_library_index_state(cache_key)
            if sync:
                _write_library_index_batch(
                    cache_key,
                    [],
                    sync["loaded"],
                    sync["total"],
                    sync["cursor"],
                    "ready",
                    state,
                )
                if sync["item_count"]:
                    try:
                        if not _persist_gas_library_snapshot(cache_key, state):
                            logger.warning(
                                "Google Sheets NAS snapshot was not saved for share %s "
                                "because persistence is not configured.",
                                cache_key[1],
                            )
                    except QnapShareError as error:
                        logger.warning(
                            "Google Sheets NAS snapshot could not be saved after "
                            "failed-offset recovery for share %s: %s",
                            cache_key[1],
                            error,
                        )
        completed_at = time.time()
        warning = (
            f"有 {len(remaining)} 筆 NAS 資源讀取失敗，已繼續讀取後續歌曲。"
            if remaining else ""
        )
        _update_env_sync_status(
            state="ready",
            phase="complete",
            loaded=int(_env_sync_status.get("loaded") or 0),
            completed_at=completed_at,
            last_activity_at=completed_at,
            failed_offsets=remaining,
            partial_error=warning,
        )
    except Exception as error:
        _update_env_sync_status(
            state="ready",
            phase="complete",
            partial_error=f"失敗項目重新讀取時發生錯誤：{error}",
            last_activity_at=time.time(),
        )
    finally:
        with _env_share_lock:
            _env_share_cache["fetching_key"] = None
            _env_share_cache.pop("fetching_at", None)


def _run_env_library_sync(
    cache_key, notify_update=None, force_full_rescan=False
):
    """Fetch the environment NAS index page by page and publish each page."""
    base_url, ssid, password = cache_key
    started_at = time.time()
    _update_env_sync_status(
        state="starting",
        phase="checking_cache_state",
        last_activity_at=started_at,
        cache_stage="",
    )
    logger.warning("QNAP background sync is checking the SQLite sync state.")
    existing_sync = _read_library_index_state(cache_key)
    logger.warning(
        "QNAP background sync finished checking the SQLite sync state (found=%s).",
        bool(existing_sync),
    )
    existing_snapshot_partial = bool(
        existing_sync
        and existing_sync.get("state_metadata", {}).get("snapshot_partial")
    )
    restored_snapshot = None
    if not force_full_rescan and not existing_sync:
        _update_env_sync_status(
            state="starting",
            phase="loading_gas_snapshot",
            last_activity_at=time.time(),
        )
        logger.warning("QNAP background sync is loading the Google Sheets snapshot.")
        if not _gas_library_configured():
            logger.warning(
                "Google Sheets NAS snapshot persistence is not configured for share %s.",
                ssid,
            )
        try:
            restored_snapshot = _restore_gas_library_snapshot(cache_key)
        except QnapShareError as error:
            logger.warning(
                "Google Sheets NAS snapshot could not be restored for share %s: %s",
                ssid,
                error,
            )
        logger.warning(
            "QNAP background sync finished loading the Google Sheets snapshot (found=%s).",
            bool(restored_snapshot),
        )
        if _gas_library_configured() and not restored_snapshot:
            logger.info(
                "No committed Google Sheets NAS snapshot exists for share %s.",
                ssid,
            )

    full_scan = (
        force_full_rescan
        or not existing_sync
        or bool(restored_snapshot)
        or existing_snapshot_partial
    )
    if force_full_rescan or not existing_sync or existing_snapshot_partial:
        offset = 0
        total = 0
        loaded = 0
    else:
        offset = max(0, int(existing_sync.get("cursor") or 0))
        total = max(0, int(existing_sync.get("total") or 0))
        loaded = int(existing_sync.get("loaded") or 0)
    _update_env_sync_status(
        state="starting",
        phase="connecting",
        loaded=loaded,
        total=total,
        error="",
        error_type="",
        started_at=started_at,
        last_activity_at=started_at,
        current_offset=offset,
        completed_at=None,
        failed_offsets=[],
        partial_error="",
        requests_started=0,
        requests_completed=0,
        requests_failed=0,
        active_requests=0,
    )
    logger.info("QNAP background sync started for share %s.", ssid)
    try:
        if restored_snapshot:
            state = restored_snapshot["state"]
            snapshot_age = max(0, time.time() - restored_snapshot["updated_at"])
            if (
                snapshot_age < _sync_interval_seconds()
                and not restored_snapshot["partial"]
            ):
                completed_at = restored_snapshot["updated_at"]
                _update_env_sync_status(
                    state="ready",
                    phase="cached",
                    loaded=restored_snapshot["total"],
                    total=restored_snapshot["total"],
                    error="",
                    error_type="",
                    completed_at=completed_at,
                    current_offset=restored_snapshot["total"],
                    last_activity_at=completed_at,
                    failed_offsets=[],
                    partial_error="",
                )
                with _env_share_lock:
                    _env_share_cache["fetching_key"] = None
                    _env_share_cache.pop("fetching_at", None)
                    _env_share_cache["key"] = cache_key
                    _env_share_cache["state"] = state
                    _env_share_lock.notify_all()
                if notify_update:
                    try:
                        notify_update()
                    except Exception:
                        logger.exception(
                            "QNAP cache-restored notification failed for share %s.",
                            ssid,
                        )
                logger.info(
                    "Restored %d NAS library items from Google Sheets for share %s.",
                    restored_snapshot["total"],
                    ssid,
                )
                return
            if restored_snapshot["partial"]:
                logger.info(
                    "Restored a partial NAS library snapshot for share %s; "
                    "refreshing it from QNAP.",
                    ssid,
                )
            full_scan = True
            offset = 0
            total = 0
            loaded = 0
        else:
            with _qnap_request_lock:
                state, _items = _share_folder_items(
                    base_url, ssid, password, load_items=False
                )
            state["_from_env"] = True
            state["_cached_items"] = []
            state["_cached_at"] = time.time()
        with _env_share_lock:
            _env_share_cache["key"] = cache_key
            _env_share_cache["state"] = state
        if full_scan:
            # Keep interrupted full scans identifiable so a restart rescans from the beginning.
            state["snapshot_partial"] = True
        sync_started_at = time.time()
        _update_env_sync_status(
            state="loading",
            phase="listing",
            loaded=loaded,
            total=total,
            error="",
            error_type="",
            failed_offsets=[],
            partial_error="",
            requests_started=0,
            requests_completed=0,
            requests_failed=0,
            active_requests=0,
        )
        failed_offsets = []
        while True:
            result = None
            for attempt in range(SYNC_PAGE_RETRIES + 1):
                request_started_at = time.time()
                _update_env_sync_status(
                    phase="requesting",
                    current_offset=offset,
                    request_started_at=request_started_at,
                    request_finished_at=None,
                    last_activity_at=request_started_at,
                )
                try:
                    result = _list_page_with_hard_timeout(
                        state,
                        offset,
                        SYNC_PAGE_SIZE,
                        REQUEST_TIMEOUT_SECONDS,
                    )
                    request_finished_at = time.time()
                    _update_env_sync_status(
                        phase="listing",
                        request_finished_at=request_finished_at,
                        last_activity_at=request_finished_at,
                    )
                    break
                except QnapShareError as error:
                    if attempt >= SYNC_PAGE_RETRIES:
                        failed_offsets.append(
                            _library_sync_failure(
                                cache_key,
                                state,
                                offset,
                                error,
                                attempts=attempt + 1,
                            )
                        )
                        _update_env_sync_status(
                            phase="skipping",
                            current_offset=offset,
                            failed_offsets=list(failed_offsets),
                            partial_error=(
                                f"有 {len(failed_offsets)} 筆 NAS 資源讀取失敗，"
                                "已繼續讀取後續歌曲。"
                            ),
                            last_activity_at=time.time(),
                        )
                        break
                    retry_at = time.time()
                    _update_env_sync_status(
                        phase="retrying",
                        request_finished_at=retry_at,
                        last_activity_at=retry_at,
                    )
                    retry_delay = min(
                        SYNC_RETRY_BASE_DELAY_SECONDS * (2 ** attempt),
                        SYNC_RETRY_MAX_DELAY_SECONDS,
                    )
                    time.sleep(retry_delay)
            if result is not None:
                page = [
                    item for item in result.get("datas", [])
                    if isinstance(item, dict)
                ]
                if _is_smartshare_url(base_url):
                    for index, item in enumerate(page):
                        # 保存來源分頁位置，避免下載時重新掃描整份歌庫。
                        item["library_offset"] = offset + index
                try:
                    total = max(
                        total,
                        int(result.get("total", offset + len(page))),
                    )
                except (TypeError, ValueError):
                    total = max(total, offset + len(page))
                state["_cached_at"] = time.time()
                loaded = max(
                    int(_env_sync_status.get("loaded") or 0),
                    offset + len(page),
                )
                _write_library_index_batch(
                    cache_key,
                    page,
                    loaded,
                    total,
                    offset + len(page),
                    "loading",
                    state,
                )
                _save_env_library_manifest(cache_key, state)
                with _env_share_lock:
                    _env_share_cache["state"] = state
                if notify_update:
                    try:
                        notify_update()
                    except Exception:
                        logger.exception(
                            "QNAP sync notification failed after item %d; continuing.",
                            offset + len(page),
                        )
                offset += len(page)
            else:
                # A failed single resource still consumes its offset. Continue
                # probing subsequent offsets until QNAP returns an empty page.
                offset += 1
            _update_env_sync_status(
                state="loading",
                phase="listing",
                loaded=max(
                    int(_env_sync_status.get("loaded") or 0),
                    offset,
                ),
                total=total,
                current_offset=offset,
                failed_offsets=list(failed_offsets),
                partial_error=(
                    f"有 {len(failed_offsets)} 筆 NAS 資源讀取失敗，"
                    "已繼續讀取後續歌曲。"
                    if failed_offsets else ""
                ),
                last_activity_at=time.time(),
            )
            if result is not None and not page:
                break
            if total and offset >= total:
                break
            if offset > MAX_LIBRARY_RESOURCE_COUNT:
                raise QnapShareError("QNAP 歌曲清單超過單次讀取上限。")
            time.sleep(SYNC_PAGE_DELAY_SECONDS)
        failed_offsets = _recover_failed_offsets(
            cache_key,
            state,
            failed_offsets,
            notify_update,
        )
        completed_at = time.time()
        if full_scan and not failed_offsets:
            _prune_library_index(cache_key, sync_started_at)
        if full_scan:
            state["snapshot_partial"] = bool(failed_offsets)
        _write_library_index_batch(
            cache_key,
            [],
            max(int(_env_sync_status.get("loaded") or 0), offset),
            total,
            offset,
            "ready",
            state,
        )
        if full_scan:
            _update_env_sync_status(
                state="loading",
                phase="persisting",
                loaded=max(int(_env_sync_status.get("loaded") or 0), offset),
                total=total,
                last_activity_at=time.time(),
            )
            try:
                index_state = _read_library_index_state(cache_key)
                if (
                    failed_offsets
                    and not int(index_state.get("item_count") or 0)
                ):
                    logger.warning(
                        "Google Sheets NAS snapshot was not saved for share %s "
                        "because no indexed items were available after the partial scan.",
                        ssid,
                    )
                elif not _persist_gas_library_snapshot(cache_key, state):
                    logger.warning(
                        "Google Sheets NAS snapshot was not saved for share %s "
                        "because persistence is not configured.",
                        ssid,
                    )
                elif failed_offsets:
                    logger.warning(
                        "Saved a partial Google Sheets NAS snapshot for share %s "
                        "with %d failed resources.",
                        ssid,
                        len(failed_offsets),
                    )
            except QnapShareError as error:
                logger.warning(
                    "Google Sheets NAS snapshot could not be saved for share %s: %s",
                    ssid,
                    error,
                )
        completed_at = time.time()
        _update_env_sync_status(
            state="ready",
            phase="complete",
            loaded=max(int(_env_sync_status.get("loaded") or 0), offset),
            total=total,
            error="",
            error_type="",
            completed_at=completed_at,
            current_offset=offset,
            last_activity_at=completed_at,
            failed_offsets=list(failed_offsets),
            partial_error=(
                f"有 {len(failed_offsets)} 筆 NAS 資源讀取失敗，"
                "已繼續讀取後續歌曲。"
                if failed_offsets else ""
            ),
        )
        with _env_share_lock:
            _env_share_cache["fetching_key"] = None
            _env_share_cache.pop("fetching_at", None)
            _env_share_cache["key"] = cache_key
            _env_share_cache["state"] = state
            _env_share_lock.notify_all()
        logger.info(
            "QNAP background sync completed for share %s: %d/%d items.",
            ssid,
            offset,
            total,
        )
    except Exception as exc:
        error_message = _safe_sync_error(exc, password)
        logger.error(
            "QNAP background sync failed for share %s: %s\n%s",
            ssid,
            error_message,
            traceback.format_exc(),
        )
        with _env_share_lock:
            if _env_share_cache.get("fetching_key") == cache_key:
                _env_share_cache["fetching_key"] = None
                _env_share_cache.pop("fetching_at", None)
            _env_share_cache["error"] = QnapShareError(error_message)
            _env_share_cache["error_key"] = cache_key
            _env_share_cache["error_at"] = time.time()
            _env_share_lock.notify_all()
        _update_env_sync_status(
            state="error",
            phase="failed",
            error=error_message,
            error_type=exc.__class__.__name__,
            completed_at=time.time(),
        )
    finally:
        _schedule_next_env_sync(notify_update)


def _run_env_library_sync_safely(
    cache_key, notify_update=None, force_full_rescan=False
):
    try:
        _update_env_sync_status(
            state="starting",
            phase="loading_cache",
            last_activity_at=time.time(),
            cache_stage="starting",
        )
        logger.warning(
            "QNAP background sync is loading the SQLite cache for share %s.",
            cache_key[1],
        )
        # SQLite 快取讀取在背景執行，避免 NAS API 等待資料庫鎖。
        cached_state = _load_cached_env_library_state(cache_key)
        logger.warning(
            "QNAP background sync finished loading the SQLite cache for share %s.",
            cache_key[1],
        )
        if cached_state is not None:
            with _env_share_lock:
                if _env_share_cache.get("fetching_key") == cache_key:
                    _env_share_cache["key"] = cache_key
                    _env_share_cache["state"] = cached_state
                    _env_share_lock.notify_all()
        _run_env_library_sync(cache_key, notify_update, force_full_rescan)
    except Exception as exc:
        _base_url, ssid, password = cache_key
        error_message = _safe_sync_error(exc, password)
        traceback_text = traceback.format_exc()
        if password:
            traceback_text = traceback_text.replace(password, "[redacted]")
        logger.error(
            "QNAP background sync failed before worker error handling for share %s: %s\n%s",
            ssid,
            error_message,
            traceback_text,
        )
        with _env_share_lock:
            if _env_share_cache.get("fetching_key") == cache_key:
                _env_share_cache["fetching_key"] = None
                _env_share_cache.pop("fetching_at", None)
            _env_share_cache["error"] = QnapShareError(error_message)
            _env_share_cache["error_key"] = cache_key
            _env_share_cache["error_at"] = time.time()
            _env_share_lock.notify_all()
        completed_at = time.time()
        _update_env_sync_status(
            state="error",
            phase="failed",
            error=error_message,
            error_type=exc.__class__.__name__,
            completed_at=completed_at,
            last_activity_at=completed_at,
            active_requests=0,
        )
        _schedule_next_env_sync(notify_update)


def _start_env_share_fetch_async(cache_key):
    with _env_share_lock:
        if _env_share_cache.get("fetching_key") == cache_key:
            return
        _env_share_cache["fetching_key"] = cache_key
        _env_share_cache["fetching_at"] = time.time()
        _env_share_cache.pop("error", None)

    def _worker():
        try:
            _run_env_share_fetch(cache_key)
        except Exception:
            logger.exception("QNAP room library fetch failed.")

    threading.Thread(target=_worker, daemon=True).start()


def _sync_interval_seconds():
    raw_value = os.environ.get(
        "QNAP_LIBRARY_SYNC_INTERVAL_SECONDS",
        str(DEFAULT_LIBRARY_SYNC_INTERVAL_SECONDS),
    ).strip()
    try:
        return max(300, int(raw_value))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid QNAP_LIBRARY_SYNC_INTERVAL_SECONDS; using the six-hour default."
        )
        return DEFAULT_LIBRARY_SYNC_INTERVAL_SECONDS


def _schedule_next_env_sync(notify_update=None):
    global _env_sync_timer

    def run_scheduled_sync():
        global _env_sync_timer
        with _env_share_lock:
            _env_sync_timer = None
        start_qnap_background_sync(
            notify_update=notify_update,
            force_full_rescan=True,
        )

    with _env_share_lock:
        if _env_sync_timer is not None and _env_sync_timer.is_alive():
            return
        timer = threading.Timer(_sync_interval_seconds(), run_scheduled_sync)
        timer.daemon = True
        _env_sync_timer = timer
        timer.start()


def start_qnap_background_sync(notify_update=None, force_full_rescan=False):
    """Start the shared NAS index sync when the application is initialized."""
    share_url, password = _env_share_credentials()
    if not share_url or not password:
        return False
    base_url, ssid = _allowed_share_url(share_url)
    cache_key = (base_url, ssid, password)
    with _env_share_lock:
        if _env_share_cache.get("fetching_key") == cache_key:
            return True
        _env_share_cache["fetching_key"] = cache_key
        _env_share_cache["fetching_at"] = time.time()
        _env_share_cache.pop("error", None)
        _env_share_cache.pop("error_key", None)
        _env_share_cache.pop("error_at", None)
    _update_env_sync_status(
        state="starting",
        phase="queued",
        loaded=0,
        total=0,
        error="",
        error_type="",
        started_at=time.time(),
        last_activity_at=time.time(),
        current_offset=0,
        completed_at=None,
    )
    threading.Thread(
        target=_run_env_library_sync_safely,
        args=(cache_key, notify_update, force_full_rescan),
        daemon=True,
        name="qnap-library-sync",
    ).start()
    return True


def _all_items(state, refresh=False):
    cached_items = state.get("_cached_items")
    cached_at = state.get("_cached_at") or 0
    if (
        state.get("_from_env")
        and state.get("base_url")
        and state.get("ssid")
        and state.get("password")
        and not refresh
    ):
        indexed_items = _read_library_index_items(
            (state["base_url"], state["ssid"], state["password"])
        )
        if indexed_items:
            state["_cached_items"] = indexed_items
            state["_cached_at"] = time.time()
            return indexed_items
    if not refresh and isinstance(cached_items, list):
        if (time.time() - float(cached_at)) < ITEMS_CACHE_TTL_SECONDS:
            return cached_items
        if state.get("_from_env") and state.get("base_url") and state.get("ssid") and state.get("password"):
            _start_env_share_fetch_async((state["base_url"], state["ssid"], state["password"]))
            return cached_items

    items = []
    start = 0
    total = None
    while total is None or start < total:
        result = _list_page(state, start)
        page = result.get("datas", [])
        if total is None:
            try:
                total = int(result.get("total", len(page)))
            except (TypeError, ValueError):
                total = len(page)
        for index, item in enumerate(page):
            if not isinstance(item, dict):
                continue
            if _is_smartshare_url(state.get("base_url")):
                # 快取資料也保存來源位置，讓下載流程可精確定位單頁。
                item.setdefault("library_offset", start + index)
            items.append(item)
        if not page or len(items) >= total:
            break
        start += len(page)
        if start > MAX_LIBRARY_RESOURCE_COUNT:
            raise QnapShareError("QNAP 歌曲清單超過單次讀取上限。")
    state["_cached_items"] = items
    state["_cached_at"] = time.time()
    if state.get("_from_env"):
        with _env_share_lock:
            cached_state = _env_share_cache.get("state")
            if isinstance(cached_state, dict) and cached_state.get("ssid") == state.get("ssid"):
                cached_state["_cached_items"] = list(items)
                cached_state["_cached_at"] = state["_cached_at"]
    return items


def _ensure_room_nas_state(state, wait_timeout=None):
    """Populate room NAS state from Render environment variables when not yet set."""
    share_url, password = _env_share_credentials()
    if state.get("ssid") and state.get("password"):
        if state.get("_from_env"):
            with _env_share_lock:
                cached_state = _env_share_cache.get("state")
                if (
                    isinstance(cached_state, dict)
                    and cached_state.get("ssid") == state.get("ssid")
                    and float(cached_state.get("_cached_at") or 0) > float(state.get("_cached_at") or 0)
                ):
                    state["_cached_items"] = list(cached_state.get("_cached_items") or [])
                    state["_cached_at"] = cached_state.get("_cached_at")
        return "ready"

    if not share_url or not password:
        return False

    base_url, ssid = _allowed_share_url(share_url)
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("Render 環境變數中的 QNAP 分享密碼長度無效。")

    cache_key = (base_url, ssid, password)
    if wait_timeout is not None:
        with _env_share_lock:
            if (
                _env_share_cache.get("key") == cache_key
                and isinstance(_env_share_cache.get("state"), dict)
            ):
                cached_state = _env_share_cache["state"]
                state.clear()
                state.update({
                    key: (list(value) if isinstance(value, list) else value)
                    for key, value in cached_state.items()
                })
                return "ready"
            if _env_share_cache.get("fetching_key") == cache_key:
                return "loading"
            if (
                _env_share_cache.get("error_key") == cache_key
                and _env_share_cache.get("error") is not None
                and time.time() - float(_env_share_cache.get("error_at") or 0) < 8
            ):
                raise _env_share_cache["error"]
        if not start_qnap_background_sync():
            return False
        return "loading"

    with _env_share_lock:
        if (
            _env_share_cache.get("key") == cache_key
            and isinstance(_env_share_cache.get("state"), dict)
        ):
            cached_state = _env_share_cache["state"]
            state.clear()
            state.update({
                key: (list(value) if isinstance(value, list) else value)
                for key, value in cached_state.items()
            })
            return "ready"

    cached_state = _load_cached_env_library_state(cache_key)
    if cached_state is not None:
        with _env_share_lock:
            if (
                _env_share_cache.get("key") == cache_key
                and isinstance(_env_share_cache.get("state"), dict)
            ):
                cached_state = _env_share_cache["state"]
            else:
                _env_share_cache["key"] = cache_key
                _env_share_cache["state"] = cached_state
            should_start_sync = _env_sync_status.get("state") == "idle"
            state.clear()
            state.update({
                key: (list(value) if isinstance(value, list) else value)
                for key, value in cached_state.items()
            })
        if should_start_sync:
            start_qnap_background_sync()
        return "ready"

    with _env_share_lock:
        while _env_share_cache.get("fetching_key") == cache_key:
            _env_share_lock.wait(timeout=1.0)
        if _env_share_cache.get("key") == cache_key and isinstance(_env_share_cache.get("state"), dict):
            cached_state = _env_share_cache["state"]
            state.clear()
            state.update({
                key: (list(value) if isinstance(value, list) else value)
                for key, value in cached_state.items()
            })
            return "ready"
        _env_share_cache["fetching_key"] = cache_key
        _env_share_cache["fetching_at"] = time.time()
        _env_share_cache.pop("error", None)

    resolved_state = _run_env_share_fetch(cache_key)
    state.clear()
    state.update({
        key: (list(value) if isinstance(value, list) else value)
        for key, value in resolved_state.items()
    })
    return "ready"


def _share_folder_items(base_url, ssid, password, load_items=True):
    if _is_smartshare_url(base_url):
        root = _read_smartshare_json(base_url, {
            "composed_link_id": ssid,
            "link_path": "/",
            "limit": SMARTSHARE_PAGE_SIZE,
            "offset": 0,
            "sort_type": 7,
            "reverse": False,
            "access_code": password,
        })
        root_items = [
            _normalize_smartshare_item(item)
            for item in root.get("resources", [])
            if isinstance(item, dict)
        ]
        if len(root_items) == 1 and int(root_items[0].get("isfolder", 0) or 0) == 1:
            folder_name = root_items[0].get("filename") or root.get("link_name") or "QNAP 歌庫"
            link_path = "/" + folder_name.strip("/")
            state = {
                "base_url": base_url,
                "ssid": ssid,
                "password": password,
                "folder_fid": link_path,
                "link_path": link_path,
                "folder_name": folder_name,
            }
            return state, _all_items(state, refresh=True) if load_items else []
        state = {
            "base_url": base_url,
            "ssid": ssid,
            "password": password,
            "folder_fid": "/",
            "link_path": "/",
            "folder_name": root.get("link_name") or "QNAP 歌庫",
        }
        try:
            total = int(root.get("resource_total", len(root_items)))
        except (TypeError, ValueError):
            total = len(root_items)
        if total > len(root_items):
            return state, _all_items(state, refresh=True) if load_items else []
        state["_cached_items"] = root_items
        state["_cached_at"] = time.time()
        return state, root_items

    root = _read_json_response(base_url, {
        "ssid": ssid,
        "func": "get_list",
        "pwd": password,
        "sort": "filename",
        "dir": "ASC",
        "start": 0,
        "limit": LIST_PAGE_SIZE,
    })
    root_items = [item for item in root.get("datas", []) if isinstance(item, dict)]
    # QNAP share links for a shared folder return the shared folder itself in the
    # first response. Its URL contains the folder fid used to list its contents.
    if len(root_items) == 1 and int(root_items[0].get("isfolder", 0) or 0) == 1:
        folder = root_items[0]
        folder_url = urllib.parse.urlsplit(folder.get("url", ""))
        folder_fid = (urllib.parse.parse_qs(folder_url.query).get("fid") or [ssid])[0]
        if not SSID_PATTERN.fullmatch(folder_fid):
            folder_fid = ssid
        state = {
            "base_url": base_url,
            "ssid": ssid,
            "password": password,
            "folder_fid": folder_fid,
            "folder_name": folder.get("filename", "QNAP 歌庫"),
        }
        return state, _all_items(state, refresh=True)
    state = {
        "base_url": base_url,
        "ssid": ssid,
        "password": password,
        "folder_fid": ssid,
        "folder_name": root.get("link_name") or "QNAP 歌庫",
        "_cached_items": root_items,
        "_cached_at": time.time(),
    }
    # A shared single file has no list to enumerate; folder shares and multi-file
    # shares return datas entries here.
    return state, root_items


def _qnap_file_id(state, filename):
    value = "qnap:{0}:{1}:{2}".format(
        state["ssid"], state.get("folder_fid", state["ssid"]), filename,
    )
    return "qnap_" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _songs_from_items(state, items, download_dir):
    lyrics_by_stem = {}
    media = []
    for item in items:
        name = item.get("filename")
        if not isinstance(name, str) or os.path.basename(name.replace("\\", "/")) != name:
            continue
        extension = os.path.splitext(name)[1].lower()
        if extension == ".mp4" and not int(item.get("isfolder", 0) or 0):
            media.append(item)
        elif extension in LYRIC_EXTENSIONS and not int(item.get("isfolder", 0) or 0):
            lyrics_by_stem.setdefault(os.path.splitext(name)[0].casefold(), []).append(item)

    downloaded = {
        record["file_id"]: record
        for record in drive_core.get_downloaded_song_records(download_dir)
        if record.get("file_id", "").startswith("qnap_")
    }
    songs = []
    for item in media:
        name = item["filename"]
        file_id = _qnap_file_id(state, name)
        record = downloaded.get(file_id)
        songs.append({
            "id": file_id,
            "name": name,
            "size": item.get("filesize"),
            "has_lyrics": bool(lyrics_by_stem.get(os.path.splitext(name)[0].casefold())),
            "downloaded": record is not None,
            "filename": record.get("filename", "") if record else "",
        })
    return songs, lyrics_by_stem


def _refresh_smartshare_download_items(state, filenames, indexed_items):
    """僅刷新指定下載檔案所在的 SmartShare 分頁，避免重掃整份歌庫。"""
    wanted = {name for name in filenames if isinstance(name, str) and name}
    offsets = {}
    for index, item in enumerate(indexed_items):
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if filename not in wanted or filename in offsets:
            continue
        offset = item.get("library_offset")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            offset = index
        offsets[filename] = offset

    if wanted - offsets.keys():
        raise QnapShareError(
            "NAS 歌曲索引缺少 SmartShare 項目位置，請重新同步歌庫後重試。"
        )

    refreshed = {}
    page_offsets = {
        (offset // SMARTSHARE_PAGE_SIZE) * SMARTSHARE_PAGE_SIZE
        for offset in offsets.values()
    }
    for page_offset in sorted(page_offsets):
        result = _list_page_with_hard_timeout(
            state,
            page_offset,
            SMARTSHARE_PAGE_SIZE,
            PAGE_REQUEST_TIMEOUT_SECONDS,
        )
        for item in result.get("datas", []):
            if isinstance(item, dict) and item.get("filename") in wanted:
                refreshed[item["filename"]] = item

    missing = wanted - refreshed.keys()
    if missing:
        raise QnapShareError(
            "無法在 NAS 對應頁面更新歌曲或歌詞下載網址，請重新同步歌庫後重試。"
        )
    return refreshed


def _download_file(state, filename, target_path, expected_size, download_url=None,
                   progress_callback=None):
    if _is_smartshare_url(state.get("base_url")):
        url = download_url
        if not url:
            for item in _all_items(state):
                if item.get("filename") == filename and item.get("download_url"):
                    url = item.get("download_url")
                    break
        if not url:
            raise QnapShareError("QNAP 分享服務未提供此檔案的下載網址。")
    else:
        params = {
            "ssid": state["ssid"],
            "fid": state.get("folder_fid", state["ssid"]),
            "openfolder": "forcedownload",
            "pwd": state["password"],
            "path": "/",
            "filename": filename,
        }
        url = _request_url(state["base_url"], params)
    _validate_remote_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "ianAutoKTV/1.0"})
    temporary_path = None
    size = 0
    try:
        with _opener().open(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared = int(content_length)
                except ValueError:
                    declared = None
                if declared is not None and declared > MAX_MEDIA_BYTES:
                    raise QnapShareError("歌曲超過目前雲端版允許的下載大小。")
            fd, temporary_path = tempfile.mkstemp(prefix=".qnap-download-", dir=os.path.dirname(target_path))
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_MEDIA_BYTES:
                        raise QnapShareError("歌曲超過目前雲端版允許的下載大小。")
                    output.write(chunk)
                    if progress_callback:
                        progress_callback(size, declared if declared is not None else expected_size)
                output.flush()
                os.fsync(output.fileno())
        if expected_size is not None and size != expected_size:
            raise QnapShareError("歌曲下載大小與 QNAP 清單不符，請重新點播。")
        os.replace(temporary_path, target_path)
        temporary_path = None
    except QnapShareError:
        raise
    except OSError as error:
        if error.errno == errno.ENOSPC:
            raise QnapShareError(
                "Render 儲存空間不足，歌曲下載已停止。請稍後重試或清理公共 NAS 快取。"
            ) from None
        raise QnapShareError("QNAP 歌曲下載失敗，請確認分享仍有效後重試。") from None
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        raise QnapShareError("QNAP 歌曲下載失敗，請確認分享仍有效後重試。") from None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                pass
    return size


def register_qnap_share_routes(app, get_state, get_download_dir, is_room_owner,
                               notify_room=None, get_local_songs=None,
                               get_play_counts=None):
    """Register host-managed, room-shared QNAP share library endpoints."""
    download_jobs = {}
    download_jobs_lock = threading.Lock()
    # Coordinate shared-cache metadata and reservations without blocking Drive downloads.
    qnap_cache_lock = threading.RLock()
    qnap_cache_reservations = {}
    active_download_jobs = {}
    # 只有伺服器建立的工作執行緒可進入同步下載流程。
    worker_token = uuid.uuid4().hex

    def require_room_player():
        if session.get("room_role") == "player":
            return True
        token = request.headers.get("X-KTV-Room-Owner", "")
        return bool(is_room_owner and is_room_owner(session.get("room_code"), token))

    def response(payload, status=200):
        result = jsonify(payload)
        result.status_code = status
        result.headers["Cache-Control"] = "no-store, max-age=0"
        result.headers["Pragma"] = "no-cache"
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return result

    def set_download_stage(job_id, stage, phase=None, progress=None):
        """更新下載子階段，並在切換時記錄不含歌曲名稱的診斷訊息。"""
        if not job_id:
            return
        with download_jobs_lock:
            job = download_jobs.get(job_id)
            if not job:
                return
            stage_changed = job.get("stage") != stage
            job["stage"] = stage
            if phase is not None:
                job["phase"] = phase
            if progress is not None:
                job["progress"] = progress
        if stage_changed:
            logger.info("QNAP download job %s stage=%s", job_id, stage)

    @app.get("/api/nas/library-songs/combined-page")
    def qnap_library_songs_combined_page():
        try:
            offset = max(0, int(request.args.get("offset", "0")))
            page_size = min(20, max(1, int(request.args.get("limit", "20"))))
            title_length_value = request.args.get("length", "").strip()
            title_length = int(title_length_value) if title_length_value else None
            if title_length is not None and not 0 <= title_length <= 256:
                raise ValueError
        except (TypeError, ValueError):
            return response({"error": "合併曲庫分頁或歌曲長度參數無效。"}, 400)

        keyword = (request.args.get("q") or "").strip()
        singer = request.args.get("singer", "").strip()
        language = request.args.get("language", "").strip()
        categories = [
            value.strip()
            for value in (request.args.getlist("category") or request.args.getlist("affiliation"))
            if value.strip()
        ]
        sort = request.args.get("sort", "newest").strip().lower()
        if (
            any(len(value) > 128 for value in (keyword, singer, language))
            or any(len(value) > 128 for value in categories)
            or len(categories) > 50
        ):
            return response({
                "error": "合併曲庫搜尋條件不可超過 128 個字元，歸屬篩選最多 50 項。"
            }, 400)
        if sort not in {"newest", "popular", "cold"}:
            return response({"error": "合併曲庫排序方式無效。"}, 400)

        state = get_state()
        try:
            status = _ensure_room_nas_state(state, wait_timeout=1.5)
            if status == "loading":
                with _env_share_lock:
                    sync_status = dict(_env_sync_status)
                return response({
                    "loading": True,
                    "songs": [],
                    "next_offset": offset,
                    "total_count": 0,
                    "has_more": True,
                    "sync_state": sync_status.get("state"),
                    "sync_loaded": sync_status.get("loaded", 0),
                    "sync_total": sync_status.get("total", 0),
                })
            if not status:
                return response({"error": "尚未設定 QNAP NAS 歌庫。"}, 409)
            if not state.get("_from_env") or not state.get("base_url") or not state.get("ssid"):
                return response({
                    "error": "合併曲庫分頁目前僅支援 Render 預設 NAS 歌庫。"
                }, 409)

            cache_key = (
                state["base_url"],
                state["ssid"],
                state.get("password"),
            )
            indexed_sync = _read_library_index_state(cache_key)
            if not indexed_sync:
                with _env_share_lock:
                    sync_status = dict(_env_sync_status)
                if sync_status.get("state") in {"starting", "loading"}:
                    return response({
                        "loading": True,
                        "songs": [],
                        "next_offset": offset,
                        "total_count": 0,
                        "has_more": True,
                        "sync_state": sync_status.get("state"),
                        "sync_loaded": sync_status.get("loaded", 0),
                        "sync_total": sync_status.get("total", 0),
                    })
                return response({"error": "NAS 歌曲索引尚未建立。"}, 503)

            with _env_share_lock:
                sync_status = dict(_env_sync_status)
            persisted_status = _load_persisted_env_sync_status()
            if sync_status.get("state") == "idle" and persisted_status:
                sync_status.update(persisted_status)
            sync_state = sync_status.get("state")
            if sync_state == "idle":
                sync_state = indexed_sync.get("state", "ready")
            if sync_state == "idle":
                sync_state = "ready"

            songs, total, facets = _read_library_index_combined_page(
                cache_key,
                state,
                get_download_dir(),
                get_local_songs() if get_local_songs else [],
                get_play_counts() if get_play_counts else {},
                keyword=keyword,
                singer=singer,
                language=language,
                categories=categories,
                title_length=title_length,
                sort=sort,
                offset=offset,
                limit=page_size,
            )
            return response({
                "loading": False,
                "songs": songs,
                "offset": offset,
                "limit": page_size,
                "next_offset": offset + len(songs),
                "total_count": total,
                "has_more": offset + len(songs) < total,
                "facets": facets,
                "sync_state": sync_state,
                "sync_loaded": int(sync_status.get("loaded") or indexed_sync.get("loaded") or 0),
                "sync_total": int(sync_status.get("total") or indexed_sync.get("total") or 0),
                "partial_error": sync_status.get("partial_error", ""),
            })
        except (ValueError, QnapShareError) as error:
            return response({"error": str(error)}, 400 if isinstance(error, ValueError) else 502)
        except sqlite3.Error as error:
            logger.exception("Unable to read the NAS library SQLite index.")
            return response({
                "error": "NAS 歌曲索引資料庫暫時無法讀取，請稍後重試。",
                "error_type": error.__class__.__name__,
            }, 503)

    @app.get("/api/nas/library-status")
    def qnap_library_status():
        state = get_state()
        error_message = ""
        loading = False
        if not (state.get("ssid") and state.get("password")):
            try:
                status = _ensure_room_nas_state(state, wait_timeout=0.5)
                loading = (status == "loading")
            except (ValueError, QnapShareError) as error:
                error_message = str(error)
        configured = bool(state.get("ssid") and state.get("password"))
        if configured and "song_count" not in state and isinstance(state.get("_cached_items"), list):
            songs, _lyrics = _songs_from_items(state, state["_cached_items"], get_download_dir())
            state["song_count"] = len(songs)
        payload = {
            "configured": configured,
            "loading": loading,
            "folder_name": state.get("folder_name", ""),
            "song_count": state.get("song_count", 0),
            "can_manage": require_room_player(),
            "from_env": bool(state.get("_from_env")),
        }
        if error_message:
            payload["error"] = error_message
        return response(payload)

    @app.get("/api/nas/library-sync-status")
    def qnap_library_sync_status():
        with _env_share_lock:
            sync_idle = _env_sync_status.get("state") == "idle"
            persisted_status = _load_persisted_env_sync_status()
            if sync_idle and persisted_status:
                _env_sync_status.update(persisted_status)
                sync_idle = _env_sync_status.get("state") == "idle"
            stale_sync = (
                _env_sync_status.get("state") in {"starting", "loading"}
                and not _env_share_cache.get("fetching_key")
            )
        if stale_sync:
            _update_env_sync_status(state="idle", phase="queued", error="")
            sync_idle = True
        if sync_idle:
            try:
                started = start_qnap_background_sync()
                if not started:
                    _update_env_sync_status(
                        state="error",
                        phase="configuration",
                        error="Render 尚未設定 QNAP_SHARE_URL 或 QNAP_SHARE_PASSWORD。",
                        error_type="ConfigurationError",
                        completed_at=time.time(),
                    )
            except (ValueError, OSError) as error:
                _update_env_sync_status(
                    state="error",
                    phase="configuration",
                    error=str(error),
                    error_type=error.__class__.__name__,
                    completed_at=time.time(),
                )
        with _env_share_lock:
            payload = dict(_env_sync_status)
            cached_state = _env_share_cache.get("state")
            if isinstance(cached_state, dict):
                payload["folder_name"] = cached_state.get("folder_name", "")
            last_activity = payload.get("last_activity_at")
            payload["last_activity_age_seconds"] = (
                max(0, round(time.time() - float(last_activity), 1))
                if last_activity
                else None
            )
        return response(payload)

    @app.post("/api/nas/library-sync-retry-failed")
    def qnap_library_sync_retry_failed():
        share_url, password = _env_share_credentials()
        if not share_url or not password:
            return response({"error": "Render 尚未設定 QNAP 歌庫。"}, 503)
        try:
            base_url, ssid = _allowed_share_url(share_url)
        except ValueError as error:
            return response({"error": str(error)}, 400)
        cache_key = (base_url, ssid, password)
        with _env_share_lock:
            failed_offsets = list(_env_sync_status.get("failed_offsets") or [])
            busy = _env_share_cache.get("fetching_key") is not None
            has_state = (
                _env_share_cache.get("key") == cache_key
                and isinstance(_env_share_cache.get("state"), dict)
            )
        if busy:
            return response({"error": "NAS 歌庫目前正在讀取中。"}, 409)
        if not has_state or not failed_offsets:
            return response({"error": "目前沒有待重新讀取的 NAS 項目。"}, 409)
        _update_env_sync_status(
            state="loading",
            phase="recovery",
            error="",
            last_activity_at=time.time(),
        )
        threading.Thread(
            target=_run_env_failed_offset_recovery,
            args=(cache_key,),
            daemon=True,
            name="qnap-library-recovery",
        ).start()
        return response({"started": True, "failed_count": len(failed_offsets)}, 202)

    @app.post("/api/nas/library-sync-rescan")
    def qnap_library_sync_rescan():
        if not require_room_player():
            return response({"error": "只有房主可以重新掃描預設 NAS 歌庫。"}, 403)
        share_url, password = _env_share_credentials()
        if not share_url or not password:
            return response({"error": "Render 尚未設定 QNAP 歌庫。"}, 503)
        try:
            _allowed_share_url(share_url)
        except ValueError as error:
            return response({"error": str(error)}, 400)
        with _env_share_lock:
            busy = _env_share_cache.get("fetching_key") is not None
        if busy:
            return response({"error": "NAS 歌庫目前正在讀取中。"}, 409)
        try:
            started = start_qnap_background_sync(force_full_rescan=True)
        except (ValueError, OSError) as error:
            logger.exception("Unable to start a manual QNAP library rescan.")
            return response({"error": str(error)}, 503)
        if not started:
            return response({"error": "Render 尚未設定 QNAP 歌庫。"}, 503)
        return response({"started": True, "full_rescan": True}, 202)

    @app.get("/api/storage/status")
    def storage_status():
        try:
            return response(_storage_status(get_download_dir()))
        except OSError:
            return response({"error": "Render 儲存空間狀態目前無法讀取。"}, 503)

    @app.post("/api/nas/connect")
    def qnap_library_connect():
        if not require_room_player():
            return response({"error": "只有建立房間的房主可以設定 NAS 歌庫。請使用建立房間的瀏覽器重新整理頁面。"}, 403)
        payload = request.get_json(silent=True) or {}
        try:
            base_url, ssid = _allowed_share_url(payload.get("share_url", ""))
            password = payload.get("password", "")
            if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
                raise ValueError("請輸入分享連結密碼。")
            state, items = _share_folder_items(base_url, ssid, password)
            songs, _lyrics = _songs_from_items(state, items, get_download_dir())
        except ValueError as error:
            return response({"error": str(error)}, 400)
        except QnapShareError as error:
            return response({"error": str(error)}, 502)
        state["song_count"] = len(songs)
        get_state().clear()
        get_state().update(state)
        if notify_room:
            notify_room()
        return response({"folder_name": state["folder_name"], "song_count": len(songs)})

    @app.delete("/api/nas/library")
    def qnap_library_disconnect():
        if not require_room_player():
            return response({"error": "只有建立房間的房主可以解除 NAS 歌庫。"}, 403)
        get_state().clear()
        if notify_room:
            notify_room()
        return response({"configured": False})

    @app.get("/api/nas/library-songs")
    def qnap_library_songs():
        state = get_state()
        non_blocking = request.args.get("async") == "1"
        try:
            status = _ensure_room_nas_state(state, wait_timeout=1.5 if non_blocking else None)
            if not status:
                return response({"error": "尚未設定 QNAP NAS 歌庫（請在 Render Environment 設定 QNAP_SHARE_URL 與 QNAP_SHARE_PASSWORD）。"}, 409)
            if status == "loading":
                return response({
                    "loading": True,
                    "folder": {"name": "QNAP 歌庫"},
                    "songs": [],
                    "song_count": 0,
                })
            items = _all_items(state)
            songs, _lyrics = _songs_from_items(state, items, get_download_dir())
        except ValueError as error:
            return response({"error": str(error)}, 400)
        except QnapShareError as error:
            return response({"error": str(error)}, 502)
        state["song_count"] = len(songs)
        return response({
            "loading": False,
            "folder": {"name": state.get("folder_name", "QNAP 歌庫")},
            "songs": songs,
            "song_count": len(songs),
        })

    @app.get("/api/nas/library-songs/page")
    def qnap_library_songs_page():
        """Return one small NAS page so the browser can render incrementally."""
        state = get_state()
        try:
            with _env_share_lock:
                sync_idle = _env_sync_status.get("state") == "idle"
                stale_sync = (
                    _env_sync_status.get("state") in {"starting", "loading"}
                    and not _env_share_cache.get("fetching_key")
                )
            if stale_sync:
                _update_env_sync_status(state="idle", phase="queued", error="")
                sync_idle = True
            if sync_idle:
                started = start_qnap_background_sync()
                if not started:
                    return response({
                        "error": "Render 尚未設定 QNAP_SHARE_URL 或 QNAP_SHARE_PASSWORD。",
                        "sync_state": "error",
                        "sync_phase": "configuration",
                    }, 503)
            status = _ensure_room_nas_state(state, wait_timeout=1.5)
            if status == "loading":
                with _env_share_lock:
                    sync_status = dict(_env_sync_status)
                return response({
                    "loading": True,
                    "folder": {"name": "QNAP 歌庫"},
                    "songs": [],
                    "next_offset": 0,
                    "has_more": True,
                    "sync_state": sync_status.get("state"),
                    "sync_phase": sync_status.get("phase"),
                    "sync_loaded": sync_status.get("loaded", 0),
                    "sync_total": sync_status.get("total", 0),
                    "partial_error": sync_status.get("partial_error", ""),
                })
            if not status:
                return response({"error": "尚未設定 QNAP NAS 歌庫。"}, 409)
            try:
                offset = max(0, int(request.args.get("offset", "0")))
                page_size = min(20, max(1, int(request.args.get("limit", "20"))))
            except ValueError:
                return response({"error": "NAS 歌庫分頁參數無效。"}, 400)
            cached_items = state.get("_cached_items")
            with _env_share_lock:
                sync_status = dict(_env_sync_status)
            is_env_library = bool(state.get("_from_env"))
            sync_state = sync_status.get("state") if is_env_library else None
            sync_phase = sync_status.get("phase") if is_env_library else None
            sync_loaded = int(sync_status.get("loaded") or 0) if is_env_library else 0
            sync_total = int(sync_status.get("total") or 0) if is_env_library else 0
            partial_error = (
                sync_status.get("partial_error", "") if is_env_library else ""
            )
            indexed_items = []
            indexed_song_total = 0
            indexed_sync = None
            if (
                state.get("_from_env")
                and state.get("base_url")
                and state.get("ssid")
                and state.get("password") is not None
            ):
                indexed_items, indexed_song_total, indexed_sync = _read_library_index_song_page(
                    (
                        state["base_url"],
                        state["ssid"],
                        state["password"],
                    ),
                    offset,
                    page_size,
                )
            if (
                state.get("_from_env")
                and sync_state in {"starting", "loading"}
                and (
                    not isinstance(cached_items, list)
                    or (
                        offset >= indexed_song_total
                        and not indexed_items
                    )
                )
            ):
                return response({
                    "loading": True,
                    "folder": {"name": state.get("folder_name", "QNAP 歌庫")},
                    "songs": [],
                    "next_offset": offset,
                    "has_more": True,
                    "loaded_count": (
                        sync_loaded if is_env_library else len(cached_items or [])
                    ),
                    "total_count": indexed_song_total,
                    "sync_state": sync_state,
                    "sync_phase": sync_phase,
                    "sync_loaded": sync_loaded,
                    "sync_total": sync_total,
                    "partial_error": partial_error,
                })
            if indexed_sync is not None:
                raw_items = indexed_items
                total = indexed_song_total
            elif isinstance(cached_items, list) and cached_items:
                cached_songs = [
                    item
                    for item in cached_items
                    if isinstance(item, dict)
                    and os.path.splitext(
                        str(item.get("filename") or item.get("name") or "")
                    )[1].lower() == ".mp4"
                    and not int(item.get("isfolder", 0) or 0)
                ]
                raw_items = cached_songs[offset:offset + page_size]
                selected_stems = {
                    os.path.splitext(
                        str(item.get("filename") or item.get("name") or "")
                    )[0].casefold()
                    for item in raw_items
                }
                raw_items.extend(
                    item
                    for item in cached_items
                    if isinstance(item, dict)
                    and os.path.splitext(
                        str(item.get("filename") or item.get("name") or "")
                    )[1].lower() in LYRIC_EXTENSIONS
                    and os.path.splitext(
                        str(item.get("filename") or item.get("name") or "")
                    )[0].casefold() in selected_stems
                )
                total = len(cached_songs)
            else:
                result = _list_page_with_hard_timeout(
                    state,
                    offset,
                    page_size,
                    PAGE_REQUEST_TIMEOUT_SECONDS,
                )
                raw_items = [item for item in result.get("datas", []) if isinstance(item, dict)]
                try:
                    total = max(offset + len(raw_items), int(result.get("total", offset + len(raw_items))))
                except (TypeError, ValueError):
                    total = offset + len(raw_items)
            songs, _lyrics = _songs_from_items(state, raw_items, get_download_dir())
            if indexed_sync is None:
                cached_items = state.setdefault("_cached_items", [])
                cached_names = {
                    item.get("filename")
                    for item in cached_items
                    if isinstance(item, dict)
                }
                cached_items.extend(
                    item for item in raw_items
                    if item.get("filename") not in cached_names
                )
                state["_cached_at"] = time.time()
            if (
                indexed_sync is None
                and state.get("_from_env")
                and state.get("base_url")
                and state.get("ssid")
                and state.get("password") is not None
            ):
                _save_env_library_manifest(
                    (state["base_url"], state["ssid"], state["password"]),
                    state,
                )
            next_offset = offset + len(songs)
            return response({
                "loading": False,
                "folder": {"name": state.get("folder_name", "QNAP 歌庫")},
                "songs": songs,
                "next_offset": next_offset,
                "has_more": bool(raw_items) and (
                    next_offset < total or sync_state in {"starting", "loading"}
                ),
                "source_count": total,
                "loaded_count": next_offset,
                "total_count": total,
                "sync_state": sync_state,
                "sync_phase": sync_phase,
                "sync_loaded": sync_loaded,
                "sync_total": sync_total,
                "partial_error": partial_error,
            })
        except ValueError as error:
            return response({"error": str(error)}, 400)
        except QnapShareError as error:
            with _env_share_lock:
                sync_status = dict(_env_sync_status)
            return response({
                "error": str(error),
                "sync_state": sync_status.get("state"),
                "sync_phase": sync_status.get("phase"),
                "loaded_count": sync_status.get("loaded", 0),
                "total_count": sync_status.get("total", 0),
            }, 502)

    @app.get("/api/nas/library-songs/search")
    def qnap_library_songs_search():
        keyword = (
            request.args.get("q")
            or request.args.get("keyword")
            or ""
        ).strip()
        singer = request.args.get("singer", "").strip()
        language = request.args.get("language", "").strip()
        categories = [
            value.strip()
            for value in (
                request.args.getlist("category")
                or request.args.getlist("affiliation")
            )
            if value.strip()
        ]
        sort = request.args.get("sort", "newest").strip().lower()
        if (
            any(len(value) > 128 for value in (keyword, singer, language))
            or any(len(value) > 128 for value in categories)
            or len(categories) > 50
        ):
            return response({
                "error": "NAS 搜尋條件不可超過 128 個字元，歸屬篩選最多 50 項。"
            }, 400)
        try:
            length_value = request.args.get("length", "").strip()
            title_length = int(length_value) if length_value else None
            if title_length is not None and not 0 <= title_length <= 256:
                raise ValueError
            if "page" in request.args:
                page = int(request.args.get("page", "1"))
                if page < 1:
                    raise ValueError
                limit = min(100, max(1, int(request.args.get("limit", "20"))))
                offset = (page - 1) * limit
            else:
                offset = int(request.args.get("offset", "0"))
                limit = min(100, max(1, int(request.args.get("limit", "20"))))
                page = offset // limit + 1
            if offset < 0:
                raise ValueError
        except (TypeError, ValueError):
            return response({"error": "NAS 歌曲搜尋分頁或長度參數無效。"}, 400)
        if sort not in {"newest", "oldest", "title", "singer", "language"}:
            return response({"error": "NAS 歌曲搜尋排序方式無效。"}, 400)

        state = get_state()
        try:
            with _env_share_lock:
                sync_idle = _env_sync_status.get("state") == "idle"
                stale_sync = (
                    _env_sync_status.get("state") in {"starting", "loading"}
                    and not _env_share_cache.get("fetching_key")
                )
            if stale_sync:
                _update_env_sync_status(state="idle", phase="queued", error="")
                sync_idle = True
            if sync_idle:
                started = start_qnap_background_sync()
                if not started:
                    return response({
                        "error": "Render 尚未設定 QNAP_SHARE_URL 或 QNAP_SHARE_PASSWORD。",
                        "sync_state": "error",
                        "sync_phase": "configuration",
                    }, 503)
            room_status = _ensure_room_nas_state(state, wait_timeout=1.5)
            if not room_status:
                return response({"error": "尚未設定 QNAP NAS 歌庫。"}, 409)
            if room_status != "loading" and not state.get("_from_env"):
                return response({
                    "error": "完整索引搜尋目前僅支援 Render 預設 NAS 歌庫。",
                }, 409)
            if state.get("_from_env"):
                cache_key = (
                    state["base_url"],
                    state["ssid"],
                    state["password"],
                )
            else:
                share_url, password = _env_share_credentials()
                if not share_url or not password:
                    return response({
                        "error": "Render 尚未設定 QNAP_SHARE_URL 或 QNAP_SHARE_PASSWORD。",
                    }, 503)
                base_url, ssid = _allowed_share_url(share_url)
                cache_key = (base_url, ssid, password)

            indexed_sync = _read_library_index_state(cache_key)
            if not state.get("_from_env"):
                state.update((indexed_sync or {}).get("state_metadata") or {})
                state.update({
                    "base_url": cache_key[0],
                    "ssid": cache_key[1],
                    "password": cache_key[2],
                    "_from_env": True,
                })
            with _env_share_lock:
                sync_status = dict(_env_sync_status)
            persisted_status = _load_persisted_env_sync_status()
            if (
                sync_status.get("state") == "idle"
                and persisted_status
                and indexed_sync
            ):
                sync_status.update(persisted_status)
            sync_state = sync_status.get("state")
            if not indexed_sync:
                if sync_state in {"starting", "loading"}:
                    return response({
                        "loading": True,
                        "songs": [],
                        "offset": offset,
                        "page": page,
                        "limit": limit,
                        "total_count": 0,
                        "has_more": True,
                        "sync_state": sync_state,
                        "sync_phase": sync_status.get("phase"),
                        "sync_loaded": sync_status.get("loaded", 0),
                        "sync_total": sync_status.get("total", 0),
                        "partial_error": sync_status.get("partial_error", ""),
                    })
                return response({
                    "error": "NAS 歌曲索引尚未建立。",
                    "sync_state": sync_state,
                    "sync_phase": sync_status.get("phase"),
                }, 503)
            if sync_state == "idle":
                sync_state = indexed_sync.get("state", "ready")
            if sync_state == "idle":
                sync_state = "ready"
            items, total, _indexed_search_sync = _read_library_index_search(
                cache_key,
                keyword=keyword,
                singer=singer,
                language=language,
                category=categories,
                title_length=title_length,
                sort=sort,
                offset=offset,
                limit=limit,
            )
            songs, _lyrics = _songs_from_items(state, items, get_download_dir())
            loaded = int(sync_status.get("loaded") or indexed_sync.get("loaded") or 0)
            sync_total = int(sync_status.get("total") or indexed_sync.get("total") or 0)
            partial_error = sync_status.get("partial_error", "")
            return response({
                "loading": False,
                "songs": songs,
                "offset": offset,
                "page": page,
                "limit": limit,
                "total_count": total,
                "has_more": offset + len(songs) < total,
                "sync_state": sync_state,
                "sync_phase": sync_status.get("phase"),
                "sync_loaded": loaded,
                "sync_total": sync_total,
                "partial_error": partial_error,
            })
        except ValueError as error:
            return response({"error": str(error)}, 400)
        except QnapShareError as error:
            return response({"error": str(error)}, 502)

    @app.get("/api/nas/library-songs/download-status")
    def qnap_library_download_status():
        job_id = request.args.get("job_id", "")
        with download_jobs_lock:
            job = dict(download_jobs.get(job_id, {}))
        if not job:
            return response({"error": "下載工作不存在或已過期。"}, 404)
        return response(job)

    @app.post("/api/nas/library-songs/download")
    def qnap_library_download():
        payload = request.get_json(silent=True) or {}
        job_id = payload.get("_job_id")
        if payload.get("_worker_token") != worker_token:
            file_id = payload.get("file_id")
            if not isinstance(file_id, str) or not re.fullmatch(r"qnap_[a-f0-9]{64}", file_id):
                return response({"error": "歌曲識別碼無效。"}, 400)
            session_data = dict(session)
            with download_jobs_lock:
                # 同首歌曲沿用進行中的工作，避免重複下載及覆寫快取檔。
                active_job_id = active_download_jobs.get(file_id)
                active_job = download_jobs.get(active_job_id, {})
                if active_job.get("status") == "downloading":
                    return response({
                        "status": "downloading",
                        "job_id": active_job_id,
                    }, 202)
                job_id = uuid.uuid4().hex[:24]
                download_jobs[job_id] = {
                    "status": "downloading",
                    "phase": "preparing",
                    "progress": 0,
                }
                active_download_jobs[file_id] = job_id
            set_download_stage(job_id, "queued", phase="preparing", progress=0)

            def run_download_job():
                try:
                    set_download_stage(job_id, "initializing_nas_state", phase="locating")
                    with app.test_request_context(
                        "/api/nas/library-songs/download",
                        method="POST",
                        json={
                            "file_id": file_id,
                            "_worker_token": worker_token,
                            "_job_id": job_id,
                        },
                    ):
                        session.update(session_data)
                        result = qnap_library_download()
                        body = result.get_json(silent=True) or {}
                        status_code = result.status_code or 200
                    with download_jobs_lock:
                        if status_code >= 400:
                            download_jobs[job_id].update(
                                status="error",
                                phase="failed",
                                stage="failed",
                                error=body.get("error", "歌曲下載失敗。"),
                            )
                        else:
                            download_jobs[job_id].update(
                                status="completed",
                                phase="complete",
                                stage="complete",
                                progress=100,
                                filename=body.get("filename", ""),
                            )
                    terminal_stage = "failed" if status_code >= 400 else "complete"
                    logger.info(
                        "QNAP download job %s stage=%s",
                        job_id,
                        terminal_stage,
                    )
                except Exception:
                    logger.exception("QNAP background download job %s failed.", job_id)
                    with download_jobs_lock:
                        download_jobs[job_id].update(
                            status="error",
                            phase="failed",
                            stage="failed",
                            error="歌曲下載失敗，請稍後再試。",
                        )
                finally:
                    with qnap_cache_lock:
                        qnap_cache_reservations.pop(job_id, None)
                    with download_jobs_lock:
                        if active_download_jobs.get(file_id) == job_id:
                            active_download_jobs.pop(file_id, None)

            try:
                threading.Thread(target=run_download_job, daemon=True).start()
            except RuntimeError:
                with download_jobs_lock:
                    download_jobs[job_id].update(
                        status="error",
                        phase="failed",
                        stage="failed",
                        error="目前無法啟動歌曲下載工作，請稍後重試。",
                    )
                    if active_download_jobs.get(file_id) == job_id:
                        active_download_jobs.pop(file_id, None)
                return response({"error": "目前無法啟動歌曲下載工作，請稍後重試。"}, 503)
            return response({"status": "downloading", "job_id": job_id}, 202)

        file_id = payload.get("file_id")
        state = get_state()
        try:
            set_download_stage(
                job_id,
                "initializing_nas_state",
                phase="locating",
            )
            room_status = _ensure_room_nas_state(state, wait_timeout=1.5)
            if room_status == "loading":
                return response({
                    "error": "NAS 歌庫仍在初始化，下載工作已停止；請稍後重試。",
                }, 503)
            if not room_status:
                return response({"error": "尚未設定 QNAP NAS 歌庫。"}, 409)
        except ValueError as error:
            return response({"error": str(error)}, 400)
        except QnapShareError as error:
            return response({"error": str(error)}, 502)
        if not isinstance(file_id, str) or not re.fullmatch(r"qnap_[a-f0-9]{64}", file_id):
            return response({"error": "歌曲識別碼無效。"}, 400)
        set_download_stage(job_id, "checking_cached_song")
        download_dir = get_download_dir()
        try:
            with qnap_cache_lock:
                existing = next((item for item in drive_core.get_downloaded_song_records(download_dir)
                                 if item["file_id"] == file_id), None)
            if existing:
                return response({"status": "downloaded", "filename": existing["filename"]})

            used_cached_items = (
                isinstance(state.get("_cached_items"), list)
                and (time.time() - float(state.get("_cached_at") or 0)) < ITEMS_CACHE_TTL_SECONDS
            )
            set_download_stage(job_id, "reading_library_index")
            items = _all_items(state)
            set_download_stage(job_id, "matching_library_song")
            songs, lyrics_by_stem = _songs_from_items(state, items, download_dir)
            song = next((item for item in songs if item["id"] == file_id), None)
            # SmartShare 索引未命中時，不在下載工作中觸發整份歌庫重掃。
            if (
                not song
                and used_cached_items
                and not _is_smartshare_url(state.get("base_url"))
            ):
                set_download_stage(job_id, "refreshing_library_listing")
                items = _all_items(state, refresh=True)
                used_cached_items = False
                set_download_stage(job_id, "matching_library_song")
                songs, lyrics_by_stem = _songs_from_items(state, items, download_dir)
                song = next((item for item in songs if item["id"] == file_id), None)
            if not song:
                return response({"error": "這首歌不在目前房間的 NAS 歌庫中。"}, 404)

            lyric_stem = os.path.splitext(song["name"])[0].casefold()
            paired_lyrics = lyrics_by_stem.get(lyric_stem, [])
            smartshare_filenames = [song["name"]]
            if paired_lyrics:
                smartshare_filenames.append(paired_lyrics[0]["filename"])
            if _is_smartshare_url(state.get("base_url")) and any(
                not any(
                    item.get("filename") == filename and item.get("download_url")
                    for item in items
                    if isinstance(item, dict)
                )
                for filename in smartshare_filenames
            ):
                # SmartShare URLs are short-lived and are intentionally absent from snapshots.
                set_download_stage(job_id, "refreshing_smartshare_download_urls")
                refreshed_items = _refresh_smartshare_download_items(
                    state,
                    smartshare_filenames,
                    items,
                )
                items.extend(refreshed_items.values())
                used_cached_items = False

            set_download_stage(job_id, "preparing_cache_paths")
            os.makedirs(download_dir, exist_ok=True)
            download_urls = {
                item.get("filename"): item.get("download_url")
                for item in items
                if isinstance(item, dict) and item.get("filename")
            }
            saved_song = file_id + ".mp4"
            song_path = os.path.join(download_dir, saved_song)
            expected_song_size = int(song.get("size") or 0) or None
            lyric_options = lyrics_by_stem.get(os.path.splitext(song["name"])[0].casefold(), [])
            lyric = lyric_options[0] if lyric_options else None
            expected_lyric_size = int(lyric.get("filesize") or 0) if lyric else 0
            reserved_song_size = expected_song_size or 0
            reserved_lyric_size = expected_lyric_size
            cache_reservation_bytes = reserved_song_size + reserved_lyric_size
            total_download_size = cache_reservation_bytes

            # Reserve estimated space atomically; concurrent songs then account for one another.
            set_download_stage(job_id, "checking_cache_space")
            with qnap_cache_lock:
                existing = next((item for item in drive_core.get_downloaded_song_records(download_dir)
                                 if item["file_id"] == file_id), None)
                if existing:
                    return response({"status": "downloaded", "filename": existing["filename"]})
                reserved_bytes = sum(qnap_cache_reservations.values())
                _prepare_cache_space(
                    download_dir,
                    cache_reservation_bytes,
                    reserved_bytes=reserved_bytes,
                )
                qnap_cache_reservations[job_id] = cache_reservation_bytes

            def report_progress(downloaded, total_size=None, offset=0):
                nonlocal total_download_size
                nonlocal reserved_song_size, reserved_lyric_size, cache_reservation_bytes
                if total_size:
                    if offset:
                        reserved_lyric_size = max(reserved_lyric_size, total_size)
                    else:
                        reserved_song_size = max(reserved_song_size, total_size)
                    cache_reservation_bytes = max(
                        cache_reservation_bytes,
                        reserved_song_size + reserved_lyric_size,
                    )
                    total_download_size = max(total_download_size, cache_reservation_bytes)
                # 隨下載進度扣減預留量，讓其他歌曲安全共用可用空間。
                with qnap_cache_lock:
                    if job_id in qnap_cache_reservations:
                        qnap_cache_reservations[job_id] = max(
                            0,
                            cache_reservation_bytes - offset - downloaded,
                        )
                if not job_id or total_download_size <= 0:
                    return
                progress = min(99, int(
                    (offset + downloaded) * 100 / total_download_size
                ))
                stage = "downloading_paired_lyrics" if offset else "downloading_song"
                set_download_stage(
                    job_id,
                    stage,
                    phase="downloading",
                    progress=progress,
                )

            if job_id:
                set_download_stage(
                    job_id,
                    "connecting_song",
                    phase="connecting",
                    progress=1,
                )
            try:
                downloaded_song_size = _download_file(
                    state,
                    song["name"],
                    song_path,
                    expected_song_size,
                    download_url=download_urls.get(song["name"]),
                    progress_callback=report_progress,
                )
            except QnapShareError:
                if not _is_smartshare_url(state.get("base_url")):
                    raise
                set_download_stage(job_id, "refreshing_song_url_after_download_error")
                refreshed_items = _refresh_smartshare_download_items(
                    state,
                    smartshare_filenames,
                    items,
                )
                download_urls.update({
                    filename: item.get("download_url")
                    for filename, item in refreshed_items.items()
                })
                used_cached_items = False
                set_download_stage(
                    job_id,
                    "reconnecting_song",
                    phase="connecting",
                )
                downloaded_song_size = _download_file(
                    state,
                    song["name"],
                    song_path,
                    expected_song_size,
                    download_url=download_urls.get(song["name"]),
                    progress_callback=report_progress,
                )

            if expected_song_size is None:
                expected_song_size = downloaded_song_size
                reserved_song_size = max(reserved_song_size, downloaded_song_size)
                cache_reservation_bytes = max(
                    cache_reservation_bytes,
                    reserved_song_size + reserved_lyric_size,
                )
                total_download_size = max(total_download_size, cache_reservation_bytes)
            subtitle_saved_as = None
            if lyric_options:
                set_download_stage(job_id, "preparing_paired_lyrics")
                lyric_extension = os.path.splitext(lyric["filename"])[1].lower()
                subtitle_saved_as = file_id + lyric_extension
                lyric_path = os.path.join(download_dir, subtitle_saved_as)
                expected_lyric_size = int(lyric.get("filesize") or 0) or None
                try:
                    set_download_stage(job_id, "connecting_paired_lyrics")
                    _download_file(
                        state,
                        lyric["filename"],
                        lyric_path,
                        expected_lyric_size,
                        download_url=lyric.get("download_url") or download_urls.get(lyric["filename"]),
                        progress_callback=lambda downloaded, total_size=None: report_progress(
                            downloaded, total_size, downloaded_song_size
                        ),
                    )
                except QnapShareError as error:
                    if _is_smartshare_url(state.get("base_url")):
                        try:
                            set_download_stage(
                                job_id,
                                "refreshing_paired_lyrics_url",
                            )
                            refreshed_items = _refresh_smartshare_download_items(
                                state,
                                [lyric["filename"]],
                                items,
                            )
                            refreshed_lyric = refreshed_items[lyric["filename"]]
                            set_download_stage(
                                job_id,
                                "reconnecting_paired_lyrics",
                            )
                            _download_file(
                                state,
                                lyric["filename"],
                                lyric_path,
                                expected_lyric_size,
                                download_url=refreshed_lyric.get("download_url"),
                                progress_callback=lambda downloaded, total_size=None: report_progress(
                                    downloaded, total_size, downloaded_song_size
                                ),
                            )
                            error = None
                        except QnapShareError as refresh_error:
                            error = refresh_error
                    if error is not None:
                        # 保留配對歌詞失敗原因，並移除未完成的歌曲檔案。
                        try:
                            os.remove(song_path)
                        except OSError:
                            pass
                        raise QnapShareError(
                            "配對歌詞下載失敗：" + str(error)
                        ) from error
                except (OSError, ValueError, TypeError) as error:
                    try:
                        os.remove(song_path)
                    except OSError:
                        pass
                    raise QnapShareError(
                        "配對歌詞檔案處理失敗：" + str(error)
                    ) from error

            item = {"id": file_id, "name": song["name"], "size": expected_song_size}
            set_download_stage(job_id, "saving_download_metadata")
            with qnap_cache_lock:
                drive_core._save_downloaded_item(
                    download_dir,
                    "qnap:" + state["ssid"],
                    item,
                    expected_song_size,
                    saved_song,
                    subtitle_saved_as,
                )
                metadata = drive_core._load_download_metadata(download_dir)
                public_filename = drive_core._safe_public_filename(song["name"], file_id)
                if any(
                    other_id != file_id and other.get("public_filename") == public_filename
                    for other_id, other in metadata.items()
                ):
                    stem = os.path.splitext(public_filename)[0]
                    public_filename = drive_core._safe_public_filename(
                        "{0}--{1}.mp4".format(stem, file_id[:8]), file_id,
                    )
                metadata[file_id]["public_filename"] = public_filename
                drive_core._save_download_metadata(download_dir, metadata)
        except QnapShareError as error:
            return response({"error": str(error)}, 502)
        except drive_core.DriveStorageError as error:
            return response({"error": str(error)}, 507)
        except (OSError, ValueError, TypeError):
            return response({"error": "Render 暫存空間無法完成歌曲下載。"}, 507)
        if notify_room:
            notify_room()
        return response({"status": "downloaded", "filename": metadata[file_id]["public_filename"]})

    def _warm_env_nas_cache():
        try:
            started = start_qnap_background_sync()
            if not started:
                logger.warning(
                    "QNAP background sync was not started because credentials are missing."
                )
        except Exception:
            logger.exception("QNAP background sync startup failed.")

    threading.Thread(target=_warm_env_nas_cache, daemon=True).start()
