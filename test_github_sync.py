import base64
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from nacl import encoding, public

import app as web_app
import github_sync


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class RecordingSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)


def create_database(path, job_count):
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO jobs (title) VALUES (?)",
            [(f"职位 {index}",) for index in range(job_count)],
        )
        connection.commit()
    finally:
        connection.close()


def create_state_archive(database_path, cache=None):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.write(database_path, "jobs.db")
        archive.writestr(
            "jobs_cache.json",
            json.dumps(cache or {}, ensure_ascii=False),
        )
    return output.getvalue()


class GitHubSyncTests(unittest.TestCase):
    def test_repository_normalization(self):
        self.assertEqual(
            github_sync.normalize_repository("https://github.com/Owner/Repo.git"),
            "Owner/Repo",
        )
        self.assertEqual(
            github_sync.normalize_repository("Owner/Repo"),
            "Owner/Repo",
        )
        with self.assertRaises(ValueError):
            github_sync.normalize_repository("https://example.com/Owner/Repo")

    def test_settings_api_masks_token(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "github_sync.yaml"
            with patch.object(github_sync, "SETTINGS_FILE", settings_file):
                with patch.dict(
                    github_sync.os.environ,
                    {github_sync.TOKEN_ENV_NAME: ""},
                    clear=False,
                ):
                    saved = github_sync.save_settings(
                        "owner/repository",
                        "github_pat_private",
                    )
                    self.assertEqual(saved["token"], github_sync.TOKEN_MASK)
                    self.assertNotIn(
                        "github_pat_private",
                        json.dumps(saved, ensure_ascii=False),
                    )
                    preserved = github_sync.save_settings(
                        "owner/renamed",
                        github_sync.TOKEN_MASK,
                    )
                    self.assertEqual(preserved["repository"], "owner/renamed")
                    raw = settings_file.read_text(encoding="utf-8")
                    self.assertIn("github_pat_private", raw)

    def test_repository_secret_is_encrypted_before_upload(self):
        private_key = public.PrivateKey.generate()
        encoded_public_key = private_key.public_key.encode(
            encoder=encoding.Base64Encoder
        ).decode("ascii")
        uploaded = {}

        def handler(method, url, kwargs):
            if method == "GET":
                return FakeResponse(
                    200,
                    {"key_id": "test-key", "key": encoded_public_key},
                )
            uploaded.update(kwargs["json"])
            return FakeResponse(204, {})

        session = RecordingSession(handler)
        client = github_sync.GitHubClient(
            "owner/repository",
            "github_pat_private",
            session=session,
        )
        result = client.update_repository_secret("EXAMPLE", "private config")

        self.assertEqual(result, "updated")
        self.assertNotEqual(uploaded["encrypted_value"], "private config")
        encrypted = base64.b64decode(uploaded["encrypted_value"])
        decrypted = public.SealedBox(private_key).decrypt(encrypted).decode("utf-8")
        self.assertEqual(decrypted, "private config")
        for _, _, kwargs in session.calls:
            self.assertNotIn("github_pat_private", json.dumps(kwargs.get("json", {})))

    def test_restore_backs_up_and_replaces_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current_database = root / "jobs.db"
            new_database = root / "new.db"
            create_database(current_database, 1)
            create_database(new_database, 3)
            archive = create_state_archive(new_database, {"known": True})

            result = github_sync.restore_job_state(
                archive,
                {"id": 123, "created_at": "2026-07-26T00:00:00Z"},
                base_dir=root,
            )

            self.assertEqual(result["job_counts"]["jobs.db"], 3)
            backup = Path(result["backup_directory"]) / "jobs.db"
            self.assertTrue(backup.exists())
            connection = sqlite3.connect(str(backup))
            try:
                old_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(old_count, 1)
            self.assertEqual(
                json.loads((root / "jobs_cache.json").read_text(encoding="utf-8")),
                {"known": True},
            )

    def test_restore_rejects_unsafe_archive_paths(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../jobs.db", b"unsafe")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(github_sync.GitHubSyncError, "不安全"):
                github_sync.restore_job_state(
                    output.getvalue(),
                    {"id": 1},
                    base_dir=Path(directory),
                )

    def test_web_routes_call_sync_services(self):
        client = web_app.app.test_client()
        with patch.object(
            web_app,
            "upload_local_configuration",
            return_value={
                "repository": "owner/repository",
                "secret_name": "JOB_MONITOR_CONFIG_B64",
                "result": "updated",
            },
        ):
            response = client.post("/api/github-sync/upload-config")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

        with patch.object(
            web_app,
            "download_latest_job_state",
            return_value={
                "repository": "owner/repository",
                "job_counts": {"jobs.db": 4},
                "backup_directory": "backup",
            },
        ):
            web_app.is_monitoring = False
            response = client.post("/api/github-sync/download-jobs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["job_counts"]["jobs.db"], 4)


if __name__ == "__main__":
    unittest.main()

