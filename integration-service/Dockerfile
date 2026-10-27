FROM python:3.12.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

USER nobody
EXPOSE 8000
# Schema first, then the service. A single instance is assumed (see DECISIONS.md).
CMD ["sh", "-c", "python -m integration_service.migrate && exec uvicorn integration_service.main:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log"]
