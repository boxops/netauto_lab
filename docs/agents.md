# AI Agents

The stack includes two AI agents built with LangChain/LangGraph:

- **Ops Agent** — Network operations assistant (monitoring, troubleshooting, runbook execution)
- **Engineering Agent** — Network engineering assistant (config generation, IP planning, playbook authoring)

Both agents use a **ReAct** (Reasoning + Acting) loop: they reason about a task, select a tool call, observe the result, and repeat until the task is complete.

## Accessing the Agents

| Interface | URL |
|-----------|-----|
| Web UI (both agents) | http://localhost:7860 |
| Ops Agent REST API | http://localhost:8000 |
| Engineering Agent REST API | http://localhost:8001 |

## Ops Agent

### Capabilities

| Capability | Description |
|------------|-------------|
| Alert investigation | Looks up active Prometheus alerts, queries related metrics |
| Log correlation | Searches Loki for syslog events matching an alert timeframe |
| Device lookup | Queries Nautobot for device info, interfaces, neighbors |
| Playbook execution | Runs Ansible playbooks (check mode by default) |

### Safety Rules

- All Ansible playbook executions default to `check_mode: true` (dry run).
- The agent will never run a live playbook without explicitly stating what it intends to do and receiving approval.
- Destructive operations (interface shutdown, BGP session clear) require explicit "yes, execute" confirmation in the prompt.

### Example Prompts

```
"Spine1 is showing as down in Prometheus. What's the alert status and what do the logs say?"

"Check the health of all BGP peers across the lab topology."

"Run a health check playbook against leaf1 in check mode."

"Show me the current active alerts and their severity."
```

### REST API

```bash
# Chat
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Check BGP status on leaf1", "session_id": "my-session"}'

# Health
curl http://localhost:8000/health
```

## Engineering Agent

### Capabilities

| Capability | Description |
|------------|-------------|
| Config generation | Generate EOS/IOS/JunOS device configs from requirements |
| IP planning | Get available IPs from Nautobot IPAM prefixes |
| VLAN design | Plan VLAN allocations with Nautobot context |
| Playbook authoring | Write Ansible playbooks from natural-language descriptions |
| Config review | Review configs against best practices |

### Example Prompts

```
"Generate an Arista EOS BGP configuration for a leaf switch with AS 65201, 
 connected to spine1 (AS 65001) and spine2 (AS 65002)."

"What IPs are available in the 10.0.0.0/24 prefix in Nautobot?"

"Write an Ansible playbook that sets SNMPv3 credentials on all EOS devices."

"Review this BGP config and suggest hardening improvements." 
```

### REST API

```bash
curl -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Design a leaf config for site NYC-DC1", "session_id": "eng-1"}'
```

## Configuration

Agent behavior is controlled via environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (none) | GPT-4o key (required for OpenAI) |
| `LLM_PROVIDER` | `openai` | `openai` or `ollama` |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model name |
| `OLLAMA_MODEL` | `llama3.1` | Ollama model name |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama endpoint |
| `AGENT_TEMPERATURE` | `0.1` | LLM temperature |
| `ALLOW_LIVE_EXECUTION` | `false` | Enable non-check-mode Ansible |

## Ollama (Local LLM Fallback)

If no `OPENAI_API_KEY` is set, agents use a locally running Ollama instance. Pull the model:

```bash
docker compose exec ollama ollama pull llama3.1
```

Local models are slower and less capable for complex multi-step reasoning. For production use, an OpenAI key is recommended.
