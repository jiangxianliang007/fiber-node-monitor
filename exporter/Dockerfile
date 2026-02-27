FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fiber_exporter.py .
COPY ckb_addr.py .

ENV FIBER_RPC_URL=http://127.0.0.1:8227 \
    CKB_RPC_URL=https://mainnet.ckbapp.dev \
    EXPORTER_PORT=8200 \
    GRAPH_SCRAPE_INTERVAL=300 \
    STATE_FILE=/data/state.json

VOLUME ["/data"]
EXPOSE 8200

CMD ["python", "fiber_exporter.py"]
