# python:3.12-slim because /usr/bin/python3 on the target box is 3.6.8 -- and
# is what dnf itself runs on, so pip-installing into it is a genuine way to
# break package management. This is the strongest single argument for the
# container over a bare-metal venv.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so an edit to the source does not re-resolve the wheels.
# Every wheel here is manylinux-prebuilt (pillow, argon2-cffi-bindings), so no
# compiler is needed and the image stays small.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
RUN pip install --no-deps /app

# Runs as a non-root user. The uid is fixed so the bind-mounted host
# directories can be chowned to match: see hub/README.md.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin kindlehub
USER kindlehub

EXPOSE 8090

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4).status == 200 else 1)"

# One gthread worker with 4 threads. Not more: the box has a single vCPU, and
# the build loop runs in a thread inside this process behind a flock, so extra
# workers would idle rather than help.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8090", \
     "--worker-class", "gthread", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "kindle_hub.app:wsgi_app()"]
