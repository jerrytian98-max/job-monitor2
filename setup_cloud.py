"""Backward-compatible helper for creating the GitHub configuration Secret."""

from __future__ import annotations

import argparse
from pathlib import Path

from github_config import DEFAULT_EXPORT_FILE, export_secret


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成供 GitHub Actions 使用的私有配置 Secret"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_EXPORT_FILE,
        help="输出文件（默认: .job-monitor-config.b64）",
    )
    parser.add_argument(
        "--include-sensitive-values",
        action="store_true",
        help="同时打包邮箱授权码和 Gemini API Key",
    )
    args = parser.parse_args()

    output = args.output.resolve()
    try:
        export_secret(output, args.include_sensitive_values)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    print(f"已生成: {output}")
    print("请将文件内容保存为 GitHub Actions Secret: JOB_MONITOR_CONFIG_B64")
    if not args.include_sensitive_values:
        print("邮箱授权码和 Gemini API Key 已移除，请分别设置对应的 Secrets。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

