#!/usr/bin/env python3
"""
Fiber Node Prometheus Exporter

Exposes metrics for a Fiber Lightning Network node:
  - Node status (up/down, peers, channels)
  - CKB wallet balance
  - Per-channel balances and last-seen timestamps
  - Network graph statistics (nodes, channels, total capacity)

Configuration via environment variables:
  FIBER_RPC_URL        (default: http://127.0.0.1:8227)
  CKB_RPC_URL          (default: https://mainnet.ckbapp.dev)
  CKB_ADDRESS          (required)
  EXPORTER_PORT        (default: 8200)
  GRAPH_SCRAPE_INTERVAL (default: 300 seconds)
  STATE_FILE           (default: state.json)
"""

import json
import logging
import os
import sys
import threading
import time
from typing import Any

import requests
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily

from ckb_addr import decode_ckb_address

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

FIBER_RPC_URL = os.environ.get("FIBER_RPC_URL", "http://127.0.0.1:8227")
CKB_RPC_URL = os.environ.get("CKB_RPC_URL", "https://mainnet.ckbapp.dev")
CKB_ADDRESS = os.environ.get("CKB_ADDRESS", "")
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "8200"))
GRAPH_SCRAPE_INTERVAL = int(os.environ.get("GRAPH_SCRAPE_INTERVAL", "300"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

_RPC_TIMEOUT = 10  # seconds


# ── Helpers ──────────────────────────────────────────────────────────────────

def _rpc(url: str, method: str, params: list) -> Any:
    """Execute a JSON-RPC 2.0 call and return the result field."""
    payload = {"id": 1, "jsonrpc": "2.0", "method": method, "params": params}
    resp = requests.post(url, json=payload, timeout=_RPC_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error from {method}: {data['error']}")
    return data["result"]


def _hex(value: str) -> int:
    """Parse a hex string like '0x3a' into an integer."""
    return int(value, 16)


def _ckb(shannons_hex: str) -> float:
    """Convert hex shannons to CKB float."""
    return _hex(shannons_hex) / 1e8


# ── State persistence ────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except OSError as exc:
        log.warning("Could not save state file %s: %s", path, exc)


# ── Channel fingerprint ──────────────────────────────────────────────────────

def _channel_fingerprint(ch: dict) -> str:
    state_name = ch.get("state", {}).get("state_name", "")
    return json.dumps(
        [
            state_name,
            ch.get("local_balance", "0x0"),
            ch.get("remote_balance", "0x0"),
            ch.get("offered_tlc_balance", "0x0"),
            ch.get("received_tlc_balance", "0x0"),
            len(ch.get("pending_tlcs", [])),
            ch.get("enabled", False),
        ],
        separators=(",", ":"),
    )


# ── Graph scraping (background thread) ──────────────────────────────────────

class GraphCache:
    """Thread-safe cache for network graph metrics."""

    def __init__(self):
        self._lock = threading.Lock()
        self._nodes_total = 0
        self._channels_total = 0
        self._total_capacity_ckb = 0.0

    def update(self, nodes: int, channels: int, capacity_ckb: float) -> None:
        with self._lock:
            self._nodes_total = nodes
            self._channels_total = channels
            self._total_capacity_ckb = capacity_ckb

    def snapshot(self) -> tuple[int, int, float]:
        with self._lock:
            return self._nodes_total, self._channels_total, self._total_capacity_ckb


_graph_cache = GraphCache()


def _paginate(method: str, result_key: str) -> list:
    """Fetch all pages from a Fiber paginated RPC method."""
    items = []
    params: dict = {}
    while True:
        result = _rpc(FIBER_RPC_URL, method, [params])
        batch = result.get(result_key, [])
        items.extend(batch)
        if not batch:
            break
        last_cursor = result.get("last_cursor", "")
        if not last_cursor:
            break
        params = {"limit": "0x64", "after": last_cursor}
    return items


def _scrape_graph() -> None:
    """Scrape graph_nodes and graph_channels; update _graph_cache."""
    try:
        nodes = _paginate("graph_nodes", "nodes")
        channels = _paginate("graph_channels", "channels")
        total_capacity = sum(
            _hex(ch.get("capacity", "0x0")) for ch in channels
        ) / 1e8
        _graph_cache.update(len(nodes), len(channels), total_capacity)
        log.info(
            "Graph scraped: %d nodes, %d channels, %.2f CKB capacity",
            len(nodes),
            len(channels),
            total_capacity,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Graph scrape failed: %s", exc)


def _graph_scrape_loop() -> None:
    """Background loop that scrapes graph data every GRAPH_SCRAPE_INTERVAL seconds."""
    while True:
        _scrape_graph()
        time.sleep(GRAPH_SCRAPE_INTERVAL)


# ── Custom Prometheus Collector ──────────────────────────────────────────────

class FiberCollector:
    """
    Custom Prometheus collector that scrapes the Fiber node on each /metrics
    request. Uses a custom Collector (not global Gauge objects) so that
    disappeared channel labels are automatically removed.
    """

    def __init__(self, ckb_lock_script: dict):
        self._lock_script = ckb_lock_script
        self._channel_last_seen: dict[str, dict] = {}  # channel_id → {ts, fp}
        self._state_lock = threading.Lock()
        state = _load_state(STATE_FILE)
        with self._state_lock:
            self._channel_last_seen = state

    def _collect_node_metrics(self):
        """Yield fiber_node_up, fiber_node_peers_count, fiber_node_channel_count."""
        node_up = GaugeMetricFamily(
            "fiber_node_up",
            "1 if the Fiber node RPC is reachable, 0 otherwise",
        )
        peers_metric = GaugeMetricFamily(
            "fiber_node_peers_count",
            "Number of connected peers",
        )
        channel_count_metric = GaugeMetricFamily(
            "fiber_node_channel_count",
            "Total number of channels reported by the node",
        )
        try:
            info = _rpc(FIBER_RPC_URL, "node_info", [])
            node_up.add_metric([], 1.0)
            peers_metric.add_metric([], float(_hex(info.get("peers_count", "0x0"))))
            channel_count_metric.add_metric([], float(_hex(info.get("channel_count", "0x0"))))
        except Exception as exc:  # noqa: BLE001
            log.warning("node_info failed: %s", exc)
            node_up.add_metric([], 0.0)
            peers_metric.add_metric([], 0.0)
            channel_count_metric.add_metric([], 0.0)
        yield node_up
        yield peers_metric
        yield channel_count_metric

    def _collect_wallet_metrics(self):
        """Yield fiber_wallet_ckb_balance."""
        wallet_metric = GaugeMetricFamily(
            "fiber_wallet_ckb_balance",
            "CKB wallet balance in CKB units",
            labels=["address"],
        )
        try:
            result = _rpc(
                CKB_RPC_URL,
                "get_cells_capacity",
                [{"script": self._lock_script, "script_type": "lock"}],
            )
            capacity_hex = result.get("capacity", "0x0") if result else "0x0"
            wallet_metric.add_metric([CKB_ADDRESS], _ckb(capacity_hex))
        except Exception as exc:  # noqa: BLE001
            log.warning("get_cells_capacity failed: %s", exc)
            wallet_metric.add_metric([CKB_ADDRESS], 0.0)
        yield wallet_metric

    def _collect_channel_metrics(self):
        """Yield per-channel and aggregated channel metrics."""
        local_bal_metric = GaugeMetricFamily(
            "fiber_channel_local_balance_ckb",
            "Local balance of a CHANNEL_READY channel in CKB",
            labels=["channel_id", "peer_id"],
        )
        remote_bal_metric = GaugeMetricFamily(
            "fiber_channel_remote_balance_ckb",
            "Remote balance of a CHANNEL_READY channel in CKB",
            labels=["channel_id", "peer_id"],
        )
        enabled_metric = GaugeMetricFamily(
            "fiber_channel_enabled",
            "1 if the channel is enabled, 0 otherwise",
            labels=["channel_id", "peer_id"],
        )
        last_seen_metric = GaugeMetricFamily(
            "fiber_channel_last_seen_timestamp",
            "Unix timestamp of when channel state last changed",
            labels=["channel_id", "peer_id"],
        )
        agg_local = GaugeMetricFamily(
            "fiber_channels_local_balance_total_ckb",
            "Sum of local balances across all CHANNEL_READY channels in CKB",
        )
        agg_remote = GaugeMetricFamily(
            "fiber_channels_remote_balance_total_ckb",
            "Sum of remote balances across all CHANNEL_READY channels in CKB",
        )
        agg_active = GaugeMetricFamily(
            "fiber_channels_active_total",
            "Count of CHANNEL_READY channels",
        )

        total_local = 0.0
        total_remote = 0.0
        active_count = 0

        try:
            result = _rpc(FIBER_RPC_URL, "list_channels", [{}])
            channels = result.get("channels", []) if result else []
            now = time.time()
            new_state: dict[str, dict] = {}

            for ch in channels:
                if ch.get("state", {}).get("state_name", "") != "CHANNEL_READY":
                    continue
                cid = ch.get("channel_id", "")
                pid = ch.get("peer_id", "")
                local_bal = _ckb(ch.get("local_balance", "0x0"))
                remote_bal = _ckb(ch.get("remote_balance", "0x0"))
                enabled = 1.0 if ch.get("enabled", False) else 0.0
                fp = _channel_fingerprint(ch)
                with self._state_lock:
                    prev = self._channel_last_seen.get(cid, {})
                ts = prev.get("ts", now) if prev.get("fp") == fp else now
                new_state[cid] = {"ts": ts, "fp": fp}

                local_bal_metric.add_metric([cid, pid], local_bal)
                remote_bal_metric.add_metric([cid, pid], remote_bal)
                enabled_metric.add_metric([cid, pid], enabled)
                last_seen_metric.add_metric([cid, pid], ts)
                total_local += local_bal
                total_remote += remote_bal
                active_count += 1

            with self._state_lock:
                self._channel_last_seen = new_state
            _save_state(STATE_FILE, new_state)

        except Exception as exc:  # noqa: BLE001
            log.warning("list_channels failed: %s", exc)

        yield local_bal_metric
        yield remote_bal_metric
        yield enabled_metric
        yield last_seen_metric
        agg_local.add_metric([], total_local)
        agg_remote.add_metric([], total_remote)
        agg_active.add_metric([], float(active_count))
        yield agg_local
        yield agg_remote
        yield agg_active

    def _collect_graph_metrics(self):
        """Yield network graph metrics from the background cache."""
        nodes_total, channels_total, capacity_ckb = _graph_cache.snapshot()
        graph_nodes = GaugeMetricFamily(
            "fiber_graph_nodes_total", "Total nodes in the Fiber network graph"
        )
        graph_channels = GaugeMetricFamily(
            "fiber_graph_channels_total",
            "Total channels in the Fiber network graph",
        )
        graph_capacity = GaugeMetricFamily(
            "fiber_graph_total_capacity_ckb",
            "Total capacity of all graph channels in CKB",
        )
        graph_nodes.add_metric([], float(nodes_total))
        graph_channels.add_metric([], float(channels_total))
        graph_capacity.add_metric([], capacity_ckb)
        yield graph_nodes
        yield graph_channels
        yield graph_capacity

    def collect(self):
        yield from self._collect_node_metrics()
        yield from self._collect_wallet_metrics()
        yield from self._collect_channel_metrics()
        yield from self._collect_graph_metrics()


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    if not CKB_ADDRESS:
        log.error("CKB_ADDRESS environment variable is required but not set.")
        sys.exit(1)

    try:
        lock_script = decode_ckb_address(CKB_ADDRESS)
    except ValueError as exc:
        log.error("Failed to decode CKB_ADDRESS %r: %s", CKB_ADDRESS, exc)
        sys.exit(1)

    log.info("Fiber RPC:  %s", FIBER_RPC_URL)
    log.info("CKB RPC:    %s", CKB_RPC_URL)
    log.info("CKB addr:   %s", CKB_ADDRESS)
    log.info("Lock script: %s", lock_script)
    log.info("Exporter port: %d", EXPORTER_PORT)
    log.info("Graph scrape interval: %ds", GRAPH_SCRAPE_INTERVAL)

    # Register custom collector
    REGISTRY.register(FiberCollector(lock_script))

    # Start background graph scraper
    t = threading.Thread(target=_graph_scrape_loop, daemon=True)
    t.start()

    # Start HTTP server
    start_http_server(EXPORTER_PORT)
    log.info("Exporter running on port %d — /metrics", EXPORTER_PORT)

    # Block forever
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
