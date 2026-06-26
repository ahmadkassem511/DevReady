"""Tests for .env generation, especially template discovery."""

from devready.environment.env_vars import generate_env_file


def test_reads_dotenv_sample_not_just_example(tmp_path):
    # Many repos ship `.env.sample` / `.env.template` instead of `.env.example`.
    (tmp_path / ".env.sample").write_text("API_HOST=localhost\nAPI_KEY=\n")
    path = generate_env_file(tmp_path, interactive=False)
    assert path is not None
    body = path.read_text()
    assert "API_HOST=localhost" in body
    # A secret-looking var with no example value gets a generated value.
    assert "API_KEY=" in body
    key_line = next(l for l in body.splitlines() if l.startswith("API_KEY="))
    assert len(key_line.split("=", 1)[1]) > 0  # not left blank (looks secret)


def test_template_variant_is_used(tmp_path):
    (tmp_path / ".env.template").write_text("PORT=4000\n")
    path = generate_env_file(tmp_path, interactive=False)
    assert path and "PORT=4000" in path.read_text()


def test_existing_env_is_not_overwritten(tmp_path):
    (tmp_path / ".env").write_text("REAL_SECRET=keepme\n")
    (tmp_path / ".env.sample").write_text("FOO=bar\n")
    # Should decline to overwrite a real .env.
    assert generate_env_file(tmp_path, interactive=False) is None
    assert "keepme" in (tmp_path / ".env").read_text()
