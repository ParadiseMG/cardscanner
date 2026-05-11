"""FastAPI entrypoint for CardScanner."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings, REPO_ROOT, UPLOAD_DIR
from app.db import init_db
from app.routers import scan, inventory, stats, auth, listings, sync, drive


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="CardScanner", version="0.1.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )

    app.include_router(scan.router, prefix="/api")
    app.include_router(inventory.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(listings.router, prefix="/api")
    app.include_router(sync.router, prefix="/api")
    app.include_router(drive.router, prefix="/api")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": "0.1.0", "ebay_env": settings.ebay_env}

    # Serve uploaded card images
    app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

    static_dir = REPO_ROOT / "app" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
