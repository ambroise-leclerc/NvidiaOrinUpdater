"""Download, inspect and patch NVIDIA's Jetson ISO installer.

How the r39.x Jetson ISO installs (see docs/iso-internals.md):
  * GRUB boots casper with `autoinstall`; casper stacks the squashfs layers named by
    LAYERFS_PATH (default from the initrd: ...installer.kernel.nvidia.squashfs, overridable
    with the `layerfs-path=` kernel parameter).
  * The top layer holds /autoinstall.yaml whose early-command runs /ai/preseed.sh; preseed
    copies /ai/jetsoniso_<family>-ai.yaml (after sed substitutions) to /autoinstall.yaml.

Patching strategy - nothing of NVIDIA's is modified in place:
  1. build one extra, tiny squashfs layer stacked on top of NVIDIA's leaf layer, containing
     copies of /ai/jetsoniso_*-ai.yaml with one more late-command appended;
  2. add `layerfs-path=<our layer>` to the "Install on ..." GRUB entries;
  3. add our payload under /orin-updater/ on the ISO.
Kernel, initrd, QSPI capsule, boot records and NVIDIA's squashfs layers are kept bit-for-bit.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

KNOWN_ISOS = {
    "r39.2.1": {
        "url": "https://developer.nvidia.com/downloads/embedded/l4t/r39_release_v2.1/iso/"
               "jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso",
        "jetpack": "7.2.1",
        "size": 5035601920,
        "sha256": "f31cf35023cd072707dfc46ef23ed71318449516333055fe3bb9455a615395d0",
    },
}
DEFAULT_RELEASE = "r39.2.1"
PAYLOAD_DIR = "/orin-updater"
LAYER_SUFFIX = "orinupdater"
MARKER = "orin-updater"
TARGET_FILES = ("late-command.sh", "orin_restore.py", "orin-restore.service")
AI_GLOB = re.compile(r"^jetsoniso_[\w-]+-ai\.yaml$")
GRUB_TITLE_SUFFIX = " + restore account (orin-updater)"


class IsoError(RuntimeError):
    pass


def late_command(payload_dir: str = PAYLOAD_DIR) -> str:
    # Runs in the installer environment, where casper mounts the ISO on /cdrom.
    return f"sh /cdrom{payload_dir}/late-command.sh /target || true"


# --------------------------------------------------------------------------- download

def fetch(release: str, dest_dir: Path, retries: int = 10) -> Path:
    info = KNOWN_ISOS.get(release)
    if info is None:
        raise IsoError(f"unknown release {release}; known: {', '.join(KNOWN_ISOS)}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / info["url"].rsplit("/", 1)[1]
    for _ in range(retries):
        if dest.exists() and dest.stat().st_size == info.get("size"):
            break
        # -C - resumes: NVIDIA's CDN tends to drop long downloads midway.
        subprocess.run(["curl", "-fL", "-C", "-", "--retry", "3", "-o", str(dest), info["url"]])
    else:
        raise IsoError("download failed repeatedly")
    if info.get("sha256") and sha256(dest) != info["sha256"]:
        raise IsoError(f"{dest}: sha256 mismatch (corrupted download? delete it and retry)")
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- ISO access

def _tool(name: str, pkg: str) -> None:
    if shutil.which(name) is None:
        raise IsoError(f"{name} is required: sudo apt install {pkg}")


def _xorriso(*args: str) -> subprocess.CompletedProcess:
    _tool("xorriso", "xorriso")
    res = subprocess.run(["xorriso", *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise IsoError(f"xorriso {' '.join(args)} failed: {res.stderr.strip()[-2000:]}")
    return res


def list_files(iso: Path) -> list[str]:
    res = _xorriso("-indev", str(iso), "-find", "/", "-type", "f")
    return [ln.strip().strip("'") for ln in res.stdout.splitlines() if ln.startswith("'/")]


def extract(iso: Path, path: str, dest: Path) -> Path:
    _xorriso("-osirrox", "on", "-indev", str(iso), "-extract", path, str(dest))
    dest.chmod(0o644)
    return dest


def leaf_layer(files: list[str]) -> str:
    """The installer's default leaf layer: deepest *.installer.* squashfs whose parents exist."""
    squash = {f.rsplit("/", 1)[1] for f in files if f.startswith("/casper/")
              and f.endswith(".squashfs")}
    best = None
    for name in squash:
        stem = name[: -len(".squashfs")]
        if ".installer" not in stem or stem.endswith("." + LAYER_SUFFIX):
            continue
        parts = stem.split(".")
        chain_ok = all(".".join(parts[:i]) + ".squashfs" in squash for i in range(1, len(parts)))
        if chain_ok and (best is None or len(parts) > len(best.split("."))):
            best = stem
    if best is None:
        raise IsoError("could not find the installer squashfs layer in /casper")
    # Prefer NVIDIA's layer explicitly when present (it is the initrd default).
    nv = [s for s in squash if s.endswith(".installer.kernel.nvidia.squashfs")]
    return nv[0][: -len(".squashfs")] if nv else best


# --------------------------------------------------------------------------- inspection

@dataclass
class IsoReport:
    files: list[str]
    leaf: str
    grub_entries: list[str]
    patched: bool
    volume: str


def inspect(iso: Path) -> IsoReport:
    files = list_files(iso)
    with tempfile.TemporaryDirectory() as tmp:
        grub = extract(iso, "/boot/grub/grub.cfg", Path(tmp) / "grub.cfg").read_text()
    entries = [ln.strip() for ln in grub.splitlines()
               if re.match(r"\s*(menuentry|submenu)\b", ln)]
    vol = _xorriso("-indev", str(iso), "-pvd_info").stdout
    m = re.search(r"Volume Id\s*:\s*(.+)", vol)
    return IsoReport(files=files, leaf=leaf_layer(files), grub_entries=entries,
                     patched=any(f.startswith(PAYLOAD_DIR + "/") for f in files),
                     volume=m.group(1).strip() if m else "?")


# --------------------------------------------------------------------------- patching

def append_late_command(text: str, command: str | None = None) -> str:
    """Append our command to the trailing `late-commands:` list of an NVIDIA ai.yaml.

    preseed.sh itself appends to these files with `>>`, so late-commands must be the last
    top-level key; we check that, append textually (keeping NVIDIA's CHANGE_* placeholders
    and comments intact) and validate the result.
    """
    command = command or late_command()
    if MARKER in text and command in text:
        return text
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or list(data)[-1] != "late-commands":
        raise IsoError("unexpected ai.yaml layout: 'late-commands' is not the last key")
    out = text.rstrip("\n") + f"\n\n  # {MARKER}: install the first-boot account restore\n" \
                              f"  - {command}\n"
    new = yaml.safe_load(out)
    if new["late-commands"][-1] != command or len(new["late-commands"]) != len(
            data["late-commands"]) + 1:
        raise IsoError("late-command injection produced an unexpected YAML structure")
    return out


def patch_grub(text: str, layer_file: str) -> str:
    """Add layerfs-path= to the 'Install on ...' entries and tag the submenu title."""
    out, current = [], ""
    for line in text.splitlines():
        m = re.match(r'\s*(menuentry|submenu)\s+"([^"]*)"', line)
        if m:
            current = m.group(2)
            if m.group(1) == "submenu" and GRUB_TITLE_SUFFIX not in line:
                line = line.replace(f'"{current}"', f'"{current}{GRUB_TITLE_SUFFIX}"', 1)
        elif re.match(r"\s*linux\s", line) and current.startswith("Install on") \
                and "layerfs-path=" not in line:
            line = f"{line.rstrip()} layerfs-path={layer_file}"
        out.append(line)
    patched = "\n".join(out) + "\n"
    if patched.count("layerfs-path=") == 0:
        raise IsoError("no 'Install on ...' entry found in grub.cfg")
    return patched


def patch_md5sums(text: str, changed: dict[str, Path]) -> str:
    """Update/add md5sum.txt lines ('<md5>  ./path') for changed or added files."""
    lines, seen = [], set()
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].lstrip(".") in changed:
            iso_path = parts[1].lstrip(".")
            line = f"{md5(changed[iso_path])}  .{iso_path}"
            seen.add(iso_path)
        lines.append(line)
    for iso_path, local in sorted(changed.items()):
        if iso_path not in seen:
            lines.append(f"{md5(local)}  .{iso_path}")
    return "\n".join(lines) + "\n"


def payload_files() -> dict[str, Path]:
    base = resources.files("orin_updater") / "target"
    return {name: Path(str(base / name)) for name in TARGET_FILES}


def build_layer(ai_files: dict[str, str], out: Path) -> None:
    """Create a squashfs with /ai/<name> files (root-owned, 0644)."""
    _tool("mksquashfs", "squashfs-tools")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        (root / "ai").mkdir(parents=True)
        root.chmod(0o755)
        (root / "ai").chmod(0o755)
        for name, content in ai_files.items():
            f = root / "ai" / name
            f.write_text(content)
            f.chmod(0o644)
        res = subprocess.run(["mksquashfs", str(root), str(out), "-noappend", "-all-root",
                              "-comp", "gzip", "-b", "1048576", "-quiet"],
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise IsoError(f"mksquashfs failed: {res.stderr.strip()}")


def patch(iso: Path, out: Path, log=print) -> dict:
    """Write a patched copy of `iso` to `out`. Returns a summary dict."""
    if out.resolve() == iso.resolve():
        raise IsoError("output must differ from input")
    _tool("unsquashfs", "squashfs-tools")
    report = inspect(iso)
    if report.patched:
        raise IsoError(f"{iso} already contains {PAYLOAD_DIR}: start from NVIDIA's original ISO")
    leaf = report.leaf
    layer_file = f"{leaf}.{LAYER_SUFFIX}.squashfs"
    log(f"  installer layer: {leaf}.squashfs -> adding {layer_file}")

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        sq = extract(iso, f"/casper/{leaf}.squashfs", t / "leaf.squashfs")
        # -no-xattrs: NVIDIA's /ai carries trusted.overlay.opaque, unwritable as non-root.
        res = subprocess.run(
            ["unsquashfs", "-q", "-no-xattrs", "-d", str(t / "leaf"), str(sq), "ai"],
            capture_output=True, text=True)
        if res.returncode != 0 or not (t / "leaf/ai").is_dir():
            raise IsoError(f"cannot read /ai from {leaf}.squashfs: {res.stderr.strip()}")
        sq.unlink()
        ai_files = {}
        for f in sorted((t / "leaf/ai").iterdir()):
            if AI_GLOB.match(f.name) and f.is_file() and not f.is_symlink():
                ai_files[f.name] = append_late_command(f.read_text())
                log(f"  late-command added to /ai/{f.name}")
        if not ai_files:
            raise IsoError(f"no /ai/jetsoniso_*-ai.yaml in {leaf}.squashfs")
        layer = t / layer_file
        build_layer(ai_files, layer)

        grub = extract(iso, "/boot/grub/grub.cfg", t / "grub.cfg")
        grub.write_text(patch_grub(grub.read_text(), layer_file))

        payload = t / "payload"
        payload.mkdir()
        for name, src in payload_files().items():
            shutil.copy(src, payload / name)
            (payload / name).chmod(0o755 if name.endswith((".sh", ".py")) else 0o644)

        changed = {"/boot/grub/grub.cfg": grub, f"/casper/{layer_file}": layer}
        changed.update({f"{PAYLOAD_DIR}/{n}": payload / n for n in TARGET_FILES})
        md5s = extract(iso, "/md5sum.txt", t / "md5sum.txt")
        md5s.write_text(patch_md5sums(md5s.read_text(), changed))

        if out.exists():
            out.unlink()
        _xorriso("-indev", str(iso), "-outdev", str(out),
                 "-map", str(grub), "/boot/grub/grub.cfg",
                 "-map", str(layer), f"/casper/{layer_file}",
                 "-map", str(payload), PAYLOAD_DIR,
                 "-map", str(md5s), "/md5sum.txt",
                 "-boot_image", "any", "replay", "-commit")
    return {"leaf": leaf, "layer": layer_file, "ai_files": sorted(ai_files)}


@dataclass
class BootLayout:
    el_torito: list[tuple[str, ...]]          # (platform, bootable, emulation, load size, blocks)
    gpt: dict[int, dict[str, str]]            # partition number -> fields
    summary: str

    def partitions(self, skip_gaps: bool = True) -> list[dict[str, str]]:
        return [p for _, p in sorted(self.gpt.items())
                if not (skip_gaps and p.get("name", "").startswith("Gap"))]


def parse_boot_report(text: str) -> BootLayout:
    eltorito, gpt, summary, blks = [], {}, "", {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        key, f = key.strip(), val.split()
        if key == "El Torito boot img" and len(f) >= 7:
            eltorito.append((f[1], f[2], f[3], f[6], f[0]))
        elif key == "El Torito img blks" and len(f) >= 2:
            blks[f[0]] = f[1]
        elif key == "System area summary":
            summary = val.strip()
        elif key.startswith("GPT ") and f and f[0].isdigit():
            field = {"GPT partname local": "name", "GPT type GUID": "type",
                     "GPT partition flags": "flags", "GPT start and size": "range"}.get(key)
            if field:
                gpt.setdefault(int(f[0]), {})[field] = " ".join(f[1:])
    eltorito = [(p, b, e, ls, blks.get(n, "?")) for (p, b, e, ls, n) in eltorito]
    return BootLayout(eltorito, gpt, summary)


def boot_layout(iso: Path) -> BootLayout:
    out = _xorriso("-indev", str(iso), "-report_el_torito", "plain",
                   "-report_system_area", "plain").stdout
    return parse_boot_report(out)


def compare_boot(original: Path, patched: Path) -> list[str]:
    """Check that the patched ISO boots like the original. Returns a list of problems.

    Positions legitimately move (the ISO9660 part changes size) and xorriso drops the
    trailing 'Gap' padding partition, so we compare: El Torito entries, partition scheme,
    the GPT partitions' names/types/flags/sizes, and the EFI partition's bytes.
    """
    a, b = boot_layout(original), boot_layout(patched)
    problems = []
    if a.el_torito != b.el_torito:
        problems.append(f"El Torito differs: {a.el_torito} -> {b.el_torito}")
    if a.summary != b.summary:
        problems.append(f"system area differs: {a.summary!r} -> {b.summary!r}")
    pa, pb = a.partitions(), b.partitions()
    if [(p.get("name"), p.get("type"), p.get("flags")) for p in pa] != \
            [(p.get("name"), p.get("type"), p.get("flags")) for p in pb]:
        problems.append("GPT partitions differ")
    for p, q in zip(pa, pb, strict=False):
        if not p.get("name", "").startswith("Gap"):  # appended partitions: same size + bytes
            (sa, na), (sb, nb) = (map(int, x["range"].split()) for x in (p, q))
            if na != nb:
                problems.append(f"partition {p.get('name')} size {na} -> {nb}")
            elif not _same_bytes(original, sa, patched, sb, na):
                problems.append(f"partition {p.get('name')} content differs")
    return problems


def _same_bytes(f1: Path, start1: int, f2: Path, start2: int, sectors: int) -> bool:
    with open(f1, "rb") as a, open(f2, "rb") as b:
        a.seek(start1 * 512)
        b.seek(start2 * 512)
        left = sectors * 512
        while left:
            n = min(left, 1 << 22)
            if a.read(n) != b.read(n):
                return False
            left -= n
    return True
