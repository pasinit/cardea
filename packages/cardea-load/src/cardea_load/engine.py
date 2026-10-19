"""Vegeta owns HTTP pacing and histograms; this adapter provides authenticated targets."""

from __future__ import annotations

import base64
import collections
import contextlib
import gzip
import json
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from cardea_load.lab import Lab, task
from cardea_load.metrics import classify, save


def attack(
    lab: Lab,
    vegeta: Path,
    output: Path,
    name: str,
    rate: int,
    seconds: int,
    profile: dict[str, Any],
    fault: bool = False,
    denial: bool = False,
    intervention: Any = None,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    command = [
        str(vegeta),
        "attack",
        "-format=json",
        "-lazy",
        "-unix-socket=" + lab.info["socket"],
        f"-rate={rate}/s",
        f"-duration={seconds}s",
        "-workers=4",
        f"-max-workers={profile['max_workers']}",
        f"-timeout={profile['request_timeout_seconds']}s",
        "-max-body=262144",
    ]
    stopped = threading.Event()
    samples: list[dict[str, Any]] = []
    feeder_errors: list[str] = []
    action_result: dict[str, Any] = {}
    token_hashes: set[str] = set()
    from cardea_load.metrics import fingerprint

    def feed(process: subprocess.Popen[bytes]) -> None:
        assert process.stdin is not None
        try:
            index = 0
            while not stopped.is_set():
                credential = lab.credential()
                token_hashes.add(fingerprint(credential))
                target = {
                    "method": "POST",
                    "url": "http://localhost/v0.2/tasks",
                    "header": {
                        "Content-Type": ["application/json"],
                        "Authorization": ["Bearer " + credential],
                    },
                    "body": base64.b64encode(
                        json.dumps(task(f"{name}-{index}", profile["payload_bytes"])).encode()
                    ).decode(),
                }
                process.stdin.write(json.dumps(target).encode() + b"\n")
                process.stdin.flush()
                index += 1
        except BrokenPipeError:
            pass
        except Exception as error:
            feeder_errors.append(type(error).__name__)
        finally:
            with contextlib.suppress(BrokenPipeError):
                process.stdin.close()

    def sample() -> None:
        process = psutil.Process(lab.info["pid"])
        while not stopped.is_set():
            try:
                if process.pid != lab.info["pid"]:
                    process = psutil.Process(lab.info["pid"])
                db = Path(lab.info["database"])
                samples.append(
                    {
                        "elapsed": time.monotonic() - started,
                        "rss_bytes": process.memory_info().rss,
                        "cpu_seconds": sum(process.cpu_times()[:2]),
                        "fds": process.num_fds(),
                        "db_bytes": db.stat().st_size if db.exists() else 0,
                        "wal_bytes": Path(str(db) + "-wal").stat().st_size
                        if Path(str(db) + "-wal").exists()
                        else 0,
                    }
                )
            except (psutil.NoSuchProcess, FileNotFoundError):
                pass
            stopped.wait(0.5)

    def intervene() -> None:
        if intervention:
            try:
                action_result.update(intervention(lab))
            except Exception as error:
                action_result["error"] = type(error).__name__

    started = time.monotonic()
    with (output / "requests.bin").open("wb") as raw, (output / "engine.log").open("wb") as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=raw, stderr=log)
        threads = [
            threading.Thread(target=feed, args=(process,), daemon=True),
            threading.Thread(target=sample, daemon=True),
            threading.Thread(target=intervene, daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            code = process.wait(timeout=seconds + 150)
        finally:
            stopped.set()
            if process.poll() is None:
                process.kill()
                process.wait()
            for thread in threads:
                thread.join(timeout=125)
    duration = time.monotonic() - started
    if code:
        raise RuntimeError(f"Vegeta exited {code}; inspect {output / 'engine.log'}")
    metrics = json.loads(
        subprocess.check_output(
            [str(vegeta), "report", "-type=json", str(output / "requests.bin")], timeout=30
        )
    )
    save(output / "vegeta.json", metrics)
    save(output / "resources.json", samples)
    counts: collections.Counter[str] = collections.Counter()
    acks: set[str] = set()
    malformed = 0
    accepted_latencies: list[float] = []
    decoder = subprocess.Popen(
        [str(vegeta), "encode", "-to=json", str(output / "requests.bin")],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert decoder.stdout is not None
    with gzip.open(output / "requests.jsonl.gz", "wt") as normalized:
        for line in decoder.stdout:
            row = json.loads(line)
            status = int(row["code"])
            category = classify(status, deliberate_denial=denial, fault=fault)
            counts[category] += 1
            task_id = None
            error_code = None
            try:
                body = json.loads(base64.b64decode(row["body"]))
                if category == "accepted":
                    task_id = body["task"]["task_id"]
                    acks.add(task_id)
                    accepted_latencies.append(row["latency"] / 1e6)
                else:
                    error_code = body.get("error", {}).get("code")
            except (ValueError, KeyError, TypeError, AttributeError):
                if category == "accepted":
                    malformed += 1
            normalized.write(
                json.dumps(
                    {
                        "seq": row["seq"],
                        "timestamp": row["timestamp"],
                        "latency_ns": row["latency"],
                        "status": status,
                        "category": category,
                        "task_id": task_id,
                        "error_code": error_code,
                    }
                )
                + "\n"
            )
    if decoder.wait(timeout=30):
        raise RuntimeError("Vegeta result decoder failed")
    # Binary results contain response data only. Compress losslessly for independent reanalysis.
    with (
        (output / "requests.bin").open("rb") as source,
        gzip.open(output / "requests.bin.gz", "wb") as dest,
    ):
        import shutil

        shutil.copyfileobj(source, dest)
    (output / "requests.bin").unlink()
    return {
        "accepted_p95_ms": (
            statistics.quantiles(accepted_latencies, n=100, method="inclusive")[94]
            if len(accepted_latencies) > 1
            else (accepted_latencies[0] if accepted_latencies else None)
        ),
        "requests_per_configured_second": metrics["requests"] / seconds,
        "rate": rate,
        "seconds": seconds,
        "http_seconds": duration,
        "requests": metrics["requests"],
        "p95_ms": metrics["latencies"]["95th"] / 1e6,
        "p99_ms": metrics["latencies"]["99th"] / 1e6,
        "achieved_request_rps": metrics["rate"],
        "http_success_rps": metrics["throughput"],
        **{
            k: counts[k]
            for k in (
                "accepted",
                "capacity_rejected",
                "expected_denial",
                "fault_unavailable",
                "unexpected_error",
            )
        },
        "acknowledged": sorted(acks),
        "malformed_successes": malformed,
        "feeder_errors": feeder_errors,
        "intervention": action_result,
        "distinct_credentials_used": len(token_hashes),
        "peak_rss_bytes": max((s["rss_bytes"] for s in samples), default=0),
        "peak_fds": max((s["fds"] for s in samples), default=0),
        "peak_wal_bytes": max((s["wal_bytes"] for s in samples), default=0),
    }
