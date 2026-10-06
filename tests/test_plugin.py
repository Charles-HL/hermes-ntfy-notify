import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("ntfy_notify", pathlib.Path(__file__).parents[1] / "__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_plain_strips_markdown_and_truncates():
    out = mod._plain("# Title\n**bold** [link](http://x) ```code``` " + "word " * 100, 60)
    assert "**" not in out and "http" not in out and out.endswith("…") and len(out) <= 61


def test_subject_drops_workspace_tag():
    assert mod._subject("[Workspace::v1: /home/x] Hello there") == "Hello there"


def test_session_links():
    cfg = {"webui": "https://h.example"}
    links = mod._session_links(cfg, "abc123")
    assert links["click"] == "hermes://session/abc123"
    assert links["actions"][0]["url"] == "https://h.example/session/abc123"
