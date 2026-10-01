FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOUSING_CONFIG=/app/config.yaml \
    HOUSING_DATA_DIR=/app/data \
    HOUSING_SECRETS_DIR=/app/secrets

COPY pyproject.toml ./
COPY housing_bot ./housing_bot
RUN pip install --no-cache-dir . \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

CMD ["python", "-m", "housing_bot", "run"]
