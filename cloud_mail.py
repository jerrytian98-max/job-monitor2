"""Server-owned SMTP settings for the multi-user cloud portal."""

from __future__ import annotations

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import yaml

from web_config import SECRET_MASK


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('JOB_MONITOR_CLOUD_DATA_DIR', BASE_DIR / 'cloud_data')).resolve()
ADMIN_EMAIL_FILE = DATA_DIR / 'admin-email.yaml'


def validate_receiver(value: object) -> str:
    address = str(value or '').strip()
    if address and (len(address) > 254 or not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', address)):
        raise ValueError('请输入有效的接收邮箱地址')
    return address


def load_admin_email() -> dict:
    if not ADMIN_EMAIL_FILE.exists():
        return {}
    with ADMIN_EMAIL_FILE.open(encoding='utf-8') as stream:
        value = yaml.safe_load(stream) or {}
    if not isinstance(value, dict):
        raise ValueError('管理员发信配置格式错误')
    return value


def validate_admin_email(submitted: dict, existing: dict | None = None) -> dict:
    if not isinstance(submitted, dict):
        raise ValueError('发信配置格式错误')
    existing = existing or {}
    sender = validate_receiver(submitted.get('sender'))
    auth_code = str(submitted.get('auth_code') or '').strip()
    if auth_code == SECRET_MASK:
        auth_code = str(existing.get('auth_code') or '')
    host = str(submitted.get('smtp_server') or '').strip()
    if not host or len(host) > 253 or not re.fullmatch(r'[A-Za-z0-9.-]+', host) or '..' in host:
        raise ValueError('请输入有效的 SMTP 服务器域名')
    try:
        port = int(submitted.get('smtp_port'))
    except (ValueError, TypeError):
        raise ValueError('SMTP 端口必须是整数')
    if not 1 <= port <= 65535:
        raise ValueError('SMTP 端口超出有效范围')
    if not sender or not auth_code or len(auth_code) > 512:
        raise ValueError('请填写发件邮箱和授权码')
    return {'sender': sender, 'auth_code': auth_code, 'smtp_server': host, 'smtp_port': port}


def save_admin_email(value: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    temp = ADMIN_EMAIL_FILE.with_suffix('.yaml.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        yaml.safe_dump(value, stream, allow_unicode=True, sort_keys=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, ADMIN_EMAIL_FILE)
    os.chmod(ADMIN_EMAIL_FILE, 0o600)


def redacted_admin_email(value: dict) -> dict:
    return {
        'sender': value.get('sender', ''),
        'auth_code': SECRET_MASK if value.get('auth_code') else '',
        'smtp_server': value.get('smtp_server', ''),
        'smtp_port': value.get('smtp_port', 465),
    }


def delivery_config(user_config: dict) -> dict:
    """Never trust tenant-provided sender fields, including legacy values."""
    shared = load_admin_email()
    user_email = user_config.get('email')
    receiver = validate_receiver(user_email.get('receiver') if isinstance(user_email, dict) else '')
    return {**shared, 'receiver': receiver}


def send_verification_code(address: str, code: str, purpose: str) -> bool:
    """Send a short-lived code without recording it in logs or files."""
    settings = load_admin_email()
    if not all(settings.get(key) for key in ('sender', 'auth_code', 'smtp_server', 'smtp_port')):
        return False
    message = EmailMessage()
    message['Subject'] = '招聘监测系统 - 邮箱验证码'
    message['From'] = settings['sender']
    message['To'] = address
    action = '注册账号' if purpose == 'register' else '绑定邮箱'
    message.set_content(f'你正在{action}。验证码：{code}\n\n验证码 10 分钟内有效。若非本人操作，请忽略本邮件。')
    try:
        port = int(settings['smtp_port'])
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(settings['smtp_server'], port, timeout=20, context=context) as server:
                server.login(settings['sender'], settings['auth_code'])
                server.send_message(message)
        else:
            with smtplib.SMTP(settings['smtp_server'], port, timeout=20) as server:
                server.starttls(context=context)
                server.login(settings['sender'], settings['auth_code'])
                server.send_message(message)
        return True
    except (OSError, smtplib.SMTPException, ValueError):
        return False
