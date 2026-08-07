"""Export and restore private configuration for GitHub Actions."""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import os
from pathlib import Path
import re
import tempfile
from typing import Optional

import yaml

from config_bootstrap import BASE_DIR, ensure_config_file


SECRET_ENV_NAME = "JOB_MONITOR_CONFIG_B64"
DEFAULT_EXPORT_FILE = BASE_DIR / ".job-monitor-config.b64"
GITHUB_SECRET_SIZE_LIMIT = 48 * 1024
CONFIG_NAME = re.compile(
    r"^config(?:_user[A-Za-z0-9_-]+)?\.yaml$",
    re.IGNORECASE,
)
REQUIRED_FIELDS = {
    "job_sites",
    "job_keywords",
    "cities",
    "exclude_keywords",
    "check_interval",
    "email",
}


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def _validate_config(config_name: str, config: object) -> dict:
    if not CONFIG_NAME.fullmatch(config_name):
        raise ValueError(f"不安全或不支持的配置文件名: {config_name}")
    if not isinstance(config, dict):
        raise ValueError(f"{config_name} 的顶层必须是对象")
    missing = sorted(REQUIRED_FIELDS.difference(config))
    if missing:
        raise ValueError(f"{config_name} 缺少字段: {', '.join(missing)}")
    return config


def _config_paths() -> list[Path]:
    ensure_config_file("config.yaml")
    paths = [BASE_DIR / "config.yaml"]
    paths.extend(sorted(BASE_DIR.glob("config_user*.yaml")))
    return list(dict.fromkeys(path.resolve() for path in paths if path.is_file()))


def _without_api_credentials(config: dict) -> dict:
    clean = copy.deepcopy(config)
    clean["gemini_api_key"] = ""
    email = clean.get("email")
    if isinstance(email, dict):
        email["auth_code"] = ""
    return clean


def build_secret_value(include_sensitive_values: bool = False) -> str:
    """Build the base64 value stored in JOB_MONITOR_CONFIG_B64."""
    configs = {}
    for path in _config_paths():
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = _validate_config(path.name, config)
        if not include_sensitive_values:
            config = _without_api_credentials(config)
        configs[path.name] = config

    payload = {
        "version": 1,
        "configs": configs,
    }
    yaml_bytes = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    encoded = base64.b64encode(yaml_bytes)
    if len(encoded) > GITHUB_SECRET_SIZE_LIMIT:
        raise ValueError(
            f"生成内容为 {len(encoded)} 字节，超过 GitHub Secret 的 48 KB 限制"
        )
    return encoded.decode("ascii")


def export_secret(output: Path, include_sensitive_values: bool = False) -> Path:
    """Bundle all local profiles into one base64 GitHub Secret value."""
    encoded = build_secret_value(include_sensitive_values)
    _atomic_write(output, encoded.encode("ascii") + b"\n")
    return output


def _decode_secret(encoded: str) -> dict[str, dict]:
    try:
        decoded = base64.b64decode("".join(encoded.split()), validate=True)
        payload = yaml.safe_load(decoded.decode("utf-8")) or {}
    except (binascii.Error, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError(f"{SECRET_ENV_NAME} 不是有效的 Base64 YAML: {error}") from error

    # Accept both the documented multi-profile bundle and a single raw config.
    if isinstance(payload, dict) and isinstance(payload.get("configs"), dict):
        raw_configs = payload["configs"]
    elif isinstance(payload, dict):
        raw_configs = {"config.yaml": payload}
    else:
        raise ValueError(f"{SECRET_ENV_NAME} 解码后的内容必须是 YAML 对象")

    configs = {}
    for config_name, config in raw_configs.items():
        configs[config_name] = _validate_config(config_name, config)
    if not configs:
        raise ValueError(f"{SECRET_ENV_NAME} 中没有配置")
    return configs


def restore_secret(encoded: Optional[str] = None) -> list[Path]:
    """Restore configs inside the ephemeral Actions runner."""
    secret_value = encoded if encoded is not None else os.environ.get(SECRET_ENV_NAME, "")
    if not secret_value.strip():
        raise ValueError(
            f"缺少 GitHub Actions Secret: {SECRET_ENV_NAME}。"
            "请先按照 README 的“GitHub 自动运行”章节设置。"
        )

    restored = []
    for config_name, config in _decode_secret(secret_value).items():
        target = BASE_DIR / config_name
        yaml_bytes = yaml.safe_dump(
            config,
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        _atomic_write(target, yaml_bytes)
        restored.append(target)
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description="管理 GitHub Actions 私有配置")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser(
        "export",
        help="把本地 config.yaml/config_user*.yaml 打包成 GitHub Secret",
    )
    export_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_EXPORT_FILE,
        help="输出文件（默认: .job-monitor-config.b64）",
    )
    export_parser.add_argument(
        "--include-sensitive-values",
        action="store_true",
        help="把各配置中的邮箱授权码和 API Key 也放进 Secret",
    )
    subparsers.add_parser(
        "restore",
        help=f"从环境变量 {SECRET_ENV_NAME} 恢复配置（供 Actions 使用）",
    )

    args = parser.parse_args()
    try:
        if args.command == "export":
            output = args.output
            if not output.is_absolute():
                output = (BASE_DIR / output).resolve()
            export_secret(output, args.include_sensitive_values)
            print(f"已生成: {output}")
            print(f"请把文件内容保存为 GitHub Actions Secret: {SECRET_ENV_NAME}")
            if not args.include_sensitive_values:
                print("邮箱授权码和 Gemini API Key 已从该文件中移除。")
        else:
            restored = restore_secret()
            print("已恢复配置: " + ", ".join(path.name for path in restored))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
