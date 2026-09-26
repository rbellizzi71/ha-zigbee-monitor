FROM python:3.13.7-alpine3.22
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir paho-mqtt==2.1.0
WORKDIR /app
COPY monitor.py /app/monitor.py
CMD ["python3", "/app/monitor.py"]
