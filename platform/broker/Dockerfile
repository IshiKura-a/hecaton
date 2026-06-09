FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml broker.py /app/

RUN pip install --no-cache-dir .

# Stamped at build time so `docker inspect <image>` can identify which
# commit produced any running broker pod. Defaults keep CI / hand
# builds working without extra flags.
ARG BUILD_GIT_SHA=unknown
ARG BUILD_GIT_DIRTY=unknown
ARG BUILD_TIMESTAMP=unknown
LABEL hecaton.git.sha="$BUILD_GIT_SHA" \
      hecaton.git.dirty="$BUILD_GIT_DIRTY" \
      hecaton.build.timestamp="$BUILD_TIMESTAMP"

ENV PYTHONUNBUFFERED=1 \
    HECATON_NAMESPACE=hecaton-sandboxes

EXPOSE 8000

CMD ["uvicorn", "broker:app", "--host", "0.0.0.0", "--port", "8000"]
