"""Encrypted backup of a user account, its home and selected system configuration.

Output directory layout (see __init__ constants):
    INCOMPLETE                present while a backup is being written; removed only once the
                              manifest is written and the archives are verified
    manifest.json             plaintext, non-sensitive: who/what/where + sha256 of every part
    system.tar.zst.gpg        identity (incl. password hash), system files, inventory
    home.tar.zst.gpg.000 ...  the home directory, split in parts
"""

from __future__ import annotations

import glob
import grp
import hashlib
import json
import os
import pwd
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import (
    BACKUP_LABEL,
    FORMAT_VERSION,
    HOME_ARCHIVE_PREFIX,
    INCOMPLETE_NAME,
    MANIFEST_NAME,
    SUMS_NAME,
    SYSTEM_ARCHIVE,
    __version__,
    inventory,
)
from .config import SYSTEM_FILES, Config
from .preflight import parse_nv_tegra_release

GPG_ENCRYPT = [
    "gpg", "--batch", "--yes", "--no-tty", "--quiet", "--pinentry-mode", "loopback",
    "--symmetric", "--cipher-algo", "AES256", "--compress-algo", "none",
    "--s2k-mode", "3", "--s2k-digest-algo", "SHA512", "--s2k-count", "65011712",
]
GPG_DECRYPT = ["gpg", "--batch", "--no-tty", "--quiet", "--pinentry-mode", "loopback", "--decrypt"]
TAR_CREATE = ["tar", "-C", "/", "-cpf", "-", "--numeric-owner", "--xattrs", "--acls",
              "--sparse", "--warning=no-file-ignored", "--warning=no-file-changed"]


class BackupError(RuntimeError):
    pass


@dataclass
class Identity:
    name: str
    uid: int
    gid: int
    group: str
    gecos: str
    home: str
    shell: str
    password_hash: str
    groups: list[dict] = field(default_factory=list)  # [{"name":..., "gid":...}]


def collect_identity(user: str, shadow_path: str = "/etc/shadow") -> Identity:
    pw = pwd.getpwnam(user)
    primary = grp.getgrgid(pw.pw_gid).gr_name
    groups = sorted(
        ({"name": g.gr_name, "gid": g.gr_gid} for g in grp.getgrall()
         if user in g.gr_mem and g.gr_gid != pw.pw_gid),
        key=lambda g: g["name"],
    )
    return Identity(
        name=user, uid=pw.pw_uid, gid=pw.pw_gid, group=primary, gecos=pw.pw_gecos,
        home=pw.pw_dir, shell=pw.pw_shell, password_hash=read_shadow_hash(user, shadow_path),
        groups=groups,
    )


def read_shadow_hash(user: str, shadow_path: str = "/etc/shadow") -> str:
    try:
        with open(shadow_path) as fh:
            for line in fh:
                fields = line.rstrip("\n").split(":")
                if fields[0] == user:
                    return fields[1]
    except PermissionError as exc:
        raise BackupError("cannot read /etc/shadow: run the backup with sudo") from exc
    raise BackupError(f"user {user!r} not found in {shadow_path}")


def stage_system(stage: Path, ident: Identity, cfg: Config) -> list[str]:
    """Copy identity, selected system files and inventory into `stage`. Returns copied paths."""
    meta = stage / "orin-updater"
    meta.mkdir(parents=True)
    (meta / "identity.json").write_text(json.dumps(asdict(ident), indent=2))
    copied = []
    patterns = [p for item in cfg.system_items for p in SYSTEM_FILES[item]]
    patterns.append(f"/var/spool/cron/crontabs/{ident.name}")
    for pat in patterns:
        for src in sorted(glob.glob(pat)):
            if not os.path.isfile(src):  # skips directories and dangling symlinks
                continue
            dst = stage / "files" / src.lstrip("/")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=True)
            copied.append(src)
    # /etc/localtime is a symlink: record its target instead of copying the zone file.
    if os.path.islink("/etc/localtime"):
        (meta / "localtime").write_text(os.readlink("/etc/localtime"))
    inventory.collect(meta / "inventory", ident.name)
    return copied


def _gpg_env(tmp: Path) -> dict:
    env = dict(os.environ)
    home = tmp / "gnupg"
    home.mkdir(mode=0o700, exist_ok=True)
    env["GNUPGHOME"] = str(home)
    return env


def encrypt_stream(cmd: list[str], out_prefix: Path, passphrase: str, tmp: Path,
                   chunk_size: int | None, ok_codes=(0,), progress=None) -> list[dict]:
    """Run `cmd | zstd | gpg` and write the ciphertext to one file or numbered parts.

    Every finished part is fsync'ed. `progress(total_bytes_written)` is called after each
    1 MiB block when given (the caller throttles).

    Returns [{"file":..., "size":..., "sha256":...}].
    """
    r, w = os.pipe()
    os.write(w, passphrase.encode() + b"\n")
    os.close(w)
    src = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    zst = subprocess.Popen(["zstd", "-T0", "-3", "-q", "-c"], stdin=src.stdout,
                           stdout=subprocess.PIPE)
    src.stdout.close()
    gpg = subprocess.Popen([*GPG_ENCRYPT, "--passphrase-fd", str(r), "-o", "-"],
                           stdin=zst.stdout, stdout=subprocess.PIPE, pass_fds=(r,),
                           env=_gpg_env(tmp))
    zst.stdout.close()
    os.close(r)

    parts: list[dict] = []
    index = 0
    fh = None
    written = 0
    hasher = None
    total = 0

    def close_part():
        nonlocal fh
        if fh is not None:
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()
            parts[-1]["size"] = written
            parts[-1]["sha256"] = hasher.hexdigest()
            fh = None

    try:
        while True:
            block = gpg.stdout.read(1 << 20)
            if not block:
                break
            total += len(block)
            if progress:
                progress(total)
            view = memoryview(block)
            while view:
                if fh is None:
                    name = (f"{out_prefix.name}{index:03d}" if chunk_size else out_prefix.name)
                    fh = open(out_prefix.parent / name, "wb")
                    parts.append({"file": name})
                    written, hasher, index = 0, hashlib.sha256(), index + 1
                room = (chunk_size - written) if chunk_size else len(view)
                piece = view[:room]
                fh.write(piece)
                hasher.update(piece)
                written += len(piece)
                view = view[len(piece):]
                if chunk_size and written >= chunk_size:
                    close_part()
        close_part()
    finally:
        if fh is not None:
            fh.close()
        gpg.stdout.close()
    rc_gpg, rc_zst, rc_src = gpg.wait(), zst.wait(), src.wait()
    if rc_gpg != 0 or rc_zst != 0 or rc_src not in ok_codes:
        raise BackupError(f"archive pipeline failed: {cmd[0]}={rc_src} zstd={rc_zst} gpg={rc_gpg}")
    return parts


def verify_archive(files: list[Path], passphrase: str, tmp: Path) -> int:
    """Decrypt + decompress + list. Returns the number of entries."""
    r, w = os.pipe()
    os.write(w, passphrase.encode() + b"\n")
    os.close(w)
    cat = subprocess.Popen(["cat", *map(str, files)], stdout=subprocess.PIPE)
    gpg = subprocess.Popen([*GPG_DECRYPT, "--passphrase-fd", str(r)], stdin=cat.stdout,
                           stdout=subprocess.PIPE, pass_fds=(r,), env=_gpg_env(tmp),
                           stderr=subprocess.PIPE)
    cat.stdout.close()
    os.close(r)
    zst = subprocess.Popen(["zstd", "-d", "-q", "-c"], stdin=gpg.stdout, stdout=subprocess.PIPE)
    gpg.stdout.close()
    tar = subprocess.Popen(["tar", "-tf", "-"], stdin=zst.stdout, stdout=subprocess.PIPE)
    zst.stdout.close()
    count = sum(1 for _ in tar.stdout)
    rcs = (tar.wait(), zst.wait(), gpg.wait(), cat.wait())
    if any(rcs):
        err = gpg.stderr.read().decode(errors="replace").strip()
        raise BackupError(f"verification failed (tar,zstd,gpg,cat)={rcs} {err}")
    return count


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _fmt_gib(n: float) -> str:
    return f"{n / 1024**3:.2f} GiB"


def make_progress(label: str, log, every: float = 10.0):
    """Return a throttled progress callback that logs the volume written and the speed."""
    start = last = time.monotonic()

    def progress(total: int) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last >= every:
            last = now
            rate = total / max(now - start, 1e-6) / 1024**2
            log(f"  {label}: {_fmt_gib(total)} written ({rate:.0f} MiB/s)")

    return progress


def discard_incomplete(out_dir: Path, log=print) -> bool:
    """Remove what an interrupted run left behind. Only touches files this tool creates,
    and only when the INCOMPLETE marker proves the previous run never finished."""
    if not (out_dir / INCOMPLETE_NAME).exists():
        return False
    log(f"* Found an unfinished backup in {out_dir} (INCOMPLETE marker): discarding it")
    for f in [*out_dir.glob(HOME_ARCHIVE_PREFIX + "*"), out_dir / SYSTEM_ARCHIVE,
              out_dir / MANIFEST_NAME, out_dir / SUMS_NAME, out_dir / INCOMPLETE_NAME]:
        f.unlink(missing_ok=True)
    return True


def verify_backup_dir(out_dir: Path, log=print) -> list[str]:
    """Check a finished backup without the passphrase: marker, manifest, sizes, sha256.

    Returns a list of problems (empty = the backup is complete and uncorrupted).
    """
    if (out_dir / INCOMPLETE_NAME).exists():
        return [f"{INCOMPLETE_NAME} marker present: the backup never finished"]
    mf = out_dir / MANIFEST_NAME
    if not mf.is_file():
        return [f"no {MANIFEST_NAME}: the backup never finished"]
    try:
        manifest = json.loads(mf.read_text())
        parts = [manifest["system_archive"], *manifest["home_parts"]]
    except (ValueError, KeyError) as exc:
        return [f"unreadable {MANIFEST_NAME}: {exc}"]
    problems = []
    for p in parts:
        f = out_dir / p["file"]
        if not f.is_file():
            problems.append(f"{p['file']}: missing")
        elif f.stat().st_size != p["size"]:
            problems.append(f"{p['file']}: size {f.stat().st_size} != {p['size']}")
        else:
            log(f"  checking {p['file']} ({_fmt_gib(p['size'])}) ...")
            if sha256_file(f) != p["sha256"]:
                problems.append(f"{p['file']}: sha256 mismatch")
    return problems


def run_backup(user: str, out_dir: Path, passphrase: str, cfg: Config, verify: bool = True,
               log=print) -> dict:
    if os.geteuid() != 0:
        raise BackupError("the backup must run as root (sudo) to read /etc/shadow and system files")
    out_dir.mkdir(parents=True, exist_ok=True)
    discard_incomplete(out_dir, log)
    if any(out_dir.glob(HOME_ARCHIVE_PREFIX + "*")) or (out_dir / MANIFEST_NAME).exists():
        raise BackupError(f"{out_dir} already contains a backup; choose an empty directory")
    ident = collect_identity(user)
    # From here until the very end the directory is marked unusable. A crash, a power cut or
    # Ctrl-C leaves the marker behind, so nobody mistakes the partial parts for a backup.
    (out_dir / INCOMPLETE_NAME).write_text(
        f"backup started {time.strftime('%Y-%m-%dT%H:%M:%S%z')}; not finished or not verified\n")
    os.sync()
    home_rel = ident.home.strip("/")
    tmp = Path(tempfile.mkdtemp(prefix="orin-updater-"))
    os.chmod(tmp, 0o700)
    try:
        log(f"* Collecting account, system files and inventory for {user} ...")
        stage = tmp / "stage"
        copied = stage_system(stage, ident, cfg)
        sys_parts = encrypt_stream(
            ["tar", "-C", str(stage), "-cpf", "-", "--numeric-owner", "."],
            out_dir / SYSTEM_ARCHIVE, passphrase, tmp, chunk_size=None)
        log(f"  {len(copied)} system files saved")

        log(f"* Archiving /{home_rel} (this can take a while) ...")
        t0 = time.time()
        home_parts = encrypt_stream(
            [*TAR_CREATE, *cfg.tar_excludes(home_rel), home_rel],
            out_dir / HOME_ARCHIVE_PREFIX, passphrase, tmp, chunk_size=cfg.chunk_size,
            ok_codes=(0, 1),  # tar exits 1 when files changed while being read
            progress=make_progress("home", log))
        total = sum(p["size"] for p in home_parts)
        log(f"  {len(home_parts)} part(s), {total / 1024**3:.2f} GiB in {time.time() - t0:.0f}s")

        rel = parse_nv_tegra_release(Path("/etc/nv_tegra_release").read_text()) \
            if Path("/etc/nv_tegra_release").exists() else None
        manifest = {
            "format": FORMAT_VERSION,
            "tool_version": __version__,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": {
                "hostname": socket.gethostname(),
                "l4t": f"R{rel[0]}.{rel[1]}" if rel else None,
            },
            "user": {"name": ident.name, "uid": ident.uid, "home": ident.home},
            "label": BACKUP_LABEL,
            "encryption": "gpg-symmetric-aes256",
            "compression": "zstd",
            "system_archive": sys_parts[0],
            "home_parts": home_parts,
            "home_excludes": cfg.home_excludes + cfg.pattern_excludes,
            "system_items": cfg.system_items,
        }
        (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
        (out_dir / SUMS_NAME).write_text("".join(
            f"{p['sha256']}  {p['file']}\n" for p in [*sys_parts, *home_parts]))

        if verify:
            log("* Verifying (decrypt + list) ...")
            n = verify_archive([out_dir / SYSTEM_ARCHIVE], passphrase, tmp)
            m = verify_archive([out_dir / p["file"] for p in home_parts], passphrase, tmp)
            log(f"  system: {n} entries, home: {m} entries: OK")
        # Only now is the backup usable: flush it to the medium, then drop the marker.
        os.sync()
        (out_dir / INCOMPLETE_NAME).unlink(missing_ok=True)
        os.sync()
        return manifest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
