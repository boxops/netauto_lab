#!/usr/bin/env python3
"""Topology API: serves nodes and edges JSON for Grafana node-graph panel.

Queries Nautobot's cable/device APIs and returns data with field names
that Grafana's node-graph panel requires (id, title, source, target, x, y).

Endpoints:
  GET /nodes?layout=<force|hierarchical|circular>
  GET /edges
  GET /health -> "ok"
"""

import json
import math
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

NAUTOBOT_URL = os.environ.get("NAUTOBOT_URL", "http://nautobot:8080")
NAUTOBOT_TOKEN = os.environ.get("NAUTOBOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8765"))

# Role name (lower) -> tier row index (0 = top of screen)
ROLE_TIERS = {
    "spine": 0,
    "leaf": 1,
    "server": 2,
}


def nautobot_get(path):
    req = urllib.request.Request(
        f"{NAUTOBOT_URL}{path}",
        headers={"Authorization": f"Token {NAUTOBOT_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _role_tier(role_name):
    return ROLE_TIERS.get((role_name or "").lower(), len(ROLE_TIERS))


def _apply_hierarchical(nodes):
    """y = role tier, x = evenly spread within tier."""
    SPACING_X = 200
    SPACING_Y = 250
    tiers = defaultdict(list)
    for n in nodes:
        tiers[_role_tier(n["mainStat"])].append(n)
    for tier_index in sorted(tiers):
        tier_nodes = tiers[tier_index]
        total_width = (len(tier_nodes) - 1) * SPACING_X
        for i, n in enumerate(tier_nodes):
            n["x"] = float(i * SPACING_X - total_width / 2)
            n["y"] = float(tier_index * SPACING_Y)
    return nodes


def _apply_circular(nodes):
    """Evenly spaced on a circle, sorted by role then name."""
    nodes = sorted(nodes, key=lambda n: (_role_tier(n["mainStat"]), n["id"]))
    count = len(nodes)
    if count == 0:
        return nodes
    radius = max(150, count * 50)
    for i, n in enumerate(nodes):
        angle = 2 * math.pi * i / count - math.pi / 2  # start at top
        n["x"] = round(radius * math.cos(angle), 2)
        n["y"] = round(radius * math.sin(angle), 2)
    return nodes


def get_nodes(layout="force"):
    data = nautobot_get("/api/dcim/devices/?limit=0&depth=1")
    nodes = [
        {
            "id": d["name"],
            "title": d["name"],
            "mainStat": (d.get("role") or {}).get("name", ""),
            "secondaryStat": (d.get("platform") or {}).get("name", ""),
        }
        for d in data["results"]
    ]
    if layout == "hierarchical":
        nodes = _apply_hierarchical(nodes)
    elif layout == "circular":
        nodes = _apply_circular(nodes)
    # "force" -> no x/y, Grafana uses force-directed layout automatically
    return nodes


def get_edges():
    data = nautobot_get("/api/dcim/cables/?limit=0&depth=3")
    edges = []
    for c in data["results"]:
        a = c.get("termination_a") or {}
        b = c.get("termination_b") or {}
        src = (a.get("device") or {}).get("name", "")
        tgt = (b.get("device") or {}).get("name", "")
        if src and tgt:
            edges.append(
                {
                    "id": c["id"],
                    "source": src,
                    "target": tgt,
                    "mainStat": a.get("name", ""),
                }
            )
    return edges


class TopologyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress per-request access logs

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        layout = params.get("layout", ["force"])[0]

        try:
            if parsed.path == "/nodes":
                body = json.dumps({"nodes": get_nodes(layout)}).encode()
            elif parsed.path == "/edges":
                body = json.dumps({"edges": get_edges()}).encode()
            elif parsed.path == "/health":
                body = b'"ok"'
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode())


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), TopologyHandler)
    print(f"Topology API running on :{PORT}", flush=True)
    server.serve_forever()
