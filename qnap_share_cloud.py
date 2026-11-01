"""Read-only QNAP File Station share links for cloud KTV rooms."""

import hashlib
import errno
import json
import logging
import os
import re
import shutil
import ssl
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

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
SYNC_PAGE_SIZE = 1
REQUEST_TIMEOUT_SECONDS = 25
PAGE_REQUEST_TIMEOUT_SECONDS = 12
DOWNLOAD_TIMEOUT_SECONDS = 180
# Listing a large SmartShare folder may require several paginated requests.
# Keep the background fetch alive long enough for Render's variable network
# latency instead of restarting it while QNAP is still responding.
ENV_FETCH_TIMEOUT_SECONDS = 180
QNAP_CACHE_RESERVE_BYTES = 128 * 1024 * 1024
LYRIC_EXTENSIONS = (".vtt", ".srt", ".lrc")

# _ensure_room_nas_state may start a fetch while it is inspecting the cache
# under this condition. A re-entrant lock keeps that operation atomic without
# deadlocking when the fetch starter checks the same condition.
_env_share_lock = threading.Condition(threading.RLock())
_env_share_cache = {}
_env_sync_status = {
    "state": "idle",
    "phase": "idle",
    "loaded": 0,
    "total": 0,
    "error": "",
    "error_type": "",
    "started_at": None,
    "completed_at": None,
}
ENV_LIBRARY_MANIFEST_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "drive_downloads",
    "qnap_shared",
)


class QnapShareError(Exception):
    """A safe error that can be shown to a room member."""


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


def _prepare_cache_space(download_dir, required_bytes):
    """Evict the least recently used indexed NAS songs before a new download."""
    if required_bytes < 0:
        raise QnapShareError("歌曲大小無效，無法準備 Render 儲存空間。")
    os.makedirs(download_dir, exist_ok=True)
    usage, records = _cache_usage(download_dir)
    disk = shutil.disk_usage(download_dir)
    limit = _cache_limit_bytes()
    reserve = QNAP_CACHE_RESERVE_BYTES

    def enough():
        return (
            (not limit or usage + required_bytes <= limit)
            and disk.free >= required_bytes + reserve
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
                    "items": state.get("_cached_items", []),
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
        _env_share_lock.notify_all()


def _safe_sync_error(exc, password):
    message = str(exc).strip() or exc.__class__.__name__
    if password:
        message = message.replace(password, "[redacted]")
    return message


def _run_env_library_sync(cache_key, notify_update=None):
    """Fetch the environment NAS index page by page and publish each page."""
    base_url, ssid, password = cache_key
    started_at = time.time()
    _update_env_sync_status(
        state="starting",
        phase="connecting",
        loaded=0,
        total=0,
        error="",
        error_type="",
        started_at=started_at,
        completed_at=None,
    )
    logger.info("QNAP background sync started for share %s.", ssid)
    try:
        state, _items = _share_folder_items(
            base_url, ssid, password, load_items=False
        )
        state["_from_env"] = True
        state["_cached_items"] = []
        state["_cached_at"] = time.time()
        with _env_share_lock:
            _env_share_cache["key"] = cache_key
            _env_share_cache["state"] = state
        offset = 0
        total = 0
        _update_env_sync_status(
            state="loading",
            phase="listing",
            loaded=0,
            total=0,
            error="",
            error_type="",
        )
        while True:
            result = _list_page(state, offset, SYNC_PAGE_SIZE)
            page = [item for item in result.get("datas", []) if isinstance(item, dict)]
            try:
                total = max(total, int(result.get("total", offset + len(page))))
            except (TypeError, ValueError):
                total = max(total, offset + len(page))
            state["_cached_items"].extend(page)
            state["_cached_at"] = time.time()
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
            _update_env_sync_status(
                state="loading",
                phase="listing",
                loaded=offset,
                total=total,
            )
            if not page or offset >= total:
                break
            if offset > 10000:
                raise QnapShareError("QNAP 歌曲清單超過單次讀取上限。")
        completed_at = time.time()
        _update_env_sync_status(
            state="ready",
            phase="complete",
            loaded=offset,
            total=total,
            error="",
            error_type="",
            completed_at=completed_at,
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
            _env_share_lock.notify_all()
        _update_env_sync_status(
            state="error",
            phase="failed",
            error=error_message,
            error_type=exc.__class__.__name__,
            completed_at=time.time(),
        )


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


def start_qnap_background_sync(notify_update=None):
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
    _update_env_sync_status(
        state="starting",
        phase="queued",
        loaded=0,
        total=0,
        error="",
        error_type="",
        started_at=time.time(),
        completed_at=None,
    )
    threading.Thread(
        target=_run_env_library_sync,
        args=(cache_key, notify_update),
        daemon=True,
        name="qnap-library-sync",
    ).start()
    return True


def _all_items(state, refresh=False):
    cached_items = state.get("_cached_items")
    cached_at = state.get("_cached_at") or 0
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
        items.extend(item for item in page if isinstance(item, dict))
        if not page or len(items) >= total:
            break
        start += len(page)
        if start > 10000:
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
    with _env_share_lock:
        if _env_share_cache.get("key") == cache_key and isinstance(_env_share_cache.get("state"), dict):
            cached_state = _env_share_cache["state"]
            state.clear()
            state.update({
                key: (list(value) if isinstance(value, list) else value)
                for key, value in cached_state.items()
            })
            return "ready"

        manifest_state = _load_env_library_manifest(cache_key)
        if manifest_state is not None:
            manifest_state["base_url"] = base_url
            manifest_state["ssid"] = ssid
            manifest_state["password"] = password
            manifest_state["_from_env"] = True
            _env_share_cache["key"] = cache_key
            _env_share_cache["state"] = manifest_state
            state.clear()
            state.update({
                key: (list(value) if isinstance(value, list) else value)
                for key, value in manifest_state.items()
            })
            return "ready"

        if wait_timeout is not None:
            fetching_at = float(_env_share_cache.get("fetching_at") or 0)
            if (
                _env_share_cache.get("fetching_key") == cache_key
                and fetching_at
                and time.time() - fetching_at >= ENV_FETCH_TIMEOUT_SECONDS
            ):
                _env_share_cache["fetching_key"] = None
                _env_share_cache.pop("fetching_at", None)
                _env_share_cache["error"] = QnapShareError(
                    "QNAP 歌庫連線逾時，請確認 Render 可連線至 myQNAPcloud，"
                    "以及分享網址與密碼是否正確。"
                )
                _env_share_cache["error_key"] = cache_key
                _env_share_cache["error_at"] = time.time()
                _env_share_lock.notify_all()
            if _env_share_cache.get("fetching_key") != cache_key:
                if (
                    _env_share_cache.get("error_key") == cache_key
                    and _env_share_cache.get("error") is not None
                    and (time.time() - float(_env_share_cache.get("error_at") or 0)) < 8
                ):
                    raise _env_share_cache["error"]
                _start_env_share_fetch_async(cache_key)
            if wait_timeout > 0:
                _env_share_lock.wait(timeout=wait_timeout)
            if _env_share_cache.get("key") == cache_key and isinstance(_env_share_cache.get("state"), dict):
                cached_state = _env_share_cache["state"]
                state.clear()
                state.update({
                    key: (list(value) if isinstance(value, list) else value)
                    for key, value in cached_state.items()
                })
                return "ready"
            if (
                _env_share_cache.get("error_key") == cache_key
                and _env_share_cache.get("fetching_key") != cache_key
                and _env_share_cache.get("error") is not None
            ):
                raise _env_share_cache["error"]
            return "loading"

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
                               notify_room=None):
    """Register host-managed, room-shared QNAP share library endpoints."""
    download_jobs = {}
    download_jobs_lock = threading.Lock()

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
            payload = dict(_env_sync_status)
            cached_state = _env_share_cache.get("state")
            if isinstance(cached_state, dict):
                payload["loaded"] = len(cached_state.get("_cached_items") or [])
                payload["folder_name"] = cached_state.get("folder_name", "")
        return response(payload)

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
            status = _ensure_room_nas_state(state, wait_timeout=1.5)
            if status == "loading":
                return response({
                    "loading": True,
                    "folder": {"name": "QNAP 歌庫"},
                    "songs": [],
                    "next_offset": 0,
                    "has_more": True,
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
                sync_state = _env_sync_status.get("state")
                sync_total = int(_env_sync_status.get("total") or 0)
            if (
                state.get("_from_env")
                and sync_state in {"starting", "loading"}
                and (not isinstance(cached_items, list) or offset >= len(cached_items))
            ):
                return response({
                    "loading": True,
                    "folder": {"name": state.get("folder_name", "QNAP 歌庫")},
                    "songs": [],
                    "next_offset": offset,
                    "has_more": True,
                    "loaded_count": len(cached_items or []),
                    "total_count": sync_total,
                })
            if isinstance(cached_items, list) and cached_items:
                raw_items = [
                    item for item in cached_items[offset:offset + page_size]
                    if isinstance(item, dict)
                ]
                total = len(cached_items)
            else:
                result = _list_page(
                    state,
                    offset,
                    page_size,
                    timeout=PAGE_REQUEST_TIMEOUT_SECONDS,
                )
                raw_items = [item for item in result.get("datas", []) if isinstance(item, dict)]
                try:
                    total = max(offset + len(raw_items), int(result.get("total", offset + len(raw_items))))
                except (TypeError, ValueError):
                    total = offset + len(raw_items)
            songs, _lyrics = _songs_from_items(state, raw_items, get_download_dir())
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
                state.get("_from_env")
                and state.get("base_url")
                and state.get("ssid")
                and state.get("password") is not None
            ):
                _save_env_library_manifest(
                    (state["base_url"], state["ssid"], state["password"]),
                    state,
                )
            next_offset = offset + len(raw_items)
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
        if not payload.get("_async_worker"):
            job_id = hashlib.sha256(
                f"{time.time_ns()}:{session.get('room_code', '')}".encode()
            ).hexdigest()[:24]
            session_data = dict(session)
            with download_jobs_lock:
                download_jobs[job_id] = {"status": "downloading", "progress": 0}

            def run_download_job():
                try:
                    with app.test_request_context(
                        "/api/nas/library-songs/download",
                        method="POST",
                        json={
                            "file_id": payload.get("file_id"),
                            "_async_worker": True,
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
                                error=body.get("error", "歌曲下載失敗。"),
                            )
                        else:
                            download_jobs[job_id].update(
                                status="completed",
                                progress=100,
                                filename=body.get("filename", ""),
                            )
                except Exception:
                    with download_jobs_lock:
                        download_jobs[job_id].update(
                            status="error",
                            error="歌曲下載失敗，請稍後再試。",
                        )

            threading.Thread(target=run_download_job, daemon=True).start()
            return response({"status": "downloading", "job_id": job_id}, 202)

        file_id = payload.get("file_id")
        state = get_state()
        try:
            if not _ensure_room_nas_state(state):
                return response({"error": "尚未設定 QNAP NAS 歌庫。"}, 409)
        except ValueError as error:
            return response({"error": str(error)}, 400)
        except QnapShareError as error:
            return response({"error": str(error)}, 502)
        if not isinstance(file_id, str) or not re.fullmatch(r"qnap_[a-f0-9]{64}", file_id):
            return response({"error": "歌曲識別碼無效。"}, 400)
        download_dir = get_download_dir()
        try:
            with drive_core._download_lock:
                existing = next((item for item in drive_core.get_downloaded_song_records(download_dir)
                                 if item["file_id"] == file_id), None)
                if existing:
                    return response({"status": "downloaded", "filename": existing["filename"]})

                used_cached_items = (
                    isinstance(state.get("_cached_items"), list)
                    and (time.time() - float(state.get("_cached_at") or 0)) < ITEMS_CACHE_TTL_SECONDS
                )
                items = _all_items(state)
                songs, lyrics_by_stem = _songs_from_items(state, items, download_dir)
                song = next((item for item in songs if item["id"] == file_id), None)
                if not song and used_cached_items:
                    items = _all_items(state, refresh=True)
                    used_cached_items = False
                    songs, lyrics_by_stem = _songs_from_items(state, items, download_dir)
                    song = next((item for item in songs if item["id"] == file_id), None)
                if not song:
                    return response({"error": "這首歌不在目前房間的 NAS 歌庫中。"}, 404)

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
                total_download_size = (expected_song_size or 0) + expected_lyric_size

                def report_progress(downloaded, total_size=None, offset=0):
                    nonlocal total_download_size
                    if total_size and total_download_size <= offset:
                        total_download_size = offset + total_size
                    if not job_id or total_download_size <= 0:
                        return
                    progress = min(99, int(
                        (offset + downloaded) * 100 / total_download_size
                    ))
                    with download_jobs_lock:
                        if job_id in download_jobs:
                            download_jobs[job_id]["progress"] = progress

                _prepare_cache_space(
                    download_dir,
                    (expected_song_size or 0) + expected_lyric_size,
                )
                try:
                    _download_file(
                        state,
                        song["name"],
                        song_path,
                        expected_song_size,
                        download_url=download_urls.get(song["name"]),
                        progress_callback=report_progress,
                    )
                except QnapShareError:
                    if not used_cached_items or not _is_smartshare_url(state.get("base_url")):
                        raise
                    items = _all_items(state, refresh=True)
                    used_cached_items = False
                    songs, lyrics_by_stem = _songs_from_items(state, items, download_dir)
                    download_urls = {
                        item.get("filename"): item.get("download_url")
                        for item in items
                        if isinstance(item, dict) and item.get("filename")
                    }
                    _download_file(
                        state,
                        song["name"],
                        song_path,
                        expected_song_size,
                        download_url=download_urls.get(song["name"]),
                        progress_callback=report_progress,
                    )

                subtitle_saved_as = None
                if lyric_options:
                    lyric_extension = os.path.splitext(lyric["filename"])[1].lower()
                    subtitle_saved_as = file_id + lyric_extension
                    lyric_path = os.path.join(download_dir, subtitle_saved_as)
                    expected_lyric_size = int(lyric.get("filesize") or 0) or None
                    try:
                        _download_file(
                            state,
                            lyric["filename"],
                            lyric_path,
                            expected_lyric_size,
                            download_url=lyric.get("download_url") or download_urls.get(lyric["filename"]),
                            progress_callback=lambda downloaded, total_size=None: report_progress(
                                downloaded, total_size, expected_song_size or 0
                            ),
                        )
                    except Exception:
                        try:
                            os.remove(song_path)
                        except OSError:
                            pass
                        raise

                item = {"id": file_id, "name": song["name"], "size": expected_song_size}
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
        share_url, password = _env_share_credentials()
        if not share_url or not password:
            return
        try:
            _ensure_room_nas_state({})
        except Exception:
            pass

    if all(_env_share_credentials()):
        threading.Thread(target=_warm_env_nas_cache, daemon=True).start()
