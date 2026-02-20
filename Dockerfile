FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends gcc curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data /app/keys
RUN chmod +x /app/entrypoint.sh

EXPOSE 5052
ENTRYPOINT ["/app/entrypoint.sh"]
