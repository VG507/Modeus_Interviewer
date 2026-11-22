FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY alembic.ini ./
COPY backend/ ./backend/
COPY migrations/ ./migrations/
COPY data/interview/sql_topic.json ./data/interview/sql_topic.json
COPY --from=frontend /build/dist/ ./frontend/dist/
USER 10001:10001
EXPOSE 8080
CMD ["sh", "-c", ": \"${DATABASE_URL:?Set DATABASE_URL before starting Modeus}\" && python -m alembic upgrade head && exec python -m uvicorn backend.web_deploy:create_web_app --factory --host 0.0.0.0 --port 8080"]
