"""Configuration loading and validation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when public configuration is incomplete or unsafe."""


def _require(mapping: dict[str, Any], key: str, expected: type, path: str) -> Any:
    value = mapping.get(key)
    if not isinstance(value, expected):
        raise ConfigError(f"{path}.{key} must be {expected.__name__}")
    return value


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read configuration: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in configuration: {config_path}") from exc

    if not isinstance(config, dict):
        raise ConfigError("configuration root must be an object")

    telegram = _require(config, "telegram", dict, "config")
    for key in ("token_file", "allowed_chat_id_file", "state_dir"):
        value = _require(telegram, key, str, "config.telegram")
        if not value.startswith("/"):
            raise ConfigError(f"config.telegram.{key} must be an absolute path")
    pairing_file = telegram.get("pairing_secret_file")
    if pairing_file is not None and (not isinstance(pairing_file, str) or not pairing_file.startswith("/")):
        raise ConfigError("config.telegram.pairing_secret_file must be an absolute path")

    network_checks = config.get("network_checks", [])
    if not isinstance(network_checks, list):
        raise ConfigError("config.network_checks must be an array")
    for index, check in enumerate(network_checks):
        if not isinstance(check, dict):
            raise ConfigError(f"config.network_checks[{index}] must be an object")
        _require(check, "name", str, f"config.network_checks[{index}]")
        _require(check, "host", str, f"config.network_checks[{index}]")
        kind = check.get("type", "icmp")
        if kind not in {"icmp", "tcp", "dns"}:
            raise ConfigError(f"config.network_checks[{index}].type is unsupported")
        if kind == "tcp" and not isinstance(check.get("port"), int):
            raise ConfigError(f"config.network_checks[{index}].port must be int")

    router = config.get("routeros")
    if router is not None:
        if not isinstance(router, dict):
            raise ConfigError("config.routeros must be an object")
        if router.get("enabled"):
            for key in ("host", "user", "identity_file", "known_hosts_file"):
                _require(router, key, str, "config.routeros")
            for key in ("identity_file", "known_hosts_file"):
                if not router[key].startswith("/"):
                    raise ConfigError(f"config.routeros.{key} must be an absolute path")
            interface = router.get("wireguard_interface", "wg1")
            if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", interface):
                raise ConfigError("config.routeros.wireguard_interface contains unsafe characters")

    hosts = _require(config, "hosts", list, "config")
    if not hosts:
        raise ConfigError("config.hosts must contain at least one host")
    for index, host in enumerate(hosts):
        if not isinstance(host, dict):
            raise ConfigError(f"config.hosts[{index}] must be an object")
        _require(host, "name", str, f"config.hosts[{index}]")
        mode = _require(host, "mode", str, f"config.hosts[{index}]")
        if mode not in {"local", "ssh"}:
            raise ConfigError(f"config.hosts[{index}].mode must be local or ssh")
        if mode == "ssh":
            ssh = _require(host, "ssh", dict, f"config.hosts[{index}]")
            for key in ("host", "user", "identity_file", "known_hosts_file"):
                _require(ssh, key, str, f"config.hosts[{index}].ssh")
            for key in ("identity_file", "known_hosts_file"):
                if not ssh[key].startswith("/"):
                    raise ConfigError(f"config.hosts[{index}].ssh.{key} must be an absolute path")

    checks = config.get("http_checks", [])
    if not isinstance(checks, list):
        raise ConfigError("config.http_checks must be an array")
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise ConfigError(f"config.http_checks[{index}] must be an object")
        _require(check, "name", str, f"config.http_checks[{index}]")
        url = _require(check, "url", str, f"config.http_checks[{index}]")
        if not url.startswith(("https://", "http://")):
            raise ConfigError(f"config.http_checks[{index}].url must be HTTP(S)")

    return config
