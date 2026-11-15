"""orin-updater command line."""

from __future__ import annotations

import argparse
import getpass
import os
import pwd
import subprocess
import sys
from pathlib import Path

from . import BACKUP_LABEL, __version__
from .config import Config

DEFAULT_WORKDIR = Path("~/orin-updater").expanduser()


def default_user() -> str:
    return os.environ.get("SUDO_USER") or getpass.getuser()


def home_of(user: str) -> Path:
    return Path(pwd.getpwnam(user).pw_dir)


def read_passphrase(path: str | None) -> str:
    if path:
        return Path(path).read_text().rstrip("\n")
    print("The backup holds your SSH keys and password hash: choose a strong passphrase.")
    print("You will type it on the Jetson's console at first boot after the upgrade.")
    while True:
        p1 = getpass.getpass("Backup passphrase: ")
        if len(p1) < 8:
            print("At least 8 characters, please.")
            continue
        if getpass.getpass("Again: ") == p1:
            return p1
        print("Passphrases differ, try again.")


def need_root(what: str) -> None:
    if os.geteuid() != 0:
        sys.exit(f"{what} needs root: re-run with sudo (e.g. sudo -E orin-updater ...)")


def confirm(prompt: str, expected: str) -> bool:
    try:
        return input(f"{prompt}\nType '{expected}' to continue: ").strip() == expected
    except EOFError:
        return False


# --------------------------------------------------------------------------- commands

def cmd_check(args) -> int:
    from .preflight import run_checks
    user = args.user or default_user()
    cfg = Config.load(args.config, home_of(user))
    checks = run_checks(home_of(user), cfg, measure_home=not args.quick)
    for c in checks:
        print(c.render())
    return 1 if any(not c.ok and c.fatal for c in checks) else 0


def cmd_inventory(args) -> int:
    from .inventory import collect
    out = Path(args.out)
    collect(out, args.user or default_user())
    print(f"Inventory written to {out} (see {out / 'reinstall.sh'})")
    return 0


def cmd_backup(args) -> int:
    from . import usb
    from .backup import BackupError, run_backup
    need_root("backup")
    user = args.user or default_user()
    if user == "root":
        sys.exit("refusing to back up root; pass --user <name> (or run through sudo)")
    cfg = Config.load(args.config, home_of(user))
    passphrase = read_passphrase(args.passphrase_file)
    mounted = False
    try:
        if args.usb:
            out = usb.mount_backup()
            mounted = True
        else:
            out = Path(args.out)
        print(f"Backing up {user} to {out}")
        run_backup(user, out, passphrase, cfg, verify=not args.no_verify)
        print("Backup complete and verified (no INCOMPLETE marker left).")
        print("Recommended before rebooting: orin-updater verify-backup "
              + ("--usb" if args.usb else f"--dir {out}"))
        return 0
    except (BackupError, usb.UsbError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("The backup is INCOMPLETE and will not be used by the restore. Do NOT start the "
              "installation from this key; fix the problem and run the backup again "
              "(it discards the unfinished attempt itself).", file=sys.stderr)
        return 1
    finally:
        if mounted:
            usb.unmount_backup()


def cmd_verify_backup(args) -> int:
    from . import usb
    from .backup import verify_backup_dir
    need_root("verify-backup")
    mounted = False
    try:
        if args.usb:
            out = usb.mount_backup()
            mounted = True
        else:
            out = Path(args.dir)
        print(f"Checking {out} (sizes and sha256 of every part; no passphrase needed) ...")
        problems = verify_backup_dir(out)
    except usb.UsbError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if mounted:
            usb.unmount_backup()
    if problems:
        print("BACKUP NOT USABLE:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print("OK: the backup is complete and every part matches the manifest.")
    return 0


def cmd_fetch_iso(args) -> int:
    from .iso import fetch, sha256
    path = fetch(args.release, Path(args.dir).expanduser())
    print(f"{path}\nsha256 {sha256(path)}")
    return 0


def cmd_inspect_iso(args) -> int:
    from .iso import inspect
    rep = inspect(Path(args.iso))
    print(f"volume: {rep.volume}  files: {len(rep.files)}  patched by orin-updater: "
          f"{rep.patched}")
    print(f"installer layer: /casper/{rep.leaf}.squashfs")
    print("grub entries:", *rep.grub_entries, sep="\n  ")
    if args.verbose:
        print(*rep.files, sep="\n")
    return 0


def cmd_patch_iso(args) -> int:
    from .iso import IsoError, compare_boot, patch
    src = Path(args.iso)
    out = Path(args.output) if args.output else src.with_name(src.stem + "-orin-updater.iso")
    print(f"Patching {src} -> {out}")
    try:
        patch(src, out)
    except IsoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    problems = compare_boot(src, out)
    if problems:
        print("ERROR: the patched ISO would not boot like the original:", *problems,
              sep="\n  ", file=sys.stderr)
        return 1
    print("OK: El Torito, GPT layout and EFI partition bytes match the original.")
    print(f"Patched ISO: {out}")
    return 0


def cmd_make_usb(args) -> int:
    from . import usb
    need_root("make-usb")
    iso = Path(args.iso)
    disk = usb.get_disk(args.device)
    min_size = iso.stat().st_size + args.min_backup_gb * 1024**3
    problems = usb.validate_target(disk, min_size)
    if problems:
        print("Refusing to write:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"Target: {disk.path}  {disk.model}  {disk.size / 1e9:.1f} GB  (USB)")
    print(f"ALL DATA ON {disk.path} WILL BE ERASED.")
    if not args.yes and not confirm("", os.path.basename(disk.path)):
        print("Aborted.")
        return 1
    part = usb.write_key(iso, disk)
    print(f"Done: {disk.path} boots the installer, {part} ('{BACKUP_LABEL}') awaits the backup:")
    print("  sudo orin-updater backup --usb")
    return 0


def cmd_restore(args) -> int:
    from importlib import resources
    need_root("restore")
    script = resources.files("orin_updater") / "target" / "orin_restore.py"
    cmd = [sys.executable, str(script)]
    if args.backup_dir:
        cmd += ["--backup-dir", args.backup_dir]
    if args.no_finalize:
        cmd.append("--no-finalize")
    if args.skip_home:
        cmd.append("--skip-home")
    return subprocess.call(cmd)


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="orin-updater",
        description="Upgrade a Jetson Orin from JetPack 6 to JetPack 7 keeping your account.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="pre-flight checks (firmware, tools, backup size)")
    p.add_argument("--user")
    p.add_argument("--config")
    p.add_argument("--quick", action="store_true", help="skip measuring the home directory")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("inventory", help="list installed software + generate reinstall.sh")
    p.add_argument("--user")
    p.add_argument("--out", default=str(DEFAULT_WORKDIR / "inventory"))
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("backup", help="encrypted backup of account, home and system config")
    p.add_argument("--user", help="account to back up (default: the user who ran sudo)")
    dest = p.add_mutually_exclusive_group(required=True)
    dest.add_argument("--usb", action="store_true",
                      help=f"write to the '{BACKUP_LABEL}' partition created by make-usb")
    dest.add_argument("--out", help="write to this (empty) directory")
    p.add_argument("--config", help="YAML config (default ~/.config/orin-updater/config.yaml)")
    p.add_argument("--passphrase-file", help="read the passphrase from a file (automation)")
    p.add_argument("--no-verify", action="store_true", help="skip the decrypt+list verification")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("verify-backup",
                       help="check that a backup is complete (marker, manifest, sizes, sha256)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--usb", action="store_true", help=f"the '{BACKUP_LABEL}' partition")
    src.add_argument("--dir", help="a backup directory")
    p.set_defaults(func=cmd_verify_backup)

    p = sub.add_parser("fetch-iso", help="download NVIDIA's Jetson ISO installer")
    p.add_argument("--release", default="r39.2.1")
    p.add_argument("--dir", default=str(DEFAULT_WORKDIR))
    p.set_defaults(func=cmd_fetch_iso)

    p = sub.add_parser("inspect-iso", help="show the installer's autoinstall config and boot setup")
    p.add_argument("iso")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_inspect_iso)

    p = sub.add_parser("patch-iso", help="add the first-boot restore hook to the ISO")
    p.add_argument("iso")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_patch_iso)

    p = sub.add_parser("make-usb", help="write the patched ISO to a USB key + backup partition")
    p.add_argument("device", help="whole USB disk, e.g. /dev/sda")
    p.add_argument("--iso", required=True, help="patched ISO (from patch-iso)")
    p.add_argument("--min-backup-gb", type=int, default=16,
                   help="refuse keys leaving less than this for the backup (default 16)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_make_usb)

    p = sub.add_parser("restore", help="restore manually (on the upgraded system)")
    p.add_argument("--backup-dir")
    p.add_argument("--no-finalize", action="store_true")
    p.add_argument("--skip-home", action="store_true")
    p.set_defaults(func=cmd_restore)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
