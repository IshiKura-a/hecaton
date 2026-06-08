ARG HECATON_REPO=https://github.com/IshiKura-a/hecaton.git
ARG HECATON_REF=main

# --- stage 1: fetch the repo so later stages can COPY from it ---
FROM alpine/git:latest AS src
ARG HECATON_REPO
ARG HECATON_REF
RUN git clone --depth=1 --branch "$HECATON_REF" "$HECATON_REPO" /src

# --- stage 2: runtime ---
FROM python:3.11-slim
WORKDIR /app

# tailscale (userspace mode, no host TUN device needed)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates iproute2 \
 && curl -fsSL https://tailscale.com/install.sh | sh \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=src /src/envs /tmp/hecaton-envs
COPY --from=src /src/envs/trainer-entrypoint.sh /usr/local/bin/trainer-entrypoint.sh
COPY --from=src /src/examples/trainer-smoke/run.py /app/run.py
RUN pip install --no-cache-dir /tmp/hecaton-envs \
 && rm -rf /tmp/hecaton-envs \
 && chmod +x /usr/local/bin/trainer-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/trainer-entrypoint.sh"]
CMD ["python", "/app/run.py"]
