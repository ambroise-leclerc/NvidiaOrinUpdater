# NvidiaOrinUpdater

Upgrade a **Jetson Orin** (Nano / NX / AGX dev kits) from **JetPack 6** (L4T R36, Ubuntu 22.04)
to **JetPack 7** (L4T R39, Ubuntu 24.04) **without losing your user account**: password, SSH
keys, dotfiles, projects, Wi-Fi, hostname, crontab and the rest.

NVIDIA's only supported path to JetPack 7 is the USB *Jetson ISO* installer, which **erases the
whole target disk** and asks you to create a fresh user. That is painful on a development machine.
`orin-updater` wraps that installer:

1. **Backs up** your account and home directory into an encrypted archive (gpg AES-256).
2. **Patches NVIDIA's ISO**, without touching its kernel, firmware or packages. The patch adds a
   hook that installs a one-shot restore service into the new system.
3. **Writes a USB key** that holds both the installer and the encrypted backup.
4. **On first boot** of JetPack 7, asks for your passphrase and recreates your account (same
   uid/gid, groups, password hash). It restores your home and system settings, then reboots to
   your usual login screen.

If anything goes wrong (wrong passphrase three times, key missing, error), the restore steps
aside and NVIDIA's normal first-boot setup runs. Your backup on the key is never modified.

> Status: tested end to end in containers (backup on 22.04, restore on 24.04) and against
> the real r39.2.1 ISO (patch + boot-record verification). Read the "Risks" section first.

## What is preserved

| Item | How |
|---|---|
| User account: uid, gid, groups, shell, GECOS, **password hash** | recreated with `useradd`/`usermod -p` |
| Whole home directory (`~/.ssh`, `~/.gitconfig`, projects, …), permissions, xattrs, ACLs | `tar` → `zstd` → `gpg`, split into 4 GiB parts |
| SSH **host** keys (clients won't warn "host key changed") | `/etc/ssh/ssh_host_*` |
| Hostname, custom `/etc/hosts` entries | merged |
| Wi-Fi / NetworkManager connections | `/etc/NetworkManager/system-connections` |
| Timezone, locale, keyboard | `/etc/timezone`, `/etc/default/{locale,keyboard}` |
| `sudoers.d` rules (validated with `visudo -c`), user crontab | restored |
| Your extra `fstab` mounts (NAS, data disks) | merged, with `nofail` added |
| Software inventory | `~/orin-updater-inventory/reinstall.sh`, **commented out, for you to review** |

**Not preserved:** system packages (Ubuntu 22.04 → 24.04 and JetPack 6 → 7 differ, so reinstall
them from the generated `reinstall.sh`), Docker images and volumes (`/var/lib/docker`; use
`docker save` into your home), and anything else outside your home except the items above.
Caches (`~/.cache`, `__pycache__`, …) are excluded by default.

After the upgrade, rebuild anything compiled against the old system: Python venvs (Python 3.10
becomes 3.12), CUDA/TensorRT engines and out-of-tree modules.

## Requirements

* A Jetson Orin dev kit on JetPack 6 with **UEFI/QSPI firmware ≥ 36.0** (`orin-updater check`
  verifies this). Older firmware: follow NVIDIA's "JetPack 6.x Update Path" first.
* A **USB key** big enough for the ISO (~5 GB) plus your compressed home (`check` tells you).
  128 GB is comfortable for a ~70 GB home.
* A **monitor and keyboard** on the Jetson. You need them to confirm NVIDIA's firmware update
  prompt (30 s timeout), to pick the GRUB entry, and to type the passphrase at first boot.
* Packages: `sudo apt install xorriso squashfs-tools gdisk zstd gnupg parted rsync`

## Usage

```sh
git clone https://github.com/ambroise-leclerc/NvidiaOrinUpdater.git
cd NvidiaOrinUpdater            # ./orin-updater runs from the checkout, no install needed

./orin-updater check                       # 1. pre-flight checks + backup size
./orin-updater fetch-iso                   # 2. download NVIDIA's ISO (resumable, sha256-checked)
./orin-updater patch-iso ~/orin-updater/jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso
lsblk -o NAME,SIZE,TRAN,MODEL              #    find your USB key, e.g. /dev/sda
sudo ./orin-updater make-usb /dev/sda \
     --iso ~/orin-updater/jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64-orin-updater.iso
sudo ./orin-updater backup --usb           # 3. encrypted backup onto the key (asks a passphrase)
```

Then reboot with the key plugged in:

1. Press **Esc** at the NVIDIA splash, open *Boot Manager* and pick the USB key.
2. If asked to update the firmware, press **Y** within 30 s. The board may reboot twice.
3. In GRUB, open **"Install Jetson ISO r39.2.1 + restore account (orin-updater)"**, then
   **Install on NVMe** (or the entry for your boot medium). Wait about 20–30 min.
4. At first boot, **keep the USB key plugged in** and type your passphrase on the console. The
   home restore takes a while, about 10 min for 50 GB. The Jetson then reboots to your usual
   login screen.
5. Review `~/orin-updater-inventory/reinstall.sh`, then `sudo apt install nvidia-jetpack` for
   CUDA/TensorRT.

Do the steps in that order, make-usb before backup: the backup is the last thing you do on the
old system, so it is as fresh as possible. Close your applications first.

You can also restore manually on an already installed JetPack 7 system, for example a headless
one set up through the serial console. Mount or plug in the key, then run:
`sudo ./orin-updater restore` (or `sudo python3 /usr/local/lib/orin-updater/orin_restore.py`).

### Configuration

`~/.config/orin-updater/config.yaml` (optional):

```yaml
extra_home_excludes:        # relative to your home, added to the defaults
  - models/huggingface      # e.g. large re-downloadable data
  - .ollama/models
system_items: [ssh_host_keys, hostname, network, locale, sudoers, fstab]   # default: all
chunk_size_mib: 4096
```

## How it works

See [docs/iso-internals.md](docs/iso-internals.md) for the full analysis of NVIDIA's installer.
In short:

* NVIDIA's installer is Ubuntu's subiquity/curtin booted by casper from stacked squashfs layers.
  NVIDIA's top layer generates the autoinstall config from `/ai/jetsoniso_orin-ai.yaml`.
* `patch-iso` adds **one extra squashfs layer** (a few KB) on top of NVIDIA's. The layer holds a
  copy of that YAML with **one more late-command**: `sh /cdrom/orin-updater/late-command.sh`.
  The patch then adds `layerfs-path=` to the *Install on …* GRUB entries. NVIDIA's own files,
  kernel, initrd, firmware capsule and EFI partition stay bit-for-bit identical, and
  `patch-iso` verifies this.
* The late-command copies `orin_restore.py` and `orin-restore.service` into the new system.
  The service runs before NVIDIA's `nv-oobe` first-boot setup. It never fails the install.
* `make-usb` writes the ISO, moves the GPT backup header to the end of the key and creates an
  ext4 partition labelled `ORINBAK`. The restore finds the backup by that label, so a separate
  disk labelled `ORINBAK` works too.

## Risks and safety nets

* **The install wipes the target disk.** Keep a second copy of anything precious on another
  medium. `sudo ./orin-updater backup --out /some/other/disk` produces the same backup anywhere.
* The backup is checked right after creation (decrypt + list), and every part's sha256 is checked
  during restore.
* `make-usb` refuses non-USB disks and disks that hold system mounts. It also asks you to type
  the device name.
* Rollback: reinstall JetPack 6 (SDK Manager or NVIDIA's SD image/ISO), then run
  `sudo ./orin-updater restore`. The backup format does not depend on the JetPack version.
* NVIDIA's EULA is normally shown by its first-boot setup, which the restore skips. You
  already accepted it on JetPack 6; it is in `/usr/share/desktop-provision/eula/`.
* Known NVIDIA issue: some users report the ISO installs the non-*Super* power config on
  Orin Nano Super. Check `nvpmodel -q` after the upgrade.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install pytest ruff pyyaml
.venv/bin/pytest            # unit tests
.venv/bin/ruff check .
tests/e2e/docker_roundtrip.sh   # backup on Ubuntu 22.04 -> restore on 24.04, in Docker
```

## Français

`orin-updater` passe une Jetson Orin de JetPack 6 à JetPack 7 **sans perdre le compte
utilisateur**. Le principe :

1. Il sauvegarde le compte, le home et la configuration système dans une archive chiffrée.
2. Il patche l'ISO officielle de NVIDIA en ajoutant une couche squashfs qui contient une
   late-command de plus. Rien de NVIDIA n'est modifié.
3. Il écrit sur une clé USB l'installeur et la sauvegarde.
4. Au premier démarrage de JetPack 7, il demande la phrase de passe et restaure le compte :
   même uid, mêmes groupes, même mot de passe, clés SSH, Wi-Fi, etc.

En cas de problème, l'assistant de configuration NVIDIA se lance normalement. Les commandes sont
dans la section *Usage* ci-dessus. Gardez un écran et un clavier branchés sur la Jetson pendant
toute l'opération.

## License

MIT, see [LICENSE](LICENSE). Not affiliated with NVIDIA. JetPack, Jetson and the Jetson ISO are
NVIDIA's; this project only downloads the ISO from NVIDIA and does not redistribute it.
