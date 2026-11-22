"""Import versioned O*NET task statements and importance ratings in one batch."""

import csv
import re
from contextlib import contextmanager
from io import TextIOWrapper
from tempfile import SpooledTemporaryFile
from time import monotonic

import httpx
from sqlalchemy import select

from backend.db import OnetProfile, VacancyCollection, insert_for, now
from backend.documentation import iso_time


VERSION = "31.0"
DOWNLOAD_ROOT = "https://www.onetcenter.org/dl_files/database"
LICENSE = "https://www.onetcenter.org/license_db.html"
# The source occupations are broader than our technology-specific roles.
# For DevOps, systems engineering contributes monitoring and deployment tasks.
OCCUPATIONS = {
    "python_backend": "15-1252.00",
    "frontend": "15-1254.00",
    "qa": "15-1253.00",
    "devops": "15-1299.08",
}
ML_CODE = "15-2051.00"
ML_TITLES = ("ml инженер", "ml engineer", "machine learning engineer", "инженер машинного обучения")
# Local interview prompts derived from selected tasks in release 31.0.
EXAMPLES = {
    "python_backend": {
        21670: "Вспомните, когда вы исправляли сбой или ускоряли существующий сервис. Что вы изменили?",
        21668: "Как в одном из проектов вы определили, какой производительности должен достичь сервис?",
        21669: "Расскажите, как вы проверяли конкретное изменение в сервисе перед выпуском.",
    },
    "frontend": {
        14700: "Расскажите о случае, когда интерфейс работал по-разному в браузерах или на устройствах.",
        14710: "Вспомните изменение интерфейса, которое вы проверяли после обновления. Как прошла проверка?",
        14719: "Расскажите о случае, когда вы ускорили страницу. Как вы измерили результат?",
    },
    "qa": {
        14642: "Вспомните дефект, который было трудно воспроизвести. Как вы его исследовали?",
        14640: "Расскажите, как вы выбрали проверки для регрессии после конкретного изменения.",
        14650: "Вспомните, когда пришлось обновить автотесты. Что изменилось в продукте?",
    },
    "devops": {
        14675: "Вспомните инцидент, на который первым указал мониторинг. Как вы подтвердили проблему?",
        14686: "Расскажите, как вы проверяли конкретное исправление инфраструктуры после развёртывания.",
        14669: "Вспомните изменение, после которого нужно было проверить стабильность или масштабируемость системы.",
    },
}
EXAMPLES_BY_CODE = {
    **{code: EXAMPLES[profession] for profession, code in OCCUPATIONS.items()},
    ML_CODE: {
        21826: "Вспомните, когда качество исходных данных мешало обучению модели. Как вы это обнаружили?",
        21827: "Расскажите о случае, когда вы сравнивали модели. Какие метрики повлияли на ваш выбор?",
        21837: "Как вы проверяли, что модель хорошо работает на данных, которых не видела при обучении?",
    },
}
CODE = "O*NET-SOC Code"
TASK_ID = "Task ID"
STATEMENTS = "task_statements.csv"
RATINGS = "task_ratings.csv"
MAX_BYTES = 80 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 300


class OnetError(RuntimeError):
    pass


@contextmanager
def csv_file(client, version, filename, directory):
    if directory is not None:
        with (directory / filename).open(encoding="utf-8-sig", newline="") as source:
            yield source
        return

    deadline = monotonic() + MAX_DOWNLOAD_SECONDS
    url = f"{DOWNLOAD_ROOT}/db_{version.replace('.', '_')}_csv/{filename}"
    with SpooledTemporaryFile(max_size=2 * 1024 * 1024) as data:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            size = 0
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise OnetError(f"{filename}: file exceeds the download limit")
                if monotonic() > deadline:
                    raise OnetError(f"{filename}: download exceeded the time limit")
                data.write(chunk)
            expected = response.headers.get("Content-Length")
            if expected and not response.headers.get("Content-Encoding") and size != int(expected):
                raise OnetError(f"{filename}: incomplete download")
        data.seek(0)
        with TextIOWrapper(data, encoding="utf-8-sig", newline="") as source:
            yield source


def selected_rows(source, codes, required):
    reader = csv.DictReader(source, strict=True)
    if not {CODE, TASK_ID, *required}.issubset(reader.fieldnames or ()):
        raise OnetError("Unexpected O*NET CSV columns")
    for row in reader:
        if row[CODE] not in codes:
            continue
        if None in row or any(row[key] is None for key in (TASK_ID, *required)):
            raise OnetError(f"Incomplete O*NET CSV row at line {reader.line_num}")
        yield (row[CODE], int(row[TASK_ID])), row


def read_profiles(statements, ratings, codes):
    occupations = {code: {"occupation_title": "", "tasks": {}} for code in codes}
    for (code, task_id), row in selected_rows(statements, codes, {"Title", "Task", "Task Type"}):
        profile = occupations[code]
        title = row["Title"].strip()
        task = row["Task"]
        if (not title or len(title) > 150 or not task.strip() or task_id <= 0 or task_id in profile["tasks"]
                or profile["occupation_title"] not in ("", title)):
            raise OnetError(f"Invalid or duplicate statement {code}/{task_id}")
        profile["occupation_title"] = title
        profile["tasks"][task_id] = {
            "id": task_id, "text": task, "type": row["Task Type"] or None, "importance": None,
        }

    rated = set()
    for (code, task_id), row in selected_rows(ratings, codes, {"Scale ID", "Data Value", "Recommend Suppress"}):
        if row["Scale ID"] != "IM" or task_id not in occupations[code]["tasks"]:
            continue
        key = (code, task_id)
        if key in rated:
            raise OnetError(f"Duplicate importance rating {code}/{task_id}")
        rated.add(key)
        if row["Recommend Suppress"] == "Y" or not row["Data Value"]:
            continue
        importance = float(row["Data Value"])
        if not 1 <= importance <= 5:
            raise OnetError(f"Invalid importance rating {code}/{task_id}")
        occupations[code]["tasks"][task_id]["importance"] = importance

    for code, profile in occupations.items():
        if not profile["tasks"]:
            raise OnetError(f"No task statements for {code}")
        if not any((code, task) in rated for task in profile["tasks"]):
            raise OnetError(f"No matching importance ratings for {code}")
        profile["tasks"] = sorted(profile["tasks"].values(), key=lambda task: (
            task["type"] != "Core", -(task["importance"] or 0), task["id"],
        ))
    return occupations


def configured_occupations(db):
    professions = db.scalars(select(VacancyCollection.profession_id).where(
        (VacancyCollection.profession_id.in_(OCCUPATIONS)) | (VacancyCollection.title_key.in_(ML_TITLES)),
    ))
    return {profession: OCCUPATIONS.get(profession, ML_CODE) for profession in professions}


def import_tasks(sessions, version=VERSION, directory=None):
    if not re.fullmatch(r"\d{1,4}\.\d{1,4}", version):
        raise OnetError("Use an O*NET release number such as 31.0")
    stamp = now()
    with sessions() as db:
        selected = configured_occupations(db)
    if missing := OCCUPATIONS.keys() - selected.keys():
        raise OnetError(f"Missing Modeus profiles: {', '.join(sorted(missing))}")
    codes = set(selected.values())
    try:
        with httpx.Client(timeout=30, follow_redirects=True, max_redirects=3) as client:
            with csv_file(client, version, STATEMENTS, directory) as statements:
                with csv_file(client, version, RATINGS, directory) as ratings:
                    profiles = read_profiles(statements, ratings, codes)
    except httpx.HTTPStatusError as exc:
        raise OnetError(f"O*NET returned HTTP {exc.response.status_code}") from None
    except (httpx.RequestError, OSError, ValueError, csv.Error) as exc:
        raise OnetError(f"Could not read the O*NET CSV files ({type(exc).__name__})") from None

    imported = {}
    with sessions.begin() as db:
        if configured_occupations(db) != selected:
            raise OnetError("Modeus profiles changed during import; run import-onet again")
        for profession, code in sorted(selected.items()):
            profile = profiles[code]
            values = {"version": version, "occupation_code": code, "collected_at": stamp, **profile}
            insert = insert_for(db, OnetProfile).values(profession_id=profession, **values)
            changed = db.execute(insert.on_conflict_do_update(
                index_elements=[OnetProfile.profession_id], set_=values, where=OnetProfile.collected_at <= stamp,
            )).rowcount
            imported[profession] = {"code": code, "tasks": len(profile["tasks"]), "saved": bool(changed)}
    return {"version": version, "profiles": imported}


def profile_output(profile):
    examples = EXAMPLES_BY_CODE.get(profile.occupation_code, {}) if profile.version == VERSION else {}
    return {
        "version": profile.version, "occupation_code": profile.occupation_code,
        "occupation_title": profile.occupation_title,
        "collected_at": iso_time(profile.collected_at),
        "url": f"https://www.onetonline.org/link/details/{profile.occupation_code}#Tasks",
        "license_url": LICENSE,
        "tasks": [{**task, "example_ru": examples.get(task["id"])} for task in profile.tasks],
    }
