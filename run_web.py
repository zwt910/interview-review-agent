"""
本地一键启动：API + 前端页面。

用法：
    python run_web.py

浏览器打开 http://127.0.0.1:8000/
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="启动面试复盘助手本地 Web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args()

    from config.llm_config import (
        get_llm_config,
        get_llm_config_analysis,
        get_llm_config_dialogue,
        get_llm_config_resume,
        get_settings,
    )
    from src.llm.client import get_llm_client, get_llm_client_for
    from src.utils.logging_setup import setup_logging

    get_settings.cache_clear()
    get_llm_config.cache_clear()
    get_llm_config_resume.cache_clear()
    get_llm_config_dialogue.cache_clear()
    get_llm_config_analysis.cache_clear()
    get_llm_client.cache_clear()
    get_llm_client_for.cache_clear()
    settings = get_settings()

    log_dir = setup_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        force=True,
    )

    cfg = get_llm_config()
    if cfg.api_key:
        print(f"已读取环境变量 interview（长度 {len(cfg.api_key)}）")
    else:
        print("警告：未读到环境变量 interview，生成复盘时会认证失败。")
        print("请在 Windows 用户环境变量中设置 interview = 你的 DeepSeek API Key")

    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        # 稍晚打开，等服务起来
        import threading

        def _open():
            import time

            time.sleep(1.2)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    import uvicorn

    print(f"面试复盘助手已启动：{url}")
    print(f"后台日志目录：{log_dir}")
    print(f"  - {log_dir / 'app.log'}   （业务/流水线/LLM）")
    print(f"  - {log_dir / 'access.log'}（HTTP 访问）")
    print("按 Ctrl+C 停止")
    uvicorn.run(
        "src.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=(settings.log_level or "info").lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
