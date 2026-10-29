from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from ceo_arena.dashboard import create_dashboard_app


def test_dashboard_owned_text_is_english_and_branding_is_consistent(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2] / "src/ceo_arena/dashboard"
    for path in root.rglob("*"):
        if path.suffix in {".py", ".js", ".html", ".css"}:
            assert not re.search(r"[\u3400-\u9fff]", path.read_text(encoding="utf-8")), path
    with TestClient(create_dashboard_app(tmp_path)) as client:
        html = client.get("/").text
        script = client.get("/app.js").text
        assert '<html lang="en">' in html
        assert "CEO Arena" in html
        assert "UTC+8" in html
        assert 'Intl.NumberFormat("en-US"' in script
        assert "X-CEOArena-Control" in script
        assert "zh-CN" not in script
        assert client.app.title == "CEO Arena Dashboard"
