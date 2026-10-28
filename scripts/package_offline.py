#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the offline-installable zip of the Headroom plugin.

The plugin has zero third-party dependencies (stdlib only on the backend, a
plain ES module on the frontend), so "offline packaging" here means: validate
the tree, copy only the runtime files, and emit a byte-stable zip that can be
carried on a USB stick into an air-gapped machine.

    python3 scripts/package_offline.py                 # -> release/*.zip
    python3 scripts/package_offline.py --out-dir dist  # custom output dir
    python3 scripts/package_offline.py --no-verify     # skip py_compile/JSON checks

The zip is deterministic: entries are sorted, timestamps are clamped to the
1980-01-01 epoch and permissions are normalised, so two builds of the same
tree produce the same SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import py_compile
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Everything the runtime needs — and nothing that only matters during development.
INCLUDE = [
    "plugin.json",
    "plugin.py",
    "headroom_plugin",
    "frontend",
    "headroom.config.json",
    "README.md",
    "LICENSE",
]

EXCLUDE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".git",
    ".venv",
    "node_modules",
    "release",
    "dist_pkg",
}

EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".swp", ".bak"}

#: Fixed timestamp for reproducible archives (zip epoch minimum).
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class PackagingError(RuntimeError):
    """Raised when the tree cannot produce a shippable archive."""


def log(message: str) -> None:
    print(message, flush=True)


def read_manifest() -> dict:
    path = ROOT / "plugin.json"
    if not path.is_file():
        raise PackagingError(f"missing {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackagingError(f"plugin.json is not valid JSON: {exc}") from exc
    for key in ("id", "version", "name", "entry"):
        if key not in manifest:
            raise PackagingError(f"plugin.json is missing '{key}'")
    if "sdk" in manifest:
        # A manifest with `sdk` hands the plugin a PluginContext instead of the
        # legacy PluginApi, and PluginContext has no register_http_router().
        raise PackagingError("plugin.json must not declare 'sdk' (the router needs the legacy PluginApi)")
    entry = manifest["entry"]
    backend = ROOT / entry.get("backend", "")
    frontend = ROOT / entry.get("frontend", "")
    if not backend.is_file():
        raise PackagingError(f"backend entry not found: {entry.get('backend')}")
    if not frontend.is_file():
        raise PackagingError(f"frontend entry not found: {entry.get('frontend')}")
    return manifest


def verify_sources() -> None:
    """Compile every module so a syntax error never reaches the air-gapped box."""
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(ROOT.rglob("*.py")):
            if any(part in EXCLUDE_DIRS for part in path.relative_to(ROOT).parts):
                continue
            target = Path(tmp) / (path.stem + ".pyc")
            try:
                py_compile.compile(str(path), cfile=str(target), doraise=True)
            except py_compile.PyCompileError as exc:  # pragma: no cover - build guard
                failures.append(f"{path.relative_to(ROOT)}: {exc.msg}")
    if failures:
        raise PackagingError("syntax errors:\n  " + "\n  ".join(failures))
    log(f"  verified {len(list(ROOT.rglob('*.py')))} python files")


def iter_included_files() -> list[Path]:
    files: list[Path] = []
    for entry in INCLUDE:
        path = ROOT / entry
        if not path.exists():
            continue
        if path.is_file():
            files.append(path)
            continue
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            relative = child.relative_to(ROOT)
            if any(part in EXCLUDE_DIRS for part in relative.parts):
                continue
            if child.suffix in EXCLUDE_SUFFIXES:
                continue
            if child.name.startswith("."):
                continue
            files.append(child)
    return sorted(set(files), key=lambda item: item.relative_to(ROOT).as_posix())


def stage(files: list[Path], manifest: dict) -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory(prefix="headroom-plugin-")
    base = Path(tmp.name) / manifest["id"]
    for source in files:
        relative = source.relative_to(ROOT)
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return base, tmp


def write_zip(base: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries = sorted((p for p in base.rglob("*") if p.is_file()), key=lambda p: p.relative_to(base).as_posix())
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in entries:
            relative = path.relative_to(base).as_posix()
            info = zipfile.ZipInfo(relative, date_time=ZIP_DATE)
            info.external_attr = (0o644 & 0xFFFF) << 16
            info.create_system = 3
            archive.writestr(info, path.read_bytes())
    # Keep the staging directory usable for a listing after the zip is written.
    log(f"  wrote {len(entries)} entries")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize(base: Path, manifest: dict, archive: Path) -> None:
    size_kb = archive.stat().st_size / 1024
    log("")
    log(f"plugin      : {manifest['id']} {manifest['version']}")
    log(f"archive     : {archive} ({size_kb:.1f} KiB)")
    log(f"sha256      : {sha256_of(archive)}")
    log(f"install to  : ~/.qwenpaw/plugins/{manifest['id']}/")
    log("")
    log("contents:")
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        log(f"  {path.relative_to(base).as_posix()}")


def build(out_dir: Path, verify: bool = True) -> Path:
    log("[1/4] reading manifest")
    manifest = read_manifest()
    log(f"  {manifest['id']} {manifest['version']}")

    if verify:
        log("[2/4] verifying sources")
        verify_sources()
    else:
        log("[2/4] verifying sources (skipped)")

    log("[3/4] collecting files")
    files = iter_included_files()
    for path in files:
        log(f"  + {path.relative_to(ROOT).as_posix()}")

    base, tmp = stage(files, manifest)
    try:
        log("[4/4] writing archive")
        name = f"{manifest['id']}-plugin-{manifest['version']}.zip"
        archive = out_dir / name
        write_zip(base, archive)
        digest_path = archive.with_suffix(archive.suffix + ".sha256")
        digest_path.write_text(f"{sha256_of(archive)}  {archive.name}\n", encoding="utf-8")
        summarize(base, manifest, archive)
        log(f"checksum    : {digest_path.name}")
        return archive
    finally:
        tmp.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the offline install zip for the Headroom plugin")
    parser.add_argument("--out-dir", default="release", help="output directory (default: release)")
    parser.add_argument("--no-verify", action="store_true", help="skip syntax/manifest verification")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    try:
        archive = build(out_dir, verify=not args.no_verify)
    except PackagingError as exc:
        log(f"error: {exc}")
        return 1
    log(f"\nOK -> {archive.relative_to(ROOT) if archive.is_relative_to(ROOT) else archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
