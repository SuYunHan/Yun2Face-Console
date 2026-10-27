import configparser

from web_app import is_auth_failure, persist_login_result


def test_persist_login_result_writes_all_credentials(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[User]\ntoken = old\ndevice_id = old\n", encoding="utf-8")
    persist_login_result(
        str(path),
        ("new-token", "device-2", "Pixel", "uuid-2", "14"),
    )
    conf = configparser.ConfigParser()
    conf.read(path, encoding="utf-8")
    assert conf.get("User", "token") == "new-token"
    assert conf.get("User", "device_id") == "device-2"
    assert conf.get("User", "device_name") == "Pixel"
    assert conf.get("User", "uuid") == "uuid-2"
    assert conf.get("User", "sys_edition") == "14"


def test_auth_failure_detects_expired_business_response():
    class Error:
        code = 401
        msg = "token expired"
    assert is_auth_failure(Error()) is True


def test_web_real_command_enables_reauth_persistence():
    import web_app
    argv = web_app._main_argv({
        "config": "F:/yunForNewVersion/students_cofig/TEST_STUDENT/config.ini",
        "mode": "real",
    })
    assert "--reauth-save-token" in argv
    assert "--login-retries" in argv
