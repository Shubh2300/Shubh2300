# Back Office Temporal worker. Executes approved ActionIntents via the bridge.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/worker

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . /app/worker

ENV PYTHONPATH=/app/worker

CMD ["python", "main.py"]
