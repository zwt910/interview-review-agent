"""
大模型与应用运行配置（独立文件，便于集中管理与审查）。

API Key 优先从操作系统环境变量 interview 读取。
FR2 / FR3 / FR5 可分别配置模型与生成参数（共用 Key 与默认 base_url）。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LLMStage = Literal["resume", "dialogue", "analysis"]


def _hydrate_os_env_from_windows_registry() -> None:
    """把 Windows 用户/系统级环境变量同步进当前进程。"""
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return

    def _read(root, path: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            with winreg.OpenKey(root, path) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                        if isinstance(name, str) and isinstance(value, str):
                            out[name] = value
                        i += 1
                    except OSError:
                        break
        except OSError:
            return out
        return out

    merged = {}
    merged.update(
        _read(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        )
    )
    merged.update(_read(winreg.HKEY_CURRENT_USER, r"Environment"))
    for k, v in merged.items():
        if k and k not in os.environ:
            os.environ[k] = v


_hydrate_os_env_from_windows_registry()


class _LLMBase(BaseSettings):
    """公共连接默认值；子类用阶段前缀覆盖 model 等。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    api_key: str = Field(default="", validation_alias="interview")
    base_url: str = Field(default="https://api.deepseek.com", validation_alias="LLM_BASE_URL")
    model: str = Field(default="deepseek-v4-pro", validation_alias="LLM_MODEL")
    temperature: float = Field(default=0.2, validation_alias="LLM_TEMPERATURE")
    max_tokens: int = Field(default=15000, validation_alias="LLM_MAX_TOKENS")
    timeout: float = Field(default=180.0, validation_alias="LLM_TIMEOUT")
    max_retries: int = Field(default=3, validation_alias="LLM_MAX_RETRIES")
    thinking_enabled: bool = Field(default=False, validation_alias="LLM_THINKING")
    reasoning_effort: str = Field(default="high", validation_alias="LLM_REASONING_EFFORT")


class LLMConfig(_LLMBase):
    """兼容旧代码的默认配置（等同全局 LLM_*）。"""


class LLMConfigResume(_LLMBase):
    """FR2 简历结构化摘要。"""

    model: str = Field(default="deepseek-flash", validation_alias="LLM_RESUME_MODEL")
    temperature: float = Field(default=0.2, validation_alias="LLM_RESUME_TEMPERATURE")
    max_tokens: int = Field(default=15000, validation_alias="LLM_RESUME_MAX_TOKENS")
    thinking_enabled: bool = Field(default=False, validation_alias="LLM_RESUME_THINKING")


class LLMConfigDialogue(_LLMBase):
    """FR3 对话整理。"""

    model: str = Field(default="deepseek-flash", validation_alias="LLM_DIALOGUE_MODEL")
    temperature: float = Field(default=0.2, validation_alias="LLM_DIALOGUE_TEMPERATURE")
    max_tokens: int = Field(default=15000, validation_alias="LLM_DIALOGUE_MAX_TOKENS")
    thinking_enabled: bool = Field(default=False, validation_alias="LLM_DIALOGUE_THINKING")


class LLMConfigAnalysis(_LLMBase):
    """FR5 逐题分析。"""

    model: str = Field(default="deepseek-v4-pro", validation_alias="LLM_ANALYSIS_MODEL")
    temperature: float = Field(default=0.2, validation_alias="LLM_ANALYSIS_TEMPERATURE")
    max_tokens: int = Field(default=15000, validation_alias="LLM_ANALYSIS_MAX_TOKENS")
    thinking_enabled: bool = Field(default=False, validation_alias="LLM_ANALYSIS_THINKING")


class AppSettings(BaseSettings):
    """业务与运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    qa_analysis_concurrency: int = Field(default=3, validation_alias="QA_ANALYSIS_CONCURRENCY")
    persist_intermediate: bool = Field(default=True, validation_alias="PERSIST_INTERMEDIATE")
    output_dir: Path = Field(default=PROJECT_ROOT / "output", validation_alias="OUTPUT_DIR")
    sessions_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "sessions",
        validation_alias="SESSIONS_DIR",
    )
    pdf_min_text_chars: int = 200
    empty_answer_threshold: int = 5
    long_answer_threshold: int = 3000
    log_dir: Path = Field(default=PROJECT_ROOT / "logs", validation_alias="LOG_DIR")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_max_bytes: int = Field(default=5 * 1024 * 1024, validation_alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(default=5, validation_alias="LOG_BACKUP_COUNT")


@lru_cache
def get_llm_config() -> LLMConfig:
    return LLMConfig()


@lru_cache
def get_llm_config_resume() -> LLMConfigResume:
    return LLMConfigResume()


@lru_cache
def get_llm_config_dialogue() -> LLMConfigDialogue:
    return LLMConfigDialogue()


@lru_cache
def get_llm_config_analysis() -> LLMConfigAnalysis:
    return LLMConfigAnalysis()


def get_llm_config_for(stage: LLMStage) -> _LLMBase:
    if stage == "resume":
        return get_llm_config_resume()
    if stage == "dialogue":
        return get_llm_config_dialogue()
    if stage == "analysis":
        return get_llm_config_analysis()
    raise ValueError(f"未知 LLM stage: {stage}")


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()
