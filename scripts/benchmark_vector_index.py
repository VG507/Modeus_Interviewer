"""Compare exact vector search with pgvector's HNSW and IVFFlat indexes.

Rows are the evaluation cards' vectors plus noise, so they cluster like real cases;
queries are the same centres with fresh noise. For each method the script reports
the index build time, the mean query time and recall@10 against the exact answer.
Everything lives in temporary tables of one session, so nothing stays in the database.

Usage: .venv/bin/python scripts/benchmark_vector_index.py [--rows 20000] [--queries 50]
Needs DATABASE_URL on PostgreSQL with pgvector and the embedding model from .env.
"""

import argparse
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text

from backend.embeddings import card_texts, embed
from backend.settings import Settings

DATA = Path(__file__).resolve().parent.parent / "data" / "embeddings" / "catalog_search.json"
NOISE = 0.05  # per dimension; about half a unit vector's length in 1024 dimensions
TOP = 10


def literal(vector):
    return json.dumps([round(value, 6) for value in vector])


def timed_queries(connection, queries, sql):
    found, took = [], []
    for vector in queries:
        started = time.perf_counter()
        found.append([row[0] for row in connection.execute(text(sql), {"q": literal(vector), "top": TOP})])
        took.append((time.perf_counter() - started) * 1000)
    return found, statistics.mean(took)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--queries", type=int, default=50)
    args = parser.parse_args()
    settings = Settings()
    data = json.loads(DATA.read_text(encoding="utf-8"))
    contents = [card["content"] for card in data["cards"]] + [twin["content"] for twin in data["twins"]]
    centres = embed(settings, ["\n".join(body for _, body in card_texts(content)) for content in contents])
    dims = len(centres[0])
    rng = random.Random(7)
    queries = [[value + (rng.random() - 0.5) * NOISE for value in rng.choice(centres)] for _ in range(args.queries)]
    lists = max(1, args.rows // 1000)
    probes = max(1, round(math.sqrt(lists)))

    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        connection.execute(text(f"create temp table bench_centres (id int primary key, v vector({dims}))"))
        connection.execute(text("insert into bench_centres values (:id, cast(:v as vector))"),
                           [{"id": index, "v": literal(vector)} for index, vector in enumerate(centres)])
        connection.execute(text(f"create temp table bench (id serial primary key, embedding vector({dims}))"))
        started = time.perf_counter()
        # The noise subquery names g, so it is drawn again for every row.
        connection.execute(text(f"""
            insert into bench (embedding)
            select c.v + (select array_agg(((random() - 0.5) * :noise)::real)
                          from generate_series(1, {dims}) where g > 0)::vector({dims})
            from generate_series(1, :rows) g join bench_centres c on c.id = g % :centres
        """), {"noise": NOISE, "rows": args.rows, "centres": len(centres)})
        connection.execute(text("analyze bench"))
        print(f"{args.rows} rows x {dims} dims from {len(centres)} centres in {time.perf_counter() - started:.1f} s, "
              f"{args.queries} queries, top {TOP}\n")
        order = "select id from bench order by embedding <=> cast(:q as vector) limit :top"

        connection.execute(text("set enable_indexscan = off"))
        exact, exact_ms = timed_queries(connection, queries, order)
        connection.execute(text("set enable_indexscan = on"))
        print(f"{'method':<34} {'build':>8} {'query':>9} {'recall@10':>10}")
        print(f"{'exact (sequential scan)':<34} {'-':>8} {exact_ms:>7.1f}ms {1.0:>10.2f}")

        def measure(name, create, settings_sql=()):
            started = time.perf_counter()
            connection.execute(text(create))
            build = time.perf_counter() - started
            for statement in settings_sql:
                connection.execute(text(statement))
            plan = "\n".join(row[0] for row in connection.execute(
                text("explain " + order), {"q": literal(queries[0]), "top": TOP}))
            found, query_ms = timed_queries(connection, queries, order)
            recall = statistics.mean(len(set(a) & set(b)) / TOP for a, b in zip(found, exact))
            used = "" if "Index Scan" in plan else "  (planner skipped the index)"
            print(f"{name:<34} {build:>7.1f}s {query_ms:>7.1f}ms {recall:>10.2f}{used}")

        measure("HNSW m=16 ef_construction=64",
                "create index bench_hnsw on bench using hnsw (embedding vector_cosine_ops)",
                ["set hnsw.ef_search = 40"])
        measure("  same, ef_search=100", "select 1", ["set hnsw.ef_search = 100"])
        connection.execute(text("drop index bench_hnsw"))
        measure(f"IVFFlat lists={lists} probes={probes}",
                f"create index bench_ivf on bench using ivfflat (embedding vector_cosine_ops) with (lists = {lists})",
                [f"set ivfflat.probes = {probes}"])
        measure(f"  same, probes={probes * 3}", "select 1", [f"set ivfflat.probes = {probes * 3}"])
    engine.dispose()


if __name__ == "__main__":
    main()
