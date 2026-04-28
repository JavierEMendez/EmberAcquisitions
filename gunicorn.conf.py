"""Gunicorn config — reads PORT from os.environ in Python so we don't
depend on shell variable expansion at any layer (Docker CMD, Procfile,
or Railway's start-command override). All three of those paths have
historically had issues passing $PORT to gunicorn unexpanded; doing
the env lookup in Python is bulletproof.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
# Two workers minimum so an in-process HTTP fetch (e.g. WeasyPrint
# loading a /static/... resource while rendering a PDF) doesn't
# deadlock against the same worker that's serving the request.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
accesslog = "-"
errorlog = "-"
