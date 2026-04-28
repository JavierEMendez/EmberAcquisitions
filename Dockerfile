# EmberApps — production image.
#
# We use a Dockerfile rather than Nixpacks because WeasyPrint loads
# Pango / Cairo / GDK-Pixbuf via ctypes at runtime, and the Nix-based
# Python runtime that Nixpacks builds doesn't expose apt-installed
# libraries on its library search path. With a regular Debian base,
# the apt-installed .so files end up in the standard /usr/lib path
# where ctypes.util.find_library() finds them.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System libraries WeasyPrint links against at runtime, plus a small
# set of Liberation/Dejavu fonts so the brand fallback ("Plus Jakarta
# Sans" -> system-ui) renders something legible.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi-dev \
        libjpeg62-turbo \
        libxml2 \
        shared-mime-info \
        fonts-liberation \
        fonts-dejavu-core \
        fontconfig \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer caches across code-only changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Then the rest of the app.
COPY . .

# Railway sets PORT at runtime; default 8000 lets `docker run` work too.
EXPOSE 8000

# Use exec form + explicit `sh -c` so $PORT is unambiguously expanded by
# the shell. Plain CMD shell form *should* expand it too, but on Railway
# we saw "'$PORT' is not a valid port number" — gunicorn was getting the
# literal string. The `exec` keeps gunicorn as PID 1 so SIGTERM still
# propagates cleanly on container shutdown.
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:${PORT:-8000} --workers 1 --timeout 120 --log-level info"]
