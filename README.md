# 面试复盘助手（后端 + 算法）

根据《需求文档1》实现：上传简历与面试对话 → 结构化摘要 → 整理问答 → 逐题分析 → 输出 `interview_review.md`。

本地 Web 前端与 API 一体启动，历史会话侧边栏可回看，无需重复调用模型。

## 目录结构

```
config/
  llm_config.py          # 大模型与业务配置（独立文件）
prompts/                 # 提示词模板
web/                     # 本地前端（HTML/CSS/JS）
src/
  models/                # 数据结构 + 会话模型
  storage/session_store.py  # 会话持久化（历史侧边栏）
  parsers/               # FR1 文本提取
  llm/client.py          # 大模型客户端
  services/              # FR2–FR6 业务逻辑
  pipeline/              # 端到端编排
  api/                   # FastAPI（同时托管前端）
data/sessions/           # 历史会话落盘目录
run_web.py               # 一键启动本地页面
main.py                  # CLI
```

提示词与模型配置均独立存放，业务代码只调用，便于人工审查与迭代。

## 会话管理（类似 DeepSeek 历史）

每次上传生成会创建一个会话，产物写入 `data/sessions/{session_id}/`。  
之后**直接打开会话即可查看**，不会再次调用模型。

| 接口 | 说明 |
|------|------|
| `GET /api/v1/sessions` | 历史列表（侧边栏） |
| `GET /api/v1/sessions/{id}` | 详情（报告/问答/结构化结果，纯读盘） |
| `PATCH /api/v1/sessions/{id}` | 重命名 |
| `DELETE /api/v1/sessions/{id}` | 删除 |
| `POST /api/v1/sessions/stream` | SSE 流式生成（进度 + 逐题推送；推荐） |
| `POST /api/v1/sessions` | 阻塞至全部完成（兼容；岗位必填） |

## 快速开始（推荐：本地页面）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置大模型
copy .env.example .env
# 编辑 .env，填写 interview（API Key，仓库中保持为空）；默认 DeepSeek deepseek-v4-pro
# .env 已被忽略，不要提交

# 3. 启动本地 Web（浏览器打开 http://127.0.0.1:8000/）
python run_web.py
```

页面能力：新建复盘（上传）· 历史会话侧边栏 · 报告/问答回看 · 重命名/删除/下载。

### CLI（可选）

```bash
python main.py --dialogue path\to\dialogue.docx --resume path\to\resume.pdf
python main.py --list
python main.py --show <session_id>
```

## 流水线对应关系

| 阶段 | 模块 | 说明 |
|------|------|------|
| FR1 | `src/parsers/` | PDF/DOCX → 纯文本；扫描件明确报错 |
| FR2 | `services/resume_summary.py` + `prompts/resume_summary.txt` | 简历 JSON 摘要 |
| FR3 | `services/dialogue_organize.py` + `prompts/dialogue_organize.txt` | → `interview_qa.md` |
| FR4 | `services/qa_splitter.py` | 切分为 QAItem |
| FR5 | `services/qa_analyzer.py` + `prompts/qa_analysis.txt` | 并发逐题四段分析 |
| FR6 | `services/report_generator.py` | → `interview_review.md` |

## 配置说明

见 `config/llm_config.py` 与 `.env.example`：

- `interview`：API Key，仓库与 `.env.example` 中保持为空；只在本地 `.env` 或系统环境变量里填写。`LLM_BASE_URL` / `LLM_MODEL` 等可覆盖（默认 DeepSeek）
- `QA_ANALYSIS_CONCURRENCY`：逐题并发数
- `PERSIST_INTERMEDIATE`：是否额外写入 `output/` 兼容目录
- `SESSIONS_DIR`：会话根目录（默认 `data/sessions`）
- `LOG_DIR` / `LOG_LEVEL`：后台日志目录与级别（默认 `logs/` + INFO）

## 后台日志

启动后日志落在 `logs/`：

| 文件 | 内容 |
|------|------|
| `app.log` | 流水线 FR1–FR6、LLM 调用耗时/重试、业务异常 |
| `access.log` | HTTP 方法、路径、状态码、耗时、request_id |

可用环境变量 `LOG_LEVEL=DEBUG` 提高详细程度。日志滚动默认单文件约 5MB、保留 5 份。

## 隐私

按需求**不脱敏**。界面/接口会提示用户自行注意隐私；可通过 `persist=false` 减少落盘。

## 测试

```bash
pip install pytest
pytest tests/ -q
```
