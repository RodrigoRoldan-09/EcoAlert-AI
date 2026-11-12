# EcoAlert AI

Clasificación inteligente de incidentes de contaminación ambiental con IA en AWS.

EcoAlert AI es una demo serverless: una persona describe un incidente (por ejemplo, *"Una fábrica vierte residuos químicos al río Lerma, en Toluca"*) desde una app web. El backend clasifica la gravedad como **BAJA**, **MEDIA** o **ALTA** con dos modelos en paralelo para compararlos (**Amazon Comprehend** y **Amazon Bedrock** con Claude), extrae la ubicación, guarda un ticket en **Amazon DynamoDB** y, si la gravedad es **ALTA**, envía una alerta por correo con **Amazon SES** a todos los usuarios registrados (correos verificados). La app muestra la gravedad de ambos modelos, la ubicación, la justificación de Bedrock y métricas de rendimiento (segundos, tokens y unidades de texto).

## Arquitectura

```
Usuario ──HTTPS──▶ CloudFront (OAC) ──▶ S3 privado (index.html, styles.css, app.js, config.js)

app.js ──POST /report──▶ API Gateway (HTTP API: HTTPS, CORS, throttling)
                              │
                              ▼
                     Lambda (Python 3.12, rol IAM de mínimo privilegio)
                     ├── Amazon Comprehend  ┐ en paralelo
                     └── Amazon Bedrock     ┘
                              │
                              ├──▶ DynamoDB (ticket, gravedad, segundos, tokens)
                              └──▶ Amazon SES (solo si ALTA) ──▶ usuarios verificados
```

Todo se crea con **una sola plantilla de CloudFormation** (`template.yaml`) en la región **us-east-1**.

| Recurso | Para qué |
|---|---|
| S3 (privado) + CloudFront con Origin Access Control | Sitio web estático por HTTPS; el bucket no es público |
| API Gateway HTTP API `POST /report` | Entrada HTTPS con CORS (solo el dominio de CloudFront) y throttling (10 req/s, ráfaga 20) |
| Lambda `ecoalert-analyze` | Valida la entrada y orquesta Comprehend, Bedrock, DynamoDB y SES |
| DynamoDB (on-demand) | Registro de tickets |
| Amazon SES | Correo HTML de alerta |
| Recurso personalizado `SiteConfig` | Escribe `config.js` con la URL de la API en el bucket y lo vacía al eliminar el stack |

## Estructura del repositorio

```
.
├── template.yaml              Plantilla única de CloudFormation (todos los recursos)
├── backend/app.py             Lambda principal (handler: app.handler)
├── frontend/
│   ├── index.html             Interfaz web (español, accesible y responsiva)
│   ├── styles.css             Estilos
│   ├── app.js                 Lógica del cliente (lee window.ECOALERT_API_URL)
│   └── config.example.js      Ejemplo de config.js (el real lo genera el stack)
├── events/sample-report.json  Reporte de ejemplo para probar con curl
├── tests/test_app.py          Pruebas unitarias con boto3 simulado
└── README.md
```

## Cómo decide la gravedad

### Amazon Comprehend

Se llama a `DetectSentiment` y `DetectEntities` con `LanguageCode="es"`. La gravedad se deriva del puntaje **NEGATIVE** del sentimiento:

| Puntaje NEGATIVE | Gravedad |
|---|---|
| ≥ 0.85 | ALTA |
| ≥ 0.50 | MEDIA |
| < 0.50 | BAJA |

Los umbrales son las constantes `UMBRAL_ALTA` y `UMBRAL_MEDIA` en `backend/app.py`. La ubicación se toma de las entidades de tipo `LOCATION` con puntaje ≥ 0.5 (sin duplicados, unidas con coma). Como estas API aceptan como máximo 5000 bytes UTF-8, el texto que se envía a Comprehend se recorta a ese tamaño si hace falta (solo ocurre con muchos emojis o caracteres especiales); Bedrock siempre recibe el texto completo.

### Amazon Bedrock (Claude)

Se usa la API **Converse** (devuelve el uso de tokens) con temperatura 0 y este prompt:

> Eres un analista ambiental. Clasifica la gravedad del reporte como BAJA, MEDIA o ALTA, extrae la ubicación si existe y justifica en una frase. Responde solo en JSON con las claves "gravedad", "ubicacion" y "justificacion".
>
> Reporte: {texto del usuario}

La respuesta se interpreta de forma robusta: se quitan bloques ```` ```json ```` y, si hay texto alrededor, se extrae el objeto JSON.

### Reglas finales

- **Gravedad final** = la de Bedrock si respondió; si Bedrock falla, la de Comprehend.
- **Ubicación final** = la de Bedrock si la encontró; si no, la de Comprehend; si ninguna, "No identificada".
- **Alerta por correo**: solo cuando la gravedad final es **ALTA**. Se envía un correo por destinatario (no se ven entre sí) a todas las identidades de **correo** verificadas en SES, hasta 50. El remitente es el parámetro `SenderEmail`.
- **Tolerancia a fallos**: si fallan Bedrock, SES o DynamoDB, la respuesta sigue siendo 200 con los resultados disponibles y una nota en `errores`. Si fallan ambos modelos se responde 502. Bedrock se llama con un solo intento y un tiempo máximo de lectura de 12 s, para que todo quepa en el límite de 29 s de API Gateway: si tarda más, cuenta como fallo y se usa la gravedad de Comprehend. Una entrada inválida (vacía, sin `texto`, JSON inválido o más de 2000 caracteres) responde 400.

### Métricas

- **Segundos** por modelo, medidos con `time.perf_counter()` alrededor de cada llamada.
- **Tokens de Bedrock**: `usage.inputTokens`, `usage.outputTokens` y `usage.totalTokens` de Converse.
- **Unidades de Comprehend** = `ceil(caracteres / 100)` por llamada (mínimo 1). Nota: Comprehend factura un mínimo de 3 unidades por solicitud.

### Ejemplo de petición y respuesta

```bash
curl -s -X POST "$API_URL" -H "Content-Type: application/json" \
  -d '{"texto": "Una fábrica vierte residuos químicos al río Lerma, en Toluca"}'
```

```json
{
  "ticketId": "6f1c2a4e-...",
  "fecha": "2026-11-12T10:42:05Z",
  "comprehend": "ALTA",
  "bedrock": "ALTA",
  "gravedadFinal": "ALTA",
  "ubicacion": "Río Lerma, Toluca",
  "justificacion": "Vertido de químicos en un río con riesgo para la fauna y la salud.",
  "sentimiento": "NEGATIVE",
  "puntajeNegativo": 0.97,
  "segundos": {"comprehend": 0.42, "bedrock": 2.8},
  "tokens": {"entrada": 186, "salida": 54, "total": 240},
  "unidadesComprehend": 1,
  "alertaEnviada": true,
  "destinatarios": 2,
  "modeloBedrock": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  "errores": {}
}
```

### Ticket en DynamoDB

Atributos: `ticketId` (clave), `fecha` (ISO UTC), `reporte`, `ubicacion`, `gravedadComprehend`, `gravedadBedrock`, `gravedadFinal`, `sentimiento`, `justificacion`, `segundosComprehend`, `segundosBedrock`, `tokensEntradaBedrock`, `tokensSalidaBedrock`, `tokensBedrock`, `unidadesComprehend`, `alertaEnviada`. Los números se guardan como tipo `N` (decimal exacto) y los valores faltantes como `N/D` o `0`.

## Permisos IAM (rol de la Lambda)

| Permiso | Recurso | Motivo |
|---|---|---|
| `logs:CreateLogStream`, `logs:PutLogEvents` | Solo el grupo `/aws/lambda/ecoalert-analyze` | Logs de la función |
| `comprehend:DetectSentiment`, `comprehend:DetectEntities` | `*` | Estas acciones no admiten permisos por recurso |
| `bedrock:InvokeModel` | `foundation-model/*` y `inference-profile/*` de la cuenta | Converse usa este permiso; los perfiles `us.` enrutan a varias regiones de EE. UU. |
| `dynamodb:PutItem` | Solo la tabla del stack | Guardar tickets |
| `ses:SendEmail`, `ses:SendRawEmail` | `identity/*` de la cuenta en us-east-1 | Enviar alertas |
| `ses:ListIdentities`, `ses:GetIdentityVerificationAttributes` | `*` | Listar correos verificados; no admiten permisos por recurso |

## Requisitos previos

1. **AWS CLI v2** con credenciales configuradas. Comprueba con:
   ```bash
   aws --version
   aws sts get-caller-identity
   ```
2. **Región us-east-1** (todo el despliegue usa `--region us-east-1`).
3. **Acceso a Claude en Amazon Bedrock**: los modelos de Anthropic requieren llenar una sola vez el formulario de caso de uso ("first time use"). En la consola de Bedrock (us-east-1) entra a *Model catalog*, abre un modelo Claude y completa el formulario si te lo pide. Para ver los perfiles de inferencia disponibles (y, si quieres, cambiar `BedrockModelId`):
   ```bash
   aws bedrock list-inference-profiles --region us-east-1 \
     --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'anthropic')].inferenceProfileId" \
     --output table
   ```
4. **Amazon SES en modo sandbox**: solo puedes enviar desde y hacia correos verificados. Verifica el remitente y cada destinatario (llega un correo con un enlace que hay que abrir):
   ```bash
   aws ses verify-email-identity --email-address tu-correo@ejemplo.com --region us-east-1
   aws ses verify-email-identity --email-address otro-usuario@ejemplo.com --region us-east-1
   ```
   Comprueba el estado:
   ```bash
   aws ses list-identities --identity-type EmailAddress --region us-east-1
   aws ses get-identity-verification-attributes --identities tu-correo@ejemplo.com --region us-east-1
   ```
   Cada correo verificado recibe las alertas (incluido el remitente). Salir del sandbox (solicitar acceso de producción) es opcional para esta demo.

## Despliegue paso a paso (macOS, zsh)

Ejecuta los comandos desde la carpeta del proyecto. La ruta tiene espacios (y un espacio después de `IPN`), así que va entre comillas. Si clonaste el repositorio en otro lugar, usa tu propia ruta.

> **Importante en zsh (macOS):** por defecto zsh no acepta comentarios (`# ...`) al pegar comandos en la terminal. Por eso cada bloque empieza con `setopt interactivecomments`, que los habilita en la sesión actual. Ejecútalo (o pega el bloque completo) antes de los demás comandos.

```bash
setopt interactivecomments
cd "/Users/rdsroldn/Documents/AWS SCD 2026 - SBG IPN /EcoAlert-Demo"

# 1. Variables
export AWS_REGION=us-east-1
export STACK_NAME=ecoalert-demo
export SENDER_EMAIL="tu-correo@ejemplo.com"     # verificado en SES
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ARTIFACT_BUCKET="ecoalert-artifacts-${ACCOUNT_ID}-${AWS_REGION}"

# 2. Bucket para el código empaquetado de la Lambda (solo la primera vez)
aws s3 mb "s3://${ARTIFACT_BUCKET}" --region "$AWS_REGION"

# 3. Empaquetar: sube backend/ a S3 y genera packaged.yaml
aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --output-template-file packaged.yaml \
  --region "$AWS_REGION"

# 4. Desplegar el stack (tarda varios minutos por CloudFront)
aws cloudformation deploy \
  --template-file packaged.yaml \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides SenderEmail="$SENDER_EMAIL" \
  --region "$AWS_REGION"
# Opcional: otro modelo -> agrega BedrockModelId=us.anthropic.claude-... en --parameter-overrides

# 5. Ver los outputs
aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs" --output table

# 6. Guardar los outputs en variables
export SITE_BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='SiteBucketName'].OutputValue" --output text)
export DISTRIBUTION_ID=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text)
export API_URL=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)
export WEBSITE_URL=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='WebsiteURL'].OutputValue" --output text)
export TABLE_NAME=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='TableName'].OutputValue" --output text)

# 7. Subir el frontend (config.js lo escribe el stack; no se sube)
aws s3 sync frontend/ "s3://${SITE_BUCKET}/" \
  --exclude "config.js" --exclude "config.example.js" \
  --region "$AWS_REGION"

# 8. Invalidar la caché de CloudFront
aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" --paths "/*"

# 9. Probar la API
curl -s -X POST "$API_URL" -H "Content-Type: application/json" \
  -d @events/sample-report.json | python3 -m json.tool

# 10. Abrir la app web
open "$WEBSITE_URL"
```

Notas:

- CloudFront puede tardar unos minutos en servir el sitio la primera vez.
- `config.js` (con `window.ECOALERT_API_URL`) lo genera el stack automáticamente en el bucket; nunca lo subas ni lo borres con `sync --delete`.
- Para **actualizar**: repite los pasos 3 y 4 (cambios en backend o plantilla) y los pasos 7 y 8 (cambios en el frontend).
- Usa siempre la URL de CloudFront (`WebsiteURL`): la API solo acepta CORS desde ese dominio. `curl` no se ve afectado por CORS.

## Verificación y logs

```bash
setopt interactivecomments

# Logs de la Lambda en vivo
aws logs tail "/aws/lambda/ecoalert-analyze" --follow --region us-east-1

# Últimos tickets guardados
aws dynamodb scan --table-name "$TABLE_NAME" --max-items 5 --region us-east-1
```

| Síntoma | Causa probable y solución |
|---|---|
| `errores.bedrock` con `AccessDeniedException` | Falta habilitar Claude / formulario de primer uso en Bedrock (us-east-1) |
| `errores.bedrock` con `ValidationException` | ID de modelo no válido: revisa `aws bedrock list-inference-profiles` y despliega con otro `BedrockModelId` |
| No llega el correo | Revisa que remitente y destinatarios estén verificados (`Success`) y la carpeta de spam; mira `errores.ses` |
| Error de CORS en el navegador | Abre la app desde la URL de CloudFront, no desde un archivo local |
| CloudFront responde 403 | Falta el `s3 sync` del frontend o la invalidación; espera unos minutos |
| `Falta config.js…` en la app | El stack no terminó de crearse; revisa los eventos del stack |

## Pruebas locales

No requieren red ni credenciales de AWS (boto3 se simula):

```bash
setopt interactivecomments
cd "/Users/rdsroldn/Documents/AWS SCD 2026 - SBG IPN /EcoAlert-Demo"
python3 -m unittest discover -s tests -v
```

Opcional, validar la plantilla con cfn-lint en un entorno virtual:

```bash
setopt interactivecomments
python3 -m venv /tmp/ecoalert-venv
/tmp/ecoalert-venv/bin/pip install cfn-lint
/tmp/ecoalert-venv/bin/cfn-lint --regions us-east-1 -- template.yaml
rm -rf /tmp/ecoalert-venv
```

## Costos y limpieza

Todo es de pago por uso (DynamoDB on-demand, Lambda, API Gateway, Comprehend, Bedrock, SES, CloudFront). Para eliminar todo:

```bash
setopt interactivecomments

# Si abriste una terminal nueva, vuelve a definir las variables
export AWS_REGION=us-east-1
export STACK_NAME=ecoalert-demo
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ARTIFACT_BUCKET="ecoalert-artifacts-${ACCOUNT_ID}-${AWS_REGION}"

# Eliminar el stack y esperar a que termine
aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$AWS_REGION"
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$AWS_REGION"

# Eliminar el bucket de artefactos (no pertenece al stack)
aws s3 rb "s3://${ARTIFACT_BUCKET}" --force --region "$AWS_REGION"

# Comprobar que no quedaron grupos de logs (debe salir vacío)
aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/ecoalert-" \
  --region "$AWS_REGION" --query "logGroups[].logGroupName" --output text
```

El bucket del sitio se vacía automáticamente al borrar el stack (recurso personalizado `SiteConfig`), y los grupos de logs de ambas funciones (`/aws/lambda/ecoalert-analyze` y `/aws/lambda/ecoalert-site-config`) están declarados en la plantilla, así que también se eliminan. Opcionalmente, elimina los correos verificados:

```bash
aws ses delete-identity --identity tu-correo@ejemplo.com --region us-east-1
```

## Subir a GitHub

1. En github.com crea un repositorio vacío (sin README ni .gitignore), por ejemplo `ecoalert-ai`.
2. Desde la carpeta del proyecto:

```bash
setopt interactivecomments
cd "/Users/rdsroldn/Documents/AWS SCD 2026 - SBG IPN /EcoAlert-Demo"
git init
git add .

# Revisa la lista: NO deben aparecer packaged.yaml, frontend/config.js ni .agents/
git status

git commit -m "EcoAlert AI: demo serverless con Comprehend, Bedrock y SES"
git branch -M main
git remote add origin https://github.com/<tu-usuario>/ecoalert-ai.git
git push -u origin main
```

La presentación `.pptx` se incluye en el repositorio por defecto como material del curso. Si no quieres subirla, agrega una línea `*.pptx` al archivo `.gitignore` antes de `git add .`.
