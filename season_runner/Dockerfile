# Playwright's own image: Chromium plus every system library it needs, matched
# to the Playwright version the project pins. Must equal the pip version below.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV DEBIAN_FRONTEND=noninteractive PIP_BREAK_SYSTEM_PACKAGES=1 PIP_ROOT_USER_ACTION=ignore \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# A virtual display + VNC, used only for the one-time Sleeper sign-in screen.
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc tzdata procps python3-venv \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
# Its own virtualenv: installing over the image's system Python fails, because
# pip cannot replace the Debian-packaged modules apt just pulled in
# ("uninstall-no-record-file"). The browsers live in /ms-playwright either way.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir "playwright==1.62.0" -e /app

COPY seed_state.json run.sh /app/
RUN chmod +x /app/run.sh

CMD ["/app/run.sh"]
