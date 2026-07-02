FROM python:3.12-slim

# Install system dependencies: FFmpeg (required by yt-dlp for audio extraction)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create downloads directory for local fallback
RUN mkdir -p /app/downloads

EXPOSE 8000

# Default: run the web server
CMD ["bash", "scripts/start-web.sh"]
