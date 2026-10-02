FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py *.sql starter_data.json ./
ENV PYTHONUNBUFFERED=1
ENV DATABASE_SCHEMA=lera
EXPOSE 8080
CMD ["python", "app.py"]
