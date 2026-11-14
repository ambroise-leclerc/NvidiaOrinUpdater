#!/bin/sh
# orin-updater late-command: runs in the Jetson ISO installer environment, after curtin has
# installed the new system into $TARGET (default /target).
#
# It only installs the first-boot restore service; the actual restore happens at first boot,
# when the passphrase can be asked. It must NEVER make the installation fail: every error
# is logged and the script exits 0, leaving NVIDIA's normal first-boot setup in place.

SRC=$(cd "$(dirname "$0")" && pwd)
TARGET=${1:-/target}
LOG="$TARGET/var/log/orin-updater-install.log"

mkdir -p "$TARGET/var/log" 2>/dev/null
exec >>"$LOG" 2>&1
echo "orin-updater late-command $(date -u '+%F %T') src=$SRC target=$TARGET"

install_restore() {
    set -e
    [ -d "$TARGET/etc/systemd/system" ] || { echo "no systemd in target"; return 1; }
    dest="$TARGET/usr/local/lib/orin-updater"
    mkdir -p "$dest"
    cp "$SRC/orin_restore.py" "$dest/orin_restore.py"
    chmod 0755 "$dest/orin_restore.py"
    cp "$SRC/orin-restore.service" "$TARGET/etc/systemd/system/orin-restore.service"
    chmod 0644 "$TARGET/etc/systemd/system/orin-restore.service"
    for t in nv-oobe.target nv-oem-config.target graphical.target multi-user.target; do
        mkdir -p "$TARGET/etc/systemd/system/$t.wants"
        ln -sf ../orin-restore.service "$TARGET/etc/systemd/system/$t.wants/orin-restore.service"
    done
    mkdir -p "$TARGET/var/lib/orin-updater"
    chmod 0700 "$TARGET/var/lib/orin-updater"
    date -u '+%F %T' >"$TARGET/var/lib/orin-updater/pending"
    for tool in python3 gpg zstd tar; do
        if [ -x "$TARGET/usr/bin/$tool" ] || [ -x "$TARGET/bin/$tool" ]; then
            echo "target has $tool"
        else
            echo "WARNING: target lacks $tool, first-boot restore will fall back to oem-config"
        fi
    done
    echo "orin-restore.service installed"
}

if (install_restore); then
    echo "done"
else
    echo "FAILED (installation continues, NVIDIA oem-config will run as usual)"
    rm -f "$TARGET/var/lib/orin-updater/pending"
fi
exit 0
