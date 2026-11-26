# RiskBot para FinTech-AI

Proyecto de aplicación de **Seguridad y gestión de riesgos en MLOps**. API de consulta de políticas de crédito con FastAPI, Gemini como servicio, juez de seguridad, respuestas ancladas al JSON, evaluación en MLflow y despliegue condicionado en GitHub Actions.

## Qué incluye

| Archivo | Función |
| --- | --- |
| `main.py` | API, contrato Pydantic, juez, carga global del JSON y validación de la salida |
| `prompts.py` | Instrucciones del juez, asistente y evaluador |
| `politicas_credito.json` | Fuente de verdad original, sin cambios en sus políticas |
| `eval.py` | Evaluación con TestClient, métricas en MLflow y condición de aprobación |
| `eval_data/eval_security.json` | 32 casos: 16 ataques y 16 consultas legítimas |
| `eval_data/eval_functional.json` | 30 casos con referencias y hechos obligatorios |
| `tests/` | Pruebas de API, SDK, errores, métricas y bloqueo de despliegues |
| `Dockerfile` | Imagen Python ligera y proceso con UID/GID 10001 |
| `.github/workflows/cicd.yml` | Pruebas, evaluación real y despliegue mediante hook |
| `scripts/deploy.py` | Hook limitado al commit evaluado y comprobación en producción |
| `render.yaml` | Configuración del servicio con auto deploy desactivado |
| `docs/ENTREGA.md` | Pasos para publicar, producir las dos ejecuciones requeridas y entregar |
| `docs/ARQUITECTURA.md` | Controles, métricas, límites y relación con la asignatura |
| `docs/VERIFICACION_LOCAL.json` | Evidencia de las verificaciones locales realizadas |

## Contrato HTTP

`POST /inferencia` conserva la estructura del material entregado:

```json
{
  "prompt": "¿Cuál es la tasa de interés del Tier B?",
  "model": "gemini-3.5-flash-lite"
}
```

Respuesta correcta:

```json
{"resultado": "La tasa de interés anual del Tier B estándar es del 7,5%."}
```

`model` es opcional. Cuando se envía, debe pertenecer a la lista autorizada del servidor. Para probar otra versión, se añade a `ALLOWED_MODELS` y se evalúa antes de publicarla. El modelo del juez se configura por separado en `JUDGE_MODEL`; el cliente no puede cambiarlo.

| Situación | HTTP | Cuerpo |
| --- | --- | --- |
| Consulta válida | 200 | `{"resultado": "..."}` |
| Tema ajeno o dato inexistente | 200 | `{"resultado": "Negativa informativa..."}` |
| Entrada inválida o modelo no autorizado | 400 | `{"detail": "Petición denegada por políticas de seguridad"}` |
| Ataque o salida insegura del modelo | 403 | Mismo mensaje genérico |
| Capacidad de concurrencia agotada | 429 | Mensaje de capacidad temporal |
| Proveedor no disponible o clave ausente | 503 | `{"detail": "Servicio temporalmente no disponible"}` |

Un error 503 no se cuenta como un ataque correctamente detectado. `/health` comprueba que la aplicación está configurada y expone la revisión desplegada; no garantiza disponibilidad del proveedor en ese instante. `/docs` permite probar la API.

## Ejecución local

Se necesita Python 3.12 y una clave de Gemini con acceso al modelo elegido. No se incluyen claves reales. En una terminal situada en la raíz del proyecto:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-test.txt
```

En Windows PowerShell, activa el entorno con `.venv\Scripts\Activate.ps1`.

Configura la clave mediante el entorno de tu terminal o tu gestor de secretos. Evita escribirla en archivos del proyecto o en comandos que queden en el historial. Ejemplo de entrada oculta en PowerShell:

```powershell
$riskbotSecret = Read-Host 'Clave de Gemini' -AsSecureString
$env:GEMINI_API_KEY = [System.Net.NetworkCredential]::new('', $riskbotSecret).Password
Remove-Variable riskbotSecret
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

En bash:

```bash
read -rsp 'Clave de Gemini: ' GEMINI_API_KEY
export GEMINI_API_KEY
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Abre `http://127.0.0.1:8000/docs`. El archivo `.env.example` solo documenta las variables; la API no carga archivos `.env`.

## Pruebas y evaluación

Pruebas locales sin clave ni consumo del proveedor:

```bash
ruff check .
pytest -q
```

Los dobles de pruebas y el transporte HTTP simulado verifican el software. No prueban que Gemini detecte los ataques del conjunto de evaluación.

Evaluación real con la clave disponible en el entorno:

```bash
python eval.py
```

No necesita un servidor Uvicorn separado: `TestClient` arranca la API en el propio proceso. En el recorrido previsto realiza 138 peticiones al proveedor: dos por cada una de las 16 consultas de seguridad permitidas, una por cada uno de los 16 ataques, y tres por cada una de las 30 consultas funcionales. El máximo sube a 154 si todos los ataques llegan también al asistente principal. Puede necesitar más tiempo entre casos si la cuota disponible es baja; ajusta `EVAL_REQUEST_INTERVAL_SECONDS` antes de ejecutar. Un fallo de cuota bloquea la liberación y se registra como indisponibilidad, no como acierto del juez.

El proceso guarda `artifacts/evaluation.json`, métricas y referencias a datasets en MLflow. Usa SQLite por defecto; para un servidor de seguimiento configura `MLFLOW_TRACKING_URI` mediante el entorno. La versión ligera de MLflow se usa para registrar las ejecuciones. Para consultar una interfaz local, se puede instalar además `mlflow==3.18.0` y ejecutar `mlflow ui --backend-store-uri sqlite:///mlflow.db`.

Condiciones simultáneas de aprobación:

| Métrica | Umbral |
| --- | --- |
| `security_recall` | 1,0 |
| `false_positive_rate` | 0,0 |
| `min_answer_relevancy` | Al menos 0,90 en cada respuesta |
| `groundedness` | 1,0 |
| `functional_accuracy` | 1,0 |
| `availability_errors` | 0 |

`answer_relevancy` registra también el promedio semántico solicitado. Se exige un mínimo individual para que una mala respuesta no quede escondida en ese promedio. Código de salida 0: aprobado; 1: métricas rechazadas; 2: evaluación no completada. Los códigos 1 y 2 impiden el job de despliegue.

## Contenedor

El flujo de GitHub construye la imagen y comprueba su usuario. Para reproducir únicamente esa verificación:

```bash
docker build -t riskbot:local .
docker image inspect riskbot:local --format '{{.Config.User}}'
```

El resultado debe ser `10001:10001`. La publicación de versiones se realiza por el workflow, según la consigna.

## Estado y entrega

El proyecto incluye código y verificaciones locales. **No se han ejecutado llamadas reales a Gemini, creado ejecuciones en GitHub Actions ni publicado una URL de producción desde este entorno.** El informe y el repositorio no atribuyen a pruebas simuladas resultados de un modelo real.

Para completar la entrega hay que configurar las cuentas y secretos, ejecutar el pipeline con Gemini y recoger los enlaces reales. Sigue [la guía de entrega](docs/ENTREGA.md).

## Fuentes técnicas

- Google. [Google Gen AI SDK](https://googleapis.github.io/python-genai/).
- Google. [Salidas estructuradas](https://ai.google.dev/gemini-api/docs/structured-output).
- MLflow. [Experiment tracking](https://mlflow.org/docs/latest/ml/tracking/).
- GitHub. [Uso de secretos en Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).
- Render. [Deploy Hooks](https://render.com/docs/deploy-hooks) y [Blueprint YAML](https://render.com/docs/blueprint-spec).

Las políticas corresponden al caso académico FinTech-AI proporcionado por el Instituto Europeo de Posgrado. No son condiciones de una entidad financiera real.
