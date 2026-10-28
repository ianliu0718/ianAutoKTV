"""Render WSGI entry point for the ianAutoKTV room-based web service."""

import os
import time
from collections import defaultdict, deque
from datetime import timedelta

from flask import abort, g, redirect, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

if os.environ.get('IANAUTOKTV_CLOUD_MODE') != '1':
    raise RuntimeError('Set IANAUTOKTV_CLOUD_MODE=1 when running the Render entry point.')

import main as ktv_main  # noqa: E402
from google_drive_cloud import register_room_google_drive  # noqa: E402

app = ktv_main.app
socketio = ktv_main.socketio
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or None
if not app.config['SECRET_KEY']:
    raise RuntimeError('Set SECRET_KEY in the Render environment.')

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_join_failures = defaultdict(deque)
_JOIN_RATE_WINDOW = 60
_JOIN_RATE_LIMIT = 10


@app.get('/healthz')
def cloud_health_check():
    return {'status': 'ok', 'service': 'ianAutoKTV'}, 200


def _rate_limited_join():
    now = time.time()
    failures = _join_failures[request.remote_addr or 'unknown']
    while failures and now - failures[0] > _JOIN_RATE_WINDOW:
        failures.popleft()
    if len(failures) >= _JOIN_RATE_LIMIT:
        return True
    return False


def _record_failed_join():
    _join_failures[request.remote_addr or 'unknown'].append(time.time())


def _join_room(code):
    room = ktv_main.get_cloud_room(code)
    if not room:
        _record_failed_join()
        return redirect('/rooms?error=找不到此房間，請確認房號或請房主重新建立。')
    session.clear()
    session.permanent = True
    session['room_code'] = code
    session['room_role'] = 'remote'
    return redirect('/remote')


@app.post('/api/rooms/create')
def create_room():
    code = ktv_main.create_cloud_room()
    session.clear()
    session.permanent = True
    session['room_code'] = code
    session['room_role'] = 'player'
    return redirect('/combo')


@app.post('/api/rooms/join')
def join_room_by_code():
    if _rate_limited_join():
        return redirect('/rooms?error=嘗試次數過多，請稍後再試。'), 429
    code = request.form.get('room_code', '').strip()
    if not code.isascii() or not code.isdigit() or len(code) != 8:
        _record_failed_join()
        return redirect('/rooms?error=請輸入 8 位數字房號。')
    return _join_room(code)


@app.get('/join/<code>')
def join_room_by_qr(code):
    if _rate_limited_join():
        return redirect('/rooms?error=嘗試次數過多，請稍後再試。'), 429
    if not code.isascii() or not code.isdigit() or len(code) != 8:
        _record_failed_join()
        return redirect('/rooms?error=QR Code 中的房號格式無效。')
    return _join_room(code)


@app.post('/api/rooms/close')
def close_room():
    code = session.get('room_code')
    if session.get('room_role') != 'player' or not code:
        abort(403)
    ktv_main.close_cloud_room(code)
    session.clear()
    return redirect('/')


@app.before_request
def require_room_membership():
    if request.endpoint == 'cloud_health_check' or request.endpoint == 'static':
        return None
    # The Socket.IO connect wrapper checks the session and joins its room.
    if request.path == '/socket.io' or request.path.startswith('/socket.io/'):
        return None
    if request.endpoint in {'page_index', 'page_rooms', 'create_room', 'join_room_by_code', 'join_room_by_qr'}:
        return None

    code = session.get('room_code')
    room = ktv_main.get_cloud_room(code)
    if not room:
        session.clear()
        if request.path.startswith('/api/'):
            return {'error': '請先建立房間或加入房間。'}, 401
        return redirect('/')

    if request.path == '/player' and session.get('room_role') != 'player':
        return redirect('/remote')
    if request.path.startswith(('/admin', '/api/videos/', '/api/subtitles/upload',
                                '/api/subtitles/manual', '/api/lyrics/download',
                                '/api/songs/rename', '/api/drive-prototype/')):
        abort(404)

    g.cloud_room_context = ktv_main.push_cloud_room_state(code)
    if not g.cloud_room_context:
        session.clear()
        if request.path.startswith('/api/'):
            return {'error': '房間已關閉，請重新加入。'}, 401
        return redirect('/')
    return None


@app.after_request
def restore_room_state(response):
    context = getattr(g, 'cloud_room_context', None)
    if context:
        ktv_main.pop_cloud_room_state(context)
    return response


application = app

register_room_google_drive(
    app,
    get_state=lambda: ktv_main.cloud_drive_state,
    get_download_dir=ktv_main._room_drive_download_dir,
)
