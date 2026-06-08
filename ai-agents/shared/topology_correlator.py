"""
Topology-aware incident correlation for cross-device failure grouping.

Implements the cross-device correlation layer from docs/autonomous-agent-framework.md.
When multiple alerts fire simultaneously, this module walks the physical topology
graph (from Nautobot) to determine which device is the root cause and which are
downstream symptoms.

Usage:
    correlator = TopologyCorrelator(nautobot_url, nautobot_token, cache_ttl=60)

    root = correlator.find_root_cause(
        alert_device="leaf1",
        existing_rca_devices=["spine1"],   # from task_store
    )
    # Returns "spine1" if leaf1 is topologically downstream of spine1

    blast = correlator.get_blast_radius("spine1", hops=2)
    # Returns ["leaf1", "leaf2", "leaf3", ...]
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

import httpx

logger = logging.getLogger(__name__)


class TopologyCorrelator:
    """
    Builds and caches an adjacency graph from Nautobot cable data.
    Provides root-cause inference and blast-radius calculation.

    The graph is undirected (cables connect both ends equally). Root cause
    inference uses the heuristic: if an upstream device (closer to the
    distribution/core layer) already has an active RCA, a downstream alert
    is likely a symptom rather than an independent root cause.
    """

    def __init__(
        self,
        nautobot_url: str,
        nautobot_token: str,
        cache_ttl: int = 60,
        blast_radius_hops: int = 2,
    ) -> None:
        self._url         = nautobot_url.rstrip("/")
        self._token       = nautobot_token
        self._cache_ttl   = cache_ttl
        self._hops        = blast_radius_hops
        self._graph: dict[str, set[str]] = {}
        self._graph_ts: float = 0.0
        self._lock        = threading.Lock()

    # ── public interface ──────────────────────────────────────────────────────

    def find_root_cause(
        self,
        alert_device: str,
        existing_rca_devices: list[str],
        alertname: str = "",
    ) -> str | None:
        """
        Return the most likely root cause device for alert_device, considering which
        devices already have active RCAs. Returns None if alert_device is itself the
        root cause (no upstream device with an active RCA found).
        """
        if not existing_rca_devices:
            return None

        graph = self._get_graph()
        if not graph:
            return None

        # BFS outward from alert_device; find the first neighbour that has an active RCA.
        # "First" = fewest hops away.
        visited = {alert_device}
        queue: deque[tuple[str, int]] = deque()
        for neighbour in graph.get(alert_device, set()):
            if neighbour not in visited:
                queue.append((neighbour, 1))
                visited.add(neighbour)

        while queue:
            device, depth = queue.popleft()
            if device in existing_rca_devices:
                logger.debug(
                    "TopologyCorrelator: %s is downstream of root-cause %s (depth=%d)",
                    alert_device, device, depth,
                )
                return device
            if depth < self._hops + 1:
                for neighbour in graph.get(device, set()):
                    if neighbour not in visited:
                        visited.add(neighbour)
                        queue.append((neighbour, depth + 1))

        return None

    def get_blast_radius(self, device: str, hops: int | None = None) -> list[str]:
        """
        Return the list of devices reachable from `device` within `hops` graph steps.
        Does not include `device` itself.
        """
        max_hops = hops if hops is not None else self._hops
        graph    = self._get_graph()
        if not graph:
            return []

        visited: set[str] = {device}
        queue: deque[tuple[str, int]] = deque([(device, 0)])
        affected: list[str] = []

        while queue:
            current, depth = queue.popleft()
            if depth >= max_hops:
                continue
            for neighbour in graph.get(current, set()):
                if neighbour not in visited:
                    visited.add(neighbour)
                    affected.append(neighbour)
                    queue.append((neighbour, depth + 1))

        return affected

    def is_downstream_symptom(self, alert_device: str, candidate_root: str) -> bool:
        """True if alert_device is reachable from candidate_root within blast_radius_hops."""
        blast = self.get_blast_radius(candidate_root)
        return alert_device in blast

    # ── graph management ──────────────────────────────────────────────────────

    def _get_graph(self) -> dict[str, set[str]]:
        with self._lock:
            if time.monotonic() - self._graph_ts > self._cache_ttl:
                self._refresh_graph()
            return self._graph

    def _refresh_graph(self) -> None:
        try:
            graph = self._fetch_topology_graph()
            self._graph    = graph
            self._graph_ts = time.monotonic()
            logger.debug("TopologyCorrelator: graph refreshed (%d nodes)", len(graph))
        except Exception:
            logger.exception("TopologyCorrelator: failed to refresh topology graph")

    def _fetch_topology_graph(self) -> dict[str, set[str]]:
        """Build an undirected adjacency graph of devices from Nautobot.

        Nautobot v3 cables API returns termination_a/termination_b as bare
        interface ID references (no inline device name), so we build the graph
        via two API calls:
          1. /dcim/devices/  → interface_id-to-device-name is not available here,
             but device ID→name mapping is needed.
          2. /dcim/interfaces/?has_cable=true  → each interface has device.id and
             cable_peer (peer interface ID); join both sides on interface ID to
             get device pairs.
        """
        headers = {"Authorization": f"Token {self._token}"}
        timeout = 20

        # Step 1: Build device_id → device_name map.
        dev_resp = httpx.get(
            f"{self._url}/api/dcim/devices/",
            headers=headers,
            params={"limit": 500},
            timeout=timeout,
        )
        dev_resp.raise_for_status()
        id_to_name: dict[str, str] = {}
        for dev in dev_resp.json().get("results", []):
            did  = dev.get("id", "")
            name = (dev.get("name") or dev.get("display", "")).strip()
            if did and name:
                id_to_name[did] = name

        # Step 2: Fetch all interfaces that have a cable, build interface_id → device_name.
        intf_resp = httpx.get(
            f"{self._url}/api/dcim/interfaces/",
            headers=headers,
            params={"has_cable": "true", "limit": 1000},
            timeout=timeout,
        )
        intf_resp.raise_for_status()
        intf_to_device: dict[str, str] = {}
        intf_to_peer: dict[str, str] = {}
        for intf in intf_resp.json().get("results", []):
            iid  = intf.get("id", "")
            did  = (intf.get("device") or {}).get("id", "")
            peer = (intf.get("cable_peer") or {}).get("id", "")
            dev_name = id_to_name.get(did, "")
            if iid and dev_name:
                intf_to_device[iid] = dev_name
            if iid and peer:
                intf_to_peer[iid] = peer

        # Step 3: Build graph — for each interface, add an edge to its cable peer.
        graph: dict[str, set[str]] = {}
        for iid, peer_id in intf_to_peer.items():
            da = intf_to_device.get(iid, "")
            db = intf_to_device.get(peer_id, "")
            if da and db and da != db:
                graph.setdefault(da, set()).add(db)
                graph.setdefault(db, set()).add(da)

        return graph
