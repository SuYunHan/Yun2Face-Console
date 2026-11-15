# -*- coding: utf-8 -*-
"""多账号隔离与调度原子性的回归测试。"""
import configparser
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import web_app
from conftest import FIXTURES


def _account_config(tmp_path: Path, name: str, token: str, host: str) -> Path:
    conf = configparser.ConfigParser()
    conf.read(Path(FIXTURES) / "test_config.ini", encoding="utf-8")
    conf.set("User", "token", token)
    conf.set("User", "device_name", name)
    conf.set("Yun", "school_host", host)
    path = tmp_path / f"{name}.ini"
    with path.open("w", encoding="utf-8") as fh:
        conf.write(fh)
    return path


def test_account_contexts_keep_config_client_and_session_isolated(tmp_path):
    """交错创建两个账号时，配置、客户端和 CookieJar 不得串用。"""
    context_type = getattr(web_app, "AccountContext", None)
    assert context_type is not None, "网页请求必须使用独立 AccountContext，而不是 reload(main)"
    assert not hasattr(web_app, "_load_main_module"), "不得再通过 reload(main) 切换账号"

    path_a = _account_config(tmp_path, "account_a", "TOKEN-A", "http://a.invalid")
    path_b = _account_config(tmp_path, "account_b", "TOKEN-B", "http://b.invalid")

    with ThreadPoolExecutor(max_workers=2) as pool:
        ctx_a, ctx_b = list(pool.map(context_type.from_config, (str(path_a), str(path_b))))

    assert ctx_a.config_path == str(path_a.resolve())
    assert ctx_b.config_path == str(path_b.resolve())
    assert ctx_a.client.profile.token == "TOKEN-A"
    assert ctx_b.client.profile.token == "TOKEN-B"
    assert ctx_a.client.base_url == "http://a.invalid"
    assert ctx_b.client.base_url == "http://b.invalid"
    assert ctx_a.client is not ctx_b.client
    assert ctx_a.client.session is not ctx_b.client.session

    ctx_a.client.session.cookies.set("account", "A")
    assert ctx_b.client.session.cookies.get("account") is None


@pytest.fixture
def schedule_store(monkeypatch):
    base = Path(__file__).parents[1] / "out"
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / ("_test_schedule_safety_" + uuid.uuid4().hex[:8])
    tmp.mkdir()
    monkeypatch.setattr(web_app, "RUN_DATA_DIR", str(tmp))
    monkeypatch.setattr(web_app, "SCHEDULES_FILE", str(tmp / "schedules.json"))
    web_app.SCHEDULES.clear()
    try:
        yield tmp
    finally:
        web_app.SCHEDULES.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def _create_schedule():
    return web_app.create_schedule({
        "name": "并发测试",
        "time_of_day": "07:30",
        "options": {"mode": "dry"},
    })


def test_schedule_mutation_and_persistence_share_one_lock(schedule_store, monkeypatch):
    lock = getattr(web_app, "SCHEDULE_LOCK", None)
    assert lock is not None, "所有定时任务状态必须由统一的 SCHEDULE_LOCK 保护"
    lock_states = []
    monkeypatch.setattr(web_app, "_persist_schedules",
                        lambda: lock_states.append(lock._is_owned()))

    _create_schedule()

    assert lock_states == [True], "内存修改与持久化必须位于同一临界区"


def test_concurrent_schedule_steps_fire_due_task_only_once(schedule_store, monkeypatch):
    item = _create_schedule()
    due = item["next_run"]
    gate = threading.Barrier(2)
    release = threading.Event()
    both_entered = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def start_run(options):
        with calls_lock:
            calls.append(dict(options))
            if len(calls) == 2:
                both_entered.set()
            run_id = f"r{len(calls)}"
        release.wait(timeout=1)
        return {"id": run_id}

    def step():
        gate.wait(timeout=1)
        return web_app._schedule_step(now=due)

    monkeypatch.setattr(web_app, "start_run", start_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(step) for _ in range(2)]
        both_entered.wait(timeout=0.25)
        release.set()
        [future.result(timeout=2) for future in futures]

    assert len(calls) == 1
    assert item["last_run"] == "r1"
    assert item["next_run"] > due

