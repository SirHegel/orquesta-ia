import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import types
import unittest
from unittest import mock

import orqlib


def cargar_cli_orq():
    ruta = os.path.join(os.path.dirname(orqlib.__file__), "orq")
    cargador = importlib.machinery.SourceFileLoader("orq_cli_regresiones", ruta)
    spec = importlib.util.spec_from_loader(cargador.name, cargador)
    modulo = importlib.util.module_from_spec(spec)
    cargador.exec_module(modulo)
    return modulo


ORQ_CLI = cargar_cli_orq()


def tarea(tid, depende=None, tipo="code"):
    return {
        "id": tid,
        "titulo": f"Tarea {tid}",
        "tipo": tipo,
        "depende": list(depende or []),
        "instruccion": f"Completa {tid}",
        "archivos": [f"src/{tid}.py"],
    }


def resultado_modelo(pid, rc=0, texto="listo"):
    return {
        "perfil": pid,
        "texto": texto,
        "tokens": 7,
        "seg": 0.1,
        "rc": rc,
        "run_id": f"run-{pid}",
    }


class RastreadorConcurrencia:
    """Hace observable el solape sin bloquear una implementacion serial."""

    def __init__(self):
        self.lock = threading.Lock()
        self.activos = 0
        self.max_activos = 0
        self.llamadas = 0
        self.solape = threading.Event()

    def __call__(self, pid, _perfil, _prompt, _tarea, _timeout, _carpeta=None,
                 **_kwargs):
        with self.lock:
            self.llamadas += 1
            numero = self.llamadas
            self.activos += 1
            self.max_activos = max(self.max_activos, self.activos)
            if self.activos > 1:
                self.solape.set()
        if numero == 1:
            self.solape.wait(0.15)
        with self.lock:
            self.activos -= 1
        return resultado_modelo(pid)


class EjecucionProyectoTests(unittest.TestCase):
    def test_escritores_de_perfiles_distintos_tambien_son_exclusivos(self):
        perfiles = {
            "claude-a": {"provider": "claude"},
            "codex-b": {"provider": "gpt"},
        }
        plan = {
            "nombre": "sin colisiones",
            "resumen": "dos piezas",
            "tareas": [tarea("t1"), tarea("t2")],
        }
        rastreador = RastreadorConcurrencia()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "cfg", return_value={"profiles": perfiles}
        ), mock.patch.object(
            orqlib, "disponibles", return_value=perfiles
        ), mock.patch.object(
            orqlib, "autenticado", return_value=True
        ), mock.patch.object(
            orqlib, "bloqueado", return_value=None
        ), mock.patch.object(orqlib, "correr", side_effect=rastreador):
            resultados = orqlib.ejecutar_proyecto(
                plan, tmp, asignacion={"t1": "claude-a", "t2": "codex-b"}
            )

        self.assertEqual(len(resultados), 2)
        self.assertEqual(rastreador.llamadas, 2)
        self.assertEqual(rastreador.max_activos, 1)

    def test_dos_tareas_del_mismo_perfil_no_corren_simultaneamente(self):
        perfil = {"provider": "claude"}
        plan = {
            "nombre": "serial por perfil",
            "resumen": "dos piezas independientes",
            "tareas": [tarea("t1"), tarea("t2")],
        }
        asignacion = {"t1": "claude-max20", "t2": "claude-max20"}
        rastreador = RastreadorConcurrencia()

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                orqlib,
                "cfg",
                return_value={"profiles": {"claude-max20": perfil}},
            ),
            mock.patch.object(
                orqlib, "disponibles", return_value={"claude-max20": perfil}
            ),
            mock.patch.object(orqlib, "autenticado", return_value=True),
            mock.patch.object(orqlib, "bloqueado", return_value=None),
            mock.patch.object(
                orqlib, "correr", side_effect=rastreador
            ) as correr,
        ):
            resultados = orqlib.ejecutar_proyecto(
                plan, tmp, asignacion=asignacion
            )

        self.assertEqual(len(resultados), 2)
        self.assertEqual(correr.call_count, 2)
        self.assertTrue(all(r["rc"] == 0 for r in resultados))
        self.assertEqual(rastreador.max_activos, 1)

    def test_fallo_no_desbloquea_dependientes_y_los_reporta_bloqueados(self):
        perfiles = {
            "constructor": {"provider": "claude"},
            "integrador": {"provider": "gpt"},
        }
        plan = {
            "nombre": "dependencias",
            "resumen": "la segunda necesita la primera",
            "tareas": [tarea("base"), tarea("dependiente", ["base"])],
        }
        ejecutadas = []

        def correr(pid, *_args, **_kwargs):
            ejecutadas.append(pid)
            if pid == "constructor":
                return resultado_modelo(pid, rc=1, texto="fallo la base")
            return resultado_modelo(pid)

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orqlib, "cfg", return_value={"profiles": perfiles}),
            mock.patch.object(orqlib, "disponibles", return_value=perfiles),
            mock.patch.object(orqlib, "autenticado", return_value=True),
            mock.patch.object(orqlib, "bloqueado", return_value=None),
            mock.patch.object(orqlib, "ranking", return_value=[]),
            mock.patch.object(orqlib, "correr", side_effect=correr),
        ):
            resultados = orqlib.ejecutar_proyecto(
                plan,
                tmp,
                asignacion={"base": "constructor", "dependiente": "integrador"},
            )

        por_id = {r["id"]: r for r in resultados}
        self.assertEqual(set(por_id), {"base", "dependiente"})
        self.assertNotEqual(por_id["base"]["rc"], 0)
        self.assertEqual(por_id["dependiente"]["rc"], 125)
        self.assertEqual(por_id["dependiente"].get("estado"), "bloqueado")
        self.assertEqual(ejecutadas, ["constructor"])

    def test_fallo_releva_a_otra_cuenta_y_entrega_el_estado_existente(self):
        perfiles = {
            "claude-a": {"provider": "claude"},
            "codex-b": {"provider": "gpt"},
        }
        plan = {
            "nombre": "relevo",
            "resumen": "continuidad entre modelos",
            "tareas": [tarea("base")],
        }
        prompts = []

        def correr(pid, _perfil, prompt, *_args, **_kwargs):
            prompts.append((pid, prompt))
            if pid == "claude-a":
                return resultado_modelo(pid, rc=124, texto="timeout con cambios")
            return resultado_modelo(pid, texto="audite el diff y termine")

        def ranking(_tipo, **_kwargs):
            return [
                {"pid": "claude-a", "p": perfiles["claude-a"]},
                {"pid": "codex-b", "p": perfiles["codex-b"]},
            ]

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orqlib, "cfg", return_value={"profiles": perfiles}),
            mock.patch.object(orqlib, "disponibles", return_value=perfiles),
            mock.patch.object(orqlib, "autenticado", return_value=True),
            mock.patch.object(orqlib, "bloqueado", return_value=None),
            mock.patch.object(orqlib, "ranking", side_effect=ranking),
            mock.patch.object(orqlib, "correr", side_effect=correr),
        ):
            resultados = orqlib.ejecutar_proyecto(
                plan, tmp, asignacion={"base": "claude-a"}
            )

        self.assertEqual([pid for pid, _ in prompts], ["claude-a", "codex-b"])
        self.assertIn("RELEVO CONTROLADO", prompts[1][1])
        self.assertIn("git diff", prompts[1][1])
        self.assertTrue(any(r.get("relevado") for r in resultados))
        finales = [r for r in resultados if not r.get("relevado")]
        self.assertEqual(len(finales), 1)
        self.assertEqual(finales[0]["perfil"], "codex-b")
        self.assertEqual(finales[0]["rc"], 0)

    def test_sin_cuentas_cada_tarea_devuelve_un_fallo_explicito(self):
        plan = {
            "nombre": "sin motores",
            "resumen": "no debe perder tareas",
            "tareas": [tarea("t1"), tarea("t2")],
        }

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orqlib, "cfg", return_value={"profiles": {}}),
            mock.patch.object(orqlib, "disponibles", return_value={}),
            mock.patch.object(orqlib, "ranking", return_value=[]),
            mock.patch.object(orqlib, "correr") as correr,
        ):
            resultados = orqlib.ejecutar_proyecto(plan, tmp)

        self.assertEqual({r["id"] for r in resultados}, {"t1", "t2"})
        self.assertTrue(all(r.get("rc", 0) != 0 for r in resultados))
        correr.assert_not_called()

    def test_prompt_de_tarea_no_se_presenta_a_si_misma_como_en_curso(self):
        perfiles = {
            "claude-a": {"provider": "claude"},
            "codex-b": {"provider": "gpt"},
        }
        plan = {
            "nombre": "prompts limpios",
            "resumen": "cada tarea ve solo a las otras",
            "tareas": [tarea("t1"), tarea("t2")],
        }
        capturados = {}

        def correr(pid, _perfil, prompt, *_args, **_kwargs):
            capturados[pid] = prompt
            return resultado_modelo(pid)

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orqlib, "cfg", return_value={"profiles": perfiles}),
            mock.patch.object(orqlib, "disponibles", return_value=perfiles),
            mock.patch.object(orqlib, "autenticado", return_value=True),
            mock.patch.object(orqlib, "bloqueado", return_value=None),
            mock.patch.object(orqlib, "correr", side_effect=correr),
        ):
            resultados = orqlib.ejecutar_proyecto(
                plan,
                tmp,
                asignacion={"t1": "claude-a", "t2": "codex-b"},
            )

        self.assertEqual(len(resultados), 2)
        for tid, pid in (("t1", "claude-a"), ("t2", "codex-b")):
            with self.subTest(tarea=tid, perfil=pid):
                antes_de_tarea = capturados[pid].split("\nTU TAREA:", 1)[0]
                self.assertNotIn(f"[{pid}] Tarea {tid}", antes_de_tarea)


class ValidacionPlanTests(unittest.TestCase):
    @staticmethod
    def plan_valido():
        return {
            "nombre": "plan-valido",
            "resumen": "validar antes de ejecutar",
            "tareas": [tarea("t1"), tarea("t2", ["t1"])],
        }

    def test_plan_valido_se_normaliza_sin_error(self):
        normalizado, error = orqlib.validar_plan(self.plan_valido())

        self.assertIsNone(error)
        self.assertIsNotNone(normalizado)
        self.assertEqual([t["id"] for t in normalizado["tareas"]], ["t1", "t2"])

    def test_ids_duplicados_y_depende_string_se_rechazan_explicitamente(self):
        duplicado = self.plan_valido()
        duplicado["tareas"][1]["id"] = "t1"
        depende_string = self.plan_valido()
        depende_string["tareas"][1]["depende"] = "t1"

        for plan, pista in ((duplicado, "duplic"), (depende_string, "depende")):
            with self.subTest(pista=pista):
                normalizado, error = orqlib.validar_plan(plan)
                self.assertIsNone(normalizado)
                self.assertIsInstance(error, str)
                self.assertIn(pista, error.lower())

    def test_campos_obligatorios_ausentes_o_vacios_se_rechazan(self):
        for campo in ("id", "titulo", "instruccion"):
            for modo in ("ausente", "vacio"):
                with self.subTest(campo=campo, modo=modo):
                    plan = self.plan_valido()
                    if modo == "ausente":
                        plan["tareas"][0].pop(campo)
                    else:
                        plan["tareas"][0][campo] = ""

                    normalizado, error = orqlib.validar_plan(plan)

                    self.assertIsNone(normalizado)
                    self.assertIsInstance(error, str)
                    self.assertIn(campo, error.lower())

    def test_archivo_compartido_solo_es_valido_si_hay_orden_por_dependencia(self):
        paralelo = self.plan_valido()
        paralelo["tareas"][0]["archivos"] = ["src/shared.py"]
        paralelo["tareas"][1]["archivos"] = ["src/shared.py"]
        paralelo["tareas"][1]["depende"] = []
        secuencial = self.plan_valido()
        secuencial["tareas"][0]["archivos"] = ["src/shared.py"]
        secuencial["tareas"][1]["archivos"] = ["src/shared.py"]

        normalizado, error = orqlib.validar_plan(paralelo)
        self.assertIsNone(normalizado)
        self.assertIn("comparten", error.lower())
        normalizado, error = orqlib.validar_plan(secuencial)
        self.assertIsNone(error)
        self.assertIsNotNone(normalizado)

    def test_tarea_escritora_sin_archivos_se_rechaza(self):
        plan = self.plan_valido()
        plan["tareas"][0]["archivos"] = []
        normalizado, error = orqlib.validar_plan(plan)
        self.assertIsNone(normalizado)
        self.assertIn("declarar", error.lower())

        plan["tareas"][0]["tipo"] = "research"
        normalizado, error = orqlib.validar_plan(plan)
        self.assertIsNone(error)
        self.assertIsNotNone(normalizado)

    def test_planificar_no_repara_ni_devuelve_un_plan_invalido(self):
        perfil = {"provider": "claude"}
        invalido = self.plan_valido()
        invalido["tareas"][1]["id"] = "t1"
        respuesta = {
            **resultado_modelo("claude-max20"),
            "texto": json.dumps(invalido),
        }

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                orqlib, "_mejor_para", return_value=("claude-max20", perfil)
            ),
            mock.patch.object(orqlib, "correr", return_value=respuesta),
            mock.patch.object(
                orqlib, "_reparar_plan", wraps=orqlib._reparar_plan
            ) as reparar,
        ):
            plan, error = orqlib.planificar("construye", tmp)

        self.assertIsNone(plan)
        self.assertIsInstance(error, str)
        self.assertIn("duplic", error.lower())
        reparar.assert_not_called()


class AuditoriaProyectoTests(unittest.TestCase):
    def setUp(self):
        self.fuertes = [
            {"pid": "claude-a", "p": {"provider": "claude"}},
            {"pid": "codex-b", "p": {"provider": "gpt"}},
        ]
        self.resultados = [
            {"perfil": "claude-a", "titulo": "backend"},
            {"perfil": "codex-b", "titulo": "pruebas"},
        ]
        self.plan = {"resumen": "construir y verificar"}

    def test_auditoria_que_arregla_serializa_a_los_escritores(self):
        rastreador = RastreadorConcurrencia()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "_mas_potentes", return_value=self.fuertes
        ), mock.patch.object(orqlib, "correr", side_effect=rastreador):
            salidas, error = orqlib.auditar_proyecto(
                self.plan, tmp, self.resultados, arreglar=True
            )

        self.assertIsNone(error)
        self.assertEqual(len(salidas), 2)
        self.assertEqual(rastreador.max_activos, 1)

    def test_auditoria_solo_lectura_puede_correr_en_paralelo(self):
        rastreador = RastreadorConcurrencia()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "_mas_potentes", return_value=self.fuertes
        ), mock.patch.object(orqlib, "correr", side_effect=rastreador):
            salidas, error = orqlib.auditar_proyecto(
                self.plan, tmp, self.resultados, arreglar=False
            )

        self.assertIsNone(error)
        self.assertEqual(len(salidas), 2)
        self.assertEqual(rastreador.max_activos, 2)


class PreferenciaProyectoTests(unittest.TestCase):
    def test_ranking_coloca_primero_el_perfil_preferido_si_es_elegible(self):
        perfiles = {
            "router-primero": {"provider": "gpt"},
            "preferido": {"provider": "claude"},
        }

        def puntuar(pid, _perfil, _tarea):
            return ((100 if pid == "router-primero" else 10), "prueba")

        with mock.patch.object(
            orqlib, "disponibles", return_value=perfiles
        ), mock.patch.object(orqlib, "puntuar", side_effect=puntuar):
            filas = orqlib.ranking("agentic", preferir="preferido")

        self.assertEqual(filas[0]["pid"], "preferido")

    def test_planificar_propaga_preferencia_al_selector(self):
        perfil = {"provider": "claude"}
        observada = []

        def mejor(tarea_, preferir=None):
            observada.append((tarea_, preferir))
            return preferir, perfil

        plan_json = json.dumps(
            {
                "nombre": "preferencias",
                "resumen": "plan",
                "tareas": [tarea("t1", tipo="agentic")],
            }
        )
        respuesta = {**resultado_modelo("claude-max20"), "texto": plan_json}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "_mejor_para", side_effect=mejor
        ), mock.patch.object(
            orqlib, "correr", return_value=respuesta
        ) as correr, mock.patch.object(
            orqlib, "_reparar_plan", side_effect=lambda plan, _n: (plan, None)
        ), mock.patch.object(orqlib, "disponibles", return_value={"x": perfil}):
            plan, error = orqlib.planificar(
                "termina el proyecto", tmp, preferir="claude-max20"
            )

        self.assertIsNone(error)
        self.assertEqual(plan["_planificador"], "claude-max20")
        self.assertEqual(observada, [("agentic", "claude-max20")])
        self.assertEqual(correr.call_count, 1)
        self.assertEqual(correr.call_args.kwargs.get("carpeta"), tmp)

    def test_ejecutar_sin_asignacion_prioriza_el_perfil_indicado(self):
        perfil = {"provider": "claude"}
        observada = []

        def ranking(tarea_, *args, **kwargs):
            observada.append(kwargs.get("preferir"))
            return [{"pid": "claude-max20", "p": perfil, "pts": 1}]

        plan = {
            "nombre": "preferencias",
            "resumen": "ejecucion",
            "tareas": [tarea("t1")],
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                orqlib,
                "cfg",
                return_value={"profiles": {"claude-max20": perfil}},
            ),
            mock.patch.object(
                orqlib, "disponibles", return_value={"claude-max20": perfil}
            ),
            mock.patch.object(orqlib, "ranking", side_effect=ranking),
            mock.patch.object(
                orqlib,
                "correr",
                return_value=resultado_modelo("claude-max20"),
            ),
        ):
            resultados = orqlib.ejecutar_proyecto(
                plan, tmp, preferir="claude-max20"
            )

        self.assertEqual(resultados[0]["perfil"], "claude-max20")
        self.assertEqual(observada, ["claude-max20"])

    def test_integrar_propaga_preferencia_al_selector(self):
        perfil = {"provider": "claude"}
        observada = []

        def mejor(tarea_, preferir=None):
            observada.append((tarea_, preferir))
            return preferir, perfil

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "_mejor_para", side_effect=mejor
        ), mock.patch.object(
            orqlib,
            "correr",
            return_value=resultado_modelo("claude-max20"),
        ) as correr:
            cierre = orqlib.integrar_proyecto(
                {"nombre": "preferencias"},
                tmp,
                [],
                preferir="claude-max20",
            )

        self.assertEqual(cierre["perfil"], "claude-max20")
        self.assertEqual(observada, [("review", "claude-max20")])
        self.assertEqual(correr.call_args.kwargs.get("carpeta"), tmp)


class VerificacionProyectoTests(unittest.TestCase):
    def test_verificador_exige_comandos_reales_rc_cero_y_sin_hallazgos(self):
        perfiles = [{"pid": "auditor", "p": {"provider": "gpt"}}]
        evidencia = resultado_modelo(
            "auditor",
            texto=json.dumps({
                "ok": True,
                "comprobaciones": [
                    {"comando": "python3 -m unittest", "rc": 0, "resultado": "12 OK"},
                    {"comando": "git diff --check", "rc": 0, "resultado": "limpio"},
                ],
                "hallazgos": [],
            }),
        )
        def ejecutar(comando, _carpeta, _timeout):
            return {"comando": comando, "rc": 0, "resultado": "OK real"}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "ranking", return_value=perfiles
        ), mock.patch.object(orqlib, "correr", return_value=evidencia), \
                mock.patch.object(
                    orqlib, "ejecutar_comando_verificacion", side_effect=ejecutar
                ) as ejecutor:
            salida = orqlib.verificar_proyecto(
                {"resumen": "terminar"}, tmp, []
            )

        self.assertTrue(salida["verificacion_ok"])
        self.assertEqual(len(salida["comprobaciones"]), 2)
        self.assertEqual(ejecutor.call_count, 2)

    def test_verificador_ignora_rc_inventado_y_ejecuta_el_comando(self):
        perfiles = [{"pid": "auditor", "p": {"provider": "gpt"}}]
        evidencia = resultado_modelo(
            "auditor",
            texto=json.dumps({
                "ok": True,
                "comprobaciones": [
                    {"comando": "git diff --check", "rc": 0,
                     "resultado": "el modelo dijo limpio"},
                ],
                "hallazgos": [],
            }),
        )
        fallo_real = {"comando": "git diff --check", "rc": 1,
                      "resultado": "error real"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "ranking", return_value=perfiles
        ), mock.patch.object(orqlib, "correr", return_value=evidencia), \
                mock.patch.object(
                    orqlib, "ejecutar_comando_verificacion", return_value=fallo_real
                ) as ejecutar:
            salida = orqlib.verificar_proyecto(
                {"resumen": "terminar"}, tmp, []
            )

        ejecutar.assert_called_once()
        self.assertFalse(salida["verificacion_ok"])
        self.assertEqual(salida["comprobaciones"][0]["rc"], 1)
        self.assertTrue(any("fallo real rc=1" in h for h in salida["hallazgos"]))

    def test_comando_arbitrario_no_se_ejecuta_aunque_el_modelo_diga_rc_cero(self):
        self.assertIsNone(
            orqlib._argv_verificacion("git diff --check --output=/tmp/escape")
        )
        evidencia = resultado_modelo(
            "auditor",
            texto=json.dumps({
                "ok": True,
                "comprobaciones": [
                    {"comando": "false", "rc": 0, "resultado": "inventado"}
                ],
                "hallazgos": [],
            }),
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "ranking",
            return_value=[{"pid": "auditor", "p": {"provider": "gpt"}}],
        ), mock.patch.object(
            orqlib, "correr", return_value=evidencia
        ), mock.patch.object(
            orqlib, "ejecutar_comando_verificacion"
        ) as ejecutar:
            salida = orqlib.verificar_proyecto({"resumen": "terminar"}, tmp, [])

        ejecutar.assert_not_called()
        self.assertFalse(salida["verificacion_ok"])
        self.assertIn("no permitida", " ".join(salida["hallazgos"]))

    def test_verificacion_invalida_se_releva_a_otro_modelo(self):
        perfiles = [
            {"pid": "claude-a", "p": {"provider": "claude"}},
            {"pid": "codex-b", "p": {"provider": "gpt"}},
        ]
        invalida = resultado_modelo("claude-a", texto="todo bien")
        valida = resultado_modelo(
            "codex-b",
            texto=json.dumps({
                "ok": False,
                "comprobaciones": [
                    {"comando": "pytest", "rc": 1, "resultado": "1 fallo"}
                ],
                "hallazgos": ["falla una prueba"],
            }),
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "ranking", return_value=perfiles
        ), mock.patch.object(
            orqlib, "correr", side_effect=[invalida, valida]
        ) as correr, mock.patch.object(
            orqlib, "ejecutar_comando_verificacion",
            return_value={"comando": "pytest", "rc": 1,
                          "resultado": "1 fallo real"},
        ):
            salida = orqlib.verificar_proyecto(
                {"resumen": "terminar"}, tmp, []
            )

        self.assertEqual(correr.call_count, 2)
        self.assertEqual(salida["perfil"], "codex-b")
        self.assertFalse(salida["verificacion_ok"])
        self.assertEqual(salida["hallazgos"][0], "falla una prueba")
        self.assertTrue(any("fallo real rc=1" in h for h in salida["hallazgos"]))


class CliImagenTests(unittest.TestCase):
    def test_generacion_visual_bloquea_la_carpeta_destino(self):
        perfil = {"provider": "antigravity"}
        resultado = {
            "perfil": "gemini-antigravity",
            "texto": "sin archivo en esta simulacion",
            "tokens": 0,
            "seg": 0.1,
            "rc": 0,
            "run_id": "run-imagen-simulada",
            "archivos": [],
        }
        with tempfile.TemporaryDirectory() as tmp, (
            mock.patch.object(
                ORQ_CLI.L,
                "ranking",
                return_value=[{"pid": "gemini-antigravity", "p": perfil}],
            )
        ), mock.patch.object(
            ORQ_CLI.L,
            "bloqueo_proyecto",
            return_value=contextlib.nullcontext(),
        ) as bloqueo, mock.patch.object(
            ORQ_CLI.L, "generar_imagen", return_value=resultado
        ) as generar, contextlib.redirect_stdout(io.StringIO()):
            ORQ_CLI.cmd_imagen(types.SimpleNamespace(
                prompt="crea un logo",
                perfil=None,
                en=tmp,
                nombre=None,
                timeout=420,
            ))

        bloqueo.assert_called_once_with(tmp)
        generar.assert_called_once()
        self.assertEqual(generar.call_args.args[3], tmp)


class CliProyectoTests(unittest.TestCase):
    def test_parser_de_proyecto_reconoce_preferir(self):
        ruta = os.path.join(os.path.dirname(orqlib.__file__), "orq")
        proceso = subprocess.run(
            [ruta, "proyecto", "--help"], capture_output=True, text=True, timeout=10
        )

        self.assertEqual(proceso.returncode, 0)
        self.assertIn("--preferir", proceso.stdout)
        self.assertIn("--rondas", proceso.stdout)
        self.assertIn("--sin-verificar", proceso.stdout)

    def test_cualquier_resultado_fallido_cierra_como_incompleto_y_exit_1(self):
        plan = {
            "nombre": "proyecto fallido",
            "resumen": "debe informar el fallo",
            "_planificador": "claude-max20",
            "_tokens_plan": 3,
            "_aviso": None,
            "tareas": [tarea("t1")],
        }
        args = types.SimpleNamespace(
            prompt="construye",
            en=None,
            contexto=None,
            timeout=30,
            preferir="claude-max20",
            sin_deliberar=True,
            si=True,
            sin_auditar=True,
            sin_arreglar=False,
            sin_integrar=True,
        )
        fallo = {
            "id": "t1",
            "titulo": "Tarea t1",
            "perfil": "claude-max20",
            "tipo": "code",
            "rc": 1,
            "tokens": 7,
            "seg": 0.1,
            "texto": "fallo",
            "run_id": "run-fallo",
        }
        salida = io.StringIO()

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(args, "en", tmp),
            mock.patch.object(ORQ_CLI.L, "planificar", return_value=(plan, None)),
            mock.patch.object(
                ORQ_CLI.L,
                "ranking",
                return_value=[{"pid": "claude-max20", "p": {}}],
            ),
            mock.patch.object(
                ORQ_CLI.L, "ejecutar_proyecto", return_value=[fallo]
            ),
            contextlib.redirect_stdout(salida),
        ):
            try:
                ORQ_CLI.cmd_proyecto(args)
            except SystemExit as exc:
                codigo = exc.code
            else:
                codigo = None

        limpia = ORQ_CLI.RE_ANSI.sub("", salida.getvalue())
        self.assertEqual(
            (codigo, "INCOMPLETO" in limpia, "LISTO" in limpia),
            (1, True, False),
        )

    def test_texto_de_auditoria_llega_al_integrador_final(self):
        plan = {
            "nombre": "proyecto auditado",
            "resumen": "conservar evidencia de auditoria",
            "_planificador": "claude-max20",
            "_tokens_plan": 3,
            "_aviso": None,
            "tareas": [tarea("t1")],
        }
        args = types.SimpleNamespace(
            prompt="construye y audita",
            en=None,
            contexto=None,
            timeout=30,
            preferir="claude-max20",
            sin_deliberar=True,
            si=True,
            sin_auditar=False,
            sin_arreglar=False,
            sin_integrar=False,
            publicar=False,
        )
        construccion = {
            "id": "t1",
            "titulo": "Tarea t1",
            "perfil": "claude-max20",
            "tipo": "code",
            "rc": 0,
            "tokens": 7,
            "seg": 0.1,
            "texto": "codigo terminado",
            "run_id": "run-construccion",
        }
        auditoria = {
            "perfil": "codex-auditor",
            "texto": "sin defectos pendientes",
            "tokens": 5,
            "seg": 0.1,
            "rc": 0,
            "run_id": "run-auditoria",
        }
        recibidos = []

        def integrar(_plan, _carpeta, resultados, *_args, **_kwargs):
            recibidos.extend(resultados)
            return resultado_modelo("claude-max20", texto="cierre listo")

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(args, "en", tmp),
            mock.patch.object(ORQ_CLI.L, "planificar", return_value=(plan, None)),
            mock.patch.object(
                ORQ_CLI.L,
                "ranking",
                return_value=[{"pid": "claude-max20", "p": {}}],
            ),
            mock.patch.object(
                ORQ_CLI.L,
                "disponibles",
                return_value={"claude-max20": {}, "codex-auditor": {}},
            ),
            mock.patch.object(
                ORQ_CLI.L, "ejecutar_proyecto", return_value=[construccion]
            ),
            mock.patch.object(
                ORQ_CLI.L,
                "auditar_proyecto",
                return_value=([auditoria], None),
            ),
            mock.patch.object(
                ORQ_CLI.L, "integrar_proyecto", side_effect=integrar
            ) as integrar_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            ORQ_CLI.cmd_proyecto(args)

        integrar_mock.assert_called_once()
        auditorias = [
            r for r in recibidos if r.get("titulo", "").startswith("auditoria")
        ]
        self.assertEqual(len(auditorias), 1)
        self.assertEqual(auditorias[0].get("texto"), "sin defectos pendientes")


if __name__ == "__main__":
    unittest.main()
