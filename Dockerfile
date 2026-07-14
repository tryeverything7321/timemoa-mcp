FROM python:3.12-slim

WORKDIR /app

COPY mcp-server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp-server/submission_server.py ./submission_server.py

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

ENV COORDINATION_DB_PATH=/data/coordination.db
ENV COORDINATION_ROOM_TTL_DAYS=7

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health', timeout=2)"

USER appuser

CMD ["python", "submission_server.py"]
