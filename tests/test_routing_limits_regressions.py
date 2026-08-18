import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

import orqlib


class CapacidadesProveedorTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {
            "gemini-antigravity": {
                "provider": "antigravity",
                "weights": {tarea: 100 for tarea in orqlib.TAREAS},
            },
            "claude-polidinamica": {
                "provider": "claude",
                "weights": {tarea: 10 for tarea in orqlib.TAREAS},
            },
            "codex-personal": {
                "provider": "gpt",
                "weights": {tarea: 9 for tarea in orqlib.TAREAS},
            },
        }

    def test_antigravity_solo_admite_imagen(self):
        perfil = self.profiles["gemini-antigravity"]

        self.assertTrue(orqlib.admite_tarea(perfil, "imagen"))
        for tarea in set(orqlib.TAREAS) - {"imagen"}:
            with self.subTest(tarea=tarea):
                self.assertFalse(orqlib.admite_tarea(perfil, tarea))
        adulterado = {"provider": "antigravity", "allowed_tasks": ["reasoning"]}
        self.assertFalse(orqlib.admite_tarea(adulterado, "reasoning"))

    def test_rankings_textuales_excluyen_antigravity_aunque_tenga_mayor_peso(self):
        parches = (
            mock.patch.object(orqlib, "cfg", return_value={"profiles": self.profiles}),
            mock.patch.object(orqlib, "autenticado", return_value=True),
            mock.patch.object(orqlib, "bloqueado", return_value=None),
            mock.patch.object(orqlib, "scores", return_value={}),
            mock.patch.object(orqlib, "cuota", return_value={}),
            mock.patch.object(
                orqlib,
                "cuota_global",
                return_value={"pct": None, "fuente": "sin dato", "edad_h": None},
            ),
        )
        with parches[0], parches[1], parches[2], parches[3], parches[4], parches[5]:
            for tarea in set(orqlib.TAREAS) - {"imagen"}:
                with self.subTest(tarea=tarea):
                    ids = [fila["pid"] for fila in orqlib.ranking(tarea)]
                    self.assertNotIn("gemini-antigravity", ids)
                    self.assertIn("claude-polidinamica", ids)
                    self.assertIn("codex-personal", ids)

    def test_sesion_global_antigravity_no_se_duplica_por_aliases(self):
        perfiles = {
            "visual-uno": {"provider": "antigravity"},
            "visual-dos": {"provider": "antigravity"},
        }
        with mock.patch.object(orqlib, "cfg", return_value={"profiles": perfiles}), mock.patch.object(
            orqlib, "autenticado", return_value=True
        ), mock.patch.object(orqlib, "bloqueado", return_value=None):
            disponibles = orqlib.disponibles(tarea="imagen")
        self.assertEqual(list(disponibles), ["visual-uno"])

    def test_alias_antigravity_no_elude_el_limite_de_otro_alias(self):
        perfiles = {
            "visual-uno": {"provider": "antigravity"},
            "visual-dos": {"provider": "antigravity"},
        }
        futuro = datetime.datetime.now() + datetime.timedelta(hours=1)
        limites = {
            orqlib.CLAVE_LIMITE_ANTIGRAVITY: {
                "bloqueado_hasta": futuro.isoformat()
            }
        }
        with mock.patch.object(orqlib, "cfg", return_value={"profiles": perfiles}), mock.patch.object(
            orqlib, "limites", return_value=limites
        ), mock.patch.object(orqlib, "autenticado", return_value=True):
            disponibles = orqlib.disponibles(tarea="imagen")
        self.assertEqual(disponibles, {})

    def test_pesos_arbitrarios_no_reemplazan_la_potencia_efectiva(self):
        perfiles = {
            "claude-opus": {
                "provider": "claude",
                "weights": {"reasoning": -1000},
            },
            "codex-potente": {
                "provider": "gpt",
                "weights": {"reasoning": 10**12},
            },
            "motor-visual": {
                "provider": "antigravity",
                "weights": {"reasoning": 10**30, "imagen": -1000},
            },
        }

        self.assertEqual(
            orqlib.potencia_perfil(perfiles["claude-opus"], "reasoning"),
            orqlib.POTENCIA_BASE["claude"],
        )
        self.assertEqual(
            orqlib.potencia_perfil(perfiles["codex-potente"], "reasoning"),
            orqlib.POTENCIA_BASE["gpt"],
        )
        self.assertEqual(
            orqlib.potencia_perfil(perfiles["motor-visual"], "imagen"),
            orqlib.POTENCIA_BASE["antigravity"],
        )

        with (
            mock.patch.object(orqlib, "cfg", return_value={"profiles": perfiles}),
            mock.patch.object(orqlib, "autenticado", return_value=True),
            mock.patch.object(orqlib, "bloqueado", return_value=None),
            mock.patch.object(orqlib, "scores", return_value={}),
            mock.patch.object(orqlib, "cuota", return_value={}),
            mock.patch.object(
                orqlib,
                "cuota_global",
                return_value={"pct": None, "fuente": "sin dato", "edad_h": None},
            ),
        ):
            ranking = orqlib.ranking("reasoning")

        puntos = {fila["pid"]: fila["pts"] for fila in ranking}
        self.assertEqual(puntos["claude-opus"], puntos["codex-potente"])
        self.assertNotIn("motor-visual", puntos)


class ErrorAntigravityTests(unittest.TestCase):
    def test_error_json_conserva_429_y_reset_relativo_se_parsea_completo(self):
        payload = {
            "response": "",
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "Quota exhausted. Resets in 4h19m46s",
            },
        }

        texto, tokens, diagnostico = orqlib.extraer(
            "antigravity", json.dumps(payload), ""
        )
        preservado = f"{texto}\n{diagnostico}"
        self.assertEqual(tokens, 0)
        self.assertIn("429", preservado)
        self.assertIn("RESOURCE_EXHAUSTED", preservado)
        self.assertIn("Resets in 4h19m46s", preservado)

        instante = datetime.datetime(2026, 8, 18, 12, 0, 0)
        esperado = instante + datetime.timedelta(hours=4, minutes=19, seconds=46)
        with tempfile.TemporaryDirectory() as tmp, (
            mock.patch.object(orqlib, "LIMITS", os.path.join(tmp, "limits.json"))
        ), mock.patch.object(orqlib, "LOCK", os.path.join(tmp, ".lock")), (
            mock.patch.object(orqlib, "ahora", return_value=instante)
        ):
            hasta = orqlib.detectar_limite(
                "gemini-antigravity",
                {"provider": "antigravity", "ventana_horas": 5},
                diagnostico,
            )
            with open(orqlib.LIMITS, encoding="utf-8") as archivo:
                guardado = json.load(archivo)[orqlib.CLAVE_LIMITE_ANTIGRAVITY]

        self.assertEqual(hasta, esperado)
        self.assertEqual(
            guardado["bloqueado_hasta"], esperado.isoformat(timespec="seconds")
        )

    def test_error_estructurado_con_exit_cero_se_convierte_en_fallo(self):
        payload = json.dumps(
            {
                "status": "ERROR",
                "response": "No puedo continuar por cuota",
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Quota reached",
                },
            }
        )
        proceso = mock.Mock(stdout=payload, stderr="", returncode=0)
        limite = datetime.datetime(2026, 8, 18, 16, 0, 0)
        perfil = {"provider": "antigravity", "allowed_tasks": ["imagen"]}

        with mock.patch.object(orqlib, "comando", return_value=["agy"]), mock.patch.object(
            orqlib, "entorno", return_value={}
        ), mock.patch.object(orqlib.subprocess, "run", return_value=proceso), mock.patch.object(
            orqlib, "detectar_limite", return_value=limite
        ), mock.patch.object(orqlib, "log"):
            resultado = orqlib.correr(
                "gemini-antigravity", perfil, "genera", tarea="imagen"
            )

        self.assertEqual(resultado["rc"], 1)
        self.assertIn("RESOURCE_EXHAUSTED", resultado["texto"])
        self.assertEqual(resultado["limitado"], limite.isoformat(timespec="seconds"))


class DiagnosticoCodexTests(unittest.TestCase):
    def test_respuesta_exitosa_no_bloquea_por_un_429_mencionado_en_stderr(self):
        proceso = mock.Mock(
            stdout="Respuesta correcta",
            stderr="Revisa el archivo en la linea 429\ntokens used\n12",
            returncode=0,
        )
        perfil = {"provider": "gpt"}

        with mock.patch.object(orqlib, "comando", return_value=["codex"]), mock.patch.object(
            orqlib, "entorno", return_value={}
        ), mock.patch.object(orqlib, "_lock_proveedor", return_value=None), mock.patch.object(
            orqlib.subprocess, "run", return_value=proceso
        ), mock.patch.object(orqlib, "detectar_limite") as detectar, mock.patch.object(
            orqlib, "log"
        ):
            resultado = orqlib.correr("codex-personal", perfil, "revisa", "reasoning")

        self.assertEqual(resultado["rc"], 0)
        self.assertIsNone(resultado["limitado"])
        detectar.assert_not_called()


class CuotaManualTests(unittest.TestCase):
    def test_medicion_manual_caducada_queda_sin_pct_y_no_penaliza(self):
        instante = datetime.datetime(2026, 8, 18, 14, 0, 0)
        perfil = {
            "provider": "claude",
            "ventana_horas": 5,
            "weights": {"writing": 10},
        }
        caducada = {
            "claude-polidinamica": {
                "usado_pct": 78.0,
                "declarado": (instante - datetime.timedelta(hours=5, seconds=1))
                .isoformat(timespec="minutes"),
                "nota": "lectura anterior",
            }
        }

        with mock.patch.object(orqlib, "ahora", return_value=instante), mock.patch.object(
            orqlib, "cuota_manual_leer", return_value=caducada
        ):
            cuota = orqlib.cuota_global("claude-polidinamica", perfil)

        self.assertIsNone(cuota["pct"])
        self.assertEqual(cuota["fuente"], "caducado")

        comunes = (
            mock.patch.object(
                orqlib,
                "cfg",
                return_value={"profiles": {"claude-polidinamica": perfil}},
            ),
            mock.patch.object(orqlib, "autenticado", return_value=True),
            mock.patch.object(orqlib, "bloqueado", return_value=None),
            mock.patch.object(orqlib, "scores", return_value={}),
            mock.patch.object(
                orqlib, "cuota", return_value={"fuente": "local", "mensajes": 0}
            ),
            mock.patch.object(orqlib, "ahora", return_value=instante),
        )
        with comunes[0], comunes[1], comunes[2], comunes[3], comunes[4], comunes[5]:
            with mock.patch.object(orqlib, "cuota_manual_leer", return_value=caducada):
                puntos_caducada, _ = orqlib.puntuar(
                    "claude-polidinamica", perfil, "writing"
                )
            with mock.patch.object(orqlib, "cuota_manual_leer", return_value={}):
                puntos_sin_dato, _ = orqlib.puntuar(
                    "claude-polidinamica", perfil, "writing"
                )

        self.assertEqual(puntos_caducada, puntos_sin_dato)

    def test_cuota_global_manda_sobre_local_y_local_agotada_tambien_bloquea(self):
        perfil = {"provider": "claude", "ventana_horas": 5, "cupo_ventana": 100}
        local = {
            "fuente": "local",
            "usado_pct": 120,
            "facturable": 120,
            "mensajes": 1,
            "ventana_horas": 5,
        }
        comunes = (
            mock.patch.object(orqlib, "cfg", return_value={"profiles": {"claude-x": perfil}}),
            mock.patch.object(orqlib, "autenticado", return_value=True),
            mock.patch.object(orqlib, "bloqueado", return_value=None),
            mock.patch.object(orqlib, "scores", return_value={}),
            mock.patch.object(orqlib, "cuota", return_value=local),
            mock.patch.object(orqlib, "gastado_ventana", return_value=0),
        )
        with comunes[0], comunes[1], comunes[2], comunes[3], comunes[4], comunes[5]:
            with mock.patch.object(
                orqlib, "cuota_global", return_value={"pct": 99, "fuente": "declarado"}
            ):
                declarado, _ = orqlib.puntuar("claude-x", perfil, "reasoning")
            with mock.patch.object(
                orqlib, "cuota_global", return_value={"pct": None, "fuente": "sin dato"}
            ):
                agotado_local, _ = orqlib.puntuar("claude-x", perfil, "reasoning")

        self.assertEqual(declarado, 0)
        self.assertEqual(agotado_local, 0)


if __name__ == "__main__":
    unittest.main()
