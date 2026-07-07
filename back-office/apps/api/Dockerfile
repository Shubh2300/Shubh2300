# Back Office API (FastAPI). Runs in the office-machine docker-compose stack.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/apps/api

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . /app/apps/api

# The action-registry package and db schema are mounted read-only at runtime
# (see docker-compose.yml). PYTHONPATH lets `import config`, `import db`, etc.
ENV PYTHONPATH=/app/apps/api

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
