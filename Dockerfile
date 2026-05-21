FROM python:3.11-slim

# Playwright system deps (Chromium needs these)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libx11-xcb1 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium (headless on server)
RUN playwright install chromium

COPY . .

# Persistent data lives in a mounted volume
RUN mkdir -p /data /data/video-tmp

ENV CARDSCANNER_PW_HEADLESS=0
ENV HOST=0.0.0.0
ENV PORT=8765

EXPOSE 8765

# Xvfb gives Chromium a virtual display so it runs in headed mode —
# Akamai's bot detection is much less aggressive against headed browsers.
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1280x1024x24", \
     "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
