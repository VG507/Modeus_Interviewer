import csv
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from backend import onet
from backend.db import OnetProfile, VacancyCollection, database
from backend.discovery_api import onet_profile


def test_onet_batch_import_and_failed_refresh(database_url, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine, sessions = database(database_url)

    def write_csv(name, header, rows):
        with (tmp_path / name).open("w", encoding="utf-8-sig", newline="") as destination:
            writer = csv.writer(destination)
            writer.writerow(header)
            writer.writerows(rows)

    statements = []
    ratings = []
    for profession, code in onet.OCCUPATIONS.items():
        first = next(iter(onet.EXAMPLES[profession]))
        statements.extend([
            [code, profession, first, "A task, with punctuation.\nAnd a second line.", "Core"],
            [code, profession, 1, "Lower-ranked primary task.", "Core"],
            [code, profession, 2, "Supplemental task.", "Supplemental"],
        ])
        ratings.extend([
            [code, first, "FT", "99", ""],
            [code, first, "IM", "4.2", "N"],
            [code, 1, "IM", "5", "Y"],
            [code, 2, "IM", "4.9", "N"],
        ])
    statements.append(["11-1011.00", "Other", 55, "Ignore this occupation.", "Core"])
    statements.append([onet.ML_CODE, "Data Scientists", 21827, "Compare model metrics.", "Core"])
    ratings.append([onet.ML_CODE, 21827, "IM", "4.6", "N"])
    write_csv(onet.STATEMENTS, [onet.CODE, "Title", onet.TASK_ID, "Task", "Task Type"], statements)
    write_csv(onet.RATINGS, [onet.CODE, onet.TASK_ID, "Scale ID", "Data Value", "Recommend Suppress"], ratings)

    collected = []

    def download(request):
        filename = request.url.path.rsplit("/", 1)[-1]
        collected.append(filename)
        assert request.url.path.endswith(f"/db_31_0_csv/{filename}")
        return httpx.Response(200, content=(tmp_path / filename).read_bytes())

    client_type = httpx.Client
    monkeypatch.setattr(onet.httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(download),
    ))
    try:
        status = onet.import_tasks(sessions)
        assert collected == [onet.STATEMENTS, onet.RATINGS]
        assert set(status["profiles"]) == onet.OCCUPATIONS.keys() | {"ml_engineer"}
        assert all(status["profiles"][key]["tasks"] == 3 for key in onet.OCCUPATIONS)
        assert status["profiles"]["ml_engineer"]["tasks"] == 1
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(sessions=sessions)))
        output = onet_profile(request, {"is_editor": True}, "python_backend")
        assert [row["importance"] for row in output["tasks"]] == [4.2, None, 4.9]
        assert output["tasks"][0]["text"] == "A task, with punctuation.\nAnd a second line."
        assert output["tasks"][0]["example_ru"]

        with sessions.begin() as db:
            db.add(VacancyCollection(profession_id="custom_ml", title="Machine Learning Engineer", title_key="machine learning engineer"))
        assert onet.import_tasks(sessions, directory=tmp_path)["profiles"]["custom_ml"]["code"] == onet.ML_CODE
        ml = onet_profile(request, {"is_editor": True}, "custom_ml")
        assert ml["tasks"][0]["importance"] == 4.6 and ml["tasks"][0]["example_ru"]
        output = onet_profile(request, {"is_editor": True}, "python_backend")

        write_csv(onet.STATEMENTS, [onet.CODE, "Title", onet.TASK_ID, "Task", "Task Type"],
                  [row for row in statements if row[0] != onet.OCCUPATIONS["devops"]])
        with pytest.raises(onet.OnetError, match="No task statements"):
            onet.import_tasks(sessions, directory=tmp_path)
        assert onet_profile(request, {"is_editor": True}, "python_backend") == output
        with sessions() as db:
            assert len(db.scalars(select(OnetProfile)).all()) == 6
    finally:
        engine.dispose()
