import httpx
import pytest

from labfoundry.backends.gns3.client import GNS3Client


@pytest.mark.asyncio
async def test_snapshot_controller_routes() -> None:
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/snapshots") and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "snapshot_id": "snap-id",
                    "project_id": "project-id",
                    "name": "clean",
                    "created_at": 1,
                },
            )
        if request.url.path.endswith("/snapshots") and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "snapshot_id": "snap-id",
                        "project_id": "project-id",
                        "name": "clean",
                        "created_at": 1,
                    }
                ],
            )
        if request.url.path.endswith("/restore"):
            return httpx.Response(
                201,
                json={"project_id": "project-id", "name": "p", "status": "opened"},
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(request.url)

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        snap = await client.create_snapshot("project-id", "clean")
        snaps = await client.list_snapshots("project-id")
        project = await client.restore_snapshot("project-id", "snap-id")
        await client.delete_snapshot("project-id", "snap-id")
    finally:
        await client.aclose()

    assert snap.name == "clean"
    assert snaps[0].snapshot_id == "snap-id"
    assert project.project_id == "project-id"
    assert calls == [
        ("POST", "/v2/projects/project-id/snapshots"),
        ("GET", "/v2/projects/project-id/snapshots"),
        ("POST", "/v2/projects/project-id/snapshots/snap-id/restore"),
        ("DELETE", "/v2/projects/project-id/snapshots/snap-id"),
    ]
