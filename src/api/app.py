"""
FastAPI 应用入口（API + 本地前端）。

启动：
    python run_web.py
    或
    uvicorn src.api.app:app --reload --host 127.0.0.1 --port 8000
然后浏览器打开 http://127.0.0.1:8000/
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config.llm_config import get_settings
from src.api.middleware import RequestLoggingMiddleware
from src.api.routes import router
from src.utils.logging_setup import setup_logging

_settings = get_settings()
_log_dir = setup_logging(
    level=_settings.log_level,
    log_dir=_settings.log_dir,
    max_bytes=_settings.log_max_bytes,
    backup_count=_settings.log_backup_count,
)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

app = FastAPI(
    title="面试复盘助手",
    description=(
        "上传简历（PDF/DOCX）与面试对话（DOCX），"
        "生成 interview_qa.md 与 interview_review.md。"
        "注意：本服务不脱敏，请自行注意隐私。"
    ),
    version="0.1.0",
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.on_event("startup")
def _on_startup():
    import logging

    logging.getLogger(__name__).info(
        "应用启动完成，日志目录=%s（app.log / access.log）",
        _log_dir,
    )


@app.get("/health")
def health():
    return {"status": "ok", "log_dir": str(_log_dir)}


@app.get("/")
def index():
    """本地前端首页。"""
    index_path = WEB_DIR / "index.html"
    if not index_path.exists():
        return {"message": "前端未找到，请确认 web/index.html 存在", "docs": "/docs"}
    return FileResponse(index_path)
