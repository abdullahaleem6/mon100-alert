FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mon100_alert.py .
RUN mkdir -p logs

CMD ["python", "-u", "mon100_alert.py"]
