from sqlalchemy import inspect, text


def test_all_tables_created(engine):
    names = set(inspect(engine).get_table_names())
    assert {"study", "event", "patient", "device", "code", "classification",
            "group_scheme", "group_band", "reference_set", "reference_value"} <= names


def test_mariadb_collations(engine):
    if engine.dialect.name not in ("mysql", "mariadb"):
        return
    with engine.connect() as conn:
        rows = dict(conn.execute(text(
            "SELECT CONCAT(TABLE_NAME,'.',COLUMN_NAME), COLLATION_NAME "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND "
            "COLUMN_NAME IN ('study_uid','event_uid','legacy_sha256','study_description')"
        )).all())
    assert rows["study.study_uid"] == "ascii_bin"
    assert rows["event.event_uid"] == "ascii_bin"
    assert rows["patient.legacy_sha256"] == "ascii_bin"
    assert rows["study.study_description"].startswith("utf8mb4")
