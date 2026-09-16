from pathlib import Path

from bot.provenance import build_runtime_provenance


def test_build_runtime_provenance_hashes_config_without_persisting_contents(
    tmp_path: Path,
) -> None:
    config = tmp_path / "my-config.yml"
    config.write_text("mcp_session_token: do-not-copy\n")

    result = build_runtime_provenance(
        str(config),
        repo_root=tmp_path,
        pid=123,
    )

    assert result["schema_version"] == 1
    assert result["pid"] == 123
    assert len(result["config_sha256"]) == 64
    assert result["code_commit"] == "unknown"
    assert "do-not-copy" not in str(result)


def test_build_runtime_provenance_uses_valid_environment_commit(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENCLAW_COMMIT_SHA", "A" * 40)

    result = build_runtime_provenance(None, repo_root=tmp_path, pid=321)

    assert result["code_commit"] == "a" * 40
