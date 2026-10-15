FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0
COPY requirements.lock /requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /requirements.lock
COPY render.py /render.py
RUN chown root:root /render.py /requirements.lock && chmod u=rwX,go=rX /render.py /requirements.lock
USER 10004:10004
ENV HOME=/work TMPDIR=/work PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
ENTRYPOINT ["timeout", "--signal=TERM", "--kill-after=3", "50", "python", "/render.py"]
