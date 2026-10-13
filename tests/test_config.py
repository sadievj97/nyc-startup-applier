import pytest
import yaml

from applier.config import ConfigError, load_config, load_targets
from applier.paths import PROJECT_ROOT


def test_real_settings_files_are_valid():
    config = load_config(PROJECT_ROOT / "config" / "config.yaml")
    targets = load_targets(PROJECT_ROOT / "config" / "targets.yaml")
    assert config.mode in ("dry_run", "review")  # never auto without your say-so
    assert config.max_applications_per_day == 5
    assert config.ashby_submit == "assisted"
    assert targets.salary_floor_usd >= 0
    assert len(targets.role_families) == 8


def write_config(tmp_path, **changes):
    data = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_misspelled_setting_is_caught(tmp_path):
    with pytest.raises(ConfigError, match="max_aplications_per_day"):
        load_config(write_config(tmp_path, max_aplications_per_day=3))


def test_unknown_mode_is_caught(tmp_path):
    with pytest.raises(ConfigError, match="mode"):
        load_config(write_config(tmp_path, mode="yolo"))


def test_window_must_start_before_it_ends(tmp_path):
    with pytest.raises(ConfigError, match="submit_window"):
        load_config(write_config(tmp_path, submit_window={"start": "20:00", "end": "08:00"}))


def test_unknown_time_zone_is_caught(tmp_path):
    with pytest.raises(ConfigError, match="time zone"):
        load_config(write_config(tmp_path, submit_window={"start": "08:00", "end": "20:00", "timezone": "Mars/Base"}))


def test_broken_yaml_gets_a_plain_message(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: [dry_run\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)
