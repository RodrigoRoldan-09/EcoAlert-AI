"""Pruebas unitarias de backend/app.py con boto3 simulado (sin red ni AWS).

Ejecutar desde la raíz del proyecto:
    python3 -m unittest discover -s tests -v
"""

import base64
import json
import math
import os
import sys
import threading
import unittest
from unittest import mock

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "backend"))

os.environ["TABLE_NAME"] = "test-table"
os.environ["SENDER_EMAIL"] = "alertas@example.com"
os.environ["BEDROCK_MODEL_ID"] = "test-model"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

# boto3 viene incluido en el runtime de Lambda, pero puede no estar instalado
# en la máquina local. Las pruebas reemplazan app.client, así que basta con
# módulos vacíos para poder importar app.py sin instalar nada.
try:
    import boto3  # noqa: F401
    import botocore.config  # noqa: F401
except ImportError:
    import types

    _boto3 = types.ModuleType("boto3")
    _boto3.client = mock.MagicMock(
        side_effect=AssertionError("No se deben crear clientes reales"))
    _botocore = types.ModuleType("botocore")
    _botocore_config = types.ModuleType("botocore.config")
    _botocore_config.Config = mock.MagicMock()
    _botocore.config = _botocore_config
    sys.modules["boto3"] = _boto3
    sys.modules["botocore"] = _botocore
    sys.modules["botocore.config"] = _botocore_config

import logging  # noqa: E402

import app  # noqa: E402

# Silencia los logs esperados de los casos de fallo simulados.
logging.disable(logging.CRITICAL)

TEXTO_ALTA = ("Una fábrica está vertiendo residuos químicos al río Lerma, en "
              "Toluca; hay peces muertos y un olor muy fuerte.")


def evento(texto=None, body=None, base64_body=False):
    if body is None:
        body = json.dumps({"texto": texto}, ensure_ascii=False)
    if base64_body:
        body = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "version": "2.0",
        "routeKey": "POST /report",
        "requestContext": {"http": {"method": "POST", "path": "/report"}},
        "headers": {"content-type": "application/json"},
        "body": body,
        "isBase64Encoded": base64_body,
    }


def respuesta_bedrock(texto):
    return {
        "output": {"message": {"role": "assistant",
                               "content": [{"text": texto}]}},
        "usage": {"inputTokens": 186, "outputTokens": 54, "totalTokens": 240},
    }


def json_bedrock(gravedad, ubicacion="Río Lerma, Toluca",
                 justificacion="Vertido químico con daño visible a la fauna."):
    return json.dumps({"gravedad": gravedad, "ubicacion": ubicacion,
                       "justificacion": justificacion}, ensure_ascii=False)


class FakeClients(object):
    def __init__(self, negativo=0.97, sentimiento="NEGATIVE",
                 bedrock_texto=None, ubicaciones=("río Lerma", "Toluca")):
        self.comprehend = mock.MagicMock()
        self.comprehend.detect_sentiment.return_value = {
            "Sentiment": sentimiento,
            "SentimentScore": {"Positive": 0.01, "Negative": negativo,
                               "Neutral": 0.01, "Mixed": 0.01},
        }
        self.comprehend.detect_entities.return_value = {
            "Entities": [{"Type": "LOCATION", "Text": u, "Score": 0.95}
                         for u in ubicaciones]
            + [{"Type": "ORGANIZATION", "Text": "fábrica", "Score": 0.9}],
        }

        self.bedrock = mock.MagicMock()
        self.bedrock.converse.return_value = respuesta_bedrock(
            bedrock_texto if bedrock_texto is not None else json_bedrock("ALTA"))

        self.ses = mock.MagicMock()
        self.ses.get_paginator.return_value.paginate.return_value = [
            {"Identities": ["a@example.com", "b@example.com",
                            "alertas@example.com"]}]
        self.ses.get_identity_verification_attributes.return_value = {
            "VerificationAttributes": {
                "a@example.com": {"VerificationStatus": "Success"},
                "b@example.com": {"VerificationStatus": "Pending"},
                "alertas@example.com": {"VerificationStatus": "Success"},
            }}

        self.dynamodb = mock.MagicMock()
        self.mapa = {"comprehend": self.comprehend,
                     "bedrock-runtime": self.bedrock,
                     "ses": self.ses, "dynamodb": self.dynamodb}

    def patch(self):
        return mock.patch.object(app, "client",
                                 side_effect=lambda n: self.mapa[n])


def invocar(fakes, ev):
    with fakes.patch():
        resp = app.handler(ev, None)
    return resp, json.loads(resp["body"]) if resp["body"] else None


class HandlerTests(unittest.TestCase):

    def test_reporte_alta_envia_correo(self):
        fakes = FakeClients(negativo=0.97)
        resp, body = invocar(fakes, evento(TEXTO_ALTA))

        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["comprehend"], "ALTA")
        self.assertEqual(body["bedrock"], "ALTA")
        self.assertEqual(body["gravedadFinal"], "ALTA")
        self.assertIs(body["alertaEnviada"], True)
        self.assertEqual(body["destinatarios"], 2)
        self.assertEqual(body["ubicacion"], "Río Lerma, Toluca")
        self.assertEqual(body["sentimiento"], "NEGATIVE")
        self.assertEqual(body["tokens"],
                         {"entrada": 186, "salida": 54, "total": 240})
        self.assertEqual(body["unidadesComprehend"],
                         math.ceil(len(TEXTO_ALTA) / 100))
        self.assertIn("comprehend", body["segundos"])
        self.assertIn("bedrock", body["segundos"])
        self.assertEqual(body["errores"], {})

        fakes.comprehend.detect_sentiment.assert_called_once_with(
            Text=TEXTO_ALTA, LanguageCode="es")
        self.assertEqual(fakes.ses.send_email.call_count, 2)
        destinos = [c.kwargs["Destination"]["ToAddresses"]
                    for c in fakes.ses.send_email.call_args_list]
        self.assertNotIn(["b@example.com"], destinos)
        llamada = fakes.ses.send_email.call_args_list[0].kwargs
        self.assertEqual(llamada["Source"], "alertas@example.com")
        html_body = llamada["Message"]["Body"]["Html"]["Data"]
        self.assertIn("Gravedad ALTA", html_body)
        self.assertIn(body["ticketId"], html_body)
        self.assertIn("EcoAlert AI · Alerta ambiental", html_body)

        fakes.dynamodb.put_item.assert_called_once()
        item_kwargs = fakes.dynamodb.put_item.call_args.kwargs
        self.assertEqual(item_kwargs["TableName"], "test-table")
        item = item_kwargs["Item"]
        self.assertEqual(item["gravedadFinal"], {"S": "ALTA"})
        self.assertEqual(item["tokensBedrock"], {"N": "240"})
        self.assertEqual(item["alertaEnviada"], {"BOOL": True})

        prompt = fakes.bedrock.converse.call_args.kwargs[
            "messages"][0]["content"][0]["text"]
        self.assertIn("Eres un analista ambiental.", prompt)
        self.assertIn(TEXTO_ALTA, prompt)

    def test_reporte_media_no_envia_correo(self):
        fakes = FakeClients(negativo=0.6, bedrock_texto=json_bedrock("MEDIA"))
        resp, body = invocar(fakes, evento("Queman basura en un baldío."))

        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["comprehend"], "MEDIA")
        self.assertEqual(body["gravedadFinal"], "MEDIA")
        self.assertIs(body["alertaEnviada"], False)
        fakes.ses.send_email.assert_not_called()
        fakes.dynamodb.put_item.assert_called_once()

    def test_fallo_bedrock_usa_comprehend(self):
        fakes = FakeClients(negativo=0.9)
        fakes.bedrock.converse.side_effect = Exception("boom")
        resp, body = invocar(fakes, evento(TEXTO_ALTA))

        self.assertEqual(resp["statusCode"], 200)
        self.assertIsNone(body["bedrock"])
        self.assertEqual(body["comprehend"], "ALTA")
        self.assertEqual(body["gravedadFinal"], "ALTA")
        self.assertIn("bedrock", body["errores"])
        self.assertIsNone(body["segundos"]["bedrock"])
        self.assertEqual(body["tokens"]["total"], 0)
        self.assertEqual(body["ubicacion"], "río Lerma, Toluca")
        self.assertTrue(body["alertaEnviada"])
        self.assertEqual(fakes.ses.send_email.call_count, 2)
        item = fakes.dynamodb.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["gravedadBedrock"], {"S": "N/D"})

    def test_respuesta_bedrock_con_bloque_json(self):
        texto = ("Aquí tienes el análisis:\n```json\n"
                 + json_bedrock("alta", "Río Lerma",
                                "Riesgo para la salud y la fauna.")
                 + "\n```\nEspero que sirva.")
        fakes = FakeClients(negativo=0.3, bedrock_texto=texto)
        resp, body = invocar(fakes, evento(TEXTO_ALTA))

        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["bedrock"], "ALTA")
        self.assertEqual(body["comprehend"], "BAJA")
        self.assertEqual(body["gravedadFinal"], "ALTA")
        self.assertEqual(body["ubicacion"], "Río Lerma")
        self.assertEqual(body["justificacion"],
                         "Riesgo para la salud y la fauna.")

    def test_entrada_invalida_400(self):
        casos = {
            "vacio": evento(""),
            "espacios": evento("   \n  "),
            "sin_texto": evento(body=json.dumps({"otro": "x"})),
            "no_json": evento(body="esto no es json"),
            "texto_no_cadena": evento(body=json.dumps({"texto": 123})),
            "muy_largo": evento("a" * 2001),
        }
        for nombre, ev in casos.items():
            with self.subTest(caso=nombre):
                fakes = FakeClients()
                with mock.patch.object(app, "client") as cli:
                    resp = app.handler(ev, None)
                cli.assert_not_called()
                self.assertEqual(resp["statusCode"], 400)
                body = json.loads(resp["body"])
                self.assertIn("error", body)
                self.assertTrue(body["error"].startswith("El "))
                del fakes

    def test_fallo_ses_no_rompe(self):
        fakes = FakeClients()
        fakes.ses.send_email.side_effect = Exception("SES caído")
        resp, body = invocar(fakes, evento(TEXTO_ALTA))

        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["gravedadFinal"], "ALTA")
        self.assertIs(body["alertaEnviada"], False)
        self.assertIn("ses", body["errores"])
        fakes.dynamodb.put_item.assert_called_once()

    def test_fallo_listado_ses_no_rompe(self):
        fakes = FakeClients()
        fakes.ses.get_paginator.side_effect = Exception("AccessDenied")
        resp, body = invocar(fakes, evento(TEXTO_ALTA))
        self.assertEqual(resp["statusCode"], 200)
        self.assertIs(body["alertaEnviada"], False)
        self.assertIn("ses", body["errores"])

    def test_fallo_dynamodb_no_rompe(self):
        fakes = FakeClients(negativo=0.6, bedrock_texto=json_bedrock("MEDIA"))
        fakes.dynamodb.put_item.side_effect = Exception("throttled")
        resp, body = invocar(fakes, evento(TEXTO_ALTA))
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("dynamodb", body["errores"])

    def test_fallan_ambos_modelos_502(self):
        fakes = FakeClients()
        fakes.comprehend.detect_sentiment.side_effect = Exception("x")
        fakes.bedrock.converse.side_effect = Exception("y")
        resp, body = invocar(fakes, evento(TEXTO_ALTA))

        self.assertEqual(resp["statusCode"], 502)
        self.assertIn("error", body)
        self.assertIn("comprehend", body["errores"])
        self.assertIn("bedrock", body["errores"])
        fakes.ses.send_email.assert_not_called()
        fakes.dynamodb.put_item.assert_not_called()

    def test_body_base64(self):
        fakes = FakeClients()
        resp, body = invocar(fakes, evento(TEXTO_ALTA, base64_body=True))
        self.assertEqual(resp["statusCode"], 200)
        fakes.comprehend.detect_sentiment.assert_called_once_with(
            Text=TEXTO_ALTA, LanguageCode="es")

    def test_options_204(self):
        ev = {"requestContext": {"http": {"method": "OPTIONS"}}}
        with mock.patch.object(app, "client") as cli:
            resp = app.handler(ev, None)
        self.assertEqual(resp["statusCode"], 204)
        cli.assert_not_called()

    def test_clientes_se_crean_en_hilo_principal(self):
        fakes = FakeClients()
        hilos = {}

        def crear(nombre):
            hilos.setdefault(nombre, threading.current_thread())
            return fakes.mapa[nombre]

        with mock.patch.object(app, "client", side_effect=crear):
            resp = app.handler(evento(TEXTO_ALTA), None)
        self.assertEqual(resp["statusCode"], 200)
        principal = threading.current_thread()
        self.assertIs(hilos["comprehend"], principal)
        self.assertIs(hilos["bedrock-runtime"], principal)

    def test_texto_multibyte_se_recorta_para_comprehend(self):
        texto = "😀" * 2000  # 2000 caracteres, 8000 bytes UTF-8
        fakes = FakeClients()
        resp, body = invocar(fakes, evento(texto))
        self.assertEqual(resp["statusCode"], 200)
        enviado = fakes.comprehend.detect_sentiment.call_args.kwargs["Text"]
        self.assertLessEqual(len(enviado.encode("utf-8")), 5000)
        self.assertEqual(enviado, "😀" * 1250)
        self.assertEqual(
            fakes.comprehend.detect_entities.call_args.kwargs["Text"], enviado)
        prompt = fakes.bedrock.converse.call_args.kwargs[
            "messages"][0]["content"][0]["text"]
        self.assertIn(texto, prompt)
        self.assertEqual(body["errores"], {})

    def test_correo_escapa_html(self):
        fakes = FakeClients()
        texto = "Vertido <script>alert(1)</script> & humo en el río"
        invocar(fakes, evento(texto))
        html_body = fakes.ses.send_email.call_args_list[0].kwargs[
            "Message"]["Body"]["Html"]["Data"]
        self.assertNotIn("<script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)


class UtilidadesTests(unittest.TestCase):

    def test_umbrales_gravedad(self):
        self.assertEqual(app.gravedad_desde_sentimiento(0.85), "ALTA")
        self.assertEqual(app.gravedad_desde_sentimiento(0.849), "MEDIA")
        self.assertEqual(app.gravedad_desde_sentimiento(0.5), "MEDIA")
        self.assertEqual(app.gravedad_desde_sentimiento(0.49), "BAJA")
        self.assertEqual(app.gravedad_desde_sentimiento(0), "BAJA")

    def test_unidades_comprehend(self):
        self.assertEqual(app.unidades_comprehend("a"), 1)
        self.assertEqual(app.unidades_comprehend("a" * 100), 1)
        self.assertEqual(app.unidades_comprehend("a" * 101), 2)

    def test_limpiar_json(self):
        plano = '{"gravedad": "BAJA"}'
        self.assertEqual(app.limpiar_json(plano)["gravedad"], "BAJA")
        self.assertEqual(
            app.limpiar_json("```json\n" + plano + "\n```")["gravedad"], "BAJA")
        self.assertEqual(
            app.limpiar_json("```\n" + plano + "\n```")["gravedad"], "BAJA")
        self.assertEqual(
            app.limpiar_json("Resultado: " + plano + " fin")["gravedad"],
            "BAJA")
        with self.assertRaises(ValueError):
            app.limpiar_json("sin json aquí")
        with self.assertRaises(ValueError):
            app.limpiar_json("[1, 2]")

    def test_normalizar_gravedad(self):
        self.assertEqual(app.normalizar_gravedad("alta"), "ALTA")
        self.assertEqual(app.normalizar_gravedad(" Media "), "MEDIA")
        self.assertEqual(app.normalizar_gravedad("Gravedad: BAJA"), "BAJA")
        self.assertIsNone(app.normalizar_gravedad("crítica"))
        self.assertIsNone(app.normalizar_gravedad(None))

    def test_ubicacion_valida(self):
        self.assertTrue(app.ubicacion_valida("Toluca"))
        for v in (None, "", "null", "No especificada", "Sin ubicación", "N/A"):
            self.assertFalse(app.ubicacion_valida(v), v)

    def test_recortar_bytes(self):
        self.assertEqual(app.recortar_bytes("río Lerma"), "río Lerma")
        self.assertEqual(app.recortar_bytes("ñ" * 3, max_bytes=5), "ññ")
        self.assertEqual(app.recortar_bytes("a" * 6000), "a" * 5000)

    def test_timeouts_bedrock_dentro_del_limite(self):
        with mock.patch.object(app, "Config") as cfg, \
                mock.patch.object(app, "boto3") as b3, \
                mock.patch.dict(app._CLIENTES, {}, clear=True):
            app.client("bedrock-runtime")
        kwargs = cfg.call_args.kwargs
        self.assertLessEqual(kwargs["read_timeout"], 12)
        self.assertLessEqual(kwargs["connect_timeout"], 3)
        self.assertEqual(kwargs["retries"]["max_attempts"], 1)
        b3.client.assert_called_once_with("bedrock-runtime",
                                          config=cfg.return_value)

    def test_formatear_fecha(self):
        self.assertEqual(app.formatear_fecha("2026-11-12T10:42:05Z"),
                         "12 nov 2026 · 10:42 UTC")


if __name__ == "__main__":
    unittest.main()
