import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-stage2.db'}",
        jwt_secret="test-secret-with-at-least-32-bytes-for-hs256",
        auto_seed=True,
        graph_backend="relational",
        data_dir=tmp_path / "data",
    )


@pytest.fixture()
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def auth(settings):
    return {"Authorization": f"Bearer {issue_token('policy-user', 'ROLE-04', settings.jwt_secret)}"}


def _judge(client, settings, **payload):
    body = {"asOf": "2025-06-01", "facts": {}, "question": "条款是否适用"}
    body.update(payload)
    return client.post("/api/policy/judge", json=body, headers=auth(settings))


@pytest.mark.parametrize("payload", [
    {},
    {"asOf": None},
    {"asOf": ""},
    {"asOf": "2025-2-01"},
    {"asOf": "2025-02-30"},
    {"asOf": 20250601},
])
def test_singular_judge_requires_valid_as_of(client, settings, payload):
    body = {"facts": {}, "question": "条款是否适用", **payload}
    response = client.post("/api/policy/judge", json=body, headers=auth(settings))
    assert response.status_code == 422


@pytest.mark.parametrize("max_hops", [0, 7, -1, "3", 1.5, None, True])
def test_singular_judge_rejects_invalid_max_hops(client, settings, max_hops):
    response = _judge(client, settings, maxHops=max_hops)
    assert response.status_code == 422


@pytest.mark.parametrize("max_hops", [1, 3, 6])
def test_singular_judge_accepts_max_hops_bounds(client, settings, max_hops):
    response = _judge(client, settings, maxHops=max_hops)
    assert response.status_code == 200
    assert set(response.json()) == {"outcomes", "asOf"}


def test_singular_judge_defaults_max_hops_to_three(client, settings, monkeypatch):
    service = client.app.state.conclusion
    original = service.judge
    observed = {}

    def spy(*args, **kwargs):
        observed["maxHops"] = kwargs.get("max_hops")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "judge", spy)
    response = _judge(client, settings)
    assert response.status_code == 200
    assert observed["maxHops"] == 3
    assert response.json()["asOf"] == "2025-06-01"
    assert set(response.json()) == {"outcomes", "asOf"}
