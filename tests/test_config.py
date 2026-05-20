from pathlib import Path

from core.config import AppConfig, ConfigError, load_config


def test_load_example_config(tmp_path: Path) -> None:
    src = Path("config/config.example.yaml")
    dst = tmp_path / "config.yaml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    cfg = load_config(dst)

    assert isinstance(cfg, AppConfig)
    assert cfg.jira.base_url == "https://jira.example.com"
    assert cfg.policies
    policy = cfg.policies[0]
    assert policy.id == "mission_assurance_team"
    assert policy.user_source.users == ["alice.smith", "bob.jones", "carol.lee"]


def test_missing_jira_section(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("{}", encoding="utf-8")

    try:
        load_config(cfg_path)
    except ConfigError as e:
        assert "jira" in str(e)
    else:  # pragma: no cover
        raise AssertionError("Expected ConfigError")
