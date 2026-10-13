"""tugline: push notifications and phone replies for coding-agent sessions, over ntfy."""

import importlib.metadata

try:
    __version__ = importlib.metadata.version("tugline")
except importlib.metadata.PackageNotFoundError:
    # Running from a source checkout that is not installed anywhere.
    __version__ = "0.0.0.dev0"
