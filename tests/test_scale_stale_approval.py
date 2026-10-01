"""Regression coverage for Deployment scale approval concurrency."""

from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from tools import kubernetes_write


class FakeScaleApi:
    """Small API-server model with resourceVersion conflict semantics."""

    def __init__(self):
        self.replicas = 1
        self.resource_version = "100"
        self.patch_calls = []
        self.replace_calls = []
        self.conflict_on_replace = False

    def external_scale(self, replicas: int) -> None:
        self.replicas = replicas
        self.resource_version = str(int(self.resource_version) + 1)

    def patch_namespaced_deployment_scale(self, name, namespace, body):
        self.patch_calls.append((name, namespace, body))
        self.replicas = body["spec"]["replicas"]
        self.resource_version = str(int(self.resource_version) + 1)

    def read_namespaced_deployment_scale(self, name, namespace):
        return SimpleNamespace(
            metadata=SimpleNamespace(resource_version=self.resource_version),
            spec=SimpleNamespace(replicas=self.replicas),
        )

    def replace_namespaced_deployment_scale(self, name, namespace, body):
        self.replace_calls.append((name, namespace, body))
        if self.conflict_on_replace:
            self.external_scale(5)
            raise ApiException(status=409, reason="Conflict")
        if body.metadata.resource_version != self.resource_version:
            raise ApiException(status=409, reason="Conflict")
        self.replicas = body.spec.replicas
        self.resource_version = str(int(self.resource_version) + 1)


def test_stale_scale_approval_does_not_overwrite_external_change(monkeypatch):
    api = FakeScaleApi()
    monkeypatch.setattr(kubernetes_write, "apps_v1", lambda: api)

    proposal = {
        "deployment_name": "api",
        "namespace": "production",
        "approved_current_replicas": 1,
        "replicas": 2,
        "expected_resource_version": "100",
    }

    # The proposal is paused. Another actor changes 1 -> 5 before approval.
    api.external_scale(5)
    result = kubernetes_write.kubectl_scale_deployment.invoke(proposal)

    assert api.replicas == 5
    assert api.patch_calls == []
    assert api.replace_calls == []
    assert "stale" in result.lower()
    assert "409 conflict" not in result.lower()
    assert "was not applied" in result.lower()


def test_current_scale_approval_uses_conditional_scale_update(monkeypatch):
    api = FakeScaleApi()
    monkeypatch.setattr(kubernetes_write, "apps_v1", lambda: api)

    result = kubernetes_write.kubectl_scale_deployment.invoke(
        {
            "deployment_name": "api",
            "namespace": "production",
            "approved_current_replicas": 1,
            "replicas": 2,
            "expected_resource_version": "100",
        }
    )

    assert api.replicas == 2
    assert api.patch_calls == []
    assert len(api.replace_calls) == 1
    assert "1 to 2" in result


def test_conditional_update_closes_race_after_preflight_read(monkeypatch):
    api = FakeScaleApi()
    api.conflict_on_replace = True
    monkeypatch.setattr(kubernetes_write, "apps_v1", lambda: api)

    result = kubernetes_write.kubectl_scale_deployment.invoke(
        {
            "deployment_name": "api",
            "namespace": "production",
            "approved_current_replicas": 1,
            "replicas": 2,
            "expected_resource_version": "100",
        }
    )

    assert api.replicas == 5
    assert len(api.replace_calls) == 1
    assert "stale" in result.lower()
    assert "409 conflict" in result.lower()
