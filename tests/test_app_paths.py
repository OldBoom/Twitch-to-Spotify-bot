from pathlib import Path

import app_paths


def test_data_dir_is_project_root_when_not_frozen() -> None:
    root = app_paths.data_dir()
    assert (root / "src" / "app_paths.py").is_file()
    assert root == Path(app_paths.__file__).resolve().parent.parent


def test_src_dir_points_at_package_tree() -> None:
    source = app_paths.src_dir()
    assert source is not None
    assert (source / "main.py").is_file()
    assert (source / "control_app.py").is_file()


def test_ensure_data_cwd_changes_into_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_paths, "data_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    assert app_paths.ensure_data_cwd() == tmp_path
    assert Path.cwd() == tmp_path
