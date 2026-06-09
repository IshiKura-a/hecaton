# Trainer smoke-test image. Three build modes via HECATON_SOURCE:
#
#   git    - hecaton fetched from GitHub at build time (HECATON_REF
#            picks the ref). Reproducible, slowest to iterate.
#   local  - hecaton COPYd from the local build context. Use when you
#            need a self-contained image of an uncommitted change.
#   mount  - empty image. trainer-entrypoint.sh expects hecaton to be
#            bind-mounted at runtime (HECATON_SDK_PATH env var) and
#            `pip install -e`s it. Used by `make dev host=...` for
#            sub-second iteration without rebuild.
#
# INCLUDE_R2EGYM (true|false) controls whether the r2egym Action
# interface gets baked in. The trainer SDK only needs Action; we
# install --no-deps to skip r2egym's heavy transitive set.

ARG HECATON_SOURCE=git
ARG HECATON_REPO=https://github.com/IshiKura-a/hecaton.git
ARG HECATON_REF=main
ARG INCLUDE_R2EGYM=false
ARG R2E_GYM_REPO=https://github.com/R2E-Gym/R2E-Gym.git
ARG R2E_GYM_REF=main

# --- source stages: one per HECATON_SOURCE mode ---

FROM alpine/git:latest AS git-src
ARG HECATON_REPO
ARG HECATON_REF
RUN git clone --depth=1 --branch "$HECATON_REF" "$HECATON_REPO" /src

FROM alpine:3.20 AS local-src
COPY envs /src/envs
COPY examples/trainer-smoke /src/examples/trainer-smoke

FROM alpine:3.20 AS mount-src
RUN mkdir -p /src

FROM ${HECATON_SOURCE}-src AS hecaton-src

# Optional r2egym fetch. Always built; runtime stage decides whether
# to install it.
FROM alpine/git:latest AS r2egym-src
ARG R2E_GYM_REPO
ARG R2E_GYM_REF
RUN git clone --depth=1 --branch "$R2E_GYM_REF" "$R2E_GYM_REPO" /src

# --- runtime ---

FROM python:3.11-slim
ARG HECATON_SOURCE
ARG INCLUDE_R2EGYM

WORKDIR /app

# tailscale (userspace mode, no host TUN device needed). git stays
# installed so `pip install -e` against a mounted source tree works.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates iproute2 git \
 && curl -fsSL https://tailscale.com/install.sh | sh \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# Entrypoint always comes from the local build context — tiny and we
# want the same shim regardless of HECATON_SOURCE.
COPY envs/trainer-entrypoint.sh /usr/local/bin/trainer-entrypoint.sh
RUN chmod +x /usr/local/bin/trainer-entrypoint.sh

# Hecaton SDK + smoke scripts.
#   git/local: baked in.
#   mount:     deferred to runtime — trainer-entrypoint pip-installs
#              -e $HECATON_SDK_PATH and the script comes from the same
#              mount.
COPY --from=hecaton-src /src /tmp/hecaton-src
RUN if [ "$HECATON_SOURCE" != "mount" ]; then \
      pip install --no-cache-dir /tmp/hecaton-src/envs \
        && cp /tmp/hecaton-src/examples/trainer-smoke/*.py /app/ ; \
    fi \
 && rm -rf /tmp/hecaton-src

# r2egym Action interface (--no-deps to skip its heavy transitive set).
# Always installed when INCLUDE_R2EGYM=true, regardless of where hecaton
# itself comes from — r2egym is trainer-side, not part of the SDK.
COPY --from=r2egym-src /src /tmp/r2egym-src
RUN if [ "$INCLUDE_R2EGYM" = "true" ]; then \
      pip install --no-cache-dir --no-deps /tmp/r2egym-src; \
    fi \
 && rm -rf /tmp/r2egym-src

ENTRYPOINT ["/usr/local/bin/trainer-entrypoint.sh"]
CMD ["python", "/app/run_r2egym.py"]
