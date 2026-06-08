FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml broker.py /app/

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    HECATON_NAMESPACE=hecaton-sandboxes

EXPOSE 8000

CMD ["uvicorn", "broker:app", "--host", "0.0.0.0", "--port", "8000"]
