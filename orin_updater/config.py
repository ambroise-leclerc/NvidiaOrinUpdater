"""User-tunable configuration (defaults + optional YAML override file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Paths relative to the user's home that are pure caches / regenerable data.
DEFAULT_HOME_EXCLUDES = [
    ".cache",
    ".local/share/Trash",
    ".ccache",
    ".npm/_cacache",
    ".cargo/registry/cache",
    ".gradle/caches",
    ".thumbnails",
    "snap/*/common/.cache",
]

# Basename patterns excluded anywhere under the home directory.
DEFAULT_PATTERN_EXCLUDES = [
    "__pycache__",
]

# System files carried over (besides the account itself). Globs are allowed.
SYSTEM_FILES = {
    "ssh_host_keys": ["/etc/ssh/ssh_host_*"],
    "hostname": ["/etc/hostname", "/etc/hosts"],
    "network": ["/etc/NetworkManager/system-connections/*"],
    "locale": ["/etc/timezone", "/etc/default/locale", "/etc/default/keyboard"],
    "sudoers": ["/etc/sudoers.d/*"],
    "fstab": ["/etc/fstab"],
}

DEFAULT_CONFIG_PATH = Path("~/.config/orin-updater/config.yaml")
CHUNK_SIZE_DEFAULT = 4 * 1024**3  # 4 GiB per archive part


@dataclass
class Config:
    home_excludes: list[str] = field(default_factory=lambda: list(DEFAULT_HOME_EXCLUDES))
    pattern_excludes: list[str] = field(default_factory=lambda: list(DEFAULT_PATTERN_EXCLUDES))
    system_items: list[str] = field(default_factory=lambda: list(SYSTEM_FILES))
    chunk_size: int = CHUNK_SIZE_DEFAULT

    @classmethod
    def load(cls, path: str | os.PathLike | None, home: Path | None = None) -> Config:
        cfg = cls()
        if path is None:
            base = home if home is not None else Path.home()
            candidate = base / ".config/orin-updater/config.yaml"
            if not candidate.is_file():
                return cfg
            path = candidate
        data = yaml.safe_load(Path(path).read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top-level YAML must be a mapping")
        # 'extra_home_excludes' extends the defaults, 'home_excludes' replaces them.
        if "home_excludes" in data:
            cfg.home_excludes = list(data["home_excludes"])
        cfg.home_excludes += list(data.get("extra_home_excludes", []))
        if "pattern_excludes" in data:
            cfg.pattern_excludes = list(data["pattern_excludes"])
        if "system_items" in data:
            unknown = set(data["system_items"]) - set(SYSTEM_FILES)
            if unknown:
                raise ValueError(f"unknown system_items: {sorted(unknown)}")
            cfg.system_items = list(data["system_items"])
        if "chunk_size_mib" in data:
            cfg.chunk_size = int(data["chunk_size_mib"]) * 1024**2
        return cfg

    def tar_excludes(self, home_rel: str) -> list[str]:
        """Return --exclude arguments for a tar run with -C / on `home_rel` (e.g. 'home/bob')."""
        args = []
        for rel in self.home_excludes:
            args.append(f"--exclude={home_rel}/{rel.strip('/')}")
        for pat in self.pattern_excludes:
            args.append(f"--exclude={pat}")
        return args
