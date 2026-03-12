FROM python:3.11-slim

# Install aria2c
RUN apt-get update && \
    apt-get install -y --no-install-recommends aria2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Create download directory
RUN mkdir -p /downloads

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
