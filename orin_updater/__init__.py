"""NvidiaOrinUpdater: JetPack 6 -> 7 upgrade for Jetson Orin that keeps your account."""

__version__ = "0.1.0"

# Layout of the backup directory (on the ORINBAK partition, under BACKUP_DIRNAME).
BACKUP_LABEL = "ORINBAK"
BACKUP_DIRNAME = "orin-backup"
MANIFEST_NAME = "manifest.json"
SYSTEM_ARCHIVE = "system.tar.zst.gpg"
HOME_ARCHIVE_PREFIX = "home.tar.zst.gpg."
FORMAT_VERSION = 1
