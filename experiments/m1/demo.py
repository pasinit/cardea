"""Disposable authenticated headless kernel demonstration; no model or UI."""
import json

from cardea.service.models import Signal, TaskRequest
from harness import SUPERVISOR, OUTSIDER, ServiceLab, wait_for
from provider_lab import Lab


def main():
    provider = Lab()
    service = None
    try:
        provider.start()
        provider.register(SUPERVISOR)
        provider.register(OUTSIDER)
        wait_for(lambda: provider.token(SUPERVISOR))
        service = ServiceLab(provider)
        first = service.start()
        task_id = service.client.submit(TaskRequest(request_id="demo", agent_id="agent-a",
            namespace="test", objective="Publish and verify a durable JSON artifact",
            payload={"message": "Hello from Cardea M1"}, wait_for_event=True)).task.task_id
        service.wait_state(task_id, "waiting")
        receipt = service.client.request("GET", f"/tasks/{task_id}/receipt")
        service.stop(first)
        service.start()
        service.client.control(task_id, Signal(request_id="resume-demo"))
        service.wait_state(task_id, "succeeded")
        assert service.client.request("GET", f"/tasks/{task_id}/receipt") == receipt
        print(json.dumps({"state": "succeeded", "authenticated_supervisor": service.client.inspect(task_id).supervisor_id,
            "receipt_preserved_across_restart": True, "output": service.client.request("GET", f"/tasks/{task_id}/output"),
            "model_calls": 0}, indent=2))
    finally:
        if service:
            service.close()
        provider.close()


if __name__ == "__main__":
    main()
