"""Local-to-GitHub configuration upload and job-state download."""

from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import time
from typing import Optional
from urllib.parse import urlparse
import zipfile

from nacl import encoding, public
import requests
import yaml

from config_bootstrap import BASE_DIR
from github_config import build_secret_value


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
CONFIG_SECRET_NAME = "JOB_MONITOR_CONFIG_B64"
ARTIFACT_NAME = "job-monitor-state"
SETTINGS_FILE = BASE_DIR / "github_sync.yaml"
TOKEN_ENV_NAME = "JOB_MONITOR_GITHUB_TOKEN"
TOKEN_MASK = "••••••••••••"
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
DB_NAME = re.compile(r"^jobs(?:_[A-Za-z0-9_-]+)?\.db$", re.IGNORECASE)
CACHE_NAME = re.compile(
    r"^jobs_cache(?:_[A-Za-z0-9_-]+)?\.json$",
    re.IGNORECASE,
)


class GitHubSyncError(RuntimeError):
    """A user-facing GitHub synchronization failure."""


def normalize_repository(value: str) -> str:
    """Normalize owner/repository or a github.com repository URL."""
    raw = str(value or "").strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != "github.com":
            raise ValueError("仓库地址必须是 github.com 链接")
        raw = parsed.path.strip("/")

    parts = raw.split("/")
    if (
        len(parts) != 2
        or not all(REPOSITORY_PART.fullmatch(part or "") for part in parts)
        or any(part in (".", "..") for part in parts)
    ):
        raise ValueError("请输入 owner/repository 或完整的 GitHub 仓库网址")
    return f"{parts[0]}/{parts[1]}"


def load_settings() -> dict:
    """Load local-only GitHub settings and optional token environment override."""
    settings = {}
    if SETTINGS_FILE.exists():
        try:
            loaded = yaml.safe_load(SETTINGS_FILE.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                settings = loaded
        except (OSError, yaml.YAMLError) as error:
            raise GitHubSyncError(f"读取 GitHub 同步设置失败: {error}") from error

    environment_token = os.environ.get(TOKEN_ENV_NAME, "").strip()
    if environment_token:
        settings["token"] = environment_token
    return {
        "repository": str(settings.get("repository", "")).strip(),
        "token": str(settings.get("token", "")).strip(),
    }


def public_settings() -> dict:
    settings = load_settings()
    return {
        "repository": settings["repository"],
        "token": TOKEN_MASK if settings["token"] else "",
        "has_token": bool(settings["token"]),
        "token_from_environment": bool(os.environ.get(TOKEN_ENV_NAME, "").strip()),
    }


def save_settings(repository: str, token: str) -> dict:
    """Persist repository and token locally; masked input preserves the old token."""
    normalized_repository = normalize_repository(repository)
    existing = load_settings()
    clean_token = str(token or "").strip()
    if clean_token == TOKEN_MASK:
        clean_token = existing.get("token", "")
    if not clean_token:
        raise ValueError("请输入 GitHub Personal Access Token")

    data = {
        "repository": normalized_repository,
        "token": clean_token,
    }
    temporary_path = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(temporary_path, SETTINGS_FILE)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return public_settings()


class GitHubClient:
    def __init__(
        self,
        repository: str,
        token: str,
        session: Optional[requests.Session] = None,
    ):
        self.repository = normalize_repository(repository)
        self.token = str(token or "").strip()
        if not self.token:
            raise ValueError("GitHub Token 未配置")
        self.session = session or requests.Session()
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "job-monitor-local-sync",
        }

    def _url(self, path: str) -> str:
        return f"{API_ROOT}{path}"

    def _error_message(self, response: requests.Response, operation: str) -> str:
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("message", "")).strip()
        except (ValueError, requests.RequestException):
            pass
        if response.status_code == 401:
            return "GitHub Token 无效或已过期"
        if response.status_code == 403:
            return "GitHub Token 权限不足；需要 Actions 读取和 Secrets 写入权限"
        if response.status_code == 404:
            return f"{operation}失败：仓库、Secret 接口或数据包不存在"
        suffix = f"：{detail}" if detail else ""
        return f"{operation}失败（GitHub HTTP {response.status_code}）{suffix}"

    def _request(self, method: str, path: str, operation: str, **kwargs):
        try:
            response = self.session.request(
                method,
                self._url(path),
                headers=self.headers,
                timeout=30,
                **kwargs,
            )
        except requests.RequestException as error:
            raise GitHubSyncError(f"{operation}失败：无法连接 GitHub（{error}）") from error
        if not 200 <= response.status_code < 300:
            raise GitHubSyncError(self._error_message(response, operation))
        return response

    def update_repository_secret(self, name: str, value: str) -> str:
        public_key_response = self._request(
            "GET",
            f"/repos/{self.repository}/actions/secrets/public-key",
            "读取仓库公钥",
        )
        try:
            public_key_data = public_key_response.json()
            key_id = public_key_data["key_id"]
            key = public.PublicKey(
                public_key_data["key"].encode("ascii"),
                encoding.Base64Encoder(),
            )
            encrypted_value = base64.b64encode(
                public.SealedBox(key).encrypt(value.encode("utf-8"))
            ).decode("ascii")
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubSyncError("GitHub 返回了无效的仓库公钥") from error

        response = self._request(
            "PUT",
            f"/repos/{self.repository}/actions/secrets/{name}",
            "上传配置",
            json={
                "encrypted_value": encrypted_value,
                "key_id": key_id,
            },
        )
        return "created" if response.status_code == 201 else "updated"

    def latest_state_artifact(self) -> dict:
        response = self._request(
            "GET",
            f"/repos/{self.repository}/actions/artifacts",
            "查询职位数据包",
            params={"name": ARTIFACT_NAME, "per_page": 100},
        )
        try:
            artifacts = response.json().get("artifacts", [])
        except (AttributeError, ValueError) as error:
            raise GitHubSyncError("GitHub 返回了无效的数据包列表") from error
        available = [
            artifact
            for artifact in artifacts
            if isinstance(artifact, dict) and not artifact.get("expired")
        ]
        if not available:
            raise GitHubSyncError(
                "没有找到可下载的职位数据。请先成功运行一次 Daily Job Monitor。"
            )
        return max(
            available,
            key=lambda artifact: str(artifact.get("created_at", "")),
        )

    def download_artifact(self, artifact_id: int) -> bytes:
        response = self._request(
            "GET",
            f"/repos/{self.repository}/actions/artifacts/{artifact_id}/zip",
            "下载职位数据",
            allow_redirects=True,
        )
        content_length = int(response.headers.get("Content-Length", "0") or 0)
        if content_length > MAX_ARTIFACT_BYTES or len(response.content) > MAX_ARTIFACT_BYTES:
            raise GitHubSyncError("GitHub 职位数据包超过 100 MB，已停止下载")
        return response.content


def client_from_settings(session: Optional[requests.Session] = None) -> GitHubClient:
    settings = load_settings()
    if not settings["repository"] or not settings["token"]:
        raise ValueError("请先保存 GitHub 仓库和 Token")
    return GitHubClient(settings["repository"], settings["token"], session=session)


def upload_local_configuration(client: Optional[GitHubClient] = None) -> dict:
    """Upload every local profile, including its credentials, as one secret."""
    github_client = client or client_from_settings()
    secret_value = build_secret_value(include_sensitive_values=True)
    result = github_client.update_repository_secret(CONFIG_SECRET_NAME, secret_value)
    return {
        "repository": github_client.repository,
        "secret_name": CONFIG_SECRET_NAME,
        "result": result,
    }


def _validated_state_files(archive_bytes: bytes, staging: Path) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(BytesIO(archive_bytes))
    except zipfile.BadZipFile as error:
        raise GitHubSyncError("下载的职位数据包不是有效的 ZIP 文件") from error

    files = {}
    total_uncompressed = 0
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            path = PurePosixPath(info.filename)
            if len(path.parts) != 1:
                raise GitHubSyncError("职位数据包包含不安全的目录路径")
            name = path.name
            if not (DB_NAME.fullmatch(name) or CACHE_NAME.fullmatch(name)):
                raise GitHubSyncError(f"职位数据包包含不支持的文件: {name}")
            if name in files:
                raise GitHubSyncError(f"职位数据包包含重复文件: {name}")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_ARTIFACT_BYTES:
                raise GitHubSyncError("解压后的职位数据超过 100 MB，已停止恢复")
            files[name] = archive.read(info)

    if not any(DB_NAME.fullmatch(name) for name in files):
        raise GitHubSyncError("职位数据包中没有数据库文件")

    for name, content in files.items():
        staged_path = staging / name
        staged_path.write_bytes(content)
        if DB_NAME.fullmatch(name):
            try:
                connection = sqlite3.connect(str(staged_path))
                check = connection.execute("PRAGMA quick_check").fetchone()
                has_jobs = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
                ).fetchone()
            except sqlite3.DatabaseError as error:
                raise GitHubSyncError(f"{name} 不是有效的职位数据库") from error
            finally:
                if "connection" in locals():
                    connection.close()
                    del connection
            if not check or check[0] != "ok" or not has_jobs:
                raise GitHubSyncError(f"{name} 数据库完整性检查失败")
        else:
            try:
                cache = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise GitHubSyncError(f"{name} 不是有效的 JSON 缓存") from error
            if not isinstance(cache, dict):
                raise GitHubSyncError(f"{name} 的 JSON 顶层必须是对象")
    return files


def _atomic_replace(path: Path, content: bytes) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".sync.tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        for attempt in range(5):
            try:
                os.replace(temporary_path, path)
                temporary_path = None
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def restore_job_state(
    archive_bytes: bytes,
    artifact: dict,
    base_dir: Path = BASE_DIR,
) -> dict:
    """Validate, back up, and atomically restore downloaded databases/caches."""
    with tempfile.TemporaryDirectory(prefix="job-monitor-github-sync-") as directory:
        staging = Path(directory)
        files = _validated_state_files(archive_bytes, staging)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_dir = base_dir / "backups" / f"github-sync-{timestamp}"
        previous = {}
        for name in files:
            target = base_dir / name
            if target.exists():
                previous[name] = target.read_bytes()

        if previous:
            backup_dir.mkdir(parents=True, exist_ok=False)
            for name, content in previous.items():
                (backup_dir / name).write_bytes(content)

        applied = []
        try:
            for name, content in files.items():
                _atomic_replace(base_dir / name, content)
                applied.append(name)
        except OSError as error:
            for name in reversed(applied):
                target = base_dir / name
                if name in previous:
                    _atomic_replace(target, previous[name])
                elif target.exists():
                    target.unlink()
            raise GitHubSyncError(f"写入本地职位数据失败，已回滚: {error}") from error

    job_counts = {}
    for name in sorted(files):
        if DB_NAME.fullmatch(name):
            connection = sqlite3.connect(str(base_dir / name))
            try:
                job_counts[name] = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            finally:
                connection.close()
    return {
        "artifact_id": artifact.get("id"),
        "artifact_created_at": artifact.get("created_at"),
        "files": sorted(files),
        "job_counts": job_counts,
        "backup_directory": str(backup_dir) if previous else "",
    }


def download_latest_job_state(client: Optional[GitHubClient] = None) -> dict:
    github_client = client or client_from_settings()
    artifact = github_client.latest_state_artifact()
    archive_bytes = github_client.download_artifact(int(artifact["id"]))
    result = restore_job_state(archive_bytes, artifact)
    result["repository"] = github_client.repository
    return result

