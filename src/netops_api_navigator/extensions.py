"""Verified, release-bundled Ladybug extensions. Runtime never downloads code."""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

import ladybug as lb

ENGINE_VERSION = "0.19.1"
EXTENSION_ABI = "v0.19.0"
STORAGE_VERSION = 43
KNOWLEDGE_RELEASE_PREFIX = "knowledge-lbug43-"
_ROOT = Path(__file__).parent / "extension_bundle"
_REQUIRED = ("fts", "algo")


class ExtensionError(RuntimeError):
    """The installed runtime cannot safely use its required extensions."""


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64",
            "aarch64": "arm64"}.get(machine, machine)
    system = {"Darwin": "osx", "Linux": "linux", "Windows": "win"}.get(
        platform.system(), platform.system().lower()
    )
    if system == "linux" and platform.libc_ver()[0] != "glibc":
        raise ExtensionError("Bundled Ladybug extensions require glibc on Linux; musl is unsupported.")
    return f"{system}_{arch}"


def _error(extension: str, reason: str) -> ExtensionError:
    return ExtensionError(
        f"Ladybug {ENGINE_VERSION} / extensions {EXTENSION_ABI} "
        f"({platform.system()} {platform.machine()}), {extension}: {reason}. "
        "Reinstall the matching Navigator distribution. For a source checkout, run "
        "'uv run --no-project python scripts/prepare_extensions.py' before uv sync. "
        "Existing databases must not be reset or deleted."
    )


def _engine_version() -> str:
    try:
        return lb.__version__
    except (ImportError, OSError, RuntimeError) as exc:
        repair = ""
        if platform.system() == "Windows":
            repair = (
                " The Windows engine requires the OpenSSL libraries supplied by "
                "uv-managed Python. Reinstall with 'uv tool install --reinstall "
                "--managed-python --python 3.12 --no-build netops-api-navigator'."
            )
        raise _error("engine", f"native library could not load: {exc}.{repair}") from exc


def verify_extensions() -> tuple[Path, ...]:
    """Check every required binary before loading any executable code."""
    installed_version = _engine_version()
    if installed_version != ENGINE_VERSION:
        raise _error("engine", f"installed version is {installed_version}")
    try:
        manifest = json.loads((_ROOT / "manifest.json").read_text(encoding="utf-8"))
        if (manifest["engine_version"], manifest["extension_abi"]) != (
            ENGINE_VERSION, EXTENSION_ABI
        ):
            raise _error("manifest", "engine/ABI mismatch")
        entries = manifest["platforms"][platform_key()]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _error("bundle", "manifest missing, invalid, or platform unsupported") from exc
    paths = []
    for name in _REQUIRED:
        try:
            entry = entries[name]
            path = _ROOT / platform_key() / f"lib{name}.lbug_extension"
            data = path.read_bytes()
            if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise _error(name, "binary checksum mismatch")
        except (OSError, KeyError, TypeError) as exc:
            raise _error(name, "bundled binary missing or unreadable") from exc
        paths.append(path.resolve())
    return tuple(paths)


def load_extensions(conn: lb.Connection, paths: tuple[Path, ...] | None = None) -> None:
    """Load only verified local libraries into this database instance."""
    paths = verify_extensions() if paths is None else paths
    for name, path in zip(_REQUIRED, paths, strict=True):
        try:
            conn.execute(f"LOAD EXTENSION {json.dumps(path.as_posix())}").close()
        except Exception as exc:
            raise _error(name, f"cannot load bundled binary: {exc}") from exc


def preflight_extensions() -> tuple[Path, ...]:
    """Verify ABI/loadability without opening or migrating any user's database."""
    paths = verify_extensions()
    db = lb.Database(buffer_pool_size=64 * 1024 * 1024)
    try:
        with lb.Connection(db) as conn:
            load_extensions(conn, paths)
    finally:
        db.close()
    return paths


def extension_diagnostics() -> dict:
    return {"engine": _engine_version(), "expected_engine": ENGINE_VERSION,
            "extension_abi": EXTENSION_ABI, "platform": platform_key(),
            "source": "bundled", "required": list(_REQUIRED)}
