import csv
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile


ROOT = Path(__file__).resolve().parents[1]


with tempfile.TemporaryDirectory() as data_dir:
    legacy_db = sqlite3.connect(Path(data_dir) / "greennet.db")
    legacy_db.execute(
        "CREATE TABLE chat_group_members ("
        "group_id INTEGER NOT NULL, user_id INTEGER NOT NULL, joined_at TEXT NOT NULL, "
        "PRIMARY KEY (group_id, user_id))"
    )
    legacy_db.close()
    admin_password = secrets.token_urlsafe(18)
    user_password = secrets.token_urlsafe(18)
    os.environ.update({
        "GREENNET_DATA": data_dir,
        "GREENNET_ADMIN_PASSWORD": admin_password,
        "GREENNET_DEMO": "0",
        "GREENNET_REGISTRATION": "1",
    })

    spec = importlib.util.spec_from_file_location("greennet_smoke_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with closing(sqlite3.connect(Path(data_dir) / "greennet.db")) as migrated_db:
        assert "muted" in {row[1] for row in migrated_db.execute("PRAGMA table_info(chat_group_members)")}

    admin = module.app.test_client()
    creator = module.app.test_client()
    member_one = module.app.test_client()
    member_two = module.app.test_client()
    member_three = module.app.test_client()

    assert admin.post("/api/login", json={
        "username": "admin", "password": admin_password,
    }).status_code == 200

    rotated_admin_password = secrets.token_urlsafe(18)
    os.environ["GREENNET_ADMIN_PASSWORD"] = rotated_admin_password
    module.init_db()
    assert module.app.test_client().post("/api/login", json={
        "username": "admin", "password": admin_password,
    }).status_code == 401
    assert module.app.test_client().post("/api/login", json={
        "username": "admin", "password": rotated_admin_password,
    }).status_code == 200

    users = (
        (creator, "creator", "Ирина", "Орлова"),
        (member_one, "member-one", "Анна", "Петрова"),
        (member_two, "member-two", "Олег", "Смирнов"),
        (member_three, "member-three", "Мария", "Волкова"),
    )
    for client, username, first_name, last_name in users:
        response = client.post("/api/register", json={
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "password": user_password,
            "dept": "Проект 11",
            "accept_terms": True,
        })
        assert response.status_code == 200, response.get_json()

    state = creator.get("/api/state").get_json()
    ids = {user["name"]: user["id"] for user in state["roster"]}
    created = creator.post("/api/chat/groups", json={
        "name": "Координация",
        "description": "Общая рабочая комната",
        "member_ids": [ids["Анна Петрова"], ids["Олег Смирнов"]],
    })
    assert created.status_code == 200, created.get_json()

    group_id = creator.get("/api/state").get_json()["chat_groups"][0]["id"]
    message = "Групповая история синхронизируется"
    assert creator.post(f"/api/chat/groups/{group_id}/messages", json={
        "text": message,
    }).status_code == 200
    assert member_one.get("/api/state").get_json()["group_messages"][0]["text"] == message

    member_one_id = ids["Анна Петрова"]
    member_two_id = ids["Олег Смирнов"]
    member_three_id = ids["Мария Волкова"]
    creator_id = ids["Ирина Орлова"]

    assert member_one.post(f"/api/chat/groups/{group_id}/members", json={
        "member_ids": [member_three_id],
    }).status_code == 403
    added = creator.post(f"/api/chat/groups/{group_id}/members", json={
        "member_ids": [member_three_id],
    })
    assert added.status_code == 200, added.get_json()
    group = creator.get("/api/state").get_json()["chat_groups"][0]
    assert len(group["members"]) == 4
    assert all("muted" in member for member in group["members"])

    muted = creator.patch(f"/api/chat/groups/{group_id}/members/{member_one_id}", json={
        "muted": True,
    })
    assert muted.status_code == 200, muted.get_json()
    assert member_one.post(f"/api/chat/groups/{group_id}/messages", json={
        "text": "Это сообщение должно быть заблокировано",
    }).status_code == 403
    assert member_two.patch(f"/api/chat/groups/{group_id}/members/{member_one_id}", json={
        "muted": False,
    }).status_code == 403
    assert creator.patch(f"/api/chat/groups/{group_id}/members/{member_one_id}", json={
        "muted": False,
    }).status_code == 200
    assert member_one.post(f"/api/chat/groups/{group_id}/messages", json={
        "text": "После снятия mute отправка снова работает",
    }).status_code == 200

    assert creator.delete(f"/api/chat/groups/{group_id}/members/{member_three_id}").status_code == 200
    assert member_three.get("/api/state").get_json()["chat_groups"] == []
    assert member_three.post(f"/api/chat/groups/{group_id}/messages", json={
        "text": "Удалённый участник не может писать",
    }).status_code == 404
    assert creator.delete(f"/api/chat/groups/{group_id}/members/{creator_id}").status_code == 400
    assert creator.delete(f"/api/chat/groups/{group_id}/members/{member_two_id}").status_code == 400

    rejected = creator.post("/api/chat/groups", json={
        "name": "Личный диалог",
        "member_ids": [ids["Анна Петрова"]],
    })
    assert rejected.status_code == 400

    csv_response = admin.get("/api/users.csv")
    assert csv_response.status_code == 200
    csv_response.close()
    with open(Path(data_dir) / "users.csv", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert {"username", "first_name", "last_name", "role", "dept"}.issubset(rows[0])
    assert not any("password" in column.lower() for column in rows[0])

print("Application smoke test passed")
