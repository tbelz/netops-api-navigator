"""NetOps API Navigator."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("netops-api-navigator")
except PackageNotFoundError:
    __version__ = "0.3.0"
