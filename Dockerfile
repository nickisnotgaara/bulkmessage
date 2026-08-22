FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    INSIDE_DOCKER=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bulkmessage/ ./bulkmessage/
RUN mkdir -p /app/data

ENV PYTHONPATH=/app

# Default command; override in docker-compose.yml per service
CMD ["python", "-m", "bulkmessage.sender"]
