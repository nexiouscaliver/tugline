"""The tug console: a loopback web view over one tugline home.

server.py is the HTTP API, ingest.py the background spool to store
fold, web/ the static page. Everything binds 127.0.0.1 only; the
loopback is the security boundary for reads, the shared bearer token
gates the write endpoints.
"""

from __future__ import annotations


def __getattr__(name):
    # lazy so importing tugline.console never pulls the server stack
    if name == "ConsoleServer":
        from tugline.console.server import ConsoleServer

        return ConsoleServer
    raise AttributeError(name)
