"""Tests for POST /api/nodes/bulk — the comic fan-out's batch node+edge create."""


def _board(client) -> int:
    return client.post("/api/boards", json={"name": "bulk"}).json()["id"]


def _node(client, bid: int, typ: str = "comic_import") -> int:
    return client.post("/api/nodes", json={"board_id": bid, "type": typ, "x": 0, "y": 0}).json()["id"]


def test_bulk_creates_nodes_and_edges(client):
    bid = _board(client)
    up = _node(client, bid)
    body = {
        "board_id": bid,
        "nodes": [
            {"type": "comic_page", "x": 340, "y": 0, "data": {"pageIdx": 0}, "source_id": up},
            {"type": "comic_page", "x": 340, "y": 330, "data": {"pageIdx": 1}, "source_id": up},
        ],
    }
    r = client.post("/api/nodes/bulk", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["nodes"]) == 2 and len(out["edges"]) == 2
    assert all(n["type"] == "comic_page" for n in out["nodes"])
    assert all(e["source_id"] == up for e in out["edges"])
    # data survives the round-trip
    assert {n["data"]["pageIdx"] for n in out["nodes"]} == {0, 1}

    detail = client.get(f"/api/boards/{bid}").json()
    assert len(detail["nodes"]) == 3  # upload + 2 pages
    assert len(detail["edges"]) == 2


def test_bulk_node_without_source_makes_no_edge(client):
    bid = _board(client)
    r = client.post("/api/nodes/bulk", json={"board_id": bid, "nodes": [{"type": "comic_panel", "x": 0, "y": 0}]})
    assert r.status_code == 200
    assert len(r.json()["nodes"]) == 1 and len(r.json()["edges"]) == 0


def test_bulk_rejects_bad_source(client):
    bid = _board(client)
    r = client.post("/api/nodes/bulk", json={"board_id": bid, "nodes": [{"type": "comic_page", "x": 0, "y": 0, "source_id": 999999}]})
    assert r.status_code == 400


def test_bulk_rejects_empty_and_unknown_board(client):
    bid = _board(client)
    assert client.post("/api/nodes/bulk", json={"board_id": bid, "nodes": []}).status_code == 400
    assert client.post("/api/nodes/bulk", json={"board_id": 999999, "nodes": [{"type": "comic_page", "x": 0, "y": 0}]}).status_code == 404
