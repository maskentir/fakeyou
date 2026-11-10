from typing import Any

class V1JobStatus:
    active: int | None

class V1ObjectMeta:
    labels: dict[str, str] | None

class V1Job:
    metadata: V1ObjectMeta
    status: V1JobStatus | None

class V1JobList:
    items: list[V1Job]

class BatchV1Api:
    def list_namespaced_job(
        self, namespace: str, *, label_selector: str | None = ...
    ) -> V1JobList: ...
    def create_namespaced_job(self, namespace: str, body: dict[str, Any]) -> V1Job: ...
