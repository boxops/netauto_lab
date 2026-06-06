"""
Network AI Agents – FastAPI + Jinja2 + HTMX Web UI
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import uuid
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.activity_store import ActivityStore
from shared.config import settings
from shared.task_store import TaskStore

# ── Config ────────────────────────────────────────────────────────────────────

OPS_AGENT_URL = os.getenv("OPS_AGENT_URL", "http://ai-agent:8000")

AGENT_URLS = {
    "ops": OPS_AGENT_URL,
}

AGENT_LABELS = {
    "ops": ("Ops Agent", "#6366f1"),
}

AGENT_QUICK_PROMPTS = {
    "ops": [
        "What alerts are currently firing?",
        "Why is spine1 showing high CPU?",
        "Show me interface errors on leaf1 in the last hour",
        "Investigate BGP peer down alert on spine2",
        "Generate a network health report for all lab devices",
        "What changed on the network in the last 30 minutes?",
        "List all devices and their current operational status",
        "Are there any interface flaps in the last 2 hours?",
        "Show recent log errors from spine2",
        "Correlate the current CPU alert with recent config changes on leaf1",
        "Show BGP neighbor states for all routers in the topology",
        "Check for OSPF adjacency issues across all devices",
        "What is the average latency between spine1 and leaf2?",
        "Summarize all critical and warning events from the last 24 hours",
        "Design BGP configuration for a new leaf router with AS 65104",
        "What IP addresses are available in 10.10.0.0/16?",
        "Generate an Ansible playbook to configure VLANs 100-110 on all leaf switches",
        "Review this EOS config snippet for security issues",
        "What VLANs are currently configured on leaf1?",
        "Validate the IP addressing scheme across all devices for inconsistencies",
        "Propose a safe chaos test for BGP flap detection in this lab",
        "Simulate a leaf uplink failure on leaf1 in check mode",
        "What is the expected blast radius if I bounce Ethernet1 on spine1?",
        "Shut down Ethernet1 on leaf2 in check mode and show me what alerts would fire",
        "Run a connectivity validation test across all leaf-spine links",
    ],
}

_ACTIVITY_DB         = os.environ.get("ACTIVITY_DB_PATH", "./activity.db")
APPROVAL_WEBHOOK_URL = os.getenv("APPROVAL_WEBHOOK_URL", "")
APPROVAL_WEBHOOK_SECRET = os.getenv("APPROVAL_WEBHOOK_SECRET", "")
AGENT_UI_URL         = os.getenv("AGENT_UI_URL", "http://localhost:7860")
AGENT_API_KEY        = os.getenv("AGENT_API_KEY", "")

# Shared persistent HTTP client — initialised in lifespan, reused across all requests.
_http_client: httpx.AsyncClient | None = None


def _get_hourly_series(hours: int = 24) -> dict:
    """Hourly aggregated cost & token series from token_usage table."""
    now     = datetime.now(timezone.utc)
    buckets = [(now - timedelta(hours=hours - 1 - i)).strftime("%Y-%m-%d %H") for i in range(hours)]
    labels  = [(now - timedelta(hours=hours - 1 - i)).strftime("%H") for i in range(hours)]
    result: dict = {
        "buckets":         buckets,
        "labels":          labels,
        "total_cost":      [0.0] * hours,
        "total_tokens":    [0]   * hours,
        "by_agent_cost":   {},
        "by_agent_tokens": {},
    }
    try:
        with sqlite3.connect(_ACTIVITY_DB, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cutoff = buckets[0] + ":00:00 UTC"
            rows = conn.execute(
                "SELECT substr(timestamp,1,13) hk, agent, "
                "SUM(estimated_cost_usd) cost, "
                "SUM(prompt_tokens+completion_tokens) tokens "
                "FROM token_usage WHERE timestamp>=? GROUP BY hk,agent",
                (cutoff,),
            ).fetchall()
        idx = {b: i for i, b in enumerate(buckets)}
        for r in rows:
            i = idx.get(r["hk"])
            if i is None:
                continue
            ag = r["agent"]
            result["total_cost"][i]   += r["cost"]
            result["total_tokens"][i] += r["tokens"]
            if ag not in result["by_agent_cost"]:
                result["by_agent_cost"][ag]   = [0.0] * hours
                result["by_agent_tokens"][ag] = [0]   * hours
            result["by_agent_cost"][ag][i]   = r["cost"]
            result["by_agent_tokens"][ag][i] = r["tokens"]
    except Exception:
        pass
    return result


def _sparkline_svg(points: list[float], color: str = "#6366f1") -> str:
    """Inline SVG sparkline (area + line + dashed trendline) for card backgrounds."""
    if not points or len(points) < 2:
        return ""
    W, H, pad = 200, 52, 4
    eh = H - pad * 2
    n  = len(points)
    lo, hi = min(points), max(points)
    span   = (hi - lo) or (hi or 1.0)
    lo_adj = lo if (hi - lo) > 0 else 0.0

    def _x(i: int) -> float: return (i / (n - 1)) * W
    def _y(v: float) -> float: return pad + eh - ((v - lo_adj) / span) * eh

    pts    = [(_x(i), _y(v)) for i, v in enumerate(points)]
    line_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area_d = line_d + f" L {pts[-1][0]:.1f},{H} L {pts[0][0]:.1f},{H} Z"

    xm  = (n - 1) / 2.0
    ym  = sum(points) / n
    num = sum((i - xm) * (points[i] - ym) for i in range(n))
    den = sum((i - xm) ** 2 for i in range(n))
    sl  = num / den if den else 0.0
    ic  = ym - sl * xm
    ty1 = max(0.0, min(float(H), _y(ic)))
    ty2 = max(0.0, min(float(H), _y(sl * (n - 1) + ic)))

    uid = abs(hash(str(points))) % 100000
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="100%" height="100%" preserveAspectRatio="none">'
        f'<defs><linearGradient id="sg{uid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity="0.3"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0.03"/>'
        f'</linearGradient></defs>'
        f'<path d="{area_d}" fill="url(#sg{uid})"/>'
        f'<path d="{line_d}" fill="none" stroke="{color}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<line x1="{_x(0):.1f}" y1="{ty1:.1f}" x2="{_x(n-1):.1f}" y2="{ty2:.1f}" '
        f'stroke="{color}" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>'
        f'</svg>'
    )


def _chart_svg(
    series: list[tuple[str, str, list[float]]],
    labels: list[str],
    fmt_y=None,
    vw: int = 560,
    vh: int = 110,
) -> str:
    """Multi-line area chart SVG with gridlines, x-labels, legend, and trendline."""
    if fmt_y is None:
        fmt_y = lambda v: f"{v:.2f}"
    lm, rm, tm, bm = 40, 8, 14, 18
    cw = vw - lm - rm
    ch = vh - tm - bm

    all_vals = [v for _, _, vals in series for v in vals]
    raw_max  = max(all_vals, default=0.0)
    max_v    = (raw_max * 1.15) or 1.0
    n        = len(labels)
    if n < 2:
        return ""

    def _x(i: int) -> float:   return lm + (i / (n - 1)) * cw
    def _y(v: float) -> float:  return tm + ch * (1.0 - min(1.0, max(0.0, v / max_v)))

    parts: list[str] = []

    # Background
    parts.append(f'<rect x="{lm}" y="{tm}" width="{cw}" height="{ch}" fill="#0a111e" rx="3"/>')

    # Horizontal gridlines + y-axis labels
    for step in range(1, 5):
        gv = max_v * step / 4
        gy = _y(gv)
        parts.append(
            f'<line x1="{lm}" y1="{gy:.1f}" x2="{lm+cw}" y2="{gy:.1f}" '
            f'stroke="#1e2d45" stroke-width="0.6"/>'
        )
        parts.append(
            f'<text x="{lm-3}" y="{gy+2:.1f}" font-size="6.5" fill="#64748b" '
            f'text-anchor="end" font-family="system-ui,sans-serif">{fmt_y(gv)}</text>'
        )

    # Vertical ticks + x-axis labels every 4 hours
    for i, lbl in enumerate(labels):
        if i % 4 == 0 or i == n - 1:
            vx = _x(i)
            parts.append(
                f'<line x1="{vx:.1f}" y1="{tm}" x2="{vx:.1f}" y2="{tm+ch+3}" '
                f'stroke="#1e2d45" stroke-width="0.4"/>'
            )
            parts.append(
                f'<text x="{vx:.1f}" y="{tm+ch+12}" font-size="6.5" fill="#64748b" '
                f'text-anchor="middle" font-family="system-ui,sans-serif">{lbl}h</text>'
            )

    # Area fills (rendered first)
    uid_base = abs(hash(str(all_vals))) % 100000
    for si, (name, color, vals) in enumerate(series):
        if len(vals) != n:
            continue
        uid = uid_base + si
        pts = [(_x(i), _y(v)) for i, v in enumerate(vals)]
        ld  = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        ad  = ld + f" L {_x(n-1):.1f},{_y(0):.1f} L {_x(0):.1f},{_y(0):.1f} Z"
        parts.append(
            f'<defs><linearGradient id="cg{uid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0%" stop-color="{color}" stop-opacity="0.25"/>'
            f'<stop offset="100%" stop-color="{color}" stop-opacity="0.01"/>'
            f'</linearGradient></defs>'
        )
        parts.append(f'<path d="{ad}" fill="url(#cg{uid})"/>')

    # Lines on top
    for si, (name, color, vals) in enumerate(series):
        if len(vals) != n:
            continue
        pts = [(_x(i), _y(v)) for i, v in enumerate(vals)]
        ld  = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        sw  = 1.8 if si == 0 else 1.2
        op  = 0.95 if si == 0 else 0.75
        parts.append(
            f'<path d="{ld}" fill="none" stroke="{color}" stroke-width="{sw}" '
            f'stroke-linejoin="round" stroke-linecap="round" opacity="{op}"/>'
        )

    # Trendline for the total series (first in list)
    if series:
        _, tc, tv = series[0]
        tn  = len(tv)
        xm2 = (tn - 1) / 2.0
        ym2 = sum(tv) / tn
        n2  = sum((i - xm2) * (tv[i] - ym2) for i in range(tn))
        d2  = sum((i - xm2) ** 2 for i in range(tn))
        sl2 = n2 / d2 if d2 else 0.0
        ic2 = ym2 - sl2 * xm2
        clamp = lambda y: max(float(tm), min(float(tm + ch), y))
        ty1   = clamp(_y(ic2))
        ty2   = clamp(_y(sl2 * (tn - 1) + ic2))
        parts.append(
            f'<line x1="{_x(0):.1f}" y1="{ty1:.1f}" x2="{_x(tn-1):.1f}" y2="{ty2:.1f}" '
            f'stroke="{tc}" stroke-width="1.2" stroke-dasharray="5,3" opacity="0.9"/>'
        )

    # Legend (top-right, built right-to-left)
    lx = vw - rm
    for name, color, _ in reversed(series):
        tw  = len(name) * 4.0 + 14
        lx -= tw
        parts.append(f'<rect x="{lx:.1f}" y="3" width="6" height="4" fill="{color}" rx="1"/>')
        parts.append(
            f'<text x="{lx+8:.1f}" y="8" font-size="6.5" fill="{color}" '
            f'font-family="system-ui,sans-serif" font-weight="600">{name}</text>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw} {vh}" width="100%">'
        + "".join(parts)
        + "</svg>"
    )


STATUS_COLORS = {
    "pending":           "#f59e0b",
    "claimed":           "#3b82f6",
    "running":           "#22c55e",
    "awaiting_approval": "#a855f7",
    "complete":          "#6b7280",
    "failed":            "#ef4444",
    "rejected":          "#9ca3af",
}

PRIORITY_COLORS = {
    "critical": "#ef4444",
    "high":     "#f97316",
    "normal":   "#3b82f6",
    "low":      "#6b7280",
}

TYPE_ICONS = {
    "rca": "🔍",
}

# ── Approval webhook ──────────────────────────────────────────────────────────

async def _fire_approval_webhook(task: dict) -> None:
    """POST task details to APPROVAL_WEBHOOK_URL when a gate enters awaiting_approval."""
    if not APPROVAL_WEBHOOK_URL:
        return

    content: dict = {}
    try:
        content = json.loads(task.get("content") or "{}")
    except Exception:
        pass

    fix_proposal = content.get("fix_proposal", {})
    device   = fix_proposal.get("device") or content.get("device", "unknown")
    commands = fix_proposal.get("commands") or content.get("commands", "none")
    fix_type = fix_proposal.get("fix_type", "config_change")
    risk     = fix_proposal.get("risk") or content.get("risk_confirmed", "unknown")
    task_id  = task["id"]

    payload = {
        "task_id":   task_id,
        "title":     task.get("title", ""),
        "device":    device,
        "commands":  commands,
        "fix_type":  fix_type,
        "risk":      risk,
        "priority":  task.get("priority", "normal"),
        "created_at": task.get("created_at", ""),
        "approve_url": f"{AGENT_UI_URL}/tasks/{task_id}/approve",
        "reject_url":  f"{AGENT_UI_URL}/tasks/{task_id}/reject",
        "detail_url":  f"{AGENT_UI_URL}/#task-{task_id}",
    }

    body = json.dumps(payload).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if APPROVAL_WEBHOOK_SECRET:
        sig = hmac.new(APPROVAL_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={sig}"

    try:
        r = await _http_client.post(APPROVAL_WEBHOOK_URL, content=body, headers=headers, timeout=5)
        logger.info("Approval webhook fired for task=%s status=%s", task_id, r.status_code)
    except Exception as exc:
        logger.warning("Approval webhook failed for task=%s: %s", task_id, exc)


# Task IDs already notified — persists for the process lifetime so restarts
# don't re-fire webhooks for gates that are still awaiting_approval.
_webhook_notified: set[str] = set()


async def _webhook_poller() -> None:
    """
    Background task: poll every 10 s for new awaiting_approval gates and fire
    the approval webhook exactly once per task.  Pre-seeds from existing tasks
    on startup so a container restart does not re-notify for open gates.
    """
    if not APPROVAL_WEBHOOK_URL:
        return

    # Seed from tasks already in the store so we don't re-fire on restart
    try:
        for t in await run_in_threadpool(task_store.list_tasks, status="awaiting_approval", limit=500):
            _webhook_notified.add(t["id"])
    except Exception:
        pass

    while True:
        await asyncio.sleep(10)
        try:
            pending = await run_in_threadpool(task_store.list_tasks, status="awaiting_approval", limit=100)
            for t in pending:
                if t["id"] not in _webhook_notified:
                    _webhook_notified.add(t["id"])
                    await _fire_approval_webhook(t)
        except Exception as exc:
            logger.warning("Webhook poller error: %s", exc)


# ── App setup ─────────────────────────────────────────────────────────────────

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TMPL_DIR   = os.path.join(BASE_DIR, "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    headers = {"X-API-Key": AGENT_API_KEY} if AGENT_API_KEY else {}
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0), headers=headers)
    asyncio.create_task(_webhook_poller())
    yield
    await _http_client.aclose()


app = FastAPI(title="Network AI Agents", description="Network Automation AI Agents UI", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TMPL_DIR)
templates.env.filters["from_json"] = lambda s: json.loads(s) if s else {}

store      = ActivityStore()
task_store = TaskStore()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _truncate(text: str, max_len: int = 140) -> str:
    text = text or ""
    return text if len(text) <= max_len else f"{text[:max_len - 3]}..."


def _age(ts_str: str | None) -> str:
    if not ts_str:
        return "—"
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - ts).total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:
        return ts_str


async def _fetch_agent_health(client: httpx.AsyncClient, name: str, url: str) -> dict:
    try:
        r = await client.get(f"{url}/health", timeout=3)
        label, color = ("Online", "#22c55e") if r.status_code == 200 else (f"HTTP {r.status_code}", "#f59e0b")
    except Exception:
        label, color = "Offline", "#ef4444"
    return {"name": name, "label": label, "color": color}


async def _fetch_agent_status(client: httpx.AsyncClient, url: str) -> dict:
    try:
        r = await client.get(f"{url}/status", timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"state": "unreachable", "agent_name": ""}


async def _fetch_agent_usage(client: httpx.AsyncClient, url: str) -> dict:
    try:
        r = await client.get(f"{url}/usage", timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


# ── Pipeline Chronicle helpers ────────────────────────────────────────────────

def _parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _seconds_between(ts1: str | None, ts2: str | None) -> int:
    t1, t2 = _parse_ts(ts1), _parse_ts(ts2)
    return max(0, int((t2 - t1).total_seconds())) if t1 and t2 else 0


def _fmt_gap(seconds: int) -> str:
    if seconds <= 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if s else f"{m}m"


def _ts_short(ts_str: str | None) -> str:
    if not ts_str:
        return "—"
    parts = ts_str.split(" ")
    return parts[1] if len(parts) > 1 else ts_str


def _confidence_badge(c: str) -> tuple[str, str, str]:
    cl = (c or "").lower()
    if cl == "high":    return "High confidence",   "#22c55e", "✅"
    if cl == "medium":  return "Medium confidence", "#f59e0b", "🟡"
    if cl == "low":     return "Low confidence",    "#ef4444", "⚠️"
    return "", "#6b7280", ""


def _risk_badge(r: str) -> tuple[str, str, str]:
    rl = (r or "").lower()
    if rl == "low":     return "Low risk",    "#22c55e", "✅"
    if rl == "medium":  return "Medium risk", "#f59e0b", "🟡"
    if rl == "high":    return "High risk",   "#ef4444", "🔴"
    return "", "#6b7280", ""


def _verdict_badge(v: str) -> tuple[str, str, str]:
    vl = (v or "").lower()
    if vl == "correct":       return "Correct",       "#22c55e", "✅"
    if vl == "partial":       return "Partial",       "#f59e0b", "🟡"
    if vl == "incorrect":     return "Incorrect",     "#ef4444", "❌"
    if vl == "unverifiable":  return "Unverifiable",  "#6b7280", "❓"
    return "", "#6b7280", ""


def _extract_gate_events(task: dict) -> dict:
    """Scan a gate task's events for execution / verification data."""
    events = task.get("events") or []
    if not events:
        full = task_store.get_task(task["id"])
        events = (full or {}).get("events", [])

    out: dict = {
        "approved_by": "", "approval_ts": "",
        "exec_status": "", "changes_applied": "",
        "config_applied": None, "found_lines": [], "missing_lines": [],
        "alert_resolved": None, "ttr_seconds": 0, "check_at": "",
        "autonomy_level": "", "policy_id": None, "policy_reason": "",
    }
    for e in events:
        et = e.get("event_type", "")
        try:
            d = json.loads(e.get("detail") or "{}")
        except Exception:
            d = {}
        if et == "approved":
            out["approved_by"] = e.get("agent", "human")
            out["approval_ts"] = _ts_short(e.get("timestamp"))
        elif et == "auto_approved":
            out["approved_by"] = "system (auto-approved)"
            out["approval_ts"] = _ts_short(e.get("timestamp"))
            out["autonomy_level"] = d.get("autonomy_level", "")
            out["policy_id"]      = d.get("policy_id")
            out["policy_reason"]  = d.get("reason", "")
        elif et == "approval_policy":
            out["autonomy_level"] = d.get("autonomy_level", "")
            out["policy_id"]      = d.get("policy_id")
            out["policy_reason"]  = d.get("reason", "")
        elif et == "execution_complete":
            out["exec_status"]     = d.get("status", "")
            out["config_applied"]  = d.get("config_applied")
            out["found_lines"]     = d.get("found_lines", [])
            out["missing_lines"]   = d.get("missing_lines", [])
            out["changes_applied"] = d.get("changes_applied", "")
        elif et == "execution_verified":
            out["alert_resolved"] = d.get("alert_resolved")
            out["ttr_seconds"]    = d.get("ttr_seconds", 0)
            out["check_at"]       = d.get("check_at", "")
    return out


def _build_chapter(task: dict, prev_completed_at: str | None) -> dict:
    tp      = task.get("type", "")
    status  = task.get("status", "pending")
    created = task.get("created_at", "")
    done    = task.get("completed_at", "")

    try:
        result = json.loads(task.get("result") or "{}")
    except Exception:
        result = {}
    try:
        content = json.loads(task.get("content") or "{}")
    except Exception:
        content = {}

    gap_seconds = _seconds_between(prev_completed_at, created) if prev_completed_at else 0

    ch: dict = {
        "type":        tp,
        "task_id":     task.get("id", ""),
        "status":      status,
        "created_at":  created,
        "completed_at": done,
        "timestamp":   _ts_short(created),
        "gap_str":     _fmt_gap(gap_seconds),
        "gap_seconds": gap_seconds,
        # badge defaults overridden per stage below
        "label":       tp.upper(),
        "badge_text":  "",
        "badge_color": "#6b7280",
        "badge_icon":  "",
    }

    if tp == "rca":
        conf = result.get("confidence", "")
        bt, bc, bi = _confidence_badge(conf)
        ch.update({
            "label":           "ROOT CAUSE IDENTIFIED" if status == "complete" else
                               ("INVESTIGATING…" if status in ("running", "claimed") else "INVESTIGATION PENDING"),
            "badge_text":      bt, "badge_color": bc, "badge_icon": bi,
            "alertname":       content.get("alertname", ""),
            "severity":        content.get("severity", ""),
            "device":          content.get("device") or result.get("affected", ""),
            "instance":        content.get("instance", ""),
            "summary":         content.get("summary", ""),
            "diagnosis":       result.get("diagnosis", ""),
            "confidence":      conf,
            "action":          result.get("action", ""),
            "tool_calls":      result.get("tool_calls", 0),
            "full_response":   _truncate(result.get("full_response", ""), 1800),
            "affected_devices": result.get("affected_devices", []),
        })

    elif tp == "fix_proposal":
        risk = result.get("fix_type", "")
        bt, bc, bi = _risk_badge(result.get("risk", ""))
        commands = result.get("commands", "")
        ch.update({
            "label":       "FIX PROPOSED" if status == "complete" else
                           ("GENERATING FIX…" if status in ("running", "claimed") else "FIX PENDING"),
            "badge_text":  bt, "badge_color": bc, "badge_icon": bi,
            "fix_type":    result.get("fix_type", ""),
            "device":      result.get("device", ""),
            "commands":    commands if commands not in ("none", "", None) else "",
            "risk":        result.get("risk", ""),
            "confidence":  result.get("confidence", ""),
            "reason":      result.get("reason", ""),
            "runbook":     result.get("runbook", ""),
            "config_diff": result.get("config_diff", ""),
            "tool_calls":  result.get("tool_calls", 0),
            "full_response": _truncate(result.get("full_response", ""), 1800),
        })

    elif tp == "validation":
        verdict = result.get("verdict", "")
        bt, bc, bi = _verdict_badge(verdict)
        fix = content.get("fix_proposal", {})
        ch.update({
            "label":          "VALIDATED" if status == "complete" else
                              ("VALIDATING…" if status in ("running", "claimed") else "VALIDATION PENDING"),
            "badge_text":     bt, "badge_color": bc, "badge_icon": bi,
            "verdict":        verdict,
            "confidence":     result.get("confidence", ""),
            "risk_confirmed": result.get("risk_confirmed", ""),
            "notes":          result.get("notes", ""),
            "tool_calls":     result.get("tool_calls", 0),
            "device":         fix.get("device", ""),
        })

    elif tp == "approval_gate":
        fix    = content.get("fix_proposal", {})
        device = fix.get("device") or content.get("device", "")
        commands = fix.get("commands") or content.get("commands", "")
        ev = _extract_gate_events(task)
        ttr_str = _fmt_gap(ev["ttr_seconds"])

        awaiting = (status == "awaiting_approval")
        es = ev["exec_status"]
        ar = ev["alert_resolved"]

        if awaiting:
            label, bt, bc, bi = "AWAITING APPROVAL",  "Requires action", "#a855f7", "🟣"
        elif status == "rejected":
            label, bt, bc, bi = "REJECTED",           "Rejected",        "#9ca3af", "✗"
        elif es == "success" and ar is True:
            label = "RESOLVED"
            bt, bc, bi = (f"TTR {ttr_str}" if ttr_str else "Resolved"), "#22c55e", "✅"
        elif es == "success":
            label, bt, bc, bi = "EXECUTED",           "Success",         "#22c55e", "✅"
        elif es == "failed":
            label, bt, bc, bi = "EXECUTION FAILED",   "Failed",          "#ef4444", "❌"
        else:
            label, bt, bc, bi = "APPROVAL GATE",      "Pending",         "#6b7280", "⏳"

        ch.update({
            "label": label, "badge_text": bt, "badge_color": bc, "badge_icon": bi,
            "device":             device,
            "commands":           commands if commands not in ("none", "", None) else "",
            "fix_type":           fix.get("fix_type", ""),
            "validation_verdict": content.get("validation_verdict", ""),
            "risk_confirmed":     content.get("risk_confirmed", ""),
            "chaos_notes":        content.get("chaos_notes", ""),
            "awaiting_approval":  awaiting,
            "do_not_auto_execute": bool(content.get("do_not_auto_execute") or task.get("do_not_auto_execute")),
            "config_diff":        content.get("config_diff") or fix.get("config_diff", ""),
            **ev,
            "ttr_str":       ttr_str,
            "autonomy_level": ev.get("autonomy_level", ""),
            "policy_reason":  ev.get("policy_reason", ""),
        })

    return ch


def _pipeline_chronicle_context(fp: str) -> dict:
    if not fp:
        return {"fp": fp, "chapters": [], "overall": {}}

    tasks = task_store.list_tasks(alert_fingerprint=fp, type="rca", limit=1)
    if not tasks:
        return {"fp": fp, "chapters": [], "overall": {}}

    task = task_store.get_task(tasks[0]["id"])
    if not task:
        return {"fp": fp, "chapters": [], "overall": {}}

    events = task.get("events") or []
    try:
        content = json.loads(task.get("content") or "{}")
    except Exception:
        content = {}

    # Index events by type for quick lookup; keep last occurrence of each
    event_map: dict[str, dict] = {}
    for e in events:
        event_map[e.get("event_type", "")] = e

    def _event_detail(et: str) -> dict:
        e = event_map.get(et, {})
        try:
            return json.loads(e.get("detail") or "{}")
        except Exception:
            return {}

    chapters: list[dict] = []
    created_at = task.get("created_at", "")
    status = task.get("status", "pending")

    # ── RCA chapter ──────────────────────────────────────────────────────────
    rca_event = event_map.get("rca_complete")
    rca_d = _event_detail("rca_complete")
    # Fall back to task result if event not yet written
    if not rca_d:
        try:
            rca_d = json.loads(task.get("result") or "{}")
        except Exception:
            rca_d = {}

    if rca_d or status in ("running", "claimed", "pending"):
        conf = rca_d.get("confidence", "")
        bt, bc, bi = _confidence_badge(conf)
        rca_ts = _ts_short(rca_event.get("timestamp") if rca_event else created_at)
        chapters.append({
            "type":             "rca",
            "task_id":          task["id"],
            "status":           "complete" if rca_d.get("diagnosis") else status,
            "timestamp":        rca_ts,
            "gap_str":          "",
            "gap_seconds":      0,
            "label":            "ROOT CAUSE IDENTIFIED" if rca_d.get("diagnosis") else
                                ("INVESTIGATING…" if status in ("running", "claimed") else "INVESTIGATION PENDING"),
            "badge_text":       bt, "badge_color": bc, "badge_icon": bi,
            "alertname":        content.get("alertname", ""),
            "severity":         content.get("severity", ""),
            "device":           content.get("device") or rca_d.get("affected", ""),
            "instance":         content.get("instance", ""),
            "summary":          content.get("summary", ""),
            "diagnosis":        rca_d.get("diagnosis", ""),
            "confidence":       conf,
            "action":           rca_d.get("action", ""),
            "tool_calls":       rca_d.get("tool_calls", 0),
            "full_response":    _truncate(rca_d.get("response", ""), 1800),
            "affected_devices": rca_d.get("affected_devices", []),
        })

    # ── Fix Proposal chapter ─────────────────────────────────────────────────
    fix_event = event_map.get("fix_proposal_complete")
    fix_d = _event_detail("fix_proposal_complete")
    if fix_d:
        commands = fix_d.get("commands", "")
        bt, bc, bi = _risk_badge(fix_d.get("risk", ""))
        rca_done = rca_event.get("timestamp") if rca_event else None
        fix_ts = _ts_short(fix_event.get("timestamp") if fix_event else None)
        gap_s = _seconds_between(rca_done, fix_event.get("timestamp") if fix_event else None)
        chapters.append({
            "type":          "fix_proposal",
            "task_id":       task["id"],
            "status":        "complete",
            "timestamp":     fix_ts,
            "gap_str":       _fmt_gap(gap_s),
            "gap_seconds":   gap_s,
            "label":         "FIX PROPOSED",
            "badge_text":    bt, "badge_color": bc, "badge_icon": bi,
            "fix_type":      fix_d.get("fix_type", ""),
            "device":        fix_d.get("device", ""),
            "commands":      commands if commands not in ("none", "", None) else "",
            "risk":          fix_d.get("risk", ""),
            "confidence":    fix_d.get("confidence", ""),
            "reason":        fix_d.get("reason", ""),
            "runbook":       fix_d.get("runbook", ""),
            "config_diff":   fix_d.get("config_diff", ""),
            "tool_calls":    fix_d.get("tool_calls", 0),
            "full_response": _truncate(fix_d.get("full_response", ""), 1800),
        })

    # ── Validation chapter ───────────────────────────────────────────────────
    val_event = event_map.get("validation_complete")
    val_d = _event_detail("validation_complete")
    if val_d:
        verdict = val_d.get("verdict", "")
        bt, bc, bi = _verdict_badge(verdict)
        fix_done = fix_event.get("timestamp") if fix_event else None
        val_ts = _ts_short(val_event.get("timestamp") if val_event else None)
        gap_s = _seconds_between(fix_done, val_event.get("timestamp") if val_event else None)
        chapters.append({
            "type":           "validation",
            "task_id":        task["id"],
            "status":         "complete",
            "timestamp":      val_ts,
            "gap_str":        _fmt_gap(gap_s),
            "gap_seconds":    gap_s,
            "label":          "VALIDATED",
            "badge_text":     bt, "badge_color": bc, "badge_icon": bi,
            "verdict":        verdict,
            "confidence":     val_d.get("confidence", ""),
            "risk_confirmed": val_d.get("risk_confirmed", ""),
            "notes":          val_d.get("notes", ""),
            "tool_calls":     val_d.get("tool_calls", 0),
            "device":         content.get("fix_proposal", {}).get("device", ""),
        })

    # ── Approval Gate chapter ────────────────────────────────────────────────
    if status in ("awaiting_approval", "complete", "rejected") and (
        content.get("fix_proposal") or content.get("escalation_reason")
    ):
        fix = content.get("fix_proposal", {})
        device = fix.get("device") or content.get("device", "")
        commands = fix.get("commands") or content.get("commands", "")
        ev = _extract_gate_events(task)
        ttr_str = _fmt_gap(ev["ttr_seconds"])

        awaiting = (status == "awaiting_approval")
        es = ev["exec_status"]
        ar = ev["alert_resolved"]

        val_done = val_event.get("timestamp") if val_event else (
            fix_event.get("timestamp") if fix_event else None)
        gap_s = _seconds_between(val_done, task.get("completed_at") or task.get("created_at"))

        if awaiting:
            gate_label, bt, bc, bi = "AWAITING APPROVAL", "Requires action", "#a855f7", "🟣"
        elif status == "rejected":
            gate_label, bt, bc, bi = "REJECTED",          "Rejected",        "#9ca3af", "✗"
        elif es:
            gate_label, bt, bc, bi = "APPROVED",          "Approved",        "#22c55e", "✅"
        else:
            gate_label, bt, bc, bi = "APPROVAL GATE",     "Pending",         "#6b7280", "⏳"

        chapters.append({
            "type":               "approval_gate",
            "task_id":            task["id"],
            "status":             status,
            "timestamp":          _ts_short(task.get("completed_at") or task.get("created_at")),
            "gap_str":            _fmt_gap(gap_s),
            "gap_seconds":        gap_s,
            "label":              gate_label,
            "badge_text":         bt, "badge_color": bc, "badge_icon": bi,
            "device":             device,
            "commands":           commands if commands not in ("none", "", None) else "",
            "fix_type":           fix.get("fix_type", ""),
            "validation_verdict": content.get("validation_verdict", ""),
            "risk_confirmed":     content.get("risk_confirmed", ""),
            "chaos_notes":        content.get("chaos_notes", ""),
            "awaiting_approval":  awaiting,
            "do_not_auto_execute": bool(content.get("do_not_auto_execute")),
            "config_diff":        content.get("config_diff") or fix.get("config_diff", ""),
            "approved_by":        ev["approved_by"],
            "approval_ts":        ev["approval_ts"],
            "autonomy_level":     ev.get("autonomy_level", ""),
            "policy_reason":      ev.get("policy_reason", ""),
        })

        # ── Stage 5: VERIFY chapter — emitted only after execution ───────────
        if es:
            if es == "success" and ar is True:
                v_label, v_bt, v_bc, v_bi = "RESOLVED",          f"TTR {ttr_str}" if ttr_str else "Resolved", "#22c55e", "✅"
            elif es == "success":
                v_label, v_bt, v_bc, v_bi = "EXECUTED",          "Verifying…",     "#3b82f6", "⟳"
            else:
                v_label, v_bt, v_bc, v_bi = "EXECUTION FAILED",  "Failed",          "#ef4444", "❌"

            chapters.append({
                "type":           "verify",
                "task_id":        task["id"],
                "status":         "complete" if ar is not None else "running",
                "timestamp":      _ts_short(task.get("completed_at")),
                "gap_str":        "",
                "gap_seconds":    0,
                "label":          v_label,
                "badge_text":     v_bt, "badge_color": v_bc, "badge_icon": v_bi,
                "device":         device,
                "exec_status":    es,
                "config_applied": ev["config_applied"],
                "found_lines":    ev["found_lines"],
                "missing_lines":  ev["missing_lines"],
                "changes_applied": ev["changes_applied"],
                "alert_resolved": ar,
                "ttr_seconds":    ev["ttr_seconds"],
                "ttr_str":        ttr_str,
                "check_at":       ev["check_at"],
                "approved_by":    ev["approved_by"],
            })

    # Overall pipeline status
    statuses = [c["status"] for c in chapters]
    verify_ch = next((c for c in chapters if c["type"] == "verify"), {})
    if status == "awaiting_approval":
        o_status, o_label, o_color = "awaiting_approval", "Awaiting Approval", "#a855f7"
    elif status == "rejected":
        o_status, o_label, o_color = "rejected", "Rejected", "#9ca3af"
    elif status == "failed" or "failed" in statuses:
        o_status, o_label, o_color = "failed", "Pipeline Failed", "#ef4444"
    elif verify_ch.get("alert_resolved") is True:
        o_status, o_label, o_color = "resolved", "Resolved", "#22c55e"
    elif verify_ch.get("exec_status") == "success":
        o_status, o_label, o_color = "verifying", "Verifying…", "#3b82f6"
    elif status in ("running", "claimed", "pending"):
        o_status, o_label, o_color = "active", "In Progress", "#3b82f6"
    elif status == "complete":
        o_status, o_label, o_color = "complete", "Complete", "#22c55e"
    else:
        o_status, o_label, o_color = "pending", "Starting…", "#6b7280"

    first = next((c for c in chapters if c["type"] == "rca"), {})

    # Detect intent-triggered pipelines
    is_intent_triggered = fp.startswith("intent:")
    intent_name = content.get("alertname", "").replace("intent:", "") if is_intent_triggered else ""

    return {
        "fp": fp,
        "chapters": chapters,
        "verify_delay_min": max(1, settings.execution_verify_delay // 60),
        "overall": {
            "status":               o_status,
            "label":                o_label,
            "color":                o_color,
            "alertname":            first.get("alertname", ""),
            "device":               first.get("device", ""),
            "severity":             first.get("severity", ""),
            "alert_resolved":       verify_ch.get("alert_resolved"),
            "ttr_str":              verify_ch.get("ttr_str", ""),
            "is_intent_triggered":  is_intent_triggered,
            "intent_name":          intent_name,
        },
    }



def _incident_list_context(open_only: bool = True) -> dict:
    incidents = task_store.list_incidents(open_only=open_only, limit=100)
    rows = []
    for inc in incidents:
        try:
            content = json.loads(inc.get("content") or "{}")
        except Exception:
            content = {}
        severity  = content.get("severity", "P3")
        impact    = content.get("impact", "")
        devices   = content.get("affected_devices", [])
        rows.append({
            "id":          inc["id"],
            "severity":    severity,
            "impact":      impact,
            "status":      inc["status"],
            "status_color": STATUS_COLORS.get(inc["status"], "#6b7280"),
            "priority":    inc["priority"],
            "priority_color": PRIORITY_COLORS.get(inc["priority"], "#6b7280"),
            "devices":     devices,
            "device_count": len(devices),
            "age":         _age(inc.get("created_at")),
            "created_at":  inc.get("created_at", ""),
        })
    return {"incidents": rows, "open_only": open_only}


def _pipeline_fingerprints() -> list[tuple[str, str]]:
    tasks = task_store.list_tasks(type="rca", limit=200)
    seen: dict[str, str] = {}
    for t in tasks:
        fp = t.get("alert_fingerprint", "")
        if not fp or fp in seen:
            continue
        title = (t.get("title") or "").strip()
        seen[fp] = title if title else fp[:20]
    return [(fp, label) for fp, label in seen.items()]


def _task_queue_context(
    status_filter: str = "",
    type_filter: str = "",
    tenant_id: str = "",
) -> dict:
    tasks = task_store.list_tasks(
        status=status_filter or None,
        type=type_filter or None,
        tenant_id=tenant_id or None,
        limit=200,
    )
    rows = []
    for t in tasks:
        rows.append({
            "id":          t["id"],
            "type":        t["type"],
            "type_icon":   TYPE_ICONS.get(t["type"], "📋"),
            "status":      t["status"],
            "status_color": STATUS_COLORS.get(t["status"], "#6b7280"),
            "priority":    t["priority"],
            "priority_color": PRIORITY_COLORS.get(t["priority"], "#6b7280"),
            "assigned_to": t.get("assigned_to") or "—",
            "created_by":  t.get("created_by") or "—",
            "title":       _truncate(t.get("title") or "", 50),
            "age":         _age(t.get("created_at")),
        })
    return {"tasks": rows, "status_filter": status_filter, "type_filter": type_filter, "tenant_id": tenant_id}


def _task_detail_context(task_id: str) -> dict:
    task = task_store.get_task(task_id)
    if not task:
        return {"task": None, "task_id": task_id}

    chain = task_store.get_task_chain(task_id)

    try:
        content_obj = json.loads(task.get("content") or "{}")
        content_str = json.dumps(content_obj, indent=2)
    except Exception:
        content_str = task.get("content") or ""

    result_str = ""
    if task.get("result"):
        try:
            result_obj = json.loads(task["result"])
            result_str = json.dumps(result_obj, indent=2)
        except Exception:
            result_str = task["result"]

    events = task.get("events", [])
    processed_events = []
    for e in events:
        detail_str = ""
        if e.get("detail"):
            try:
                d = json.loads(e["detail"])
                if e["event_type"] == "tool_call":
                    detail_str = f'→ {d.get("tool","")}'
                    if d.get("input"):
                        detail_str += f' {_truncate(d["input"], 60)}'
                elif e["event_type"] == "tool_result":
                    detail_str = f'← {d.get("tool","")} · {_truncate(d.get("output",""), 60)}'
                elif e["event_type"] == "llm_end":
                    pt = d.get("prompt_tokens", 0)
                    ct = d.get("completion_tokens", 0)
                    detail_str = f'· {pt}+{ct} tokens'
                elif e["event_type"] in ("failed", "rejected"):
                    detail_str = f'· {d.get("error", d.get("reason", ""))}'
                elif e["event_type"] == "feedback_added":
                    detail_str = f'· verdict={d.get("verdict")} confidence={d.get("confidence")}'
                elif e["event_type"] == "execution_complete":
                    parts = [f'status={d.get("status","?")}']
                    ca = d.get("config_applied")
                    if ca is True:
                        parts.append("config ✅")
                    elif ca is False:
                        missing = d.get("missing_lines", [])
                        parts.append(f'config ❌ missing: {", ".join(missing[:2])}{"…" if len(missing) > 2 else ""}')
                    elif ca is None and "error" not in d:
                        parts.append("config ?")
                    detail_str = " · ".join(parts)
                elif e["event_type"] == "execution_verified":
                    ar = d.get("alert_resolved")
                    ttr = d.get("ttr_seconds", 0)
                    status = "alert ✅ resolved" if ar else ("alert ❌ still firing" if ar is False else "alert ?")
                    detail_str = f'· {status}'
                    if ttr:
                        mins, secs = divmod(ttr, 60)
                        detail_str += f' · TTR {mins}m {secs}s'
            except Exception:
                pass
        ts_short = e["timestamp"].split(" ")[1] if " " in e["timestamp"] else e["timestamp"]
        processed_events.append({"ts": ts_short, "type": e["event_type"], "agent": e["agent"], "detail": detail_str})

    feedback = task.get("feedback", [])
    processed_feedback = []
    for f in feedback:
        conf = f"{f['confidence']:.2f}" if f.get("confidence") is not None else ""
        processed_feedback.append({
            "from_agent": f["from_agent"],
            "verdict":    f["verdict"],
            "confidence": conf,
            "notes":      f.get("notes", ""),
        })

    # For rca tasks in approval/execution state, extract diff, resolution history,
    # and post-execution verification summary (config check + alert check).
    resolution_history: list[dict] = []
    config_diff: str = ""
    verification: dict = {}   # populated from execution_complete + execution_verified events
    _needs_approval_ctx = (
        task.get("type") == "rca"
        and task.get("status") in ("awaiting_approval", "complete", "rejected")
    ) or task.get("type") == "approval_gate"
    if _needs_approval_ctx:
        try:
            content_obj = json.loads(task.get("content") or "{}")
            device = (content_obj.get("device")
                      or content_obj.get("fix_proposal", {}).get("device", ""))
            alertname = (content_obj.get("alertname")
                         or content_obj.get("rca", {}).get("alertname", ""))
            config_diff = (
                content_obj.get("config_diff")
                or content_obj.get("fix_proposal", {}).get("config_diff", "")
            )
            if device:
                resolution_history = task_store.get_resolution_history(
                    alertname=alertname, device=device, limit=5
                )
        except Exception:
            pass

        # Build verification summary from events on this gate task
        for e in task.get("events", []):
            et = e.get("event_type", "")
            if et not in ("execution_complete", "execution_verified"):
                continue
            try:
                d = json.loads(e.get("detail") or "{}")
            except Exception:
                continue
            if et == "execution_complete":
                verification["exec_status"]     = d.get("status")
                verification["config_applied"]   = d.get("config_applied")
                verification["found_lines"]       = d.get("found_lines", [])
                verification["missing_lines"]     = d.get("missing_lines", [])
                verification["changes_applied"]   = d.get("changes_applied", "")
            elif et == "execution_verified":
                verification["alert_resolved"]    = d.get("alert_resolved")
                verification["ttr_seconds"]       = d.get("ttr_seconds", 0)
                verification["alertname"]         = d.get("alertname", "")
                verification["verify_device"]     = d.get("device", "")
                verification["check_at"]          = d.get("check_at", "")

    # Build analysis summary for rca tasks in approval/execution state so
    # the reviewer sees what each stage concluded before deciding to approve.
    analysis_summary: dict = {}
    topology_ctx: dict = {}
    if _needs_approval_ctx:
        try:
            c = json.loads(task.get("content") or "{}")
            rca  = c.get("rca", {})
            fix  = c.get("fix_proposal", {})
            analysis_summary = {
                "alertname":         c.get("alertname", ""),
                "escalation_reason": c.get("escalation_reason", ""),
                # RCA
                "diagnosis":         rca.get("diagnosis", ""),
                "rca_confidence":    rca.get("confidence", ""),
                "rca_action":        rca.get("recommended_action", ""),
                # Fix
                "fix_type":          fix.get("fix_type", ""),
                "fix_device":        fix.get("device", c.get("device", "")),
                "fix_commands":      fix.get("commands", ""),
                "fix_risk":          fix.get("risk", ""),
                "fix_confidence":    fix.get("confidence", ""),
                "fix_reason":        fix.get("reason", ""),
                # Validation
                "validation_verdict": c.get("validation_verdict", ""),
                "risk_confirmed":     c.get("risk_confirmed", ""),
                "chaos_notes":        c.get("chaos_notes", ""),
                # Gate metadata
                "gate_reason":       c.get("reason", ""),
            }
            # Topology context: leaf-symptom badge + blast radius
            topology_ctx = {
                "is_leaf_symptom":  bool(rca.get("is_leaf_symptom", False)),
                "upstream_cause":   rca.get("upstream_cause", ""),
                "affected_devices": rca.get("affected_devices", []),
            }
        except Exception:
            pass

    return {
        "task":               task,
        "task_id":            task_id,
        "chain":              chain,
        "content_str":        content_str,
        "result_str":         result_str,
        "events":             processed_events,
        "feedback":           processed_feedback,
        "type_icons":         TYPE_ICONS,
        "status_colors":      STATUS_COLORS,
        "age":                _age(task.get("created_at")),
        "resolution_history": resolution_history,
        "config_diff":        config_diff,
        "verification":       verification,
        "analysis_summary":   analysis_summary,
        "topology_ctx":       topology_ctx,
        "verify_delay_min":   max(1, settings.execution_verify_delay // 60),
    }


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    fps, task_ctx = await asyncio.gather(
        run_in_threadpool(_pipeline_fingerprints),
        run_in_threadpool(_task_queue_context),
    )
    sel_fp = fps[0][0] if fps else ""
    return templates.TemplateResponse(request, "pipeline.html", {
        "request":  request,
        "fps":      fps,
        "sel_fp":   sel_fp,
        **task_ctx,
    })


@app.get("/chat/{agent_name}", response_class=HTMLResponse)
async def chat_page(request: Request, agent_name: str):
    if agent_name not in AGENT_URLS:
        return HTMLResponse("Unknown agent", status_code=404)
    label, color = AGENT_LABELS[agent_name]
    return templates.TemplateResponse(request, "chat.html", {
        "request":      request,
        "agent_name":   agent_name,
        "agent_label":  label,
        "agent_color":  color,
        "quick_prompts": AGENT_QUICK_PROMPTS.get(agent_name, []),
        "session_id":   str(uuid.uuid4()),
    })


@app.get("/cost", response_class=HTMLResponse)
async def cost_page(request: Request):
    return templates.TemplateResponse(request, "cost_monitor.html", {"request": request})


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    records, summary = await asyncio.gather(
        run_in_threadpool(store.get_recent, limit=150),
        run_in_threadpool(store.summary),
    )
    return templates.TemplateResponse(request, "activity.html", {
        "request":  request,
        "records":  records,
        "summary":  summary,
        "truncate": _truncate,
    })


# ── Partial routes ────────────────────────────────────────────────────────────

@app.get("/partials/pending-approvals", response_class=HTMLResponse)
async def partial_pending_approvals(request: Request):
    count = len(await run_in_threadpool(task_store.list_tasks, status="awaiting_approval", limit=100))
    return templates.TemplateResponse(request, "partials/pending_approvals.html", {
        "request": request,
        "count":   count,
    })


@app.get("/partials/status-bar", response_class=HTMLResponse)
async def partial_status_bar(request: Request):
    agents = [("AI Agent", OPS_AGENT_URL)]
    badges = await asyncio.gather(*[
        _fetch_agent_health(_http_client, name, url) for name, url in agents
    ])
    return templates.TemplateResponse(request, "partials/status_bar.html", {"request": request, "badges": badges})


@app.get("/partials/agent-status", response_class=HTMLResponse)
async def partial_agent_status(request: Request):
    agents_cfg = [
        ("⚡", "AI Agent", OPS_AGENT_URL, "#6366f1"),
    ]
    n = len(agents_cfg)
    results = await asyncio.gather(
        *[_fetch_agent_status(_http_client, url) for _, _, url, _ in agents_cfg],
        *[_fetch_agent_usage(_http_client, url)  for _, _, url, _ in agents_cfg],
    )
    statuses = []
    for i, (icon, label, url, color) in enumerate(agents_cfg):
        status = results[i]
        statuses.append({
            "icon":    icon,
            "label":   label,
            "color":   color,
            "status":  status,
            "usage":   results[n + i],
            "age":     _age(status.get("started_at")),
            "truncate": _truncate,
        })
    return templates.TemplateResponse(request, "partials/agent_status.html", {"request": request, "agents": statuses})


@app.get("/partials/fingerprints", response_class=HTMLResponse)
async def partial_fingerprints(request: Request):
    fps = await run_in_threadpool(_pipeline_fingerprints)
    return templates.TemplateResponse(request, "partials/fingerprints.html", {"request": request, "fps": fps})


@app.get("/incidents", response_class=HTMLResponse)
async def incidents_page(request: Request, open_only: bool = True):
    ctx = await run_in_threadpool(_incident_list_context, open_only)
    return templates.TemplateResponse(request, "incidents.html", {
        "request":   request,
        "open_only": open_only,
        **ctx,
    })


@app.get("/partials/incidents", response_class=HTMLResponse)
async def partial_incidents(request: Request, open_only: bool = True):
    ctx = await run_in_threadpool(_incident_list_context, open_only)
    return templates.TemplateResponse(request, "partials/incident_list.html", {
        "request": request,
        **ctx,
    })


@app.get("/partials/incident/{incident_id}", response_class=HTMLResponse)
async def partial_incident_detail(request: Request, incident_id: str):
    inc, pipelines = await asyncio.gather(
        run_in_threadpool(task_store.get_task, incident_id),
        run_in_threadpool(task_store.get_incident_pipelines, incident_id),
    )
    if not inc:
        return HTMLResponse(f"<span class='muted'>Incident {incident_id} not found.</span>")
    try:
        content = json.loads(inc.get("content") or "{}")
    except Exception:
        content = {}
    return templates.TemplateResponse(request, "partials/incident_detail.html", {
        "request":   request,
        "incident":  inc,
        "content":   content,
        "pipelines": pipelines,
        "age":       _age(inc.get("created_at")),
        "type_icons":   TYPE_ICONS,
        "status_colors": STATUS_COLORS,
        "truncate":  _truncate,
    })


@app.post("/incidents/{incident_id}/close", response_class=HTMLResponse)
async def incident_close(request: Request, incident_id: str,
                         resolution: str = Form("")):
    inc = await run_in_threadpool(task_store.get_task, incident_id)
    if not inc:
        msg, ok = f"Incident `{incident_id}` not found.", False
    elif inc["status"] in ("complete", "rejected"):
        msg, ok = f"Incident `{incident_id}` is already {inc['status']}.", False
    else:
        await run_in_threadpool(task_store.close_incident, incident_id, resolution)
        msg, ok = f"✅ Incident `{incident_id}` closed.", True
    return templates.TemplateResponse(request, "partials/action_status.html",
                                      {"request": request, "msg": msg, "ok": ok})



@app.get("/partials/chronicle", response_class=HTMLResponse)
async def partial_chronicle(request: Request, fp: str = ""):
    ctx = await run_in_threadpool(_pipeline_chronicle_context, fp)
    return templates.TemplateResponse(request, "partials/pipeline_chronicle.html", {
        "request":          request,
        "verify_delay_min": max(1, settings.execution_verify_delay // 60),
        **ctx,
    })


@app.get("/partials/task-queue", response_class=HTMLResponse)
async def partial_task_queue(request: Request, status: str = "", type: str = "", tenant_id: str = ""):
    ctx = await run_in_threadpool(_task_queue_context, status, type, tenant_id)
    return templates.TemplateResponse(request, "partials/task_queue.html", {"request": request, **ctx})


@app.get("/partials/task/{task_id}", response_class=HTMLResponse)
async def partial_task_detail(request: Request, task_id: str):
    ctx = await run_in_threadpool(_task_detail_context, task_id)
    return templates.TemplateResponse(request, "partials/task_detail.html", {"request": request, **ctx})


@app.get("/partials/cost-kpis", response_class=HTMLResponse)
async def partial_cost_kpis(request: Request):
    _agent_usage_cfg = [
        ("ai_agent", OPS_AGENT_URL),
    ]
    usage_values, kpis, ts = await asyncio.gather(
        asyncio.gather(*[_fetch_agent_usage(_http_client, url) for _, url in _agent_usage_cfg]),
        run_in_threadpool(task_store.get_kpis),
        run_in_threadpool(_get_hourly_series, 24),
    )
    usages = {name: u for (name, _), u in zip(_agent_usage_cfg, usage_values)}
    today_cost  = sum(u.get("today", {}).get("cost_usd", 0.0) for u in usages.values())
    today_tok   = sum(u.get("today", {}).get("total_tokens", 0) for u in usages.values())
    hour_tok    = sum(u.get("this_hour", {}).get("total_tokens", 0) for u in usages.values())
    sample      = next(iter(usages.values()), {})
    budget      = sample.get("budget", {})
    daily_lim   = budget.get("daily_limit_usd", 5.0)
    remaining   = max(0.0, daily_lim - today_cost)
    pct_used    = min(100.0, today_cost / daily_lim * 100) if daily_lim else 0
    bar_color   = "#ef4444" if pct_used >= 90 else "#f59e0b" if pct_used >= 70 else "#22c55e"

    # 24-hour hourly time-series already fetched above via run_in_threadpool

    # Cumulative series for stat-card sparklines
    cum_cost: list[float] = []
    cum_tok:  list[float] = []
    rc = rt = 0.0
    for c, t in zip(ts["total_cost"], ts["total_tokens"]):
        rc += c;  cum_cost.append(rc)
        rt += t;  cum_tok.append(rt)
    budget_series = [max(0.0, daily_lim - c) for c in cum_cost]

    cost_sparkline   = _sparkline_svg(cum_cost,                              bar_color)
    tokens_sparkline = _sparkline_svg(cum_tok,                               "#6366f1")
    hour_sparkline   = _sparkline_svg([float(v) for v in ts["total_tokens"][-8:]], "#8b5cf6")
    budget_sparkline = _sparkline_svg(budget_series,                         "#22c55e")

    # Per-agent series for 24h charts
    _AGENT_DISPLAY = {
        "ai_agent":    ("Agent",  "#6366f1"),
        # Legacy keys for historical token_usage rows written before the merge
        "ops_agent":   ("Ops",    "#3b82f6"),
        "eng_agent":   ("Eng",    "#10b981"),
        "chaos_agent": ("Chaos",  "#8b5cf6"),
    }
    cost_series: list[tuple[str, str, list[float]]] = [
        ("Total", "#e2e8f0", ts["total_cost"])
    ]
    tok_series: list[tuple[str, str, list[float]]] = [
        ("Total", "#e2e8f0", [float(v) for v in ts["total_tokens"]])
    ]
    for ag, (lbl, clr) in _AGENT_DISPLAY.items():
        if ag in ts["by_agent_cost"]:
            cost_series.append((lbl, clr, ts["by_agent_cost"][ag]))
        if ag in ts["by_agent_tokens"]:
            tok_series.append((lbl, clr, [float(v) for v in ts["by_agent_tokens"][ag]]))

    def _fmt_cost(v: float) -> str:
        if v < 0.001: return f"${v:.5f}"
        if v < 0.01:  return f"${v:.4f}"
        return f"${v:.3f}"

    def _fmt_tok(v: float) -> str:
        return f"{v/1000:.1f}k" if v >= 1000 else str(int(v))

    cost_chart   = _chart_svg(cost_series, ts["labels"], fmt_y=_fmt_cost)
    tokens_chart = _chart_svg(tok_series,  ts["labels"], fmt_y=_fmt_tok)

    return templates.TemplateResponse(request, "partials/cost_kpis.html", {
        "request":          request,
        "today_cost":       today_cost,
        "today_tok":        today_tok,
        "hour_tok":         hour_tok,
        "daily_lim":        daily_lim,
        "remaining":        remaining,
        "pct_used":         pct_used,
        "bar_color":        bar_color,
        "usages":           usages,
        "kpis":             kpis,
        "cost_sparkline":   cost_sparkline,
        "tokens_sparkline": tokens_sparkline,
        "hour_sparkline":   hour_sparkline,
        "budget_sparkline": budget_sparkline,
        "cost_chart":       cost_chart,
        "tokens_chart":     tokens_chart,
    })


@app.get("/partials/activity", response_class=HTMLResponse)
async def partial_activity(request: Request, agent: str = "All"):
    f = None if agent == "All" else agent
    records, summary = await asyncio.gather(
        run_in_threadpool(store.get_recent, limit=150, agent_filter=f),
        run_in_threadpool(store.summary),
    )
    return templates.TemplateResponse(request, "partials/activity_table.html", {
        "request":  request,
        "records":  records,
        "summary":  summary,
        "agent":    agent,
        "truncate": _truncate,
    })


@app.get("/partials/activity/{record_id}", response_class=HTMLResponse)
async def partial_activity_detail(request: Request, record_id: int):
    records = await run_in_threadpool(store.get_recent, limit=500)
    record  = next((r for r in records if r["id"] == record_id), None)
    if not record:
        return HTMLResponse("<p>Record not found.</p>")
    calls = await run_in_threadpool(store.get_tool_calls, record["session_id"])
    return templates.TemplateResponse(request, "partials/activity_detail.html", {
        "request": request,
        "record":  record,
        "calls":   calls,
    })


# ── Policies + Intents pages ──────────────────────────────────────────────────

@app.get("/policies", response_class=HTMLResponse)
async def policies_page(request: Request):
    return templates.TemplateResponse(request, "policies.html", {"request": request})


@app.get("/intents", response_class=HTMLResponse)
async def intents_page(request: Request):
    return templates.TemplateResponse(request, "intents.html", {"request": request})


@app.get("/partials/policy-list", response_class=HTMLResponse)
async def partial_policy_list(request: Request, tenant_id: str = "default"):
    policies = await run_in_threadpool(task_store.list_policies, tenant_id=tenant_id)
    return templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
    })


@app.post("/partials/policy-create", response_class=HTMLResponse)
async def partial_policy_create(
    request: Request,
    name: str = Form(...),
    fix_type: str = Form(""),
    device_role: str = Form(""),
    environment: str = Form(""),
    autonomy_level: str = Form("L2"),
    tenant_id: str = "default",
):
    data = {
        "name":           name,
        "fix_type":       fix_type,
        "device_role":    device_role,
        "environment":    environment,
        "autonomy_level": autonomy_level,
        "tenant_id":      tenant_id,
    }
    await run_in_threadpool(task_store.create_policy, data)
    policies = await run_in_threadpool(task_store.list_policies, tenant_id=tenant_id)
    return templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
    })


@app.post("/partials/policy-toggle/{policy_id}", response_class=HTMLResponse)
async def partial_policy_toggle(request: Request, policy_id: str, tenant_id: str = "default"):
    existing = await run_in_threadpool(task_store.get_policy, policy_id)
    if existing:
        await run_in_threadpool(
            task_store.update_policy, policy_id, {"enabled": 0 if existing["enabled"] else 1}
        )
    policies = await run_in_threadpool(task_store.list_policies, tenant_id=tenant_id)
    return templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
    })


@app.delete("/partials/policy-delete/{policy_id}", response_class=HTMLResponse)
async def partial_policy_delete(request: Request, policy_id: str, tenant_id: str = "default"):
    await run_in_threadpool(task_store.delete_policy, policy_id)
    policies = await run_in_threadpool(task_store.list_policies, tenant_id=tenant_id)
    return templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
    })


@app.get("/partials/policy-performance", response_class=HTMLResponse)
async def partial_policy_performance(request: Request, tenant_id: str = "default"):
    stats   = await run_in_threadpool(task_store.get_policy_stats, tenant_id)
    all_pol = await run_in_threadpool(task_store.list_policies, tenant_id=tenant_id)
    names   = {p["id"]: p["name"] for p in all_pol}
    return templates.TemplateResponse(request, "partials/policy_performance.html", {
        "request":      request,
        "stats":        stats,
        "policy_names": names,
    })


@app.get("/partials/intent-list", response_class=HTMLResponse)
async def partial_intent_list(request: Request, tenant_id: str = "default"):
    intents = await run_in_threadpool(task_store.list_intents, tenant_id=tenant_id)
    return templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


@app.post("/partials/intent-create", response_class=HTMLResponse)
async def partial_intent_create(
    request: Request,
    name: str = Form(...),
    intent_type: str = Form(...),
    device: str = Form(""),
    alertname: str = Form(""),
    description: str = Form(""),
    tenant_id: str = "default",
):
    data = {
        "name":        name,
        "intent_type": intent_type,
        "device":      device,
        "alertname":   alertname,
        "description": description,
        "tenant_id":   tenant_id,
    }
    await run_in_threadpool(task_store.create_intent, data)
    intents = await run_in_threadpool(task_store.list_intents, tenant_id=tenant_id)
    return templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


@app.post("/partials/intent-toggle/{intent_id}", response_class=HTMLResponse)
async def partial_intent_toggle(request: Request, intent_id: str, tenant_id: str = "default"):
    existing = await run_in_threadpool(task_store.get_intent, intent_id)
    if existing:
        await run_in_threadpool(
            task_store.update_intent, intent_id, {"enabled": 0 if existing["enabled"] else 1}
        )
    intents = await run_in_threadpool(task_store.list_intents, tenant_id=tenant_id)
    return templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


@app.delete("/partials/intent-delete/{intent_id}", response_class=HTMLResponse)
async def partial_intent_delete(request: Request, intent_id: str, tenant_id: str = "default"):
    await run_in_threadpool(task_store.delete_intent, intent_id)
    intents = await run_in_threadpool(task_store.list_intents, tenant_id=tenant_id)
    return templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


# ── SSE task-change stream ────────────────────────────────────────────────────

@app.get("/stream/tasks")
async def stream_tasks(request: Request):
    """
    Server-Sent Events endpoint that emits a 'tasks-changed' event whenever task
    state changes.  Clients subscribe once and use the event to trigger targeted
    HTMX refreshes, replacing the need for periodic polling on the pipeline page.
    """
    async def generator():
        last_hash: int | None = None
        while True:
            if await request.is_disconnected():
                break
            try:
                tasks = await run_in_threadpool(task_store.list_tasks, limit=200)
                state_hash = hash(str([(t["id"], t["status"]) for t in tasks]))
                if state_hash != last_hash:
                    last_hash = state_hash
                    yield "event: tasks-changed\ndata: 1\n\n"
            except Exception:
                pass
            await asyncio.sleep(1)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Chat action ───────────────────────────────────────────────────────────────

@app.post("/chat/{agent_name}", response_class=HTMLResponse)
async def chat_send(
    request: Request,
    agent_name: str,
    message: str = Form(...),
    session_id: str = Form(""),
):
    if agent_name not in AGENT_URLS:
        return HTMLResponse("Unknown agent", status_code=404)
    agent_url = AGENT_URLS[agent_name]
    if not session_id:
        session_id = str(uuid.uuid4())

    import time
    start  = time.time()
    status = "success"
    tool_calls: list[dict] = []
    label, _ = AGENT_LABELS[agent_name]

    try:
        resp = await _http_client.post(
            f"{agent_url}/chat",
            json={"message": message, "session_id": session_id},
            timeout=120,
        )
        if resp.status_code == 429:
            response = f"⚠️ Budget limit reached: {resp.json().get('detail', 'token budget exhausted')}"
            status = "failed"
        else:
            resp.raise_for_status()
            body = resp.json()
            response   = body["response"]
            tool_calls = body.get("tool_calls", [])
    except httpx.ConnectError:
        status   = "failed"
        response = "⚠️ Agent service is not available. Please check that the service is running."
    except Exception as e:
        status   = "failed"
        response = f"⚠️ Error: {e}"

    latency_ms = int((time.time() - start) * 1000)
    await run_in_threadpool(
        store.record,
        agent=label.split()[-2] if " " in label else agent_name,
        session_id=session_id,
        message=message,
        response=response,
        status=status,
        latency_ms=latency_ms,
    )
    if tool_calls:
        await run_in_threadpool(
            store.record_tool_calls,
            agent=label.split()[-2] if " " in label else agent_name,
            session_id=session_id,
            tool_calls=tool_calls,
        )

    return templates.TemplateResponse(request, "partials/chat_message.html", {
        "request":    request,
        "message":    message,
        "response":   response,
        "session_id": session_id,
        "agent_name": agent_name,
    })


# ── Task management actions ────────────────────────────────────────────────────

@app.post("/tasks/{task_id}/approve", response_class=HTMLResponse)
async def task_approve(
    request: Request,
    task_id: str,
    operator_commands: str = Form(""),
):
    task = await run_in_threadpool(task_store.get_task, task_id)
    if not task:
        msg, ok = f"Task `{task_id}` not found.", False
    elif task["status"] != "awaiting_approval":
        msg, ok = f"Task `{task_id}` is `{task['status']}`, not awaiting approval.", False
    else:
        await run_in_threadpool(task_store.approve_task, task_id, "human")
        cmds = operator_commands.strip()
        try:
            await _http_client.post(
                f"{OPS_AGENT_URL}/workflow/resume/{task_id}",
                json={"operator_commands": cmds},
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning("UI: workflow resume call failed for task=%s: %s", task_id, exc)
        extra = " Operator commands queued for execution." if cmds else ""
        msg, ok = f"✅ Task `{task_id}` approved.{extra}", True
    return templates.TemplateResponse(request, "partials/action_status.html", {"request": request, "msg": msg, "ok": ok})


@app.post("/tasks/{task_id}/reject", response_class=HTMLResponse)
async def task_reject(request: Request, task_id: str, reason: str = Form("")):
    task = await run_in_threadpool(task_store.get_task, task_id)
    if not task:
        msg, ok = f"Task `{task_id}` not found.", False
    else:
        rejection_reason = reason.strip() or "Rejected via UI"
        await run_in_threadpool(task_store.reject_task, task_id, "human", rejection_reason)
        msg, ok = f"✅ Task `{task_id}` rejected.", True
    return templates.TemplateResponse(request, "partials/action_status.html", {"request": request, "msg": msg, "ok": ok})


@app.post("/tasks/clear", response_class=HTMLResponse)
async def tasks_clear(request: Request, confirmed: str = Form("no")):
    if confirmed != "yes":
        return templates.TemplateResponse(request, "partials/action_status.html", {
            "request": request,
            "msg": "⚠️ Add confirmed=yes to permanently delete all tasks.",
            "ok": False,
        })
    n = await run_in_threadpool(task_store.clear_all_tasks)
    extra = ""
    try:
        r = await _http_client.post(f"{OPS_AGENT_URL}/poller/reset", timeout=5)
        seeded = r.json().get("seeded_fingerprints", 0) if r.status_code == 200 else "?"
        extra = f" Poller reset ({seeded} fingerprints re-seeded)."
    except Exception:
        extra = " (Poller reset failed.)"
    return templates.TemplateResponse(request, "partials/action_status.html", {
        "request": request,
        "msg": f"🗑️ Cleared {n} task(s).{extra}",
        "ok": True,
    })


# ── Schedule management ───────────────────────────────────────────────────────

@app.get("/partials/schedules", response_class=HTMLResponse)
async def partial_schedules(request: Request):
    rows = []
    try:
        r = await _http_client.get(f"{OPS_AGENT_URL}/schedules", timeout=5)
        r.raise_for_status()
        rows = r.json()
    except Exception:
        pass
    return templates.TemplateResponse(request, "partials/schedule_table.html", {"request": request, "rows": rows, "truncate": _truncate})


@app.post("/schedules", response_class=HTMLResponse)
async def schedule_create(
    request: Request,
    scenario: str = Form(...),
    interval_minutes: int = Form(30),
):
    msg, ok = "", True
    if not scenario.strip():
        msg, ok = "⚠️ Please enter a scenario.", False
    else:
        try:
            r = await _http_client.post(
                f"{OPS_AGENT_URL}/schedule",
                json={"scenario": scenario, "interval_minutes": interval_minutes},
                timeout=10,
            )
            r.raise_for_status()
            job = r.json()
            msg = f"✅ Scheduled job `{job['job_id']}` every {interval_minutes} min."
        except Exception as e:
            msg, ok = f"❌ Error: {e}", False

    rows = []
    try:
        r = await _http_client.get(f"{OPS_AGENT_URL}/schedules", timeout=5)
        rows = r.json()
    except Exception:
        pass
    return templates.TemplateResponse(request, "partials/schedule_table.html", {
        "request":  request,
        "rows":     rows,
        "msg":      msg,
        "ok":       ok,
        "truncate": _truncate,
    })


@app.delete("/schedules/{job_id}", response_class=HTMLResponse)
async def schedule_cancel(request: Request, job_id: str):
    msg, ok = "", True
    try:
        r = await _http_client.delete(f"{OPS_AGENT_URL}/schedule/{job_id}", timeout=5)
        if r.status_code == 404:
            msg, ok = f"⚠️ Job `{job_id}` not found.", False
        else:
            r.raise_for_status()
            msg = f"✅ Cancelled job `{job_id}`."
    except Exception as e:
        msg, ok = f"❌ Error: {e}", False

    rows = []
    try:
        r = await _http_client.get(f"{OPS_AGENT_URL}/schedules", timeout=5)
        rows = r.json()
    except Exception:
        pass
    return templates.TemplateResponse(request, "partials/schedule_table.html", {
        "request":  request,
        "rows":     rows,
        "msg":      msg,
        "ok":       ok,
        "truncate": _truncate,
    })


# ── Config page (Policies + Intents merged) ───────────────────────────────────

@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    return templates.TemplateResponse(request, "config.html", {"request": request})


# ── System page (Activity + Cost merged) ──────────────────────────────────────

@app.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    records, summary = await asyncio.gather(
        run_in_threadpool(store.get_recent, limit=150),
        run_in_threadpool(store.summary),
    )
    return templates.TemplateResponse(request, "system.html", {
        "request": request,
        "records": records,
        "summary": summary,
        "truncate": _truncate,
    })


# ── Ops health KPI bar ────────────────────────────────────────────────────────

@app.get("/partials/ops-health", response_class=HTMLResponse)
async def partial_ops_health(request: Request):
    kpis, badges = await asyncio.gather(
        run_in_threadpool(task_store.get_kpis),
        asyncio.gather(*[
            _fetch_agent_health(_http_client, name, url)
            for name, url in [("AI Agent", OPS_AGENT_URL)]
        ]),
    )
    agent_online = badges[0]["label"] == "Online" if badges else False
    return templates.TemplateResponse(request, "partials/ops_health.html", {
        "request":      request,
        "kpis":         kpis,
        "agent_online": agent_online,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ui.main:app", host="0.0.0.0", port=7860, log_level="info")
