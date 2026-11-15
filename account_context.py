# -*- coding: utf-8 -*-
"""账号级运行上下文：配置、API 客户端与 HTTP Session 的隔离边界。"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass

from yun_http import YunClient, profile_from_conf


@dataclass(frozen=True)
class AccountContext:
    """一个账号的一次独立运行上下文，不读写 ``main`` 的模块级状态。"""

    config_path: str
    config: configparser.ConfigParser
    client: YunClient

    @classmethod
    def from_config(cls, config_path: str) -> "AccountContext":
        resolved = os.path.abspath(os.path.normpath(config_path))
        if not os.path.isfile(resolved):
            raise FileNotFoundError(f"配置文件不存在: {resolved}")

        conf = configparser.ConfigParser()
        if not conf.read(resolved, encoding="utf-8"):
            raise ValueError(f"配置文件无法读取: {resolved}")

        legacy_uuid = conf.get("User", "legacy_uuid", fallback="").strip().lower() in {
            "1", "true", "yes", "y",
        }
        client = YunClient(
            profile_from_conf(conf),
            base_url=conf.get("Yun", "school_host", fallback=""),
            legacy_uuid=legacy_uuid,
        )
        return cls(config_path=resolved, config=conf, client=client)
