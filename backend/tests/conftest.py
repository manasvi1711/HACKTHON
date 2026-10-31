import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolated data dir and deterministic mock generation for every test run.
_TMP = tempfile.mkdtemp(prefix="docqa-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["LLM_PROVIDER"] = "mock"
os.environ["SEED_SAMPLE_DOCS"] = "true"
os.environ["SEED_TEST_FIXTURES"] = "true"   # adversarial fixtures are activated for tests only
os.environ.setdefault("DEMO_PASSWORD", "demo-only-password")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.security import rate_limiter  # noqa: E402

PASSWORD = os.environ["DEMO_PASSWORD"]


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    rate_limiter.reset()
    yield


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="session")
def tokens(client):
    return {u: _login(client, u) for u in ("emma", "eli", "sam", "hana", "adam")}


@pytest.fixture(scope="session")
def docs(client, tokens):
    return {d["title"]: d for d in client.get("/api/documents", headers=tokens["adam"]).json()}


def ask(client, headers, question, **kw):
    r = client.post("/api/ask", json={"question": question, **kw}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def upload(client, headers, name, data, roles="employee,sales,hr,admin", **form):
    return client.post("/api/documents", headers=headers, files={"file": (name, data)},
                       data={"allowed_roles": roles, **form})
