import csv
import importlib.util
import os
from pathlib import Path
import secrets
import tempfile


ROOT = Path(__file__).resolve().parents[1]


with tempfile.TemporaryDirectory() as data_dir:
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

    admin = module.app.test_client()
    creator = module.app.test_client()
    member_one = module.app.test_client()
    member_two = module.app.test_client()

    assert admin.post("/api/login", json={
        "username": "admin", "password": admin_password,
    }).status_code == 200

    users = (
        (creator, "creator", "Ирина", "Орлова"),
        (member_one, "member-one", "Анна", "Петрова"),
        (member_two, "member-two", "Олег", "Смирнов"),
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
