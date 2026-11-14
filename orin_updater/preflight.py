"""Pre-flight checks: is this machine ready to be upgraded?"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config

REQUIRED_TOOLS = {
    "backup": ["tar", "zstd", "gpg", "sha256sum"],
    "iso": ["xorriso", "unsquashfs", "mksquashfs"],
    "usb": ["sfdisk", "sgdisk", "mkfs.ext4", "lsblk", "blkid", "partprobe", "rsync"],
}
TOOL_PACKAGES = {
    "xorriso": "xorriso",
    "unsquashfs": "squashfs-tools",
    "mksquashfs": "squashfs-tools",
    "sgdisk": "gdisk",
    "zstd": "zstd",
    "gpg": "gnupg",
    "partprobe": "parted",
    "rsync": "rsync",
    "mkfs.ext4": "e2fsprogs",
}

MIN_FIRMWARE = (36, 0)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = True

    def render(self) -> str:
        mark = "OK  " if self.ok else ("FAIL" if self.fatal else "WARN")
        return f"[{mark}] {self.name}: {self.detail}"


def parse_nv_tegra_release(text: str) -> tuple[int, str] | None:
    """'# R36 (release), REVISION: 4.7, ...' -> (36, '4.7')."""
    m = re.search(r"#\s*R(\d+)\s*\(release\),\s*REVISION:\s*([\d.]+)", text)
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def parse_firmware_version(text: str) -> tuple[int, ...] | None:
    """Parse 'Current version: 36.4.7' from `nvbootctrl dump-slots-info`."""
    m = re.search(r"Current version:\s*([\d.]+)", text)
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split(".") if x)


def missing_tools(groups: list[str]) -> list[str]:
    tools = [t for g in groups for t in REQUIRED_TOOLS[g]]
    return [t for t in dict.fromkeys(tools) if shutil.which(t) is None]


def install_hint(tools: list[str]) -> str:
    pkgs = sorted({TOOL_PACKAGES.get(t, t) for t in tools})
    return "sudo apt install " + " ".join(pkgs)


def is_excluded(rel: str, cfg: Config) -> bool:
    for ex in cfg.home_excludes:
        ex = ex.strip("/")
        if fnmatch.fnmatch(rel, ex) or rel.startswith(ex + "/"):
            return True
        # glob with a directory prefix, e.g. snap/*/common/.cache/foo
        parts = rel.split("/")
        for i in range(1, len(parts)):
            if fnmatch.fnmatch("/".join(parts[:i]), ex):
                return True
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, p) for p in cfg.pattern_excludes)


def estimate_home_size(home: Path, cfg: Config) -> tuple[int, int]:
    """Return (included_bytes, excluded_bytes) using on-disk sizes."""
    included = excluded = 0
    for root, dirs, files in os.walk(home, followlinks=False):
        rel_root = os.path.relpath(root, home)
        rel_root = "" if rel_root == "." else rel_root + "/"
        keep = []
        for d in dirs:
            rel = rel_root + d
            if is_excluded(rel, cfg):
                excluded += _du(os.path.join(root, d))
            else:
                keep.append(d)
        dirs[:] = keep
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            size = st.st_blocks * 512
            if is_excluded(rel_root + f, cfg):
                excluded += size
            else:
                included += size
    return included, excluded


def _du(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_blocks * 512
            except OSError:
                pass
    return total


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def run_checks(user_home: Path, cfg: Config, measure_home: bool = True) -> list[Check]:
    checks: list[Check] = []

    model = _read("/proc/device-tree/model").rstrip("\0").strip()
    checks.append(Check("Board", "Orin" in model, model or "unknown (not a Jetson?)"))

    rel = parse_nv_tegra_release(_read("/etc/nv_tegra_release"))
    if rel is None:
        checks.append(Check("Jetson Linux", False, "/etc/nv_tegra_release not found or unparsable"))
    else:
        major, rev = rel
        checks.append(Check(
            "Jetson Linux", major == 36,
            f"R{major}.{rev}"
            + ("" if major == 36 else " (this tool targets JetPack 6 / R36 sources)"),
            fatal=False,
        ))

    fw = _firmware_version()
    if fw is None:
        checks.append(Check(
            "QSPI firmware", False,
            "could not read (run as root, or check the UEFI menu: must be >= 36.0)", fatal=False))
    else:
        checks.append(Check(
            "QSPI firmware", fw >= MIN_FIRMWARE,
            ".".join(map(str, fw)) + " (JetPack 7 ISO requires >= 36.0)"))

    for group in ("backup", "iso", "usb"):
        miss = missing_tools([group])
        checks.append(Check(
            f"Tools ({group})", not miss,
            "all present" if not miss else f"missing {', '.join(miss)} -> {install_hint(miss)}",
            fatal=group == "backup"))

    if measure_home:
        inc, exc = estimate_home_size(user_home, cfg)
        checks.append(Check(
            "Home size", True,
            f"{human(inc)} to back up ({human(exc)} excluded as caches); "
            f"USB key must hold ~{human(inc * 0.8 + 6 * 1024**3)} (archive ~compressed + ISO)",
            fatal=False))

    checks.append(Check(
        "Docker data", True,
        "/var/lib/docker (images/volumes) is NOT backed up: export what you need "
        "(docker save / volume tar) into your home first", fatal=False))
    checks.append(Check(
        "Ubuntu 22.04 -> 24.04", True,
        "Python venvs, compiled binaries, CUDA/TensorRT engines and out-of-tree "
        "modules in your home must be rebuilt after the upgrade", fatal=False))
    return checks


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def parse_bios_version(text: str) -> tuple[int, ...] | None:
    """Parse the UEFI firmware version from /sys/class/dmi/id/bios_version
    ('36.4.7-gcid-42132812' -> (36, 4, 7))."""
    m = re.match(r"\s*(\d+(?:\.\d+)+)", text)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _firmware_version() -> tuple[int, ...] | None:
    # The SMBIOS BIOS version is the running UEFI (QSPI) firmware and is world-readable.
    fw = parse_bios_version(_read("/sys/class/dmi/id/bios_version"))
    if fw is not None:
        return fw
    # nvbootctrl prints a bogus "0.0.x" when it can't open the QSPI (not root): ignore that.
    if shutil.which("nvbootctrl") is None or os.geteuid() != 0:
        return None
    try:
        res = subprocess.run(["nvbootctrl", "dump-slots-info"], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if "Permission denied" in res.stdout + res.stderr:
        return None
    return parse_firmware_version(res.stdout)
