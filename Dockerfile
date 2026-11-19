FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
COPY pyproject.toml ./
COPY santa ./santa
COPY assets ./assets
RUN pip install --no-cache-dir .
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD python -m santa.healthcheck
CMD ["secret-santa"]
