FROM python:3.11-slim

# Install system dependencies (MediaInfo CLI only — screenshots use in-process PyAV)
RUN apt-get update && apt-get install -y \
    mediainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
