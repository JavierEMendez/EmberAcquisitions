"""Gunicorn config — reads PORT from os.environ in Python so we don't
depend on shell variable expansion at any layer (Docker CMD, Procfile,
or Railway's start-command override). All three of those paths have
historically had issues passing $PORT to gunicorn unexpanded; doing
the env lookup in Python is bulletproof.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
accesslog = "-"
errorlog = "-"
