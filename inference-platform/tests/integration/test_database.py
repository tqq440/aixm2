import pytest
from sqlalchemy import event, text


def test_database_enables_sqlite_foreign_keys(app):
    database = app.extensions["database"]
    assert database.check_connection() is None
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_transaction_commits_success_and_rolls_back_failure(app):
    database = app.extensions["database"]
    completed = []
    with database.transaction() as successful:
        event.listen(successful, "after_commit", lambda session: completed.append("commit"))
        assert successful.execute(text("SELECT 1")).scalar_one() == 1
    assert not successful.in_transaction()
    with pytest.raises(RuntimeError, match="abort transaction"):
        with database.transaction() as failed:
            event.listen(failed, "after_rollback", lambda session: completed.append("rollback"))
            failed.execute(text("SELECT 1"))
            raise RuntimeError("abort transaction")
    assert completed == ["commit", "rollback"]
    assert not failed.in_transaction()
    # A failed unit of work must not poison the next independent session.
    with database.transaction() as recovered:
        assert recovered is not failed
        assert recovered.execute(text("SELECT 1")).scalar_one() == 1
