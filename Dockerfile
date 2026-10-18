FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=10000
CMD gunicorn app:app --workers 1 --threads 8 --bind 0.0.0.0:${PORT} --timeout 60
