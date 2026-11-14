#!/usr/bin/python3
"""orin-restore: recreate a user account from an orin-updater backup.

Runs on the *freshly installed* system (Ubuntu 24.04 / JetPack 7), either:
  * automatically at first boot (orin-restore.service, before NVIDIA's oem-config), or
  * manually:  sudo python3 orin_restore.py [--backup-dir DIR]

Standalone on purpose (stdlib only, no orin_updater import): it is copied onto the target.
If anything goes wrong at first boot, NVIDIA's oem-config runs as if nothing happened.
"""

from __future__ import annotations

import argparse
import getpass
import grp
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LABEL = "ORINBAK"
BACKUP_DIRNAME = "orin-backup"
STATE_DIR = Path("/var/lib/orin-updater")
PENDING = STATE_DIR / "pending"
LOG_PATH = Path("/var/log/orin-restore.log")
MOUNTPOINT = Path("/run/orin-updater/backup")
OEM_UNITS = ["nv-oem-config.service", "nv-oem-config-gui.service"]  # JetPack 6 first boot
FIRSTBOOT_TARGETS = {"nv-oobe.target", "nv-oem-config.target"}  # JetPack 7 / JetPack 6
# Groups NVIDIA's installer gives its default user on JetPack 7 (added when they exist).
JETSON_GROUPS = ["adm", "dialout", "sudo", "audio", "video", "render", "i2c", "debug",
                 "trusty", "crypto", "gpio", "weston-launch"]
GPG_DECRYPT = ["gpg", "--batch", "--no-tty", "--quiet", "--pinentry-mode", "loopback", "--decrypt"]
TAR_EXTRACT = ["tar", "-C", "/", "-xpf", "-", "--numeric-owner", "--xattrs", "--acls",
               "--xattrs-include=*"]
FSTAB_SKIP_MOUNTS = {"/", "/boot", "/boot/efi", "none", "swap"}


class RestoreError(RuntimeError):
    pass


_log_fh = None


def log(msg: str) -> None:
    line = f"[orin-restore {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def run(cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and res.returncode != 0:
        raise RestoreError(f"{' '.join(cmd)} failed ({res.returncode}): {res.stderr.strip()}")
    return res


# --------------------------------------------------------------------------- locate backup

def find_backup(explicit: str | None, wait: int = 60) -> Path:
    if explicit:
        d = Path(explicit)
        if (d / BACKUP_DIRNAME / "manifest.json").is_file():
            d = d / BACKUP_DIRNAME
        if not (d / "manifest.json").is_file():
            raise RestoreError(f"no manifest.json in {d}")
        return d
    dev = None
    for _ in range(wait):
        res = run(["blkid", "-L", LABEL], check=False)
        if res.returncode == 0 and res.stdout.strip():
            dev = res.stdout.strip()
            break
        time.sleep(1)
    if dev is None:
        raise RestoreError(f"no partition labelled {LABEL} found (is the USB key plugged in?)")
    MOUNTPOINT.mkdir(parents=True, exist_ok=True)
    if not os.path.ismount(MOUNTPOINT):
        run(["mount", "-o", "ro", dev, str(MOUNTPOINT)])
    d = MOUNTPOINT / BACKUP_DIRNAME
    if not (d / "manifest.json").is_file():
        raise RestoreError(f"{dev} has no {BACKUP_DIRNAME}/manifest.json")
    return d


# --------------------------------------------------------------------------- decryption

PROMPT_TIMEOUT = 900  # seconds; a headless box must not wait forever at first boot


def ask_passphrase(user: str, attempt: int, timeout: int = PROMPT_TIMEOUT) -> str:
    prompt = (f"orin-updater: passphrase to restore account '{user}' "
              f"(attempt {attempt}/3, empty = skip and run normal setup): ")
    if sys.stdin.isatty():
        return _tty_read_secret(prompt, timeout)
    if shutil.which("systemd-ask-password"):
        res = subprocess.run(["systemd-ask-password", f"--timeout={timeout}", prompt],
                             capture_output=True, text=True)
        return res.stdout.rstrip("\n")
    return getpass.getpass(prompt)


def _tty_read_secret(prompt: str, timeout: int) -> str:
    import select
    import termios
    fd = sys.stdin.fileno()
    sys.stdout.write("\n" + prompt)
    sys.stdout.flush()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            sys.stdout.write("\n(timed out)")
            return ""
        return sys.stdin.readline().rstrip("\n")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()


def gpg_env(tmp: Path) -> dict:
    env = dict(os.environ)
    home = tmp / "gnupg"
    home.mkdir(mode=0o700, exist_ok=True)
    env["GNUPGHOME"] = str(home)
    return env


def decrypt_to_tar(parts: list[tuple[Path, str]], passphrase: str, tar_args: list[str],
                   tmp: Path, progress: bool = False) -> None:
    """Stream parts (checking sha256 on the fly) | gpg -d | zstd -d | tar <tar_args>."""
    r, w = os.pipe()
    os.write(w, passphrase.encode() + b"\n")
    os.close(w)
    gpg = subprocess.Popen([*GPG_DECRYPT, "--passphrase-fd", str(r)], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(r,),
                           env=gpg_env(tmp))
    os.close(r)
    zst = subprocess.Popen(["zstd", "-d", "-q", "-c"], stdin=gpg.stdout, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)
    gpg.stdout.close()
    tar = subprocess.Popen(tar_args, stdin=zst.stdout, stderr=subprocess.PIPE)
    zst.stdout.close()
    bad = None
    total = sum(p.stat().st_size for p, _ in parts)
    done = 0
    t0 = last = time.time()
    try:
        for path, expected in parts:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
                    gpg.stdin.write(block)
                    done += len(block)
                    if progress and time.time() - last > 10:
                        last = time.time()
                        rate = done / max(last - t0, 1) / 1024**2
                        log(f"  {done * 100 // max(total, 1)}% ({done / 1024**3:.1f} GiB, "
                            f"{rate:.0f} MiB/s)")
            if expected and h.hexdigest() != expected:
                bad = path.name
                break
    except BrokenPipeError:
        pass
    finally:
        try:
            gpg.stdin.close()
        except BrokenPipeError:
            pass
    rc_tar, rc_zst = tar.wait(), zst.wait()
    rc_gpg = gpg.wait()
    gpg_err = gpg.stderr.read().decode(errors="replace").strip()
    tar_err = tar.stderr.read().decode(errors="replace").strip()
    if bad:
        raise RestoreError(f"checksum mismatch on {bad}: backup is corrupted")
    if rc_gpg != 0:
        if "bad passphrase" in gpg_err.lower() or "decryption failed" in gpg_err.lower():
            raise BadPassphrase(gpg_err)
        raise RestoreError(f"gpg failed ({rc_gpg}): {gpg_err}")
    if rc_zst != 0 or rc_tar != 0:
        raise RestoreError(f"extraction failed (zstd={rc_zst}, tar={rc_tar}): {tar_err[-2000:]}")


class BadPassphrase(RestoreError):
    pass


# --------------------------------------------------------------------------- identity

def group_exists(name: str) -> grp.struct_group | None:
    try:
        return grp.getgrnam(name)
    except KeyError:
        return None


def gid_free(gid: int) -> bool:
    try:
        grp.getgrgid(gid)
        return False
    except KeyError:
        return True


def ensure_group(name: str, gid: int) -> str:
    existing = group_exists(name)
    if existing:
        if existing.gr_gid != gid:
            log(f"  group {name} exists with gid {existing.gr_gid} (was {gid})")
        return name
    cmd = ["groupadd"]
    if gid_free(gid):
        cmd += ["-g", str(gid)]
    elif gid < 1000:
        cmd += ["-r"]
    run([*cmd, name])
    log(f"  created group {name} (gid {grp.getgrnam(name).gr_gid})")
    return name


def apply_identity(ident: dict) -> None:
    name, uid = ident["name"], ident["uid"]
    log(f"* Account {name} (uid {uid})")
    # Groups that can keep their original gid go first, so that auto-allocated gids of
    # conflicting groups can't steal a gid another restored group needs.
    wanted = [{"name": ident["group"], "gid": ident["gid"]}, *ident["groups"]]
    keeps = [g for g in wanted if group_exists(g["name"]) or gid_free(g["gid"])]
    for g in keeps + [g for g in wanted if g not in keeps]:
        ensure_group(g["name"], g["gid"])
    extra = [g["name"] for g in ident["groups"]]
    extra += [g for g in JETSON_GROUPS if g not in extra and group_exists(g)]
    shell = ident["shell"] if os.path.exists(ident["shell"]) else "/bin/bash"
    if shell != ident["shell"]:
        log(f"  shell {ident['shell']} not installed yet, using {shell} (chsh later)")
    try:
        existing = pwd.getpwnam(name)
    except KeyError:
        existing = None
    if existing is None:
        try:
            other = pwd.getpwuid(uid)
            raise RestoreError(f"uid {uid} already belongs to {other.pw_name}")
        except KeyError:
            pass
        run(["useradd", "-u", str(uid), "-g", ident["group"], "-d", ident["home"], "-M",
             "-s", shell, "-c", ident["gecos"], name])
    elif existing.pw_uid != uid:
        raise RestoreError(f"user {name} already exists with uid {existing.pw_uid} != {uid}")
    else:
        run(["usermod", "-s", shell, "-c", ident["gecos"], name])
    if extra:
        run(["usermod", "-a", "-G", ",".join(extra), name])
    if ident.get("password_hash"):
        run(["usermod", "-p", ident["password_hash"], name])
    log(f"  groups: {', '.join([ident['group'], *extra])}")


# --------------------------------------------------------------------------- system files

def merge_fstab(old: str, new: str) -> tuple[str, list[str]]:
    """Append to `new` the user mounts of `old` (with nofail). Returns (text, added mountpoints)."""
    def entries(text):
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                f = s.split()
                if len(f) >= 4:
                    yield line, f
    present = {f[1] for _, f in entries(new)}
    added, lines = [], []
    for _line, f in entries(old):
        if f[1] in present or f[1] in FSTAB_SKIP_MOUNTS or f[2] == "swap":
            continue
        opts = f[3].split(",")
        if "nofail" not in opts:
            opts.append("nofail")
        f[3] = ",".join(opts)
        lines.append("\t".join(f))
        added.append(f[1])
    if not lines:
        return new, []
    out = new.rstrip("\n") + "\n\n# --- restored by orin-updater (nofail added) ---\n"
    return out + "\n".join(lines) + "\n", added


def merge_hosts(old: str, new: str, hostname: str) -> str:
    out = []
    for line in new.splitlines():
        f = line.split()
        if f and f[0] == "127.0.1.1":
            line = f"127.0.1.1\t{hostname}"
        out.append(line)
    if not any(ln.split()[:1] == ["127.0.1.1"] for ln in out):
        out.insert(1, f"127.0.1.1\t{hostname}")
    known = {tuple(ln.split()) for ln in out}
    standard = {"127.0.0.1", "127.0.1.1", "::1", "fe00::0", "ff00::0", "ff02::1", "ff02::2"}
    extra = [ln for ln in old.splitlines()
             if ln.split() and not ln.lstrip().startswith("#")
             and ln.split()[0] not in standard and tuple(ln.split()) not in known]
    if extra:
        out += ["", "# --- restored by orin-updater ---", *extra]
    return "\n".join(out) + "\n"


def install_file(src: Path, dst: Path, mode: int, uid: int = 0, gid: int = 0) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".orin-tmp")
    shutil.copyfile(src, tmp)
    os.chown(tmp, uid, gid)
    os.chmod(tmp, mode)
    os.replace(tmp, dst)


def apply_system(stage: Path, ident: dict, root: Path = Path("/")) -> None:
    files = stage / "files"
    meta = stage / "orin-updater"
    R = lambda p: root / p.lstrip("/")  # noqa: E731

    keys = sorted((files / "etc/ssh").glob("ssh_host_*")) if (files / "etc/ssh").is_dir() else []
    for k in keys:
        install_file(k, R(f"/etc/ssh/{k.name}"), 0o644 if k.name.endswith(".pub") else 0o600)
    if keys:
        log(f"* SSH host keys restored ({len(keys)} files)")

    hn = files / "etc/hostname"
    if hn.is_file():
        hostname = hn.read_text().strip()
        R("/etc/hostname").write_text(hostname + "\n")
        old_hosts = (files / "etc/hosts").read_text() if (files / "etc/hosts").is_file() else ""
        cur = R("/etc/hosts").read_text() if R("/etc/hosts").is_file() else "127.0.0.1\tlocalhost\n"
        R("/etc/hosts").write_text(merge_hosts(old_hosts, cur, hostname))
        log(f"* Hostname: {hostname}")

    nm = files / "etc/NetworkManager/system-connections"
    if nm.is_dir():
        for c in sorted(nm.iterdir()):
            install_file(c, R(f"/etc/NetworkManager/system-connections/{c.name}"), 0o600)
        log(f"* NetworkManager connections: {len(list(nm.iterdir()))}")

    for rel in ("etc/timezone", "etc/default/locale", "etc/default/keyboard"):
        if (files / rel).is_file():
            install_file(files / rel, R("/" + rel), 0o644)
    if (meta / "localtime").is_file():
        target = (meta / "localtime").read_text().strip()
        if R(target).exists() or root != Path("/"):
            lt = R("/etc/localtime")
            if lt.is_symlink() or lt.exists():
                lt.unlink()
            lt.symlink_to(target)
            log(f"* Timezone: {target}")

    sud = files / "etc/sudoers.d"
    if sud.is_dir():
        for s in sorted(sud.iterdir()):
            if s.name == "README":
                continue
            if shutil.which("visudo") and run(["visudo", "-cf", str(s)], check=False).returncode:
                log(f"  sudoers.d/{s.name} is invalid, skipped")
                continue
            install_file(s, R(f"/etc/sudoers.d/{s.name}"), 0o440)
            log(f"* sudoers.d/{s.name}")

    cron = files / "var/spool/cron/crontabs" / ident["name"]
    if cron.is_file():
        g = group_exists("crontab")
        install_file(cron, R(f"/var/spool/cron/crontabs/{ident['name']}"), 0o600,
                     ident["uid"], g.gr_gid if g else 0)
        log("* crontab restored")

    old_fstab = files / "etc/fstab"
    if old_fstab.is_file() and R("/etc/fstab").is_file():
        merged, added = merge_fstab(old_fstab.read_text(), R("/etc/fstab").read_text())
        if added:
            shutil.copy2(R("/etc/fstab"), R("/etc/fstab.orin-updater.bak"))
            R("/etc/fstab").write_text(merged)
            for mp in added:
                if mp.startswith("/"):
                    R(mp).mkdir(parents=True, exist_ok=True)
            log(f"* fstab: added {', '.join(added)} (nofail)")

    inv = meta / "inventory"
    if inv.is_dir():
        dest = R(ident["home"]) / "orin-updater-inventory"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(inv, dest)
        for p in [dest, *dest.rglob("*")]:
            os.chown(p, ident["uid"], ident["gid"])
        log(f"* Inventory + reinstall.sh -> {dest}")


# --------------------------------------------------------------------------- finalize

def mark_initial_setup_done(ident: dict) -> None:
    """Keep GNOME Initial Setup from greeting a restored user as a new one."""
    cfg = Path(ident["home"]) / ".config"
    done = cfg / "gnome-initial-setup-done"
    if not done.exists():
        cfg.mkdir(exist_ok=True)
        os.chown(cfg, ident["uid"], ident["gid"])
        done.write_text("yes")
        os.chown(done, ident["uid"], ident["gid"])


def finalize(reboot: bool) -> None:
    """Leave NVIDIA's first-boot mode (nv-oobe / nv-oem-config) for good, like its setup
    would after creating a user: boot into the desktop from now on."""
    current = run(["systemctl", "get-default"], check=False).stdout.strip()
    if current in FIRSTBOOT_TARGETS or not current:
        run(["systemctl", "set-default", "graphical.target"], check=False)
    for unit in OEM_UNITS:
        run(["systemctl", "disable", unit], check=False)
    if PENDING.exists():
        PENDING.unlink()
    log("Restore complete." + (" Rebooting into the restored system." if reboot else ""))
    if reboot:
        time.sleep(3)
        run(["systemctl", "--no-block", "reboot"], check=False)


def give_up(reason: str) -> None:
    log(f"!! {reason}")
    log("!! Falling back to NVIDIA's normal first-boot setup. Your backup is untouched;")
    log("!! you can retry later with: sudo /usr/local/lib/orin-updater/orin_restore.py")
    if PENDING.exists():
        PENDING.unlink()


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    global _log_fh
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--backup-dir", help="directory containing manifest.json (default: find "
                    f"the {LABEL} partition)")
    ap.add_argument("--passphrase-file", help="read the passphrase from this file (testing)")
    ap.add_argument("--firstboot", action="store_true", help="called by orin-restore.service")
    ap.add_argument("--no-finalize", action="store_true",
                    help="do not touch systemd targets / oem-config / reboot")
    ap.add_argument("--skip-home", action="store_true", help="only restore account + system files")
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("orin-restore must run as root", file=sys.stderr)
        return 2
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _log_fh = open(LOG_PATH, "a")
        os.chmod(LOG_PATH, 0o600)
    except OSError:
        _log_fh = None

    if args.firstboot and not PENDING.exists():
        log("nothing pending, exiting")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="orin-restore-"))
    os.chmod(tmp, 0o700)
    try:
        backup = find_backup(args.backup_dir)
        manifest = json.loads((backup / "manifest.json").read_text())
        user = manifest["user"]["name"]
        log(f"Backup of {user}@{manifest['source']['hostname']} "
            f"({manifest['source'].get('l4t')}, {manifest['created']}) found in {backup}")
        for tool in ("gpg", "zstd", "tar"):
            if not shutil.which(tool):
                raise RestoreError(f"'{tool}' is not installed on this system")

        stage = tmp / "stage"
        sys_part = manifest["system_archive"]
        for attempt in (1, 2, 3):
            if args.passphrase_file:
                passphrase = Path(args.passphrase_file).read_text().rstrip("\n")
            else:
                passphrase = ask_passphrase(user, attempt)
            if not passphrase:
                raise RestoreError("skipped by user")
            stage.mkdir(exist_ok=True)
            try:
                decrypt_to_tar([(backup / sys_part["file"], sys_part["sha256"])], passphrase,
                               ["tar", "-C", str(stage), "-xpf", "-", "--numeric-owner"], tmp)
                break
            except BadPassphrase:
                log("wrong passphrase")
                shutil.rmtree(stage, ignore_errors=True)
                if args.passphrase_file or attempt == 3:
                    raise RestoreError("wrong passphrase") from None
        ident = json.loads((stage / "orin-updater/identity.json").read_text())

        apply_identity(ident)
        if not args.skip_home:
            parts = [(backup / p["file"], p["sha256"]) for p in manifest["home_parts"]]
            size = sum(p["size"] for p in manifest["home_parts"])
            log(f"* Restoring {ident['home']} ({size / 1024**3:.1f} GiB encrypted) ...")
            decrypt_to_tar(parts, passphrase, TAR_EXTRACT, tmp, progress=True)
            os.chown(ident["home"], ident["uid"], ident["gid"])
        apply_system(stage, ident)
        mark_initial_setup_done(ident)
        if args.no_finalize:
            if PENDING.exists():
                PENDING.unlink()
            log("Restore complete (--no-finalize).")
        else:
            finalize(reboot=args.firstboot)
        return 0
    except (RestoreError, OSError, KeyError, json.JSONDecodeError) as exc:
        if args.firstboot:
            give_up(str(exc))
        else:
            log(f"!! {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.ismount(MOUNTPOINT):
            run(["umount", str(MOUNTPOINT)], check=False)


if __name__ == "__main__":
    sys.exit(main())
