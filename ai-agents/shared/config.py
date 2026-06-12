"""
Shared configuration and settings for all AI agents.
"""
from __future__ import annotations

import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM configuration
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3"

    # LangSmith / observability
    langsmith_api_key: str = ""
    langsmith_project: str = "netauto-agents"
    langsmith_tracing: bool = False

    # Nautobot
    nautobot_url: str = "http://nautobot:8080"
    nautobot_token: str = ""

    # Prometheus
    prometheus_url: str = "http://prometheus:9090"

    # Alertmanager (separate container from Prometheus)
    alertmanager_url: str = "http://alertmanager:9093"

    # Loki
    loki_url: str = "http://loki:3100"

    # Alert event receiver
    alert_event_receiver_url: str = "http://alert-event-receiver:8770"

    # Agent API ports
    ops_agent_port: int = 8000
    eng_agent_port: int = 8001
    chaos_agent_port: int = 8002

    # Cost control — token budgets and pricing
    # The hourly limit is set high so the daily dollar budget is the real hard stop.
    # A low hourly limit causes problems during testing when multiple runs happen
    # within the same clock-hour window.
    max_tokens_per_agent_per_hour: int = 2_000_000
    max_tokens_per_agent_per_day: int = 1_000_000
    daily_budget_usd: float = 5.00
    # GPT-4o pricing per 1k tokens (update when model changes)
    openai_input_cost_per_1k: float = 0.005
    openai_output_cost_per_1k: float = 0.015

    # Lab validation before production execution
    lab_validation_enabled: bool = False   # set true to apply changes in clab before prod
    lab_device_prefix: str = "clab-"       # prefix mapping prod device → lab device name
    lab_verify_delay: int = 30             # seconds to wait after applying lab fix before checking

    # Post-execution verification delay — how long to wait after applying a fix
    # before querying Prometheus to confirm the alert resolved.
    # Default is 60 s (lab-friendly); set to 300 s or higher in production.
    execution_verify_delay: int = 60

    # Gitea runbook library
    gitea_url: str = "http://gitea:3000"
    gitea_token: str = ""          # Gitea API token with repo read access
    gitea_runbook_owner: str = "netauto"
    gitea_runbook_repo: str = "runbooks"
    gitea_runbook_branch: str = "main"

    # Maintenance window suppression
    # When enabled, the alert poller checks Nautobot device status and tags
    # before creating RCA tasks.  Devices in maintenance are deprioritised and
    # auto-execution is suppressed without preventing human review.
    maintenance_check_enabled: bool = False
    maintenance_statuses: str = "planned,staged,decommissioning"  # CSV of Nautobot statuses
    maintenance_tag: str = "maintenance"  # Nautobot tag that marks devices in maintenance

    # Agent API authentication — required on all /chat, /status, /usage endpoints
    agent_api_key: str = ""

    # Tenant identifier — used to scope all task queue queries.
    # Set to a unique value per customer/deployment in multi-tenant environments.
    agent_tenant_id: str = "default"

    # Agent identifier — used for status tracking and interactive chat budget key
    agent_name: str = "ai_agent"

    # Deployment environment — drives autonomy policy defaults.
    # lab: permissive defaults (L4 for known fix types)
    # staging: moderate defaults (L3)
    # production: conservative defaults (L2, always human-gated)
    environment: str = "lab"

    # Autonomy policy — seed built-in default policies on startup if the table is empty.
    policy_auto_seed: bool = True
    # Days before a promoted autonomy level expires and requires re-validation (Fix 3).
    # Set to 0 to disable TTL (not recommended for production).
    policy_promotion_ttl_days: int = 90

    # Standing intent registry — proactive health monitoring independent of alerts.
    intent_check_enabled: bool = True
    intent_evaluation_interval: int = 300  # seconds between proactive intent evaluation cycles

    # Topology correlation — cross-device incident grouping.
    topology_correlation_window: int = 30   # minutes: window to search for upstream RCAs
    topology_blast_radius_hops: int = 2     # BFS depth for blast-radius calculation
    topology_cache_ttl: int = 60            # seconds: cache Nautobot topology between polls

    # Policy synthesis — compile repeated, identical AI resolutions of the same
    # alert pattern into DRAFT fast-path policies (created disabled; an operator
    # reviews and enables them). Runs inside the hourly promotion sweep.
    policy_synthesis_enabled: bool = True
    policy_synthesis_min_successes: int = 3

    # Self-grading eval loop — chaos injections are recorded as ground truth and
    # the pipeline's response is graded into the accuracy ledger (System page).
    eval_grading_enabled: bool = True
    eval_grading_interval: int = 300       # seconds between grading sweeps
    eval_min_age_seconds: int = 300        # wait before grading (pipeline run time)
    eval_match_window_seconds: int = 1800  # injection → rca-task matching window

    # Chaos / destructive tools gate — set true ONLY in lab environments.
    # When false, shutdown_interface, restore_interface, and flap_bgp_neighbor
    # are removed from the chaos agent's tool set entirely.
    chaos_tools_enabled: bool = False

    # AI-optional mode — when False, LLM investigation is suppressed entirely.
    # Only programmatic fast-path policies (with conditions defined) will resolve
    # alerts. Alerts with no matching fast-path policy are placed in
    # awaiting_approval with a no_ai_skipped event for manual review.
    ai_enabled: bool = False

    # Inbound Alertmanager webhook authentication.
    # When set, POST /webhook/alert requires "Authorization: Bearer <secret>".
    # Configure Alertmanager's matching side via http_config.authorization
    # (see prometheus/alertmanager.yml). Empty = unauthenticated (lab default).
    alert_webhook_secret: str = ""

    # Approval webhook — POST when a task enters awaiting_approval
    approval_webhook_url: str = ""
    approval_webhook_secret: str = ""  # HMAC-SHA256 signing secret; empty = unsigned
    agent_ui_url: str = "http://localhost:7860"  # public URL used in webhook links

    # Slack incoming webhook URL for approval gate notifications
    slack_webhook_url: str = ""

    # PagerDuty Events API v2 routing key for approval gate incidents
    pagerduty_routing_key: str = ""

    @property
    def use_openai(self) -> bool:
        """Return True if OpenAI API key is configured."""
        return bool(self.openai_api_key)


settings = Settings()
