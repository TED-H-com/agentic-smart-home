FROM python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY fetch_upstream.py hermes.lock.json ./
RUN python fetch_upstream.py && \
    useradd --uid 10003 --create-home --shell /usr/sbin/nologin hermes && \
    mkdir -p /data && chown 10003:10003 /data
COPY agentic_hermes ./agentic_hermes
USER 10003:10003
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,pathlib,urllib.request; p=os.environ.get('AGENTIC_HERMES_TOKEN_FILE','/hermes-link/service-token'); r=urllib.request.Request('http://127.0.0.1:8096/v1/capabilities',headers={'Authorization':'Bearer '+pathlib.Path(p).read_text().strip()}); urllib.request.urlopen(r,timeout=3)"
CMD ["python", "-m", "agentic_hermes.app"]
