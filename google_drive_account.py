"""KTV 主機專用的 Google 歌庫部署設定與 refresh token 保護儲存。"""

import ctypes
import json
import os
import tempfile
import threading
from ctypes import wintypes


# 部署檔與權杖檔都放在該 KTV 安裝目錄，以隔離不同安裝。
DEPLOY_FILENAME = "google_drive_deploy.json"
DEPLOY_EXAMPLE_FILENAME = "google_drive_deploy.example.json"
ACCOUNT_FILENAME = "google_drive_account.bin"
CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"
CRYPTPROTECT_UI_FORBIDDEN = 0x01

_account_lock = threading.Lock()


class DATA_BLOB(ctypes.Structure):
    """Windows DPAPI 輸入／輸出緩衝區。"""

    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class DriveAccountError(Exception):
    """可顯示給管理者、不含權杖內容的帳號儲存錯誤。"""


def deploy_config_path(app):
    """回傳此 KTV 安裝的 Google 部署設定路徑。"""
    return app.config.get(
        "GOOGLE_DRIVE_DEPLOY_PATH",
        os.path.join(app.root_path, DEPLOY_FILENAME),
    )


def account_store_path(app):
    """回傳此 KTV 安裝的加密帳號狀態路徑。"""
    return app.config.get(
        "GOOGLE_DRIVE_ACCOUNT_PATH",
        os.path.join(app.root_path, ACCOUNT_FILENAME),
    )


def _protect_bytes(data):
    """以目前 Windows 使用者 DPAPI 加密，不彈出系統對話框。"""
    if os.name != "nt":
        raise DriveAccountError("此 KTV 主機無法使用 Windows 保護儲存 Google 權杖。")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise DriveAccountError("無法保護空白的 Google 帳號資料。")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(bytes(data))
    in_blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise DriveAccountError("KTV 主機無法加密保存 Google 授權資料。")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect_bytes(data):
    """解密由此 KTV 主機使用者保護的帳號資料。"""
    if os.name != "nt":
        raise DriveAccountError("此 KTV 主機無法使用 Windows 保護儲存 Google 權杖。")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise DriveAccountError("Google 授權資料損毀，請重新連結帳號。")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(bytes(data))
    in_blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise DriveAccountError("KTV 主機無法讀取已保存的 Google 授權，請重新連結帳號。")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def load_deploy_config(app):
    """讀取伺服器端部署設定；終端使用者不得從瀏覽器提交這些值。"""
    path = deploy_config_path(app)
    # 相容目前部署方式：若正式設定檔尚未另存，但使用者已填妥 example 檔，
    # 允許伺服器先讀取 example 檔；正式 google_drive_deploy.json 仍具有優先權。
    if not os.path.isfile(path):
        example_path = os.path.join(os.path.dirname(path), DEPLOY_EXAMPLE_FILENAME)
        if os.path.isfile(example_path):
            path = example_path
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise DriveAccountError(
            "尚未設定 Google 部署檔。請在 KTV 主機複製 google_drive_deploy.example.json "
            "為 google_drive_deploy.json 並填入 Desktop OAuth 與 Picker 設定。"
        ) from None
    except OSError:
        raise DriveAccountError("無法讀取 KTV 主機的 Google 部署設定檔。") from None
    except ValueError:
        raise DriveAccountError("Google 部署設定檔格式無效，請檢查 JSON。") from None

    if not isinstance(payload, dict):
        raise DriveAccountError("Google 部署設定檔格式無效。")

    client_id = payload.get("oauth_client_id", "")
    client_secret = payload.get("oauth_client_secret", "")
    picker_api_key = payload.get("picker_api_key", "")
    project_number = payload.get("project_number", "")
    if not all(isinstance(value, str) for value in (
        client_id, client_secret, picker_api_key, project_number,
    )):
        raise DriveAccountError("Google 部署設定必須使用文字欄位。")

    client_id = client_id.strip()
    client_secret = client_secret.strip()
    picker_api_key = picker_api_key.strip()
    project_number = project_number.strip()
    if (
        len(client_id) > 512
        or not client_id.endswith(CLIENT_ID_SUFFIX)
        or len(client_secret) > 512
        or not picker_api_key
        or len(picker_api_key) > 512
        or not project_number.isdigit()
        or len(project_number) > 32
    ):
        raise DriveAccountError(
            "請確認部署檔中的 Desktop OAuth Client ID、Picker API Key 與純數字 Project Number。"
        )
    return {
        "oauth_client_id": client_id,
        "oauth_client_secret": client_secret or None,
        "picker_api_key": picker_api_key,
        "project_number": project_number,
    }


def public_library_status(state, deploy_error=None):
    """回傳可給區網顯示的共用歌庫狀態，不含權杖或用戶端密鑰。"""
    state = state or {}
    return {
        "linked": bool(state.get("refresh_token")),
        "expired": bool(state.get("auth_expired")),
        "account_email": state.get("account_email") or "",
        "account_name": state.get("account_name") or "",
        "folder_id": state.get("folder_id") or "",
        "folder_name": state.get("folder_name") or "",
        "deploy_configured": deploy_error is None,
        "deploy_error": deploy_error,
    }


def load_account_state(path):
    """讀取並解密此安裝保存的 Google 帳號狀態。"""
    with _account_lock:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as handle:
                protected = handle.read()
            payload = json.loads(_unprotect_bytes(protected).decode("utf-8"))
        except DriveAccountError:
            raise
        except (OSError, UnicodeDecodeError, ValueError):
            raise DriveAccountError("已保存的 Google 授權資料無法讀取，請重新連結帳號。") from None
        if not isinstance(payload, dict):
            raise DriveAccountError("已保存的 Google 授權資料無效，請重新連結帳號。")
        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise DriveAccountError("已保存的 Google 授權缺少 refresh token，請重新連結帳號。")
        return {
            "refresh_token": refresh_token,
            "account_email": payload.get("account_email") if isinstance(payload.get("account_email"), str) else "",
            "account_name": payload.get("account_name") if isinstance(payload.get("account_name"), str) else "",
            "folder_id": payload.get("folder_id") if isinstance(payload.get("folder_id"), str) else "",
            "folder_name": payload.get("folder_name") if isinstance(payload.get("folder_name"), str) else "",
            "drive_id": payload.get("drive_id") if isinstance(payload.get("drive_id"), str) else "",
            "auth_expired": bool(payload.get("auth_expired")),
        }


def save_account_state(path, state):
    """以 DPAPI 加密後原子寫入帳號狀態，不留下明文權杖檔。"""
    refresh_token = state.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise DriveAccountError("缺少可保存的 Google refresh token。")
    payload = {
        "refresh_token": refresh_token,
        "account_email": state.get("account_email") or "",
        "account_name": state.get("account_name") or "",
        "folder_id": state.get("folder_id") or "",
        "folder_name": state.get("folder_name") or "",
        "drive_id": state.get("drive_id") or "",
        "auth_expired": bool(state.get("auth_expired")),
    }
    protected = _protect_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    directory = os.path.dirname(path) or "."
    with _account_lock:
        try:
            os.makedirs(directory, exist_ok=True)
            handle, temporary_path = tempfile.mkstemp(
                prefix=".google-drive-account-",
                suffix=".tmp",
                dir=directory,
            )
        except OSError:
            raise DriveAccountError("無法在 KTV 主機建立 Google 授權暫存檔。") from None
        try:
            with os.fdopen(handle, "wb") as output:
                output.write(protected)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, path)
        except OSError:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise DriveAccountError("無法保存 Google 授權至 KTV 主機。") from None


def clear_account_state(path):
    """刪除本機保存的 refresh token 與資料夾設定。"""
    with _account_lock:
        if not os.path.exists(path):
            return
        try:
            os.remove(path)
        except OSError:
            raise DriveAccountError("無法清除 KTV 主機保存的 Google 授權。") from None


def merge_linked_account(existing, refresh_token, user):
    """連結新帳號；若 Google 帳號變更則清除舊資料夾選擇。"""
    email = user.get("email") if isinstance(user.get("email"), str) else ""
    name = user.get("name") if isinstance(user.get("name"), str) else ""
    existing = existing or {}
    same_account = bool(existing.get("account_email")) and existing.get("account_email") == email
    return {
        "refresh_token": refresh_token,
        "account_email": email,
        "account_name": name,
        "folder_id": existing.get("folder_id") if same_account else "",
        "folder_name": existing.get("folder_name") if same_account else "",
        "drive_id": existing.get("drive_id") if same_account else "",
        "auth_expired": False,
    }
