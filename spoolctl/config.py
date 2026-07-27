"""Project-local read-only configuration discovery and validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from spoolctl.errors import CliError
from spoolctl.models import DEFAULT_DB_RELPATH, MAX_PATH_CHARS

CONFIG_RELPATH = os.path.join(".spoolctl", "config.json")
CONFIG_FORMAT = "json"
CONFIG_SCHEMA_VERSION = 1
CONFIG_DB_PATH_KEY = "db_path"
CONFIG_VERSION_KEY = "version"
SUPPORTED_KEYS = (CONFIG_VERSION_KEY, CONFIG_DB_PATH_KEY)
RECOGNIZED_KEYS = (CONFIG_DB_PATH_KEY,)
PRECEDENCE = ("flag", "environment", "project_config", "default")


@dataclass(frozen=True)
class EffectiveConfig:
    config_path: str | None
    config_exists: bool
    config_valid: bool
    db_path: str
    db_source: str
    ignored_keys: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "config_exists": self.config_exists,
            "config_valid": self.config_valid,
            "values": {"db_path": self.db_path},
            "sources": {"db_path": self.db_source},
            "precedence": list(PRECEDENCE),
            "ignored_keys": self.ignored_keys,
        }


@dataclass(frozen=True)
class ConfigValidation:
    config_path: str
    exists: bool
    valid: bool
    format: str
    schema_version: int
    recognized_keys: list[str]
    unknown_keys: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "exists": self.exists,
            "valid": self.valid,
            "format": self.format,
            "schema_version": self.schema_version,
            "recognized_keys": self.recognized_keys,
            "unknown_keys": self.unknown_keys,
        }


def default_config_path(base_dir: str) -> str:
    return os.path.realpath(os.path.join(base_dir, CONFIG_RELPATH))


def _validate_db_path_value(raw: Any, *, source: str) -> str:
    if not isinstance(raw, str):
        raise CliError(
            "INVALID_INPUT",
            f"{source} db_path must be a string",
            "set db_path to a non-empty path string",
        )
    if raw == "":
        raise CliError(
            "INVALID_INPUT",
            f"{source} db_path must not be empty",
            "set db_path to a non-empty path string",
        )
    if "\x00" in raw:
        raise CliError(
            "INVALID_INPUT",
            f"{source} db_path must not contain NUL bytes",
            "remove embedded NUL bytes from the database path",
        )
    if len(raw) > MAX_PATH_CHARS:
        raise CliError(
            "INVALID_INPUT",
            f"{source} db_path must be <= {MAX_PATH_CHARS} characters (got {len(raw)})",
            "use a shorter database path",
        )
    return raw


def _load_config_object(path: str) -> tuple[dict[str, Any] | None, bool]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None, False
    except json.JSONDecodeError as exc:
        raise CliError(
            "INVALID_INPUT",
            f"malformed JSON config at {path!r}: {exc.msg}",
            "fix .spoolctl/config.json or remove it",
        ) from None
    except OSError as exc:
        raise CliError(
            "INVALID_INPUT",
            f"cannot read config at {path!r}: {exc.strerror or exc}",
            "fix permissions or remove the config file",
        ) from None
    if not isinstance(raw, dict):
        raise CliError(
            "INVALID_INPUT",
            f"config at {path!r} must contain a JSON object",
            "use an object such as {\"version\": 1, \"db_path\": \"queue.db\"}",
        )
    version = raw.get(CONFIG_VERSION_KEY, CONFIG_SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version != CONFIG_SCHEMA_VERSION:
        raise CliError(
            "INVALID_INPUT",
            f"config version must be integer {CONFIG_SCHEMA_VERSION}",
            f"set version to {CONFIG_SCHEMA_VERSION} or omit it",
        )
    if CONFIG_DB_PATH_KEY in raw:
        _validate_db_path_value(raw[CONFIG_DB_PATH_KEY], source="config")
    return raw, True


def validate_config_file(path: str) -> ConfigValidation:
    config_path = os.path.realpath(path)
    data, exists = _load_config_object(config_path)
    unknown = sorted(set(data or {}) - set(SUPPORTED_KEYS))
    if unknown:
        raise CliError(
            "INVALID_INPUT",
            "unknown config key(s): " + ", ".join(unknown),
            "remove unsupported keys; supported keys are version and db_path",
        )
    return ConfigValidation(
        config_path=config_path,
        exists=exists,
        valid=True,
        format=CONFIG_FORMAT,
        schema_version=CONFIG_SCHEMA_VERSION,
        recognized_keys=list(RECOGNIZED_KEYS),
        unknown_keys=[],
    )


def resolve_effective_config(
    db_path_flag: str | None = None,
    *,
    base_dir: str | None = None,
    strict_unknown_keys: bool = False,
) -> EffectiveConfig:
    if db_path_flag is not None:
        raw = _validate_db_path_value(db_path_flag, source="--db")
        return EffectiveConfig(
            config_path=default_config_path(base_dir) if base_dir is not None else None,
            config_exists=False,
            config_valid=True,
            db_path=os.path.realpath(raw),
            db_source="flag",
            ignored_keys=[],
        )

    env_path = os.environ.get("SPOOLCTL_DB")
    if env_path is not None:
        raw = _validate_db_path_value(env_path, source="SPOOLCTL_DB")
        return EffectiveConfig(
            config_path=default_config_path(base_dir) if base_dir is not None else None,
            config_exists=False,
            config_valid=True,
            db_path=os.path.realpath(raw),
            db_source="environment",
            ignored_keys=[],
        )

    config_path = default_config_path(base_dir) if base_dir is not None else None
    config_obj: dict[str, Any] | None = None
    config_exists = False
    if config_path is not None:
        config_obj, config_exists = _load_config_object(config_path)
        unknown = sorted(set(config_obj or {}) - set(SUPPORTED_KEYS))
        if unknown and strict_unknown_keys:
            raise CliError(
                "INVALID_INPUT",
                "unknown config key(s): " + ", ".join(unknown),
                "remove unsupported keys; supported keys are version and db_path",
            )
        if config_obj and CONFIG_DB_PATH_KEY in config_obj:
            raw = _validate_db_path_value(config_obj[CONFIG_DB_PATH_KEY], source="config")
            path = raw if os.path.isabs(raw) else os.path.join(os.path.dirname(config_path), raw)
            return EffectiveConfig(
                config_path=config_path,
                config_exists=config_exists,
                config_valid=True,
                db_path=os.path.realpath(path),
                db_source="project_config",
                ignored_keys=unknown,
            )
    else:
        unknown = []

    default_base = os.getcwd() if base_dir is None else base_dir
    return EffectiveConfig(
        config_path=config_path,
        config_exists=config_exists,
        config_valid=True,
        db_path=os.path.realpath(os.path.join(default_base, DEFAULT_DB_RELPATH)),
        db_source="default",
        ignored_keys=unknown,
    )
