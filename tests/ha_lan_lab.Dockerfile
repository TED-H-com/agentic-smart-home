# Test-only namespace: never shipped as a product image.
FROM agentic-home-builder:0.2.0
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    iproute2 dnsmasq-base && rm -rf /var/lib/apt/lists/*
