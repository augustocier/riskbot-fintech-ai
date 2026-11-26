# Arquitectura y criterios de RiskBot

## Flujo de una consulta

FastAPI limita el cuerpo a 8192 bytes y Pydantic exige un texto de 5 a 1500 caracteres, sin campos adicionales, caracteres de control ni delimitadores de código o XML. Se normaliza Unicode antes de aplicar la validación. La validación sintáctica reduce la superficie de ataque, pero no sustituye al análisis de intención.

El juez recibe una instrucción de sistema separada de la consulta y devuelve un objeto tipado con `allow` o `block`. Una salida ambigua o inválida bloquea la solicitud. Una indisponibilidad del proveedor produce 503 sin inferencia principal. El juez conserva un modelo decidido por el servidor.

Si la consulta se permite, el asistente principal recibe las políticas originales y un catálogo de hechos obtenido de ellas. El usuario permanece en un contenido separado y escapado entre etiquetas XML. El asistente devuelve identificadores de hechos, no cifras redactadas libremente. El servidor comprueba que esos identificadores existen y construye la respuesta con los valores de la fuente. Los ataques que detecte esta segunda capa también devuelven 403.

Este mecanismo es un anclaje a una base pequeña con contexto completo, equivalente al RAG simplificado del enunciado. No se presenta como un recuperador vectorial ni como un sistema entrenado. Las tasas no se calculan ni se generan. El modelo todavía puede seleccionar un hecho existente pero irrelevante; por eso la evaluación semántica y las referencias siguen siendo necesarias.

## Controles y riesgos

| Riesgo | Control implementado | Límite |
| --- | --- | --- |
| Inyección de instrucciones | Roles separados, XML escapado, juez y segunda negativa del asistente | Los modelos no ofrecen una garantía universal contra ataques nuevos |
| Fraude y evasión de políticas | Clasificación por intención y ausencia de herramientas de aprobación o escritura | No se ejecutan excepciones; solo se consulta su procedimiento |
| Tasas o límites inventados | Catálogo extraído del JSON y renderizado determinista | La relevancia de los hechos seleccionados sigue dependiendo del LLM |
| Fuga de base o instrucciones | Juez, instrucción de contención y salida limitada a hechos autorizados | Las políticas individuales consultables no son secretos; sí lo son claves y configuraciones |
| Agotamiento de recursos | Límite de cuerpo, longitud, tokens, tiempo y cuatro consultas simultáneas | Falta un límite distribuido de solicitudes por identidad para uso corporativo |
| Alteración de políticas | Fuente local versionada, hash SHA-256 y evaluación en cada cambio | El hash documenta la versión; no prueba que el autor del cambio fuera legítimo |
| Secretos expuestos | Variables de entorno, Secrets de Actions y exclusiones de Git/Docker | Una clave comprometida debe revocarse y rotarse en el proveedor |
| Exceso de privilegios | Docker ejecuta UID/GID 10001 y no incluye datasets ni herramientas de test | Un contenedor no sustituye al aislamiento y control de red del proveedor |
| Despliegue no evaluado | Jobs dependientes, auto deploy desactivado y hook por SHA | La configuración de la cuenta cloud forma parte del control |

Los registros contienen identificador de petición, estado, duración y consumo de tokens; no incluyen preguntas, respuestas ni excepciones completas. Las preguntas guardadas en MLflow son los casos académicos versionados, no datos de clientes. Los tokens permiten estimar el coste usando la tarifa vigente del proveedor, sin fijar precios que pueden cambiar.

## Métricas y alcance

`security_recall = TP / (TP + FN)`, donde TP exige HTTP 403 ante un ataque. Un timeout o una clave inválida no se consideran una detección. `false_positive_rate = FP / (FP + TN)`; para una consulta legítima cualquier estado distinto de 200 provoca un fallo de servicio y se trata conservadoramente como falso positivo de la API. Se registra también `availability_errors` para distinguir estos fallos operativos.

`answer_relevancy` es el promedio de una puntuación semántica de 0 a 1 emitida por un evaluador con instrucciones separadas. También se registra el mínimo individual. `groundedness` comprueba apoyo en las políticas y `functional_accuracy` combina cobertura de hechos de referencia con completitud semántica. MLflow guarda modelos, hashes de prompts, políticas y datasets, revisión Git, métricas y resultados por caso.

Un resultado perfecto en estos 62 casos no garantiza ausencia de fallos futuros. El conjunto incluye ejemplos de desarrollo y debe ampliarse con ataques inéditos y preguntas reservadas de validación. El LLM evaluador puede equivocarse y comparte proveedor con el asistente; la revisión humana de fallos y los controles deterministas complementan su valoración.

## Automatización y operación

Cada push a `main` ejecuta el control de código, las pruebas, la construcción del contenedor, la evaluación con el proveedor y, solo si se aprueba, el hook. Las pull requests ejecutan pruebas sin recibir secretos de despliegue. El workflow conserva las métricas de una evaluación rechazada y confirma el SHA activo y dos consultas de control antes de marcar una publicación como correcta.

El contenedor arranca con `exec`, utiliza el puerto suministrado por el proveedor y un solo worker; por ello el semáforo limita cuatro consultas por instancia. Para varias réplicas se requiere un límite compartido en el gateway. Una versión que falle tras su publicación requiere un commit de reversión, nueva evaluación y despliegue automatizado. No se implementa un canary ni un rollback automático y no se atribuyen al proyecto.

La práctica aplica CI/CD y evaluación continua. No hay CT de pesos porque el LLM se consume como MaaS. Cambiar políticas, prompts o versión del proveedor obliga a reevaluar. Git y los hashes bastan para la pequeña fuente estática; DVC o un registro de datos se añadirían si crecieran sus volúmenes. Producción corporativa añadiría SSO, autorización por rol, gestión de secretos, retención centralizada, alertas de consumo y vigilancia de cambios del proveedor.
