FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY agentic_controller ./agentic_controller
COPY static ./static
COPY start.py ./start.py
COPY source/agentic-home.bundle /app/source/agentic-home.bundle
# Public image files must be readable after start.py drops privileges, regardless
# of the build host's umask. Runtime state under /data keeps its private modes.
RUN chown -R root:root /app && chmod -R u=rwX,go=rX /app
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/health',timeout=3)"
CMD ["python", "start.py"]
