"""Editor download of the profession summary workbook for a period."""

import asyncio
from datetime import date
from io import BytesIO

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, model_validator
from sqlalchemy import select

from backend.db import VacancyCollection
from backend.interview_api import Actor
from backend.interview_schemas import Identifier
from backend.profession_summary import NoData, build_summary, demo_path, report_name, write_workbook
from backend.vacancy_api import require_editor, require_editor_write

router = APIRouter(prefix="/api/v1/reports")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profession_id: Identifier
    date_from: date
    date_to: date
    demo: StrictBool = False

    @model_validator(mode="after")
    def check_period(self):
        if self.date_from > self.date_to:
            raise ValueError("The period ends before it starts")
        return self


@router.get("/professions")
def professions(request: Request, actor: Actor):
    require_editor(actor)
    with request.app.state.sessions() as db:
        rows = db.execute(select(VacancyCollection.profession_id, VacancyCollection.title, VacancyCollection.stats)
                          .order_by(VacancyCollection.title, VacancyCollection.profession_id)).all()
    return {"professions": [{"id": profession_id, "title": title or profession_id,
                             "vacancies": (stats or {}).get("vacancies", 0), "demo": demo_path(profession_id) is not None}
                            for profession_id, title, stats in rows]}


@router.post("/profession")
async def profession_report(body: ReportRequest, request: Request, actor: Actor):
    require_editor_write(request, actor)
    sessions, settings = request.app.state.sessions, request.app.state.settings
    with sessions() as db:
        if db.get(VacancyCollection, body.profession_id) is None:
            raise HTTPException(404, "PROFILE_NOT_FOUND")
    materials = demo_path(body.profession_id) if body.demo else None
    if body.demo and materials is None:
        raise HTTPException(404, "REPORT_DEMO_NOT_FOUND")
    try:
        summary = await build_summary(sessions, settings, body.profession_id, materials, body.date_from, body.date_to)
    except NoData:
        raise HTTPException(409, "REPORT_NO_DATA") from None
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(504, "PROCESSING_TIMEOUT") from None
    except ValueError:
        # A grouping that names missing statements, or a reply outside the schema.
        raise HTTPException(502, "INVALID_MODEL_OUTPUT") from None
    except (RuntimeError, httpx.HTTPError) as exc:
        if str(exc) == "LLM_INVALID_RESPONSE":
            raise HTTPException(502, "INVALID_MODEL_OUTPUT") from None
        raise HTTPException(503, "LLM_UNAVAILABLE") from None
    output = BytesIO()
    await asyncio.to_thread(write_workbook, summary, output)
    return Response(output.getvalue(), media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{report_name(summary)}"'})
