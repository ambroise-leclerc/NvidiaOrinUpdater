# Jetson ISO internals (r39.2.1 / JetPack 7.2.1)

Notes from analysing `jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso`
(sha256 `f31cf350…95d0`, 5 035 601 920 bytes). Everything here can be re-checked with
`orin-updater inspect-iso -v <iso>`, `xorriso`, `unsquashfs` and `lsinitramfs`.

## Boot layout

* ISO 9660 + Rock Ridge, volume id `jetsoninstaller-r39.2.1`, label "Ubuntu-Server 24.04.3 LTS".
* Hybrid image: protective MBR + GPT. Partition 1 `Gap0` covers the ISO 9660 tree,
  partition 2 `Appended2` is a 512 MiB EFI System Partition (EF00) holding
  `/efi/boot/bootaa64.efi` (shim) + GRUB. Partition 3 `Gap1` is 600 sectors of padding.
* El Torito: one UEFI no-emulation entry pointing at the start of partition 2.
* `xorriso -indev orig -outdev new -map ... -boot_image any replay` keeps all of this
  (it drops the trailing `Gap1` padding, which is harmless). `orin-updater patch-iso`
  verifies El Torito, the GPT layout and the ESP bytes after patching.

## GRUB (`/boot/grub/grub.cfg`)

A submenu "Install Jetson ISO r39.2.1" with entries *Install on NVMe / USB / eMMC / microSD*,
*Boot Into Rescue Shell* and *Check Disc for Defects*, all booting
`/casper/Image autoinstall …` with `/casper/initrd`. The NVMe entry passes
`force-bootdisk=nvme0n1`: **the target disk is wiped without further questions.**

## casper layers

`/casper/` holds layered squashfs images; casper stacks the chain named by `LAYERFS_PATH`,
whose default lives in the initrd (`conf/conf.d/default-layer.conf`):

```
ubuntu-server-minimal.squashfs                                   (target system base)
└─ .ubuntu-server.squashfs                                       (target system, layered)
   └─ .installer.squashfs                                        (live installer env)
      ├─ .installer.generic-hwe.squashfs
      └─ .installer.kernel.squashfs                              (tegra 6.8 kernel)
         └─ .installer.kernel.nvidia.squashfs   <- LAYERFS_PATH  (NVIDIA tooling, /ai)
```

The `layerfs-path=<file>` kernel parameter overrides the default. casper derives the parent
chain by stripping the last dotted component, so a file named
`…installer.kernel.nvidia.orinupdater.squashfs` is stacked **on top of** NVIDIA's layers.

## Autoinstall flow

1. The NVIDIA layer's `/autoinstall.yaml` only has an early-command:
   `bash /ai/preseed.sh jetsoniso`.
2. `preseed.sh` parses `/proc/cmdline` (`force-bootdisk=`, `nooemconfig`, `offwhendone`,
   `oem-iso-cfg`, …), detects the board (`/ai/jetsoniso_setup.bash`), runs `sed`
   substitutions (`CHANGE_*` placeholders) on `/ai/jetsoniso_<family>-ai.yaml`
   (`orin` or `thor`), may **append** extra late-commands with `>>`, and copies the result to
   `/autoinstall.yaml`; subiquity then reloads it.
3. The Orin `late-commands` (13 steps) bind-mount `/cdrom` into the target, install ~2000
   packages from the ISO's `/pool` (incl. `ubuntu-desktop`, `nvidia-l4t-*`), replace GRUB with
   L4TLauncher, then set up the first-boot experience (below).

`late-commands` is the last key of the ai.yaml (preseed appends to it), which is what makes the
textual append done by orin-updater safe; it is validated with a YAML parser.

`oem-iso-cfg` would run `/cdrom/oemdata/oem-iso-cfg.sh` *inside* the target chroot, where
`/cdrom` is not mounted (NVIDIA itself bind-mounts it to `/target/tmp/cdrom`), so orin-updater
does not rely on it.

## First boot (package `nvidia-l4t-oobe`)

* The late-command sets `default.target -> nv-oobe.target` and touches `/etc/nv/nvautoconfig`.
* `nv-oobe.service` (after `nv-load-display-modules.service`) runs `/etc/systemd/nv-oobe.sh`:
  with a display connected it switches the default to `graphical.target` and GDM runs
  **GNOME Initial Setup** (user creation); headless, it runs the debconf oem-config on the
  serial / USB gadget console (`nv-oem-config-debconf@ttyGS0`).
* With `nooemconfig` on the cmdline, a default user `nvidia`/`nvidia` is created instead.

orin-updater's `orin-restore.service` is ordered after the display modules and before
`nv-oobe.service` / GDM. On success it sets `graphical.target` as default and reboots; GDM then
shows the restored account. On failure it does nothing and NVIDIA's setup runs as usual.

## Target system

Ubuntu 24.04 (noble), kernel `6.8.12-tegra`. Includes `python3`, `gpg`, `zstd`, `tar`,
`openssh-server`, `network-manager`, `cron`, `docker.io`, `gdm3`, `gnome-initial-setup`, which
is everything `orin_restore.py` needs.
