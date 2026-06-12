"""
Standing intent routes for the web UI (page + HTMX partials).

Shared singletons (templates, stores, the HTTP client) are accessed lazily
through the ui.main module object (M.*) so tests that patch ui.main.task_store
etc. keep working unchanged. Importing ui.main here is circular-import-safe:
this module is imported at the BOTTOM of ui/main.py, after all globals exist,
and M attributes are only dereferenced at request time.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

import ui.main as M
from ui.yaml_codec import INTENT_BLUEPRINTS, intent_to_yaml, yaml_to_intent

router = APIRouter()


@router.get("/intents", response_class=HTMLResponse)
async def intents_page(request: Request):
    return M.templates.TemplateResponse(request, "intents.html", {"request": request})


@router.get("/partials/intent-list", response_class=HTMLResponse)
async def partial_intent_list(request: Request, tenant_id: str = "default"):
    intents = await run_in_threadpool(M.task_store.list_intents, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


@router.post("/partials/intent-create", response_class=HTMLResponse)
async def partial_intent_create(
    request: Request,
    name: str = Form(...),
    intent_type: str = Form(...),
    device: str = Form(""),
    alertname: str = Form(""),
    description: str = Form(""),
    metric_query: str = Form(""),
    threshold: str = Form(""),
    schedule: str = Form(""),
    action: str = Form(""),
    tenant_id: str = "default",
):
    data = {
        "name":         name,
        "intent_type":  intent_type,
        "device":       device,
        "alertname":    alertname,
        "description":  description,
        "metric_query": metric_query,
        "threshold":    threshold,
        "schedule":     schedule,
        "action":       action,
        "tenant_id":    tenant_id,
    }
    await run_in_threadpool(M.task_store.create_intent, data)
    intents = await run_in_threadpool(M.task_store.list_intents, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


@router.post("/partials/intent-toggle/{intent_id}", response_class=HTMLResponse)
async def partial_intent_toggle(request: Request, intent_id: str, tenant_id: str = "default"):
    existing = await run_in_threadpool(M.task_store.get_intent, intent_id)
    if existing:
        await run_in_threadpool(
            M.task_store.update_intent, intent_id, {"enabled": 0 if existing["enabled"] else 1}
        )
    intents = await run_in_threadpool(M.task_store.list_intents, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


@router.delete("/partials/intent-delete/{intent_id}", response_class=HTMLResponse)
async def partial_intent_delete(request: Request, intent_id: str, tenant_id: str = "default"):
    await run_in_threadpool(M.task_store.delete_intent, intent_id)
    intents = await run_in_threadpool(M.task_store.list_intents, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


@router.get("/partials/intent-edit/{intent_id}", response_class=HTMLResponse)
async def partial_intent_edit_form(request: Request, intent_id: str):
    intent = await run_in_threadpool(M.task_store.get_intent, intent_id)
    if not intent:
        return HTMLResponse("Not found", status_code=404)
    return M.templates.TemplateResponse(request, "partials/intent_edit_form.html", {
        "request": request,
        "intent": intent,
    })


@router.post("/partials/intent-edit/{intent_id}", response_class=HTMLResponse)
async def partial_intent_edit_save(
    request: Request,
    intent_id: str,
    name: str = Form(...),
    description: str = Form(""),
    device: str = Form(""),
    alertname: str = Form(""),
    metric_query: str = Form(""),
    threshold: str = Form(""),
    schedule: str = Form(""),
    action: str = Form(""),
    tenant_id: str = "default",
):
    await run_in_threadpool(M.task_store.update_intent, intent_id, {
        "name":         name,
        "description":  description,
        "device":       device,
        "alertname":    alertname,
        "metric_query": metric_query,
        "threshold":    threshold,
        "schedule":     schedule,
        "action":       action,
    })
    intents = await run_in_threadpool(M.task_store.list_intents, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


# ── Intent YAML editor routes ─────────────────────────────────────────────────

@router.get("/partials/intent-yaml-new", response_class=HTMLResponse)
async def partial_intent_yaml_new(request: Request, blueprint: str = ""):
    yaml_content = INTENT_BLUEPRINTS.get(blueprint, ("", ""))[1] if blueprint else ""
    blueprints = [(k, v[0]) for k, v in INTENT_BLUEPRINTS.items()]
    return M.templates.TemplateResponse(request, "partials/intent_yaml_editor.html", {
        "intent_id":   None,
        "blueprints":  blueprints,
        "yaml_content": yaml_content,
        "error":       None,
        "preview":     None,
    })


@router.post("/partials/intent-yaml-create", response_class=HTMLResponse)
async def partial_intent_yaml_create(request: Request, tenant_id: str = "default"):
    form = await request.form()
    yaml_str = (form.get("yaml_content") or "").strip()
    blueprints = [(k, v[0]) for k, v in INTENT_BLUEPRINTS.items()]
    try:
        data = yaml_to_intent(yaml_str, tenant_id=tenant_id)
    except ValueError as exc:
        return M.templates.TemplateResponse(request, "partials/intent_yaml_editor.html", {
            "intent_id":    None,
            "blueprints":   blueprints,
            "yaml_content": yaml_str,
            "error":        str(exc),
            "preview":      None,
        })
    await run_in_threadpool(M.task_store.create_intent, data)
    intents = await run_in_threadpool(M.task_store.list_intents, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


@router.get("/partials/intent-yaml-edit/{intent_id}", response_class=HTMLResponse)
async def partial_intent_yaml_edit(request: Request, intent_id: str):
    intent = await run_in_threadpool(M.task_store.get_intent, intent_id)
    if not intent:
        return HTMLResponse("Not found", status_code=404)
    blueprints = [(k, v[0]) for k, v in INTENT_BLUEPRINTS.items()]
    return M.templates.TemplateResponse(request, "partials/intent_yaml_editor.html", {
        "intent_id":    intent_id,
        "blueprints":   blueprints,
        "yaml_content": intent_to_yaml(dict(intent)),
        "error":        None,
        "preview":      None,
    })


@router.post("/partials/intent-yaml-save/{intent_id}", response_class=HTMLResponse)
async def partial_intent_yaml_save(
    request: Request, intent_id: str, tenant_id: str = "default"
):
    form = await request.form()
    yaml_str = (form.get("yaml_content") or "").strip()
    blueprints = [(k, v[0]) for k, v in INTENT_BLUEPRINTS.items()]
    try:
        data = yaml_to_intent(yaml_str, tenant_id=tenant_id)
    except ValueError as exc:
        return M.templates.TemplateResponse(request, "partials/intent_yaml_editor.html", {
            "intent_id":    intent_id,
            "blueprints":   blueprints,
            "yaml_content": yaml_str,
            "error":        str(exc),
            "preview":      None,
        })
    await run_in_threadpool(M.task_store.update_intent, intent_id, data)
    intents = await run_in_threadpool(M.task_store.list_intents, tenant_id=tenant_id)
    return M.templates.TemplateResponse(request, "partials/intent_list.html", {
        "request": request,
        "intents": intents,
    })


@router.post("/partials/intent-validate-yaml", response_class=HTMLResponse)
async def partial_intent_validate_yaml(request: Request):
    form = await request.form()
    yaml_str = (form.get("yaml_content") or "").strip()
    if not yaml_str:
        return HTMLResponse('<span class="muted" style="font-size:0.82em">Start typing to see a parsed summary…</span>')
    try:
        data = yaml_to_intent(yaml_str)
    except ValueError as exc:
        return HTMLResponse(
            f'<div class="yaml-error" style="font-size:0.82em">⚠ {exc}</div>'
        )
    itype   = data.get("intent_type", "monitor")
    enabled = "enabled" if data.get("enabled") else "disabled"
    lines   = [
        f'<div style="font-size:0.82em; display:flex; flex-direction:column; gap:6px">',
        f'<div><span class="muted">name:</span> <strong>{data["name"]}</strong></div>',
        f'<div><span class="muted">type:</span> {itype} &nbsp; <span class="muted">status:</span> {enabled}</div>',
    ]
    if data.get("device"):
        lines.append(f'<div><span class="muted">device:</span> {data["device"]}</div>')
    if itype == "monitor":
        lines.append(f'<div><span class="muted">query:</span> <code style="font-size:0.9em">{data.get("metric_query","")}</code></div>')
        if data.get("threshold"):
            lines.append(f'<div><span class="muted">threshold:</span> {data["threshold"]}</div>')
        lines.append(f'<div><span class="muted">interval:</span> {data.get("interval_seconds",300)}s &nbsp; <span class="muted">cooldown:</span> {data.get("cooldown_minutes",0)}min &nbsp; <span class="muted">priority:</span> {data.get("priority","normal")}</div>')
    elif itype == "chaos_schedule":
        lines.append(f'<div><span class="muted">schedule:</span> <code>{data.get("schedule","")}</code></div>')
    lines.append(f'<div style="color:#22c55e; margin-top:4px; font-size:0.8em">✓ Valid</div>')
    lines.append('</div>')
    return HTMLResponse("".join(lines))
