FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS agents
WORKDIR /opt/agents
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=agents /usr/local/bin/node /usr/local/bin/node
COPY --from=agents /opt/agents /opt/agents
RUN ln -s /opt/agents/node_modules/.bin/codex /usr/local/bin/codex && \
    ln -s /opt/agents/node_modules/.bin/claude /usr/local/bin/claude && \
    ln -s /opt/agents/node_modules/.bin/grok /usr/local/bin/grok
WORKDIR /app
COPY fetch_agy.py agy.lock.json ./
RUN python fetch_agy.py
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY agentic_runner ./agentic_runner
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/health',timeout=3)"
CMD ["python", "-m", "agentic_runner.app"]
