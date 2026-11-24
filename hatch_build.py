"""Refuse distributions missing any of the pinned native resource files."""
import hashlib
import json
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # Editable development can stage binaries after uv sync. Release artifacts
        # must be complete for every advertised platform, including sdist rebuilds.
        if version == "editable":
            return
        root = Path(self.root) / "src/netops_api_navigator/extension_bundle"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for platform, entries in manifest["platforms"].items():
            for name, entry in entries.items():
                path = root / platform / f"lib{name}.lbug_extension"
                if (not path.is_file() or path.stat().st_size != entry["size"]
                        or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]):
                    raise RuntimeError(
                        f"Missing or invalid extension: {platform}/{name}. "
                        "Run 'uv run --no-project python scripts/prepare_extensions.py' first."
                    )
