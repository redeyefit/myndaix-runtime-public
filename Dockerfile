# The runtime service image (used by docker-compose.yml). The same `pip install -e .`
# the CI workflow runs green on Linux/Python 3.12.
FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .

ENV PYTHONPATH=/app/src

# Default: run the worker pool. Override `command:` in compose to run the HTTP API
# (uvicorn runtime.api:app) instead.
CMD ["python", "-m", "runtime.serve", "--size", "4"]
