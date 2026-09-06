"""Export and import portable recruitment-site configuration only.

The transfer file intentionally excludes email, GitHub settings, credentials,
AI keys, runtime databases, caches, logs, and scraped job records.  It is
safe to give to another Job Monitor installation when the listed recruitment
URLs and labels may be shared.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import yaml

from config_bootstrap import BASE_DIR, ensure_config_file
from profile_config import config_filename, profile_from_config_filename
from site_labels import build_site_labels, build_site_modes


FORMAT_NAME = "job-monitor-site-config"
FORMAT_VERSION = 1
DEFAULT_OUTPUT = BASE_DIR / "job-monitor-site-config.yaml"


def _load_mapping(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"读取配置文件失败: {path.name}（{error}）") from error
    if not isinstance(value, dict):
        raise ValueError(f"配置文件格式无效: {path.name}")
    return value


def _atomic_write_yaml(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"导入文件包含无效招聘网址: {url[:100]}")
    return url


def _unique_strings(values: object, field_name: str) -> list[str]:
    if not isinstance(values, list):
        raise ValueError(f"{field_name} 必须是列表")
    result = []
    for raw in values:
        value = str(raw or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def _portable_profile(config: dict) -> dict:
    """Return only values required to recreate the site rows elsewhere."""
    sites = []
    for raw_url in config.get("job_sites", []):
        url = _validate_url(raw_url)
        if url not in sites:
            sites.append(url)
    return {
        "job_sites": sites,
        "job_site_labels": build_site_labels(
            sites,
            config.get("job_site_labels"),
        ),
        "job_site_modes": build_site_modes(
            sites,
            config.get("job_site_modes"),
        ),
        # Keywords are useful for sites in search mode, but importing them is
        # opt-in so an existing user's search preferences are not overwritten.
        "suggested_job_keywords": _unique_strings(
            config.get("job_keywords", []),
            "job_keywords",
        ),
    }


def _config_paths() -> list[Path]:
    ensure_config_file("config.yaml")
    paths = [BASE_DIR / "config.yaml"]
    paths.extend(sorted(BASE_DIR.glob("config_user*.yaml")))
    return [path for path in paths if path.is_file()]


def export_site_config(output: Path = DEFAULT_OUTPUT) -> dict:
    """Write one sanitized portable file for every local user profile."""
    profiles = {}
    for path in _config_paths():
        profile = profile_from_config_filename(path.name) or "user1"
        if profile in profiles:
            raise ValueError(f"导出的用户名称重复: {profile}")
        profiles[profile] = _portable_profile(_load_mapping(path))

    payload = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profiles": profiles,
    }
    output = Path(output).resolve()
    _atomic_write_yaml(output, payload)
    return {
        "output": str(output),
        "profiles": {name: len(data["job_sites"]) for name, data in profiles.items()},
    }


def _load_portable_profile(input_path: Path, source_profile: str) -> dict:
    payload = _load_mapping(input_path)
    if payload.get("format") != FORMAT_NAME or payload.get("version") != FORMAT_VERSION:
        raise ValueError("不是受支持的招聘网站配置包")
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("配置包缺少 profiles")
    if source_profile not in profiles:
        available = "、".join(sorted(str(name) for name in profiles)) or "无"
        raise ValueError(f"配置包中没有用户 {source_profile}（可用: {available}）")
    profile = profiles[source_profile]
    if not isinstance(profile, dict):
        raise ValueError(f"用户 {source_profile} 的配置格式无效")

    sites = []
    for raw_url in profile.get("job_sites", []):
        url = _validate_url(raw_url)
        if url not in sites:
            sites.append(url)
    labels = build_site_labels(sites, profile.get("job_site_labels"))
    modes = build_site_modes(sites, profile.get("job_site_modes"))
    keywords = _unique_strings(
        profile.get("suggested_job_keywords", []),
        "suggested_job_keywords",
    )
    return {
        "job_sites": sites,
        "job_site_labels": labels,
        "job_site_modes": modes,
        "suggested_job_keywords": keywords,
    }


def _site_fields(config: dict) -> tuple[list[str], dict[str, str], dict[str, str]]:
    sites = []
    for raw_url in config.get("job_sites", []):
        url = _validate_url(raw_url)
        if url not in sites:
            sites.append(url)
    return (
        sites,
        build_site_labels(sites, config.get("job_site_labels")),
        build_site_modes(sites, config.get("job_site_modes")),
    )


def import_site_config(
    input_path: Path,
    source_profile: str,
    target_profile: str = "",
    *,
    replace: bool = False,
    include_keywords: bool = False,
) -> dict:
    """Import site rows into one local user while preserving private fields."""
    imported = _load_portable_profile(Path(input_path), source_profile)
    target_path = Path(ensure_config_file(BASE_DIR / config_filename(target_profile)))
    target = _load_mapping(target_path)
    current_sites, current_labels, current_modes = _site_fields(target)

    if replace:
        sites = imported["job_sites"]
        labels = imported["job_site_labels"]
        modes = imported["job_site_modes"]
    else:
        sites = list(current_sites)
        labels = dict(current_labels)
        modes = dict(current_modes)
        for url in imported["job_sites"]:
            if url in sites:
                continue
            sites.append(url)
            labels[url] = imported["job_site_labels"][url]
            modes[url] = imported["job_site_modes"][url]

    target["job_sites"] = sites
    target["job_site_labels"] = build_site_labels(sites, labels)
    target["job_site_modes"] = build_site_modes(sites, modes)
    if include_keywords:
        current_keywords = _unique_strings(target.get("job_keywords", []), "job_keywords")
        target["job_keywords"] = (
            imported["suggested_job_keywords"]
            if replace
            else current_keywords
            + [
                keyword
                for keyword in imported["suggested_job_keywords"]
                if keyword not in current_keywords
            ]
        )
    _atomic_write_yaml(target_path, target)
    return {
        "target": str(target_path),
        "site_count": len(sites),
        "imported_site_count": len(imported["job_sites"]),
        "keywords_imported": include_keywords,
        "replaced": replace,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导入或导出不含隐私数据的招聘网站配置")
    commands = parser.add_subparsers(dest="command", required=True)

    export_parser = commands.add_parser("export", help="导出所有本地用户的招聘网站配置")
    export_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    import_parser = commands.add_parser("import", help="导入一个用户的招聘网站配置")
    import_parser.add_argument("--input", type=Path, required=True)
    import_parser.add_argument("--source-profile", default="user1")
    import_parser.add_argument("--target-profile", default="")
    import_parser.add_argument("--replace", action="store_true", help="替换目标用户现有网址")
    import_parser.add_argument(
        "--include-keywords",
        action="store_true",
        help="同时导入配置包附带的推荐搜索关键词",
    )
    args = parser.parse_args()
    try:
        if args.command == "export":
            result = export_site_config(args.output)
            summary = "，".join(
                f"{name} {count} 个网址" for name, count in result["profiles"].items()
            )
            print(f"已导出招聘网站配置: {result['output']}（{summary}）")
        else:
            result = import_site_config(
                args.input,
                args.source_profile,
                args.target_profile,
                replace=args.replace,
                include_keywords=args.include_keywords,
            )
            print(
                f"已导入 {result['imported_site_count']} 个网址，"
                f"目标现有 {result['site_count']} 个网址: {result['target']}"
            )
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
