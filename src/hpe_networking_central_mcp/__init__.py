"""HPE Networking Central MCP Server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("hpe-networking-central-mcp")
except PackageNotFoundError:
    __version__ = "0.3.0"
