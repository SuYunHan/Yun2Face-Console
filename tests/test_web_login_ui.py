from pathlib import Path


APP_JS = Path(__file__).parents[1] / "web" / "app.js"


def _do_login_source() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("async function doLogin()")
    end = source.index("async function loadTasks()", start)
    return source[start:end]


def test_login_uses_selected_project_config_without_removed_rconfig_control():
    source = _do_login_source()
    assert "$('rConfig')" not in source
    assert "projectConfigPath()" in source
    assert "action: 'login'" in source
