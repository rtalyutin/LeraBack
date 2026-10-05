FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py *.sql starter_data.json ./
ENV PYTHONUNBUFFERED=1
ENV DATABASE_SCHEMA=lera
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import http.client, os; c = http.client.HTTPConnection('127.0.0.1', int(os.environ.get('PORT', '8080')), timeout=5); c.request('GET', '/livez'); s = c.getresponse().status; c.close(); raise SystemExit(0 if 200 <= s < 300 else 1)"]
CMD ["python", "app.py"]
