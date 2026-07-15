from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.auth.garmin_auth import GarminAuthError, get_auth
from app.config import get_settings
from app.services.workout_flow import draft_workout, execute_workout
from app.telegram.bot import build_application, run_bot_polling
from app.telegram.date_parse import parse_date_pt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_bot_task: asyncio.Task | None = None
_bot_app = None


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200):
    ctx = dict(context or {})
    ctx["request"] = request
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_task, _bot_app
    _bot_app = build_application()
    if _bot_app is not None:
        _bot_task = asyncio.create_task(run_bot_polling(_bot_app))
    yield
    if _bot_app is not None:
        try:
            await _bot_app.updater.stop()
            await _bot_app.stop()
            await _bot_app.shutdown()
        except Exception:
            logger.exception("Erro ao parar bot")
    if _bot_task:
        _bot_task.cancel()


app = FastAPI(title="Garmin Workout Integrator", lifespan=lifespan)


class DraftRequest(BaseModel):
    text: str = Field(min_length=1)


class ExecuteRequest(BaseModel):
    workout_body: dict[str, Any]
    date: str
    device_id: int | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return get_auth().status()


@app.post("/api/draft-workout")
def api_draft(req: DraftRequest) -> dict[str, Any]:
    try:
        return draft_workout(req.text).to_dict()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/execute")
def api_execute(req: ExecuteRequest) -> dict[str, Any]:
    try:
        d = parse_date_pt(req.date)
        result = execute_workout(req.workout_body, d.isoformat(), device_id=req.device_id)
        return result.to_dict()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    auth = get_auth()
    return render(request, "index.html", {"status": auth.status()})


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    settings = get_settings()
    return render(
        request,
        "setup.html",
        {
            "status": get_auth().status(),
            "default_email": settings.garmin_email,
            "message": None,
            "mfa_required": False,
        },
    )


@app.post("/setup/login", response_class=HTMLResponse)
def setup_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    auth = get_auth()
    try:
        result = auth.start_login(email, password)
        if result.get("status") == "mfa_required":
            return render(
                request,
                "setup.html",
                {
                    "status": auth.status(),
                    "default_email": email,
                    "message": f"MFA necessário ({result.get('mfa_method')}). Informe o código.",
                    "mfa_required": True,
                },
            )
        return RedirectResponse("/setup?ok=1", status_code=303)
    except GarminAuthError as exc:
        return render(
            request,
            "setup.html",
            {
                "status": auth.status(),
                "default_email": email,
                "message": str(exc),
                "mfa_required": False,
            },
            status_code=400,
        )


@app.post("/setup/mfa", response_class=HTMLResponse)
def setup_mfa(request: Request, code: str = Form(...)):
    auth = get_auth()
    try:
        auth.complete_mfa(code)
        return RedirectResponse("/setup?ok=1", status_code=303)
    except GarminAuthError as exc:
        return render(
            request,
            "setup.html",
            {
                "status": auth.status(),
                "default_email": "",
                "message": str(exc),
                "mfa_required": True,
            },
            status_code=400,
        )


@app.post("/setup/ticket", response_class=HTMLResponse)
def setup_ticket(request: Request, ticket: str = Form(...), email: str = Form("")):
    auth = get_auth()
    try:
        auth.login_with_ticket(ticket, email=email or None)
        return RedirectResponse("/setup?ok=1", status_code=303)
    except GarminAuthError as exc:
        return render(
            request,
            "setup.html",
            {
                "status": auth.status(),
                "default_email": email,
                "message": str(exc),
                "mfa_required": False,
            },
            status_code=400,
        )


@app.post("/setup/device", response_class=HTMLResponse)
def setup_device(request: Request, device_id: int = Form(...)):
    auth = get_auth()
    try:
        auth.set_device_id(device_id)
        return RedirectResponse("/setup?ok=1", status_code=303)
    except Exception as exc:
        return render(
            request,
            "setup.html",
            {
                "status": auth.status(),
                "default_email": "",
                "message": str(exc),
                "mfa_required": False,
            },
            status_code=400,
        )


@app.post("/test/run", response_class=HTMLResponse)
def test_run(
    request: Request,
    text: str = Form(...),
    date: str = Form(...),
):
    auth = get_auth()
    error = None
    result = None
    draft = None
    try:
        draft = draft_workout(text)
        d = parse_date_pt(date)
        result = execute_workout(draft.workout_body, d.isoformat())
    except Exception as exc:
        error = str(exc)
    return render(
        request,
        "index.html",
        {
            "status": auth.status(),
            "draft": draft.summary if draft else None,
            "result": result.to_dict() if result else None,
            "error": error,
            "text": text,
            "date": date,
        },
    )
