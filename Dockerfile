FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser (needed for Miruro Playwright fallback)
RUN python3 -m playwright install chromium \
    && rm -rf /root/.cache/ms-playwright/chromium-*/chrome-linux64/locales \
    && rm -rf /var/lib/apt/lists/*

COPY src/ ./src/
COPY consumet_api/ ./consumet_api/
COPY tests/ ./tests/
COPY start.sh .
RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000 4000

CMD ./start.sh

