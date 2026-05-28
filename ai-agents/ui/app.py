"""
Network AI Agents – Gradio Web UI
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import gradio as gr
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.activity_store import ActivityStore

OPS_AGENT_URL = os.getenv("OPS_AGENT_URL", "http://ai-ops-agent:8000")
ENG_AGENT_URL = os.getenv("ENG_AGENT_URL", "http://ai-eng-agent:8001")
CHAOS_AGENT_URL = os.getenv("CHAOS_AGENT_URL", "http://ai-chaos-agent:8002")

store = ActivityStore()

AGENT_LABELS = {"Ops": OPS_AGENT_URL, "Engineering": ENG_AGENT_URL, "Chaos": CHAOS_AGENT_URL}
AGENT_COLORS = {"Ops": "#3b82f6", "Engineering": "#10b981", "Chaos": "#f97316"}


def _agent_name(agent_url: str) -> str:
    for name, url in AGENT_LABELS.items():
        if url == agent_url:
            return name
    return "Unknown"


def _truncate(text: str, max_len: int = 140) -> str:
    text = text or ""
    return text if len(text) <= max_len else f"{text[:max_len - 3]}..."


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
            if r.status_code == 200:
                label, color = "Online", "#22c55e"
            else:
                label, color = f"HTTP {r.status_code}", "#f59e0b"
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
    agent = record["agent"]
    status = record["status"]
    latency_ms = record["latency_ms"]
    ts = record["timestamp"]
    message = record["message"]
    response = record["response"]

    calls = store.get_tool_calls(session_id)
    status_icon = "✅" if status == "success" else "❌"
    agent_icons = {"Ops": "🚨", "Engineering": "🔧", "Chaos": "🔥"}
    agent_icon = agent_icons.get(agent, "🤖")

    lines = [
        f"## {agent_icon} {agent} Agent &nbsp; {status_icon} {status} &nbsp; · &nbsp; {latency_ms:,} ms",
        f"*{ts}*",
        "",
        "---",
        "",
        "### 💬 User Message",
        "",
        message,
        "",
        "---",
        "",
        "### 🤖 Agent Response",
        "",
        response,
    ]

    if calls:
        lines += [
            "",
            "---",
            "",
            f"### 🔧 Tools Used ({len(calls)} call{'s' if len(calls) != 1 else ''})",
        ]
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
            [
                j["job_id"],
                j["interval_minutes"],
                _truncate(j["scenario"], 80),
                j.get("next_run", "—"),
                j.get("last_run") or "—",
                j.get("last_status") or "—",
            ]
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
/* Chatbot */
.chatbot { height: 460px !important; }

/* Scrollable prompt list container */
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

/* Prompt list buttons — full-width list-item style */
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

/* Activity detail panel */
.detail-panel {
    border: 1px solid var(--border-color-primary);
    border-radius: 10px;
    padding: 6px 16px;
    background: var(--background-fill-secondary);
}
"""


# ── UI ────────────────────────────────────────────────────────────────────────

def create_ui():
    with gr.Blocks(title="Network AI Agents") as demo:

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

        # Always-visible agent status bar
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
                ops_chatbot = gr.Chatbot(
                    height=460, show_label=False,
                    avatar_images=(None, "https://img.icons8.com/color/96/robot.png"),
                )
                with gr.Row():
                    ops_input = gr.Textbox(
                        placeholder="Ask about alerts, device health, log patterns...",
                        scale=9, show_label=False,
                    )
                    ops_send = gr.Button("Send ↵", scale=1, variant="primary")
                gr.Markdown("**Quick prompts** *(click to fill)*")
                with gr.Group(elem_classes=["prompt-list"]):
                    for ex in OPS_EXAMPLES:
                        gr.Button(ex, elem_classes=["prompt-btn"], size="sm").click(
                            lambda e=ex: e, outputs=ops_input
                        )
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
                eng_chatbot = gr.Chatbot(
                    height=460, show_label=False,
                    avatar_images=(None, "https://img.icons8.com/color/96/robot.png"),
                )
                with gr.Row():
                    eng_input = gr.Textbox(
                        placeholder="Ask about designs, configs, IP planning, playbooks...",
                        scale=9, show_label=False,
                    )
                    eng_send = gr.Button("Send ↵", scale=1, variant="primary")
                gr.Markdown("**Quick prompts** *(click to fill)*")
                with gr.Group(elem_classes=["prompt-list"]):
                    for ex in ENG_EXAMPLES:
                        gr.Button(ex, elem_classes=["prompt-btn"], size="sm").click(
                            lambda e=ex: e, outputs=eng_input
                        )
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
                chaos_chatbot = gr.Chatbot(
                    height=460, show_label=False,
                    avatar_images=(None, "https://img.icons8.com/color/96/robot.png"),
                )
                with gr.Row():
                    chaos_input = gr.Textbox(
                        placeholder="Ask for a chaos experiment, blast radius estimate, or rollback plan...",
                        scale=9, show_label=False,
                    )
                    chaos_send = gr.Button("Send ↵", scale=1, variant="primary")
                gr.Markdown("**Quick prompts** *(click to fill)*")
                with gr.Group(elem_classes=["prompt-list"]):
                    for ex in CHAOS_EXAMPLES:
                        gr.Button(ex, elem_classes=["prompt-btn"], size="sm").click(
                            lambda e=ex: e, outputs=chaos_input
                        )
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
                            scale=4,
                        )
                        sched_interval = gr.Slider(
                            label="Interval (minutes)", minimum=5, maximum=120, step=5, value=30, scale=1,
                        )
                    sched_btn = gr.Button("Schedule", variant="primary")
                    sched_status = gr.Markdown()

                    gr.Markdown("**Active schedules:**")
                    sched_table = gr.Dataframe(
                        headers=["Job ID", "Interval (min)", "Scenario", "Next Run", "Last Run", "Last Status"],
                        datatype=["str", "number", "str", "str", "str", "str"],
                        value=_schedule_rows(),
                        interactive=False,
                        wrap=True,
                        row_count=5,
                    )
                    with gr.Row():
                        cancel_id_input = gr.Textbox(label="Job ID to cancel", scale=3)
                        cancel_btn = gr.Button("Cancel Job", variant="stop", scale=1)
                    refresh_sched = gr.Button("🔄 Refresh Schedules", variant="secondary")

                    sched_btn.click(create_schedule, [sched_scenario, sched_interval],
                                    [sched_status, sched_table])
                    cancel_btn.click(cancel_schedule, [cancel_id_input],
                                     [sched_status, sched_table])
                    refresh_sched.click(lambda: _schedule_rows(), outputs=sched_table)

            # ── Agent Activity ─────────────────────────────────────────────
            with gr.Tab("🕒 Agent Activity"):
                full_records_state = gr.State([])

                with gr.Row():
                    agent_filter = gr.Dropdown(
                        choices=["All", "Ops", "Engineering", "Chaos"],
                        value="All", label="Filter by agent", scale=1,
                    )
                    refresh_activity = gr.Button("🔄 Refresh", variant="secondary", scale=1)
                    gr.HTML(
                        "<span style='font-style:italic;color:var(--body-text-color-subdued)'>"
                        "Auto-refreshes every 5 s. Click any row to see the full interaction below.</span>"
                    )

                activity_summary = gr.HTML(value=_activity_summary_html())

                activity_table = gr.Dataframe(
                    headers=["Timestamp", "Agent", "Status", "Latency", "Session", "Message", "Response"],
                    datatype=["str", "str", "str", "str", "str", "str", "str"],
                    value=_activity_data()[0],
                    interactive=False,
                    wrap=True,
                    row_count=10,
                    column_count=(7, "fixed"),
                )

                with gr.Group(elem_classes=["detail-panel"]):
                    gr.Markdown("### Interaction Detail")
                    detail_panel = gr.Markdown(
                        value="*Click a row above to see the full message, response, and tools used.*",
                    )

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
                    outputs=[activity_table, full_records_state, activity_summary],
                )
                agent_filter.change(
                    _refresh, inputs=[agent_filter],
                    outputs=[activity_table, full_records_state, activity_summary],
                )
                gr.Timer(5).tick(
                    _refresh, inputs=[agent_filter],
                    outputs=[activity_table, full_records_state, activity_summary],
                )

        # ── Global load + timers ───────────────────────────────────────────
        demo.load(_status_bar_html, outputs=status_bar)
        demo.load(
            lambda: get_activity_view("All"),
            outputs=[activity_table, full_records_state, activity_summary],
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
        css=CUSTOM_CSS,
    )
