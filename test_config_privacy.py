import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

import config_bootstrap
import github_config
from main import JobMonitor


def sample_config(secret_value=""):
    return {
        "job_sites": ["https://jobs.example.com/search?keywords=test"],
        "job_site_labels": {
            "https://jobs.example.com/search?keywords=test": "测试"
        },
        "job_keywords": ["法务"],
        "cities": [],
        "exclude_keywords": [],
        "check_interval": 4,
        "email": {
            "sender": "sender@example.com",
            "auth_code": secret_value,
            "receiver": "receiver@example.com",
            "smtp_server": "smtp.example.com",
            "smtp_port": 465,
        },
        "gemini_api_key": secret_value,
        "gemini_model": "gemini-3.5-flash-lite",
        "ai_filter_prompt": "",
    }


class ConfigPrivacyTests(unittest.TestCase):
    def test_missing_local_config_is_created_from_safe_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "config.example.yaml"
            template.write_text(
                yaml.safe_dump(sample_config(""), allow_unicode=True),
                encoding="utf-8",
            )
            target = root / "config.yaml"

            with patch.object(config_bootstrap, "CONFIG_TEMPLATE", template):
                result = config_bootstrap.ensure_config_file(target)

            self.assertEqual(Path(result), target)
            self.assertEqual(
                yaml.safe_load(target.read_text(encoding="utf-8")),
                sample_config(""),
            )

    def test_export_strips_credentials_and_restore_keeps_all_profiles(self):
        with tempfile.TemporaryDirectory() as source_directory:
            source = Path(source_directory)
            (source / "config.yaml").write_text(
                yaml.safe_dump(sample_config("default-secret"), allow_unicode=True),
                encoding="utf-8",
            )
            (source / "config_user2.yaml").write_text(
                yaml.safe_dump(sample_config("user2-secret"), allow_unicode=True),
                encoding="utf-8",
            )
            output = source / ".job-monitor-config.b64"

            with patch.object(github_config, "BASE_DIR", source):
                with patch.object(github_config, "ensure_config_file"):
                    github_config.export_secret(output)

            encoded = output.read_text(encoding="utf-8")
            payload = yaml.safe_load(base64.b64decode(encoded).decode("utf-8"))
            self.assertEqual(
                set(payload["configs"]),
                {"config.yaml", "config_user2.yaml"},
            )
            for config in payload["configs"].values():
                self.assertEqual(config["email"]["auth_code"], "")
                self.assertEqual(config["gemini_api_key"], "")

            with tempfile.TemporaryDirectory() as target_directory:
                target = Path(target_directory)
                with patch.object(github_config, "BASE_DIR", target):
                    restored = github_config.restore_secret(encoded)

                self.assertEqual(
                    {path.name for path in restored},
                    {"config.yaml", "config_user2.yaml"},
                )
                self.assertTrue((target / "config.yaml").exists())
                self.assertTrue((target / "config_user2.yaml").exists())

    def test_restore_rejects_path_traversal(self):
        payload = {
            "version": 1,
            "configs": {
                "../config.yaml": sample_config(""),
            },
        }
        encoded = base64.b64encode(
            yaml.safe_dump(payload, allow_unicode=True).encode("utf-8")
        ).decode("ascii")

        with self.assertRaisesRegex(ValueError, "不安全"):
            github_config.restore_secret(encoded)

    def test_profile_credentials_win_and_environment_fills_blanks(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(sample_config("profile-secret"), allow_unicode=True),
                encoding="utf-8",
            )
            monitor = JobMonitor.__new__(JobMonitor)
            with patch.dict(
                "os.environ",
                {
                    "JOB_EMAIL_AUTH_CODE": "environment-email",
                    "GEMINI_API_KEY": "environment-gemini",
                },
            ):
                loaded = monitor._load_config(str(config_path))
            self.assertEqual(loaded["email"]["auth_code"], "profile-secret")
            self.assertEqual(loaded["gemini_api_key"], "profile-secret")

            blank = sample_config("")
            config_path.write_text(
                yaml.safe_dump(blank, allow_unicode=True),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "JOB_EMAIL_AUTH_CODE": "environment-email",
                    "GEMINI_API_KEY": "environment-gemini",
                },
            ):
                loaded = monitor._load_config(str(config_path))
            self.assertEqual(loaded["email"]["auth_code"], "environment-email")
            self.assertEqual(loaded["gemini_api_key"], "environment-gemini")


if __name__ == "__main__":
    unittest.main()
