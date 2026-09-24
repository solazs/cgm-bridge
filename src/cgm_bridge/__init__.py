"""cgm-bridge: a minimal Nightscout upload receiver."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cgm-bridge")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"
