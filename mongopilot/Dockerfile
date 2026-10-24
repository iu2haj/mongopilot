FROM python:3.12-slim-bookworm

# OpenSSH client: server automation and host metrics over SSH.
# mongosh and the MongoDB Database Tools (mongodump, mongoexport, ...): the Tools pages.
# MongoDB publishes Debian 12 packages for x86_64 only: on other architectures the image is built without them
# and the Tools pages say so (set INSTALL_MONGODB_TOOLS=false to skip them on purpose).
ARG INSTALL_MONGODB_TOOLS=true
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client ca-certificates curl gnupg \
 && if [ "$INSTALL_MONGODB_TOOLS" = "true" ] && [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      curl -fsSL https://pgp.mongodb.com/server-8.0.asc | gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor \
      && echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main" \
           > /etc/apt/sources.list.d/mongodb-org-8.0.list \
      && apt-get update \
      && apt-get install -y --no-install-recommends mongodb-mongosh mongodb-database-tools \
      && printf 'mongosh:\n  enableTelemetry: false\n' > /etc/mongosh.conf; \
    else echo "MongoDB tools not installed (architecture $(dpkg --print-architecture))"; fi \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mongopilot ./mongopilot
COPY tests ./tests

RUN useradd --system --uid 10001 --home-dir /data mongopilot \
 && mkdir -p /data && chown mongopilot /data
USER mongopilot
ENV MP_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "--factory", "mongopilot.app:create_app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
