"""Create a private local config from the committed safe template."""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from typing import Union


BASE_DIR = Path(__file__).resolve().parent
CONFIG_TEMPLATE = BASE_DIR / "config.example.yaml"
GENERATED_CONFIG_NAME = re.compile(
    r"^config(?:_user[A-Za-z0-9_-]+)?\.ya?ml$",
    re.IGNORECASE,
)


def resolve_config_path(config_file: Union[str, os.PathLike]) -> Path:
    """Resolve an existing relative path, otherwise place it beside the app."""
    path = Path(config_file)
    if path.is_absolute():
        return path

    current_candidate = (Path.cwd() / path).resolve()
    if current_candidate.exists():
        return current_candidate
    return (BASE_DIR / path).resolve()


def ensure_config_file(config_file: Union[str, os.PathLike] = "config.yaml") -> str:
    """Return a config path, bootstrapping supported missing configs safely."""
    config_path = resolve_config_path(config_file)
    if config_path.exists():
        return str(config_path)

    if not GENERATED_CONFIG_NAME.fullmatch(config_path.name):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    if not CONFIG_TEMPLATE.exists():
        raise FileNotFoundError(f"配置模板不存在: {CONFIG_TEMPLATE}")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    template_bytes = CONFIG_TEMPLATE.read_bytes()
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            dir=config_path.parent,
            delete=False,
        ) as temporary:
            temporary.write(template_bytes)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, config_path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()

    return str(config_path)
