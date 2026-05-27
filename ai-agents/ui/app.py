"""
Network AI Agents – Gradio Web UI

Provides chat interfaces for the Ops, Engineering, and Chaos agents together
with a persistent activity log and a scheduling panel for chaos experiments.
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


def _agent_name(agent_url: str) -> str:
    for name, url in AGENT_LABELS.items():
        if url == agent_url:
            return name
    return "Unknown"


def _truncate(text: str, max_len: int = 140) -> str:
    text = text or ""
    return text if len(text) <= max_len else f"{text[: max_len - 3]}..."


def _chat_with_agent(
    agent_url: str, message: str, history: list, session_id: str
) -> tuple[str, list, str]:
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
        response = f"⚠️ Error: {str(e)}"

    latency_ms = int((time.time() - start) * 1000)
    store.record(
        agent=agent,
        session_id=session_id,
        message=message,
        response=response,
        status=status,
        latency_ms=latency_ms,
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


# ── Activity helpers ───────────────────────────────────────────────────────────

def _activity_table_rows(agent_filter: str = "All") -> list[list]:
    f = None if agent_filter == "All" else agent_filter
    rows = store.get_recent(limit=150, agent_filter=f)
    return [
        [
            r["timestamp"], r["agent"], r["status"],
            r["latency_ms"], r["session_id"],
            _truncate(r["message"]), _truncate(r["response"]),
        ]
        for r in rows
    ]


def _activity_summary() -> str:
    s = store.summary()
    if s["total"] == 0:
        return "No activity recorded yet."
    by_agent = " | ".join(f"{k}: **{v}**" for k, v in s["by_agent"].items())
    return (
        f"Total: **{s['total']}** | "
        f"Success: **{s['success']}** | "
        f"Failed: **{s['failed']}** | {by_agent}"
    )


def get_activity_view(agent_filter: str = "All") -> tuple[list[list], str]:
    return _activity_table_rows(agent_filter), _activity_summary()


def get_tool_calls_for_session(evt: gr.SelectData, table_data) -> str:
    """Return a markdown table of tool calls for the selected session row."""
    try:
        session_id = table_data[evt.index[0]][4]
        calls = store.get_tool_calls(session_id)
        if not calls:
            return f"*No tool calls recorded for session `{session_id}`.*"
        lines = ["| Tool | Input | Output |", "|------|-------|--------|"]
        for c in calls:
            lines.append(
                f"| `{c['tool_name']}` "
                f"| {_truncate(c.get('input_summary') or '', 80)} "
                f"| {_truncate(c.get('output_summary') or '', 120)} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"*Could not load tool calls: {e}*"


# ── Schedule helpers ───────────────────────────────────────────────────────────

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


# ── Example prompts ────────────────────────────────────────────────────────────

OPS_EXAMPLES = [
    "What alerts are currently firing?",
    "Why is spine1 showing high CPU?",
    "Show me interface errors on leaf1 in the last hour",
    "Investigate BGP peer down alert on spine2",
    "Generate a network health report for all lab devices",
    "What changed on the network in the last 30 minutes?",
]

ENG_EXAMPLES = [
    "Design BGP configuration for a new leaf router with AS 65104",
    "Show me all devices with their IP addresses",
    "What IP addresses are available in 10.10.0.0/16?",
    "Generate an Ansible playbook to configure VLANs 100-110 on all leaf switches",
    "Create documentation for the spine-leaf OSPF design",
    "Review this EOS config snippet for security issues",
]

CHAOS_EXAMPLES = [
    "Propose a safe chaos test for BGP flap detection in this lab",
    "Simulate a leaf uplink failure on leaf1 in check mode",
    "What is the expected blast radius if I bounce Ethernet1 on spine1?",
    "Create a rollback-first chaos runbook for testing alert correlation",
    "Design a 15-minute game day with one controlled failure and validation steps",
    "Shut down Ethernet1 on leaf2 in check mode and show me what alerts would fire",
]


# ── UI ─────────────────────────────────────────────────────────────────────────

def create_ui():
    with gr.Blocks(title="Network AI Agents") as demo:
        gr.Markdown(
            """
            # 🌐 Network Automation AI Agents
            **Ops Agent** – Monitor, investigate, and remediate network incidents
            **Engineering Agent** – Design, configure, and document your network
            **Chaos Agent** – Plan and run controlled lab chaos experiments with safety gates
            """
        )

        with gr.Tabs():

            # ── Ops Agent ────────────────────────────────────────────────────
            with gr.Tab("🚨 Ops Agent"):
                gr.Markdown("Monitor Prometheus alerts, investigate root causes, and execute remediations.")
                ops_session_id = gr.State(value="")
                ops_chatbot = gr.Chatbot(label="Network Ops Agent", elem_classes=["chatbot"],
                                         avatar_images=(None, "https://img.icons8.com/color/96/robot.png"))
                with gr.Row():
                    ops_input = gr.Textbox(placeholder="Ask about alerts, device health, log patterns...",
                                           scale=9, show_label=False)
                    ops_send = gr.Button("Send", scale=1, variant="primary")
                gr.Markdown("**Example prompts:**")
                for ex in OPS_EXAMPLES:
                    gr.Button(ex, elem_classes=["example-btn"]).click(lambda e=ex: e, outputs=ops_input)
                ops_clear = gr.Button("🗑️ Clear Conversation", variant="stop")
                ops_send.click(ops_chat, [ops_input, ops_chatbot, ops_session_id],
                               [ops_input, ops_chatbot, ops_session_id])
                ops_input.submit(ops_chat, [ops_input, ops_chatbot, ops_session_id],
                                 [ops_input, ops_chatbot, ops_session_id])
                ops_clear.click(lambda: ([], ""), outputs=[ops_chatbot, ops_session_id])

            # ── Engineering Agent ────────────────────────────────────────────
            with gr.Tab("🔧 Engineering Agent"):
                gr.Markdown("Design configurations, plan IP space, generate playbooks, and document your network.")
                eng_session_id = gr.State(value="")
                eng_chatbot = gr.Chatbot(label="Network Engineering Agent", elem_classes=["chatbot"],
                                         avatar_images=(None, "https://img.icons8.com/color/96/robot.png"))
                with gr.Row():
                    eng_input = gr.Textbox(placeholder="Ask about designs, configs, IP planning, playbooks...",
                                           scale=9, show_label=False)
                    eng_send = gr.Button("Send", scale=1, variant="primary")
                gr.Markdown("**Example prompts:**")
                for ex in ENG_EXAMPLES:
                    gr.Button(ex, elem_classes=["example-btn"]).click(lambda e=ex: e, outputs=eng_input)
                eng_clear = gr.Button("🗑️ Clear Conversation", variant="stop")
                eng_send.click(eng_chat, [eng_input, eng_chatbot, eng_session_id],
                               [eng_input, eng_chatbot, eng_session_id])
                eng_input.submit(eng_chat, [eng_input, eng_chatbot, eng_session_id],
                                 [eng_input, eng_chatbot, eng_session_id])
                eng_clear.click(lambda: ([], ""), outputs=[eng_chatbot, eng_session_id])

            # ── Chaos Agent ──────────────────────────────────────────────────
            with gr.Tab("🔥 Chaos Agent"):
                gr.Markdown("Plan and run controlled chaos experiments with simulation-first safety checks.")
                chaos_session_id = gr.State(value="")
                chaos_chatbot = gr.Chatbot(label="Network Chaos Agent", elem_classes=["chatbot"],
                                           avatar_images=(None, "https://img.icons8.com/color/96/robot.png"))
                with gr.Row():
                    chaos_input = gr.Textbox(
                        placeholder="Ask for a safe chaos experiment, blast radius estimate, or rollback plan...",
                        scale=9, show_label=False,
                    )
                    chaos_send = gr.Button("Send", scale=1, variant="primary")
                gr.Markdown("**Example prompts:**")
                for ex in CHAOS_EXAMPLES:
                    gr.Button(ex, elem_classes=["example-btn"]).click(lambda e=ex: e, outputs=chaos_input)
                chaos_clear = gr.Button("🗑️ Clear Conversation", variant="stop")
                chaos_send.click(chaos_chat, [chaos_input, chaos_chatbot, chaos_session_id],
                                 [chaos_input, chaos_chatbot, chaos_session_id])
                chaos_input.submit(chaos_chat, [chaos_input, chaos_chatbot, chaos_session_id],
                                   [chaos_input, chaos_chatbot, chaos_session_id])
                chaos_clear.click(lambda: ([], ""), outputs=[chaos_chatbot, chaos_session_id])

                gr.Markdown("---")
                with gr.Accordion("⏰ Schedule Chaos Run", open=False):
                    gr.Markdown(
                        "Schedule a chaos scenario to run automatically on a repeating interval. "
                        "The chaos agent will execute it in **check mode** unless the scenario explicitly says otherwise."
                    )
                    with gr.Row():
                        sched_scenario = gr.Textbox(
                            label="Scenario prompt",
                            placeholder="e.g. Shut down Ethernet1 on leaf1 (check mode) and report blast radius",
                            scale=4,
                        )
                        sched_interval = gr.Slider(
                            label="Interval (minutes)", minimum=5, maximum=120, step=5, value=30, scale=1
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
                        cancel_id_input = gr.Textbox(label="Job ID to cancel", scale=3, show_label=True)
                        cancel_btn = gr.Button("Cancel Job", variant="stop", scale=1)

                    refresh_sched = gr.Button("🔄 Refresh Schedules", variant="secondary")

                    sched_btn.click(
                        create_schedule,
                        inputs=[sched_scenario, sched_interval],
                        outputs=[sched_status, sched_table],
                    )
                    cancel_btn.click(
                        cancel_schedule,
                        inputs=[cancel_id_input],
                        outputs=[sched_status, sched_table],
                    )
                    refresh_sched.click(lambda: _schedule_rows(), outputs=sched_table)

            # ── Agent Activity ───────────────────────────────────────────────
            with gr.Tab("🕒 Agent Activity"):
                gr.Markdown(
                    "Live and historical agent activity across all agents. "
                    "Click a row to inspect the tool calls made during that interaction."
                )

                with gr.Row():
                    agent_filter = gr.Dropdown(
                        choices=["All", "Ops", "Engineering", "Chaos"],
                        value="All",
                        label="Filter by agent",
                        scale=1,
                    )
                    refresh_activity = gr.Button("🔄 Refresh", variant="secondary", scale=1)

                activity_summary = gr.Markdown(value=_activity_summary())
                activity_table = gr.Dataframe(
                    headers=["Timestamp", "Agent", "Status", "Latency(ms)",
                             "Session ID", "Message", "Response"],
                    datatype=["str", "str", "str", "number", "str", "str", "str"],
                    value=_activity_table_rows(),
                    interactive=False,
                    wrap=True,
                    row_count=12,
                    column_count=(7, "fixed"),
                )

                gr.Markdown("**Tool calls for selected interaction:**")
                tool_calls_panel = gr.Markdown(
                    value="*Click a row above to see the tools the agent invoked.*"
                )

                activity_table.select(
                    get_tool_calls_for_session,
                    inputs=[activity_table],
                    outputs=[tool_calls_panel],
                )

                def refresh_activity_view(f):
                    return get_activity_view(f)

                refresh_activity.click(
                    refresh_activity_view,
                    inputs=[agent_filter],
                    outputs=[activity_table, activity_summary],
                )
                agent_filter.change(
                    refresh_activity_view,
                    inputs=[agent_filter],
                    outputs=[activity_table, activity_summary],
                )
                demo.load(lambda: get_activity_view("All"), outputs=[activity_table, activity_summary])
                gr.Timer(5).tick(lambda: get_activity_view("All"), outputs=[activity_table, activity_summary])

            # ── Service Status ───────────────────────────────────────────────
            with gr.Tab("📊 Service Status"):
                gr.Markdown("## Service Health")

                def check_services():
                    services = {
                        "Ops Agent": f"{OPS_AGENT_URL}/health",
                        "Engineering Agent": f"{ENG_AGENT_URL}/health",
                        "Chaos Agent": f"{CHAOS_AGENT_URL}/health",
                    }
                    lines = []
                    for name, url in services.items():
                        try:
                            r = httpx.get(url, timeout=5)
                            if r.status_code == 200:
                                lines.append(f"✅ **{name}**: Online")
                            else:
                                lines.append(f"⚠️ **{name}**: HTTP {r.status_code}")
                        except Exception:
                            lines.append(f"❌ **{name}**: Unreachable")
                    return "\n".join(lines)

                status_output = gr.Markdown()
                refresh_btn = gr.Button("🔄 Refresh Status", variant="secondary")
                refresh_btn.click(check_services, outputs=status_output)
                demo.load(check_services, outputs=status_output)

    return demo


if __name__ == "__main__":
    ui = create_ui()
    ui.launch(
        server_name="0.0.0.0",
        server_port=7860,
        show_error=True,
        share=False,
        theme=gr.themes.Soft(primary_hue="blue"),
        css=".chatbot { height: 520px !important; } .example-btn { font-size: 0.85em; }",
    )
