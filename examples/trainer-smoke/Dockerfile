FROM python:3.11-slim

WORKDIR /app

# tailscale (userspace mode, no host TUN device needed)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates iproute2 \
 && curl -fsSL https://tailscale.com/install.sh | sh \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# hecaton trainer SDK + the entrypoint that joins the tailnet
COPY envs/pyproject.toml /tmp/hecaton-envs/
COPY envs/hecaton_envs   /tmp/hecaton-envs/hecaton_envs
COPY envs/trainer-entrypoint.sh /usr/local/bin/trainer-entrypoint.sh
RUN pip install --no-cache-dir /tmp/hecaton-envs && rm -rf /tmp/hecaton-envs \
 && chmod +x /usr/local/bin/trainer-entrypoint.sh

# Test script
COPY examples/trainer-smoke/run.py /app/run.py

ENTRYPOINT ["/usr/local/bin/trainer-entrypoint.sh"]
CMD ["python", "/app/run.py"]
