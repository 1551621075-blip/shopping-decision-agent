from pathlib import Path

import pytest

from shopping_advisor.schemas import DomainError
from shopping_advisor.storage import Store


@pytest.mark.parametrize("name", ["normal?mode=memory", "normal#fragment", "normal%20space",
                                  "中文 空格", "normal%2Fchild?mode=rwc#end"])
def test_database_uri_preserves_literal_directory_name(tmp_path, name):
    root = tmp_path / name
    store, tokens = Store.initialize(root, now=1900000000)
    assert store.authenticate(tokens["alice"]).user_id == "alice"
    reopened = Store(root)
    with reopened.transaction() as db:
        assert Path(db.execute("PRAGMA database_list").fetchone()[2]).resolve() == store.path.resolve()
        assert len(store.list(db, "product", owner="system")) == 16
    assert set(tmp_path.iterdir()) == {root}


def test_missing_database_is_not_recreated_by_transaction(tmp_path):
    store, _ = Store.initialize(tmp_path / "data", now=1900000000)
    store.path.rename(store.root / "saved.sqlite")
    with pytest.raises(DomainError, match="不存在"):
        with store.transaction():
            pytest.fail("Missing database must not open")
    assert not store.path.exists()
