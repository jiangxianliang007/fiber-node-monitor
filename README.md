# Fiber Node Monitor

A complete Prometheus/Grafana monitoring solution for [Fiber](https://github.com/nervosnetwork/fiber) Lightning Network nodes on CKB.

## Architecture

```
Fiber Node RPC ─┐
CKB Indexer RPC ┼─► fiber_exporter.py ──► Prometheus ──► Grafana
                │        :8200/metrics
```

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/jiangxianliang007/fiber-node-monitor.git
cd fiber-node-monitor
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your actual values
```

### 3. Run the exporter

**Option A — bare Python:**

```bash
pip install -r exporter/requirements.txt
export $(cat .env | xargs)
python exporter/fiber_exporter.py
```

**Option B — Docker:**

```bash
docker build -t fiber-exporter exporter/
docker run -d \
  --env-file .env \
  -v /path/to/data:/data \
  -p 8200:8200 \
  fiber-exporter
```

### 4. Add scrape target to Prometheus

Add the following to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: fiber_node
    static_configs:
      - targets: ['<exporter-host>:8200']
```

### 5. Import the Grafana dashboard

1. Open Grafana → **Dashboards → Import**
2. Upload `grafana/fiber-node-dashboard.json`
3. Select your Prometheus datasource

### 6. Import alert rules into Prometheus

Add or include `prometheus/alerts.yml` in your Prometheus configuration:

```yaml
rule_files:
  - /path/to/fiber-node-monitor/prometheus/alerts.yml
```

---

## Configuration

| Environment Variable    | Default                          | Description                                  |
|-------------------------|----------------------------------|----------------------------------------------|
| `FIBER_RPC_URL`         | `http://127.0.0.1:8227`          | Fiber node JSON-RPC endpoint                 |
| `CKB_RPC_URL`           | `https://mainnet.ckbapp.dev`     | CKB/Indexer RPC endpoint                     |
| `CKB_ADDRESS`           | *(required)*                     | Your CKB wallet address (bech32 or bech32m)  |
| `EXPORTER_PORT`         | `8200`                           | HTTP port for `/metrics`                     |
| `GRAPH_SCRAPE_INTERVAL` | `300`                            | Seconds between network graph scrapes        |
| `STATE_FILE`            | `state.json`                     | Path to channel last-seen state file         |

---

## Metrics Reference

### Node-level

| Metric | Type | Description |
|--------|------|-------------|
| `fiber_node_up` | Gauge | 1 if RPC is reachable, 0 otherwise |
| `fiber_node_peers_count` | Gauge | Number of connected peers |
| `fiber_node_channel_count` | Gauge | Total channel count from node_info |

### Wallet

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `fiber_wallet_ckb_balance` | Gauge | `address` | CKB balance in CKB units |

### Per-channel (CHANNEL_READY only)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `fiber_channel_local_balance_ckb` | Gauge | `channel_id`, `peer_id` | Local balance in CKB |
| `fiber_channel_remote_balance_ckb` | Gauge | `channel_id`, `peer_id` | Remote balance in CKB |
| `fiber_channel_enabled` | Gauge | `channel_id`, `peer_id` | 1=enabled, 0=disabled |
| `fiber_channel_last_seen_timestamp` | Gauge | `channel_id`, `peer_id` | Unix timestamp of last state change |

### Aggregated channels

| Metric | Type | Description |
|--------|------|-------------|
| `fiber_channels_local_balance_total_ckb` | Gauge | Sum of local balances |
| `fiber_channels_remote_balance_total_ckb` | Gauge | Sum of remote balances |
| `fiber_channels_active_total` | Gauge | Count of CHANNEL_READY channels |

### Network graph

| Metric | Type | Description |
|--------|------|-------------|
| `fiber_graph_nodes_total` | Gauge | Total nodes in the network graph |
| `fiber_graph_channels_total` | Gauge | Total channels in the network graph |
| `fiber_graph_total_capacity_ckb` | Gauge | Sum of all channel capacities in CKB |

---

## Alert Rules

| Alert | Severity | Condition | For |
|-------|----------|-----------|-----|
| `FiberNodeDown` | critical | `fiber_node_up == 0` | 2m |
| `FiberWalletBalanceLow` | warning | `fiber_wallet_ckb_balance < 100` | 5m |
| `FiberNoPeers` | warning | `fiber_node_peers_count == 0` | 5m |
| `FiberChannelStale` | warning | channel inactive > 24h | 10m |
| `FiberChannelDisabled` | warning | `fiber_channel_enabled == 0` | 10m |

---

## File Structure

```
fiber-node-monitor/
├── exporter/
│   ├── fiber_exporter.py      # Main exporter (custom Prometheus collector)
│   ├── ckb_addr.py            # CKB address bech32/bech32m decoder
│   ├── requirements.txt       # Python dependencies
│   └── Dockerfile             # Docker image
├── grafana/
│   └── fiber-node-dashboard.json   # Importable Grafana dashboard
├── prometheus/
│   └── alerts.yml             # Prometheus alert rules
├── README.md
├── .env.example
├── LICENSE
└── .gitignore
```

---

## Screenshots

*(Add your own screenshots here after deployment)*

---

## License

MIT — see [LICENSE](LICENSE).