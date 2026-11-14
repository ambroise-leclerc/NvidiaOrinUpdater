import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from orin_updater import iso, preflight, usb
from orin_updater.config import Config
from orin_updater.inventory import parse_snap_list, render_reinstall

# --------------------------------------------------------------------------- preflight


def test_parse_nv_tegra_release():
    text = "# R36 (release), REVISION: 4.7, GCID: 42132812, BOARD: generic, EABI: aarch64"
    assert preflight.parse_nv_tegra_release(text) == (36, "4.7")
    assert preflight.parse_nv_tegra_release("garbage") is None


def test_parse_firmware_version():
    out = "Current version: 36.4.7\nCapsule update status: 0\nCurrent bootloader slot: A\n"
    assert preflight.parse_firmware_version(out) == (36, 4, 7)
    assert preflight.parse_firmware_version(out) >= preflight.MIN_FIRMWARE
    assert preflight.parse_firmware_version("Current version: 35.5.0") < preflight.MIN_FIRMWARE
    assert preflight.parse_bios_version("36.4.7-gcid-42132812\n") == (36, 4, 7)
    assert preflight.parse_bios_version("") is None


def test_excludes_and_size(tmp_path):
    cfg = Config()
    assert preflight.is_excluded(".cache/pip/x", cfg)
    assert preflight.is_excluded("src/pkg/__pycache__", cfg)
    assert preflight.is_excluded("snap/firefox/common/.cache/a", cfg)
    assert not preflight.is_excluded(".ssh/id_ed25519", cfg)
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache/big").write_bytes(b"x" * 100_000)
    (tmp_path / "code").mkdir()
    (tmp_path / "code/main.c").write_bytes(b"y" * 50_000)
    inc, exc = preflight.estimate_home_size(tmp_path, cfg)
    assert inc >= 50_000 and exc >= 100_000


def test_config_load(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("extra_home_excludes: [models]\nsystem_items: [network]\nchunk_size_mib: 10\n")
    cfg = Config.load(f)
    assert "models" in cfg.home_excludes and ".cache" in cfg.home_excludes
    assert cfg.system_items == ["network"]
    assert cfg.chunk_size == 10 * 1024**2
    assert "--exclude=home/bob/models" in cfg.tar_excludes("home/bob")
    f.write_text("system_items: [bogus]\n")
    with pytest.raises(ValueError):
        Config.load(f)


# --------------------------------------------------------------------------- iso


# Same shape as NVIDIA's /ai/jetsoniso_orin-ai.yaml (r39.2.1): placeholders, comments,
# block scalars, and late-commands as the last key (preseed.sh appends to it with >>).
AI_YAML = """version: 1
storage:
  config:
#CHANGE_STORAGE_DO_NVME  - id: CHANGE_BOOT_DISK_NAME_1
#CHANGE_STORAGE_DO_NVME    type: disk
  - type: dummy
user-data:
  runcmd:
    - [ echo, "done" ]

late-commands:
  - logger -p info "Step 1/13"
  - |
    cat << EOF > /target/etc/nv_boot_control.conf
    TNSPEC CHANGE_TNSPEC
    EOF
  - curtin in-target -- /bin/bash -c "if CHANGE_DOOEMCONFIG; then
        touch /etc/nv/nvautoconfig;
      fi"
  - logger -p info "100% Step 13/13 Finished installation"
"""

GRUB = """set default=0
submenu "Install Jetson ISO r39.2.1" {
    menuentry "Install on NVMe" {
        linux /casper/Image autoinstall fsck.mode=skip force-bootdisk=nvme0n1 loglevel=debug
        initrd /casper/initrd
    }
    menuentry "Install on USB" {
        linux /casper/Image autoinstall fsck.mode=skip loglevel=debug
        initrd /casper/initrd
    }
    menuentry "Boot Into Rescue Shell" {
        linux /casper/Image autoinstall boot-live-env systemd.unit=rescue.target
        initrd /casper/initrd
    }
}
menuentry 'UEFI Firmware Settings' {
    fwsetup
}
"""


def test_append_late_command_keeps_nvidia_text():
    out = iso.append_late_command(AI_YAML)
    assert out.startswith(AI_YAML.rstrip("\n"))  # NVIDIA's text untouched (sed placeholders)
    cmds = yaml.safe_load(out)["late-commands"]
    assert len(cmds) == 5 and cmds[-1] == iso.late_command()
    assert iso.append_late_command(out) == out  # idempotent
    # preseed.sh appends more commands with >> afterwards: still inside late-commands
    more = yaml.safe_load(out + "  - poweroff\n")["late-commands"]
    assert more[-2:] == [iso.late_command(), "poweroff"]


def test_append_late_command_rejects_unknown_layout():
    with pytest.raises(iso.IsoError):
        iso.append_late_command("late-commands:\n  - a\nuser-data: {}\n")


def test_late_command_never_fails_install():
    assert iso.late_command().endswith("|| true")
    assert iso.late_command().startswith("sh /cdrom/orin-updater/")


def test_patch_grub():
    layer = "x.installer.kernel.nvidia.orinupdater.squashfs"
    out = iso.patch_grub(GRUB, layer)
    lines = out.splitlines()
    linux = [ln for ln in lines if ln.strip().startswith("linux")]
    assert linux[0].endswith(f"force-bootdisk=nvme0n1 loglevel=debug layerfs-path={layer}")
    assert linux[1].endswith(f"layerfs-path={layer}")
    assert "layerfs-path" not in linux[2]  # rescue shell untouched
    assert iso.GRUB_TITLE_SUFFIX in lines[1]
    assert iso.patch_grub(out, layer) == out  # idempotent
    with pytest.raises(iso.IsoError):
        iso.patch_grub("menuentry 'x' {\n linux /k\n}\n", layer)


def test_leaf_layer():
    base = "/casper/ubuntu-server-minimal"
    files = [f"{base}.squashfs", f"{base}.ubuntu-server.squashfs",
             f"{base}.ubuntu-server.installer.squashfs",
             f"{base}.ubuntu-server.installer.generic-hwe.squashfs",
             f"{base}.ubuntu-server.installer.kernel.squashfs",
             f"{base}.ubuntu-server.installer.kernel.nvidia.squashfs", "/casper/initrd"]
    assert iso.leaf_layer(files) == "ubuntu-server-minimal.ubuntu-server.installer.kernel.nvidia"
    files.append(f"{base}.ubuntu-server.installer.kernel.nvidia.orinupdater.squashfs")
    assert iso.leaf_layer(files) == "ubuntu-server-minimal.ubuntu-server.installer.kernel.nvidia"


def test_patch_md5sums(tmp_path):
    f = tmp_path / "g"
    f.write_text("new")
    text = "aaaa  ./boot/grub/grub.cfg\nbbbb  ./casper/initrd\n"
    out = iso.patch_md5sums(text, {"/boot/grub/grub.cfg": f, "/orin-updater/x": f})
    assert "bbbb  ./casper/initrd" in out
    assert f"{iso.md5(f)}  ./boot/grub/grub.cfg" in out
    assert out.strip().endswith(f"{iso.md5(f)}  ./orin-updater/x")


def test_payload_files_exist():
    for name, path in iso.payload_files().items():
        assert path.is_file(), name


# --------------------------------------------------------------------------- usb

LSBLK = [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 1000204886016, "tran": "nvme",
     "rm": False, "type": "disk", "model": "WD_BLACK SN7100 1TB", "mountpoint": None,
     "children": [{"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "type": "part",
                   "mountpoint": "/"}]},
    {"name": "sda", "path": "/dev/sda", "size": 128043712512, "tran": "usb", "rm": True,
     "type": "disk", "model": "Ultra", "mountpoint": None,
     "children": [{"name": "sda1", "path": "/dev/sda1", "type": "part",
                   "mountpoint": "/media/bob/KEY"}]},
]


def test_usb_guard_rails():
    nvme = usb.get_disk("/dev/nvme0n1", LSBLK)
    problems = usb.validate_target(nvme, 10)
    assert any("not a USB" in p for p in problems)
    assert any("system mounts" in p for p in problems)
    key = usb.get_disk("/dev/sda", LSBLK)
    assert key.mountpoints == ["/media/bob/KEY"]
    assert usb.validate_target(key, 64 * 10**9) == []
    assert any("too small" in p for p in usb.validate_target(key, 256 * 10**9))
    with pytest.raises(usb.UsbError):
        usb.get_disk("/dev/sda1", LSBLK)


# --------------------------------------------------------------------------- inventory

SNAP_LIST = """Name      Version   Rev   Tracking       Publisher   Notes
bare      1.0       5     latest/stable  canonical✓  base
code      1.95      200   latest/stable  vscode✓     classic
core22    2024      2194  latest/stable  canonical✓  base
glab      1.50      4807  latest/stable  gitlab      -
snapd     2.66      25585 latest/stable  canonical✓  snapd
"""


def test_inventory_render():
    assert parse_snap_list(SNAP_LIST) == [("code", "latest/stable"), ("glab", "latest/stable")]
    script = render_reinstall({
        "apt-manual.txt": "git\nnvidia-jetpack\ncuda-toolkit-12-6\nhtop\n",
        "snap-list.txt": SNAP_LIST,
        "pip-user.txt": "rich==13.0\n",
    })
    assert "# sudo apt install -y git" in script and "# sudo apt install -y htop" in script
    assert "\n# sudo apt install -y nvidia-jetpack\n" not in script
    assert "Skipped" in script and "cuda-toolkit-12-6" in script
    assert "# sudo snap install code --channel=latest/stable --classic" in script
    assert "# sudo snap install glab --channel=latest/stable\n" in script
    assert all(ln.startswith("#") or not ln.strip() or ln in ("set -e",)
               for ln in script.splitlines()), "nothing must run without review"


# --------------------------------------------------------------------------- restore helpers


def test_merge_fstab(restore_mod):
    old = ("UUID=old / ext4 defaults 0 1\n"
           "UUID=efi /boot/efi vfat defaults 0 1\n"
           "/swapfile none swap sw 0 0\n"
           "nas:/data /mnt/nas nfs defaults,_netdev 0 0\n"
           "UUID=ssd /data ext4 nofail 0 2\n")
    new = "# new\nUUID=new / ext4 defaults 0 1\n"
    merged, added = restore_mod.merge_fstab(old, new)
    assert added == ["/mnt/nas", "/data"]
    assert "UUID=old" not in merged and "/swapfile" not in merged
    assert "defaults,_netdev,nofail" in merged
    assert sum("nofail" in ln for ln in merged.splitlines() if not ln.startswith("#")) == 2
    assert restore_mod.merge_fstab(old, merged)[1] == []


def test_merge_hosts(restore_mod):
    new = "127.0.0.1\tlocalhost\n127.0.1.1\tubuntu\n::1 ip6-localhost\n"
    old = "127.0.0.1 localhost\n127.0.1.1 orin\n192.168.1.10 nas\n# comment\n"
    out = restore_mod.merge_hosts(old, new, "orin")
    assert "127.0.1.1\torin" in out and "ubuntu" not in out
    assert "192.168.1.10 nas" in out and "# comment" not in out


def test_apply_system_fake_root(restore_mod, tmp_path):
    stage = tmp_path / "stage"
    files = stage / "files"
    (files / "etc/ssh").mkdir(parents=True)
    (files / "etc/ssh/ssh_host_ed25519_key").write_text("PRIVATE")
    (files / "etc/ssh/ssh_host_ed25519_key.pub").write_text("PUBLIC")
    (files / "etc/hostname").write_text("orin\n")
    (files / "etc/NetworkManager/system-connections").mkdir(parents=True)
    (files / "etc/NetworkManager/system-connections/home.nmconnection").write_text("[wifi]")
    (stage / "orin-updater/inventory").mkdir(parents=True)
    (stage / "orin-updater/inventory/reinstall.sh").write_text("#!/bin/sh\n")
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n")
    home = root / "home/bob"
    home.mkdir(parents=True)
    ident = {"name": "bob", "uid": os.getuid(), "gid": os.getgid(), "home": "/home/bob"}

    orig_install = restore_mod.install_file

    def install_noroot(src, dst, mode, uid=0, gid=0):  # chown to root needs root
        return orig_install(src, dst, mode, os.getuid(), os.getgid())

    restore_mod.install_file = install_noroot
    try:
        restore_mod.apply_system(stage, ident, root=root)
    finally:
        restore_mod.install_file = orig_install
    key = root / "etc/ssh/ssh_host_ed25519_key"
    assert key.read_text() == "PRIVATE" and oct(key.stat().st_mode & 0o777) == "0o600"
    assert oct((root / "etc/ssh/ssh_host_ed25519_key.pub").stat().st_mode & 0o777) == "0o644"
    assert (root / "etc/hostname").read_text() == "orin\n"
    assert "127.0.1.1\torin" in (root / "etc/hosts").read_text()
    nm = root / "etc/NetworkManager/system-connections/home.nmconnection"
    assert oct(nm.stat().st_mode & 0o777) == "0o600"
    assert (home / "orin-updater-inventory/reinstall.sh").is_file()


# --------------------------------------------------------------------------- crypto round-trip

needs_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("gpg", "zstd", "tar")), reason="gpg/zstd/tar missing")


@needs_tools
def test_encrypt_split_decrypt_roundtrip(tmp_path, restore_mod):
    from orin_updater.backup import encrypt_stream, verify_archive
    src = tmp_path / "src"
    (src / "home/bob/.ssh").mkdir(parents=True)
    (src / "home/bob/.ssh/id_ed25519").write_text("secret-key")
    os.chmod(src / "home/bob/.ssh/id_ed25519", 0o600)
    (src / "home/bob/blob").write_bytes(os.urandom(300_000))  # incompressible -> several parts
    out = tmp_path / "out"
    out.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    parts = encrypt_stream(["tar", "-C", str(src), "-cpf", "-", "home"], out / "home.tar.zst.gpg.",
                           "correct horse", work, chunk_size=100_000)
    assert len(parts) >= 3
    assert all(p["size"] <= 100_000 for p in parts)
    assert verify_archive([out / p["file"] for p in parts], "correct horse", work) >= 4

    dest = tmp_path / "dest"
    dest.mkdir()
    plist = [(out / p["file"], p["sha256"]) for p in parts]
    restore_mod.decrypt_to_tar(plist, "correct horse", ["tar", "-C", str(dest), "-xpf", "-"], work)
    assert (dest / "home/bob/.ssh/id_ed25519").read_text() == "secret-key"
    assert oct((dest / "home/bob/.ssh/id_ed25519").stat().st_mode & 0o777) == "0o600"
    assert (dest / "home/bob/blob").read_bytes() == (src / "home/bob/blob").read_bytes()

    with pytest.raises(restore_mod.BadPassphrase):
        restore_mod.decrypt_to_tar(plist, "wrong", ["tar", "-C", str(dest), "-xpf", "-"], work)

    corrupted = out / parts[1]["file"]
    data = bytearray(corrupted.read_bytes())
    data[100] ^= 0xFF
    corrupted.write_bytes(bytes(data))
    with pytest.raises(restore_mod.RestoreError, match="checksum"):
        restore_mod.decrypt_to_tar(plist, "correct horse",
                                   ["tar", "-C", str(dest), "-xpf", "-"], work)


def test_find_backup_explicit(restore_mod, tmp_path):
    d = tmp_path / "key" / "orin-backup"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{}")
    assert restore_mod.find_backup(str(tmp_path / "key")) == d
    with pytest.raises(restore_mod.RestoreError):
        restore_mod.find_backup(str(tmp_path))


def test_shell_payload_syntax():
    path = Path(iso.payload_files()["late-command.sh"])
    assert os.system(f"sh -n {path}") == 0


def test_late_command_installs_service(tmp_path):
    target = tmp_path / "target"
    (target / "etc/systemd/system").mkdir(parents=True)
    (target / "usr/bin").mkdir(parents=True)
    for tool in ("python3", "gpg", "zstd", "tar"):
        (target / "usr/bin" / tool).write_text("")
        (target / "usr/bin" / tool).chmod(0o755)
    script = iso.payload_files()["late-command.sh"]
    assert subprocess.run(["sh", str(script), str(target)]).returncode == 0
    unit = target / "etc/systemd/system/orin-restore.service"
    assert unit.is_file()
    for t in ("nv-oobe.target", "graphical.target", "multi-user.target"):
        link = target / f"etc/systemd/system/{t}.wants/orin-restore.service"
        assert os.readlink(link) == "../orin-restore.service"
    assert (target / "usr/local/lib/orin-updater/orin_restore.py").stat().st_mode & 0o111
    assert (target / "var/lib/orin-updater/pending").is_file()
    log = (target / "var/log/orin-updater-install.log").read_text()
    assert "installed" in log and "WARNING" not in log


def test_late_command_never_breaks_install(tmp_path):
    target = tmp_path / "target"  # no systemd: must log, clean up and still exit 0
    target.mkdir()
    script = iso.payload_files()["late-command.sh"]
    assert subprocess.run(["sh", str(script), str(target)]).returncode == 0
    assert not (target / "var/lib/orin-updater/pending").exists()
    assert "FAILED" in (target / "var/log/orin-updater-install.log").read_text()
