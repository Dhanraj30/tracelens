from types import SimpleNamespace

from backend import db


def test_graph_passes_cloud_connection_settings_to_client(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def select_graph(self, name):
            return object()

    monkeypatch.setattr(db, "FalkorDB", FakeClient)
    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(
            falkordb_host="db.example",
            falkordb_port=50251,
            falkordb_username="falkordb",
            falkordb_password="secret",
            falkordb_ssl=True,
        ),
    )

    db.Graph("test")

    assert captured == {
        "host": "db.example",
        "port": 50251,
        "username": "falkordb",
        "password": "secret",
        "ssl": True,
    }