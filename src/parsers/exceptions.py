"""解析相关异常。"""


class ParseError(Exception):
    """文本提取失败（空文本 / 格式不支持 / 疑似扫描件等）。"""

    def __init__(self, message: str, *, code: str = "PARSE_ERROR"):
        super().__init__(message)
        self.code = code
        self.message = message
