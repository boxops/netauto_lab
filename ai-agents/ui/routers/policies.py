"""
Autonomy policy routes for the web UI (page + HTMX partials).

Shared singletons (templates, stores, the HTTP client) are accessed lazily
through the ui.main module object (M.*) so tests that patch ui.main.task_store
etc. keep working unchanged. Importing ui.main here is circular-import-safe:
this module is imported at the BOTTOM of ui/main.py, after all globals exist,
and M attributes are only dereferenced at request time.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

import ui.main as M
from ui.yaml_codec import POLICY_BLUEPRINTS, policy_to_yaml, yaml_to_policy

router = APIRouter()


@router.get("/policies", response_class=HTMLResponse)
async def policies_page(request: Request):
    return M.templates.TemplateResponse(request, "policies.html", {"request": request})


async def _get_agent_ai_mode() -> bool:
    try:
        r = await M._http_client.get(f"{M.OPS_AGENT_URL}/ai-mode", timeout=3.0)
        return r.json().get("ai_enabled", True)
    except Exception:
        return True  # assume enabled if agent unreachable


@router.get("/partials/policy-ai-notice", response_class=HTMLResponse)
async def partial_policy_ai_notice(request: Request):
    ai_enabled = await _get_agent_ai_mode()
    if ai_enabled:
        return HTMLResponse("")
    return HTMLResponse(
        '<div style="background:#78350f22; border:1px solid #d97706; border-radius:6px; '
        'padding:8px 14px; margin-bottom:12px; font-size:0.82em; color:#fbbf24">'
        '<strong>⚠ AI investigation is disabled.</strong> '
        'Regular autonomy policies (gate rules) only fire when the AI pipeline runs. '
        'Policies with programmatic <code>conditions</code> (fast-path) still fire. '
        'Enable AI in the Operations tab to activate full policy matching.'
        '</div>'
    )


@router.get("/partials/policy-list", response_class=HTMLResponse)
async def partial_policy_list(request: Request, tenant_id: str = "default"):
    policies = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
        "now_iso":  datetime.now(timezone.utc).isoformat(),
    })


@router.post("/partials/policy-create", response_class=HTMLResponse)
async def partial_policy_create(
    request: Request,
    name: str = Form(...),
    alertname: str = Form(""),
    fix_type: str = Form(""),
    device_role: str = Form(""),
    environment: str = Form(""),
    autonomy_level: str = Form("L2"),
    min_confidence: str = Form("low"),
    max_risk: str = Form("high"),
    promotable: str = Form(""),
    tenant_id: str = "default",
):
    form_data = await request.form()
    conditions   = M._parse_condition_rows(form_data)
    rca_template = M._parse_rca_template(form_data)
    fix_template = M._parse_fix_template(form_data)
    data = {
        "name":           name,
        "alertname":      alertname,
        "fix_type":       fix_type,
        "device_role":    device_role,
        "environment":    environment,
        "autonomy_level": autonomy_level,
        "min_confidence": min_confidence,
        "max_risk":       max_risk,
        "promotable":     bool(promotable),
        "tenant_id":      tenant_id,
        "conditions":     conditions,
        "rca_template":   rca_template,
        "fix_template":   fix_template,
    }
    await run_in_threadpool(M.task_store.create_policy, data)
    policies = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
        "now_iso":  datetime.now(timezone.utc).isoformat(),
    })


@router.post("/partials/policy-toggle/{policy_id}", response_class=HTMLResponse)
async def partial_policy_toggle(request: Request, policy_id: str, tenant_id: str = "default"):
    existing = await run_in_threadpool(M.task_store.get_policy, policy_id)
    if existing:
        await run_in_threadpool(
            M.task_store.update_policy, policy_id, {"enabled": 0 if existing["enabled"] else 1}
        )
    policies = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
        "now_iso":  datetime.now(timezone.utc).isoformat(),
    })


@router.delete("/partials/policy-delete/{policy_id}", response_class=HTMLResponse)
async def partial_policy_delete(request: Request, policy_id: str, tenant_id: str = "default"):
    await run_in_threadpool(M.task_store.delete_policy, policy_id)
    policies = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
        "now_iso":  datetime.now(timezone.utc).isoformat(),
    })


@router.get("/partials/policy-edit/{policy_id}", response_class=HTMLResponse)
async def partial_policy_edit_form(request: Request, policy_id: str):
    policy = await run_in_threadpool(M.task_store.get_policy, policy_id)
    if not policy:
        return HTMLResponse(f"<span class='muted'>Policy {policy_id} not found.</span>")
    import json as _json
    conditions_parsed: list = []
    if policy.get("conditions"):
        try:
            conditions_parsed = _json.loads(policy["conditions"])
        except Exception:
            conditions_parsed = []
    rca_parsed: dict = {}
    if policy.get("rca_template"):
        try:
            rca_parsed = _json.loads(policy["rca_template"])
        except Exception:
            rca_parsed = {}
    fix_parsed: dict = {}
    if policy.get("fix_template"):
        try:
            fix_parsed = _json.loads(policy["fix_template"])
        except Exception:
            fix_parsed = {}
    return M.templates.TemplateResponse(request, "partials/policy_edit_form.html", {
        "request":           request,
        "policy":            policy,
        "conditions_parsed": conditions_parsed,
        "rca_parsed":        rca_parsed,
        "fix_parsed":        fix_parsed,
    })


@router.post("/partials/policy-edit/{policy_id}", response_class=HTMLResponse)
async def partial_policy_edit_save(
    request: Request,
    policy_id: str,
    name:            str = Form(""),
    alertname:       str = Form(""),
    fix_type:        str = Form(""),
    device_role:     str = Form(""),
    environment:     str = Form(""),
    autonomy_level:  str = Form("L2"),
    min_confidence:  str = Form("low"),
    max_risk:        str = Form("high"),
    description:     str = Form(""),
    tenant_id:       str = "default",
):
    form_data = await request.form()
    conditions   = M._parse_condition_rows(form_data)
    rca_template = M._parse_rca_template(form_data)
    fix_template = M._parse_fix_template(form_data)
    updates: dict = {
        "autonomy_level": autonomy_level,
        "min_confidence": min_confidence,
        "max_risk":       max_risk,
        "description":    description,
        "conditions":     conditions,
        "rca_template":   rca_template,
        "fix_template":   fix_template,
    }
    if name:
        updates["name"] = name
    if alertname is not None:
        updates["alertname"] = alertname
    if fix_type is not None:
        updates["fix_type"] = fix_type
    if device_role is not None:
        updates["device_role"] = device_role
    if environment is not None:
        updates["environment"] = environment
    await run_in_threadpool(M.task_store.update_policy, policy_id, updates)
    policies = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
        "now_iso":  datetime.now(timezone.utc).isoformat(),
    })


def _policy_list_response(request: Request, policies: list, tenant_id: str = "default"):
    return M.templates.TemplateResponse(request, "partials/policy_list.html", {
        "request":  request,
        "policies": policies,
        "now_iso":  datetime.now(timezone.utc).isoformat(),
    })


@router.get("/partials/policy-yaml-new", response_class=HTMLResponse)
async def partial_policy_yaml_new(request: Request, blueprint: str = ""):
    yaml_content = POLICY_BLUEPRINTS.get(blueprint, ("", ""))[1] if blueprint else ""
    return M.templates.TemplateResponse(request, "partials/policy_yaml_editor.html", {
        "request":    request,
        "policy_id":  None,
        "yaml_content": yaml_content,
        "blueprints": [(k, v[0]) for k, v in POLICY_BLUEPRINTS.items()],
        "error":      "",
    })


@router.post("/partials/policy-yaml-create", response_class=HTMLResponse)
async def partial_policy_yaml_create(request: Request, tenant_id: str = "default"):
    form = await request.form()
    yaml_str = (form.get("yaml_content") or "").strip()
    try:
        data = yaml_to_policy(yaml_str, tenant_id=tenant_id)
    except ValueError as exc:
        return M.templates.TemplateResponse(request, "partials/policy_yaml_editor.html", {
            "request":      request,
            "policy_id":    None,
            "yaml_content": yaml_str,
            "blueprints":   [(k, v[0]) for k, v in POLICY_BLUEPRINTS.items()],
            "error":        str(exc),
        })
    await run_in_threadpool(M.task_store.create_policy, data)
    policies = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    return _policy_list_response(request, policies, tenant_id)


@router.get("/partials/policy-yaml-edit/{policy_id}", response_class=HTMLResponse)
async def partial_policy_yaml_edit(request: Request, policy_id: str):
    policy = await run_in_threadpool(M.task_store.get_policy, policy_id)
    if not policy:
        return HTMLResponse(f"<span class='muted'>Policy {policy_id} not found.</span>")
    yaml_content = policy_to_yaml(dict(policy))
    return M.templates.TemplateResponse(request, "partials/policy_yaml_editor.html", {
        "request":      request,
        "policy_id":    policy_id,
        "yaml_content": yaml_content,
        "blueprints":   [(k, v[0]) for k, v in POLICY_BLUEPRINTS.items()],
        "error":        "",
    })


@router.post("/partials/policy-yaml-save/{policy_id}", response_class=HTMLResponse)
async def partial_policy_yaml_save(request: Request, policy_id: str, tenant_id: str = "default"):
    form = await request.form()
    yaml_str = (form.get("yaml_content") or "").strip()
    try:
        data = yaml_to_policy(yaml_str, tenant_id=tenant_id)
    except ValueError as exc:
        return M.templates.TemplateResponse(request, "partials/policy_yaml_editor.html", {
            "request":      request,
            "policy_id":    policy_id,
            "yaml_content": yaml_str,
            "blueprints":   [(k, v[0]) for k, v in POLICY_BLUEPRINTS.items()],
            "error":        str(exc),
        })
    del data["tenant_id"]
    await run_in_threadpool(M.task_store.update_policy, policy_id, data)
    policies = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    return _policy_list_response(request, policies, tenant_id)


@router.get("/partials/policy-blueprint/{bp_id}", response_class=HTMLResponse)
async def partial_policy_blueprint(request: Request, bp_id: str):
    entry = POLICY_BLUEPRINTS.get(bp_id)
    if not entry:
        return HTMLResponse("")
    _, yaml_text = entry
    escaped = yaml_text.replace("`", "\\`").replace("${", "\\${")
    return HTMLResponse(
        f'<script>document.getElementById("policy-yaml-ta").value=`{escaped}`; '
        f'document.getElementById("policy-yaml-ta").dispatchEvent(new Event("input"));</script>'
    )


@router.post("/partials/policy-duplicate/{policy_id}", response_class=HTMLResponse)
async def partial_policy_duplicate(request: Request, policy_id: str, tenant_id: str = "default"):
    policy = await run_in_threadpool(M.task_store.get_policy, policy_id)
    if policy:
        copy: dict = {
            k: policy.get(k)
            for k in ("alertname", "fix_type", "device_role", "environment", "description",
                      "autonomy_level", "min_confidence", "max_risk", "promotable",
                      "conditions", "rca_template", "fix_template")
        }
        copy["name"] = f"{policy.get('name', 'policy')} (copy)"
        copy["tenant_id"] = tenant_id
        await run_in_threadpool(M.task_store.create_policy, copy)
    policies = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    return _policy_list_response(request, policies, tenant_id)


@router.post("/partials/policy-validate-yaml", response_class=HTMLResponse)
async def partial_policy_validate_yaml(request: Request):
    form = await request.form()
    yaml_str = (form.get("yaml_content") or "").strip()
    if not yaml_str:
        return M.templates.TemplateResponse(request, "partials/policy_yaml_validation.html", {
            "request": request, "ok": False, "summary": None, "error": "",
        })
    try:
        data = yaml_to_policy(yaml_str)
        summary = {
            "name":           data["name"],
            "alertname":      data["alertname"] or "any",
            "fix_type":       data["fix_type"] or "any",
            "device_role":    data["device_role"] or "any",
            "environment":    data["environment"] or "any",
            "level":          data["autonomy_level"],
            "min_confidence": data["min_confidence"],
            "max_risk":       data["max_risk"],
            "has_fast_path":  bool(data.get("conditions") or data.get("rca_template")),
            "condition_count": len(json.loads(data["conditions"])) if data.get("conditions") else 0,
        }
        return M.templates.TemplateResponse(request, "partials/policy_yaml_validation.html", {
            "request": request, "ok": True, "summary": summary, "error": "",
        })
    except ValueError as exc:
        return M.templates.TemplateResponse(request, "partials/policy_yaml_validation.html", {
            "request": request, "ok": False, "summary": None, "error": str(exc),
        })


@router.get("/partials/policy-export-yaml", response_class=HTMLResponse)
async def partial_policy_export_yaml(request: Request, tenant_id: str = "default"):
    policies = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    parts = [f"# Clano policy export — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"]
    for p in policies:
        parts.append("---")
        parts.append(policy_to_yaml(dict(p)).rstrip())
    full_yaml = "\n".join(parts) + "\n"
    from fastapi.responses import Response
    return Response(
        content=full_yaml,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=clano_policies.yaml"},
    )


@router.post("/partials/policy-simulate", response_class=HTMLResponse)
async def partial_policy_simulate(
    request: Request,
    alertname:   str = Form(""),
    device_role: str = Form(""),
    environment: str = Form(""),
    fix_type:    str = Form("config_change"),
    confidence:  str = Form("high"),
    risk:        str = Form("low"),
    tenant_id:   str = "default",
):
    from shared.policy_registry import PolicyRegistry
    registry = PolicyRegistry(M.task_store)

    gate_decision = await run_in_threadpool(
        registry.query,
        fix_type=fix_type,
        device_role=device_role,
        environment=environment,
        confidence=confidence,
        risk=risk,
        alertname=alertname,
        tenant_id=tenant_id,
    )
    fast_path_candidates = await run_in_threadpool(
        registry.get_fast_path_policies,
        alertname, tenant_id, device_role,
    )
    # Parse conditions JSON for display (don't execute)
    for p in fast_path_candidates:
        try:
            p["conditions_parsed"] = json.loads(p["conditions"]) if p.get("conditions") else []
        except Exception:
            p["conditions_parsed"] = []

    level_colors = {
        "L0": "#ef4444", "L1": "#f97316", "L2": "#eab308",
        "L3": "#22c55e", "L4": "#3b82f6", "L5": "#8b5cf6",
    }
    return M.templates.TemplateResponse(request, "partials/policy_simulate_result.html", {
        "request":              request,
        "alertname":            alertname,
        "device_role":          device_role,
        "environment":          environment,
        "fix_type":             fix_type,
        "confidence":           confidence,
        "risk":                 risk,
        "gate_decision":        gate_decision,
        "fast_path_candidates": fast_path_candidates,
        "level_colors":         level_colors,
    })


@router.get("/partials/policy-performance", response_class=HTMLResponse)
async def partial_policy_performance(request: Request, tenant_id: str = "default"):
    stats   = await run_in_threadpool(M.task_store.get_policy_stats, tenant_id)
    all_pol = await run_in_threadpool(M.task_store.list_policies, tenant_id=tenant_id)
    names   = {p["id"]: p["name"] for p in all_pol}
    return M.templates.TemplateResponse(request, "partials/policy_performance.html", {
        "request":      request,
        "stats":        stats,
        "policy_names": names,
    })
