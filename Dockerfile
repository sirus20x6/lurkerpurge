FROM python:3.13-slim
RUN pip install --no-cache-dir "discord.py>=2.4,<3"
RUN install -d -o 1000 -g 1000 /data
WORKDIR /app
COPY bot.py .
ENV PYTHONUNBUFFERED=1 PURGE_DB=/data/purge.db PURGE_LOG=/data/purge_log.csv
USER 1000:1000
CMD ["python", "bot.py"]
