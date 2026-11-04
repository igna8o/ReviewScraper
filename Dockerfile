FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY reviewscraper ./reviewscraper
RUN pip install --no-cache-dir .

ENV REVIEWSCRAPER_DB=/data/reviewscraper.db
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

# Cloud Run, Render, Railway... set PORT; plain Docker uses 8000.
CMD ["sh", "-c", "exec reviewscraper serve --host 0.0.0.0 --port ${PORT:-8000}"]
