FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
# Keep pending.sqlite3 on a volume so undo/pending survive restarts
ENV PENDING_DB_PATH=/data/pending.sqlite3
VOLUME /data
CMD ["python", "bot.py"]
