from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

import site_config_transfer


def config_with_private_values(sites, keywords):
    return {
        "job_sites": sites,
        "job_site_labels": {url: f"标签{index}" for index, url in enumerate(sites, 1)},
        "job_site_modes": {url: "search" for url in sites},
        "job_keywords": keywords,
        "cities": ["北京"],
        "exclude_keywords": ["实习"],
        "check_interval": 4,
        "email": {
            "sender": "private@example.com",
            "receiver": "receiver@example.com",
            "auth_code": "private-auth-code",
            "smtp_server": "smtp.example.com",
            "smtp_port": 465,
        },
        "gemini_api_key": "private-api-key",
        "ai_filter_prompt": "private filter",
    }


class SiteConfigTransferTests(unittest.TestCase):
    def test_export_excludes_email_credentials_and_non_site_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user1_url = "https://jobs.example.com/user1"
            user2_url = "https://jobs.example.com/user2"
            (root / "config.yaml").write_text(
                yaml.safe_dump(config_with_private_values([user1_url], ["法务"]), allow_unicode=True),
                encoding="utf-8",
            )
            (root / "config_user2.yaml").write_text(
                yaml.safe_dump(config_with_private_values([user2_url], ["战略"]), allow_unicode=True),
                encoding="utf-8",
            )
            output = root / "job-monitor-site-config.yaml"
            with patch.object(site_config_transfer, "BASE_DIR", root):
                result = site_config_transfer.export_site_config(output)

            payload = yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertEqual(result["profiles"], {"user1": 1, "user2": 1})
            self.assertEqual(set(payload["profiles"]), {"user1", "user2"})
            self.assertEqual(
                set(payload["profiles"]["user1"]),
                {"job_sites", "job_site_labels", "job_site_modes", "suggested_job_keywords"},
            )
            serialized = output.read_text(encoding="utf-8")
            for private_value in (
                "private@example.com",
                "receiver@example.com",
                "private-auth-code",
                "private-api-key",
                "private filter",
                "check_interval",
                "cities",
            ):
                self.assertNotIn(private_value, serialized)

    def test_import_merges_sites_and_preserves_private_target_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_url = "https://jobs.example.com/source"
            existing_url = "https://jobs.example.com/existing"
            package = {
                "format": site_config_transfer.FORMAT_NAME,
                "version": site_config_transfer.FORMAT_VERSION,
                "profiles": {
                    "user1": {
                        "job_sites": [source_url],
                        "job_site_labels": {source_url: "来源网站"},
                        "job_site_modes": {source_url: "fixed"},
                        "suggested_job_keywords": ["法务"],
                    }
                },
            }
            input_path = root / "import.yaml"
            input_path.write_text(yaml.safe_dump(package, allow_unicode=True), encoding="utf-8")
            target_config = config_with_private_values([existing_url], ["战略"])
            target_path = root / "config_user2.yaml"
            target_path.write_text(yaml.safe_dump(target_config, allow_unicode=True), encoding="utf-8")

            with patch.object(site_config_transfer, "BASE_DIR", root):
                result = site_config_transfer.import_site_config(
                    input_path,
                    "user1",
                    "user2",
                    include_keywords=True,
                )

            saved = yaml.safe_load(target_path.read_text(encoding="utf-8"))
            self.assertEqual(result["site_count"], 2)
            self.assertEqual(saved["job_sites"], [existing_url, source_url])
            self.assertEqual(saved["job_site_labels"][source_url], "来源网站")
            self.assertEqual(saved["job_site_modes"][source_url], "fixed")
            self.assertEqual(saved["job_keywords"], ["战略", "法务"])
            self.assertEqual(saved["email"], target_config["email"])
            self.assertEqual(saved["gemini_api_key"], "private-api-key")

    def test_import_rejects_invalid_recruitment_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = {
                "format": site_config_transfer.FORMAT_NAME,
                "version": site_config_transfer.FORMAT_VERSION,
                "profiles": {"user1": {"job_sites": ["not-a-url"]}},
            }
            input_path = root / "invalid.yaml"
            input_path.write_text(yaml.safe_dump(package), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "无效招聘网址"):
                site_config_transfer.import_site_config(input_path, "user1")


if __name__ == "__main__":
    unittest.main()
