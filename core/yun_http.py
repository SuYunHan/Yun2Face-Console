# -*- coding: utf-8 -*-
"""`yun_http` 的最小替身：只提供规则代码导入所需的符号。

**这个文件不实现任何网络/加密功能，也不需要 gmssl / pycryptodome / cryptography。**
本工具做的是"上传之前的预检"，全程不发请求；原 `yun_face.py` 里凡是用到这些符号的
都是上传/比对路径（`compare_once` / `FaceVerifier`），预检路径一行都用不到。

如果以后要在这里做真实比对，请直接调用 yunForNewVersion 项目里的实现，
不要在本替身里补功能——否则就成了第二份协议实现，必然与项目分叉。
"""


class YunError(Exception):
    """项目异常基类（yun_http.YunError 的等价物）。"""


class HttpStatusException(YunError):
    def __init__(self, status: int, url: str, snippet: str = ""):
        super().__init__(f"HTTP {status} @ {url}: {snippet}")
        self.status = status
        self.url = url
        self.snippet = snippet


class TransportOutcomeUnknown(HttpStatusException):
    pass


class DecodeException(YunError):
    """响应无法解码（本工具不会产生）。"""


class BusinessException(YunError):
    def __init__(self, code, msg: str, payload: dict = None):
        super().__init__(f"业务错误 code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.payload = payload


class RunNotPermittedError(YunError):
    def __init__(self, message: str, record_id=None, raw=None):
        super().__init__(message)
        self.record_id = record_id
        self.raw = raw


class FaceRequiredError(YunError):
    pass


class YunClient:
    """占位：仅用于类型注解。本工具绝不实例化它。"""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "Yun2Face 是离线预检工具，不提供网络客户端。"
            "需要真实上传/比对请使用 yunForNewVersion 项目。"
        )
