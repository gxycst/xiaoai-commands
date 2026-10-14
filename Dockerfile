FROM python:3.11-alpine
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY server.py /app/server.py
COPY restore.py /app/restore.py
COPY static/ /app/static/
EXPOSE 9528
CMD ["python", "/app/server.py"]
