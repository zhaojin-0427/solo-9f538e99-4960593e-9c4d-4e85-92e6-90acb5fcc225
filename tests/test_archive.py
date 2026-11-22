import io
import sqlite3

import pytest

from app import create_app
from app.db import connect, init_db
from app.service import (SaveError, correct_statement, load_archives, load_statement,
                         lock_row)

CSV = ("院系,设备费,差旅费,材料费\n"
       "物理学院,4500,5500,12000\n"
       "化学学院,5500,4500,8000\n"
       "数学学院,12340,9999,5000\n").encode("utf-8")


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test.sqlite3")


@pytest.fixture()
def client(db_path):
    app = create_app(db_path)
    app.config.update(TESTING=True)
    with app.test_client() as c:
        c.post("/upload", data={"csv": (io.BytesIO(CSV), "a.csv")},
               content_type="multipart/form-data")
        yield c


def _base_row(stmt, row_key):
    return [stmt["base"][(row_key, c.key)] for c in stmt["cols"]]


def _correct(db_path, stmt, overrides):
    cells = [[stmt["base"][(r.key, c.key)] for c in stmt["cols"]] for r in stmt["rows"]]
    row_idx = {r.key: i for i, r in enumerate(stmt["rows"])}
    col_idx = {c.key: j for j, c in enumerate(stmt["cols"])}
    for (rk, ck), v in overrides.items():
        cells[row_idx[rk]][col_idx[ck]] = v
    correct_statement(db_path, cells, stmt["base_version"])


def _delete_archives(db_path):
    """模拟升级前的旧库：只有锁定记录，没有留档。"""
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM archive_record")
    finally:
        conn.close()


def test_lock_saves_archive_with_yuan_amounts(client, db_path):
    stmt = load_statement(db_path)
    lock_row(db_path, "r1", stmt["version"])
    locked = load_statement(db_path)
    data = load_archives(db_path)
    assert len(data["records"]) == 1
    rec = data["records"][0]
    assert rec["row_label"] == "物理学院"
    assert rec["statement_version"] == stmt["version"]
    assert rec["base_status"] == "recorded"
    assert [i["col_label"] for i in rec["entries"]] == [c.label for c in stmt["cols"]]
    assert [i["value_wan"] for i in rec["entries"]] == locked["locks"]["r1"]["cells"]
    assert [i["amount_yuan"] for i in rec["entries"]] == _base_row(stmt, "r1")
    assert rec["subtotal_wan"] == stmt["row_totals"]["r1"]
    assert rec["base_subtotal_yuan"] == sum(_base_row(stmt, "r1"))


def test_archive_failure_rolls_back_lock_and_version(client, db_path, monkeypatch):
    """留档写入失败：锁定与留档都不生效，版号照旧。"""
    from app import service

    stmt = load_statement(db_path)

    def failing_insert(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service, "_insert_archive", failing_insert)
    with pytest.raises(SaveError):
        service.lock_row(db_path, "r1", stmt["version"])
    after = load_statement(db_path)
    assert after["version"] == stmt["version"]
    assert after["base_version"] == stmt["base_version"]
    assert "r1" not in after["locks"]
    assert load_archives(db_path)["records"] == []
    # 留档故障排除后仍可正常锁定
    monkeypatch.undo()
    lock_row(db_path, "r1", after["version"])
    assert len(load_archives(db_path)["records"]) == 1


def test_correction_between_two_locks_keeps_old_archives(client, db_path):
    """两次锁定之间的更正：旧档保持锁定时的元金额，新档留存更正后的金额。"""
    stmt = load_statement(db_path)
    lock_row(db_path, "r1", stmt["version"])
    r1_amounts = _base_row(stmt, "r1")
    stmt = load_statement(db_path)
    _correct(db_path, stmt, {("r2", "c1"): 6000})
    stmt = load_statement(db_path)
    lock_row(db_path, "r2", stmt["version"])
    stmt = load_statement(db_path)

    data = load_archives(db_path)
    assert len(data["records"]) == 2
    by_label = {r["row_label"]: r for r in data["records"]}
    rec1, rec2 = by_label["物理学院"], by_label["化学学院"]
    # 旧档不随更正改变
    assert [i["amount_yuan"] for i in rec1["entries"]] == r1_amounts
    assert rec1["statement_version"] == stmt["locks"]["r1"]["statement_version"]
    # 第二次锁定留存的是更正后的元金额
    assert [i["amount_yuan"] for i in rec2["entries"]] == _base_row(stmt, "r2")
    assert _base_row(stmt, "r2")[0] == 6000
    assert stmt["verified"] is True


def test_upload_clears_locks_but_keeps_archives(client, db_path):
    """整表换 CSV：当前锁定照常清空，旧档保留并可按原名称与版号查询。"""
    stmt = load_statement(db_path)
    client.post("/lock/r1", data={"version": stmt["version"]})
    stmt = load_statement(db_path)
    old_version = stmt["locks"]["r1"]["statement_version"]
    old_amounts = _base_row(stmt, "r1")
    # 换表前没有人打开过留档页
    new_csv = "院系,设备\n新学院,12345\n".encode()
    resp = client.post("/upload", data={"csv": (io.BytesIO(new_csv), "n.csv"),
                                        "version": str(stmt["version"])},
                       follow_redirects=True)
    assert "底表已导入" in resp.get_data(as_text=True)
    stmt = load_statement(db_path)
    assert not stmt["locks"]

    data = load_archives(db_path)
    assert len(data["records"]) == 1
    rec = data["records"][0]
    assert rec["row_label"] == "物理学院"
    assert rec["statement_version"] == old_version
    assert [i["col_label"] for i in rec["entries"]] == ["设备费", "差旅费", "材料费"]
    assert [i["amount_yuan"] for i in rec["entries"]] == old_amounts
    # 旧档可按原名称和版号查询
    html = client.get(f"/archive?row_label=物理学院&version={old_version}").get_data(as_text=True)
    assert "物理学院 · 版号" in html and "设备费" in html
    html = client.get("/archive?row_label=新学院").get_data(as_text=True)
    assert "无符合条件的留档记录" in html
    # 换表后的新锁定继续留档，版号不复用
    client.post("/lock/r1", data={"version": str(stmt["version"])})
    data = load_archives(db_path)
    assert len(data["records"]) == 2
    new_rec = [r for r in data["records"] if r["row_label"] == "新学院"][0]
    assert new_rec["statement_version"] > old_version
    assert [i["col_label"] for i in new_rec["entries"]] == ["设备"]


def test_backfill_existing_locks_recorded_when_no_correction(client, db_path):
    """旧库迁移：锁定后无更正，版号与锁定记录可确认，用现有底表补记元金额。"""
    stmt = load_statement(db_path)
    lock_row(db_path, "r1", stmt["version"])
    stmt = load_statement(db_path)
    lock_row(db_path, "r2", stmt["version"])
    stmt = load_statement(db_path)
    _delete_archives(db_path)
    init_db(db_path)  # 重启/升级时补建留档
    data = load_archives(db_path)
    assert len(data["records"]) == 2
    by_label = {r["row_label"]: r for r in data["records"]}
    for label, key in (("物理学院", "r1"), ("化学学院", "r2")):
        rec = by_label[label]
        assert rec["base_status"] == "recorded"
        assert [i["amount_yuan"] for i in rec["entries"]] == _base_row(stmt, key)
        assert [i["value_wan"] for i in rec["entries"]] == stmt["locks"][key]["cells"]


def test_backfill_marks_unavailable_when_corrected_after_lock(client, db_path):
    """旧库迁移：两次锁定之间发生过更正，证据不足的标「未留存」并保留公示数字。"""
    stmt = load_statement(db_path)
    lock_row(db_path, "r1", stmt["version"])
    stmt = load_statement(db_path)
    _correct(db_path, stmt, {("r2", "c1"): 6000})
    stmt = load_statement(db_path)
    lock_row(db_path, "r2", stmt["version"])
    stmt = load_statement(db_path)
    _delete_archives(db_path)
    init_db(db_path)
    data = load_archives(db_path)
    by_label = {r["row_label"]: r for r in data["records"]}
    rec1, rec2 = by_label["物理学院"], by_label["化学学院"]
    # 第一次锁定后底表曾更正：元金额未留存，公示数字保留
    assert rec1["base_status"] == "unavailable"
    assert all(i["amount_yuan"] is None for i in rec1["entries"])
    assert rec1["base_subtotal_yuan"] is None
    assert [i["value_wan"] for i in rec1["entries"]] == stmt["locks"]["r1"]["cells"]
    assert rec1["subtotal_wan"] == stmt["locks"]["r1"]["subtotal"]
    # 第二次锁定之后没有更正：可用现有底表补记
    assert rec2["base_status"] == "recorded"
    assert [i["amount_yuan"] for i in rec2["entries"]] == _base_row(stmt, "r2")


def test_backfill_is_idempotent_and_never_rewrites(client, db_path):
    """重启重复迁移不产生新档、不改旧档；之后的更正也不改旧档。"""
    stmt = load_statement(db_path)
    lock_row(db_path, "r1", stmt["version"])
    _delete_archives(db_path)
    init_db(db_path)
    first = load_archives(db_path)["records"]
    assert len(first) == 1
    init_db(db_path)
    init_db(db_path)
    assert load_archives(db_path)["records"] == first
    stmt = load_statement(db_path)
    _correct(db_path, stmt, {("r2", "c1"): 6000})
    init_db(db_path)
    assert load_archives(db_path)["records"] == first


def test_archive_page_lists_filters_and_marks_unavailable(client, db_path):
    stmt = load_statement(db_path)
    client.post("/lock/r1", data={"version": stmt["version"]})
    stmt = load_statement(db_path)
    _correct(db_path, stmt, {("r2", "c1"): 6000})
    stmt = load_statement(db_path)
    client.post("/lock/r2", data={"version": stmt["version"]})
    stmt = load_statement(db_path)
    v1 = stmt["locks"]["r1"]["statement_version"]

    html = client.get("/archive").get_data(as_text=True)
    assert "物理学院 · 版号" in html and "化学学院 · 版号" in html
    assert "设备费" in html  # 原科目
    assert "查看留档" in client.get("/statement").get_data(as_text=True)

    html = client.get(f"/archive?row_label=物理学院&version={v1}").get_data(as_text=True)
    assert "物理学院 · 版号" in html
    assert "化学学院 · 版号" not in html

    html = client.get("/archive?row_label=物理学院&version=999").get_data(as_text=True)
    assert "无符合条件的留档记录" in html

    html = client.get("/archive?version=abc").get_data(as_text=True)
    assert "版号须为非负整数" in html

    # 模拟旧库：第一次锁定后曾更正，页面显示「未留存」
    _delete_archives(db_path)
    init_db(db_path)
    html = client.get("/archive").get_data(as_text=True)
    assert "未留存" in html
