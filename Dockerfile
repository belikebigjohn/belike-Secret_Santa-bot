FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY santa ./santa
COPY assets ./assets
RUN pip install --no-cache-dir .
CMD ["secret-santa"]
