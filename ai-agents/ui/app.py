"""
Network AI Agents – Gradio Web UI

Provides a chat interface for both the Ops and Engineering agents.
"""
from __future__ import annotations

import os
import uuid

import gradio as gr
import httpx

OPS_AGENT_URL = os.getenv("OPS_AGENT_URL", "http://ai-ops-agent:8000")
ENG_AGENT_URL = os.getenv("ENG_AGENT_URL", "http://ai-eng-agent:8001")


def _chat_with_agent(agent_url: str, message: str, history: list, session_id: str) -> tuple[str, list, str]:
    """Send a message to an agent and return the response."""
    if not session_id:
        session_id = str(uuid.uuid4())
    try:
        resp = httpx.post(
            f"{agent_url}/chat",
            json={"message": message, "session_id": session_id},
            timeout=120,
        )
        resp.raise_for_status()
        response = resp.json()["response"]
    except httpx.ConnectError:
        response = "⚠️ Agent service is not available. Please check that the service is running."
    except Exception as e:
        response = f"⚠️ Error: {str(e)}"

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


def create_ui():
    with gr.Blocks(
        title="Network AI Agents",
        theme=gr.themes.Soft(primary_hue="blue"),
        css="""
        .chatbot { height: 550px !important; }
        .example-btn { font-size: 0.85em; }
        """,
    ) as demo:
        gr.Markdown(
            """
            # 🌐 Network Automation AI Agents
            **Ops Agent** – Monitor, investigate, and remediate network incidents  
            **Engineering Agent** – Design, configure, and document your network
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

            # ── Status Tab ───────────────────────────────────────────────────
            with gr.Tab("📊 Service Status"):
                gr.Markdown("## Service Health")

                def check_services():
                    services = {
                        "Ops Agent": f"{OPS_AGENT_URL}/health",
                        "Engineering Agent": f"{ENG_AGENT_URL}/health",
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
    )
