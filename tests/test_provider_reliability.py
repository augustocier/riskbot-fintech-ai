import asyncio
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from eval import request_diagnostic
from main import (
    FACTS, ROOT, AnswerPlan, GeminiBackend, InferenceRequest,
    ProviderPacer, SecurityVerdict, create_app,
)


def install_mock_sdk(monkeypatch, handler):
    from google import genai
    import httpx2

    original = genai.Client
    retry_settings = []

    def factory(**kwargs):
        options = kwargs["http_options"]
        retry_settings.append(options.retry_options.model_dump())
        options.retry_options.initial_delay = 0.001
        options.retry_options.max_delay = 0.001
        options.retry_options.exp_base = 1
        options.retry_options.jitter = 0
        options.client_args = {"transport": httpx2.MockTransport(handler), "trust_env": False}
        options.async_client_args = {"transport": httpx2.MockTransport(handler), "trust_env": False}
        return original(**kwargs)

    monkeypatch.setattr(genai, "Client", factory)
    return retry_settings


@pytest.mark.parametrize("transient_status", [429, 503])
def test_sdk_retries_a_transient_failure_then_returns_real_response(monkeypatch, transient_status):
    import httpx2

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(transient_status, json={"error": {
                "code": transient_status, "message": "temporary failure",
                "status": "RESOURCE_EXHAUSTED" if transient_status == 429 else "UNAVAILABLE",
            }})
        return httpx2.Response(200, json={"candidates": [{
            "content": {"role": "model", "parts": [{"text": json.dumps({
                "decision": "allow", "category": "policy_query"
            })}]}, "finishReason": "STOP",
        }]})

    retry_settings = install_mock_sdk(monkeypatch, handler)

    async def run():
        backend = GeminiBackend("placeholder-not-a-key", 2, pacer=ProviderPacer(0))
        try:
            result = await backend.generate(
                model="gemini-3.5-flash-lite", system="system", user="user", schema=SecurityVerdict
            )
            assert result.decision == "allow"
        finally:
            await backend.close()

    asyncio.run(run())
    assert len(calls) == 2
    assert retry_settings[0]["attempts"] == 3
    assert 429 in retry_settings[0]["http_status_codes"]


@pytest.mark.parametrize("status,expected_attempts", [(503, 3), (403, 1)])
def test_retry_is_bounded_and_auth_errors_are_not_retried(monkeypatch, status, expected_attempts):
    import httpx2
    from google.genai.errors import APIError

    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(status, json={"error": {
            "code": status, "message": "provider error", "status": "UNAVAILABLE"
        }})

    install_mock_sdk(monkeypatch, handler)

    async def run():
        backend = GeminiBackend("placeholder-not-a-key", 2, pacer=ProviderPacer(0))
        try:
            with pytest.raises(APIError) as error:
                await backend.generate(
                    model="gemini-3.5-flash-lite", system="system", user="user", schema=SecurityVerdict
                )
            assert error.value.code == status
        finally:
            await backend.close()

    asyncio.run(run())
    assert len(calls) == expected_attempts


def test_pacer_spaces_calls_even_when_the_client_role_changes():
    now, sleeps = [0.0], []

    async def fake_sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    pacer = ProviderPacer(6, clock=lambda: now[0], sleep=fake_sleep)

    async def run():
        await pacer.wait()  # Juez
        now[0] += 2        # Tiempo de respuesta del proveedor
        await pacer.wait()  # Asistente
        now[0] += 1
        await pacer.wait()  # Evaluador

    asyncio.run(run())
    assert sleeps == [4.0, 5.0]
    assert now[0] == 12


def test_ci_spacing_is_configurable(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("GEMINI_MIN_INTERVAL_SECONDS", raising=False)
    assert ProviderPacer.from_env().interval == 6
    monkeypatch.setenv("GEMINI_MIN_INTERVAL_SECONDS", "12")
    assert ProviderPacer.from_env().interval == 12


def test_every_security_probe_reaches_the_semantic_filter_without_input_errors():
    cases = json.loads((ROOT / "eval_data/eval_security.json").read_text())
    for case in cases:
        assert InferenceRequest(prompt=case["prompt"]).prompt == case["prompt"]


def test_response_schema_constrains_the_model_to_trusted_fact_ids():
    choices = AnswerPlan.model_json_schema()["properties"]["fact_ids"]["items"]["enum"]
    assert set(choices) == set(FACTS)
    assert "cumplimiento.2" in choices
    assert "tasas.tier_D_inventado" not in choices


@pytest.mark.parametrize("phase", ["judge", "answer"])
def test_provider_diagnostic_is_preserved_privately_without_exposing_the_error(phase, caplog):
    class ProviderError(RuntimeError):
        code = 429

    class Backend:
        calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            if phase == "answer" and self.calls == 1:
                return SecurityVerdict(decision="allow", category="policy_query")
            raise ProviderError("SECRET_DO_NOT_EXPOSE")

    with TestClient(create_app(Backend())) as client:
        response = client.post("/inferencia", json={"prompt": "¿Cuál es la tasa de Tier B?"})
        assert response.status_code == 503
        assert request_diagnostic(client, response) == {
            "stage": phase, "kind": "provider_error", "provider_status": 429,
        }
    assert response.json() == {"detail": "Servicio temporalmente no disponible"}
    assert "SECRET_DO_NOT_EXPOSE" not in response.text + caplog.text


def test_requesting_the_exception_procedure_can_be_answered_without_execution():
    class Backend:
        async def generate(self, **kwargs):
            if kwargs["schema"] is SecurityVerdict:
                return SecurityVerdict(decision="allow", category="policy_query")
            return AnswerPlan(status="answer", fact_ids=["cumplimiento.2"])

    with TestClient(create_app(Backend())) as client:
        response = client.post("/inferencia", json={
            "prompt": "¿Qué autorización necesito para tramitar una excepción de política?"
        })
    assert response.status_code == 200
    assert "firma digital" in response.json()["resultado"]
    assert "Director de Riesgos" in response.json()["resultado"]


def test_full_evaluation_shares_one_pacer_and_closes_both_clients(monkeypatch, tmp_path):
    api_module = importlib.import_module("main")
    eval_module = importlib.import_module("eval")
    clients = []

    class Backend:
        def __init__(self, *args, pacer=None):
            self.pacer = pacer if pacer is not None else ProviderPacer(0)
            self.closed = False
            clients.append(self)

        async def close(self):
            self.closed = True

    metrics = {
        "security_recall": 1.0, "false_positive_rate": 0.0,
        "answer_relevancy": 1.0, "min_answer_relevancy": 1.0,
        "groundedness": 1.0, "functional_accuracy": 1.0, "availability_errors": 0.0,
    }
    monkeypatch.setenv("GEMINI_API_KEY", "placeholder-not-a-key")
    monkeypatch.setattr(api_module, "GeminiBackend", Backend)
    monkeypatch.setattr(eval_module, "GeminiBackend", Backend)
    monkeypatch.setattr(eval_module, "app", create_app())
    monkeypatch.setattr(eval_module, "evaluate", lambda *args, **kwargs: (metrics, []))
    monkeypatch.setattr(eval_module, "persist_run", lambda *args, **kwargs: None)
    assert eval_module.main(["--output-dir", str(tmp_path)]) == 0
    assert len(clients) == 2
    assert clients[0].pacer is clients[1].pacer
    assert all(c.closed for c in clients)
