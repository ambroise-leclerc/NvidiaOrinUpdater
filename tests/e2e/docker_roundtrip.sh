#!/bin/sh
# shellcheck disable=SC2016  # the container scripts are single-quoted on purpose
# End-to-end backup -> restore test without Jetson hardware.
#   1. "old system" container (Ubuntu 22.04, like JetPack 6): create a user with groups,
#      password, SSH keys, files; run `orin-updater backup`.
#   2. "new system" container (Ubuntu 24.04, like JetPack 7): run orin_restore.py and check
#      that the account, password hash, permissions and system files came back.
# Usage: tests/e2e/docker_roundtrip.sh   (needs docker; downloads ubuntu images + packages)
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d)
trap 'docker run --rm -v "$WORK:/w" ubuntu:24.04 rm -rf /w/backup >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT
mkdir -p "$WORK/backup"
printf 'e2e passphrase\n' >"$WORK/pp"

echo "=== old system (22.04): create user + backup"
docker run --rm -v "$REPO:/src:ro" -v "$WORK:/w" ubuntu:22.04 sh -euc '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq python3 python3-yaml gnupg zstd sudo openssh-client cron >/dev/null
  groupadd -g 994 docker; groupadd -g 993 ollama; groupadd -g 999 gpio
  useradd -m -u 1500 -s /bin/bash -c "Alice,,," -G docker,ollama,gpio,sudo alice
  echo "alice:S3cret-pass" | chpasswd
  su alice -c "ssh-keygen -q -t ed25519 -N \"\" -f /home/alice/.ssh/id_ed25519"
  su alice -c "mkdir -p ~/code/.git ~/.cache/pip && echo hello > ~/code/main.c && echo junk > ~/.cache/pip/x && ln -s code/main.c ~/link"
  chmod 700 /home/alice/code
  mkdir -p /etc/ssh && echo HOSTKEY > /etc/ssh/ssh_host_ed25519_key && chmod 600 /etc/ssh/ssh_host_ed25519_key
  echo HOSTPUB > /etc/ssh/ssh_host_ed25519_key.pub
  echo orin-dev > /etc/hostname
  printf "127.0.0.1 localhost\n127.0.1.1 orin-dev\n10.0.0.5 nas\n" > /etc/hosts
  mkdir -p /etc/NetworkManager/system-connections
  printf "[connection]\nid=home\n[wifi-security]\npsk=wifipass\n" > /etc/NetworkManager/system-connections/home.nmconnection
  chmod 600 /etc/NetworkManager/system-connections/home.nmconnection
  echo "alice ALL=(ALL) NOPASSWD: /usr/bin/apt" > /etc/sudoers.d/alice && chmod 440 /etc/sudoers.d/alice
  printf "nas:/vol /mnt/nas nfs defaults 0 0\n" >> /etc/fstab
  echo "0 3 * * * echo backup" | crontab -u alice -
  getent shadow alice | cut -d: -f2 > /w/hash
  cp -r /src /tmp/src && cd /tmp/src
  python3 -m orin_updater backup --user alice --out /w/backup --passphrase-file /w/pp
'

echo "=== new system (24.04): restore"
docker run --rm -v "$REPO:/src:ro" -v "$WORK:/w" ubuntu:24.04 sh -euc '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq python3 gnupg zstd sudo cron >/dev/null
  groupadd -g 994 render-x   # gid of the old "docker" group already taken on the new system
  printf "127.0.0.1 localhost\n127.0.1.1 ubuntu\n" > /etc/hosts
  echo "UUID=new / ext4 defaults 0 1" > /etc/fstab
  python3 /src/orin_updater/target/orin_restore.py --backup-dir /w/backup --passphrase-file /w/pp --no-finalize
  fail() { echo "FAIL: $*"; exit 1; }
  id alice
  [ "$(id -u alice)" = 1500 ] || fail uid
  id -nG alice | grep -qw docker || fail "docker group"
  id -nG alice | grep -qw ollama || fail "ollama group"
  id -nG alice | grep -qw sudo || fail "sudo group"
  [ "$(getent group ollama | cut -d: -f3)" = 993 ] || fail "ollama gid kept"
  [ "$(getent shadow alice | cut -d: -f2)" = "$(cat /w/hash)" ] || fail "password hash"
  [ "$(getent passwd alice | cut -d: -f7)" = /bin/bash ] || fail shell
  [ "$(stat -c %U:%a /home/alice/.ssh/id_ed25519)" = alice:600 ] || fail "ssh key perms"
  [ "$(stat -c %U:%a /home/alice/code)" = alice:700 ] || fail "dir perms"
  [ "$(cat /home/alice/code/main.c)" = hello ] || fail content
  [ -L /home/alice/link ] || fail symlink
  [ ! -e /home/alice/.cache/pip/x ] || fail "cache should be excluded"
  [ "$(cat /etc/ssh/ssh_host_ed25519_key)" = HOSTKEY ] || fail "host key"
  [ "$(stat -c %a /etc/ssh/ssh_host_ed25519_key)" = 600 ] || fail "host key perms"
  [ "$(cat /etc/hostname)" = orin-dev ] || fail hostname
  grep -q "127.0.1.1.orin-dev" /etc/hosts || fail "hosts 127.0.1.1"
  grep -q "10.0.0.5 nas" /etc/hosts || fail "hosts extra"
  grep -q "psk=wifipass" /etc/NetworkManager/system-connections/home.nmconnection || fail nm
  [ "$(stat -c %a /etc/NetworkManager/system-connections/home.nmconnection)" = 600 ] || fail "nm perms"
  [ "$(stat -c %a /etc/sudoers.d/alice)" = 440 ] || fail sudoers
  grep -q "/mnt/nas.*nofail" /etc/fstab || fail fstab
  crontab -u alice -l | grep -q "echo backup" || fail crontab
  [ -x /home/alice/orin-updater-inventory/reinstall.sh ] || fail inventory
  [ -f /home/alice/.config/gnome-initial-setup-done ] || fail "initial setup marker"
  su alice -c "sudo -n -l" >/dev/null 2>&1 || true
  echo "ALL CHECKS PASSED"
'
