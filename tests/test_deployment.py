"""Deployment acceptance. Docker test is explicit and uses an isolated volume."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from time import monotonic, sleep
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from deployment.mock_backend import create_smoke_app


ROOT = Path(__file__).resolve().parents[1]


def wait_for(check, timeout=15):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = check()
        if result:
            return result
        sleep(.1)
    raise AssertionError("Deployment state did not reach the expected value.")


def assert_chain(graph):
    evidence = {item["evidence_id"]: item for item in graph["evidence"]}
    sources = {item["source_id"] for item in graph["sources"]}
    assert graph["claims"] and graph["verification_results"] and graph["relevance_results"]
    for claim in graph["claims"]:
        assert claim["evidence_ids"]
        assert all(evidence[key]["source_id"] in sources for key in claim["evidence_ids"])


def test_offline_deployment_fixture_keeps_real_report_and_evidence_across_restart(tmp_path, monkeypatch):
    # Fail loudly if the acceptance fixture accidentally creates a real model.
    def forbidden(*args, **kwargs):
        raise AssertionError("Deployment fixture must never call a provider.")
    monkeypatch.setattr("app.research.orchestrator.create_llm", forbidden)
    cfg = Settings(_env_file=None, research_db_path=str(tmp_path / "deployment.db"))
    with TestClient(create_smoke_app(settings=cfg)) as client:
        created = client.post("/api/runs", json={"question": "deployment acceptance"})
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        wait_for(lambda: client.get(f"/api/runs/{run_id}").json()["status"] == "COMPLETED")
        report = client.get(f"/api/runs/{run_id}/report").json()
        graph = client.get(f"/api/runs/{run_id}/evidence").json()
        assert report["references"] and "[1]" in report["markdown"]
        assert_chain(graph)
        stream = client.get(f"/api/runs/{run_id}/events")
        assert "event: run_completed" in stream.text
    with TestClient(create_smoke_app(settings=cfg)) as restarted:
        assert restarted.get(f"/api/runs/{run_id}/report").json() == report
        assert restarted.get(f"/api/runs/{run_id}/evidence").json() == graph
        assert restarted.post(f"/api/runs/{run_id}/resume").status_code == 409


def test_docker_start_stream_restart_and_checkpoint_resume(tmp_path):
    if os.environ.get("RUN_DOCKER_TESTS") != "1":
        pytest.skip("Opt-in: RUN_DOCKER_TESTS=1 requires Docker Engine and Compose v2+.")
    docker = shutil.which("docker")
    assert docker, "Docker acceptance requested but docker is not installed."
    subprocess.run([docker, "info"], check=True, capture_output=True, timeout=30)
    project = "research-test-" + uuid4().hex[:12]
    sentinel = "offline-fixture-key-" + uuid4().hex
    env_file = tmp_path / "fixture.env"
    env_file.write_text(f"MODEL_API_KEY={sentinel}\nMODEL_NAME=offline-fixture\nMODEL_BASE_URL=https://example.invalid/v1\n"
                        "TAVILY_API_KEY=unused-offline-key\nAPI_SSE_HEARTBEAT_SECONDS=0.2\nAPI_SSE_STATE_CHECK_SECONDS=0.2\n", encoding="utf-8")
    override = tmp_path / "smoke.json"
    override.write_text(json.dumps({"services": {
        "backend": {"command": ["python", "-m", "uvicorn", "mock_backend:app", "--app-dir", "/smoke",
                                 "--host", "0.0.0.0", "--port", "8000", "--workers", "1"],
                    "volumes": [{"type": "bind", "source": str(ROOT / "tests/deployment/mock_backend.py"),
                                 "target": "/smoke/mock_backend.py", "read_only": True}]},
    }}), encoding="utf-8")
    environment = {**os.environ, "RESEARCH_ENV_FILE": str(env_file), "WEB_PORT": "0"}
    command = [docker, "compose", "-p", project, "-f", str(ROOT / "docker-compose.yml"), "-f", str(override)]

    def compose(*args, timeout=180):
        return subprocess.run([*command, *args], cwd=ROOT, env=environment, check=True,
                              capture_output=True, text=True, timeout=timeout).stdout

    try:
        compose("config", "--quiet")  # Never print an interpolated env/secret.
        compose("up", "-d", "--build", "--wait", timeout=1200)
        address = compose("port", "frontend", "8080").strip().splitlines()[0]
        base = "http://" + address
        with httpx.Client(base_url=base, timeout=15) as client:
            def healthy():
                try:
                    return client.get("/api/health").status_code == 200
                except httpx.HTTPError:
                    return False
            wait_for(healthy, timeout=60)
            assert client.get("/docs").status_code == client.get("/openapi.json").status_code == 200
            index = client.get("/").text
            assert '<div id="root">' in index
            for path in re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', index):
                assert sentinel not in client.get(path).text
            assert client.get("/api/runs/unknown").status_code == 404
            created = client.post("/api/runs", json={"question": "deployment acceptance"})
            assert created.status_code == 202
            run_id = created.json()["run_id"]
            events = []
            with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
                assert stream.status_code == 200 and "text/event-stream" in stream.headers["content-type"]
                for line in stream.iter_lines():
                    if line.startswith("event: "):
                        events.append(line[7:])
            assert {"task_started", "task_completed", "run_completed"} <= set(events)
            report = client.get(f"/api/runs/{run_id}/report").json()
            graph = client.get(f"/api/runs/{run_id}/evidence").json()
            assert report["references"] and "[1]" in report["markdown"]
            assert_chain(graph)
            assert client.get(f"/runs/{run_id}/report").text == index

            paused = client.post("/api/runs", json={"question": "deployment pause"}).json()["run_id"]
            def at_analysis():
                run = client.get(f"/api/runs/{paused}").json()
                return run.get("plan") and all(task["status"] == "completed" for task in run["plan"]["tasks"][:2])
            wait_for(at_analysis)
            wait_for(lambda: "True" in compose("exec", "-T", "backend", "python", "-c",
                "import json; from pathlib import Path; print(any(json.loads(s)['goal']=='deployment pause' and "
                "json.loads(s)['task_id']=='A' for s in Path('/data/fixture-executions.jsonl').read_text().splitlines()))"))
            compose("kill", "-s", "SIGKILL", "backend")
            compose("up", "-d", "--wait", "backend")
            wait_for(healthy, timeout=60)  # Includes Nginx DNS re-resolution.
            recovered = client.get(f"/api/runs/{paused}").json()
            assert recovered["status"] == "INTERRUPTED" and recovered["resumable"]
            assert client.get(f"/api/runs/{run_id}/report").json() == report
            compose("exec", "-T", "backend", "python", "-c", "from pathlib import Path; Path('/data/fixture-release').touch()")
            assert client.post(f"/api/runs/{paused}/resume").status_code == 202
            wait_for(lambda: client.get(f"/api/runs/{paused}").json()["status"] == "COMPLETED")
            calls = compose("exec", "-T", "backend", "python", "-c", "from pathlib import Path; print(Path('/data/fixture-executions.jsonl').read_text())")
            counts = Counter(json.loads(line)["task_id"] for line in calls.splitlines() if line and json.loads(line)["goal"] == "deployment pause")
            assert counts["D1"] == counts["D2"] == counts["S"] == 1
            assert counts["A"] == 2

            compose("up", "-d", "--force-recreate", "--wait", "backend")
            compose("restart", "frontend")
            wait_for(healthy, timeout=60)
            assert client.get(f"/api/runs/{run_id}/report").json() == report
            assert client.get(f"/api/runs/{run_id}/evidence").json() == graph
            assert client.get(f"/api/runs/{paused}").json()["status"] == "COMPLETED"
            assert client.post(f"/api/runs/{run_id}/resume").status_code == 409
            assert run_id in compose("exec", "-T", "backend", "python", "main.py", "--list-runs")
            compose("exec", "-T", "backend", "python", "-c", "from pathlib import Path; assert not Path('/app/.env').exists()")
    finally:
        # Only this test's random project/volume; never the user's deployment.
        subprocess.run([*command, "down", "--volumes", "--remove-orphans"], cwd=ROOT, env=environment,
                       capture_output=True, timeout=120)
