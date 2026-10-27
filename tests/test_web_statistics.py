from datetime import date

import web_app

from web_app import summarize_semester_runs


def test_summarize_semester_runs_counts_morning_evening_qualified_and_total():
    runs = {
        "data": {
            "rank": [{
                "rankList": [
                    {"isMorning": "Y", "isQualified": "1"},
                    {"isMorning": "N", "isQualified": "1"},
                    {"recordEndTime": "2026-10-13 22:10:00", "isQualified": "0"},
                ]
            }]
        }
    }
    result = summarize_semester_runs(
        {"key": "2026-2027 学年 第一学期", "sjd": "2026.09.01-2027.01.31"},
        runs,
        today=date(2026, 10, 14),
    )
    assert result == {
        "semester": "2026-2027 学年 第一学期",
        "total": 3,
        "morning": 1,
        "evening": 2,
        "qualified": 2,
        "unqualified": 1,
    }


def test_statistics_client_uses_legacy_main_entrypoint_for_deployed_versions():
    source = web_app.fetch_student_statistics.__code__
    names = set(source.co_names)
    assert "profile_from_conf" not in names
    assert "set_args" in names
    assert "default_client" in names
    assert "_load_main_module" in names
