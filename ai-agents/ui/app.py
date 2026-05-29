"""
Network AI Agents – Gradio Web UI
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import gradio as gr
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.activity_store import ActivityStore
from shared.task_store import TaskStore

OPS_AGENT_URL   = os.getenv("OPS_AGENT_URL",   "http://ai-ops-agent:8000")
ENG_AGENT_URL   = os.getenv("ENG_AGENT_URL",   "http://ai-eng-agent:8001")
CHAOS_AGENT_URL = os.getenv("CHAOS_AGENT_URL", "http://ai-chaos-agent:8002")

store      = ActivityStore()
task_store = TaskStore()

AGENT_LABELS = {"Ops": OPS_AGENT_URL, "Engineering": ENG_AGENT_URL, "Chaos": CHAOS_AGENT_URL}
AGENT_COLORS = {"Ops": "#3b82f6", "Engineering": "#10b981", "Chaos": "#f97316"}

_AGENT_URLS = {
    "ops_agent":   OPS_AGENT_URL,
    "eng_agent":   ENG_AGENT_URL,
    "chaos_agent": CHAOS_AGENT_URL,
}

_STATUS_COLORS = {
    "pending":            "#f59e0b",
    "claimed":            "#3b82f6",
    "running":            "#22c55e",
    "awaiting_approval":  "#a855f7",
    "complete":           "#6b7280",
    "failed":             "#ef4444",
    "rejected":           "#9ca3af",
}

_PRIORITY_COLORS = {
    "critical": "#ef4444",
    "high":     "#f97316",
    "normal":   "#3b82f6",
    "low":      "#6b7280",
}

_TYPE_ICONS = {
    "rca":           "🔍",
    "fix_proposal":  "🔧",
    "validation":    "✅",
    "approval_gate": "🔐",
}


def _agent_name(agent_url: str) -> str:
    for name, url in AGENT_LABELS.items():
        if url == agent_url:
            return name
    return "Unknown"


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


# ── Chat helpers ──────────────────────────────────────────────────────────────

def _chat_with_agent(agent_url: str, message: str, history: list, session_id: str):
    if not session_id:
        session_id = str(uuid.uuid4())
    agent = _agent_name(agent_url)
    start = time.time()
    status = "success"
    tool_calls: list[dict] = []
    try:
        resp = httpx.post(
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
            response = body["response"]
            tool_calls = body.get("tool_calls", [])
    except httpx.ConnectError:
        status = "failed"
        response = "⚠️ Agent service is not available. Please check that the service is running."
    except Exception as e:
        status = "failed"
        response = f"⚠️ Error: {e}"

    latency_ms = int((time.time() - start) * 1000)
    store.record(
        agent=agent, session_id=session_id, message=message,
        response=response, status=status, latency_ms=latency_ms,
    )
    if tool_calls:
        store.record_tool_calls(agent=agent, session_id=session_id, tool_calls=tool_calls)

    history = history or []
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": response})
    return "", history, session_id


def ops_chat(message, history, session_id):
    return _chat_with_agent(OPS_AGENT_URL, message, history, session_id)


def eng_chat(message, history, session_id):
    return _chat_with_agent(ENG_AGENT_URL, message, history, session_id)


def chaos_chat(message, history, session_id):
    return _chat_with_agent(CHAOS_AGENT_URL, message, history, session_id)


# ── Status bar ────────────────────────────────────────────────────────────────

def _status_bar_html() -> str:
    agents = [
        ("🚨 Ops", OPS_AGENT_URL),
        ("🔧 Engineering", ENG_AGENT_URL),
        ("🔥 Chaos", CHAOS_AGENT_URL),
    ]
    badges = []
    for name, base_url in agents:
        try:
            r = httpx.get(f"{base_url}/health", timeout=3)
            label, color = ("Online", "#22c55e") if r.status_code == 200 else (f"HTTP {r.status_code}", "#f59e0b")
        except Exception:
            label, color = "Offline", "#ef4444"

        badges.append(
            f'<span style="display:inline-flex;align-items:center;gap:6px;'
            f'background:{color}18;border:1px solid {color}55;border-radius:20px;'
            f'padding:4px 14px 4px 10px;font-size:0.82em;white-space:nowrap">'
            f'<span style="width:8px;height:8px;border-radius:50%;background:{color};'
            f'flex-shrink:0;display:inline-block"></span>'
            f'<span style="font-weight:600">{name}</span>'
            f'&nbsp;<span style="color:{color};font-weight:500">{label}</span>'
            f'</span>'
        )

    return (
        f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;'
        f'padding:10px 0 14px 0;border-bottom:1px solid var(--border-color-primary);'
        f'margin-bottom:6px">'
        + "".join(badges) +
        f'<span style="margin-left:auto;font-size:0.75em;color:var(--body-text-color-subdued)">'
        f'Refreshes every 30 s</span>'
        f'</div>'
    )


# ── Activity helpers ──────────────────────────────────────────────────────────

def _activity_data(agent_filter: str = "All") -> tuple[list[list], list[dict]]:
    f = None if agent_filter == "All" else agent_filter
    records = store.get_recent(limit=150, agent_filter=f)
    table_rows = [
        [
            r["timestamp"],
            r["agent"],
            "✅ success" if r["status"] == "success" else "❌ failed",
            f'{r["latency_ms"]:,} ms',
            r["session_id"][:8],
            _truncate(r["message"], 80),
            _truncate(r["response"], 100),
        ]
        for r in records
    ]
    return table_rows, records


def _activity_summary_html() -> str:
    s = store.summary()
    if s["total"] == 0:
        return '<p style="color:var(--body-text-color-subdued);margin:12px 0">No activity recorded yet.</p>'

    cards_data = [
        ("Total", s["total"], "#6366f1"),
        ("Success", s["success"], "#22c55e"),
        ("Failed", s["failed"], "#ef4444"),
    ]
    for agent, count in sorted(s["by_agent"].items()):
        cards_data.append((agent, count, AGENT_COLORS.get(agent, "#6b7280")))

    cards = "".join(
        f'<div style="background:var(--background-fill-secondary);border-radius:10px;'
        f'padding:12px 20px;text-align:center;border:1px solid var(--border-color-primary);'
        f'border-top:3px solid {color};min-width:88px">'
        f'<div style="font-size:1.7em;font-weight:700;color:{color};line-height:1">{count}</div>'
        f'<div style="font-size:0.78em;color:var(--body-text-color-subdued);margin-top:4px">{label}</div>'
        f'</div>'
        for label, count, color in cards_data
    )
    return f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin:10px 0 16px 0">{cards}</div>'


def get_activity_view(agent_filter: str = "All") -> tuple[list[list], list[dict], str]:
    rows, records = _activity_data(agent_filter)
    return rows, records, _activity_summary_html()


def show_activity_detail(evt: gr.SelectData, full_records: list) -> str:
    try:
        record = full_records[evt.index[0]]
    except (IndexError, TypeError):
        return "*Click a row to see the full interaction.*"

    session_id = record["session_id"]
    calls = store.get_tool_calls(session_id)
    status_icon = "✅" if record["status"] == "success" else "❌"
    agent_icons = {"Ops": "🚨", "Engineering": "🔧", "Chaos": "🔥"}
    agent_icon = agent_icons.get(record["agent"], "🤖")

    lines = [
        f"## {agent_icon} {record['agent']} Agent &nbsp; {status_icon} {record['status']} "
        f"&nbsp; · &nbsp; {record['latency_ms']:,} ms",
        f"*{record['timestamp']}*", "", "---", "",
        "### 💬 User Message", "", record["message"], "", "---", "",
        "### 🤖 Agent Response", "", record["response"],
    ]
    if calls:
        lines += ["", "---", "", f"### 🔧 Tools Used ({len(calls)} call{'s' if len(calls) != 1 else ''})"]
        for i, c in enumerate(calls, 1):
            inp = (c.get("input_summary") or "").strip()
            out = (c.get("output_summary") or "").strip()
            lines += ["", f"**{i}. `{c['tool_name']}`**"]
            if inp:
                lines += ["", "*Input:*", "```", inp, "```"]
            if out:
                lines += ["", "*Output:*", "```", out, "```"]
    else:
        lines += ["", "---", "", "*No tool calls recorded for this interaction.*"]

    return "\n".join(lines)


# ── Schedule helpers ──────────────────────────────────────────────────────────

def _schedule_rows() -> list[list]:
    try:
        resp = httpx.get(f"{CHAOS_AGENT_URL}/schedules", timeout=5)
        resp.raise_for_status()
        jobs = resp.json()
        return [
            [j["job_id"], j["interval_minutes"], _truncate(j["scenario"], 80),
             j.get("next_run", "—"), j.get("last_run") or "—", j.get("last_status") or "—"]
            for j in jobs
        ]
    except Exception:
        return []


def create_schedule(scenario: str, interval: int) -> tuple[str, list[list]]:
    if not scenario.strip():
        return "⚠️ Please enter a scenario.", _schedule_rows()
    try:
        resp = httpx.post(
            f"{CHAOS_AGENT_URL}/schedule",
            json={"scenario": scenario, "interval_minutes": int(interval)},
            timeout=10,
        )
        resp.raise_for_status()
        job = resp.json()
        return f"✅ Scheduled job `{job['job_id']}` every {interval} min.", _schedule_rows()
    except Exception as e:
        return f"❌ Error: {e}", _schedule_rows()


def cancel_schedule(job_id: str) -> tuple[str, list[list]]:
    if not job_id.strip():
        return "⚠️ Enter a job ID to cancel.", _schedule_rows()
    try:
        resp = httpx.delete(f"{CHAOS_AGENT_URL}/schedule/{job_id.strip()}", timeout=5)
        if resp.status_code == 404:
            return f"⚠️ Job `{job_id}` not found.", _schedule_rows()
        resp.raise_for_status()
        return f"✅ Cancelled job `{job_id}`.", _schedule_rows()
    except Exception as e:
        return f"❌ Error: {e}", _schedule_rows()


# ── Pipeline Dashboard helpers ────────────────────────────────────────────────

def _fetch_agent_status(url: str) -> dict:
    try:
        r = httpx.get(f"{url}/status", timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"state": "unreachable", "agent_name": ""}


def _state_badge(state: str) -> tuple[str, str]:
    """Return (label, colour) for a state string."""
    mapping = {
        "idle":          ("IDLE",         "#6b7280"),
        "thinking":      ("THINKING",     "#3b82f6"),
        "calling_tool":  ("CALLING TOOL", "#f59e0b"),
        "writing_result":("WRITING",      "#22c55e"),
        "unreachable":   ("OFFLINE",      "#ef4444"),
    }
    return mapping.get(state, (state.upper(), "#6b7280"))


def _agent_card_html(
    icon: str,
    label: str,
    url: str,
    color: str,
    status_data: dict,
    usage_data: dict,
) -> str:
    state       = status_data.get("state", "idle")
    task_id     = status_data.get("task_id") or "—"
    tool        = status_data.get("current_tool") or ""
    tool_input  = status_data.get("tool_input_preview") or ""
    started_at  = status_data.get("started_at") or ""
    last_event  = status_data.get("last_event_at") or ""
    tokens_hr   = status_data.get("tokens_this_hour", 0)

    state_label, state_color = _state_badge(state)

    # budget bar
    max_tokens = 50_000
    pct = min(100, int(tokens_hr / max_tokens * 100)) if max_tokens else 0
    bar_color = "#ef4444" if pct >= 90 else "#f59e0b" if pct >= 70 else "#22c55e"

    # tool line
    tool_line = ""
    if tool:
        preview = f"({_truncate(tool_input, 40)})" if tool_input else ""
        tool_line = (
            f'<div style="font-size:0.78em;color:#f59e0b;margin-top:4px;font-family:monospace">'
            f'⚡ {tool} {preview}</div>'
        )

    # task line
    task_line = ""
    if task_id != "—":
        age_str = _age(started_at)
        task_line = (
            f'<div style="font-size:0.76em;color:var(--body-text-color-subdued);margin-top:2px">'
            f'Task: <code>{task_id}</code> · {age_str}</div>'
        )

    # pulse animation for active states
    pulse = (
        'animation:pulse 1.5s infinite;'
        if state in ("thinking", "calling_tool", "writing_result")
        else ""
    )

    today_cost = usage_data.get("today", {}).get("cost_usd", 0.0) if usage_data else 0.0

    return (
        f'<div style="background:var(--background-fill-secondary);border-radius:12px;'
        f'padding:16px;border:1px solid var(--border-color-primary);'
        f'border-top:3px solid {color};flex:1;min-width:200px">'

        f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">'
        f'<span style="font-weight:700;font-size:0.95em">{icon} {label}</span>'
        f'<span style="background:{state_color}22;color:{state_color};border:1px solid {state_color}55;'
        f'border-radius:12px;padding:2px 10px;font-size:0.75em;font-weight:600;{pulse}">'
        f'{state_label}</span>'
        f'</div>'

        f'{tool_line}'
        f'{task_line}'

        f'<div style="margin-top:10px">'
        f'<div style="display:flex;justify-content:space-between;font-size:0.72em;'
        f'color:var(--body-text-color-subdued);margin-bottom:3px">'
        f'<span>Tokens/hr: {tokens_hr:,} / 50,000</span>'
        f'<span>Today: ${today_cost:.4f}</span>'
        f'</div>'
        f'<div style="background:var(--border-color-primary);border-radius:4px;height:5px">'
        f'<div style="background:{bar_color};width:{pct}%;height:5px;border-radius:4px;'
        f'transition:width 0.5s"></div>'
        f'</div>'
        f'</div>'
        f'</div>'
    )


def _live_agent_status_html() -> str:
    agents = [
        ("🚨", "Ops Agent",    OPS_AGENT_URL,   "#3b82f6"),
        ("🔧", "Engineering",  ENG_AGENT_URL,   "#10b981"),
        ("🔥", "Chaos Agent",  CHAOS_AGENT_URL, "#f97316"),
    ]
    cards = []
    for icon, label, url, color in agents:
        status_data = _fetch_agent_status(url)
        usage_data  = {}
        try:
            r = httpx.get(f"{url}/usage", timeout=3)
            if r.status_code == 200:
                usage_data = r.json()
        except Exception:
            pass
        cards.append(_agent_card_html(icon, label, url, color, status_data, usage_data))

    return (
        '<style>'
        '@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.6}}'
        '</style>'
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:4px">'
        + "".join(cards) +
        '</div>'
    )


def _task_queue_rows(status_filter: str, type_filter: str) -> tuple[list[list], list[dict]]:
    tasks = task_store.list_tasks(
        status=status_filter or None,
        type=type_filter or None,
        limit=200,
    )
    rows = []
    for t in tasks:
        sc = _STATUS_COLORS.get(t["status"], "#6b7280")
        pc = _PRIORITY_COLORS.get(t["priority"], "#6b7280")
        icon = _TYPE_ICONS.get(t["type"], "📋")
        rows.append([
            t["id"],
            f'{icon} {t["type"]}',
            t["status"],
            t["priority"],
            t.get("assigned_to") or "—",
            t.get("created_by") or "—",
            _truncate(t.get("title") or "", 50),
            _age(t.get("created_at")),
        ])
    return rows, tasks


def _task_detail_md(task_id: str) -> str:
    if not task_id:
        return "*Select a task row to see details.*"
    task = task_store.get_task(task_id)
    if not task:
        return f"*Task `{task_id}` not found.*"

    chain = task_store.get_task_chain(task_id)
    chain_str = " → ".join(
        f"`{t['id']}`{'  ← **(current)**' if t['id'] == task_id else ''}"
        for t in chain
    )

    children = task.get("children", [])
    children_str = ""
    if children:
        children_str = (
            "\n**Children:** "
            + " | ".join(f"`{c['id']}` ({c['type']}, {c['status']})" for c in children)
        )

    # content
    try:
        content_obj = json.loads(task.get("content") or "{}")
        content_str = json.dumps(content_obj, indent=2)
    except Exception:
        content_str = task.get("content") or ""

    # result
    result_str = ""
    if task.get("result"):
        try:
            result_obj = json.loads(task["result"])
            result_str = "\n\n### Result\n```json\n" + json.dumps(result_obj, indent=2) + "\n```"
        except Exception:
            result_str = "\n\n### Result\n" + task["result"]

    # events timeline
    events = task.get("events", [])
    event_lines = []
    for e in events:
        detail = ""
        if e.get("detail"):
            try:
                d = json.loads(e["detail"])
                if e["event_type"] == "tool_call":
                    detail = f' → `{d.get("tool","")}`'
                    if d.get("input"):
                        detail += f' `{_truncate(d["input"], 60)}`'
                elif e["event_type"] == "tool_result":
                    detail = f' ← `{d.get("tool","")}` · {_truncate(d.get("output",""), 60)}'
                elif e["event_type"] in ("llm_end",):
                    pt = d.get("prompt_tokens", 0)
                    ct = d.get("completion_tokens", 0)
                    detail = f' · {pt}+{ct} tokens'
                elif e["event_type"] in ("failed", "rejected"):
                    detail = f' · {d.get("error", d.get("reason", ""))}'
                elif e["event_type"] == "feedback_added":
                    detail = f' · verdict={d.get("verdict")} confidence={d.get("confidence")}'
            except Exception:
                pass
        ts_short = e["timestamp"].split(" ")[1] if " " in e["timestamp"] else e["timestamp"]
        event_lines.append(f"  `{ts_short}` **{e['event_type']}** ({e['agent']}){detail}")

    events_section = "\n### Timeline\n" + ("\n".join(event_lines) if event_lines else "*No events yet.*")

    # feedback
    feedback = task.get("feedback", [])
    fb_lines = []
    for f in feedback:
        conf = f"confidence={f['confidence']:.2f}" if f.get("confidence") is not None else ""
        fb_lines.append(
            f"  - **{f['from_agent']}** → `{f['verdict']}` {conf}"
            + (f"\n    *{f['notes']}*" if f.get("notes") else "")
        )
    feedback_section = "\n### Feedback\n" + ("\n".join(fb_lines) if fb_lines else "*No feedback yet.*")

    sc = _STATUS_COLORS.get(task["status"], "#6b7280")

    return (
        f"## {_TYPE_ICONS.get(task['type'], '📋')} Task `{task['id']}` "
        f"— **{task['status'].upper()}**\n\n"
        f"**Chain:** {chain_str}{children_str}\n\n"
        f"| Field | Value |\n|---|---|\n"
        f"| Type | {task['type']} |\n"
        f"| Priority | {task['priority']} |\n"
        f"| Created by | {task.get('created_by','—')} |\n"
        f"| Assigned to | {task.get('assigned_to','—')} |\n"
        f"| Created | {task.get('created_at','—')} |\n"
        f"| Claimed | {task.get('claimed_at') or '—'} |\n"
        f"| Completed | {task.get('completed_at') or '—'} |\n\n"
        f"### Input\n```json\n{content_str}\n```"
        f"{result_str}\n"
        f"{events_section}\n"
        f"{feedback_section}"
    )


def _cost_kpi_html() -> str:
    # Fetch usage from all three agents
    all_usage = {}
    for name, url in [("ops_agent", OPS_AGENT_URL), ("eng_agent", ENG_AGENT_URL), ("chaos_agent", CHAOS_AGENT_URL)]:
        try:
            r = httpx.get(f"{url}/usage", timeout=3)
            if r.status_code == 200:
                all_usage[name] = r.json()
        except Exception:
            pass

    today_cost  = sum(u.get("today", {}).get("cost_usd", 0.0) for u in all_usage.values())
    today_tok   = sum(u.get("today", {}).get("total_tokens", 0) for u in all_usage.values())
    hour_tok    = sum(u.get("this_hour", {}).get("total_tokens", 0) for u in all_usage.values())

    # Get budget from one agent's response (they share the same config)
    sample = next(iter(all_usage.values()), {})
    budget      = sample.get("budget", {})
    daily_lim   = budget.get("daily_limit_usd", 5.0)
    remaining   = max(0.0, daily_lim - today_cost)
    pct_used    = min(100.0, today_cost / daily_lim * 100) if daily_lim else 0
    bar_color   = "#ef4444" if pct_used >= 90 else "#f59e0b" if pct_used >= 70 else "#22c55e"

    kpis = task_store.get_kpis()
    today_t     = kpis["today"]
    rates       = kpis["rates"]

    def _metric_card(title: str, value: str, sub: str, color: str, extra_html: str = "") -> str:
        return (
            f'<div style="background:var(--background-fill-secondary);border-radius:10px;'
            f'padding:14px 18px;border:1px solid var(--border-color-primary);'
            f'border-top:3px solid {color};flex:1;min-width:140px">'
            f'<div style="font-size:0.72em;color:var(--body-text-color-subdued);margin-bottom:4px">{title}</div>'
            f'<div style="font-size:1.5em;font-weight:700;color:{color};line-height:1.1">{value}</div>'
            f'<div style="font-size:0.75em;color:var(--body-text-color-subdued);margin-top:3px">{sub}</div>'
            f'{extra_html}'
            f'</div>'
        )

    budget_bar = (
        f'<div style="margin-top:8px;background:var(--border-color-primary);border-radius:4px;height:5px">'
        f'<div style="background:{bar_color};width:{pct_used:.1f}%;height:5px;border-radius:4px"></div>'
        f'</div>'
    )

    per_agent_lines = "".join(
        f'<div style="font-size:0.72em;color:var(--body-text-color-subdued)">'
        f'{n}: {u.get("today",{}).get("total_tokens",0):,} tok · ${u.get("today",{}).get("cost_usd",0.0):.4f}'
        f'</div>'
        for n, u in all_usage.items()
    )

    cost_cards = (
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">'
        + _metric_card("Today's Spend", f"${today_cost:.4f}", f"{pct_used:.1f}% of ${daily_lim:.2f} budget", bar_color, budget_bar)
        + _metric_card("Today's Tokens", f"{today_tok:,}", "all agents combined", "#6366f1")
        + _metric_card("This Hour Tokens", f"{hour_tok:,}", per_agent_lines, "#8b5cf6")
        + _metric_card("Budget Remaining", f"${remaining:.4f}", f"${daily_lim:.2f} daily limit", "#22c55e")
        + f'</div>'
    )

    complete  = today_t["complete"] or 1
    kpi_cards = (
        f'<div style="display:flex;gap:10px;flex-wrap:wrap">'
        + _metric_card("Tasks Today", str(today_t["total_tasks"]),
                       f"{today_t['complete']} complete · {today_t['failed']} failed", "#6366f1")
        + _metric_card("Auto-Resolved", f"{rates['auto_resolved_pct']:.0f}%",
                       f"{today_t['auto_resolved']} tasks", "#22c55e")
        + _metric_card("Validation Rate", f"{rates['validation_rate_pct']:.0f}%",
                       "chaos confirms eng correct", "#10b981")
        + _metric_card("Escalation Rate", f"{rates['escalation_rate_pct']:.0f}%",
                       f"{today_t['awaiting_approval']} awaiting approval", "#f97316")
        + f'</div>'
    )

    return (
        f'<div style="margin-top:4px">'
        f'<div style="font-size:0.8em;font-weight:600;color:var(--body-text-color-subdued);'
        f'margin-bottom:6px;text-transform:uppercase;letter-spacing:0.05em">💰 Cost Monitor</div>'
        f'{cost_cards}'
        f'<div style="font-size:0.8em;font-weight:600;color:var(--body-text-color-subdued);'
        f'margin-bottom:6px;text-transform:uppercase;letter-spacing:0.05em">📈 KPIs (Today)</div>'
        f'{kpi_cards}'
        f'</div>'
    )


def _refresh_task_queue(status_filter: str, type_filter: str):
    rows, tasks = _task_queue_rows(status_filter, type_filter)
    return rows, tasks


def _on_task_row_select(evt: gr.SelectData, tasks: list[dict]) -> tuple[str, str]:
    """Return (detail_markdown, task_id) — task_id auto-fills the approval input."""
    try:
        task = tasks[evt.index[0]]
        task_id = task["id"]
        return _task_detail_md(task_id), task_id
    except (IndexError, TypeError):
        return "*Select a task row to see details.*", ""


def _approve_task(task_id: str) -> str:
    if not task_id.strip():
        return "⚠️ Enter a task ID."
    task = task_store.get_task(task_id.strip())
    if not task:
        return f"❌ Task `{task_id}` not found."
    if task["status"] != "awaiting_approval":
        return f"⚠️ Task `{task_id}` is `{task['status']}`, not awaiting approval."
    task_store.approve_task(task_id.strip(), "human")
    return f"✅ Task `{task_id}` approved."


def _reject_task(task_id: str) -> str:
    if not task_id.strip():
        return "⚠️ Enter a task ID."
    task = task_store.get_task(task_id.strip())
    if not task:
        return f"❌ Task `{task_id}` not found."
    task_store.reject_task(task_id.strip(), "human", "Rejected via UI")
    return f"✅ Task `{task_id}` rejected."


def _clear_tasks_confirm(armed: bool) -> tuple[str, bool]:
    """First click arms the button; second click executes."""
    if not armed:
        return "⚠️ Click **Confirm Clear** to permanently delete all tasks.", True
    n = task_store.clear_all_tasks()
    # Reset the ops agent poller's deduplication state so it re-investigates
    # any alerts that are still firing after the queue is cleared.
    try:
        resp = httpx.post(f"{OPS_AGENT_URL}/poller/reset", timeout=5)
        seeded = resp.json().get("seeded_fingerprints", 0) if resp.status_code == 200 else "?"
        return f"🗑️ Cleared {n} task(s). Poller reset ({seeded} fingerprints re-seeded).", False
    except Exception:
        return f"🗑️ Cleared {n} task(s). (Poller reset failed — restart ops agent if tasks don't reappear.)", False


# ── Quick prompts ─────────────────────────────────────────────────────────────

OPS_EXAMPLES = [
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
]

ENG_EXAMPLES = [
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
]

CHAOS_EXAMPLES = [
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
]


# ── CSS ──────────────────────────────────────────────────────────────────────

CUSTOM_CSS = """
.chatbot { height: 460px !important; }

.prompt-list {
    max-height: 260px !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    border: 1px solid var(--border-color-primary) !important;
    border-radius: 8px !important;
    padding: 2px !important;
    background: var(--background-fill-primary) !important;
    box-shadow: none !important;
}

.prompt-btn button {
    font-size: 0.82em !important;
    padding: 6px 12px !important;
    border-radius: 5px !important;
    min-height: 30px !important;
    height: auto !important;
    text-align: left !important;
    line-height: 1.35 !important;
    white-space: normal !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    justify-content: flex-start !important;
    width: 100% !important;
}
.prompt-btn button:hover {
    background: var(--color-accent-soft) !important;
}

.detail-panel {
    border: 1px solid var(--border-color-primary);
    border-radius: 10px;
    padding: 6px 16px;
    background: var(--background-fill-secondary);
}

.task-detail-panel {
    border: 1px solid var(--border-color-primary);
    border-radius: 10px;
    padding: 8px 18px;
    background: var(--background-fill-secondary);
    max-height: 480px;
    overflow-y: auto;
}
"""


# ── UI ────────────────────────────────────────────────────────────────────────

def create_ui():
    with gr.Blocks(title="Network AI Agents", css=CUSTOM_CSS) as demo:

        gr.HTML("""
        <div style="padding:16px 0 10px 0">
            <h1 style="margin:0;font-size:1.45em;font-weight:700">🌐 Network Automation AI Agents</h1>
            <p style="margin:6px 0 0 0;font-size:0.88em;color:var(--body-text-color-subdued)">
                <strong>Ops</strong> — monitor &amp; investigate incidents &nbsp;·&nbsp;
                <strong>Engineering</strong> — design &amp; configure your network &nbsp;·&nbsp;
                <strong>Chaos</strong> — controlled experiments with safety gates
            </p>
        </div>
        """)

        status_bar = gr.HTML(
            value='<div style="padding:10px 0 14px 0;border-bottom:1px solid var(--border-color-primary);'
                  'margin-bottom:6px;color:var(--body-text-color-subdued);font-size:0.85em">'
                  'Checking agent status…</div>'
        )

        with gr.Tabs():

            # ── Ops Agent ──────────────────────────────────────────────────
            with gr.Tab("🚨 Ops Agent"):
                gr.Markdown("*Monitor Prometheus alerts, investigate root causes, and coordinate remediations.*")
                ops_session_id = gr.State(value="")
                ops_chatbot = gr.Chatbot(height=460, show_label=False,
                    avatar_images=(None, "https://img.icons8.com/color/96/robot.png"))
                with gr.Row():
                    ops_input = gr.Textbox(
                        placeholder="Ask about alerts, device health, log patterns...",
                        scale=9, show_label=False)
                    ops_send = gr.Button("Send ↵", scale=1, variant="primary")
                gr.Markdown("**Quick prompts** *(click to fill)*")
                with gr.Group(elem_classes=["prompt-list"]):
                    for ex in OPS_EXAMPLES:
                        gr.Button(ex, elem_classes=["prompt-btn"], size="sm").click(
                            lambda e=ex: e, outputs=ops_input)
                ops_clear = gr.Button("Clear Conversation", variant="secondary", size="sm")
                ops_send.click(ops_chat, [ops_input, ops_chatbot, ops_session_id],
                               [ops_input, ops_chatbot, ops_session_id])
                ops_input.submit(ops_chat, [ops_input, ops_chatbot, ops_session_id],
                                 [ops_input, ops_chatbot, ops_session_id])
                ops_clear.click(lambda: ([], ""), outputs=[ops_chatbot, ops_session_id])

            # ── Engineering Agent ──────────────────────────────────────────
            with gr.Tab("🔧 Engineering Agent"):
                gr.Markdown("*Design configurations, plan IP space, generate playbooks, and document your network.*")
                eng_session_id = gr.State(value="")
                eng_chatbot = gr.Chatbot(height=460, show_label=False,
                    avatar_images=(None, "https://img.icons8.com/color/96/robot.png"))
                with gr.Row():
                    eng_input = gr.Textbox(
                        placeholder="Ask about designs, configs, IP planning, playbooks...",
                        scale=9, show_label=False)
                    eng_send = gr.Button("Send ↵", scale=1, variant="primary")
                gr.Markdown("**Quick prompts** *(click to fill)*")
                with gr.Group(elem_classes=["prompt-list"]):
                    for ex in ENG_EXAMPLES:
                        gr.Button(ex, elem_classes=["prompt-btn"], size="sm").click(
                            lambda e=ex: e, outputs=eng_input)
                eng_clear = gr.Button("Clear Conversation", variant="secondary", size="sm")
                eng_send.click(eng_chat, [eng_input, eng_chatbot, eng_session_id],
                               [eng_input, eng_chatbot, eng_session_id])
                eng_input.submit(eng_chat, [eng_input, eng_chatbot, eng_session_id],
                                 [eng_input, eng_chatbot, eng_session_id])
                eng_clear.click(lambda: ([], ""), outputs=[eng_chatbot, eng_session_id])

            # ── Chaos Agent ────────────────────────────────────────────────
            with gr.Tab("🔥 Chaos Agent"):
                gr.Markdown("*Plan and run controlled chaos experiments with simulation-first safety checks.*")
                chaos_session_id = gr.State(value="")
                chaos_chatbot = gr.Chatbot(height=460, show_label=False,
                    avatar_images=(None, "https://img.icons8.com/color/96/robot.png"))
                with gr.Row():
                    chaos_input = gr.Textbox(
                        placeholder="Ask for a chaos experiment, blast radius estimate, or rollback plan...",
                        scale=9, show_label=False)
                    chaos_send = gr.Button("Send ↵", scale=1, variant="primary")
                gr.Markdown("**Quick prompts** *(click to fill)*")
                with gr.Group(elem_classes=["prompt-list"]):
                    for ex in CHAOS_EXAMPLES:
                        gr.Button(ex, elem_classes=["prompt-btn"], size="sm").click(
                            lambda e=ex: e, outputs=chaos_input)
                chaos_clear = gr.Button("Clear Conversation", variant="secondary", size="sm")
                chaos_send.click(chaos_chat, [chaos_input, chaos_chatbot, chaos_session_id],
                                 [chaos_input, chaos_chatbot, chaos_session_id])
                chaos_input.submit(chaos_chat, [chaos_input, chaos_chatbot, chaos_session_id],
                                   [chaos_input, chaos_chatbot, chaos_session_id])
                chaos_clear.click(lambda: ([], ""), outputs=[chaos_chatbot, chaos_session_id])

                gr.Markdown("---")
                with gr.Accordion("⏰ Schedule Chaos Run", open=False):
                    gr.Markdown(
                        "Schedule a chaos scenario to run automatically at a repeating interval. "
                        "Runs in **check mode** by default unless the scenario says otherwise."
                    )
                    with gr.Row():
                        sched_scenario = gr.Textbox(
                            label="Scenario prompt",
                            placeholder="e.g. Shut down Ethernet1 on leaf1 (check mode) and report blast radius",
                            scale=4)
                        sched_interval = gr.Slider(
                            label="Interval (minutes)", minimum=5, maximum=120, step=5, value=30, scale=1)
                    sched_btn    = gr.Button("Schedule", variant="primary")
                    sched_status = gr.Markdown()
                    gr.Markdown("**Active schedules:**")
                    sched_table = gr.Dataframe(
                        headers=["Job ID", "Interval (min)", "Scenario", "Next Run", "Last Run", "Last Status"],
                        datatype=["str", "number", "str", "str", "str", "str"],
                        value=_schedule_rows(), interactive=False, wrap=True, row_count=5)
                    with gr.Row():
                        cancel_id_input = gr.Textbox(label="Job ID to cancel", scale=3)
                        cancel_btn = gr.Button("Cancel Job", variant="stop", scale=1)
                    refresh_sched = gr.Button("🔄 Refresh Schedules", variant="secondary")

                    sched_btn.click(create_schedule, [sched_scenario, sched_interval],
                                    [sched_status, sched_table])
                    cancel_btn.click(cancel_schedule, [cancel_id_input],
                                     [sched_status, sched_table])
                    refresh_sched.click(lambda: _schedule_rows(), outputs=sched_table)

            # ── Pipeline Dashboard ─────────────────────────────────────────
            with gr.Tab("📊 Pipeline"):
                gr.HTML(
                    '<p style="font-size:0.85em;color:var(--body-text-color-subdued);margin:4px 0 12px 0">'
                    'Live agent states refresh every 2 s · Task queue refreshes every 3 s · '
                    'Cost &amp; KPIs refresh every 30 s</p>'
                )

                # ── Section 1: Live Agent Status ───────────────────────────
                gr.Markdown("### 🤖 Live Agent Status")
                live_status_html = gr.HTML(value=_live_agent_status_html())

                # ── Section 2: Task Queue ──────────────────────────────────
                gr.Markdown("### 📋 Task Queue")

                task_queue_tasks_state = gr.State([])

                with gr.Row():
                    task_status_filter = gr.Dropdown(
                        choices=["", "pending", "claimed", "running",
                                 "awaiting_approval", "complete", "failed", "rejected"],
                        value="", label="Status filter", scale=1)
                    task_type_filter = gr.Dropdown(
                        choices=["", "rca", "fix_proposal", "validation", "approval_gate"],
                        value="", label="Type filter", scale=1)
                    task_refresh_btn = gr.Button("🔄 Refresh", scale=1, variant="secondary")

                task_queue_table = gr.Dataframe(
                    headers=["ID", "Type", "Status", "Priority",
                             "Assigned To", "Created By", "Title", "Age"],
                    datatype=["str", "str", "str", "str", "str", "str", "str", "str"],
                    value=[],
                    interactive=False,
                    wrap=True,
                    row_count=10,
                )

                with gr.Row():
                    approval_task_id = gr.Textbox(
                        label="Task ID for approval action", scale=3,
                        placeholder="e.g. app-1a2b3c4d")
                    approve_btn = gr.Button("✅ Approve", variant="primary", scale=1)
                    reject_btn  = gr.Button("❌ Reject",  variant="stop",    scale=1)
                approval_status = gr.Markdown()

                gr.Markdown("---")
                with gr.Row():
                    clear_armed_state = gr.State(False)
                    clear_btn         = gr.Button("🗑️ Clear All Tasks", variant="secondary", scale=1)
                    confirm_clear_btn = gr.Button("⚠️ Confirm Clear", variant="stop", scale=1, visible=False)
                clear_status = gr.Markdown()

                # ── Section 3: Task Detail ─────────────────────────────────
                gr.Markdown("### 🔍 Task Detail")
                with gr.Group(elem_classes=["task-detail-panel"]):
                    task_detail_md = gr.Markdown(
                        value="*Click a task row above to see its full timeline and context.*"
                    )

                # ── Section 4: Cost Monitor + KPIs ─────────────────────────
                gr.Markdown("### 💰 Cost Monitor & KPIs")
                cost_kpi_html = gr.HTML(value=_cost_kpi_html())

                # ── Wiring ────────────────────────────────────────────────

                def _refresh_queue(sf, tf):
                    rows, tasks = _task_queue_rows(sf, tf)
                    return rows, tasks

                task_refresh_btn.click(
                    _refresh_queue,
                    inputs=[task_status_filter, task_type_filter],
                    outputs=[task_queue_table, task_queue_tasks_state],
                )
                task_status_filter.change(
                    _refresh_queue,
                    inputs=[task_status_filter, task_type_filter],
                    outputs=[task_queue_table, task_queue_tasks_state],
                )
                task_type_filter.change(
                    _refresh_queue,
                    inputs=[task_status_filter, task_type_filter],
                    outputs=[task_queue_table, task_queue_tasks_state],
                )

                task_queue_table.select(
                    _on_task_row_select,
                    inputs=[task_queue_tasks_state],
                    outputs=[task_detail_md, approval_task_id],
                )

                approve_btn.click(
                    _approve_task,
                    inputs=[approval_task_id],
                    outputs=[approval_status],
                )
                reject_btn.click(
                    _reject_task,
                    inputs=[approval_task_id],
                    outputs=[approval_status],
                )

                def _on_clear(armed):
                    msg, new_armed = _clear_tasks_confirm(armed)
                    # Show confirm button only when armed (waiting for confirmation)
                    return msg, new_armed, gr.update(visible=new_armed)

                def _on_confirm_clear(armed):
                    msg, new_armed = _clear_tasks_confirm(armed)
                    return msg, new_armed, gr.update(visible=new_armed), [], []

                clear_btn.click(
                    _on_clear,
                    inputs=[clear_armed_state],
                    outputs=[clear_status, clear_armed_state, confirm_clear_btn],
                )
                confirm_clear_btn.click(
                    _on_confirm_clear,
                    inputs=[clear_armed_state],
                    outputs=[clear_status, clear_armed_state, confirm_clear_btn,
                             task_queue_table, task_queue_tasks_state],
                )

                # Auto-refresh timers
                gr.Timer(2).tick(_live_agent_status_html, outputs=live_status_html)
                gr.Timer(3).tick(
                    lambda sf, tf: _refresh_queue(sf, tf),
                    inputs=[task_status_filter, task_type_filter],
                    outputs=[task_queue_table, task_queue_tasks_state],
                )
                gr.Timer(30).tick(_cost_kpi_html, outputs=cost_kpi_html)

            # ── Agent Activity ─────────────────────────────────────────────
            with gr.Tab("🕒 Agent Activity"):
                full_records_state = gr.State([])

                with gr.Row():
                    agent_filter = gr.Dropdown(
                        choices=["All", "Ops", "Engineering", "Chaos"],
                        value="All", label="Filter by agent", scale=1)
                    refresh_activity = gr.Button("🔄 Refresh", variant="secondary", scale=1)
                    gr.HTML(
                        "<span style='font-style:italic;color:var(--body-text-color-subdued)'>"
                        "Auto-refreshes every 5 s. Click any row to see the full interaction below.</span>"
                    )

                activity_summary = gr.HTML(value=_activity_summary_html())

                activity_table = gr.Dataframe(
                    headers=["Timestamp", "Agent", "Status", "Latency",
                             "Session", "Message", "Response"],
                    datatype=["str", "str", "str", "str", "str", "str", "str"],
                    value=_activity_data()[0],
                    interactive=False, wrap=True,
                    row_count=10, column_count=(7, "fixed"),
                )

                with gr.Group(elem_classes=["detail-panel"]):
                    gr.Markdown("### Interaction Detail")
                    detail_panel = gr.Markdown(
                        value="*Click a row above to see the full message, response, and tools used.*")

                def _refresh(f):
                    rows, records, html = get_activity_view(f)
                    return rows, records, html

                activity_table.select(
                    show_activity_detail,
                    inputs=[full_records_state],
                    outputs=[detail_panel],
                )
                refresh_activity.click(
                    _refresh, inputs=[agent_filter],
                    outputs=[activity_table, full_records_state, activity_summary])
                agent_filter.change(
                    _refresh, inputs=[agent_filter],
                    outputs=[activity_table, full_records_state, activity_summary])
                gr.Timer(5).tick(
                    _refresh, inputs=[agent_filter],
                    outputs=[activity_table, full_records_state, activity_summary])

        # ── Global load + timers ───────────────────────────────────────────
        demo.load(_status_bar_html, outputs=status_bar)
        demo.load(
            lambda: get_activity_view("All"),
            outputs=[activity_table, full_records_state, activity_summary],
        )
        demo.load(
            lambda: _refresh_queue("", ""),
            outputs=[task_queue_table, task_queue_tasks_state],
        )
        gr.Timer(30).tick(_status_bar_html, outputs=status_bar)

    return demo


if __name__ == "__main__":
    ui = create_ui()
    ui.launch(
        server_name="0.0.0.0",
        server_port=7860,
        show_error=True,
        share=False,
        theme=gr.themes.Soft(primary_hue="blue"),
    )
