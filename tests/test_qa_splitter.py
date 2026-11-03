"""FR4 切分单元测试（不依赖大模型）。"""

from src.services.qa_splitter import split_qa_markdown

SAMPLE = """
## Q1｜话题：项目经历
**问：** 介绍一下你负责的推荐系统项目。
**答：** 我负责召回与排序链路，日活提升了 12%。

## Q2｜话题：技术方案
- 追问自：Q1
**问：** （承接上一题关于推荐系统的追问）当时为什么选双塔召回？
**答：** 主要考虑在线性能与可扩展性。
"""


def test_split_basic():
    items = split_qa_markdown(SAMPLE)
    assert len(items) == 2
    assert items[0].id == "Q1"
    assert items[0].parent_id is None
    assert items[0].topic == "项目经历"
    assert "推荐系统" in items[0].question
    assert items[1].id == "Q2"
    assert items[1].parent_id == "Q1"
    assert "双塔" in items[1].question


def test_empty_raises():
    try:
        split_qa_markdown("没有问答")
        assert False, "应抛出 ValueError"
    except ValueError:
        pass
