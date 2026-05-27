"""
Network AI Agents – Gradio Web UI

Provides a chat interface for both the Ops and Engineering agents.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import time
import uuid

import gradio as gr
import httpx

OPS_AGENT_URL = os.getenv("OPS_AGENT_URL", "http://ai-ops-agent:8000")
ENG_AGENT_URL = os.getenv("ENG_AGENT_URL", "http://ai-eng-agent:8001")
CHAOS_AGENT_URL = os.getenv("CHAOS_AGENT_URL", "http://ai-chaos-agent:8002")
ACTIVITY_LOG_MAX = 2000
ACTIVITY_LOG: list[dict] = []


def _agent_name(agent_url: str) -> str:
    if agent_url == OPS_AGENT_URL:
        return "Ops"
    if agent_url == ENG_AGENT_URL:
        return "Engineering"
    if agent_url == CHAOS_AGENT_URL:
        return "Chaos"
    return "Unknown"


def _truncate(text: str, max_len: int = 140) -> str:
    text = text or ""
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def _record_activity(
    *,
    agent: str,
    session_id: str,
    message: str,
    response: str,
    status: str,
    latency_ms: int,
) -> None:
    event = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "agent": agent,
        "session_id": session_id,
        "status": status,
        "latency_ms": latency_ms,
        "message": _truncate(message),
        "response": _truncate(response),
    }
    ACTIVITY_LOG.append(event)
    if len(ACTIVITY_LOG) > ACTIVITY_LOG_MAX:
        del ACTIVITY_LOG[:-ACTIVITY_LOG_MAX]


def _activity_table_rows(limit: int = 150) -> list[list]:
    rows = []
    for event in ACTIVITY_LOG[-limit:]:
        rows.append(
            [
                event["timestamp"],
                event["agent"],
                event["status"],
                event["latency_ms"],
                event["session_id"],
                event["message"],
                event["response"],
            ]
        )
    return list(reversed(rows))


def _activity_summary() -> str:
    total = len(ACTIVITY_LOG)
    if total == 0:
        return "No activity recorded yet."

    success = sum(1 for e in ACTIVITY_LOG if e["status"] == "success")
    failures = total - success
    return (
        f"Total events: **{total}** | "
        f"Success: **{success}** | "
        f"Failures: **{failures}**"
    )


def get_activity_view() -> tuple[list[list], str]:
    return _activity_table_rows(), _activity_summary()


def _chat_with_agent(agent_url: str, message: str, history: list, session_id: str) -> tuple[str, list, str]:
    """Send a message to an agent and return the response."""
    if not session_id:
        session_id = str(uuid.uuid4())
    agent = _agent_name(agent_url)
    start = time.time()
    status = "success"
    try:
        resp = httpx.post(
            f"{agent_url}/chat",
            json={"message": message, "session_id": session_id},
            timeout=120,
        )
        resp.raise_for_status()
        response = resp.json()["response"]
    except httpx.ConnectError:
        status = "failed"
        response = "⚠️ Agent service is not available. Please check that the service is running."
    except Exception as e:
        status = "failed"
        response = f"⚠️ Error: {str(e)}"

    latency_ms = int((time.time() - start) * 1000)
    _record_activity(
        agent=agent,
        session_id=session_id,
        message=message,
        response=response,
        status=status,
        latency_ms=latency_ms,
    )

    history = history or []
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": response})
    return "", history, session_id


def ops_chat(message: str, history: list, session_id: str) -> tuple[str, list, str]:
    """Handle Ops agent chat."""
    return _chat_with_agent(OPS_AGENT_URL, message, history, session_id)


def eng_chat(message: str, history: list, session_id: str) -> tuple[str, list, str]:
    """Handle Engineering agent chat."""
    return _chat_with_agent(ENG_AGENT_URL, message, history, session_id)


def chaos_chat(message: str, history: list, session_id: str) -> tuple[str, list, str]:
    """Handle Chaos agent chat."""
    return _chat_with_agent(CHAOS_AGENT_URL, message, history, session_id)


# ── UI Layout ──────────────────────────────────────────────────────────────────

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
]


def create_ui():
    with gr.Blocks(
        title="Network AI Agents",
    ) as demo:
        gr.Markdown(
            """
            # 🌐 Network Automation AI Agents
            **Ops Agent** – Monitor, investigate, and remediate network incidents  
            **Engineering Agent** – Design, configure, and document your network  
            **Chaos Agent** – Plan and run controlled lab chaos experiments with safety gates
            """
        )

        with gr.Tabs():
            # ── Ops Agent Tab ────────────────────────────────────────────────
            with gr.Tab("🚨 Ops Agent"):
                gr.Markdown("Monitor Prometheus alerts, investigate root causes, and execute remediations.")
                ops_session_id = gr.State(value="")

                ops_chatbot = gr.Chatbot(
                    label="Network Ops Agent",
                    elem_classes=["chatbot"],
                    avatar_images=(None, "https://img.icons8.com/color/96/robot.png"),
                )
                with gr.Row():
                    ops_input = gr.Textbox(
                        placeholder="Ask about alerts, device health, log patterns...",
                        scale=9,
                        show_label=False,
                    )
                    ops_send = gr.Button("Send", scale=1, variant="primary")

                gr.Markdown("**Example prompts:**")
                for example in OPS_EXAMPLES:
                    gr.Button(example, elem_classes=["example-btn"]).click(
                        lambda e=example: e,
                        outputs=ops_input,
                    )

                ops_clear = gr.Button("🗑️ Clear Conversation", variant="stop")

                ops_send.click(
                    ops_chat,
                    inputs=[ops_input, ops_chatbot, ops_session_id],
                    outputs=[ops_input, ops_chatbot, ops_session_id],
                )
                ops_input.submit(
                    ops_chat,
                    inputs=[ops_input, ops_chatbot, ops_session_id],
                    outputs=[ops_input, ops_chatbot, ops_session_id],
                )
                ops_clear.click(
                    lambda: ([], ""),
                    outputs=[ops_chatbot, ops_session_id],
                )

            # ── Engineering Agent Tab ────────────────────────────────────────
            with gr.Tab("🔧 Engineering Agent"):
                gr.Markdown("Design configurations, plan IP space, generate playbooks, and document your network.")
                eng_session_id = gr.State(value="")

                eng_chatbot = gr.Chatbot(
                    label="Network Engineering Agent",
                    elem_classes=["chatbot"],
                    avatar_images=(None, "https://img.icons8.com/color/96/robot.png"),
                )
                with gr.Row():
                    eng_input = gr.Textbox(
                        placeholder="Ask about designs, configs, IP planning, playbooks...",
                        scale=9,
                        show_label=False,
                    )
                    eng_send = gr.Button("Send", scale=1, variant="primary")

                gr.Markdown("**Example prompts:**")
                for example in ENG_EXAMPLES:
                    gr.Button(example, elem_classes=["example-btn"]).click(
                        lambda e=example: e,
                        outputs=eng_input,
                    )

                eng_clear = gr.Button("🗑️ Clear Conversation", variant="stop")

                eng_send.click(
                    eng_chat,
                    inputs=[eng_input, eng_chatbot, eng_session_id],
                    outputs=[eng_input, eng_chatbot, eng_session_id],
                )
                eng_input.submit(
                    eng_chat,
                    inputs=[eng_input, eng_chatbot, eng_session_id],
                    outputs=[eng_input, eng_chatbot, eng_session_id],
                )
                eng_clear.click(
                    lambda: ([], ""),
                    outputs=[eng_chatbot, eng_session_id],
                )

            # ── Chaos Agent Tab ──────────────────────────────────────────────
            with gr.Tab("🔥 Chaos Agent"):
                gr.Markdown("Plan and run controlled chaos experiments with simulation-first safety checks.")
                chaos_session_id = gr.State(value="")

                chaos_chatbot = gr.Chatbot(
                    label="Network Chaos Agent",
                    elem_classes=["chatbot"],
                    avatar_images=(None, "https://img.icons8.com/color/96/robot.png"),
                )
                with gr.Row():
                    chaos_input = gr.Textbox(
                        placeholder="Ask for a safe chaos experiment, blast radius estimate, or rollback plan...",
                        scale=9,
                        show_label=False,
                    )
                    chaos_send = gr.Button("Send", scale=1, variant="primary")

                gr.Markdown("**Example prompts:**")
                for example in CHAOS_EXAMPLES:
                    gr.Button(example, elem_classes=["example-btn"]).click(
                        lambda e=example: e,
                        outputs=chaos_input,
                    )

                chaos_clear = gr.Button("🗑️ Clear Conversation", variant="stop")

                chaos_send.click(
                    chaos_chat,
                    inputs=[chaos_input, chaos_chatbot, chaos_session_id],
                    outputs=[chaos_input, chaos_chatbot, chaos_session_id],
                )
                chaos_input.submit(
                    chaos_chat,
                    inputs=[chaos_input, chaos_chatbot, chaos_session_id],
                    outputs=[chaos_input, chaos_chatbot, chaos_session_id],
                )
                chaos_clear.click(
                    lambda: ([], ""),
                    outputs=[chaos_chatbot, chaos_session_id],
                )

            # ── Agent Activity Tab ───────────────────────────────────────────
            with gr.Tab("🕒 Agent Activity"):
                gr.Markdown("Live and recent agent activity across Ops, Engineering, and Chaos agents.")

                activity_summary = gr.Markdown(value="No activity recorded yet.")
                activity_table = gr.Dataframe(
                    headers=[
                        "Timestamp",
                        "Agent",
                        "Status",
                        "Latency(ms)",
                        "Session ID",
                        "Message",
                        "Response",
                    ],
                    datatype=["str", "str", "str", "number", "str", "str", "str"],
                    value=get_activity_view()[0],
                    interactive=False,
                    wrap=True,
                    row_count=12,
                    col_count=(7, "fixed"),
                )

                refresh_activity = gr.Button("🔄 Refresh Activity", variant="secondary")
                refresh_activity.click(get_activity_view, outputs=[activity_table, activity_summary])
                demo.load(get_activity_view, outputs=[activity_table, activity_summary], every=5)

            # ── Status Tab ───────────────────────────────────────────────────
            with gr.Tab("📊 Service Status"):
                gr.Markdown("## Service Health")

                def check_services():
                    services = {
                        "Ops Agent": f"{OPS_AGENT_URL}/health",
                        "Engineering Agent": f"{ENG_AGENT_URL}/health",
                        "Chaos Agent": f"{CHAOS_AGENT_URL}/health",
                    }
                    status_lines = []
                    for name, url in services.items():
                        try:
                            resp = httpx.get(url, timeout=5)
                            if resp.status_code == 200:
                                status_lines.append(f"✅ **{name}**: Online")
                            else:
                                status_lines.append(f"⚠️ **{name}**: HTTP {resp.status_code}")
                        except Exception:
                            status_lines.append(f"❌ **{name}**: Unreachable")
                    return "\n".join(status_lines)

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
        css="""
        .chatbot { height: 550px !important; }
        .example-btn { font-size: 0.85em; }
        """,
    )
