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

# Threads matter now that /acquisitions is in here. Its analysis endpoint
# spends 30-60s blocked on external GIS services - FEMA, USFWS wetlands, USGS
# elevation - not on CPU. With sync workers and no threads, two people running
# an analysis at once would occupy both workers and the whole portal would
# stop answering: Financials, BVA, Capital, everything. Threads let a worker
# serve other requests while one is parked on a socket, which is exactly the
# shape of this workload. The standalone GIS app ran --threads 4 for the same
# reason.
threads = int(os.environ.get("GUNICORN_THREADS", "4"))

# Those same GIS services are why the timeout is generous: a wetlands page can
# legitimately take ~50s against a slow USFWS.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
accesslog = "-"
errorlog = "-"
