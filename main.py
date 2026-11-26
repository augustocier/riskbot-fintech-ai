"""API de consulta con juez semántico, anclaje verificable y errores opacos."""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from prompts import ANSWER_SYSTEM, JUDGE_SYSTEM

ROOT = Path(__file__).resolve().parent
DENIED = "Petición denegada por políticas de seguridad"
UNAVAILABLE = "Servicio temporalmente no disponible"
OUT_OF_SCOPE = (
    "Lo siento, solo puedo responder a consultas relacionadas con las políticas "
    "de concesión de créditos y riesgos de la empresa."
)
NOT_AVAILABLE = "Información no disponible en las políticas actuales."
LOGGER = logging.getLogger("riskbot")
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Settings:
    default_model: str = "gemini-3.5-flash-lite"
    allowed_models: tuple[str, ...] = ("gemini-3.5-flash-lite",)
    judge_model: str = "gemini-3.5-flash-lite"
    timeout_seconds: float = 40.0
    max_concurrent_requests: int = 4

    @classmethod
    def from_env(cls):
        default = os.getenv("DEFAULT_MODEL", "gemini-3.5-flash-lite")
        allowed = tuple(
            value.strip()
            for value in os.getenv("ALLOWED_MODELS", default).split(",")
            if value.strip()
        )
        if default not in allowed:
            raise ValueError("DEFAULT_MODEL debe pertenecer a ALLOWED_MODELS")
        return cls(
            default_model=default,
            allowed_models=allowed,
            judge_model=os.getenv("JUDGE_MODEL", default),
        )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InferenceRequest(StrictModel):
    prompt: str = Field(min_length=5, max_length=1500)
    model: str | None = Field(default=None, min_length=3, max_length=100)

    @field_validator("prompt")
    @classmethod
    def safe_text(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", value).strip()
        if not 5 <= len(value) <= 1500:
            raise ValueError("Entrada inválida")
        punctuation = set(" .,;:!?¿¡€%+-/()'\"\n")
        if any(
            not (unicodedata.category(c).startswith(("L", "N")) or c in punctuation)
            for c in value
        ):
            raise ValueError("Entrada inválida")
        return value

    @field_validator("model")
    @classmethod
    def safe_model(cls, value: str | None):
        if value is not None and not re.fullmatch(r"[a-zA-Z0-9._-]+", value):
            raise ValueError("Modelo inválido")
        return value


class InferenceResponse(StrictModel):
    resultado: str


class SecurityVerdict(StrictModel):
    decision: Literal["allow", "block"]
    category: Literal[
        "policy_query", "off_topic", "financial_fraud", "prompt_injection",
        "confidential_extraction",
    ]


class AnswerPlan(StrictModel):
    status: Literal["answer", "not_available", "out_of_scope", "refuse"]
    fact_ids: list[str] = Field(max_length=16)


class Backend(Protocol):
    async def generate(
        self, *, model: str, system: str, user: str, schema: type[T]
    ) -> T: ...


class GeminiBackend:
    def __init__(self, api_key: str, timeout_seconds: float):
        from google import genai
        from google.genai import types

        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        self.timeout = timeout_seconds

    async def generate(self, *, model, system, user, schema):
        from google.genai import types

        response = await asyncio.wait_for(
            self.client.aio.models.generate_content(
                model=model,
                contents=types.Content(role="user", parts=[types.Part(text=user)]),
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=schema.model_json_schema(),
                    temperature=0.0,
                    max_output_tokens=2048,
                ),
            ),
            timeout=self.timeout,
        )
        usage = response.usage_metadata
        LOGGER.info(json.dumps({
            "event": "provider_usage", "model": model,
            "input_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None),
        }))
        if not response.text:
            raise ValueError("Respuesta vacía")
        return schema.model_validate_json(response.text)

    async def close(self):
        await self.client.aio.aclose()
        self.client.close()


def load_policy(path: Path) -> tuple[dict, str]:
    """Error de arranque si la fuente falta o está incompleta; nunca usar un JSON vacío."""
    raw = path.read_bytes()
    data = json.loads(raw)
    p = data["politicas_credito"]
    expected_sections = {
        "limites_sin_aval", "requisitos_hipoteca", "tasas_interes_anual",
        "documentacion_requerida", "penalizaciones_impago", "reglas_cumplimiento",
    }
    if set(p) != expected_sections or len(p["documentacion_requerida"]) != 4:
        raise ValueError("Base de políticas incompleta")
    # build_facts también comprueba la presencia de cada dato obligatorio.
    build_facts(data)
    return data, hashlib.sha256(raw).hexdigest()


def build_facts(data: dict) -> dict[str, str]:
    p = data["politicas_credito"]
    h = p["requisitos_hipoteca"]
    rates = p["tasas_interes_anual"]
    penalty = p["penalizaciones_impago"]

    def number(value):
        return f"{value:g}".replace(".", ",")

    facts = {}
    for key, label in (
        ("prestamo_personal", "préstamo personal"),
        ("tarjeta_credito", "tarjeta de crédito"),
        ("anticipo_nomina", "anticipo de nómina"),
    ):
        facts[f"limites_sin_aval.{key}"] = (
            f"El límite sin aval para {label} es {number(p['limites_sin_aval'][key])} euros."
        )
    facts.update({
        "hipoteca.edad_minima": (
            f"La edad mínima para una hipoteca es {h['edad_minima']} años. "
            "Una solicitud de una persona por debajo de esa edad no es viable."
        ),
        "hipoteca.edad_maxima_al_finalizar": (
            f"La edad máxima al finalizar la hipoteca es {h['edad_maxima_al_finalizar']} años."
        ),
        "hipoteca.financiacion": (
            f"La financiación hipotecaria máxima es el {number(h['porcentaje_financiacion_maximo'])}%."
        ),
        "hipoteca.contrato": (
            "Se requiere contrato indefinido para una hipoteca."
            if h["contrato_indefinido_requerido"]
            else "No se requiere contrato indefinido para una hipoteca."
        ),
        "hipoteca.antiguedad": (
            f"La antigüedad laboral mínima para una hipoteca es {h['antiguedad_laboral_minima_meses']} meses."
        ),
        "impago.comision": (
            "La comisión por reclamación de posiciones deudoras es "
            f"{number(penalty['comision_reclamacion_posiciones_deudoras'])} euros."
        ),
        "impago.demora": (
            "El interés de demora adicional es "
            f"{number(penalty['interes_demora_adicional_porcentual'])} puntos porcentuales."
        ),
    })
    for key, label in (("tier_A_vip", "A VIP"), ("tier_B_estandar", "B estándar"), ("tier_C_riesgo", "C riesgo")):
        facts[f"tasas.{key}"] = f"La tasa de interés anual del Tier {label} es del {number(rates[key])}%."
    for i, document in enumerate(p["documentacion_requerida"]):
        facts[f"documentacion.{i}"] = f"Documento obligatorio: {document}."
    for i, rule in enumerate(p["reglas_cumplimiento"]):
        facts[f"cumplimiento.{i}"] = rule
    return facts


POLICIES, POLICY_SHA256 = load_policy(ROOT / "politicas_credito.json")
FACTS = build_facts(POLICIES)


def render_answer(plan: AnswerPlan, facts: dict[str, str], question: str) -> str:
    """El LLM selecciona hechos; nunca genera los importes ni las políticas de salida."""
    if plan.status == "refuse":
        raise HTTPException(403, DENIED)
    if plan.status != "answer":
        if plan.fact_ids:
            raise HTTPException(403, DENIED)
        if plan.status == "out_of_scope":
            return OUT_OF_SCOPE
        if re.search(r"\btier\s+[d-z]\b", question, re.IGNORECASE):
            return NOT_AVAILABLE + " Las políticas actuales solo recogen perfiles Tier A, B y C."
        return NOT_AVAILABLE
    if not plan.fact_ids or any(key not in facts for key in plan.fact_ids):
        raise HTTPException(403, DENIED)
    # Orden estable y valores extraídos solo de la fuente versionada.
    return "\n".join(facts[key] for key in dict.fromkeys(plan.fact_ids))


class BodyLimitMiddleware:
    """Limita también peticiones sin Content-Length, antes de parsear JSON."""
    def __init__(self, app, max_bytes=8192):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                return await JSONResponse({"detail": DENIED}, status_code=400)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(backend: Backend | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application):
        owned = None
        if backend is None and os.getenv("GEMINI_API_KEY"):
            owned = GeminiBackend(os.environ["GEMINI_API_KEY"], settings.timeout_seconds)
            application.state.backend = owned
        application.state.slots = asyncio.Semaphore(settings.max_concurrent_requests)
        yield
        if owned:
            await owned.close()

    api = FastAPI(title="RiskBot API de FinTech-AI", version="1.0.0", lifespan=lifespan)
    api.state.backend = backend
    api.add_middleware(BodyLimitMiddleware)

    @api.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": DENIED}, status_code=400)

    @api.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        message = DENIED if exc.status_code in (400, 403) else UNAVAILABLE
        if exc.status_code == 404:
            message = "Recurso no encontrado"
        return JSONResponse({"detail": message}, status_code=exc.status_code)

    @api.exception_handler(Exception)
    async def unexpected_error(request, exc):
        LOGGER.error("unexpected_error type=%s", type(exc).__name__)
        return JSONResponse({"detail": UNAVAILABLE}, status_code=503)

    @api.middleware("http")
    async def audit(request: Request, call_next):
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        LOGGER.info(json.dumps({
            "event": "request", "request_id": request_id, "method": request.method,
            "status": response.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }))
        return response

    @api.get("/health")
    @api.get("/")
    async def health():
        if api.state.backend is None:
            raise HTTPException(503, UNAVAILABLE)
        return {
            "status": "ok",
            "revision": os.getenv("RENDER_GIT_COMMIT", os.getenv("GIT_COMMIT", "local")),
        }

    @api.post("/inferencia", response_model=InferenceResponse)
    async def inferencia(payload: InferenceRequest):
        selected_model = payload.model or settings.default_model
        if selected_model not in settings.allowed_models:
            raise HTTPException(400, DENIED)
        service = api.state.backend
        if service is None:
            raise HTTPException(503, UNAVAILABLE)
        user = f"<user_prompt>{html.escape(payload.prompt)}</user_prompt>"
        try:
            await asyncio.wait_for(api.state.slots.acquire(), timeout=2.0)
        except TimeoutError:
            raise HTTPException(429, "Capacidad temporalmente agotada") from None
        try:
            # El cliente elige la inferencia, nunca puede degradar el juez.
            try:
                verdict = await service.generate(
                    model=settings.judge_model, system=JUDGE_SYSTEM, user=user, schema=SecurityVerdict
                )
                verdict = SecurityVerdict.model_validate(verdict)
            except (ValueError, ValidationError):
                raise HTTPException(403, DENIED) from None
            if verdict.decision != "allow":
                raise HTTPException(403, DENIED)
            grounded_system = (
                ANSWER_SYSTEM + "\n<politicas>" + html.escape(json.dumps(POLICIES, ensure_ascii=False))
                + "</politicas>\n<catalogo>" + html.escape(json.dumps(FACTS, ensure_ascii=False)) + "</catalogo>"
            )
            plan = await service.generate(
                model=selected_model, system=grounded_system, user=user, schema=AnswerPlan
            )
            plan = AnswerPlan.model_validate(plan)
            return InferenceResponse(resultado=render_answer(plan, FACTS, payload.prompt))
        except HTTPException:
            raise
        except (ValueError, ValidationError):
            raise HTTPException(403, DENIED) from None
        except Exception as exc:
            LOGGER.warning("provider_unavailable type=%s", type(exc).__name__)
            raise HTTPException(503, UNAVAILABLE) from None
        finally:
            api.state.slots.release()

    return api


app = create_app()
