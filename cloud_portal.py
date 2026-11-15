"""Account-based cloud entry point. Run behind an HTTPS reverse proxy."""

from __future__ import annotations

import hmac
import ipaddress
import io
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, UnidentifiedImageError
from nacl.encoding import Base64Encoder
from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from nacl.utils import random as random_bytes
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import yaml

from web_config import (
    JOBS_PER_PAGE, SECRET_MASK, preserve_masked_secrets, redact_config,
    unique_site_labels_by_domain, update_ai_filter_config, validate_config,
)
from database import JobDatabase
from job_tags import job_listing
from local_schedule import WeeklySchedule
from event_stream import event_cursor, encode_event_batch
from notifier import EmailNotifier
from cloud_mail import (
    delivery_config, load_admin_email, redacted_admin_email, save_admin_email,
    send_verification_code, validate_admin_email, validate_receiver,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('JOB_MONITOR_CLOUD_DATA_DIR', BASE_DIR / 'cloud_data')).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(DATA_DIR, 0o700)
ACCOUNT_DB = DATA_DIR / 'accounts.sqlite3'
SECRET_FILE = DATA_DIR / 'flask-secret'
if not SECRET_FILE.exists():
    try:
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_hex(32))
for _ in range(20):
    if SECRET_FILE.exists() and SECRET_FILE.stat().st_size:
        break
    time.sleep(0.05)

PASSWORD_KEY_FILE = DATA_DIR / 'password-vault.key'
if not PASSWORD_KEY_FILE.exists():
    try:
        fd = os.open(PASSWORD_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(random_bytes(SecretBox.KEY_SIZE))
for _ in range(20):
    if PASSWORD_KEY_FILE.exists() and PASSWORD_KEY_FILE.stat().st_size == SecretBox.KEY_SIZE:
        break
    time.sleep(0.05)
os.chmod(PASSWORD_KEY_FILE, 0o600)
_password_box = SecretBox(PASSWORD_KEY_FILE.read_bytes())

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = SECRET_FILE.read_text().strip()
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('JOB_MONITOR_HTTPS', '') == '1',
    MAX_CONTENT_LENGTH=256 * 1024,
)
_registration_lock = threading.Lock()
_verification_lock = threading.Lock()
CODE_LIFETIME = 600
CODE_COOLDOWN = 60
FEEDBACK_IMAGE_LIMIT = 3
FEEDBACK_IMAGE_BYTES = 5 * 1024 * 1024
FEEDBACK_REQUEST_BYTES = 16 * 1024 * 1024


@app.after_request
def private_response_headers(response):
    if request.path.startswith(('/admin', '/register', '/login', '/account', '/api/feedback')) or request.path in ('/api/config', '/api/runs'):
        response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@contextmanager
def connect():
    conn = sqlite3.connect(ACCOUNT_DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=20000')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                password_ciphertext TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                result TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_user
                ON runs(user_id) WHERE status IN ('queued','running','cancel_requested');
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS auth_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS attempts_lookup ON auth_attempts(identifier, created_at);
            CREATE TABLE IF NOT EXISTS admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL REFERENCES users(id),
                target_user_id INTEGER REFERENCES users(id),
                action TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS email_verifications (
                purpose TEXT NOT NULL,
                email TEXT NOT NULL,
                user_id INTEGER NOT NULL DEFAULT 0,
                code_digest TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                sent_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (purpose, email, user_id)
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS feedback_user ON feedback(user_id);
            CREATE TABLE IF NOT EXISTS feedback_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_id INTEGER NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
                mime_type TEXT NOT NULL,
                data BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS feedback_image_parent ON feedback_images(feedback_id);
        ''')
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(users)')}
        if 'is_admin' not in columns:
            conn.execute('ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0')
        if 'email' not in columns:
            conn.execute('ALTER TABLE users ADD COLUMN email TEXT')
        if 'email_verified' not in columns:
            conn.execute('ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0')
        if 'password_ciphertext' not in columns:
            conn.execute('ALTER TABLE users ADD COLUMN password_ciphertext TEXT')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users(email) WHERE email IS NOT NULL')
    os.chmod(ACCOUNT_DB, 0o600)


init_db()


def encrypt_password(password):
    """Keep a reversible, encrypted copy for the internal administrator portal."""
    encrypted = _password_box.encrypt(password.encode('utf-8'), encoder=Base64Encoder)
    return encrypted.decode('ascii')


def decrypt_password(ciphertext):
    if not ciphertext:
        return None
    try:
        decrypted = _password_box.decrypt(ciphertext.encode('ascii'), encoder=Base64Encoder)
        return decrypted.decode('utf-8')
    except (CryptoError, UnicodeDecodeError, ValueError):
        return None


class UserUnavailableError(LookupError):
    pass


def checked_tenant_path(user_id):
    """Resolve only a numbered tenant folder inside the private users folder."""
    user_id = int(user_id)
    if user_id <= 0:
        raise ValueError('无效的用户 ID')
    root = DATA_DIR / 'users'
    path = root / str(user_id)
    if root.is_symlink() or path.is_symlink() or path.resolve().parent != root:
        raise ValueError('用户数据目录异常，已取消操作')
    return path


def _tenant_dir_locked(conn, user_id):
    if not conn.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
        raise UserUnavailableError('用户不存在或已被删除')
    path = checked_tenant_path(user_id)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def tenant_dir(user_id):
    # Share SQLite's write lock with account deletion across Web and scheduler.
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return _tenant_dir_locked(conn, user_id)


@app.errorhandler(UserUnavailableError)
def unavailable_user(error):
    if current_user() is None:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify(success=False, message='账号已被删除，请重新登录'), 401
        return redirect(url_for('login'))
    return '用户不存在或已被删除', 404


def config_path(user_id):
    return tenant_dir(user_id) / 'config.yaml'


def load_config(user_id):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        path = _tenant_dir_locked(conn, user_id) / 'config.yaml'
        source = path if path.exists() else BASE_DIR / 'config.example.yaml'
        with source.open(encoding='utf-8') as stream:
            value = yaml.safe_load(stream) or {}
        if not path.exists():
            value['local_schedule'] = {'days': [], 'time': '21:00', 'enabled': False}
            user = conn.execute('SELECT email,email_verified FROM users WHERE id=?', (user_id,)).fetchone()
            if user and user['email_verified'] and user['email']:
                value['email'] = {'receiver': user['email']}
    return value


def save_config(user_id, value):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        path = _tenant_dir_locked(conn, user_id) / 'config.yaml'
        temp = path.with_suffix('.yaml.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            yaml.safe_dump(value, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if value.get('local_schedule'):
            WeeklySchedule(path).configure(value['local_schedule'])


def job_db(user_id):
    return JobDatabase(str(tenant_dir(user_id) / 'jobs.db'))


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


def current_user():
    user_id = session.get('user_id')
    if not isinstance(user_id, int):
        return None
    with connect() as conn:
        return conn.execute('SELECT id, username, email, email_verified, is_admin FROM users WHERE id=?', (user_id,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            if request.path.startswith('/api/'):
                return jsonify(success=False, message='请先登录'), 401
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for('login'))
        if not user['is_admin']:
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def public_user_config(config):
    public = redact_config(config)
    email = config.get('email')
    public['email'] = {'receiver': email.get('receiver', '') if isinstance(email, dict) else ''}
    return public


def admin_visible_config(config):
    """Allowlisted view of a tenant config; never show old SMTP or API secrets."""
    public = public_user_config(config)
    fields = (
        'job_sites', 'job_site_labels', 'job_site_modes', 'job_keywords',
        'cities', 'exclude_keywords', 'check_interval', 'local_schedule',
        'email', 'ai_provider', 'gemini_model', 'openai_model', 'deepseek_model',
        'ai_filter_prompt', 'gemini_api_key', 'openai_api_key', 'deepseek_api_key',
        'ai_filter_enabled',
    )
    return {key: public[key] for key in fields if key in public}


def audit_admin(admin_id, action, target_user_id=None):
    with connect() as conn:
        conn.execute(
            'INSERT INTO admin_audit(admin_id,target_user_id,action) VALUES (?,?,?)',
            (admin_id, target_user_id, action),
        )


@app.before_request
def protect_requests():
    if request.path == '/api/feedback' and request.method == 'POST':
        request.max_content_length = FEEDBACK_REQUEST_BYTES
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return
    submitted = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
    if not hmac.compare_digest(submitted, session.get('csrf', '')) or not submitted:
        abort(403)
    origin = request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        abort(403)


@app.errorhandler(413)
def request_too_large(error):
    if request.path == '/api/feedback':
        return jsonify(success=False, message='图片总大小过大，请减少图片或压缩后重试'), 413
    return error


def read_feedback_image(upload):
    data = upload.read(FEEDBACK_IMAGE_BYTES + 1)
    if not data or len(data) > FEEDBACK_IMAGE_BYTES:
        raise ValueError('每张图片不能超过 5 MB，且不能为空')
    formats = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'GIF': 'image/gif', 'WEBP': 'image/webp'}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                mime_type = formats.get(picture.format)
                if not mime_type or picture.width * picture.height > 24000000:
                    raise ValueError('请使用 PNG、JPEG、GIF 或 WebP 图片，分辨率不超过 2400 万像素')
                picture.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise ValueError('图片无法读取，请重新复制或选择有效的图片') from error
    return mime_type, data


@app.post('/api/feedback')
@login_required
def submit_feedback():
    user_id = current_user()['id']
    content = request.form.get('content', '').strip()
    if not content or len(content) > 5000:
        return jsonify(success=False, message='请填写问题描述，最多 5000 字'), 400
    uploads = request.files.getlist('images')
    if len(uploads) > FEEDBACK_IMAGE_LIMIT:
        return jsonify(success=False, message='每次最多添加 3 张图片'), 400
    try:
        images = [read_feedback_image(upload) for upload in uploads]
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 400
    if is_rate_limited(f'feedback-submit:{user_id}', 10, 60):
        return jsonify(success=False, message='提交过于频繁，请稍后再试'), 429
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            raise UserUnavailableError('用户不存在或已被删除')
        cursor = conn.execute('INSERT INTO feedback(user_id,content) VALUES (?,?)', (user_id, content))
        feedback_id = cursor.lastrowid
        conn.executemany('INSERT INTO feedback_images(feedback_id,mime_type,data) VALUES (?,?,?)',
                         [(feedback_id, mime_type, data) for mime_type, data in images])
    return jsonify(success=True, message='反馈已提交，管理员可在后台查看', data={'id': feedback_id}), 201


@app.get('/api/feedback/images/<int:image_id>')
@login_required
def feedback_image(image_id):
    user = current_user()
    with connect() as conn:
        picture = conn.execute(
            '''SELECT image.mime_type,image.data FROM feedback_images image
               JOIN feedback ON feedback.id=image.feedback_id
               WHERE image.id=? AND (feedback.user_id=? OR ?=1)''',
            (image_id, user['id'], user['is_admin']),
        ).fetchone()
    if picture is None:
        abort(404)
    return send_file(io.BytesIO(picture['data']), mimetype=picture['mime_type'], max_age=0)


def is_rate_limited(identifier, maximum, minutes):
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')
    with connect() as conn:
        conn.execute('DELETE FROM auth_attempts WHERE created_at < datetime(\'now\', \'-1 day\')')
        count = conn.execute(
            'SELECT COUNT(*) FROM auth_attempts WHERE identifier=? AND created_at>=?',
            (identifier, since),
        ).fetchone()[0]
        if count >= maximum:
            return True
        conn.execute('INSERT INTO auth_attempts(identifier) VALUES (?)', (identifier,))
    return False


def normalized_email(value):
    address = validate_receiver(value).lower()
    if not address:
        raise ValueError('请输入有效的邮箱地址')
    return address


def code_digest(purpose, address, user_id, code):
    payload = f'{purpose}\0{address}\0{user_id}\0{code}'.encode('utf-8')
    return hmac.new(app.secret_key.encode('utf-8'), payload, 'sha256').hexdigest()


def consume_code(conn, purpose, address, user_id, code):
    row = conn.execute(
        'SELECT code_digest,expires_at,attempts FROM email_verifications '
        'WHERE purpose=? AND email=? AND user_id=?', (purpose, address, user_id),
    ).fetchone()
    if not row or row['expires_at'] <= int(time.time()) or row['attempts'] >= 5:
        return False
    if not (len(code) == 6 and code.isascii() and code.isdigit() and
            hmac.compare_digest(row['code_digest'], code_digest(purpose, address, user_id, code))):
        conn.execute(
            'UPDATE email_verifications SET attempts=attempts+1 '
            'WHERE purpose=? AND email=? AND user_id=?', (purpose, address, user_id),
        )
        return False
    conn.execute(
        'DELETE FROM email_verifications WHERE purpose=? AND email=? AND user_id=?',
        (purpose, address, user_id),
    )
    return True


def issue_code(purpose, address, user_id=0):
    if not all(load_admin_email().get(key) for key in ('sender', 'auth_code', 'smtp_server', 'smtp_port')):
        return '管理员尚未配置发信邮箱，请稍后注册'
    if (is_rate_limited('code-global', 100, 60) or
            is_rate_limited(f'code-ip:{request.remote_addr}', 10, 60) or
            is_rate_limited(f'code-email:{address}', 3, 60)):
        return '验证码发送次数过多，请一小时后再试'
    with _verification_lock:
        with connect() as conn:
            row = conn.execute(
                'SELECT sent_at FROM email_verifications WHERE purpose=? AND email=? AND user_id=?',
                (purpose, address, user_id),
            ).fetchone()
            if row and int(time.time()) - row['sent_at'] < CODE_COOLDOWN:
                return '请一分钟后再发送验证码'
        code = f'{secrets.randbelow(1_000_000):06d}'
        if not send_verification_code(address, code, purpose):
            return '验证码邮件发送失败，请联系管理员检查发信邮箱'
        now = int(time.time())
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if user_id and not conn.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
                return '账号不存在或已被删除'
            conn.execute('DELETE FROM email_verifications WHERE expires_at<?', (now,))
            conn.execute('''INSERT INTO email_verifications
                (purpose,email,user_id,code_digest,expires_at,sent_at,attempts)
                VALUES (?,?,?,?,?,?,0)
                ON CONFLICT(purpose,email,user_id) DO UPDATE SET
                code_digest=excluded.code_digest, expires_at=excluded.expires_at,
                sent_at=excluded.sent_at, attempts=0''',
                (purpose, address, user_id, code_digest(purpose, address, user_id, code),
                 now + CODE_LIFETIME, now),
            )
    return None


@app.post('/register/code')
def register_code():
    if current_user():
        return jsonify(success=False, message='请先退出当前账号'), 400
    try:
        address = normalized_email(request.form.get('email'))
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 400
    with connect() as conn:
        exists = conn.execute('SELECT 1 FROM users WHERE email=? OR username=?', (address, address)).fetchone()
    if exists:
        return jsonify(success=False, message='该邮箱已注册，请直接登录'), 409
    error = issue_code('register', address)
    return (jsonify(success=False, message=error), 429 if '次数' in error or '一分钟' in error else 503) if error else jsonify(success=True, message='验证码已发送，10 分钟内有效')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user():
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        address = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        code = request.form.get('code', '').strip()
        if is_rate_limited('register:' + request.remote_addr, 5, 60):
            error = '注册过于频繁，请稍后再试'
        else:
            try:
                address = normalized_email(address)
                if not password:
                    raise ValueError('请输入密码')
                with _registration_lock, connect() as conn:
                    valid_code = consume_code(conn, 'register', address, 0, code)
                    if valid_code:
                        cursor = conn.execute(
                            '''INSERT INTO users
                               (username,email,email_verified,password_hash,password_ciphertext)
                               VALUES (?,?,1,?,?)''',
                            (address, address, generate_password_hash(password, method='scrypt'),
                             encrypt_password(password)),
                        )
                        user_id = cursor.lastrowid
                if not valid_code:
                    raise ValueError('验证码无效或已过期，请重新获取')
                config = load_config(user_id)
                config['email'] = {'receiver': address}
                save_config(user_id, config)
                session.clear()
                session['user_id'] = user_id
                csrf_token()
                return redirect(url_for('index'))
            except sqlite3.IntegrityError:
                error = '该邮箱已注册，请直接登录'
            except ValueError as exc:
                error = str(exc)
    return render_template('auth.html', mode='register', error=error, csrf_token=csrf_token(),
                           email=request.form.get('email', '') if request.method == 'POST' else '')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user():
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        username = request.form.get('email', request.form.get('username', '')).strip().lower()
        password = request.form.get('password', '')
        if is_rate_limited('login:' + request.remote_addr, 15, 15) or is_rate_limited('user:' + username, 15, 15):
            error = '尝试次数过多，请稍后再试'
        else:
            with connect() as conn:
                user = conn.execute('SELECT id,password_hash FROM users WHERE username=?', (username,)).fetchone()
            if user and check_password_hash(user['password_hash'], password):
                # Backfill accounts created before the encrypted password vault existed.
                with connect() as conn:
                    conn.execute('UPDATE users SET password_ciphertext=? WHERE id=?',
                                 (encrypt_password(password), user['id']))
                session.clear()
                session['user_id'] = user['id']
                csrf_token()
                return redirect(url_for('index'))
            error = '邮箱或密码错误（旧账号可使用原用户名）'
    return render_template('auth.html', mode='login', error=error, csrf_token=csrf_token(), email='')


@app.route('/account/email', methods=['GET', 'POST'])
@login_required
def bind_email():
    user = current_user()
    if user['email_verified']:
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        try:
            address = normalized_email(request.form.get('email'))
            code = request.form.get('code', '').strip()
            if is_rate_limited(f'bind-submit:{user["id"]}', 10, 60):
                raise ValueError('尝试次数过多，请稍后再试')
            with _registration_lock, connect() as conn:
                valid_code = consume_code(conn, 'bind', address, user['id'], code)
                if valid_code:
                    conflict = conn.execute(
                        'SELECT 1 FROM users WHERE id<>? AND (email=? OR username=?)',
                        (user['id'], address, address),
                    ).fetchone()
                    if conflict:
                        raise ValueError('该邮箱已被其他账号使用')
                    conn.execute('UPDATE users SET email=?,email_verified=1 WHERE id=?',
                                 (address, user['id']))
            if not valid_code:
                raise ValueError('验证码无效或已过期，请重新获取')
            config = load_config(user['id'])
            recipient = config.get('email', {}).get('receiver') if isinstance(config.get('email'), dict) else ''
            if not recipient:
                config['email'] = {'receiver': address}
                save_config(user['id'], config)
            return redirect(url_for('index'))
        except (ValueError, sqlite3.IntegrityError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else '该邮箱已被其他账号使用'
    return render_template('bind_email.html', error=error, csrf_token=csrf_token(),
                           email=request.form.get('email', '') if request.method == 'POST' else '')


@app.post('/account/email/code')
@login_required
def bind_email_code():
    user = current_user()
    if user['email_verified']:
        return jsonify(success=False, message='当前账号已验证邮箱'), 400
    try:
        address = normalized_email(request.form.get('email'))
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 400
    with connect() as conn:
        conflict = conn.execute('SELECT 1 FROM users WHERE id<>? AND (email=? OR username=?)',
                                (user['id'], address, address)).fetchone()
    if conflict:
        return jsonify(success=False, message='该邮箱已被其他账号使用'), 409
    error = issue_code('bind', address, user['id'])
    return (jsonify(success=False, message=error), 429 if '次数' in error or '一分钟' in error else 503) if error else jsonify(success=True, message='验证码已发送，10 分钟内有效')


@app.post('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))


def reject_internal_urls(urls):
    if len(urls) > 30:
        raise ValueError('每个账号最多配置 30 个招聘网址')
    for url in urls:
        parsed = urlparse(url)
        hostname = parsed.hostname or ''
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError('网址不能包含账号密码或非标准端口')
        try:
            ipaddress.ip_address(hostname)
            raise ValueError('请使用公开招聘网站的域名，不支持 IP 地址')
        except ValueError as error:
            if str(error).startswith('请使用'):
                raise
        if not hostname or '.' not in hostname or hostname.lower().endswith(('.local', '.internal', '.localhost')):
            raise ValueError('请使用公开招聘网站的域名')
        try:
            addresses = socket.getaddrinfo(hostname, parsed.port or 443)
        except socket.gaierror:
            raise ValueError(f'网址域名无法解析: {hostname}')
        for address in addresses:
            if not ipaddress.ip_address(address[4][0]).is_global:
                raise ValueError('招聘网址不能指向内网地址')


def record_event(user_id, payload):
    with connect() as conn:
        conn.execute('''INSERT INTO events(user_id,payload)
            SELECT id,? FROM users WHERE id=?''', (json.dumps(payload, ensure_ascii=False), user_id))
        conn.execute('''DELETE FROM events WHERE user_id=? AND id NOT IN
            (SELECT id FROM events WHERE user_id=? ORDER BY id DESC LIMIT 1000)''',
            (user_id, user_id))


def enqueue_run(user_id, kind='monitor'):
    with connect() as conn:
        try:
            cursor = conn.execute(
                'INSERT INTO runs(user_id,kind,status) VALUES (?,?,\'queued\')',
                (user_id, kind),
            )
        except sqlite3.IntegrityError:
            return False
    record_event(user_id, {'type': 'status', 'data': {'message': '抓取任务已排队', 'type': 'info', 'work_log': False}})
    return cursor.lastrowid


@app.get('/')
@login_required
def index():
    user = current_user()
    return render_template(
        'index.html', config=public_user_config(load_config(user['id'])),
        status={}, profile_display_name=user['username'], cloud_mode=True,
        csrf_token=csrf_token(), is_admin=bool(user['is_admin']),
        email_verified=bool(user['email_verified']),
    )


@app.get('/api/config')
@login_required
def get_config():
    return jsonify(success=True, data=public_user_config(load_config(current_user()['id'])))


@app.post('/api/config')
@login_required
def update_config():
    user_id = current_user()['id']
    submitted = request.get_json(silent=True)
    if not isinstance(submitted, dict):
        return jsonify(success=False, message='配置格式错误'), 400
    try:
        existing = load_config(user_id)
        raw_email = submitted.get('email')
        if not isinstance(raw_email, dict):
            raise ValueError('邮箱配置格式错误')
        recipient = validate_receiver(raw_email.get('receiver'))
        # Cloud users control only their recipient. Ignore legacy sender fields.
        submitted['email'] = {'receiver': recipient}
        value = validate_config(preserve_masked_secrets(submitted, existing))
        value['email'] = {'receiver': recipient}
        if 'local_schedule' not in value:
            raise ValueError('请设置每周运行时间')
        reject_internal_urls(value['job_sites'])
        save_config(user_id, value)
        return jsonify(success=True, message='配置保存成功，云端计划已生效')
    except (ValueError, OverflowError) as error:
        return jsonify(success=False, message=str(error)), 400


@app.post('/api/ai-filter')
@login_required
def update_ai_filter():
    user_id = current_user()['id']
    try:
        updated = update_ai_filter_config(load_config(user_id), request.get_json(silent=True))
        save_config(user_id, updated)
        return jsonify(success=True, message='AI 筛选设置已保存')
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 400


@app.post('/api/monitor/start')
@login_required
def start_monitor():
    user_id = current_user()['id']
    value = load_config(user_id)
    if not value.get('job_sites'):
        return jsonify(success=False, message='请先保存招聘网址'), 400
    run_id = enqueue_run(user_id)
    if not run_id:
        return jsonify(success=False, message='已有抓取任务正在等待或运行'), 409
    return jsonify(success=True, message='抓取任务已排队，云端会继续执行')


def pause_user_monitor(user_id):
    value = load_config(user_id)
    if value.get('local_schedule'):
        value['local_schedule']['enabled'] = False
        save_config(user_id, value)
    with connect() as conn:
        conn.execute('UPDATE runs SET status=\'cancelled\',finished_at=CURRENT_TIMESTAMP WHERE user_id=? AND status=\'queued\'', (user_id,))
        conn.execute('UPDATE runs SET status=\'cancel_requested\' WHERE user_id=? AND status=\'running\'', (user_id,))


@app.post('/api/monitor/stop')
@login_required
def stop_monitor():
    pause_user_monitor(current_user()['id'])
    return jsonify(success=True, message='已暂停定时计划，正在停止当前抓取')


@app.get('/api/monitor/status')
@login_required
def monitor_status():
    user_id = current_user()['id']
    with connect() as conn:
        run = conn.execute('SELECT status,started_at,finished_at FROM runs WHERE user_id=? ORDER BY id DESC LIMIT 1', (user_id,)).fetchone()
    stats = job_db(user_id).get_statistics()
    running = bool(run and run['status'] in ('queued', 'running', 'cancel_requested'))
    config = load_config(user_id)
    scheduled = bool(config.get('job_sites') and config.get('local_schedule', {}).get('enabled'))
    return jsonify(success=True, data={
        'status': 'stopping' if run and run['status'] == 'cancel_requested' else ('running' if running else ('scheduled' if scheduled else 'stopped')),
        'is_monitoring': running,
        'scheduled': scheduled,
        'last_check': run['finished_at'] if run else None,
        'running_time': run['started_at'] if run else None,
        'total_jobs_found': stats.get('total_jobs', 0),
        'new_jobs_today': stats.get('new_jobs_today', 0),
    })


@app.get('/api/runs')
@login_required
def get_runs():
    try:
        page = int(request.args.get('page', '1'))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    per_page = 15
    user_id = current_user()['id']
    with connect() as conn:
        total = conn.execute('SELECT COUNT(*) FROM runs WHERE user_id=?', (user_id,)).fetchone()[0]
        total_pages = math.ceil(total / per_page)
        page = min(page, total_pages) if total_pages else 1
        rows = conn.execute('''
            SELECT id,kind,status,requested_at,started_at,finished_at,result
            FROM runs WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?
        ''', (user_id, per_page, (page - 1) * per_page)).fetchall()
    runs = []
    for row in rows:
        try:
            result = json.loads(row['result']) if row['result'] else {}
        except (TypeError, ValueError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        sites = result.get('site_results')
        sites = [{
            'url': site.get('url', ''),
            'label': site.get('label', ''),
            'status': site.get('status', ''),
            'scraped_jobs': site.get('scraped_jobs', 0),
        } for site in sites if isinstance(site, dict)] if isinstance(sites, list) else None
        runs.append({
            'id': row['id'], 'kind': row['kind'], 'status': row['status'],
            'requested_at': row['requested_at'], 'started_at': row['started_at'],
            'finished_at': row['finished_at'], 'site_results': sites,
            'scraped_jobs': result.get('scraped_jobs'),
            'new_jobs': result.get('new_jobs'),
            'matched_jobs': result.get('matched_jobs'),
            'notification_sent': result.get('notification_sent'),
        })
    return jsonify(success=True, data=runs, pagination={
        'page': page, 'per_page': per_page, 'total': total, 'total_pages': total_pages,
    })


@app.post('/api/test/check')
@login_required
def test_check():
    user_id = current_user()['id']
    if not load_config(user_id).get('job_sites'):
        return jsonify(success=False, message='请先保存招聘网址'), 400
    if not enqueue_run(user_id, 'test'):
        return jsonify(success=False, message='已有抓取任务正在等待或运行'), 409
    return jsonify(success=True, message='测试抓取已排队', data={'queued': True})


@app.get('/api/jobs')
@login_required
def get_jobs():
    user_id = current_user()['id']
    return jsonify(job_listing(job_db(user_id), load_config(user_id), request.args, JOBS_PER_PAGE))


@app.get('/api/jobs/<int:job_id>')
@login_required
def get_job(job_id):
    job = job_db(current_user()['id']).get_job(job_id)
    return (jsonify(success=True, data=job) if job else (jsonify(success=False, message='职位不存在'), 404))


@app.post('/api/jobs/clear')
@login_required
def clear_jobs():
    user_id = current_user()['id']
    with connect() as conn:
        active = conn.execute('SELECT 1 FROM runs WHERE user_id=? AND status IN (\'queued\',\'running\',\'cancel_requested\')', (user_id,)).fetchone()
    if active:
        return jsonify(success=False, message='请等待抓取结束再清理'), 409
    database = job_db(user_id)
    if request.args.get('all', 'false').lower() == 'true':
        count = database.clear_all_jobs()
        (tenant_dir(user_id) / 'jobs_cache.json').write_text('{}', encoding='utf-8')
    else:
        count = database.clear_old_jobs(max(1, int(request.args.get('days', 30))))
    return jsonify(success=True, message=f'已清除 {count} 条职位记录')


@app.get('/api/statistics')
@login_required
def statistics():
    return jsonify(success=True, data=job_db(current_user()['id']).get_statistics())


@app.get('/api/events')
@login_required
def events():
    user_id = current_user()['id']
    with connect() as conn:
        latest = conn.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE user_id=?', (user_id,)).fetchone()[0]
        cursor = event_cursor(request.headers.get('Last-Event-ID'), latest)
        rows = conn.execute(
            'SELECT id,payload FROM events WHERE user_id=? AND id>? ORDER BY id LIMIT 50',
            (user_id, cursor),
        ).fetchall()
    return Response(
        encode_event_batch([(row['id'], row['payload']) for row in rows], cursor),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'},
    )


@app.post('/api/email/test')
@login_required
def test_email():
    user_id = current_user()['id']
    try:
        email = delivery_config(load_config(user_id))
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 400
    if not email.get('sender') or not email.get('auth_code'):
        return jsonify(success=False, message='管理员尚未配置发信邮箱'), 400
    if not email.get('receiver'):
        return jsonify(success=False, message='请先保存接收通知邮箱'), 400
    if is_rate_limited(f'email-test:{user_id}', 3, 60):
        return jsonify(success=False, message='测试邮件次数过多，请一小时后再试'), 429
    success = EmailNotifier(email).send_test_email(email['receiver'])
    return jsonify(success=success, message='测试邮件已发送' if success else '发送失败，请检查邮箱配置')


class UserDeletionError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def delete_user_data(admin_id, user_id, confirmation):
    """Delete one idle tenant; queued work and account removal are atomic."""
    staged = None
    directory = None
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        actor = conn.execute('SELECT is_admin FROM users WHERE id=?', (admin_id,)).fetchone()
        if not actor or not actor['is_admin']:
            raise UserDeletionError('管理员权限已失效，请重新登录', 403)
        target = conn.execute('SELECT id,username,email FROM users WHERE id=?', (user_id,)).fetchone()
        if not target:
            raise UserDeletionError('用户不存在或已被删除', 404)
        if user_id == admin_id:
            raise UserDeletionError('不能删除当前登录的管理员账号')
        if confirmation.strip() != target['username']:
            raise UserDeletionError('请输入要删除用户的完整账号，以确认删除')
        active = conn.execute(
            "SELECT 1 FROM runs WHERE user_id=? AND status IN ('running','cancel_requested')",
            (user_id,),
        ).fetchone()
        if active:
            raise UserDeletionError('该用户的抓取任务仍在运行或停止中，请先停止任务，结束后再删除', 409)

        directory = checked_tenant_path(user_id)
        try:
            if directory.exists():
                staging_root = DATA_DIR / '.deleting-users'
                if staging_root.is_symlink() or staging_root.resolve() != staging_root:
                    raise ValueError('用户数据清理目录异常，已取消操作')
                staging_root.mkdir(mode=0o700, exist_ok=True)
                staged = staging_root / f'{user_id}-{secrets.token_hex(12)}'
                directory.rename(staged)

            conn.execute('DELETE FROM events WHERE user_id=?', (user_id,))
            conn.execute('DELETE FROM runs WHERE user_id=?', (user_id,))
            addresses = {target['username'], target['email']} - {None, ''}
            conn.execute('DELETE FROM email_verifications WHERE user_id=?', (user_id,))
            identifiers = {f'email-test:{user_id}', f'bind-submit:{user_id}', f'feedback-submit:{user_id}'}
            for address in addresses:
                conn.execute('DELETE FROM email_verifications WHERE email=?', (address,))
                identifiers.update((f'user:{address}', f'code-email:{address}'))
            conn.executemany('DELETE FROM auth_attempts WHERE identifier=?',
                             [(identifier,) for identifier in identifiers])
            conn.execute('DELETE FROM admin_audit WHERE admin_id=? OR target_user_id=?', (user_id, user_id))
            conn.execute('DELETE FROM users WHERE id=?', (user_id,))
            # Retain an administrative action without retaining the deleted
            # username/email or a foreign key to the removed account.
            conn.execute('INSERT INTO admin_audit(admin_id,action) VALUES (?,?)',
                         (admin_id, f'delete_user:{user_id}'))
            conn.commit()
        except Exception:
            if staged is not None and staged.exists():
                staged.rename(directory)
            raise

    if staged is not None:
        try:
            if staged.is_symlink() or staged.resolve().parent != DATA_DIR / '.deleting-users':
                raise ValueError('用户数据清理目录异常')
            shutil.rmtree(staged)
        except (OSError, ValueError):
            app.logger.exception('删除账号 %s 后清理文件失败', user_id)
            return False
    return True


def render_admin(selected_user_id=None, error=None, notice=None, show_passwords=False, show_feedback=False):
    user = current_user()
    with connect() as conn:
        rows = conn.execute(
            '''SELECT id,username,email,email_verified,is_admin,created_at,password_ciphertext
               FROM users ORDER BY id'''
        ).fetchall()
    users = [dict(row) for row in rows]
    if show_passwords:
        for item in users:
            item['password_plaintext'] = decrypt_password(item.pop('password_ciphertext'))
        audit_admin(user['id'], 'view_all_user_passwords')
    else:
        for item in users:
            item.pop('password_ciphertext')
    selected = next((item for item in users if item['id'] == selected_user_id), None)
    if selected_user_id is not None and selected is None:
        abort(404)
    config_text = None
    selected_active_status = None
    feedback_items = []
    feedback_total = 0
    feedback_page = 1
    feedback_pages = 1
    if show_feedback:
        with connect() as conn:
            feedback_total = conn.execute('SELECT COUNT(*) FROM feedback').fetchone()[0]
            feedback_pages = max(1, math.ceil(feedback_total / 20))
            feedback_page = min(max(1, request.args.get('page', 1, type=int) or 1), feedback_pages)
            feedback_items = [dict(row) for row in conn.execute(
                '''SELECT feedback.id,feedback.content,users.username,
                          datetime(feedback.created_at,'+8 hours') AS submitted_at
                   FROM feedback JOIN users ON users.id=feedback.user_id
                   ORDER BY feedback.id DESC LIMIT 20 OFFSET ?''', ((feedback_page - 1) * 20,),
            ).fetchall()]
            for item in feedback_items:
                item['images'] = [row['id'] for row in conn.execute(
                    'SELECT id FROM feedback_images WHERE feedback_id=? ORDER BY id', (item['id'],),
                ).fetchall()]
    if selected:
        path = config_path(selected['id'])
        config_text = yaml.safe_dump(
            admin_visible_config(load_config(selected['id'])),
            allow_unicode=True, sort_keys=False,
        ) if path.exists() else '该用户尚未保存配置。'
        audit_admin(user['id'], 'view_user_config', selected['id'])
        with connect() as conn:
            active = conn.execute(
                "SELECT status FROM runs WHERE user_id=? AND status IN ('running','cancel_requested')",
                (selected['id'],),
            ).fetchone()
        selected_active_status = active['status'] if active else None
    return render_template(
        'admin.html', users=users, selected=selected, config_text=config_text,
        admin_email=redacted_admin_email(load_admin_email()), csrf_token=csrf_token(),
        error=error, notice=notice, current_username=user['username'],
        current_user_id=user['id'], show_passwords=show_passwords,
        selected_active_status=selected_active_status,
        show_feedback=show_feedback, feedback_items=feedback_items,
        feedback_total=feedback_total, feedback_page=feedback_page, feedback_pages=feedback_pages,
    )


@app.get('/admin')
@admin_required
def admin_home():
    notice = '发信邮箱配置已保存' if request.args.get('saved') == '1' else None
    if request.args.get('deleted') == '1':
        notice = '用户账号及其配置、职位、任务和日志已删除'
    return render_admin(notice=notice)


@app.get('/admin/passwords')
@admin_required
def admin_passwords():
    return render_admin(show_passwords=True)


@app.get('/admin/feedback')
@admin_required
def admin_feedback():
    return render_admin(show_feedback=True)


@app.get('/admin/users/<int:user_id>/config')
@admin_required
def admin_user_config(user_id):
    notice = '已暂停该用户的定时计划并请求停止抓取，任务结束后刷新此页即可删除' if request.args.get('stopping') == '1' else None
    return render_admin(selected_user_id=user_id, notice=notice)


@app.post('/admin/users/<int:user_id>/stop')
@admin_required
def admin_stop_user(user_id):
    pause_user_monitor(user_id)
    audit_admin(current_user()['id'], 'stop_user_monitor', user_id)
    return redirect(url_for('admin_user_config', user_id=user_id, stopping='1'), code=303)


@app.post('/admin/users/<int:user_id>/delete')
@admin_required
def admin_delete_user(user_id):
    try:
        cleaned = delete_user_data(current_user()['id'], user_id,
                                   request.form.get('confirmation', ''))
    except UserDeletionError as error:
        if error.status in (403, 404):
            abort(error.status)
        return render_admin(selected_user_id=user_id, error=str(error)), error.status
    except (OSError, ValueError, sqlite3.Error):
        app.logger.exception('删除用户 %s 失败，已保留账号', user_id)
        return render_admin(selected_user_id=user_id, error='删除失败，账号已保留，请稍后重试'), 500
    if not cleaned:
        return render_admin(error='账号已删除，但部分文件清理失败，请联系维护人员处理'), 500
    return redirect(url_for('admin_home', deleted='1'), code=303)


@app.post('/admin/email')
@admin_required
def admin_save_email():
    try:
        value = validate_admin_email(request.form.to_dict(), load_admin_email())
    except ValueError as error:
        return render_admin(error=str(error)), 400
    save_admin_email(value)
    audit_admin(current_user()['id'], 'save_sender_config')
    return redirect(url_for('admin_home', saved='1'))


@app.get('/healthz')
def healthz():
    return jsonify(ok=True)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('CLOUD_PORT', '8000')), threaded=True)
