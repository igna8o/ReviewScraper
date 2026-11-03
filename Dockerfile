FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY reviewscraper ./reviewscraper
RUN pip install --no-cache-dir .

ENV REVIEWSCRAPER_DB=/data/reviewscraper.db
VOLUME ["/data"]
EXPOSE 8000

CMD ["reviewscraper", "serve", "--host", "0.0.0.0", "--port", "8000"]
