# Changelog

## 0.1.0 — 2026-11-14

First release.

- `check`: board, L4T, UEFI/QSPI firmware (≥ 36.0), tools, backup size estimate.
- `backup`: gpg-encrypted (AES-256), zstd-compressed, split archive of the account, home and
  selected system configuration, with a plaintext manifest and sha256 sums, verified after
  creation.
- `fetch-iso`, `inspect-iso`, `patch-iso` for the JetPack 7.2.1 Jetson ISO (r39.2.1): adds an
  extra casper squashfs layer with one extra late-command, plus a `layerfs-path=` in GRUB.
  It verifies that El Torito, the GPT layout and the ESP are unchanged.
- `make-usb`: writes the ISO and adds an `ORINBAK` ext4 partition, with guard rails.
- First-boot `orin-restore.service`: asks for the passphrase on tty1, recreates the account,
  restores home and system files, then leaves NVIDIA's `nv-oobe` setup. On any failure it falls
  back to that setup.
- `inventory`: software inventory and a commented, reviewable `reinstall.sh`.
