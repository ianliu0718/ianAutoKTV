"""Local-only OAuth, Google Picker, and Drive listing prototype."""

import base64
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from functools import wraps

from flask import jsonify, render_template, request

import google_drive_account as drive_account


DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DRIVE_DOWNLOAD_METADATA_FILENAME = "drive_downloads.json"
DRIVE_RATE_LIMIT_RETRIES = 5
DRIVE_TRANSIENT_RETRIES = 3
OAUTH_ERROR_HINTS = {
    "invalid_grant": "授權碼已失效、已使用，或 PKCE／回呼 URI 不相符。",
    "invalid_client": "OAuth Client ID 無效，或與建立授權碼的用戶端不相符。",
    "invalid_request": "Google 判定 OAuth 權杖交換參數不完整或格式錯誤。",
    "unauthorized_client": "此 OAuth 用戶端不允許目前的授權流程。",
    "unsupported_grant_type": "Google 不支援目前送出的 OAuth grant type。",
}
FLOW_TIMEOUT_SECONDS = 600
CALLBACK_TIMEOUT_SECONDS = 300
FOLDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
OAUTH_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
OAUTH_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b(code_verifier|client_secret|access_token|refresh_token|"
    r"authorization_code|client_id|redirect_uri|code)\b\s*[:=]\s*[^,\s;]+"
)

_flow_lock = threading.Lock()
_download_lock = threading.Lock()
_flows = {}


class OAuthFlowError(Exception):
    """A safe, user-displayable OAuth failure without credential contents."""


class DriveApiError(Exception):
    """A Drive API failure represented by its HTTP status only."""

    def __init__(self, status_code, reason="", retry_after=None):
        self.status_code = status_code
        self.reason = reason if isinstance(reason, str) else ""
        self.retry_after = retry_after if isinstance(retry_after, (int, float)) else None
        super().__init__("Google Drive API request failed.")


class DriveStorageError(Exception):
    """A safe local storage failure without exposing filesystem details."""


class DriveDownloadIntegrityError(Exception):
    """The downloaded media size does not match Drive metadata."""


def _drive_api_error_from_http(error):
    """Convert a Google HTTP error to safe retry metadata without retaining its body."""
    reason = ""
    try:
        payload = json.loads(error.read().decode("utf-8"))
        errors = payload.get("error", {}).get("errors", [])
        if errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason", "")
        if not reason and isinstance(payload.get("error"), dict):
            reason = payload["error"].get("status", "")
    except (AttributeError, UnicodeDecodeError, ValueError, TypeError):
        pass
    retry_after = None
    try:
        retry_value = error.headers.get("Retry-After") if error.headers else None
        retry_after = float(retry_value) if retry_value is not None else None
    except (AttributeError, TypeError, ValueError):
        retry_after = None
    return DriveApiError(error.code, reason, retry_after)


def _retry_limit(error):
    """Return the allowed retry count for a safe, transient Drive failure."""
    if error.status_code == 429:
        return DRIVE_RATE_LIMIT_RETRIES
    if error.status_code == 403 and error.reason in {
        "rateLimitExceeded", "userRateLimitExceeded"
    }:
        return DRIVE_RATE_LIMIT_RETRIES
    if error.status_code == 0 or 500 <= error.status_code <= 599:
        return DRIVE_TRANSIENT_RETRIES
    return 0


def _retry_drive_request(error, attempt):
    """Sleep with bounded exponential backoff when the failure is retryable."""
    limit = _retry_limit(error)
    if attempt >= limit:
        return False
    delay = error.retry_after if error.retry_after is not None else min(2 ** attempt, 32)
    # Keep jitter small and local; it prevents several KTV hosts retrying together.
    delay += secrets.randbelow(250) / 1000
    time.sleep(max(0, min(delay, 60)))
    return True


def _drive_error_message(error, action="操作"):
    """Map Drive status to an actionable message without exposing response details."""
    if error.status_code == 401:
        return "Google 授權已失效，請由 KTV 主機重新登入。"
    if error.status_code == 403 and error.reason == "dailyLimitExceeded":
        return "Google Drive 今日 API 用量已達上限，請稍後再試。"
    if error.status_code == 403 and error.reason in {
        "rateLimitExceeded", "userRateLimitExceeded"
    }:
        return "Google Drive 暫時限流，請稍後再試。"
    if error.status_code == 403:
        return "沒有讀取此 Google Drive 檔案的權限。"
    if error.status_code == 404:
        return "歌曲或資料夾已不存在，請重新整理歌庫。"
    if error.status_code == 429:
        return "Google Drive 要求稍後再試。"
    if error.status_code == 0 or 500 <= error.status_code <= 599:
        return "Google Drive 網路服務暫時無法使用，請稍後重試。"
    return "Google Drive {0} 失敗，請確認授權、檔案權限與網路後重試。".format(action)


def _download_metadata_path(download_dir):
    """Return the persistent mapping path kept beside downloaded Drive media."""
    return os.path.join(download_dir, DRIVE_DOWNLOAD_METADATA_FILENAME)


def _load_download_metadata(download_dir):
    """Load valid file-ID mappings without allowing malformed metadata to escape."""
    path = _download_metadata_path(download_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        file_id: item
        for file_id, item in payload.items()
        if FOLDER_ID_PATTERN.fullmatch(file_id)
        and isinstance(item, dict)
        and isinstance(item.get("saved_as"), str)
        and os.path.basename(item["saved_as"]) == item["saved_as"]
        and item["saved_as"].lower().endswith(".mp4")
        and (
            not item.get("public_filename")
            or (
                isinstance(item.get("public_filename"), str)
                and os.path.basename(item["public_filename"]) == item["public_filename"]
                and item["public_filename"].lower().endswith(".mp4")
            )
        )
    }


def _save_download_metadata(download_dir, metadata):
    """Atomically save Drive source metadata, never exposing credentials."""
    path = _download_metadata_path(download_dir)
    temporary_path = None
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=".drive-downloads-",
            suffix=".tmp",
            dir=download_dir,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            json.dump(metadata, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, TypeError, ValueError):
        raise DriveStorageError("KTV 主機無法保存歌曲來源資訊。") from None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                pass


def _safe_public_filename(name, file_id):
    """Keep a valid Drive name for the existing KTV filename-based client API."""
    candidate = os.path.basename(str(name).replace("\\", "/"))
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", candidate).rstrip(" .")
    if not candidate or candidate in {".", ".."} or not candidate.lower().endswith(".mp4"):
        return hashlib.sha256(file_id.encode("utf-8")).hexdigest() + ".mp4"
    return candidate


def get_downloaded_song_records(download_dir):
    """Return only indexed, existing Drive media for the KTV playback layer."""
    records = []
    for file_id, item in _load_download_metadata(download_dir).items():
        saved_as = item["saved_as"]
        if not os.path.isfile(os.path.join(download_dir, saved_as)):
            continue
        records.append({
            "file_id": file_id,
            "filename": item.get("public_filename") or saved_as,
            "saved_as": saved_as,
            "subtitle_saved_as": item.get("subtitle_saved_as", ""),
            "name": item.get("name", ""),
            "size": item.get("size"),
            "modifiedTime": item.get("modifiedTime", ""),
            "downloaded_at": item.get("downloaded_at"),
        })
    return records


def get_downloaded_song_path(download_dir, filename):
    """Resolve a public filename only through trusted persisted metadata."""
    if not isinstance(filename, str) or os.path.basename(filename) != filename:
        return None
    for record in get_downloaded_song_records(download_dir):
        if filename in {record["filename"], record["saved_as"]}:
            return os.path.join(download_dir, record["saved_as"])
    return None


def get_downloaded_subtitle_path(download_dir, filename):
    """Resolve a subtitle stored alongside an indexed Drive song."""
    if not isinstance(filename, str) or os.path.basename(filename) != filename:
        return None
    song_filename = os.path.splitext(filename)[0] + ".mp4"
    for record in get_downloaded_song_records(download_dir):
        if song_filename not in {record["filename"], record["saved_as"]}:
            continue
        subtitle_saved_as = record.get("subtitle_saved_as")
        if (
            not isinstance(subtitle_saved_as, str)
            or not subtitle_saved_as
            or os.path.basename(subtitle_saved_as) != subtitle_saved_as
        ):
            saved_stem = os.path.splitext(record["saved_as"])[0]
            for extension in (".vtt", ".srt", ".lrc"):
                candidate = os.path.join(download_dir, saved_stem + extension)
                if os.path.isfile(candidate):
                    return candidate
            return None
        subtitle_path = os.path.join(download_dir, subtitle_saved_as)
        if os.path.isfile(subtitle_path):
            return subtitle_path
    return None


def _clear_downloaded_records(download_dir, file_id=None):
    """Delete indexed Drive media and atomically remove their metadata entries."""
    metadata = _load_download_metadata(download_dir)
    if file_id is not None:
        if not FOLDER_ID_PATTERN.fullmatch(file_id):
            raise ValueError("Google Drive 歌曲識別碼格式無效。")
        selected_ids = [file_id] if file_id in metadata else []
    else:
        selected_ids = list(metadata)

    for selected_id in selected_ids:
        saved_as = metadata[selected_id]["saved_as"]
        file_path = os.path.join(download_dir, saved_as)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                raise DriveStorageError("無法清除已下載歌曲，請確認檔案未被其他程式使用。") from None

    for selected_id in selected_ids:
        del metadata[selected_id]
    _save_download_metadata(download_dir, metadata)
    return len(selected_ids)


def _save_downloaded_item(download_dir, folder_id, item, expected_size, safe_filename,
                          subtitle_saved_as=None):
    """Persist one completed Drive download and roll back media if indexing fails."""
    metadata = _load_download_metadata(download_dir)
    file_id = item["id"]
    metadata[file_id] = {
        "file_id": file_id,
        "folder_id": folder_id,
        "name": item.get("name", ""),
        "mimeType": item.get("mimeType", "video/mp4"),
        "size": expected_size,
        "modifiedTime": item.get("modifiedTime", ""),
        "saved_as": safe_filename,
        "public_filename": _safe_public_filename(item.get("name", ""), file_id),
        "downloaded_at": time.time(),
    }
    if subtitle_saved_as:
        metadata[file_id]["subtitle_saved_as"] = subtitle_saved_as
    try:
        _save_download_metadata(download_dir, metadata)
    except DriveStorageError:
        # metadata 保存失敗時回滾成品，避免留下無法追溯來源的孤立檔案。
        final_path = os.path.join(download_dir, safe_filename)
        try:
            os.remove(final_path)
        except OSError:
            pass
        raise


def _stored_library_access_token(app, account_path):
    """Refresh the host-only token for a LAN library request without exposing it."""
    try:
        deploy = drive_account.load_deploy_config(app)
        state = drive_account.load_account_state(account_path)
    except drive_account.DriveAccountError as error:
        raise OAuthFlowError(str(error)) from None
    if state is None:
        raise OAuthFlowError("尚未連結 Google 帳號，請由 KTV 主機登入。")
    if state.get("auth_expired"):
        raise OAuthFlowError("Google 授權已失效，請由 KTV 主機重新登入。")
    try:
        access_token, _expires_in = _refresh_access_token(
            state["refresh_token"],
            deploy["oauth_client_id"],
            deploy["oauth_client_secret"],
        )
    except OAuthFlowError as error:
        if "invalid_grant" in str(error):
            _mark_account_expired(account_path)
            raise OAuthFlowError("Google 授權已失效，請由 KTV 主機重新登入。") from None
        raise
    return state, access_token


def _json_response(payload, status=200):
    """Prevent browsers or intermediaries from caching prototype responses."""
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _is_loopback_request():
    """Trust only direct requests originating on this KTV host."""
    remote_addr = request.remote_addr
    if not remote_addr:
        return False
    try:
        address = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return address.is_loopback


def _local_only(handler):
    """Keep OAuth control endpoints and the token-bearing Picker page off the LAN."""
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if not _is_loopback_request():
            return _json_response({"error": "此驗證頁僅允許 KTV 主機本機使用。"}, 403)
        return handler(*args, **kwargs)

    return wrapped


def _prune_flows():
    """Expire temporary access tokens instead of persisting prototype credentials."""
    now = time.time()
    with _flow_lock:
        for flow_id, flow in list(_flows.items()):
            age = now - flow["created_at"]
            was_terminal = flow["status"] not in (
                "starting", "waiting", "authorized", "listing"
            )
            if (
                flow["status"] in ("starting", "waiting")
                and age >= FLOW_TIMEOUT_SECONDS
            ):
                flow["access_token"] = None
                flow["status"] = "expired"
                flow["error"] = "授權暫存已逾時，請重新開始。"
            if flow["status"] in ("authorized", "listed") and now >= flow["access_token_expires_at"]:
                flow["access_token"] = None
                flow["status"] = "expired"
                flow["error"] = "Google 短效授權已到期，請重新開始。"
            if (
                flow["status"] == "listing"
                and now - flow.get("list_started_at", now) >= FLOW_TIMEOUT_SECONDS
            ):
                flow["access_token"] = None
                flow["status"] = "expired"
                flow["error"] = "資料夾列檔逾時，請重新授權。"
            if (
                was_terminal
                and age >= FLOW_TIMEOUT_SECONDS * 2
            ):
                del _flows[flow_id]


def _expire_flow(flow_id):
    """Expire an unfinished OAuth flow even if the local browser closes."""
    with _flow_lock:
        flow = _flows.get(flow_id)
        if flow is None:
            return
        if (
            flow["status"] in ("starting", "waiting")
            and time.time() - flow["created_at"] >= FLOW_TIMEOUT_SECONDS
        ):
            flow["access_token"] = None
            flow["status"] = "expired"
            flow["error"] = "授權暫存已逾時，請重新開始。"
        elif (
            flow["status"] in ("authorized", "listed")
            and time.time() >= flow["access_token_expires_at"]
        ):
            flow["access_token"] = None
            flow["status"] = "expired"
            flow["error"] = "Google 短效授權已到期，請重新開始。"


def _flow_snapshot(flow_id):
    _prune_flows()
    with _flow_lock:
        flow = _flows.get(flow_id)
        return dict(flow) if flow else None


def _update_flow(flow_id, **updates):
    with _flow_lock:
        flow = _flows.get(flow_id)
        if flow is not None:
            flow.update(updates)


def _authorize_flow(flow_id, access_token, expires_in):
    """Store a callback token only if its flow has not been cancelled or expired."""
    with _flow_lock:
        flow = _flows.get(flow_id)
        if flow is None or flow["status"] != "waiting":
            return False
        flow.update({
            "status": "authorized",
            "access_token": access_token,
            "access_token_expires_at": time.time() + expires_in,
            "token_claimed": False,
            "list_started": False,
        })
        return True


def _base64url(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_pkce_pair():
    """Create the per-request PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _safe_oauth_error_description(value):
    """Keep Google's parameter hint while removing values and control characters."""
    if not isinstance(value, str):
        return ""
    safe_value = OAUTH_SENSITIVE_VALUE_PATTERN.sub(r"\1=[redacted]", value)
    safe_value = re.sub(r"[\x00-\x1f\x7f]+", " ", safe_value)
    return safe_value[:240].strip()


def _post_oauth_token(parameters):
    """POST 至 Google token 端點；錯誤訊息不含 Client Secret 或權杖值。"""
    form = urllib.parse.urlencode(parameters).encode("ascii")
    outbound = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(outbound, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            error_payload = json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            error_payload = {}
        error_code = error_payload.get("error")
        if not isinstance(error_code, str) or not OAUTH_ERROR_CODE_PATTERN.fullmatch(error_code):
            error_code = "unknown_error"
        error_description = _safe_oauth_error_description(
            error_payload.get("error_description")
        )
        if (
            error_code == "invalid_request"
            and "client_secret is missing" in error_description.lower()
        ):
            error_description = (
                "Google 此次要求 client_secret。請在 KTV 主機 google_drive_deploy.json "
                "填入同一 Desktop 用戶端的 Client Secret 後重試。"
                "不要提供給他人或提交至版本庫。"
            )
        hint = OAUTH_ERROR_HINTS.get(
            error_code,
            "請確認 OAuth 用戶端設定後再試；不要提供 Client Secret 或權杖。",
        )
        description = (
            "Google 詳細說明：{0}".format(error_description)
            if error_description
            else hint
        )
        raise OAuthFlowError(
            "Google OAuth 權杖交換失敗（HTTP {0}，{1}）：{2}".format(
                error.code,
                error_code,
                description,
            )
        ) from None
    except (urllib.error.URLError, TimeoutError):
        raise OAuthFlowError("無法連線至 Google OAuth 服務，請確認網路後重試。") from None
    except OSError:
        raise OAuthFlowError("Google OAuth 連線失敗，請確認網路後重試。") from None
    except (UnicodeDecodeError, ValueError):
        raise OAuthFlowError("Google OAuth 回應格式無效，請稍後重試。") from None
    if not isinstance(payload, dict):
        raise OAuthFlowError("Google OAuth 回應格式無效，請稍後重試。")
    return payload


def _access_token_from_payload(payload):
    """從 OAuth 回應取出短效 access token 與有效秒數。"""
    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthFlowError("Google OAuth 回應未包含可用的短效授權權杖。")
    if not isinstance(expires_in, int) or expires_in <= 0:
        raise OAuthFlowError("Google OAuth 回應的權杖有效期限無效。")
    return access_token, expires_in


def _exchange_code(code, client_id, verifier, redirect_uri, client_secret=None):
    """以授權碼換取 access token 與必須保存的 refresh token。"""
    parameters = {
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        parameters["client_secret"] = client_secret
    payload = _post_oauth_token(parameters)
    access_token, expires_in = _access_token_from_payload(payload)
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthFlowError(
            "Google 未回傳 refresh token。請在授權畫面同意離線存取後，由 KTV 主機重新連結。"
        )
    return access_token, expires_in, refresh_token


def _refresh_access_token(refresh_token, client_id, client_secret=None):
    """用本機保存的 refresh token 換取 Picker／Drive API 所需的短效 access token。"""
    parameters = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if client_secret:
        parameters["client_secret"] = client_secret
    payload = _post_oauth_token(parameters)
    return _access_token_from_payload(payload)


def _get_drive_account_profile(access_token):
    """以 Drive about 讀取已授權帳號顯示名稱與信箱，不增加額外 OAuth scope。"""
    url = "{0}?{1}".format(
        DRIVE_ABOUT_URL,
        urllib.parse.urlencode({"fields": "user(displayName,emailAddress)"}),
    )
    payload = _google_api_get(url, access_token)
    user = payload.get("user") if isinstance(payload, dict) else {}
    if not isinstance(user, dict):
        user = {}
    email = user.get("emailAddress")
    name = user.get("displayName")
    return {
        "email": email if isinstance(email, str) else "",
        "name": name if isinstance(name, str) else "",
    }


def _run_oauth_flow(
    flow_id,
    client_id,
    client_secret=None,
    account_path=None,
):
    """以 Desktop PKCE/loopback 取得離線 refresh token，並只把短效 access token 留在記憶體。"""
    verifier, challenge = _make_pkce_pair()
    state = secrets.token_urlsafe(32)
    server = None

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            # The default HTTP server logger can expose callback query parameters.
            return

        def _reply(self, status, message):
            body = (
                "<!doctype html><meta charset=\"utf-8\">"
                "<title>ianAutoKTV OAuth</title>"
                "<p>{0}</p>".format(message)
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _reply_success_and_close(self):
            """OAuth 成功後關閉回呼頁，讓原本的驗證頁繼續輪詢並開啟 Picker。"""
            body = (
                "<!doctype html><meta charset=\"utf-8\">"
                "<title>ianAutoKTV OAuth</title>"
                "<p>Google 授權完成，正在關閉此頁面。</p>"
                "<p id=\"fallback\" hidden>若頁面未自動關閉，請手動關閉此分頁，"
                "再回到原本的 KTV 驗證頁。</p>"
                "<script>"
                "window.addEventListener('load', function () {"
                "window.close();"
                "window.setTimeout(function () {"
                "var fallback = document.getElementById('fallback');"
                "if (fallback) fallback.hidden = false;"
                "}, 300);"
                "});"
                "</script>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/":
                self._reply(404, "OAuth 回呼路徑無效，請回到 KTV 驗證頁。")
                return

            returned_state = params.get("state", [""])[0]
            if not hmac.compare_digest(returned_state, state):
                self._reply(400, "OAuth 狀態驗證失敗，請回到 KTV 驗證頁重新開始。")
                return

            oauth_error = params.get("error", [""])[0]
            if oauth_error:
                _update_flow(
                    flow_id,
                    status="failed",
                    error="Google 未完成授權（{0}）。".format(oauth_error[:80]),
                )
                self._reply(400, "Google 未完成授權，請回到 KTV 驗證頁查看狀態。")
                return

            code = params.get("code", [""])[0]
            if not code:
                _update_flow(
                    flow_id,
                    status="failed",
                    error="Google OAuth 回呼未提供授權碼。",
                )
                self._reply(400, "Google OAuth 回呼資料不完整，請回到 KTV 驗證頁。")
                return

            try:
                access_token, expires_in, refresh_token = _exchange_code(
                    code,
                    client_id,
                    verifier,
                    redirect_uri,
                    client_secret,
                )
            except OAuthFlowError as error:
                _update_flow(flow_id, status="failed", error=str(error))
                self._reply(502, "權杖交換失敗，請回到 KTV 驗證頁查看狀態。")
                return

            # 短效 access token 只留在記憶體；refresh token 立刻加密寫入此 KTV 安裝目錄。
            if not _authorize_flow(flow_id, access_token, expires_in):
                self._reply(410, "此授權流程已取消或逾時，請回到 KTV 驗證頁。")
                return
            try:
                try:
                    profile = _get_drive_account_profile(access_token)
                except DriveApiError:
                    profile = {"email": "", "name": ""}
                existing = None
                if account_path:
                    try:
                        existing = drive_account.load_account_state(account_path)
                    except drive_account.DriveAccountError:
                        existing = None
                    drive_account.save_account_state(
                        account_path,
                        drive_account.merge_linked_account(
                            existing,
                            refresh_token,
                            profile,
                        ),
                    )
            except drive_account.DriveAccountError as error:
                _update_flow(flow_id, status="failed", access_token=None, error=str(error))
                self._reply(500, "授權已取得，但 KTV 主機無法保存 refresh token。")
                return
            _update_flow(
                flow_id,
                account_email=profile.get("email") or "",
                account_name=profile.get("name") or "",
            )
            self._reply_success_and_close()

    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
        server.timeout = 1
        redirect_uri = "http://127.0.0.1:{0}".format(server.server_address[1])
        authorization_query = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": DRIVE_READONLY_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        })
        authorization_url = "{0}?{1}".format(OAUTH_AUTH_URL, authorization_query)
        # 保留不含權杖的 OAuth 授權網址，讓主機瀏覽器無法自動開啟時可由本機頁面手動開啟。
        _update_flow(flow_id, status="waiting", authorization_url=authorization_url)
        try:
            browser_opened = webbrowser.open(authorization_url, new=1, autoraise=True)
        except OSError:
            browser_opened = False
        if not browser_opened:
            _update_flow(
                flow_id,
                status="failed",
                error="無法自動開啟系統瀏覽器，請確認 KTV 主機已設定預設瀏覽器。",
            )
            return

        deadline = time.time() + CALLBACK_TIMEOUT_SECONDS
        while time.time() < deadline:
            snapshot = _flow_snapshot(flow_id)
            if snapshot is None or snapshot["status"] != "waiting":
                return
            server.handle_request()

        _update_flow(
            flow_id,
            status="expired",
            error="等待 Google 授權逾時，請重新開始。",
        )
    except OSError:
        _update_flow(
            flow_id,
            status="failed",
            error="無法在 KTV 主機建立 OAuth loopback 回呼服務。",
        )
    finally:
        if server is not None:
            server.server_close()


def _google_api_get(url, access_token):
    """Call Drive with the bearer token in a header, never in a URL or log."""
    outbound = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer {0}".format(access_token),
            "Accept": "application/json",
        },
    )
    for attempt in range(DRIVE_RATE_LIMIT_RETRIES + 1):
        try:
            with urllib.request.urlopen(outbound, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            api_error = _drive_api_error_from_http(error)
        except (urllib.error.URLError, TimeoutError, OSError):
            api_error = DriveApiError(0)
        except (UnicodeDecodeError, ValueError):
            api_error = DriveApiError(0)
        if not _retry_drive_request(api_error, attempt):
            raise api_error from None


def _get_drive_metadata(file_id, access_token):
    """Fetch trusted Drive metadata for a folder or file identifier."""
    if not FOLDER_ID_PATTERN.fullmatch(file_id):
        raise ValueError("Google Drive 檔案識別碼格式無效。")
    url = "{0}/{1}?{2}".format(
        DRIVE_API_URL,
        urllib.parse.quote(file_id, safe=""),
        urllib.parse.urlencode({
            "fields": "id,name,mimeType,size,parents,trashed,driveId,modifiedTime",
            "supportsAllDrives": "true",
        }),
    )
    return _google_api_get(url, access_token)


def _validate_drive_song(folder_id, file_id, access_token):
    """Revalidate the folder and direct-child MP4 before every media download."""
    if not FOLDER_ID_PATTERN.fullmatch(file_id):
        raise ValueError("Google Drive 檔案識別碼格式無效。")
    folder = _get_drive_metadata(folder_id, access_token)
    if folder.get("id") != folder_id or folder.get("mimeType") != DRIVE_FOLDER_MIME_TYPE:
        raise ValueError("已選歌庫資料夾不存在或不再是有效資料夾，請重新選擇。")
    if folder.get("trashed"):
        raise ValueError("已選歌庫資料夾已移至垃圾桶，無法下載。")

    item = _get_drive_metadata(file_id, access_token)
    if item.get("id") != file_id:
        raise ValueError("Drive API 回傳的歌曲識別碼與要求不符。")
    if item.get("trashed"):
        raise ValueError("歌曲已移至垃圾桶，無法下載。")
    if folder_id not in item.get("parents", []):
        raise ValueError("歌曲不是已選歌庫資料夾的直接子項目，已拒絕下載。")
    if item.get("mimeType") == DRIVE_FOLDER_MIME_TYPE or not item.get("name", "").lower().endswith(".mp4"):
        raise ValueError("所選項目不是支援的 MP4 歌曲。")
    if folder.get("driveId") != item.get("driveId"):
        raise ValueError("歌曲與所選資料夾不屬於相同的 Drive。")
    try:
        expected_size = int(item.get("size"))
    except (TypeError, ValueError):
        raise ValueError("Drive 未提供有效的歌曲檔案大小，無法安全下載。") from None
    if expected_size <= 0:
        raise ValueError("Drive 歌曲檔案大小無效，無法安全下載。")
    return item, expected_size


def _find_drive_song_lyrics(folder_id, song_item, access_token):
    """Find a direct-child subtitle file with the same stem as an MP4 song."""
    listing = _list_folder_children(folder_id, access_token)
    song = next((item for item in listing.get("songs", []) if item.get("id") == song_item.get("id")), None)
    if not song:
        return None
    lyrics = song.get("lyrics") or []
    return next((item for item in lyrics if str(item.get("name", "")).lower().endswith(".vtt")), None) or (
        lyrics[0] if lyrics else None
    )


def _validate_drive_subtitle(folder_id, subtitle_id, song_stem, access_token):
    """Revalidate a matching direct-child subtitle before downloading it."""
    item = _get_drive_metadata(subtitle_id, access_token)
    name = str(item.get("name", ""))
    extension = os.path.splitext(name)[1].lower()
    if extension not in {".vtt", ".srt", ".lrc"} or os.path.splitext(name)[0].casefold() != song_stem.casefold():
        raise ValueError("Drive 歌詞檔與歌曲名稱不匹配。")
    if item.get("trashed") or folder_id not in item.get("parents", []):
        raise ValueError("Drive 歌詞檔不在選定的歌庫資料夾中。")
    try:
        expected_size = int(item.get("size"))
    except (TypeError, ValueError):
        raise ValueError("Drive 歌詞檔大小無效。") from None
    if expected_size <= 0:
        raise ValueError("Drive 歌詞檔大小無效。")
    return item, expected_size, extension


def _ensure_downloaded_song_subtitle(download_dir, folder_id, song_item, access_token):
    """Download or repair the subtitle association for an already downloaded song."""
    subtitle_item = _find_drive_song_lyrics(folder_id, song_item, access_token)
    if not subtitle_item:
        return None
    subtitle_item, subtitle_size, subtitle_extension = _validate_drive_subtitle(
        folder_id,
        subtitle_item.get("id"),
        os.path.splitext(song_item.get("name", ""))[0],
        access_token,
    )
    subtitle_filename = os.path.splitext(
        _safe_public_filename(song_item.get("name", ""), song_item["id"])
    )[0] + subtitle_extension
    subtitle_path = os.path.join(download_dir, subtitle_filename)
    if not os.path.isfile(subtitle_path):
        _download_drive_media(
            subtitle_item["id"],
            access_token,
            subtitle_size,
            download_dir,
            subtitle_extension,
            subtitle_filename,
        )
    metadata = _load_download_metadata(download_dir)
    record = metadata.get(song_item["id"])
    if isinstance(record, dict):
        record["subtitle_saved_as"] = subtitle_filename
        _save_download_metadata(download_dir, metadata)
    return subtitle_filename


def _normalize_downloaded_song_filename(download_dir, song_item):
    """Rename legacy hash-based media storage to the song's public filename."""
    metadata = _load_download_metadata(download_dir)
    record = metadata.get(song_item.get("id"))
    if not isinstance(record, dict):
        return None
    target_filename = _safe_public_filename(song_item.get("name", ""), song_item["id"])
    current_filename = record.get("saved_as")
    if current_filename != target_filename:
        current_path = os.path.join(download_dir, current_filename)
        target_path = os.path.join(download_dir, target_filename)
        if os.path.isfile(current_path) and not os.path.exists(target_path):
            os.replace(current_path, target_path)
        if os.path.isfile(target_path):
            record["saved_as"] = target_filename
            record["public_filename"] = target_filename
            _save_download_metadata(download_dir, metadata)
    return target_filename


def _download_drive_media_once(file_id, access_token, expected_size, download_dir,
                               extension=".mp4", output_filename=None):
    """Stream original Drive media to a temporary file and atomically publish it."""
    try:
        os.makedirs(download_dir, exist_ok=True)
        if shutil.disk_usage(download_dir).free < expected_size:
            raise DriveStorageError("KTV 主機可用磁碟空間不足，未完成下載。")
    except DriveStorageError:
        raise
    except OSError:
        raise DriveStorageError("無法存取 KTV 主機的專用下載目錄。") from None

    normalized_extension = extension if str(extension).startswith(".") else "." + str(extension)
    safe_filename = output_filename or (
        hashlib.sha256(file_id.encode("utf-8")).hexdigest() + normalized_extension.lower()
    )
    final_path = os.path.join(download_dir, safe_filename)
    if os.path.exists(final_path):
        raise FileExistsError("此 Drive 歌曲已存在於原型下載目錄。")

    media_url = "{0}/{1}?{2}".format(
        DRIVE_API_URL,
        urllib.parse.quote(file_id, safe=""),
        urllib.parse.urlencode({
            "alt": "media",
            "supportsAllDrives": "true",
        }),
    )
    outbound = urllib.request.Request(
        media_url,
        headers={
            "Authorization": "Bearer {0}".format(access_token),
            "Accept": "application/octet-stream",
        },
    )
    try:
        response = urllib.request.urlopen(outbound, timeout=60)
    except urllib.error.HTTPError as error:
        raise _drive_api_error_from_http(error) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DriveApiError(0) from None

    temporary_path = None
    try:
        with response:
            try:
                file_descriptor, temporary_path = tempfile.mkstemp(
                    prefix=".drive-download-",
                    suffix=".part",
                    dir=download_dir,
                )
                try:
                    output = os.fdopen(file_descriptor, "wb")
                except OSError:
                    os.close(file_descriptor)
                    raise
            except OSError:
                raise DriveStorageError("無法在 KTV 主機建立歌曲暫存檔。") from None

            with output:
                bytes_written = 0
                while True:
                    try:
                        chunk = response.read(DRIVE_DOWNLOAD_CHUNK_SIZE)
                    except (urllib.error.URLError, TimeoutError, OSError):
                        raise DriveApiError(0) from None
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > expected_size:
                        raise DriveDownloadIntegrityError("Drive 下載內容超過檔案 metadata 大小。")
                    try:
                        if output.write(chunk) != len(chunk):
                            raise OSError
                    except OSError:
                        raise DriveStorageError("KTV 主機磁碟寫入失敗，未完成下載。") from None

                if bytes_written != expected_size:
                    raise DriveDownloadIntegrityError("Drive 下載未完整，檔案大小與 metadata 不符。")
                try:
                    output.flush()
                    os.fsync(output.fileno())
                except OSError:
                    raise DriveStorageError("KTV 主機無法完成歌曲暫存檔寫入。") from None

        try:
            os.replace(temporary_path, final_path)
            temporary_path = None
        except OSError:
            raise DriveStorageError("KTV 主機無法完成歌曲檔案保存。") from None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                raise DriveStorageError("下載失敗，且無法清除未完成的歌曲暫存檔。") from None

    return safe_filename


def _download_drive_media(file_id, access_token, expected_size, download_dir,
                          extension=".mp4", output_filename=None):
    """Retry a complete original-media download without ever publishing partial data."""
    for attempt in range(DRIVE_RATE_LIMIT_RETRIES + 1):
        try:
            return _download_drive_media_once(
                file_id,
                access_token,
                expected_size,
                download_dir,
                extension,
                output_filename,
            )
        except DriveApiError as error:
            if not _retry_drive_request(error, attempt):
                raise


def _list_folder_children(folder_id, access_token):
    """List and filter only direct MP4 children after confirming the folder."""
    if not FOLDER_ID_PATTERN.fullmatch(folder_id):
        raise ValueError("Google Drive 資料夾識別碼格式無效。")

    folder_url = "{0}/{1}?{2}".format(
        DRIVE_API_URL,
        urllib.parse.quote(folder_id, safe=""),
        urllib.parse.urlencode({
            "fields": "id,name,mimeType,trashed,driveId",
            "supportsAllDrives": "true",
        }),
    )
    folder = _google_api_get(folder_url, access_token)
    if folder.get("id") != folder_id:
        raise ValueError("Drive API 回傳的資料夾識別碼與 Picker 選取結果不符。")
    if folder.get("mimeType") != DRIVE_FOLDER_MIME_TYPE:
        raise ValueError("Picker 選取項目不是 Google Drive 資料夾。")
    if folder.get("trashed"):
        raise ValueError("選取的 Google Drive 資料夾已移至垃圾桶。")

    # Query the selected folder's immediate children only; never recurse.
    query = "'{0}' in parents and trashed = false".format(folder_id)
    drive_id = folder.get("driveId")
    # Drive API requires a drive-specific corpus for Shared Drive folders.
    corpus = "drive" if drive_id else "user"
    page_token = None
    children = []
    pages_read = 0
    while True:
        params = {
            "q": query,
            "corpora": corpus,
            "pageSize": "1000",
            "orderBy": "name",
            "fields": "nextPageToken,files(id,name,mimeType,size,parents,modifiedTime,trashed)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if drive_id:
            params["driveId"] = drive_id
        if page_token:
            params["pageToken"] = page_token
        listing_url = "{0}?{1}".format(DRIVE_API_URL, urllib.parse.urlencode(params))
        page = _google_api_get(listing_url, access_token)
        pages_read += 1
        children.extend(page.get("files", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break

    # Recheck each result's parent before exposing it as a selected-folder child.
    direct_children = [
        item for item in children
        if folder_id in item.get("parents", []) and not item.get("trashed", False)
    ]
    lyrics_by_stem = {}
    for item in direct_children:
        name = item.get("name", "")
        extension = os.path.splitext(name)[1].lower()
        if extension in {".vtt", ".srt", ".lrc"}:
            lyrics_by_stem.setdefault(os.path.splitext(name)[0].casefold(), []).append({
                "id": item.get("id"),
                "name": name,
                "mimeType": item.get("mimeType", ""),
                "size": item.get("size"),
            })

    songs = [
        {
            "id": item.get("id"),
            "name": item.get("name", ""),
            "mimeType": item.get("mimeType", ""),
            "size": item.get("size"),
            "lyrics": lyrics_by_stem.get(os.path.splitext(item.get("name", ""))[0].casefold(), []),
        }
        for item in direct_children
        if item.get("mimeType") != "application/vnd.google-apps.folder"
        and item.get("name", "").lower().endswith(".mp4")
    ]
    return {
        "folder": {"id": folder.get("id"), "name": folder.get("name", "")},
        "diagnostics": {
            "folder_id_verified": True,
            "drive_type": "shared_drive" if drive_id else "my_drive",
            "corpus": corpus,
            "pages_read": pages_read,
        },
        "direct_child_count": len(direct_children),
        "song_count": len(songs),
        "ignored_item_count": len(direct_children) - len(songs),
        "songs": songs,
    }


def _has_active_flow():
    """同一時間只允許一個本機授權或 Picker 流程。"""
    return any(
        flow["status"] in ("starting", "waiting", "authorized", "listed", "listing")
        for flow in _flows.values()
    )


def _new_flow_record():
    """建立不含 refresh token 的短效流程紀錄。"""
    return {
        "created_at": time.time(),
        "status": "starting",
        "access_token": None,
        "token_claimed": False,
        "list_started": False,
        "cancel_reason": None,
        "error": None,
        "folder_id": None,
        "download_in_progress": False,
        "account_email": "",
        "account_name": "",
    }


def _mark_account_expired(path):
    """refresh token 失效時保留資料夾選擇，提示主機重新授權。"""
    try:
        state = drive_account.load_account_state(path)
    except drive_account.DriveAccountError:
        return
    if state is None:
        return
    state["auth_expired"] = True
    drive_account.save_account_state(path, state)


def _safe_library_status(app):
    """組合部署設定與帳號狀態，供本機頁與區網顯示。"""
    deploy_error = None
    account_error = None
    try:
        drive_account.load_deploy_config(app)
    except drive_account.DriveAccountError as error:
        deploy_error = str(error)
    try:
        state = drive_account.load_account_state(drive_account.account_store_path(app))
    except drive_account.DriveAccountError as error:
        # 帳號權杖損壞或由不同 Windows 使用者保存時，仍須允許主機重新登入。
        account_error = str(error)
        state = None
    payload = drive_account.public_library_status(state, deploy_error)
    if account_error:
        payload["account_error"] = account_error
    return payload, 200


def register_google_drive_prototype(app):
    """Register temporary Google Drive feasibility routes on the main Flask app."""
    download_dir = app.config.get(
        "DRIVE_PROTOTYPE_DOWNLOAD_DIR",
        os.path.join(app.root_path, "drive_downloads"),
    )
    account_path = drive_account.account_store_path(app)

    @app.route("/drive-prototype")
    @_local_only
    def drive_prototype_page():
        _prune_flows()
        response = app.make_response(render_template("drive-prototype.html"))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/drive/library-status", methods=["GET"])
    def drive_library_status():
        """區網可查看本安裝共用帳號與資料夾，但不能變更授權。"""
        payload, status = _safe_library_status(app)
        return _json_response(payload, status)

    @app.route("/api/drive-prototype/account", methods=["GET"])
    @_local_only
    def drive_prototype_account():
        """本機管理頁讀取部署設定與已連結帳號，不含 refresh token。"""
        payload, status = _safe_library_status(app)
        return _json_response(payload, status)

    @app.route("/api/drive/library-songs", methods=["GET"])
    def drive_library_songs():
        """List the selected folder for LAN remotes without returning any token."""
        try:
            state, access_token = _stored_library_access_token(app, account_path)
        except OAuthFlowError as error:
            message = str(error)
            status = 401 if "授權已失效" in message else 409 if "尚未連結" in message else 400
            return _json_response({"error": message}, status)
        folder_id = state.get("folder_id")
        if not folder_id:
            return _json_response({"error": "尚未選擇 Google Drive 歌曲資料夾。"}, 409)
        try:
            listing = _list_folder_children(folder_id, access_token)
        except ValueError as error:
            return _json_response({"error": str(error)}, 400)
        except DriveApiError as error:
            return _json_response({
                "error": _drive_error_message(error, "歌庫列檔"),
                "http_status": error.status_code or None,
            }, 401 if error.status_code == 401 else 502)
        downloaded = {
            record["file_id"]: record
            for record in get_downloaded_song_records(download_dir)
        }
        songs = []
        for song in listing.get("songs", []):
            record = downloaded.get(song.get("id"))
            songs.append({
                "id": song.get("id"),
                "name": song.get("name", ""),
                "mimeType": song.get("mimeType", ""),
                "size": song.get("size"),
                "modifiedTime": song.get("modifiedTime", ""),
                "downloaded": record is not None,
                "filename": record.get("filename") if record else "",
            })
        return _json_response({
            "folder": listing.get("folder", {}),
            "songs": songs,
            "song_count": len(songs),
            "diagnostics": listing.get("diagnostics", {}),
        })

    @app.route("/api/drive/library-songs/download", methods=["POST"])
    def drive_library_song_download():
        """Download one selected-folder song on behalf of a LAN remote."""
        data = request.get_json(silent=True) or {}
        file_id = data.get("file_id")
        if not isinstance(file_id, str) or not FOLDER_ID_PATTERN.fullmatch(file_id):
            return _json_response({"error": "Google Drive 歌曲識別碼無效。"}, 400)
        try:
            state, access_token = _stored_library_access_token(app, account_path)
        except OAuthFlowError as error:
            message = str(error)
            status = 401 if "授權已失效" in message else 409 if "尚未連結" in message else 400
            return _json_response({"error": message}, status)
        folder_id = state.get("folder_id")
        if not folder_id:
            return _json_response({"error": "尚未選擇 Google Drive 歌曲資料夾。"}, 409)
        subtitle_saved_as = None
        try:
            with _download_lock:
                item, expected_size = _validate_drive_song(folder_id, file_id, access_token)
                safe_filename = _download_drive_media(
                    file_id,
                    access_token,
                    expected_size,
                    download_dir,
                    ".mp4",
                    _safe_public_filename(item.get("name", ""), file_id),
                )
                subtitle_item = _find_drive_song_lyrics(folder_id, item, access_token)
                if subtitle_item:
                    subtitle_item, subtitle_size, subtitle_extension = _validate_drive_subtitle(
                        folder_id,
                        subtitle_item.get("id"),
                        os.path.splitext(item.get("name", ""))[0],
                        access_token,
                    )
                    try:
                        subtitle_filename = os.path.splitext(
                            _safe_public_filename(item.get("name", ""), file_id)
                        )[0] + subtitle_extension
                        subtitle_saved_as = _download_drive_media(
                            subtitle_item["id"],
                            access_token,
                            subtitle_size,
                            download_dir,
                            subtitle_extension,
                            subtitle_filename,
                        )
                    except FileExistsError:
                        subtitle_saved_as = subtitle_filename
                _save_downloaded_item(
                    download_dir,
                    folder_id,
                    item,
                    expected_size,
                    safe_filename,
                    subtitle_saved_as,
                )
        except ValueError as error:
            return _json_response({"error": str(error)}, 400)
        except FileExistsError:
            try:
                _normalize_downloaded_song_filename(download_dir, item)
                _ensure_downloaded_song_subtitle(download_dir, folder_id, item, access_token)
            except (DriveApiError, DriveStorageError, OSError, ValueError):
                pass
            # 舊版流程可能已留下完整媒體檔但尚未寫入 metadata；確認大小相符後只補建索引，不重新下載或覆寫檔案。
            recovered_filename = hashlib.sha256(file_id.encode("utf-8")).hexdigest() + ".mp4"
            recovered_path = os.path.join(download_dir, recovered_filename)
            try:
                recovered_size = os.path.getsize(recovered_path)
            except OSError:
                recovered_size = -1
            if recovered_size == expected_size:
                _save_downloaded_item(
                    download_dir,
                    folder_id,
                    item,
                    expected_size,
                    recovered_filename,
                )
                return _json_response({
                    "status": "downloaded",
                    "name": item.get("name", ""),
                    "filename": _safe_public_filename(item.get("name", ""), file_id),
                    "size": expected_size,
                })
            existing = next(
                (record for record in get_downloaded_song_records(download_dir)
                 if record["file_id"] == file_id),
                None,
            )
            if existing:
                return _json_response({
                    "status": "downloaded",
                    "name": existing["name"],
                    "filename": existing["filename"],
                    "size": existing["size"],
                })
            return _json_response({"error": "此歌曲已存在於 KTV 下載目錄。"}, 409)
        except DriveApiError as error:
            return _json_response({
                "error": _drive_error_message(error, "原始媒體下載"),
                "http_status": error.status_code or None,
            }, 401 if error.status_code == 401 else 502)
        except DriveDownloadIntegrityError as error:
            return _json_response({"error": str(error)}, 502)
        except DriveStorageError as error:
            return _json_response({"error": str(error)}, 507)
        return _json_response({
            "status": "downloaded",
            "name": item.get("name", ""),
            "filename": _safe_public_filename(item.get("name", ""), file_id),
            "size": expected_size,
        })

    @app.route("/api/drive-prototype/downloads", methods=["GET"])
    @_local_only
    def drive_prototype_downloads():
        """Show indexed local Drive downloads and host disk usage to the KTV manager."""
        try:
            os.makedirs(download_dir, exist_ok=True)
            disk = shutil.disk_usage(download_dir)
        except OSError:
            return _json_response({"error": "無法讀取 KTV 主機的 Drive 下載目錄。"}, 500)
        return _json_response({
            "downloads": get_downloaded_song_records(download_dir),
            "disk": {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
            },
        })

    @app.route("/api/drive-prototype/downloads/clear", methods=["POST"])
    @_local_only
    def drive_prototype_clear_downloads():
        """Clear one or all indexed Drive downloads from the KTV host only."""
        data = request.get_json(silent=True) or {}
        file_id = data.get("file_id")
        clear_all = bool(data.get("all"))
        if file_id is not None and (not isinstance(file_id, str) or clear_all):
            return _json_response({"error": "請指定有效歌曲識別碼，或單獨選擇全部清除。"}, 400)
        if file_id is None and not clear_all:
            return _json_response({"error": "請指定要清除的歌曲，或選擇全部清除。"}, 400)

        _prune_flows()
        with _flow_lock:
            if any(flow.get("download_in_progress") or flow["status"] == "listing" for flow in _flows.values()):
                return _json_response({"error": "列檔或下載進行中，請稍候再清除歌曲。"}, 409)
        try:
            with _download_lock:
                cleared_count = _clear_downloaded_records(
                    download_dir,
                    None if clear_all else file_id,
                )
        except ValueError as error:
            return _json_response({"error": str(error)}, 400)
        except DriveStorageError as error:
            return _json_response({"error": str(error)}, 500)
        return _json_response({"status": "cleared", "cleared_count": cleared_count})

    @app.route("/api/drive-prototype/oauth/start", methods=["POST"])
    @_local_only
    def drive_prototype_oauth_start():
        _prune_flows()
        try:
            deploy = drive_account.load_deploy_config(app)
        except drive_account.DriveAccountError as error:
            return _json_response({"error": str(error)}, 400)

        with _flow_lock:
            # OAuth 回呼成功後，若原本的驗證頁已被瀏覽器導向頁取代，
            # 仍會留下尚未領取 Picker 權杖的 authorized flow。資料夾選擇
            # 應重用該 flow，避免把已完成的登入誤判為重複驗證流程。
            for existing_flow_id, existing_flow in _flows.items():
                if (
                    existing_flow["status"] == "authorized"
                    and not existing_flow["token_claimed"]
                    and existing_flow.get("access_token")
                ):
                    return _json_response({
                        "flow_id": existing_flow_id,
                        "picker_api_key": deploy["picker_api_key"],
                        "project_number": deploy["project_number"],
                    })
            if _has_active_flow():
                return _json_response({
                    "error": "目前已有驗證流程，請完成或取消後再試。",
                }, 409)
            flow_id = secrets.token_urlsafe(32)
            _flows[flow_id] = _new_flow_record()

        threading.Thread(
            target=_run_oauth_flow,
            # Client Secret 只從部署檔讀取，不接受瀏覽器提交，也不寫入流程紀錄。
            args=(
                flow_id,
                deploy["oauth_client_id"],
                deploy["oauth_client_secret"],
                account_path,
            ),
            daemon=True,
        ).start()
        expiry_timer = threading.Timer(
            FLOW_TIMEOUT_SECONDS,
            _expire_flow,
            args=(flow_id,),
        )
        expiry_timer.daemon = True
        expiry_timer.start()
        return _json_response({
            "flow_id": flow_id,
            "picker_api_key": deploy["picker_api_key"],
            "project_number": deploy["project_number"],
        })

    @app.route("/api/drive-prototype/picker/start", methods=["POST"])
    @_local_only
    def drive_prototype_picker_start():
        """已連結帳號時用 refresh token 換短效權杖開啟 Picker，不必再貼用戶端設定。"""
        _prune_flows()
        try:
            deploy = drive_account.load_deploy_config(app)
            state = drive_account.load_account_state(account_path)
        except drive_account.DriveAccountError as error:
            return _json_response({"error": str(error)}, 400)
        if state is None:
            return _json_response({"error": "尚未連結 Google 帳號，請先由 KTV 主機登入。"}, 409)
        if state.get("auth_expired"):
            return _json_response({
                "error": "Google 授權已失效，請由 KTV 主機重新登入。",
            }, 401)

        try:
            access_token, expires_in = _refresh_access_token(
                state["refresh_token"],
                deploy["oauth_client_id"],
                deploy["oauth_client_secret"],
            )
        except OAuthFlowError as error:
            if "invalid_grant" in str(error):
                _mark_account_expired(account_path)
                return _json_response({
                    "error": "Google 授權已失效，請由 KTV 主機重新登入。",
                }, 401)
            return _json_response({"error": str(error)}, 502)

        with _flow_lock:
            if _has_active_flow():
                return _json_response({
                    "error": "目前已有驗證流程，請完成或取消後再試。",
                }, 409)
            flow_id = secrets.token_urlsafe(32)
            flow = _new_flow_record()
            flow.update({
                "status": "authorized",
                "access_token": access_token,
                "access_token_expires_at": time.time() + expires_in,
                "account_email": state.get("account_email") or "",
                "account_name": state.get("account_name") or "",
            })
            _flows[flow_id] = flow
        return _json_response({
            "flow_id": flow_id,
            "picker_api_key": deploy["picker_api_key"],
            "project_number": deploy["project_number"],
        })

    @app.route("/api/drive-prototype/unlink", methods=["POST"])
    @_local_only
    def drive_prototype_unlink():
        """解除本安裝的 Google 連結並清除 refresh token；區網遙控器不可呼叫。"""
        _prune_flows()
        with _flow_lock:
            if any(flow.get("download_in_progress") or flow["status"] == "listing" for flow in _flows.values()):
                return _json_response({"error": "列檔或下載進行中，請稍候再解除連結。"}, 409)
            for flow in _flows.values():
                flow["access_token"] = None
                if flow["status"] not in ("complete", "failed", "expired", "cancelled"):
                    flow["status"] = "cancelled"
                    flow["cancel_reason"] = "unlink"
        try:
            drive_account.clear_account_state(account_path)
        except drive_account.DriveAccountError as error:
            return _json_response({"error": str(error)}, 500)
        return _json_response({"status": "unlinked"})

    @app.route("/api/drive-prototype/oauth/status", methods=["POST"])
    @_local_only
    def drive_prototype_oauth_status():
        data = request.get_json(silent=True) or {}
        flow_id = data.get("flow_id")
        if not isinstance(flow_id, str):
            return _json_response({"error": "驗證流程識別碼無效。"}, 400)
        flow = _flow_snapshot(flow_id)
        if flow is None:
            return _json_response({"error": "驗證流程不存在或已逾時。"}, 404)
        return _json_response({
            "status": flow["status"],
            "error": flow["error"],
            "authorization_url": flow.get("authorization_url") if flow["status"] == "waiting" else None,
        })

    @app.route("/api/drive-prototype/oauth/token", methods=["POST"])
    @_local_only
    def drive_prototype_oauth_token():
        data = request.get_json(silent=True) or {}
        flow_id = data.get("flow_id")
        if not isinstance(flow_id, str):
            return _json_response({"error": "驗證流程識別碼無效。"}, 400)

        with _flow_lock:
            flow = _flows.get(flow_id)
            if flow is None or flow["status"] != "authorized":
                return _json_response({"error": "OAuth 尚未完成或已逾時。"}, 409)
            if flow["token_claimed"] or not flow["access_token"]:
                return _json_response({"error": "短效權杖已領取或已清除。"}, 409)
            flow["token_claimed"] = True
            access_token = flow["access_token"]
        return _json_response({"access_token": access_token})

    @app.route("/api/drive-prototype/oauth/cancel", methods=["POST"])
    @_local_only
    def drive_prototype_oauth_cancel():
        data = request.get_json(silent=True) or {}
        flow_id = data.get("flow_id")
        if not isinstance(flow_id, str):
            return _json_response({"error": "驗證流程識別碼無效。"}, 400)
        reason = data.get("reason", "unspecified")
        allowed_reasons = {
            "picker_cancel",
            "picker_error",
            "picker_unexpected_action",
            "picker_no_documents",
            "picker_invalid_selection",
            "picker_missing_folder_id",
            "picker_not_folder",
            "missing_flow_id",
            "user_cancel",
            "cleanup",
            "unspecified",
        }
        if reason not in allowed_reasons:
            reason = "unspecified"
        with _flow_lock:
            flow = _flows.get(flow_id)
            if flow is None:
                return _json_response({"error": "驗證流程不存在或已逾時。"}, 404)
            if flow["status"] == "listing" or flow.get("download_in_progress"):
                return _json_response({"error": "資料夾列檔進行中，不能取消。"}, 409)
            if flow["status"] in ("complete", "failed", "expired", "cancelled"):
                flow["access_token"] = None
                return _json_response({"status": flow["status"]})
            flow["access_token"] = None
            flow["status"] = "cancelled"
            flow["cancel_reason"] = reason
        return _json_response({"status": "cancelled"})

    @app.route("/api/drive-prototype/files", methods=["POST"])
    @_local_only
    def drive_prototype_list_files():
        data = request.get_json(silent=True) or {}
        flow_id = data.get("flow_id")
        folder_id = data.get("folder_id")
        if not isinstance(flow_id, str) or not flow_id:
            return _json_response({"error": "OAuth 驗證流程識別碼無效，請重新開始授權。"}, 400)
        if not isinstance(folder_id, str) or not folder_id:
            return _json_response({"error": "Google Picker 未提供有效的資料夾識別碼，請重新選擇資料夾。"}, 400)

        with _flow_lock:
            flow = _flows.get(flow_id)
            if flow is None:
                return _json_response({"error": "OAuth 驗證流程不存在或已逾時，請重新授權。"}, 409)
            if flow["status"] != "authorized":
                if flow["status"] == "cancelled":
                    return _json_response({
                        "error": "OAuth 驗證流程已取消（來源：{0}），請重新授權。".format(
                            flow.get("cancel_reason") or "未記錄"
                        ),
                    }, 409)
                if flow["status"] == "listing":
                    return _json_response({"error": "資料夾列檔已在進行中，請稍候。"}, 409)
                if flow["status"] == "complete":
                    return _json_response({"error": "此流程的資料夾列檔已完成，請勿重複提交。"}, 409)
                return _json_response({
                    "error": flow["error"] or "OAuth 尚未完成授權，請重新授權。",
                }, 409)
            if not flow["token_claimed"]:
                return _json_response({"error": "Picker 尚未取得短效授權，請重新授權。"}, 409)
            if not flow["access_token"]:
                return _json_response({"error": "短效授權已到期或清除，請重新授權。"}, 409)
            if flow["list_started"]:
                return _json_response({"error": "此驗證流程已開始列檔，請勿重複提交。"}, 409)
            flow["list_started"] = True
            flow["list_started_at"] = time.time()
            flow["status"] = "listing"
            access_token = flow["access_token"]

        try:
            listing = _list_folder_children(folder_id, access_token)
        except ValueError as error:
            _update_flow(
                flow_id,
                status="failed",
                access_token=None,
                error=str(error),
            )
            return _json_response({"error": str(error)}, 400)
        except DriveApiError as error:
            error_summary = (
                "網路連線失敗"
                if error.status_code == 0
                else "HTTP {0}".format(error.status_code)
            )
            _update_flow(
                flow_id,
                status="failed",
                access_token=None,
                error="Google Drive API {0}。".format(error_summary),
            )
            return _json_response({
                "error": _drive_error_message(error, "資料夾列檔"),
                "http_status": error.status_code or None,
            }, 401 if error.status_code == 401 else 502)
        else:
            _update_flow(
                flow_id,
                status="listed",
                folder_id=folder_id,
            )
            # 列檔成功後才保存資料夾識別，避免把無效選取寫入此安裝設定。
            try:
                state = drive_account.load_account_state(account_path)
                if state is not None:
                    folder = listing.get("folder") or {}
                    state["folder_id"] = folder.get("id") or folder_id
                    state["folder_name"] = folder.get("name") or ""
                    diagnostics = listing.get("diagnostics") or {}
                    state["drive_id"] = "shared" if diagnostics.get("drive_type") == "shared_drive" else ""
                    drive_account.save_account_state(account_path, state)
            except drive_account.DriveAccountError as error:
                return _json_response({
                    "error": "已讀取資料夾，但無法保存此安裝的歌庫資料夾設定：{0}".format(error),
                }, 500)
            return _json_response(listing)

    @app.route("/api/drive-prototype/download", methods=["POST"])
    @_local_only
    def drive_prototype_download():
        _prune_flows()
        data = request.get_json(silent=True) or {}
        flow_id = data.get("flow_id")
        file_id = data.get("file_id")
        if not isinstance(flow_id, str) or not flow_id:
            return _json_response({"error": "OAuth 驗證流程識別碼無效，請重新授權。"}, 400)
        if not isinstance(file_id, str) or not file_id:
            return _json_response({"error": "Google Drive 歌曲識別碼無效，請重新列檔。"}, 400)

        with _flow_lock:
            flow = _flows.get(flow_id)
            if flow is None or flow["status"] != "listed":
                return _json_response({"error": "授權流程不存在、尚未列檔或已逾時，請重新授權。"}, 409)
            if flow.get("download_in_progress"):
                return _json_response({"error": "已有歌曲下載作業進行中，請稍候。"}, 409)
            if not flow.get("access_token") or not flow.get("folder_id"):
                return _json_response({"error": "短效授權或歌庫資料夾已清除，請重新授權及選擇資料夾。"}, 409)
            flow["download_in_progress"] = True
            folder_id = flow["folder_id"]
            access_token = flow["access_token"]

        try:
            item, expected_size = _validate_drive_song(folder_id, file_id, access_token)
            with _flow_lock:
                if flow_id not in _flows or _flows[flow_id].get("status") != "listed":
                    return _json_response({"error": "授權流程已結束，請重新授權。"}, 409)
            with _download_lock:
                safe_filename = _download_drive_media(
                    file_id,
                    access_token,
                    expected_size,
                    download_dir,
                )
                _save_downloaded_item(
                    download_dir,
                    folder_id,
                    item,
                    expected_size,
                    safe_filename,
                )
        except ValueError as error:
            return _json_response({"error": str(error)}, 400)
        except FileExistsError:
            return _json_response({"error": "此歌曲已存在於原型下載目錄，未覆寫既有檔案。"}, 409)
        except DriveApiError as error:
            summary = "網路連線失敗" if error.status_code == 0 else "HTTP {0}".format(error.status_code)
            return _json_response({
                "error": _drive_error_message(error, "原始媒體下載"),
                "http_status": error.status_code or None,
            }, 401 if error.status_code == 401 else 502)
        except DriveDownloadIntegrityError as error:
            return _json_response({"error": str(error)}, 502)
        except DriveStorageError as error:
            return _json_response({"error": str(error)}, 507)
        finally:
            with _flow_lock:
                flow = _flows.get(flow_id)
                if flow is not None:
                    flow["download_in_progress"] = False

        return _json_response({
            "status": "downloaded",
            "name": item.get("name", ""),
            "size": expected_size,
            "saved_as": safe_filename,
        })
