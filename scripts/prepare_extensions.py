"""Stage the pinned extension bundle for source development and package builds.

Uses only the standard library, so it can run before installing this project.
Published wheels and sdists already contain these files; runtime never downloads.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "src/netops_api_navigator/extension_bundle"


def prepare(archive: Path | None = None) -> None:
    manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    expected = {
        f"{platform}/lib{name}.lbug_extension": entry
        for platform, extensions in manifest["platforms"].items()
        for name, entry in extensions.items()
    }
    def matches(data: bytes, entry: dict) -> bool:
        return len(data) == entry["size"] and hashlib.sha256(data).hexdigest() == entry["sha256"]

    if all((BUNDLE / name).is_file() and matches((BUNDLE / name).read_bytes(), entry)
           for name, entry in expected.items()):
        print("All pinned extension binaries already verified.")
        return
    if archive:
        data = archive.read_bytes()
    else:
        request = urllib.request.Request(
            manifest["bundle_url"], headers={"User-Agent": "netops-api-navigator-build"}
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read()
    if hashlib.sha256(data).hexdigest() != manifest["bundle_sha256"]:
        raise ValueError("Extension archive checksum mismatch; refusing to stage binaries")
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        # Read exact manifest members; never extract arbitrary archive paths.
        contents = {name: bundle.read(name) for name in expected}
    for name, binary in contents.items():
        if not matches(binary, expected[name]):
            raise ValueError(f"Extension checksum mismatch: {name}")
    for name, binary in contents.items():
        destination = BUNDLE / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(binary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Verified and staged {len(contents)} pinned extension binaries.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use an already downloaded pinned archive")
    prepare(parser.parse_args().archive)
