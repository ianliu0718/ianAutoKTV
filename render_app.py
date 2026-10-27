"""Render WSGI entry point for the ianAutoKTV web service."""

import hmac
import os
from datetime import timedelta

from flask import abort, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix


if os.environ.get('IANAUTOKTV_CLOUD_MODE') != '1':
    raise RuntimeError('Set IANAUTOKTV_CLOUD_MODE=1 when running the Render entry point.')

CLOUD_ACCESS_CODE = os.environ.get('CLOUD_ACCESS_CODE', '').strip()
if len(CLOUD_ACCESS_CODE) < 12:
    raise RuntimeError('Set CLOUD_ACCESS_CODE to a private code of at least 12 characters.')

from main import app, socketio  # noqa: E402

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


@app.get('/healthz')
def cloud_health_check():
    return {'status': 'ok', 'service': 'ianAutoKTV'}, 200


@app.route('/cloud-login', methods=['GET', 'POST'])
def cloud_login():
    error = ''
    if request.method == 'POST':
        supplied_code = request.form.get('access_code', '')
        if hmac.compare_digest(supplied_code, CLOUD_ACCESS_CODE):
            session.clear()
            session.permanent = True
            session['cloud_authenticated'] = True
            destination = request.form.get('next', '')
            if destination.startswith('/') and not destination.startswith('//') and '\\' not in destination:
                return redirect(destination)
            return redirect('/player')
        error = '登入碼不正確，請再試一次。'
    return render_template('cloud_login.html', error=error, next=request.args.get('next', ''))


@app.before_request
def require_cloud_login():
    if request.endpoint == 'cloud_health_check' or request.endpoint == 'cloud_login':
        return None
    if request.endpoint == 'static':
        return None
    # Socket.IO performs the authorization check in its connect handler, after
    # the HTTP upgrade/polling request reaches Flask-SocketIO.
    if request.path == '/socket.io' or request.path.startswith('/socket.io/'):
        return None
    if not session.get('cloud_authenticated'):
        if request.path.startswith('/api/'):
            return {'error': '需要先登入 ianAutoKTV。'}, 401
        return redirect(url_for('cloud_login', next=request.full_path.rstrip('?')))

    # Cloud playback does not expose the desktop's conversion and library-admin
    # endpoints. Songs are intended to come from the user's own storage.
    if request.path == '/admin' or request.path.startswith((
        '/api/videos/',
        '/api/subtitles/upload',
        '/api/subtitles/manual',
        '/api/lyrics/download',
        '/api/songs/rename',
        '/api/drive-prototype/',
    )):
        abort(404)
    return None


application = app

