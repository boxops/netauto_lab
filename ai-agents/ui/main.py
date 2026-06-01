"""
Network AI Agents – FastAPI + Jinja2 + HTMX Web UI
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.activity_store import ActivityStore
from shared.task_store import TaskStore

# ── Config ────────────────────────────────────────────────────────────────────

OPS_AGENT_URL   = os.getenv("OPS_AGENT_URL",   "http://ai-ops-agent:8000")
ENG_AGENT_URL   = os.getenv("ENG_AGENT_URL",   "http://ai-eng-agent:8001")
CHAOS_AGENT_URL = os.getenv("CHAOS_AGENT_URL", "http://ai-chaos-agent:8002")

AGENT_URLS = {
    "ops":         OPS_AGENT_URL,
    "engineering": ENG_AGENT_URL,
    "chaos":       CHAOS_AGENT_URL,
}

AGENT_LABELS = {
    "ops":         ("🚨 Ops Agent",        "#3b82f6"),
    "engineering": ("🔧 Engineering Agent", "#10b981"),
    "chaos":       ("🔥 Chaos Agent",       "#f97316"),
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
    ],
    "engineering": [
        "Design BGP configuration for a new leaf router with AS 65104",
        "Show me all devices with their IP addresses",
        "What IP addresses are available in 10.10.0.0/16?",
        "Generate an Ansible playbook to configure VLANs 100-110 on all leaf switches",
        "Create documentation for the spine-leaf OSPF design",
        "Review this EOS config snippet for security issues",
        "What VLANs are currently configured on leaf1?",
        "Generate interface description standards for all uplinks in the lab",
        "Compare spine1's running config to its intended state in Nautobot",
        "Show all prefixes in the 192.168.0.0/16 supernet and their utilization",
        "Create a change request template for adding a new leaf router",
        "Validate the IP addressing scheme across all devices for inconsistencies",
        "Generate a topology description for the current spine-leaf design",
        "What interfaces on leaf2 are currently unpatched or unused?",
    ],
    "chaos": [
        "Propose a safe chaos test for BGP flap detection in this lab",
        "Simulate a leaf uplink failure on leaf1 in check mode",
        "What is the expected blast radius if I bounce Ethernet1 on spine1?",
        "Create a rollback-first chaos runbook for testing alert correlation",
        "Design a 15-minute game day with one controlled failure and validation steps",
        "Shut down Ethernet1 on leaf2 in check mode and show me what alerts would fire",
        "Run a connectivity validation test across all leaf-spine links",
        "What would happen if spine2 went completely offline? Assess redundancy.",
        "Design a chaos test for validating OSPF reconvergence time after a link failure",
        "Generate a pre-chaos checklist for a scheduled game day",
        "Estimate recovery time for a simultaneous dual-uplink failure on leaf3",
        "Create a post-chaos incident report template for today's test session",
        "Test BGP route withdrawal and recovery on spine1 in check mode",
        "What monitoring gaps might chaos tests expose in the current alerting setup?",
    ],
}

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
    "rca":           "🔍",
    "fix_proposal":  "🔧",
    "validation":    "✅",
    "approval_gate": "🔐",
}

# ── App setup ─────────────────────────────────────────────────────────────────

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TMPL_DIR   = os.path.join(BASE_DIR, "templates")

app = FastAPI(title="Network AI Agents", description="Network Automation AI Agents UI", version="2.0.0")
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


def _get_pipeline_tasks(fp: str) -> dict[str, dict | None]:
    stages = ["rca", "fix_proposal", "validation", "approval_gate"]
    by_type: dict[str, dict | None] = {s: None for s in stages}
    if not fp:
        return by_type
    tasks = task_store.list_tasks(alert_fingerprint=fp, limit=50)
    for t in tasks:
        tp = t["type"]
        if tp in by_type:
            cur = by_type[tp]
            if cur is None or t["created_at"] > cur["created_at"]:
                by_type[tp] = t
    return by_type


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


def _task_queue_context(status_filter: str = "", type_filter: str = "") -> dict:
    tasks = task_store.list_tasks(
        status=status_filter or None,
        type=type_filter or None,
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
    return {"tasks": rows, "status_filter": status_filter, "type_filter": type_filter}


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

    return {
        "task":        task,
        "task_id":     task_id,
        "chain":       chain,
        "content_str": content_str,
        "result_str":  result_str,
        "events":      processed_events,
        "feedback":    processed_feedback,
        "type_icons":  TYPE_ICONS,
        "status_colors": STATUS_COLORS,
        "age":         _age(task.get("created_at")),
    }


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    fps = _pipeline_fingerprints()
    sel_fp = fps[0][0] if fps else ""
    pipeline_tasks = _get_pipeline_tasks(sel_fp)
    task_ctx = _task_queue_context()
    kpis = task_store.get_kpis()
    return templates.TemplateResponse(request, "pipeline.html", {
        "request":        request,
        "fps":            fps,
        "sel_fp":         sel_fp,
        "pipeline_tasks": pipeline_tasks,
        "type_icons":     TYPE_ICONS,
        "status_colors":  STATUS_COLORS,
        "truncate":       _truncate,
        "age":            _age,
        **task_ctx,
        "kpis":           kpis,
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


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    records = store.get_recent(limit=150)
    summary = store.summary()
    return templates.TemplateResponse(request, "activity.html", {
        "request":  request,
        "records":  records,
        "summary":  summary,
        "truncate": _truncate,
    })


# ── Partial routes ────────────────────────────────────────────────────────────

@app.get("/partials/status-bar", response_class=HTMLResponse)
async def partial_status_bar(request: Request):
    agents = [("🚨 Ops", OPS_AGENT_URL), ("🔧 Engineering", ENG_AGENT_URL), ("🔥 Chaos", CHAOS_AGENT_URL)]
    async with httpx.AsyncClient() as client:
        badges = [await _fetch_agent_health(client, name, url) for name, url in agents]
    return templates.TemplateResponse(request, "partials/status_bar.html", {"request": request, "badges": badges})


@app.get("/partials/agent-status", response_class=HTMLResponse)
async def partial_agent_status(request: Request):
    agents_cfg = [
        ("🚨", "Ops Agent",    OPS_AGENT_URL,   "#3b82f6"),
        ("🔧", "Engineering",  ENG_AGENT_URL,   "#10b981"),
        ("🔥", "Chaos Agent",  CHAOS_AGENT_URL, "#f97316"),
    ]
    async with httpx.AsyncClient() as client:
        statuses = []
        for icon, label, url, color in agents_cfg:
            status  = await _fetch_agent_status(client, url)
            usage   = await _fetch_agent_usage(client, url)
            statuses.append({
                "icon":    icon,
                "label":   label,
                "color":   color,
                "status":  status,
                "usage":   usage,
                "age":     _age(status.get("started_at")),
                "truncate": _truncate,
            })
    return templates.TemplateResponse(request, "partials/agent_status.html", {"request": request, "agents": statuses})


@app.get("/partials/fingerprints", response_class=HTMLResponse)
async def partial_fingerprints(request: Request):
    fps = _pipeline_fingerprints()
    return templates.TemplateResponse(request, "partials/fingerprints.html", {"request": request, "fps": fps})


@app.get("/partials/pipeline", response_class=HTMLResponse)
async def partial_pipeline(request: Request, fp: str = ""):
    pipeline_tasks = _get_pipeline_tasks(fp)
    return templates.TemplateResponse(request, "partials/pipeline_visual.html", {
        "request":        request,
        "fp":             fp,
        "pipeline_tasks": pipeline_tasks,
        "type_icons":     TYPE_ICONS,
        "status_colors":  STATUS_COLORS,
        "truncate":       _truncate,
        "age":            _age,
    })


@app.get("/partials/task-queue", response_class=HTMLResponse)
async def partial_task_queue(request: Request, status: str = "", type: str = ""):
    ctx = _task_queue_context(status, type)
    return templates.TemplateResponse(request, "partials/task_queue.html", {"request": request, **ctx})


@app.get("/partials/task/{task_id}", response_class=HTMLResponse)
async def partial_task_detail(request: Request, task_id: str):
    ctx = _task_detail_context(task_id)
    return templates.TemplateResponse(request, "partials/task_detail.html", {"request": request, **ctx})


@app.get("/partials/cost-kpis", response_class=HTMLResponse)
async def partial_cost_kpis(request: Request):
    async with httpx.AsyncClient() as client:
        usages = {}
        for name, url in [("ops_agent", OPS_AGENT_URL), ("eng_agent", ENG_AGENT_URL), ("chaos_agent", CHAOS_AGENT_URL)]:
            usages[name] = await _fetch_agent_usage(client, url)
    kpis = task_store.get_kpis()
    today_cost  = sum(u.get("today", {}).get("cost_usd", 0.0) for u in usages.values())
    today_tok   = sum(u.get("today", {}).get("total_tokens", 0) for u in usages.values())
    hour_tok    = sum(u.get("this_hour", {}).get("total_tokens", 0) for u in usages.values())
    sample      = next(iter(usages.values()), {})
    budget      = sample.get("budget", {})
    daily_lim   = budget.get("daily_limit_usd", 5.0)
    remaining   = max(0.0, daily_lim - today_cost)
    pct_used    = min(100.0, today_cost / daily_lim * 100) if daily_lim else 0
    bar_color   = "#ef4444" if pct_used >= 90 else "#f59e0b" if pct_used >= 70 else "#22c55e"
    return templates.TemplateResponse(request, "partials/cost_kpis.html", {
        "request":    request,
        "today_cost": today_cost,
        "today_tok":  today_tok,
        "hour_tok":   hour_tok,
        "daily_lim":  daily_lim,
        "remaining":  remaining,
        "pct_used":   pct_used,
        "bar_color":  bar_color,
        "usages":     usages,
        "kpis":       kpis,
    })


@app.get("/partials/activity", response_class=HTMLResponse)
async def partial_activity(request: Request, agent: str = "All"):
    f = None if agent == "All" else agent
    records = store.get_recent(limit=150, agent_filter=f)
    summary = store.summary()
    return templates.TemplateResponse(request, "partials/activity_table.html", {
        "request":  request,
        "records":  records,
        "summary":  summary,
        "agent":    agent,
        "truncate": _truncate,
    })


@app.get("/partials/activity/{record_id}", response_class=HTMLResponse)
async def partial_activity_detail(request: Request, record_id: int):
    records = store.get_recent(limit=500)
    record  = next((r for r in records if r["id"] == record_id), None)
    if not record:
        return HTMLResponse("<p>Record not found.</p>")
    calls = store.get_tool_calls(record["session_id"])
    return templates.TemplateResponse(request, "partials/activity_detail.html", {
        "request": request,
        "record":  record,
        "calls":   calls,
    })


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
        async with httpx.AsyncClient() as client:
            resp = await client.post(
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
    store.record(
        agent=label.split()[-2] if " " in label else agent_name,
        session_id=session_id,
        message=message,
        response=response,
        status=status,
        latency_ms=latency_ms,
    )
    if tool_calls:
        store.record_tool_calls(
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
async def task_approve(request: Request, task_id: str):
    task = task_store.get_task(task_id)
    if not task:
        msg, ok = f"Task `{task_id}` not found.", False
    elif task["status"] != "awaiting_approval":
        msg, ok = f"Task `{task_id}` is `{task['status']}`, not awaiting approval.", False
    else:
        task_store.approve_task(task_id, "human")
        msg, ok = f"✅ Task `{task_id}` approved.", True
    return templates.TemplateResponse(request, "partials/action_status.html", {"request": request, "msg": msg, "ok": ok})


@app.post("/tasks/{task_id}/reject", response_class=HTMLResponse)
async def task_reject(request: Request, task_id: str):
    task = task_store.get_task(task_id)
    if not task:
        msg, ok = f"Task `{task_id}` not found.", False
    else:
        task_store.reject_task(task_id, "human", "Rejected via UI")
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
    n = task_store.clear_all_tasks()
    extra = ""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{OPS_AGENT_URL}/poller/reset", timeout=5)
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
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{CHAOS_AGENT_URL}/schedules", timeout=5)
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
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{CHAOS_AGENT_URL}/schedule",
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
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{CHAOS_AGENT_URL}/schedules", timeout=5)
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
        async with httpx.AsyncClient() as client:
            r = await client.delete(f"{CHAOS_AGENT_URL}/schedule/{job_id}", timeout=5)
            if r.status_code == 404:
                msg, ok = f"⚠️ Job `{job_id}` not found.", False
            else:
                r.raise_for_status()
                msg = f"✅ Cancelled job `{job_id}`."
    except Exception as e:
        msg, ok = f"❌ Error: {e}", False

    rows = []
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{CHAOS_AGENT_URL}/schedules", timeout=5)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ui.main:app", host="0.0.0.0", port=7860, log_level="info")
