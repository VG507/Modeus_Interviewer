from pathlib import Path

from starlette.staticfiles import StaticFiles

from backend.app import create_app


def create_web_app():
    app = create_app()
    frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app
