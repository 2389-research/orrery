"""work_unit surfaced as `facets` on document reads, remote_url on collection nodes."""
import json


WU = {"key": "github.com/acme/widget", "source": "git_remote"}


def _seed(store):
    c = store.conn
    c.execute("INSERT INTO documents (id, title, content, metadata) VALUES (?,?,?,?)",
              ("d_wu", "with wu", "x", json.dumps({"work_unit": WU, "children_hash": "h"})))
    c.execute("INSERT INTO documents (id, title, content) VALUES ('d_plain', 'plain', 'x')")
    c.execute("INSERT INTO documents (id, title, content, metadata) VALUES (?,?,?,?)",
              ("d_bad", "bad meta", "x", "not json{"))
    c.execute("INSERT INTO documents (id, title, content, metadata) VALUES (?,?,?,?)",
              ("d_list", "list meta", "x", json.dumps([1, 2])))
    c.commit()


def test_get_document_facets(test_client, test_store):
    _seed(test_store)
    body = test_client.get("/documents/d_wu").json()
    assert body["facets"] == {"work_unit": WU}
    assert "metadata" not in body
    for did in ("d_plain", "d_bad", "d_list"):
        b = test_client.get(f"/documents/{did}").json()
        assert b["facets"] == {}
        assert "work_unit" not in json.dumps(b)


def test_list_documents_facets(test_client, test_store):
    _seed(test_store)
    docs = {d["id"]: d for d in test_client.get("/documents").json()}
    assert docs["d_wu"]["facets"] == {"work_unit": WU}
    for did in ("d_plain", "d_bad", "d_list"):
        assert docs[did]["facets"] == {}
        assert "work_unit" not in json.dumps(docs[did])


def test_collection_node_remote_url(test_client, test_store):
    c = test_store.conn
    c.execute("INSERT INTO collections (id, name, path, kind, remote_url) "
              "VALUES ('r1', 'widget', 'widget', 'git_repo', 'git@github.com:acme/widget.git')")
    c.execute("INSERT INTO collections (id, name, path, kind) VALUES ('r2', 'other', 'other', 'git_repo')")
    c.commit()
    r = test_client.get("/collections/r1/summary").json()
    assert r["collection"]["remote_url"] == "git@github.com:acme/widget.git"
    r2 = test_client.get("/collections/r2/summary").json()
    assert r2["collection"]["remote_url"] is None
