"""Per-room Google Drive folder Picker and temporary media cache for Render."""

import hashlib
import hmac
import os
import re
import secrets
import urllib.parse

from flask import abort, jsonify, redirect, request, session

import google_drive_prototype as drive_core


OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")


def _json(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def _credentials():
    return (
        os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        os.environ.get("GOOGLE_PICKER_API_KEY", "").strip(),
        os.environ.get("GOOGLE_PROJECT_NUMBER", "").strip(),
    )


def _redirect_uri():
    return request.url_root.rstrip("/") + "/api/drive/google/callback"


def _require_player():
    if session.get("room_role") != "player":
        abort(403)


def _access_token(state):
    if not state.get("refresh_token"):
        raise drive_core.OAuthFlowError("此房間尚未連結 Google Drive，請由播放器裝置先連結帳號。")
    client_id, client_secret, _api_key, _project_number = _credentials()
    if not client_id:
        raise drive_core.OAuthFlowError("Render 尚未設定 GOOGLE_CLIENT_ID。")
    try:
        token, _expires_in = drive_core._refresh_access_token(
            state["refresh_token"], client_id, client_secret or None,
        )
        return token
    except drive_core.OAuthFlowError as error:
        if "invalid_grant" in str(error):
            state.clear()
            raise drive_core.OAuthFlowError("Google 授權已失效，請由播放器裝置重新連結。") from None
        raise


def register_room_google_drive(app, get_state, get_download_dir):
    """Register room-scoped Google Drive OAuth, Picker and download routes."""

    @app.post("/api/drive/google/start")
    def google_drive_start():
        _require_player()
        client_id, client_secret, api_key, project_number = _credentials()
        if not all((client_id, client_secret, api_key, project_number)):
            return _json({"error": "Render 尚未設定 Google OAuth／Picker 用戶端資料。"}, 503)
        state = get_state()
        nonce = secrets.token_urlsafe(32)
        verifier, challenge = drive_core._make_pkce_pair()
        state["oauth_state"] = nonce
        state["oauth_verifier"] = verifier
        query = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": _redirect_uri(),
            "response_type": "code",
            "scope": DRIVE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return _json({"authorization_url": OAUTH_AUTH_URL + "?" + query})

    @app.get("/api/drive/google/callback")
    def google_drive_callback():
        _require_player()
        state = get_state()
        supplied_state = request.args.get("state", "")
        expected_state = state.pop("oauth_state", "")
        verifier = state.pop("oauth_verifier", "")
        if not expected_state or not hmac.compare_digest(supplied_state, expected_state):
            return "Google 授權狀態驗證失敗，請回到房間重新連結。", 400
        if request.args.get("error"):
            return redirect("/remote?drive=cancelled")
        code = request.args.get("code", "")
        client_id, client_secret, _api_key, _project_number = _credentials()
        if not code or not verifier or not client_id:
            return "Google 授權回應不完整，請回到房間重新連結。", 400
        try:
            _access, _expires, refresh_token = drive_core._exchange_code(
                code, client_id, verifier, _redirect_uri(), client_secret or None,
            )
        except drive_core.OAuthFlowError as error:
            return _json({"error": str(error)}, 502)
        state.clear()
        state.update({"refresh_token": refresh_token, "folder_id": "", "folder_name": ""})
        return redirect("/remote?drive=connected")

    @app.get("/api/drive/library-status")
    def google_drive_status():
        state = get_state()
        client_id, client_secret, api_key, project_number = _credentials()
        return _json({
            "configured": bool(client_id and client_secret and api_key and project_number),
            "linked": bool(state.get("refresh_token")),
            "folder_id": state.get("folder_id", ""),
            "folder_name": state.get("folder_name", ""),
            "song_count": state.get("song_count", 0),
        })

    @app.get("/api/drive/google/picker-token")
    def google_drive_picker_token():
        _require_player()
        state = get_state()
        _client_id, _secret, api_key, project_number = _credentials()
        if not api_key or not project_number:
            return _json({"error": "Render 尚未設定 Google Picker API Key 與 Project Number。"}, 503)
        try:
            token = _access_token(state)
        except drive_core.OAuthFlowError as error:
            return _json({"error": str(error)}, 409)
        # This short-lived access token goes only to the room owner so Google Picker can run.
        return _json({"access_token": token, "api_key": api_key, "project_number": project_number})

    @app.post("/api/drive/library-folder")
    def google_drive_select_folder():
        _require_player()
        state = get_state()
        folder_id = (request.get_json(silent=True) or {}).get("folder_id", "")
        if not isinstance(folder_id, str) or not FILE_ID_PATTERN.fullmatch(folder_id):
            return _json({"error": "Google Drive 資料夾識別碼無效。"}, 400)
        try:
            token = _access_token(state)
            folder = drive_core._get_drive_metadata(folder_id, token)
            if folder.get("id") != folder_id or folder.get("mimeType") != drive_core.DRIVE_FOLDER_MIME_TYPE:
                raise ValueError("請在 Google Picker 選擇資料夾。")
            listing = drive_core._list_folder_children(folder_id, token)
        except drive_core.OAuthFlowError as error:
            return _json({"error": str(error)}, 409)
        except ValueError as error:
            return _json({"error": str(error)}, 400)
        except drive_core.DriveApiError as error:
            return _json({"error": drive_core._drive_error_message(error, "資料夾讀取")}, 502)
        state["folder_id"] = folder_id
        state["folder_name"] = folder.get("name", "")
        state["song_count"] = len(listing.get("songs", []))
        return _json({"folder_name": state["folder_name"], "song_count": state["song_count"]})

    @app.get("/api/drive/library-songs")
    def google_drive_library_songs():
        state = get_state()
        folder_id = state.get("folder_id", "")
        if not folder_id:
            return _json({"error": "請先由房主選擇 Google Drive 歌曲資料夾。"}, 409)
        try:
            token = _access_token(state)
            listing = drive_core._list_folder_children(folder_id, token)
        except drive_core.OAuthFlowError as error:
            return _json({"error": str(error)}, 409)
        except ValueError as error:
            return _json({"error": str(error)}, 400)
        except drive_core.DriveApiError as error:
            return _json({"error": drive_core._drive_error_message(error, "歌庫列檔")}, 502)
        downloaded = {
            item["file_id"]: item
            for item in drive_core.get_downloaded_song_records(get_download_dir())
        }
        songs = []
        for item in listing.get("songs", []):
            record = downloaded.get(item.get("id"))
            songs.append({
                "id": item.get("id"),
                "name": item.get("name", ""),
                "size": item.get("size"),
                "has_lyrics": bool(item.get("lyrics")),
                "downloaded": record is not None,
                "filename": record.get("filename", "") if record else "",
            })
        state["song_count"] = len(songs)
        return _json({
            "folder": listing.get("folder", {}),
            "songs": songs,
            "song_count": len(songs),
        })

    @app.post("/api/drive/library-songs/download")
    def google_drive_download_song():
        file_id = (request.get_json(silent=True) or {}).get("file_id")
        state = get_state()
        folder_id = state.get("folder_id", "")
        if not folder_id or not isinstance(file_id, str):
            return _json({"error": "這首歌尚未加入目前房間的 Google 歌庫。"}, 404)
        try:
            token = _access_token(state)
            download_dir = get_download_dir()
            with drive_core._download_lock:
                item, expected_size = drive_core._validate_drive_song(folder_id, file_id, token)
                os.makedirs(download_dir, exist_ok=True)
                saved_filename = hashlib.sha256(file_id.encode("utf-8")).hexdigest() + ".mp4"
                existing_record = next((record for record in drive_core.get_downloaded_song_records(download_dir)
                                        if record["file_id"] == file_id), None)
                if existing_record:
                    return _json({"status": "downloaded", "filename": existing_record["filename"]})
                cached_song_path = os.path.join(download_dir, saved_filename)
                if os.path.isfile(cached_song_path):
                    if os.path.getsize(cached_song_path) != expected_size:
                        raise drive_core.DriveStorageError("房間內的歌曲快取大小不符，請重新部署後再下載。")
                else:
                    drive_core._download_drive_media(
                        file_id, token, expected_size, download_dir, ".mp4", saved_filename,
                    )

                subtitle_saved_as = None
                subtitle_item = drive_core._find_drive_song_lyrics(folder_id, item, token)
                if subtitle_item:
                    subtitle_item, subtitle_size, subtitle_extension = drive_core._validate_drive_subtitle(
                        folder_id, subtitle_item.get("id"), os.path.splitext(item.get("name", ""))[0], token,
                    )
                    subtitle_saved_as = hashlib.sha256(
                        subtitle_item["id"].encode("utf-8")
                    ).hexdigest() + subtitle_extension
                    subtitle_path = os.path.join(download_dir, subtitle_saved_as)
                    if os.path.isfile(subtitle_path):
                        if os.path.getsize(subtitle_path) != subtitle_size:
                            raise drive_core.DriveStorageError("房間內的歌詞快取大小不符，請重新部署後再下載。")
                    else:
                        drive_core._download_drive_media(
                            subtitle_item["id"], token, subtitle_size, download_dir,
                            subtitle_extension, subtitle_saved_as,
                        )
                drive_core._save_downloaded_item(
                    download_dir, folder_id, item, expected_size,
                    saved_filename, subtitle_saved_as,
                )
                metadata = drive_core._load_download_metadata(download_dir)
                public_source_name = drive_core._safe_public_filename(item.get("name", ""), file_id)
                stem, public_extension = os.path.splitext(public_source_name)
                public_filename = "{0}--{1}{2}".format(stem, file_id[:8], public_extension or ".mp4")
                metadata[file_id]["public_filename"] = public_filename
                drive_core._save_download_metadata(download_dir, metadata)
        except ValueError as error:
            return _json({"error": str(error)}, 400)
        except FileExistsError:
            existing = next((item for item in drive_core.get_downloaded_song_records(get_download_dir())
                             if item["file_id"] == file_id), None)
            if existing:
                return _json({"status": "downloaded", "filename": existing["filename"]})
            return _json({"error": "此歌曲的暫存檔已存在，請重新整理歌庫後重試。"}, 409)
        except drive_core.DriveApiError as error:
            return _json({"error": drive_core._drive_error_message(error, "歌曲下載")},
                         401 if error.status_code == 401 else 502)
        except drive_core.DriveDownloadIntegrityError as error:
            return _json({"error": str(error)}, 502)
        except drive_core.DriveStorageError as error:
            return _json({"error": str(error)}, 507)
        except OSError:
            return _json({"error": "Render 暫存空間無法寫入歌曲快取。"}, 507)
        return _json({"status": "downloaded", "filename": public_filename})
