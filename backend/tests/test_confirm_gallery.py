"""Upload + gallery listing + file serving from local disk."""

TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.\' \",#\x1c\x1c(7),01444\x1f\'9=82<.342"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x7f\xff\xd9"
)


def test_upload_list_and_fetch(client, env_local):
    upload = client.post(
        "/api/uploads",
        files=[("files", ("shot.jpg", TINY_JPEG, "image/jpeg"))],
    )
    assert upload.status_code == 200, upload.text
    key = upload.json()["saved"][0]["key"]
    url = upload.json()["saved"][0]["url"]

    photos = client.get("/api/photos")
    assert photos.status_code == 200
    body = photos.json()
    assert body["total"] == 1
    assert body["items"][0]["key"] == key
    assert body["items"][0]["url"] == url

    file_res = client.get(url)
    assert file_res.status_code == 200
    assert file_res.content == TINY_JPEG
    assert "image/jpeg" in file_res.headers.get("content-type", "")

    assert (env_local["storage"] / key).is_file()


def test_serve_rejects_path_traversal(client):
    res = client.get("/api/files/uploads/../secret.jpg")
    assert res.status_code in (400, 404)
