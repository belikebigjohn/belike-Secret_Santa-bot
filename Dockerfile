FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY santa ./santa
RUN pip install --no-cache-dir .
CMD ["secret-santa"]
