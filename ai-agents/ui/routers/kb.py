"""
Knowledge-base routes for the web UI (page + HTMX partials).

Shared singletons (templates, stores, the HTTP client) are accessed lazily
through the ui.main module object (M.*) so tests that patch ui.main.task_store
etc. keep working unchanged. Importing ui.main here is circular-import-safe:
this module is imported at the BOTTOM of ui/main.py, after all globals exist,
and M attributes are only dereferenced at request time.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

import ui.main as M

router = APIRouter()


@router.get("/knowledge-base", response_class=HTMLResponse)
async def knowledge_base_page(request: Request):
    entries, total, alert_types = await asyncio.gather(
        run_in_threadpool(M.kb_store.get_all, 100),
        run_in_threadpool(M.kb_store.count),
        run_in_threadpool(M.kb_store.get_distinct_alert_types),
    )
    return M.templates.TemplateResponse(request, "knowledge_base.html", {
        "request":     request,
        "entries":     entries,
        "total":       total,
        "alert_types": alert_types,
        "query":       "",
    })


@router.get("/partials/kb-stats", response_class=HTMLResponse)
async def partial_kb_stats(request: Request):
    stats = await run_in_threadpool(M.kb_store.get_stats)
    return M.templates.TemplateResponse(request, "partials/kb_stats.html", {
        "request": request,
        "stats":   stats,
    })


@router.get("/partials/kb-search", response_class=HTMLResponse)
async def partial_kb_search(
    request:    Request,
    q:          str = "",
    source:     str = "",
    alert_type: str = "",
):
    if q.strip():
        entries = await run_in_threadpool(M.kb_store.search, q, 100)
        if source:
            entries = [e for e in entries if e["source"] == source]
        if alert_type:
            entries = [e for e in entries if e.get("alert_type") == alert_type]
    else:
        entries = await run_in_threadpool(M.kb_store.get_all, 100, source, alert_type)
    return M.templates.TemplateResponse(request, "partials/kb_results.html", {
        "request": request,
        "entries": entries,
        "query":   q,
    })


@router.get("/partials/kb-entry/{entry_id}/card", response_class=HTMLResponse)
async def kb_entry_card(request: Request, entry_id: int):
    entry = await run_in_threadpool(M.kb_store.get, entry_id)
    if not entry:
        return HTMLResponse("")
    return M.templates.TemplateResponse(request, "partials/kb_entry_card.html", {
        "request": request,
        "e":       entry,
    })


@router.get("/partials/kb-entry/{entry_id}/edit", response_class=HTMLResponse)
async def kb_entry_edit_form(request: Request, entry_id: int):
    entry = await run_in_threadpool(M.kb_store.get, entry_id)
    if not entry:
        return HTMLResponse("Entry not found", status_code=404)
    return M.templates.TemplateResponse(request, "partials/kb_entry_edit.html", {
        "request": request,
        "e":       entry,
    })


@router.patch("/partials/kb-entry/{entry_id}", response_class=HTMLResponse)
async def update_kb_entry(
    request:     Request,
    entry_id:    int,
    symptom:     str = Form(""),
    root_cause:  str = Form(""),
    resolution:  str = Form(""),
    alert_type:  str = Form(""),
    device_type: str = Form(""),
):
    entry = await run_in_threadpool(
        M.kb_store.update,
        entry_id,
        symptom=symptom.strip() or None,
        root_cause=root_cause.strip() or None,
        resolution=resolution.strip() or None,
        alert_type=alert_type.strip() or None,
        device_type=device_type.strip() or None,
    )
    if not entry:
        return HTMLResponse("Entry not found", status_code=404)
    return M.templates.TemplateResponse(request, "partials/kb_entry_card.html", {
        "request": request,
        "e":       entry,
    })


@router.post("/partials/kb-entry", response_class=HTMLResponse)
async def create_kb_entry(
    request: Request,
    symptom:     str = Form(""),
    root_cause:  str = Form(""),
    resolution:  str = Form(""),
    alert_type:  str = Form(""),
    device_type: str = Form(""),
):
    if symptom.strip() and root_cause.strip() and resolution.strip():
        entry = await run_in_threadpool(
            M.kb_store.save,
            symptom.strip(), root_cause.strip(), resolution.strip(),
            alert_type.strip() or None, device_type.strip() or None,
        )
        return M.templates.TemplateResponse(request, "partials/kb_entry_card.html", {
            "request": request,
            "e":       entry,
        })
    return HTMLResponse("")


@router.delete("/partials/kb-entry/{entry_id}", response_class=HTMLResponse)
async def delete_kb_entry(request: Request, entry_id: int):
    await run_in_threadpool(M.kb_store.delete, entry_id)
    return HTMLResponse("")
