#!/usr/bin/env python3
"""
Fiber Node Prometheus Exporter

Exports metrics from a Fiber node RPC and CKB Indexer for Prometheus scraping.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Set

import requests
from prometheus_client import start_http_server
from prometheus_client.core import GaugeMetricFamily, REGISTRY

from ckb_addr import decode_ckb_address

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("fiber_exporter")

SHANNONS_PER_CKB = 1e8


def _hex_to_int(value: str) -> int:
    """Convert a hex string like '0x3' to an integer."""
    return int(value, 16)


def _hex_to_ckb(value: str) -> float:
    """Convert a Shannon hex string to CKB float."""
    return _hex_to_int(value) / SHANNONS_PER_CKB


def _rpc_call(
    url: str,
    method: str,
    params: list,
    timeout: int = 10,
    auth_token: Optional[str] = None,
) -> Any:
    """Make a JSON-RPC 2.0 call and return the result field."""
    payload = {"id": 1, "jsonrpc": "2.0", "method": method, "params": params}
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data and data["error"] is not None:
        raise RuntimeError(f"RPC error for {method}: {data['error']}")
    return data.get("result")


def _channel_fingerprint(ch: Dict, peer_online: bool) -> tuple:
    """Compute a fingerprint tuple from channel state fields."""
    state = ch.get("state", {})
    state_name = state.get("state_name", "")
    return (
        state_name,
        ch.get("local_balance", "0x0"),
        ch.get("remote_balance", "0x0"),
        ch.get("offered_tlc_balance", "0x0"),
        ch.get("received_tlc_balance", "0x0"),
        len(ch.get("pending_tlcs", [])),
        ch.get("enabled", False),
        peer_online,
    )


class FiberCollector:
    """Custom Prometheus collector for Fiber node metrics."""

    def __init__(
        self,
        fiber_rpc_url: str,
        ckb_rpc_url: str,
        ckb_address: str,
        node_name: str,
        state_file: str,
        graph_scrape_interval: int,
        fiber_rpc_token: Optional[str] = None,
    ):
        self.fiber_rpc_url = fiber_rpc_url
        self.ckb_rpc_url = ckb_rpc_url
        self.ckb_address = ckb_address
        self.node_name = node_name
        self.state_file = state_file
        self.graph_scrape_interval = graph_scrape_interval
        self.fiber_rpc_token = fiber_rpc_token
        logger.info(
            "Fiber RPC auth: %s",
            "enabled" if fiber_rpc_token else "disabled",
        )

        # Decode CKB address once at startup
        self.lock_script = decode_ckb_address(ckb_address)
        logger.info("Decoded CKB address: %s", self.lock_script)

        # Channel last-seen state: {channel_id: {"fingerprint": ..., "last_seen": float}}
        self._channel_state: Dict[str, Dict] = {}
        self._load_state()

        # Graph cache
        self._graph_cache: Dict[str, Any] = {
            "nodes_total": 0,
            "channels_total": 0,
            "total_capacity_ckb": 0.0,
        }
        self._graph_lock = threading.Lock()

        # Start background graph thread
        self._start_graph_thread()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    self._channel_state = json.load(f)
                logger.info("Loaded channel state from %s", self.state_file)
            except Exception as e:
                logger.warning("Failed to load state file: %s", e)
                self._channel_state = {}

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self._channel_state, f)
        except Exception as e:
            logger.warning("Failed to save state file: %s", e)

    # ------------------------------------------------------------------
    # Background graph scraping
    # ------------------------------------------------------------------

    def _start_graph_thread(self):
        t = threading.Thread(target=self._graph_loop, daemon=True)
        t.start()

    def _graph_loop(self):
        while True:
            try:
                self._refresh_graph()
            except Exception as e:
                logger.warning("Graph refresh failed: %s", e)
            time.sleep(self.graph_scrape_interval)

    def _fetch_all_paginated(self, method: str, result_key: str) -> List[Dict]:
        """Fetch all pages from a paginated Fiber RPC method."""
        items = []
        cursor = None
        while True:
            params: Dict = {}
            if cursor is not None:
                params["after"] = cursor
            params["limit"] = "0x64"
            result = _rpc_call(
                self.fiber_rpc_url, method, [params], auth_token=self.fiber_rpc_token
            )
            if result is None:
                break
            batch = result.get(result_key, [])
            items.extend(batch)
            last_cursor = result.get("last_cursor", "0x")
            # Stop when no more pages (empty cursor or no new items)
            if not batch or last_cursor == cursor or last_cursor == "0x" or last_cursor == "":
                break
            cursor = last_cursor
        return items

    def _refresh_graph(self):
        nodes = self._fetch_all_paginated("graph_nodes", "nodes")
        channels = self._fetch_all_paginated("graph_channels", "channels")

        total_capacity = 0.0
        for ch in channels:
            cap = ch.get("capacity")
            if cap:
                try:
                    total_capacity += _hex_to_ckb(cap)
                except (ValueError, TypeError):
                    pass

        with self._graph_lock:
            self._graph_cache = {
                "nodes_total": len(nodes),
                "channels_total": len(channels),
                "total_capacity_ckb": total_capacity,
            }
        logger.info(
            "Graph refreshed: %d nodes, %d channels, %.2f CKB capacity",
            len(nodes),
            len(channels),
            total_capacity,
        )

    # ------------------------------------------------------------------
    # RPC helpers
    # ------------------------------------------------------------------

    def _get_node_info(self) -> Optional[Dict]:
        return _rpc_call(self.fiber_rpc_url, "node_info", [], auth_token=self.fiber_rpc_token)

    def _get_channels(self) -> List[Dict]:
        result = _rpc_call(self.fiber_rpc_url, "list_channels", [{}], auth_token=self.fiber_rpc_token)
        return result.get("channels", []) if result else []

    def _get_peers(self) -> Set[str]:
        result = _rpc_call(self.fiber_rpc_url, "list_peers", [], auth_token=self.fiber_rpc_token)
        peers = result.get("peers", []) if result else []
        return {p["pubkey"] for p in peers if "pubkey" in p}

    def _get_ckb_balance(self) -> Optional[float]:
        try:
            result = _rpc_call(
                self.ckb_rpc_url,
                "get_cells_capacity",
                [
                    {
                        "script": {
                            "code_hash": self.lock_script["code_hash"],
                            "hash_type": self.lock_script["hash_type"],
                            "args": self.lock_script["args"],
                        },
                        "script_type": "lock",
                    }
                ],
            )
            if result and "capacity" in result:
                return _hex_to_ckb(result["capacity"])
        except Exception as e:
            logger.warning("CKB balance fetch failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Channel last-seen update
    # ------------------------------------------------------------------

    def _update_channel_last_seen(self, channels: List[Dict], online_peers: Set[str], now: float):
        seen_ids = set()
        updated = False

        for ch in channels:
            channel_id = ch.get("channel_id", "")
            if not channel_id:
                continue
            seen_ids.add(channel_id)
            pubkey = ch.get("pubkey", "")
            peer_online = pubkey in online_peers
            fp = _channel_fingerprint(ch, peer_online)
            existing = self._channel_state.get(channel_id)
            if existing is None or existing.get("fingerprint") != list(fp):
                state = ch.get("state", {})
                state_name = state.get("state_name", "")
                enabled_bool = ch.get("enabled", False)
                # Only update last_seen when channel is truly online
                if state_name == "ChannelReady" and enabled_bool and peer_online:
                    new_last_seen = now
                else:
                    # Fingerprint changed but channel is not online; preserve existing last_seen
                    new_last_seen = existing.get("last_seen", 0) if existing else 0
                self._channel_state[channel_id] = {
                    "fingerprint": list(fp),
                    "last_seen": new_last_seen,
                }
                updated = True

        # Prune channels not seen in 24h

        stale_threshold = now - 86400
        stale_ids = [
            cid
            for cid, v in self._channel_state.items()
            if cid not in seen_ids and v.get("last_seen", now) < stale_threshold
        ]
        for cid in stale_ids:
            del self._channel_state[cid]
            updated = True

        if updated:
            self._save_state()

    # ------------------------------------------------------------------
    # Prometheus collect()
    # ------------------------------------------------------------------

    def collect(self):
        node_name = self.node_name
        now = time.time()

        # ---- fiber_node_up ----
        node_up = GaugeMetricFamily(
            "fiber_node_up",
            "1 if Fiber RPC is reachable, 0 otherwise",
            labels=["node_name"],
        )

        try:
            node_info = self._get_node_info()
            if node_info is None:
                raise RuntimeError("node_info returned None")
            node_up.add_metric([node_name], 1)
        except Exception as e:
            logger.warning("Fiber RPC unreachable: %s", e)
            node_up.add_metric([node_name], 0)
            yield node_up
            # Still try wallet balance even if node is down
            yield from self._collect_wallet_metrics()
            yield from self._collect_graph_metrics()
            return

        yield node_up

        # ---- fiber_node_info ----
        node_info_metric = GaugeMetricFamily(
            "fiber_node_info",
            "Fiber node metadata info (version, commit, identity). Value is always 1.",
            labels=["node_name", "version", "commit_hash", "pubkey", "chain_hash"],
        )
        node_info_metric.add_metric(
            [
                node_name,
                node_info.get("version") or "",
                node_info.get("commit_hash") or "",
                node_info.get("pubkey") or node_info.get("node_id") or "",  # node_id fallback for older Fiber nodes
                node_info.get("chain_hash") or "",
            ],
            1,
        )
        yield node_info_metric

        # ---- node-level metrics ----
        peers_count = GaugeMetricFamily(
            "fiber_node_peers_count",
            "Number of connected peers",
            labels=["node_name"],
        )
        channel_count = GaugeMetricFamily(
            "fiber_node_channel_count",
            "Number of channels",
            labels=["node_name"],
        )
        peers_count.add_metric(
            [node_name], _hex_to_int(node_info.get("peers_count", "0x0"))
        )
        channel_count.add_metric(
            [node_name], _hex_to_int(node_info.get("channel_count", "0x0"))
        )
        yield peers_count
        yield channel_count

        # ---- channel + peer metrics ----
        try:
            channels = self._get_channels()
            online_peers = self._get_peers()
        except Exception as e:
            logger.warning("Failed to fetch channels/peers: %s", e)
            channels = []
            online_peers = set()

        self._update_channel_last_seen(channels, online_peers, now)

        labels = ["node_name", "channel_id", "pubkey"]

        local_bal = GaugeMetricFamily(
            "fiber_channel_local_balance_ckb",
            "Channel local balance in CKB",
            labels=labels,
        )
        remote_bal = GaugeMetricFamily(
            "fiber_channel_remote_balance_ckb",
            "Channel remote balance in CKB",
            labels=labels,
        )
        ch_enabled = GaugeMetricFamily(
            "fiber_channel_enabled",
            "1 if channel is enabled, 0 otherwise",
            labels=labels,
        )
        ch_online = GaugeMetricFamily(
            "fiber_channel_online",
            "1 if channel is truly usable (CHANNEL_READY + enabled + peer online), 0 otherwise",
            labels=labels,
        )
        ch_last_seen = GaugeMetricFamily(
            "fiber_channel_last_seen_timestamp",
            "Unix timestamp when channel was last fully online (CHANNEL_READY + enabled + peer connected)",
            labels=labels,
        )
        ch_status = GaugeMetricFamily(
            "fiber_channel_status",
            "Overall channel health: 2=Online (READY+enabled+peer online), 1=Pending (not READY), 0=Offline (READY but peer offline or disabled)",
            labels=labels,
        )

        total_local = 0.0
        total_remote = 0.0
        active_count = 0
        healthy_count = 0
        pending_count = 0

        for ch in channels:
            channel_id = ch.get("channel_id", "")
            pubkey = ch.get("pubkey", "")
            state = ch.get("state", {})
            state_name = state.get("state_name", "")
            lbl = [node_name, channel_id, pubkey]

            lb = _hex_to_ckb(ch.get("local_balance", "0x0"))
            rb = _hex_to_ckb(ch.get("remote_balance", "0x0"))
            enabled_bool = ch.get("enabled", False)
            enabled = 1 if enabled_bool else 0
            peer_online_bool = pubkey in online_peers

            if state_name == "ChannelReady":
                if enabled_bool and peer_online_bool:
                    status = 2
                    channel_online = 1
                else:
                    status = 0
                    channel_online = 0
            else:
                status = 1
                channel_online = 0

            local_bal.add_metric(lbl, lb)
            remote_bal.add_metric(lbl, rb)
            ch_enabled.add_metric(lbl, enabled)
            ch_online.add_metric(lbl, channel_online)
            ch_status.add_metric(lbl, status)

            # last_seen
            state_entry = self._channel_state.get(channel_id)
            if state_entry:
                ch_last_seen.add_metric(lbl, state_entry["last_seen"])
            else:
                ch_last_seen.add_metric(lbl, 0)

            if state_name == "ChannelReady":
                total_local += lb
                total_remote += rb
                active_count += 1
                if enabled_bool and peer_online_bool:
                    healthy_count += 1
            else:
                pending_count += 1

        yield local_bal
        yield remote_bal
        yield ch_enabled
        yield ch_online
        yield ch_last_seen
        yield ch_status

        # ---- aggregated ----
        local_total = GaugeMetricFamily(
            "fiber_channels_local_balance_total_ckb",
            "Total local balance across all channels in CKB",
            labels=["node_name"],
        )
        remote_total = GaugeMetricFamily(
            "fiber_channels_remote_balance_total_ckb",
            "Total remote balance across all channels in CKB",
            labels=["node_name"],
        )
        active_total = GaugeMetricFamily(
            "fiber_channels_active_total",
            "Count of CHANNEL_READY channels",
            labels=["node_name"],
        )
        healthy_total = GaugeMetricFamily(
            "fiber_channels_healthy_total",
            "Count of truly usable channels (CHANNEL_READY + enabled + peer online)",
            labels=["node_name"],
        )
        pending_total = GaugeMetricFamily(
            "fiber_channels_pending_total",
            "Count of channels not yet CHANNEL_READY",
            labels=["node_name"],
        )
        local_total.add_metric([node_name], total_local)
        remote_total.add_metric([node_name], total_remote)
        active_total.add_metric([node_name], active_count)
        healthy_total.add_metric([node_name], healthy_count)
        pending_total.add_metric([node_name], pending_count)
        yield local_total
        yield remote_total
        yield active_total
        yield healthy_total
        yield pending_total

        yield from self._collect_wallet_metrics()
        yield from self._collect_graph_metrics()

    def _collect_wallet_metrics(self):
        wallet_balance = GaugeMetricFamily(
            "fiber_wallet_ckb_balance",
            "CKB wallet balance in CKB units",
            labels=["node_name", "address"],
        )
        balance = self._get_ckb_balance()
        if balance is not None:
            wallet_balance.add_metric([self.node_name, self.ckb_address], balance)
        yield wallet_balance

    def _collect_graph_metrics(self):
        with self._graph_lock:
            cache = dict(self._graph_cache)

        graph_nodes = GaugeMetricFamily(
            "fiber_graph_nodes_total",
            "Total nodes in the Fiber network graph",
            labels=["node_name"],
        )
        graph_channels = GaugeMetricFamily(
            "fiber_graph_channels_total",
            "Total channels in the Fiber network graph",
            labels=["node_name"],
        )
        graph_capacity = GaugeMetricFamily(
            "fiber_graph_total_capacity_ckb",
            "Total capacity in the Fiber network graph in CKB",
            labels=["node_name"],
        )
        graph_nodes.add_metric([self.node_name], cache["nodes_total"])
        graph_channels.add_metric([self.node_name], cache["channels_total"])
        graph_capacity.add_metric([self.node_name], cache["total_capacity_ckb"])
        yield graph_nodes
        yield graph_channels
        yield graph_capacity


def main():
    fiber_rpc_url = os.environ.get("FIBER_RPC_URL", "http://127.0.0.1:8227")
    ckb_rpc_url = os.environ.get("CKB_RPC_URL", "https://mainnet.ckbapp.dev")
    ckb_address = os.environ.get("CKB_ADDRESS", "")
    exporter_port = int(os.environ.get("EXPORTER_PORT", "8222"))
    node_name = os.environ.get("NODE_NAME", "fiber-node-01")
    graph_scrape_interval = int(os.environ.get("GRAPH_SCRAPE_INTERVAL", "300"))
    state_file = os.environ.get("STATE_FILE", "state.json")
    fiber_rpc_token = os.environ.get("FIBER_RPC_TOKEN", "").strip() or None

    if not ckb_address:
        logger.error("CKB_ADDRESS environment variable is required but not set.")
        raise SystemExit(1)

    logger.info("Configuration:")
    logger.info("  FIBER_RPC_URL=%s", fiber_rpc_url)
    logger.info("  FIBER_RPC_TOKEN=%s", "enabled" if fiber_rpc_token else "disabled")
    logger.info("  CKB_RPC_URL=%s", ckb_rpc_url)
    logger.info("  CKB_ADDRESS=%s", ckb_address)
    logger.info("  EXPORTER_PORT=%d", exporter_port)
    logger.info("  NODE_NAME=%s", node_name)
    logger.info("  GRAPH_SCRAPE_INTERVAL=%d", graph_scrape_interval)
    logger.info("  STATE_FILE=%s", state_file)

    collector = FiberCollector(
        fiber_rpc_url=fiber_rpc_url,
        ckb_rpc_url=ckb_rpc_url,
        ckb_address=ckb_address,
        node_name=node_name,
        state_file=state_file,
        graph_scrape_interval=graph_scrape_interval,
        fiber_rpc_token=fiber_rpc_token,
    )
    REGISTRY.register(collector)

    start_http_server(exporter_port)
    logger.info("Exporter started on port %d", exporter_port)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
