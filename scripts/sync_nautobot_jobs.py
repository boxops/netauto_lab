#!/usr/bin/env python3
"""Trigger a sync of the netauto-jobs Git repository in Nautobot."""
import os
import sys
import requests

token = os.environ.get("NAUTOBOT_SUPERUSER_API_TOKEN", "")
port = os.environ.get("NAUTOBOT_PORT", "8080")
base = f"http://localhost:{port}/api"
headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}

r = requests.get(f"{base}/extras/git-repositories/?name=netauto-jobs", headers=headers)
r.raise_for_status()
results = r.json().get("results", [])
if not results:
    print("ERROR: git repository 'netauto-jobs' not found in Nautobot.", file=sys.stderr)
    sys.exit(1)

repo_id = results[0]["id"]
r2 = requests.post(f"{base}/extras/git-repositories/{repo_id}/sync/", headers=headers)
print(f"Sync triggered (HTTP {r2.status_code}):", r2.json().get("message", r2.text[:80]))
