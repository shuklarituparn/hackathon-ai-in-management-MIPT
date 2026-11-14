"""T09 static-serving checks: /app and its assets are served by the application without a build step.

Behaviour is checked in a real browser by tests/test_ui_browser.py; these checks also run where no browser exists.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sverka.web import app

STATIC = Path(__file__).resolve().parents[1] / "sverka" / "static"


def test_app_page_and_assets_are_served():
    client = TestClient(app)
    page = client.get("/app")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    html = page.text
    assert '<html lang="ru">' in html
    assert '<script src="/static/app.js" defer></script>' in html
    assert '<link rel="stylesheet" href="/static/styles.css">' in html
    script = client.get("/static/app.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    styles = client.get("/static/styles.css")
    assert styles.status_code == 200 and styles.headers["content-type"].startswith("text/css")
    assert 'action="/check"' in client.get("/").text  # the legacy form stays at /


def test_standalone_evidence_viewer_and_assets_are_served():
    client = TestClient(app)
    page = client.get("/static/evidence.html")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert '<html lang="ru">' in page.text
    assert '<script src="/static/evidence.js" defer></script>' in page.text
    assert '<link rel="stylesheet" href="/static/evidence.css">' in page.text
    for asset, media_type in (("evidence.js", "javascript"), ("evidence.css", "text/css")):
        response = client.get("/static/" + asset)
        assert response.status_code == 200 and media_type in response.headers["content-type"]


@pytest.mark.parametrize(
    "html_file,script_file,style_file",
    [("index.html", "app.js", "styles.css"), ("evidence.html", "evidence.js", "evidence.css")],
)
def test_assets_are_self_contained_and_hold_no_model_access(html_file, script_file, style_file):
    html = (STATIC / html_file).read_text(encoding="utf-8")
    script = (STATIC / script_file).read_text(encoding="utf-8")
    styles = (STATIC / style_file).read_text(encoding="utf-8")
    for text in (html, script, styles):
        assert not re.search(r"(src|href)=[\"']?https?://", text), "no external scripts, styles or fonts"
        assert "@import" not in text
    # The browser talks only to this application's /api and /health; the backend calls the model.
    for marker in ("chat/completions", "api_key", "apiKey", "SVERKA_PROVIDER", "Authorization", "Bearer"):
        assert marker not in script
    requests = script.replace('"http://www.w3.org/2000/svg"', "")  # the SVG namespace is an identifier, not a request
    assert "http://" not in requests and "https://" not in requests, "requests go to relative same-origin paths only"
    if style_file == "styles.css":
        assert "/static/fonts/" in styles, "fonts are served by the application, not fetched from a font CDN"


@pytest.mark.parametrize("script_file", ["app.js", "evidence.js"])
def test_script_has_no_html_string_rendering(script_file):
    """Guard for safe text rendering: untrusted text is inserted with text nodes, never parsed as HTML."""
    script = (STATIC / script_file).read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in script, sink
    # Amounts are formatted from decimal strings; the script never turns them into numbers to add up.
    assert "parseFloat" not in script
    assert not re.search(r"Number\((?:[a-z_.]*\.)?(amount|reported_value|expected_value|observed_sum|diff)", script)
