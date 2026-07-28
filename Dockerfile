FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libexpat1 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libxkbcommon0 libpango-1.0-0 libcairo2 \
    libasound2 libatspi2.0-0 libwayland-client0 libwayland-cursor0 \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN python3 -m playwright install chromium && \
    python3 -m playwright install-deps chromium 2>/dev/null; true

COPY checker.py .

ENTRYPOINT ["python3", "checker.py"]
