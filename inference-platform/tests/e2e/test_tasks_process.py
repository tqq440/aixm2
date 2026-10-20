"""真实 HTTP 上传和重启持久化验收；PCM 仅检验上传，不冒充模型转写。"""

import hashlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.database import build_database_url


@pytest.mark.e2e
def test_real_http_upload_survives_restart_and_replays_original_task(tmp_path):
    isolated_project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "backend", isolated_project / "backend", ignore=shutil.ignore_patterns("__pycache__"))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    settings = AppSettings(
        database_path=tmp_path / "db/tasks.sqlite3",
        log_dir=tmp_path / "logs",
        upload_dir=tmp_path / "uploads",
        asr_model_dir=tmp_path / "absent-model",
    )
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("INFERENCE_")}
    environment.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(isolated_project),
        "INFERENCE_HOST": "127.0.0.1",
        "INFERENCE_PORT": str(port),
        "INFERENCE_DATABASE_PATH": str(settings.database_path),
        "INFERENCE_LOG_DIR": str(settings.log_dir),
        "INFERENCE_UPLOAD_DIR": str(settings.upload_dir),
        "INFERENCE_ASR_MODEL_DIR": str(settings.asr_model_dir),
    })
    output_path = tmp_path / "process-output.log"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base_url = f"http://127.0.0.1:{port}"
    report = {"startedAt": datetime.now(timezone.utc).isoformat(), "baseUrl": base_url, "processes": []}

    def request(path, *, data=None, headers=None):
        request_headers = {"X-Request-ID": "part3-http-restart"}
        request_headers.update(headers or {})
        http_request = urllib.request.Request(base_url + path, data=data, headers=request_headers)
        try:
            response = opener.open(http_request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = json.loads(response.read())
            assert response.headers["X-Request-ID"] == payload["requestId"]
            return response.status, payload

    def start_process(output):
        process = subprocess.Popen(
            [sys.executable, "-m", "backend.modules.api.run"],
            cwd=isolated_project,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        report["processes"].append({"pid": process.pid})
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    pytest.fail("API exited before ready: " + output_path.read_text(encoding="utf-8", errors="replace"))
                try:
                    status, payload = request("/api/health")
                    assert status == 200 and payload["data"]["status"] == "ok"
                    return process
                except (urllib.error.URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise

    def stop_process(process):
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        exit_code = process.wait(timeout=10)
        report["processes"][-1]["exitCode"] = exit_code
        assert exit_code == 0
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        report["processes"][-1]["portClosed"] = True

    audio_buffer = io.BytesIO()
    with wave.open(audio_buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 2000)
    audio_bytes = audio_buffer.getvalue()
    boundary = "part3-real-http-audio-boundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"modelId\"\r\n\r\nwhisper-small\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nzh\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"e2e-sample.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n"
    ).encode("utf-8") + audio_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    upload_headers = {"Idempotency-Key": "part3-real-http-upload", "Content-Type": f"multipart/form-data; boundary={boundary}"}
    process = None
    with output_path.open("w", encoding="utf-8") as output:
        try:
            process = start_process(output)
            model_status, models = request("/api/models")
            assert model_status == 200 and models["data"]["items"][0]["modelId"] == "whisper-small"
            created_status, created = request("/api/tasks/asr", data=body, headers=upload_headers)
            assert created_status == 201, created
            assert created["data"]["replayed"] is False
            task = created["data"]["task"]
            assert task["status"] == "created"
            assert task["input"]["sha256"] == hashlib.sha256(audio_bytes).hexdigest()
            report.update({"uploadHttpStatus": created_status, "task": task, "testAudio": "generated 0.125 second PCM; no transcription"})
            stop_process(process)
            process = start_process(output)
            detail_status, detail = request(f"/api/tasks/{task['taskId']}")
            assert detail_status == 200 and detail["data"] == task
            list_status, listing = request("/api/tasks?page=1&pageSize=20")
            assert list_status == 200
            assert listing["data"] == {"items": [task], "total": 1, "page": 1, "pageSize": 20}
            replay_status, replay = request("/api/tasks/asr", data=body, headers=upload_headers)
            assert replay_status == 200 and replay["data"] == {"task": task, "replayed": True}
            report.update({"restartDetailHttpStatus": detail_status, "restartListHttpStatus": list_status, "restartReplayHttpStatus": replay_status})
            stop_process(process)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    engine = create_engine(build_database_url(settings))
    try:
        with engine.connect() as connection:
            counts = {
                table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in ("tasks", "idempotency_records", "dispatch_events")
            }
        assert counts == {"tasks": 1, "idempotency_records": 1, "dispatch_events": 1}
        report["databaseCountsAfterRestart"] = counts
    finally:
        engine.dispose()
    saved_files = list(settings.upload_dir.rglob("*.audio"))
    assert len(saved_files) == 1 and saved_files[0].read_bytes() == audio_bytes
    assert not settings.asr_model_dir.exists()
    report.update({"modelLoaded": False, "finishedAt": datetime.now(timezone.utc).isoformat()})
    (tmp_path / "part3-http-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
