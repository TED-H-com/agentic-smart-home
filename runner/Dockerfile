FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS agents
WORKDIR /opt/agents
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS webtoolchain
WORKDIR /opt/web-toolchain
COPY web-toolchain/package.json web-toolchain/package-lock.json ./
RUN npm ci --no-audit --no-fund \
 && chmod -R u=rwX,go=rX /opt/web-toolchain \
 && chown -R root:root /opt/web-toolchain

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends git \
    bubblewrap=0.8.0-2+deb12u1 fuse2fs=1.47.0-2+b2 e2fsprogs=1.47.0-2+b2 \
    util-linux=2.38.1-5+deb12u3 libseccomp2=2.5.4-1+deb12u1 && rm -rf /var/lib/apt/lists/*
COPY --from=agents /usr/local/bin/node /usr/local/bin/node
COPY --from=agents /opt/agents /opt/agents
COPY --from=webtoolchain /opt/web-toolchain /opt/web-toolchain
RUN ln -s /opt/agents/node_modules/.bin/codex /usr/local/bin/codex && \
    ln -s /opt/agents/node_modules/.bin/claude /usr/local/bin/claude && \
    ln -s /opt/agents/node_modules/.bin/grok /usr/local/bin/grok
WORKDIR /app
COPY fetch_agy.py agy.lock.json ./
RUN python fetch_agy.py
COPY requirements.txt requirements-coding.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements-coding.txt
COPY build_worker_source.py coding-source.lock.json /build/
RUN python /build/build_worker_source.py && rm -rf /build
RUN install -d -m 0755 /opt/agentic-worker/staging/workspace && \
    install -m 0600 /dev/null /opt/agentic-worker/staging/relay.sock
COPY worker_runtime/agentic_host /app/agentic_host
COPY agentic_runner ./agentic_runner
# Normalize public code only; the private source repository and relay staging
# under /opt/agentic-worker retain their separately enforced modes.
RUN chown -R root:root /app /opt/agents && chmod -R u=rwX,go=rX /app /opt/agents
ARG BUILD_VERSION=0.11.1
ARG BUILD_ARCH=amd64
LABEL io.hass.version=$BUILD_VERSION io.hass.type=app io.hass.arch=$BUILD_ARCH
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/health',timeout=3)"
CMD ["python", "-m", "agentic_runner.app"]
