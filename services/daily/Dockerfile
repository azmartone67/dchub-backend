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

# force headless backend and pre-warm matplotlib font cache so first
# request doesn't blow past Railway's 30s healthcheck window
ENV MPLBACKEND=Agg
ENV PYTHONUNBUFFERED=1
RUN python -c "import matplotlib.pyplot as plt; fig, ax = plt.subplots(); fig.savefig('/tmp/warmup.png'); print('matplotlib warm')"

COPY . .

ENV PORT=8080
EXPOSE 8080
# exec form to avoid subshell issues while still expanding $PORT via -c
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --log-level info"]
