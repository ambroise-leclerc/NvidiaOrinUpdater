# Changelog

## Unreleased

An unfinished backup can no longer pass for a good one. This came from a real run where the
Jetson froze during `backup --usb`, leaving four partial home parts and no manifest on the key.

- `backup` writes an `INCOMPLETE` marker as soon as it starts and removes it only after the
  manifest is written and the archives are verified. A crash, a power cut or Ctrl-C leaves it.
- A rerun on a directory that carries the marker discards the unfinished attempt by itself
  (it only touches files this tool creates). Without the marker it still refuses a non-empty
  directory.
- Every finished part is `fsync`ed, and the medium is synced before the marker is removed.
- `backup` logs the volume written and the speed every 10 s while archiving the home.
- New `verify-backup (--usb | --dir DIR)`: checks marker, manifest, size and sha256 of every
  part without the passphrase. Run it before rebooting into the installer.
- The restore (first boot or `restore`) refuses a backup directory marked `INCOMPLETE`, even if
  a `manifest.json` is present.
- On failure `backup` now says plainly not to start the installation from that key.

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
