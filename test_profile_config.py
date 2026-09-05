import unittest

from profile_config import (
    cloud_artifact_name,
    cloud_cache_key_prefix,
    cloud_config_secret_name,
    config_filename,
    profile_from_config_filename,
    state_filenames,
)


class ProfileConfigTests(unittest.TestCase):
    def test_default_and_user2_keep_separate_local_and_cloud_names(self):
        self.assertEqual(config_filename(""), "config.yaml")
        self.assertEqual(config_filename("user2"), "config_user2.yaml")
        self.assertEqual(state_filenames(""), ("jobs.db", "jobs_cache.json"))
        self.assertEqual(
            state_filenames("user2"),
            ("jobs_user2.db", "jobs_cache_user2.json"),
        )
        self.assertEqual(
            cloud_config_secret_name(""),
            "JOB_MONITOR_USER1_CONFIG_B64",
        )
        self.assertEqual(
            cloud_config_secret_name("user2"),
            "JOB_MONITOR_USER2_CONFIG_B64",
        )
        self.assertEqual(cloud_artifact_name(""), "job-monitor-state-user1")
        self.assertEqual(cloud_artifact_name("user2"), "job-monitor-state-user2")
        self.assertNotEqual(cloud_cache_key_prefix(""), cloud_cache_key_prefix("user2"))

    def test_config_name_maps_back_to_the_expected_local_profile(self):
        self.assertEqual(profile_from_config_filename("config.yaml"), "")
        self.assertEqual(profile_from_config_filename("config_user2.yaml"), "user2")
        with self.assertRaises(ValueError):
            profile_from_config_filename("../config.yaml")


if __name__ == "__main__":
    unittest.main()
