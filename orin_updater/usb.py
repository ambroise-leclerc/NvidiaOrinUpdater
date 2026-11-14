"""Write the patched ISO to a USB key and add an ext4 'ORINBAK' partition for the backup."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import BACKUP_DIRNAME, BACKUP_LABEL

MOUNTPOINT = Path("/run/orin-updater/usb")


class UsbError(RuntimeError):
    pass


@dataclass
class Disk:
    path: str
    size: int
    tran: str | None
    model: str
    removable: bool
    mountpoints: list[str]
    children: list[dict]


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if res.returncode != 0:
        raise UsbError(f"{' '.join(cmd)} failed: {res.stderr.strip()}")
    return res


def lsblk() -> list[dict]:
    out = _run(["lsblk", "-J", "-b", "-o",
                "NAME,PATH,SIZE,TRAN,RM,TYPE,MODEL,MOUNTPOINT,LABEL"]).stdout
    return json.loads(out)["blockdevices"]


def _mounts(dev: dict) -> list[str]:
    found = [dev["mountpoint"]] if dev.get("mountpoint") else []
    for child in dev.get("children", []) or []:
        found += _mounts(child)
    return found


def get_disk(path: str, devices: list[dict] | None = None) -> Disk:
    for d in devices if devices is not None else lsblk():
        if d.get("path") == path:
            return Disk(path=d["path"], size=int(d["size"]), tran=d.get("tran"),
                        model=(d.get("model") or "").strip(),
                        removable=str(d.get("rm")).lower() in ("1", "true"),
                        mountpoints=_mounts(d), children=d.get("children") or [])
    raise UsbError(f"{path} is not a whole disk (use e.g. /dev/sda, not /dev/sda1)")


def validate_target(disk: Disk, min_size: int) -> list[str]:
    """Return a list of reasons why `disk` must NOT be overwritten (empty = OK)."""
    problems = []
    if disk.tran != "usb":
        problems.append(f"{disk.path} is not a USB device (transport: {disk.tran or 'none'})")
    if os.path.basename(disk.path).startswith(("nvme", "mmcblk", "zram", "loop")):
        problems.append(f"{disk.path} looks like internal storage")
    critical = {"/", "/boot", "/boot/efi", "/home", "[SWAP]"}
    if critical & set(disk.mountpoints):
        hit = sorted(critical & set(disk.mountpoints))
        problems.append(f"{disk.path} hosts system mounts {hit}")
    if disk.size < min_size:
        problems.append(f"{disk.path} is too small ({disk.size / 1e9:.0f} GB < "
                        f"{min_size / 1e9:.0f} GB needed)")
    return problems


def write_key(iso: Path, disk: Disk, log=print) -> str:
    """dd the ISO, extend the GPT to the whole key and create+format ORINBAK. Returns its path."""
    for mp in disk.mountpoints:
        _run(["umount", mp])
    log(f"* Writing {iso.name} to {disk.path} ...")
    subprocess.run(["dd", f"if={iso}", f"of={disk.path}", "bs=4M", "conv=fsync",
                    "oflag=direct", "status=progress"], check=True)
    # The ISO's backup GPT header sits at the end of the image: move it to the end of the key,
    # then create a partition in the free space that follows the ISO.
    _run(["sgdisk", "-e", disk.path])
    _run(["sgdisk", "-a", "2048", "-n", "0:0:0", "-t", "0:8300", "-c", f"0:{BACKUP_LABEL}",
          disk.path])
    _run(["partprobe", disk.path])
    subprocess.run(["udevadm", "settle"], check=False)
    part = _last_partition(disk.path)
    log(f"* Formatting {part} as ext4 '{BACKUP_LABEL}' ...")
    _run(["mkfs.ext4", "-q", "-F", "-L", BACKUP_LABEL, part])
    return part


def _last_partition(path: str) -> str:
    for _ in range(10):
        disk = get_disk(path)
        parts = [c["path"] for c in disk.children if c.get("type") == "part"]
        if parts:
            return sorted(parts, key=lambda p: int("".join(filter(str.isdigit, p[len(path):]))
                                                     or 0))[-1]
        time.sleep(1)
    raise UsbError(f"no partition found on {path} after partitioning")


def find_backup_partition() -> str:
    res = subprocess.run(["blkid", "-L", BACKUP_LABEL], capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        raise UsbError(f"no partition labelled {BACKUP_LABEL}: run `orin-updater make-usb` first")
    return res.stdout.strip()


def mount_backup(part: str | None = None) -> Path:
    part = part or find_backup_partition()
    MOUNTPOINT.mkdir(parents=True, exist_ok=True)
    if not os.path.ismount(MOUNTPOINT):
        _run(["mount", part, str(MOUNTPOINT)])
    out = MOUNTPOINT / BACKUP_DIRNAME
    out.mkdir(mode=0o700, exist_ok=True)
    return out


def unmount_backup() -> None:
    if os.path.ismount(MOUNTPOINT):
        subprocess.run(["sync"], check=False)
        _run(["umount", str(MOUNTPOINT)])
