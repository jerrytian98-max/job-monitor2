"""Shared names for each isolated job-monitor profile.

The first local user keeps the historical default filenames so existing data is
not moved.  The second user uses the ``user2`` profile suffix.  Cloud state is
given an explicit slot for the default user so both GitHub Actions workflows
can run at the same time without sharing a Secret, cache key, or artifact.
"""

from __future__ import annotations

import re


PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_CLOUD_SLOT = "user1"


def normalize_profile(profile: object = "") -> str:
    """Return a safe local profile name, with ``""`` for the first user."""
    value = str(profile or "").strip()
    if not value:
        return ""
    if not PROFILE_NAME.fullmatch(value):
        raise ValueError("用户配置名称只能包含字母、数字、下划线和连字符")
    return value


def profile_suffix(profile: object = "") -> str:
    value = normalize_profile(profile)
    return f"_{value}" if value else ""


def config_filename(profile: object = "") -> str:
    """Return the private configuration filename for a local profile."""
    value = normalize_profile(profile)
    return f"config_{value}.yaml" if value else "config.yaml"


def profile_from_config_filename(config_name: str) -> str:
    """Map config.yaml/config_user2.yaml to its JOB_PROFILE value."""
    name = str(config_name or "").strip()
    if name == "config.yaml":
        return ""
    match = re.fullmatch(r"config_([A-Za-z0-9_-]+)\.yaml", name)
    if not match:
        raise ValueError(f"不支持的配置文件名: {name}")
    return normalize_profile(match.group(1))


def state_filenames(profile: object = "") -> tuple[str, str]:
    suffix = profile_suffix(profile)
    return f"jobs{suffix}.db", f"jobs_cache{suffix}.json"


def cloud_slot(profile: object = "") -> str:
    """Return the independent GitHub Actions slot for a local profile."""
    value = normalize_profile(profile)
    # ``config.yaml`` is the existing first-user configuration.  Give it a
    # stable explicit cloud name without renaming its local data files.
    return value or DEFAULT_CLOUD_SLOT


def cloud_config_secret_name(profile: object = "") -> str:
    return f"JOB_MONITOR_{cloud_slot(profile).upper()}_CONFIG_B64"


def cloud_artifact_name(profile: object = "") -> str:
    return f"job-monitor-state-{cloud_slot(profile)}"


def cloud_cache_key_prefix(profile: object = "") -> str:
    return f"job-monitor-state-{cloud_slot(profile)}"


def profile_display_name(profile: object = "") -> str:
    value = normalize_profile(profile)
    if not value:
        return "用户一"
    if value == "user2":
        return "用户二"
    return f"用户 {value}"
