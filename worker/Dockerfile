FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py ssh_client.py parsers.py ./
RUN useradd --uid 10001 --create-home netauto && mkdir -p /data && chown netauto /data
USER netauto

CMD ["python", "worker.py"]
