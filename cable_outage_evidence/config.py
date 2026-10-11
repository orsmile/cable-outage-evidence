"""Settings, read from an INI file with defaults for every option (spec §12)."""

import configparser
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import List, Tuple

APP = "cable-outage-evidence"


def default_base_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / APP


def default_config_path() -> Path:
    return default_base_dir() / "config.ini"


@dataclass
class Config:
    interval_ms: int = 500
    ping_timeout_sec: float = 1.0
    ping_retries: int = 2
    sustained_samples: int = 4
    recovery_rounds: int = 2
    pre_event_seconds: int = 120
    post_recovery_seconds: int = 120
    tcp_timeout_sec: float = 2.0
    # name:address pairs; the names become CSV column prefixes.
    public_targets: List[Tuple[str, str]] = field(default_factory=lambda: [
        ("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8"), ("TWNIC", "101.101.101.101")])
    path_discovery_target: str = "1.1.1.1"
    path_discovery_interval_sec: int = 300
    modem_type: str = "auto"
    modem_url: str = "http://192.168.0.1"
    # Hitron's factory default; some ISPs change it or print it on the label.
    modem_username: str = "admin"
    modem_password: str = "password"
    modem_state_poll_interval_sec: int = 5
    modem_poll_interval_sec: int = 30
    modem_channel_ds_interval_sec: int = 300
    modem_log_interval_sec: int = 300
    modem_fast_min_sec: int = 60
    modem_fast_post_sec: int = 60
    modem_fast_statechange_sec: int = 90
    modem_fast_max_sec: int = 300
    modem_fast_dsinfo_interval_sec: int = 5
    modem_fast_log_interval_sec: int = 15
    retention_days: int = 180
    data_dir: str = ""
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def modem_address(self) -> str:
        """Host part of modem_url; it is also the modem probe target."""
        host = self.modem_url.split("://", 1)[-1].split("/", 1)[0]
        return host.rsplit(":", 1)[0] if host.count(":") == 1 else host

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_base_dir() / "data"


# Options that must never appear in logs, reports or error messages.
SECRET_OPTIONS = {"modem_password", "telegram_bot_token"}


def _parse_targets(text: str) -> List[Tuple[str, str]]:
    targets = []
    for item in text.split():
        name, sep, address = item.partition(":")
        if not sep or not name.isalnum() or not address:
            raise ValueError(f"public_targets: expected name:address, got {item!r}")
        targets.append((name, address))
    if len(targets) < 2:
        raise ValueError("public_targets: at least two targets are needed")
    return targets


def load(path: Path = None) -> Config:
    """Reads the [monitor] section; options not in the file keep their defaults."""
    cfg = Config()
    path = path or default_config_path()
    if not path.exists():
        return cfg
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    if not parser.has_section("monitor"):
        return cfg
    section = parser["monitor"]
    for f in fields(Config):
        if f.name not in section:
            continue
        raw = section[f.name].strip()
        if f.name == "public_targets":
            value = _parse_targets(raw)
        elif f.type is bool or f.type == "bool":
            value = section.getboolean(f.name)
        elif f.type is int or f.type == "int":
            value = int(raw)
        elif f.type is float or f.type == "float":
            value = float(raw)
        else:
            value = raw
        setattr(cfg, f.name, value)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.interval_ms < 200:
        raise ValueError("interval_ms must be at least 200")
    for name in ("ping_retries", "sustained_samples", "recovery_rounds"):
        if getattr(cfg, name) < 1:
            raise ValueError(f"{name} must be at least 1")
    if cfg.modem_fast_statechange_sec < 90:
        raise ValueError("modem_fast_statechange_sec must be at least 90")
    if cfg.modem_type not in ("auto", "hitron-coda", "cbn", "none"):
        raise ValueError("modem_type must be auto, hitron-coda, cbn or none")


def save(cfg: Config, path: Path = None) -> Path:
    """Writes every option. The file holds the modem password, so on POSIX it is
    created mode 0600; on Windows it lives in the user's own profile."""
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser(interpolation=None)
    values = {}
    for f in fields(Config):
        value = getattr(cfg, f.name)
        if f.name == "public_targets":
            value = " ".join(f"{n}:{a}" for n, a in value)
        elif isinstance(value, bool):
            value = "1" if value else "0"
        values[f.name] = str(value)
    parser["monitor"] = values
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        parser.write(fh)
    return path
