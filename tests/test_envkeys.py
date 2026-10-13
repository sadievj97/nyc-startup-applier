"""Rule 9: keys are never shown or logged."""

import logging

from applier.envkeys import REDACTED, RedactingFilter, key_state

FAKE_KEY = "sk-ant-api03-" + "x" * 40


def test_key_states(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text("ANTHROPIC_API_KEY=paste-your-key-here\n", encoding="utf-8")
    env = tmp_path / ".env"

    assert key_state(env, example, "ANTHROPIC_API_KEY") == "missing"
    env.write_text("ANTHROPIC_API_KEY=paste-your-key-here\n", encoding="utf-8")
    assert key_state(env, example, "ANTHROPIC_API_KEY") == "placeholder"
    env.write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    assert key_state(env, example, "ANTHROPIC_API_KEY") == "missing"
    env.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    assert key_state(env, example, "ANTHROPIC_API_KEY") == "filled"


def test_real_key_in_template_is_detected(tmp_path):
    from applier.envkeys import template_has_secret

    example = tmp_path / ".env.example"
    example.write_text("# template\nANTHROPIC_API_KEY=paste-your-key-here\n", encoding="utf-8")
    assert not template_has_secret(example)
    example.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    assert template_has_secret(example)


def test_real_template_has_no_key():
    from applier.envkeys import template_has_secret
    from applier.paths import PROJECT_ROOT

    assert not template_has_secret(PROJECT_ROOT / ".env.example")


def test_logs_never_contain_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\nTELEGRAM_BOT_TOKEN=supersecretvalue123\n", encoding="utf-8")
    log_file = tmp_path / "test.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.addFilter(RedactingFilter(env))
    logger = logging.getLogger("redaction-test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("calling API with %s", FAKE_KEY)
        logger.info("token supersecretvalue123 in message")
        logger.info("a different key-shaped string sk-ant-api03-%s", "y" * 40)
        try:
            raise RuntimeError(f"failed with key {FAKE_KEY}")
        except RuntimeError:
            logger.exception("request failed")
    finally:
        logger.removeHandler(handler)
        handler.close()

    text = log_file.read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    assert "supersecretvalue123" not in text
    assert "y" * 40 not in text
    assert text.count(REDACTED) >= 4
