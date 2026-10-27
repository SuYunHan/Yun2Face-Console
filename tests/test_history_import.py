from web_app import next_history_filename, history_save_path


def test_history_filename_is_sequential_in_shared_tasks_dir(tmp_path):
    (tmp_path / "tasklist_0.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tasklist_3.json").write_text("{}", encoding="utf-8")
    name = next_history_filename(str(tmp_path))
    path = history_save_path(str(tmp_path), name)
    assert name == "tasklist_4.json"
    assert path.parent == tmp_path
    assert path.name == name
    assert ".." not in str(path)
