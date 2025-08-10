# Minimal Python image
FROM python:3.11-slim

WORKDIR /app

# System deps (optional; extend if your code needs more)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copy and install Python deps first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . /app

# Default port for optional healthcheck server
ENV HEALTHCHECK_PORT=8080
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "webhook_sniper_bot.py"]
