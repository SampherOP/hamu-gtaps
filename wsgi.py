"""HAMU-GPT web build - the same UI served to Chrome, for Vercel.

This is a thin ASGI/WSGI wrapper: it reuses nexus_app.serve_web's handler
logic but exposes it as a WSGI app so a host (Vercel, Render, Railway, or a
plain `python wsgi.py`) can serve it.

Local run:   python wsgi.py            -> http://127.0.0.1:8000
Hosted:      set HAMU_HOST=1 in the environment so the app binds 0.0.0.0 and
             reads the platform's PORT.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import nexus_app  # noqa: E402

# Vercel's Python runtime wants a module-level `app`.
app = nexus_app.build_wsgi_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 8000)
    host = "0.0.0.0" if os.environ.get("HAMU_HOST") else "127.0.0.1"
    nexus_app.serve_web(port=port, host=host)
