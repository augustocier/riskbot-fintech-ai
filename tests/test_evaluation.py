import json

import pytest
from fastapi.testclient import TestClient

from eval import Grade, evaluate, load_datasets, main, passes_gate, persist_run
from main import ROOT, create_app


def clean_metrics():
    return {
        "security_recall": 1.0, "false_positive_rate": 0.0,
        "answer_relevancy": 1.0, "min_answer_relevancy": 1.0,
        "groundedness": 1.0, "functional_accuracy": 1.0,
        "availability_errors": 0.0,
    }


@pytest.mark.parametrize("key,value", [
    ("security_recall", 0.99), ("false_positive_rate", 0.01),
    ("min_answer_relevancy", 0.89), ("groundedness", 0.99),
    ("functional_accuracy", 0.99), ("availability_errors", 1.0),
])
def test_gate_blocks_each_kind_of_failure(key, value):
    metrics = clean_metrics()
    assert passes_gate(metrics)
    metrics[key] = value
    assert not passes_gate(metrics)


def test_datasets_have_balanced_security_and_independent_references():
    security, functional = load_datasets(ROOT / "eval_data")
    assert len(security) == 32
    assert sum(c["es_ataque"] for c in security) == 16
    assert len(functional) == 30
    assert all(c["expected_answer"] and c["expected_contains"] for c in functional)


def test_invalid_model_actually_fails_gate_without_calling_provider():
    class NeverCallBackend:
        async def generate(self, **kwargs):
            pytest.fail("No debe invocarse al proveedor con un modelo inválido")

    security, functional = load_datasets(ROOT / "eval_data")
    with TestClient(create_app(NeverCallBackend())) as client:
        metrics, records = evaluate(
            client, security, functional, "modelo-no-autorizado",
            lambda *_: pytest.fail("No debe evaluarse una respuesta inexistente"),
        )
    assert metrics["security_recall"] == 0.0
    assert metrics["false_positive_rate"] == 1.0
    assert not passes_gate(metrics)
    assert all(row["status"] == 400 for row in records)


def test_semantic_judge_detects_plausible_but_wrong_rate():
    class Response:
        status_code = 200

        def json(self):
            return {"resultado": "La tasa del Tier B es 99%."}

    class Client:
        def post(self, *args, **kwargs):
            if kwargs["json"]["prompt"] == "ataque":
                class Blocked(Response):
                    status_code = 403
                return Blocked()
            return Response()

    security = [{"id": "s1", "prompt": "ataque", "es_ataque": True},
                {"id": "s2", "prompt": "consulta", "es_ataque": False}]
    functional = [{"id": "f1", "prompt": "tasa", "expected_answer": "7,5%", "expected_contains": [["Tier B"]]}]
    metrics, _ = evaluate(
        Client(), security, functional, "model",
        lambda *_: Grade(score=0.8, grounded=False, complete=False),
    )
    assert metrics["security_recall"] == 1.0
    assert metrics["false_positive_rate"] == 0.0
    assert metrics["groundedness"] == 0.0
    assert not passes_gate(metrics)


def test_no_key_produces_no_fake_evaluation(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert main(["--output-dir", str(tmp_path / "results")]) == 2
    assert "No se ha ejecutado" in capsys.readouterr().err
    assert not (tmp_path / "results").exists()


def test_mlflow_records_are_labeled_as_unit_tests(tmp_path):
    import mlflow
    from mlflow.tracking import MlflowClient

    metrics = clean_metrics()
    uri = f"sqlite:///{tmp_path / 'tracking.db'}"
    persist_run(metrics, [], {"purpose": "unit_test"}, tmp_path / "artifacts", uri, mode="unit_test")
    runs = MlflowClient(tracking_uri=uri).search_runs(
        [mlflow.get_experiment_by_name("RiskBot evaluacion de seguridad y calidad").experiment_id]
    )
    assert runs[0].data.tags["evaluation_mode"] == "unit_test"
    assert runs[0].data.metrics["security_recall"] == 1.0
    assert json.loads((tmp_path / "artifacts/evaluation.json").read_text())["mode"] == "unit_test"
