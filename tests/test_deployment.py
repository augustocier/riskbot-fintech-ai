import importlib.util

import httpx
import pytest
import yaml

from main import ROOT


def deployment_module():
    spec = importlib.util.spec_from_file_location("riskbot_deployment", ROOT / "scripts/deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_preserves_hook_key_and_checks_revision_and_api(monkeypatch):
    module = deployment_module()
    revision = "a" * 40
    monkeypatch.setenv("RENDER_DEPLOY_HOOK", "https://api.render.com/deploy/srv-demo?key=placeholder")
    monkeypatch.setenv("RENDER_API_URL", "https://riskbot-example.onrender.com")
    monkeypatch.setenv("GITHUB_SHA", revision)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "api.render.com":
            assert request.url.params["key"] == "placeholder"
            assert request.url.params["ref"] == revision
            return httpx.Response(200, json={"id": "test-deploy"})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "revision": revision})
        if b"Tier B" in request.content:
            return httpx.Response(200, json={"resultado": "La tasa es 7,5%."})
        return httpx.Response(403, json={"detail": "Petición denegada por políticas de seguridad"})

    original = httpx.Client
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), trust_env=False, **kwargs
    ))
    module.deploy()
    assert [str(c.url.path) for c in calls] == ["/deploy/srv-demo", "/health", "/inferencia", "/inferencia"]


def test_invalid_deploy_target_is_rejected_before_network(monkeypatch):
    module = deployment_module()
    monkeypatch.setenv("RENDER_DEPLOY_HOOK", "https://other.invalid/deploy?key=placeholder")
    monkeypatch.setenv("RENDER_API_URL", "https://riskbot-example.onrender.com")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    with pytest.raises(ValueError):
        module.deploy()


def test_workflow_requires_tests_and_evaluation_before_deploy():
    workflow = yaml.load((ROOT / ".github/workflows/cicd.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["branches"] == ["main"]
    jobs = workflow["jobs"]
    assert jobs["evaluate"]["needs"] == "tests"
    assert jobs["deploy"]["needs"] == "evaluate"
    assert "if" not in jobs["deploy"]  # No always() que permita saltarse el gate.
    config = yaml.load((ROOT / "render.yaml").read_text(), Loader=yaml.BaseLoader)
    assert config["services"][0]["autoDeployTrigger"] == "off"
