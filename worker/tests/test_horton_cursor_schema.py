def test_horton_cursor_table_exists(tmp_path):
    from src.db import init_db, get_connection
    db = str(tmp_path / "t.db"); init_db(db)
    conn = get_connection(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(horton_cursor)")}
    assert cols == {"server_url", "after"}
