# -*- coding: utf-8 -*-
"""Focused regression tests for non-code target_file behavior in mcp_server."""
from __future__ import annotations

import tempfile
from pathlib import Path

import mcp_server
from shared.config import TGsConfig
from shared.db import Database


class RecordingRegistry:
    def __init__(self, result_text: str):
        self.result_text = result_text
        self.last_prompt: str | None = None
        self.last_code_only: bool | None = None

    def select_provider_for_tier(
        self,
        tier: str,
        *,
        prefer_free: bool = True,
        caller: str | None = None,
        code_only: bool = False,
    ) -> dict[str, object]:
        is_free = tier == "low"
        return {
            "provider": "GitHub Copilot",
            "provider_id": "github-copilot",
            "model": "gpt-5-mini" if is_free else "gpt-5.4",
            "tier": tier,
            "is_free": is_free,
            "billing_tier": "free" if is_free else "subscription",
            "provider_cost_hint": "free" if is_free else "included in subscription/quota",
            "cost_rank": 0 if is_free else 2,
            "billing_source": "user_override" if is_free else "provider_default",
            "excluded_providers": [],
        }

    def execute_cheapest(self, **kwargs: object) -> dict[str, object]:
        self.last_prompt = str(kwargs.get("prompt"))
        self.last_code_only = bool(kwargs.get("code_only"))
        return {
            "result": self.result_text,
            "provider": "test-provider",
            "provider_id": "test-provider",
            "model": "gpt-5-mini",
            "tier": "low",
            "is_free": True,
            "billing_tier": "free",
            "provider_cost_hint": "free",
            "cost_rank": 0,
            "billing_source": "provider_default",
            "fallback_used": False,
        }


def test_is_text_doc_target_classification() -> None:
    # text/doc extensions and bare names -> True
    assert mcp_server._is_text_doc_target("README") is True
    assert mcp_server._is_text_doc_target("README.md") is True
    assert mcp_server._is_text_doc_target("notes.txt") is True
    assert mcp_server._is_text_doc_target("index.rst") is True

    # code / data files -> False
    assert mcp_server._is_text_doc_target("script.py") is False
    assert mcp_server._is_text_doc_target("module.ts") is False
    assert mcp_server._is_text_doc_target("table.csv") is False
    assert mcp_server._is_text_doc_target("data.json") is False


def _prepare_env(td: str) -> tuple[Path, TGsConfig, Database]:
    repo_root = Path(td) / "repo"
    repo_root.mkdir()
    db_path = Path(td) / "execute.db"
    cfg = TGsConfig(db_path=db_path, delegation_utilities_enabled=True)
    db = Database(db_path=db_path)
    return repo_root, cfg, db


def test_md_target_writes_raw_markdown(monkeypatch) -> None:
    md = "# Title\n\nThis is raw markdown content.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(md)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        assert Path(result["file_written"]).exists()
        content = target.read_text(encoding="utf-8")
        assert content.rstrip("\n") == md.rstrip("\n")
        # prompt shaping for .md should ask for direct file contents
        assert registry.last_prompt is not None
        assert "direct content of README.md" in registry.last_prompt


def test_md_target_unwraps_fenced_markdown(monkeypatch) -> None:
    fenced = "```markdown\n# Title\nFenced content\n```"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(fenced)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert "```" not in content
        assert "# Title" in content
        # When fences removed preamble_stripped should be True (fences differ)
        assert result.get("preamble_stripped") is True


def test_md_target_preserves_internal_fenced_blocks(monkeypatch) -> None:
    markdown = "# Title\n\nExample:\n```python\nprint('hi')\n```\n\nDone.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(markdown)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert content.rstrip("\n") == markdown.rstrip("\n")
        assert "```python" in content
        assert "Done." in content


def test_md_target_preserves_heredoc_examples(monkeypatch) -> None:
    markdown = "# Example\n\ncat > config.txt <<'EOF'\nhello\nEOF\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(markdown)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert "cat > config.txt <<'EOF'" in content


def test_md_target_strips_boxed_heredoc_preview(monkeypatch) -> None:
    markdown = "│ cat > README.md <<'EOF'\n│ # Title\n│ Body\n# Final Title\n\nFinal body.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(markdown)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert "│ cat > README.md <<'EOF'" not in content
        assert content.startswith("# Final Title")


def test_md_target_strips_unboxed_heredoc_wrapper(monkeypatch) -> None:
    markdown = "cat > README.md <<'EOF'\n# Title\n\nBody\nEOF\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(markdown)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert content.startswith("# Title")
        assert "cat > README.md <<'EOF'" not in content


def test_md_target_preserves_same_name_heredoc_example_with_trailing_text(monkeypatch) -> None:
    markdown = "cat > README.md <<'EOF'\n# Example\nEOF\n\nThis trailing explanation should remain.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(markdown)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert "cat > README.md <<'EOF'" in content
        assert "This trailing explanation should remain." in content


def test_txt_target_strips_quoted_heredoc_wrapper(monkeypatch) -> None:
    text = 'cat > "My Notes.txt" <<\'DONE\'\nReal note.\nDONE\n'
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "My Notes.txt"

        registry = RecordingRegistry(text)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write note",
            "target_file": str(target),
        })

        assert "file_written" in result
        assert target.read_text(encoding="utf-8") == "Real note."


def test_md_target_rejects_reasoning_only(monkeypatch) -> None:
    reasoning = "I will write the README for you. First, I will explain what I'll do."
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(reasoning)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_write_error" in result
        assert not target.exists()


def test_txt_target_accepts_plain_text_without_markdown_structure(monkeypatch) -> None:
    text = "I will deliver the package tomorrow.\nThis note belongs in the file.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "note.txt"

        registry = RecordingRegistry(text)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write note",
            "target_file": str(target),
        })

        assert "file_written" in result
        assert target.read_text(encoding="utf-8").rstrip("\n") == text.rstrip("\n")


def test_md_target_strips_reasoning_preamble_before_document(monkeypatch) -> None:
    markdown = "I will write the README for you.\n# Title\n\nReal content.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(markdown)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert content.startswith("# Title")
        assert "I will write the README for you." not in content


def test_md_target_strips_below_is_wrapper(monkeypatch) -> None:
    markdown = "Below is the README content:\n# Title\n\nBody.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(markdown)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert content.startswith("# Title")
        assert "Below is the README content:" not in content


def test_md_target_strips_wrapper_before_outer_fence(monkeypatch) -> None:
    markdown = "Below is the README content:\n```markdown\n# Title\n\nBody.\n```"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(markdown)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert content.startswith("# Title")
        assert "```" not in content


def test_txt_target_preserves_single_box_drawing_line(monkeypatch) -> None:
    text = "│ Copyright notice\nAll rights reserved.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "notice.txt"

        registry = RecordingRegistry(text)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write notice",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert content.rstrip("\n") == text.rstrip("\n")


def test_txt_target_preserves_multiple_box_drawing_lines(monkeypatch) -> None:
    text = "│ Line one\n│ Line two\nAll rights reserved.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "notice.txt"

        registry = RecordingRegistry(text)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write notice",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert content.rstrip("\n") == text.rstrip("\n")


def test_txt_target_strips_meta_preamble_before_plain_text(monkeypatch) -> None:
    text = "Here is the file content:\nActual notice text.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "notice.txt"

        registry = RecordingRegistry(text)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write notice",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert content == "Actual notice text."


def test_txt_target_preserves_legitimate_i_am_opening(monkeypatch) -> None:
    text = "I am a short notice.\nPlease keep this opening line.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "notice.txt"

        registry = RecordingRegistry(text)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write notice",
            "target_file": str(target),
        })

        assert "file_written" in result
        assert target.read_text(encoding="utf-8").rstrip("\n") == text.rstrip("\n")


def test_txt_target_preserves_non_wrapper_below_is_sentence(monkeypatch) -> None:
    text = "Below is the README content for release 1.\nKeep this line.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "notice.txt"

        registry = RecordingRegistry(text)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write notice",
            "target_file": str(target),
        })

        assert "file_written" in result
        assert target.read_text(encoding="utf-8").rstrip("\n") == text.rstrip("\n")


def test_txt_target_rejects_multi_line_reasoning_only(monkeypatch) -> None:
    text = "I will write the notice for you.\nFirst I'll explain what I'll do.\nThen I'll provide it.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "notice.txt"

        registry = RecordingRegistry(text)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write notice",
            "target_file": str(target),
        })

        assert "file_write_error" in result
        assert not target.exists()


def test_txt_target_rejects_plain_refusal_text(monkeypatch) -> None:
    text = "Sorry, I can't do that.\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "notice.txt"

        registry = RecordingRegistry(text)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write notice",
            "target_file": str(target),
        })

        assert "file_write_error" in result
        assert not target.exists()


def test_md_target_unwraps_windows_line_endings_fence(monkeypatch) -> None:
    fenced = "```markdown\r\n# Title\r\nWindows content\r\n```\r\n"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "README.md"

        registry = RecordingRegistry(fenced)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write README",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert "```" not in content
        assert "# Title" in content


def test_py_target_preserves_code_and_strips_preamble(monkeypatch) -> None:
    raw = "Explanation text\n```py\nprint(\'hello\')\n```"
    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        target = repo_root / "generated.py"

        registry = RecordingRegistry(raw)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a python file",
            "target_file": str(target),
        })

        assert "file_written" in result
        content = target.read_text(encoding="utf-8")
        assert "print('hello')" in content
        # code target should strip the prose/preamble
        assert result.get("preamble_stripped") is True


def test_prompt_shaping_differs_between_md_and_py(monkeypatch) -> None:
    # MD target should request direct file content wording
    md_registry = RecordingRegistry("# ok\n")
    py_registry = RecordingRegistry("print('ok')\n")

    with tempfile.TemporaryDirectory() as td:
        repo_root, cfg, db = _prepare_env(td)
        md_target = repo_root / "README.md"
        py_target = repo_root / "generated.py"

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))

        # Test MD prompt
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: md_registry)
        mcp_server.handle_execute_subtask({"prompt": "Write README", "target_file": str(md_target)})
        assert md_registry.last_prompt is not None
        assert "direct content of README.md" in md_registry.last_prompt
        assert "valid Python" not in md_registry.last_prompt
        assert md_registry.last_code_only is False

        # Test PY prompt
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: py_registry)
        mcp_server.handle_execute_subtask({"prompt": "Write a python file", "target_file": str(py_target)})
        assert py_registry.last_prompt is not None
        assert "valid Python source code" in py_registry.last_prompt
        assert py_registry.last_code_only is True
