"""EcoAlert AI - Lambda principal (POST /report).

Recibe un reporte de contaminación ambiental, lo clasifica en paralelo con
Amazon Comprehend y Amazon Bedrock (Claude, API Converse), guarda un ticket en
DynamoDB y, si la gravedad final es ALTA, envía una alerta por correo (Amazon
SES) a todas las identidades de correo verificadas.

Reglas documentadas:
- Comprehend: gravedad según el puntaje NEGATIVE de DetectSentiment
  (>= UMBRAL_ALTA -> ALTA, >= UMBRAL_MEDIA -> MEDIA, resto -> BAJA).
- Gravedad final: la de Bedrock si respondió; si no, la de Comprehend.
- Alerta: solo cuando la gravedad final es ALTA.
- Fallos de Bedrock, SES o DynamoDB no rompen la respuesta (se reportan en
  "errores"). Si fallan ambos modelos se responde 502.
"""

import base64
import concurrent.futures
import html
import json
import logging
import math
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

NIVELES = ("BAJA", "MEDIA", "ALTA")
UMBRAL_ALTA = 0.85
UMBRAL_MEDIA = 0.50
UMBRAL_ENTIDAD = 0.5
MAX_DESTINATARIOS = 50
# Límite de las API síncronas de Comprehend (bytes UTF-8 por texto).
MAX_BYTES_COMPREHEND = 5000
MODELO_POR_DEFECTO = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

PROMPT_BASE = (
    "Eres un analista ambiental. Clasifica la gravedad del reporte como BAJA, "
    "MEDIA o ALTA, extrae la ubicación si existe y justifica en una frase. "
    "Responde solo en JSON"
    ' con las claves "gravedad", "ubicacion" y "justificacion".'
    "\n\nReporte: {texto}"
)

MESES = ["ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic"]

UBICACIONES_VACIAS = {
    "", "null", "none", "n/a", "na", "n/d", "nd", "no", "-",
    "no especificada", "no especificado", "no indicada", "no indicado",
    "desconocida", "desconocido", "sin ubicacion", "ninguna", "ninguno",
    "no identificada", "no disponible",
}

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CLIENTES = {}


def client(name):
    """Devuelve (y cachea) un cliente boto3 con timeouts acotados.

    Bedrock usa un solo intento con read_timeout de 12 s para que Bedrock +
    SES + DynamoDB quepan en el límite de 29 s de API Gateway (Lambda: 30 s);
    si Bedrock tarda más, se responde con la gravedad de Comprehend.
    """
    if name not in _CLIENTES:
        if name == "bedrock-runtime":
            cfg = Config(read_timeout=12, connect_timeout=3,
                         retries={"max_attempts": 1, "mode": "standard"})
        else:
            cfg = Config(read_timeout=5, connect_timeout=3,
                         retries={"max_attempts": 2, "mode": "standard"})
        _CLIENTES[name] = boto3.client(name, config=cfg)
    return _CLIENTES[name]


def config():
    """Configuración leída del entorno en tiempo de ejecución."""
    try:
        max_chars = int(os.environ.get("MAX_CHARS", "2000"))
    except ValueError:
        max_chars = 2000
    return {
        "tabla": os.environ.get("TABLE_NAME", ""),
        "remitente": os.environ.get("SENDER_EMAIL", ""),
        "modelo": os.environ.get("BEDROCK_MODEL_ID") or MODELO_POR_DEFECTO,
        "max_chars": max_chars,
    }


def respuesta(status, body=None):
    resp = {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
    }
    resp["body"] = "" if body is None else json.dumps(body, ensure_ascii=False)
    return resp


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------

def extraer_texto(event, max_chars):
    """Devuelve (texto, mensaje_error)."""
    if not isinstance(event, dict):
        return None, "El cuerpo de la petición debe ser JSON válido."

    if "body" in event:
        crudo = event.get("body")
        if crudo is None:
            crudo = ""
        if event.get("isBase64Encoded") and isinstance(crudo, str):
            try:
                crudo = base64.b64decode(crudo).decode("utf-8")
            except Exception:  # noqa: BLE001
                return None, "El cuerpo de la petición debe ser JSON válido."
        if isinstance(crudo, (bytes, bytearray)):
            crudo = crudo.decode("utf-8", errors="replace")
        if isinstance(crudo, dict):
            datos = crudo
        else:
            try:
                datos = json.loads(crudo) if crudo.strip() else {}
            except (ValueError, TypeError):
                return None, "El cuerpo de la petición debe ser JSON válido."
    else:
        datos = event

    if not isinstance(datos, dict):
        return None, "El cuerpo de la petición debe ser JSON válido."

    texto = datos.get("texto")
    if not isinstance(texto, str) or not texto.strip():
        return None, "El campo 'texto' es obligatorio y no puede estar vacío."
    texto = texto.strip()
    if len(texto) > max_chars:
        return None, "El reporte excede el máximo de %d caracteres." % max_chars
    return texto, None


# --------------------------------------------------------------------------
# Amazon Comprehend
# --------------------------------------------------------------------------

def gravedad_desde_sentimiento(score_negativo):
    score = float(score_negativo or 0)
    if score >= UMBRAL_ALTA:
        return "ALTA"
    if score >= UMBRAL_MEDIA:
        return "MEDIA"
    return "BAJA"


def unidades_comprehend(texto):
    """Unidades de 100 caracteres (mínimo 1) por llamada."""
    return max(1, int(math.ceil(len(texto) / 100.0)))


def recortar_bytes(texto, max_bytes=MAX_BYTES_COMPREHEND):
    """Recorta el texto a max_bytes en UTF-8 sin partir un carácter."""
    datos = texto.encode("utf-8")
    if len(datos) <= max_bytes:
        return texto
    return datos[:max_bytes].decode("utf-8", errors="ignore")


def analizar_comprehend(texto, cliente):
    texto = recortar_bytes(texto)
    inicio = time.perf_counter()
    sent = cliente.detect_sentiment(Text=texto, LanguageCode="es")
    ents = cliente.detect_entities(Text=texto, LanguageCode="es")
    segundos = time.perf_counter() - inicio

    negativo = float((sent.get("SentimentScore") or {}).get("Negative", 0) or 0)

    ubicaciones = []
    vistos = set()
    for ent in ents.get("Entities") or []:
        if ent.get("Type") != "LOCATION":
            continue
        if float(ent.get("Score", 0) or 0) < UMBRAL_ENTIDAD:
            continue
        valor = (ent.get("Text") or "").strip()
        clave = valor.lower()
        if valor and clave not in vistos:
            vistos.add(clave)
            ubicaciones.append(valor)

    return {
        "gravedad": gravedad_desde_sentimiento(negativo),
        "sentimiento": sent.get("Sentiment"),
        "puntajeNegativo": round(negativo, 4),
        "ubicacion": ", ".join(ubicaciones) if ubicaciones else None,
        "segundos": round(segundos, 2),
        "unidades": unidades_comprehend(texto),
    }


# --------------------------------------------------------------------------
# Amazon Bedrock
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def limpiar_json(texto):
    """Convierte la respuesta del modelo en dict (tolera ```json y prosa)."""
    if not isinstance(texto, str):
        raise ValueError("Respuesta vacía")
    limpio = _FENCE.sub("", texto.strip()).strip()
    try:
        datos = json.loads(limpio)
    except ValueError:
        ini, fin = texto.find("{"), texto.rfind("}")
        if ini == -1 or fin <= ini:
            raise ValueError("La respuesta no contiene JSON")
        datos = json.loads(texto[ini:fin + 1])
    if not isinstance(datos, dict):
        raise ValueError("La respuesta JSON no es un objeto")
    return datos


def _sin_acentos(valor):
    return "".join(c for c in unicodedata.normalize("NFD", valor)
                   if unicodedata.category(c) != "Mn")


def normalizar_gravedad(valor):
    if not isinstance(valor, str):
        return None
    limpio = _sin_acentos(valor).upper().strip()
    if limpio in NIVELES:
        return limpio
    for nivel in ("ALTA", "MEDIA", "BAJA"):
        if re.search(r"\b%s\b" % nivel, limpio):
            return nivel
    return None


def ubicacion_valida(valor):
    if not isinstance(valor, str):
        return False
    return _sin_acentos(valor).strip().lower() not in UBICACIONES_VACIAS


def analizar_bedrock(texto, model_id, cliente):
    inicio = time.perf_counter()
    resp = cliente.converse(
        modelId=model_id,
        messages=[{"role": "user",
                   "content": [{"text": PROMPT_BASE.format(texto=texto)}]}],
        inferenceConfig={"maxTokens": 300, "temperature": 0},
    )
    segundos = time.perf_counter() - inicio

    contenido = (((resp.get("output") or {}).get("message") or {})
                 .get("content") or [])
    salida = "".join(b.get("text", "") for b in contenido if isinstance(b, dict))
    datos = limpiar_json(salida)

    gravedad = normalizar_gravedad(datos.get("gravedad"))
    if gravedad is None:
        raise ValueError("Bedrock no devolvió una gravedad válida")

    uso = resp.get("usage") or {}
    entrada = int(uso.get("inputTokens", 0) or 0)
    salida_tokens = int(uso.get("outputTokens", 0) or 0)
    total = int(uso.get("totalTokens", 0) or 0) or entrada + salida_tokens

    ubicacion = datos.get("ubicacion")
    justificacion = datos.get("justificacion")
    return {
        "gravedad": gravedad,
        "ubicacion": ubicacion.strip() if ubicacion_valida(ubicacion) else None,
        "justificacion": justificacion.strip()
        if isinstance(justificacion, str) and justificacion.strip() else None,
        "segundos": round(segundos, 2),
        "tokens": {"entrada": entrada, "salida": salida_tokens, "total": total},
    }


# --------------------------------------------------------------------------
# Amazon SES
# --------------------------------------------------------------------------

def destinatarios_verificados():
    ses = client("ses")
    identidades = []
    for pagina in ses.get_paginator("list_identities").paginate(
            IdentityType="EmailAddress"):
        identidades.extend(pagina.get("Identities") or [])

    verificados = []
    for i in range(0, len(identidades), 100):
        lote = identidades[i:i + 100]
        attrs = ses.get_identity_verification_attributes(Identities=lote)
        estado = attrs.get("VerificationAttributes") or {}
        for ident in lote:
            if (estado.get(ident) or {}).get("VerificationStatus") == "Success":
                verificados.append(ident)
    return verificados[:MAX_DESTINATARIOS]


def formatear_fecha(iso):
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return str(iso)
    return "%d %s %d · %02d:%02d UTC" % (dt.day, MESES[dt.month - 1], dt.year,
                                         dt.hour, dt.minute)


def construir_correo(ticket):
    """Devuelve (asunto, html, texto_plano) de la alerta."""
    e = html.escape
    ubicacion = ticket.get("ubicacion") or "No identificada"
    fecha = formatear_fecha(ticket.get("fecha"))
    modelos = "Comprehend: %s · Bedrock: %s" % (
        ticket.get("gravedadComprehend") or "N/D",
        ticket.get("gravedadBedrock") or "N/D")
    justificacion = ticket.get("justificacion")

    asunto = ("[EcoAlert AI] Alerta ALTA: incidente de contaminación en %s"
              % ubicacion)

    filas = [
        ("Ubicación", ubicacion),
        ("Fecha", fecha),
        ("Descripción", ticket.get("reporte", "")),
    ]
    if justificacion:
        filas.append(("Justificación (Bedrock)", justificacion))
    filas.append(("Ticket", ticket.get("ticketId", "")))
    filas.append(("Modelos", modelos))

    filas_html = "".join(
        '<tr><td style="padding:10px 0;border-bottom:1px solid #DCE6DE;">'
        '<div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;'
        'color:#6B7A70;font-weight:bold;">%s</div>'
        '<div style="font-size:15px;color:#1B2B21;margin-top:4px;'
        'line-height:1.5;">%s</div></td></tr>' % (e(k), e(str(v)))
        for k, v in filas)

    cuerpo_html = (
        '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>EcoAlert AI · Alerta ambiental</title></head>'
        '<body style="margin:0;padding:0;background:#EEF7EE;'
        'font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        ' style="background:#EEF7EE;padding:24px 12px;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        ' style="max-width:600px;background:#FFFFFF;border-radius:12px;'
        'overflow:hidden;border:1px solid #DCE6DE;">'
        '<tr><td style="background:#2E7D32;color:#FFFFFF;padding:18px 24px;'
        'font-size:16px;font-weight:bold;">EcoAlert AI · Alerta ambiental</td></tr>'
        '<tr><td style="padding:24px;">'
        '<h1 style="margin:0 0 12px;font-size:22px;color:#1B2B21;">'
        'Incidente de contaminación — Gravedad ALTA</h1>'
        '<span style="display:inline-block;background:#C62828;color:#FFFFFF;'
        'font-size:12px;font-weight:bold;letter-spacing:1px;padding:6px 14px;'
        'border-radius:999px;">GRAVEDAD ALTA</span>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        ' style="margin-top:16px;">' + filas_html + '</table>'
        '<div style="margin-top:20px;background:#EEF7EE;border:1px solid #C8E6C9;'
        'border-radius:8px;padding:12px 16px;color:#2E7D32;font-weight:bold;'
        'font-size:14px;">Ver detalle del ticket: ' + e(ticket.get("ticketId", ""))
        + '</div></td></tr>'
        '<tr><td style="padding:14px 24px;background:#F7FBF7;color:#6B7A70;'
        'font-size:12px;border-top:1px solid #DCE6DE;">'
        'Mensaje automático · Amazon SES — EcoAlert AI</td></tr>'
        '</table></td></tr></table></body></html>'
    )

    texto_plano = "\n".join(
        ["EcoAlert AI · Alerta ambiental",
         "Incidente de contaminación — Gravedad ALTA", ""]
        + ["%s: %s" % (k, v) for k, v in filas]
        + ["", "Mensaje automático · Amazon SES — EcoAlert AI"])

    return asunto, cuerpo_html, texto_plano


def enviar_alertas(ticket, remitente):
    """Devuelve (enviada, numero_enviados, nota)."""
    if not remitente:
        raise ValueError("SENDER_EMAIL no está configurado")
    destinatarios = destinatarios_verificados()
    if not destinatarios:
        return False, 0, "No hay destinatarios verificados en SES."

    asunto, cuerpo_html, texto_plano = construir_correo(ticket)
    ses = client("ses")
    enviados = 0
    fallidos = 0
    for destino in destinatarios:
        try:
            ses.send_email(
                Source=remitente,
                Destination={"ToAddresses": [destino]},
                Message={
                    "Subject": {"Data": asunto, "Charset": "UTF-8"},
                    "Body": {
                        "Html": {"Data": cuerpo_html, "Charset": "UTF-8"},
                        "Text": {"Data": texto_plano, "Charset": "UTF-8"},
                    },
                },
            )
            enviados += 1
        except Exception as exc:  # noqa: BLE001
            fallidos += 1
            logger.warning("No se pudo enviar correo a un destinatario: %s",
                           type(exc).__name__)

    nota = None
    if fallidos:
        nota = "No se pudo enviar el correo a %d destinatario(s)." % fallidos
    return enviados > 0, enviados, nota


# --------------------------------------------------------------------------
# Amazon DynamoDB
# --------------------------------------------------------------------------

def _s(valor):
    return {"S": str(valor) if valor not in (None, "") else "N/D"}


def _n(valor):
    return {"N": str(valor if valor is not None else 0)}


def guardar_ticket(ticket, tabla):
    client("dynamodb").put_item(
        TableName=tabla,
        Item={
            "ticketId": {"S": ticket["ticketId"]},
            "fecha": _s(ticket["fecha"]),
            "reporte": _s(ticket["reporte"]),
            "ubicacion": _s(ticket["ubicacion"]),
            "gravedadComprehend": _s(ticket["gravedadComprehend"]),
            "gravedadBedrock": _s(ticket["gravedadBedrock"]),
            "gravedadFinal": _s(ticket["gravedadFinal"]),
            "sentimiento": _s(ticket["sentimiento"]),
            "justificacion": _s(ticket["justificacion"]),
            "segundosComprehend": _n(ticket["segundosComprehend"]),
            "segundosBedrock": _n(ticket["segundosBedrock"]),
            "tokensEntradaBedrock": _n(ticket["tokensEntradaBedrock"]),
            "tokensSalidaBedrock": _n(ticket["tokensSalidaBedrock"]),
            "tokensBedrock": _n(ticket["tokensBedrock"]),
            "unidadesComprehend": _n(ticket["unidadesComprehend"]),
            "alertaEnviada": {"BOOL": bool(ticket["alertaEnviada"])},
        },
        ConditionExpression="attribute_not_exists(ticketId)",
    )


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------

def handler(event, context):
    metodo = (((event or {}).get("requestContext") or {}).get("http") or {}) \
        .get("method") if isinstance(event, dict) else None
    if metodo == "OPTIONS":
        return respuesta(204)

    cfg = config()
    texto, error = extraer_texto(event, cfg["max_chars"])
    if error:
        return respuesta(400, {"error": error})

    ticket_id = str(uuid.uuid4())
    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    errores = {}

    # Los clientes se crean en este hilo: boto3.client() sobre la sesión por
    # defecto no es seguro entre hilos (arranque en frío).
    cli_comp = client("comprehend")
    cli_bed = client("bedrock-runtime")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_comp = pool.submit(analizar_comprehend, texto, cli_comp)
        fut_bed = pool.submit(analizar_bedrock, texto, cfg["modelo"], cli_bed)
        comp = bed = None
        try:
            comp = fut_comp.result()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo en Amazon Comprehend")
            errores["comprehend"] = ("Amazon Comprehend no respondió: %s"
                                     % type(exc).__name__)
        try:
            bed = fut_bed.result()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo en Amazon Bedrock")
            errores["bedrock"] = ("Amazon Bedrock no respondió: %s"
                                  % type(exc).__name__)

    if comp is None and bed is None:
        return respuesta(502, {
            "error": "No fue posible analizar el reporte con ningún modelo.",
            "errores": errores,
        })

    gravedad_comp = comp["gravedad"] if comp else None
    gravedad_bed = bed["gravedad"] if bed else None
    gravedad_final = gravedad_bed or gravedad_comp

    if bed and bed.get("ubicacion"):
        ubicacion = bed["ubicacion"]
    elif comp and comp.get("ubicacion"):
        ubicacion = comp["ubicacion"]
    else:
        ubicacion = "No identificada"

    if bed and bed.get("justificacion"):
        justificacion = bed["justificacion"]
    elif bed:
        justificacion = "Bedrock no incluyó una justificación."
    else:
        justificacion = "No disponible (Bedrock no respondió)."

    tokens = bed["tokens"] if bed else {"entrada": 0, "salida": 0, "total": 0}
    segundos = {
        "comprehend": comp["segundos"] if comp else None,
        "bedrock": bed["segundos"] if bed else None,
    }
    unidades = comp["unidades"] if comp else 0

    ticket = {
        "ticketId": ticket_id,
        "fecha": fecha,
        "reporte": texto,
        "ubicacion": ubicacion,
        "gravedadComprehend": gravedad_comp,
        "gravedadBedrock": gravedad_bed,
        "gravedadFinal": gravedad_final,
        "sentimiento": comp["sentimiento"] if comp else None,
        "justificacion": justificacion,
        "segundosComprehend": segundos["comprehend"],
        "segundosBedrock": segundos["bedrock"],
        "tokensEntradaBedrock": tokens["entrada"],
        "tokensSalidaBedrock": tokens["salida"],
        "tokensBedrock": tokens["total"],
        "unidadesComprehend": unidades,
        "alertaEnviada": False,
    }

    alerta_enviada = False
    destinatarios = 0
    if gravedad_final == "ALTA":
        try:
            alerta_enviada, destinatarios, nota = enviar_alertas(
                ticket, cfg["remitente"])
            if nota:
                errores["ses"] = nota
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo en Amazon SES")
            errores["ses"] = ("No se pudo enviar la alerta por correo: %s"
                              % type(exc).__name__)
    ticket["alertaEnviada"] = alerta_enviada

    try:
        guardar_ticket(ticket, cfg["tabla"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo en Amazon DynamoDB")
        errores["dynamodb"] = ("No se pudo guardar el ticket: %s"
                               % type(exc).__name__)

    cuerpo = {
        "ticketId": ticket_id,
        "fecha": fecha,
        "comprehend": gravedad_comp,
        "bedrock": gravedad_bed,
        "gravedadFinal": gravedad_final,
        "ubicacion": ubicacion,
        "justificacion": justificacion,
        "sentimiento": comp["sentimiento"] if comp else None,
        "puntajeNegativo": comp["puntajeNegativo"] if comp else None,
        "segundos": segundos,
        "tokens": tokens,
        "unidadesComprehend": unidades,
        "alertaEnviada": alerta_enviada,
        "destinatarios": destinatarios,
        "modeloBedrock": cfg["modelo"],
        "errores": errores,
    }

    logger.info(json.dumps({
        "evento": "reporte_procesado",
        "ticketId": ticket_id,
        "gravedadComprehend": gravedad_comp,
        "gravedadBedrock": gravedad_bed,
        "gravedadFinal": gravedad_final,
        "segundos": segundos,
        "tokens": tokens,
        "unidadesComprehend": unidades,
        "alertaEnviada": alerta_enviada,
        "errores": list(errores.keys()),
    }, ensure_ascii=False))

    return respuesta(200, cuerpo)
