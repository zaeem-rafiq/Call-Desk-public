"""The UI is served with Cache-Control: no-cache so browsers never run stale JS."""

from fastapi.testclient import TestClient


def test_page_and_static_files_revalidate():
    from app.main import app  # imported inside the test, after conftest switches Sentry to mock

    client = TestClient(app)
    for path in ("/", "/static/app.js", "/static/styles.css"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers.get("cache-control") == "no-cache", path
    assert client.get("/api/state").headers.get("cache-control") != "no-cache"
