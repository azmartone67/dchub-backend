FROM python:3.12-slim

# system deps for matplotlib font rasterisation
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core fonts-dejavu-extra libfreetype6 libpng16-16 \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
