FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 netplan.io network-manager dconf-cli libglib2.0-bin && rm -rf /var/lib/apt/lists/*
ENV PYTHONPATH=/source/host
ENTRYPOINT ["python3", "/source/tests/appliance_container_probe.py"]
