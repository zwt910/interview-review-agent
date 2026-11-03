"""会话存储单元测试（不依赖大模型）。"""

from pathlib import Path

from src.models.schemas import AnalysisResult, PipelineResult, QAItem, ResumeSummary
from src.models.session import SessionStatus
from src.storage.session_store import SessionStore


def test_session_crud_and_reload(tmp_path: Path):
    store = SessionStore(root=tmp_path / "sessions")

    meta = store.create(
        dialogue_filename="面谈.docx",
        resume_filename="简历.pdf",
        title="测试会话",
        position="产品经理",
        has_job_description=False,
    )
    assert meta.status == SessionStatus.PENDING
    assert store.exists(meta.session_id)

    store.mark_running(meta.session_id)
    assert store.get_meta(meta.session_id).status == SessionStatus.RUNNING

    store.save_upload(
        meta.session_id,
        filename="面谈.docx",
        data=b"fake-docx",
        kind="dialogue",
    )

    result = PipelineResult(
        job_id=meta.session_id,
        position="产品经理",
        resume_summary=ResumeSummary(basic={"姓名": "张三"}),
        qa_items=[
            QAItem(id="Q1", topic="动机", question="为什么换工作？", answer="成长"),
        ],
        analyses=[
            AnalysisResult(
                qa_id="Q1",
                assessment_point="动机",
                original_answer="成长",
                improved_answer="希望更大舞台",
                success=True,
            )
        ],
        interview_qa_md="## Q1｜话题：动机\n**问：** 为什么换工作？\n**答：** 成长\n",
        interview_review_md="# 面试复盘报告\n\n完整内容\n",
        warnings=[],
    )
    store.save_text_artifact(meta.session_id, "dialogue_raw.txt", "面试官：你好")
    store.save_pipeline_result(meta.session_id, result, title="产品经理复盘")

    listed = store.list_sessions()
    assert len(listed) == 1
    assert listed[0].title == "产品经理复盘"
    assert listed[0].status == SessionStatus.COMPLETED
    assert listed[0].qa_count == 1

    detail = store.get_detail(meta.session_id)
    assert detail.interview_review_md.startswith("# 面试复盘报告")
    assert detail.qa_items[0].id == "Q1"
    assert detail.analyses[0].improved_answer == "希望更大舞台"
    assert detail.dialogue_raw == "面试官：你好"
    assert detail.resume_summary is not None
    assert detail.resume_summary.basic.get("姓名") == "张三"

    store.rename(meta.session_id, "新标题")
    assert store.get_meta(meta.session_id).title == "新标题"

    # 增量追加分析
    store.append_analysis(
        meta.session_id,
        AnalysisResult(qa_id="Q2", assessment_point="x", success=True),
    )
    detail2 = store.get_detail(meta.session_id)
    assert len(detail2.analyses) == 2

    # 旧会话缺 JD 字段仍可读取
    assert detail2.job_description == ""

    store.delete(meta.session_id)
    assert not store.exists(meta.session_id)


def test_old_meta_without_new_fields(tmp_path: Path):
    """模拟旧 meta.json 无 has_job_description 时仍可加载。"""
    import json

    store = SessionStore(root=tmp_path / "sessions")
    sid = "oldsession01"
    d = store.session_dir(sid)
    d.mkdir(parents=True)
    (d / "artifacts").mkdir()
    meta = {
        "session_id": sid,
        "title": "旧会话",
        "status": "completed",
        "position": "待确认",
        "qa_count": 0,
        "analysis_success_count": 0,
        "has_resume": False,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "warnings": [],
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    loaded = store.get_meta(sid)
    assert loaded.has_job_description is False
    detail = store.get_detail(sid)
    assert detail.job_description == ""
