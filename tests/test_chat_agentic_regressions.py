import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import orqchat
import orqlib


def candidato(pid, provider="claude"):
    return {"pid": pid, "p": {"provider": provider}}


def respuesta(pid, texto="trabajo terminado"):
    return {
        "perfil": pid,
        "texto": texto,
        "tokens": 25,
        "seg": 1.0,
        "rc": 0,
        "limitado": None,
    }


class TimeoutChatTests(unittest.TestCase):
    def test_timeout_tiene_default_configuracion_y_override_de_entorno(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            orqchat.L, "cfg", return_value={}
        ):
            self.assertEqual(orqchat.timeout_chat(), 1800)

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            orqchat.L, "cfg", return_value={"_chat_timeout": 2700}
        ):
            self.assertEqual(orqchat.timeout_chat(), 2700)

        with mock.patch.dict(
            os.environ, {"ORQ_CHAT_TIMEOUT": "3600"}, clear=True
        ), mock.patch.object(
            orqchat.L, "cfg", return_value={"_chat_timeout": 2700}
        ):
            self.assertEqual(orqchat.timeout_chat(), 3600)

    def test_timeout_configurable_se_acota_a_un_rango_seguro(self):
        for valor, esperado in (("1", 60), ("999999", 21600)):
            with self.subTest(valor=valor), mock.patch.dict(
                os.environ, {"ORQ_CHAT_TIMEOUT": valor}, clear=True
            ):
                self.assertEqual(orqchat.timeout_chat(), esperado)

    def test_responder_pasa_el_timeout_configurado_al_proveedor(self):
        perfil = {"provider": "claude"}
        exito = respuesta("claude-max20")

        with (
            mock.patch.object(
                orqchat.L,
                "ranking",
                return_value=[{"pid": "claude-max20", "p": perfil}],
            ),
            mock.patch.object(orqchat.L, "activas", return_value={}),
            mock.patch.object(orqchat.L, "correr", return_value=exito) as correr,
            mock.patch.object(orqchat, "timeout_chat", return_value=2345),
            mock.patch.object(orqchat, "marco_carpeta", return_value="MARCO\n"),
            mock.patch.object(orqchat, "con_contexto", return_value="PREGUNTA"),
            mock.patch.object(orqchat, "_estado_repo", return_value=(None, {})),
            mock.patch.object(orqchat, "limpiar_parcial"),
            mock.patch.object(
                orqchat,
                "Girador",
                side_effect=lambda _texto: contextlib.nullcontext(),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            resultado = orqchat.responder(
                "termina este cambio", [], tarea="agentic", forzado=None
            )

        self.assertIs(resultado, exito)
        args, kwargs = correr.call_args
        limite = kwargs.get("timeout", args[4] if len(args) > 4 else None)
        self.assertEqual(limite, 2345)


class TimeoutParcialTests(unittest.TestCase):
    def test_rc124_entrega_cambios_al_siguiente_modelo_y_continua(self):
        perfiles = [
            candidato("claude-juan"),
            candidato("codex-escritor", provider="gpt"),
        ]
        timeout = {
            "perfil": "claude-juan",
            "texto": "[ERROR rc=124] timeout tras 900s",
            "tokens": 1800,
            "seg": 900.1,
            "rc": 124,
            "limitado": None,
            "session_id": "sesion-juan",
        }
        exito = respuesta("codex-escritor", "auditado, probado y terminado")
        salida = io.StringIO()

        with (
            mock.patch.object(orqchat.L, "ranking", return_value=perfiles),
            mock.patch.object(orqchat.L, "activas", return_value={}),
            mock.patch.object(
                orqchat.L, "correr", side_effect=[timeout, exito]
            ) as correr,
            mock.patch.object(orqchat, "timeout_chat", return_value=900),
            mock.patch.object(orqchat, "marco_carpeta", return_value="MARCO\n"),
            mock.patch.object(orqchat, "con_contexto", return_value="PREGUNTA"),
            mock.patch.object(
                orqchat, "_estado_repo",
                side_effect=[("/repo", {}), ("/repo", {"bot.py": (2, 10)}),
                             ("/repo", {"bot.py": (2, 10)})],
            ),
            mock.patch.object(orqchat, "guardar_parcial"),
            mock.patch.object(
                orqchat,
                "Girador",
                side_effect=lambda _texto: contextlib.nullcontext(),
            ),
            contextlib.redirect_stdout(salida),
        ):
            resultado = orqchat.responder(
                "termina el bot", [], tarea="agentic", forzado=None
            )

        self.assertIs(resultado, exito)
        self.assertEqual(resultado["rc"], 0)
        self.assertEqual(correr.call_count, 2)
        self.assertEqual(correr.call_args_list[0].args[0], "claude-juan")
        self.assertEqual(correr.call_args_list[1].args[0], "codex-escritor")
        self.assertIn("RELEVO CONTROLADO", correr.call_args_list[1].args[2])
        self.assertIn("bot.py", correr.call_args_list[1].args[2])
        self.assertNotIn("ninguna cuenta pudo responder", salida.getvalue().lower())
        self.assertIn("relevo seguro", salida.getvalue().lower())

    def test_cuenta_forzada_no_se_releva_sin_permiso_implicito(self):
        timeout = {
            "perfil": "claude-juan", "texto": "[ERROR rc=124] timeout",
            "tokens": 1, "seg": 60, "rc": 124, "limitado": None,
        }
        with (
            mock.patch.object(
                orqchat.L, "disponibles",
                return_value={"claude-juan": {"provider": "claude"}},
            ),
            mock.patch.object(orqchat.L, "correr", return_value=timeout) as correr,
            mock.patch.object(orqchat, "timeout_chat", return_value=60),
            mock.patch.object(orqchat, "marco_carpeta", return_value=""),
            mock.patch.object(orqchat, "con_contexto", return_value="encargo"),
            mock.patch.object(orqchat, "_estado_repo", return_value=(None, {})),
            mock.patch.object(orqchat, "guardar_parcial"),
            mock.patch.object(orqchat, "Girador",
                              side_effect=lambda _t: contextlib.nullcontext()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            resultado = orqchat.responder(
                "termina", [], tarea="agentic", forzado="claude-juan"
            )

        self.assertEqual(resultado["estado"], "parcial")
        self.assertEqual(correr.call_count, 1)


class SeleccionCuentaTests(unittest.TestCase):
    def test_cuenta_activa_se_prefiere_entre_candidatos_del_router(self):
        juan = candidato("claude-juan")
        max20 = candidato("claude-max20")

        with mock.patch.dict(
            os.environ, {"ORQ_CHAT_PERFIL": ""}, clear=False
        ), mock.patch.object(
            orqchat.L, "ranking", return_value=[juan, max20]
        ), mock.patch.object(
            orqchat.L, "activas", return_value={"claude": "claude-max20"}
        ):
            elegidos = orqchat.candidatos_para("agentic")

        self.assertEqual([fila["pid"] for fila in elegidos], [
            "claude-max20",
            "claude-juan",
        ])

    def test_cuenta_forzada_prevalece_sobre_la_activa(self):
        perfiles = {
            "claude-juan": {"provider": "claude"},
            "claude-max20": {"provider": "claude"},
        }
        with mock.patch.object(
            orqchat.L, "disponibles", return_value=perfiles
        ) as disponibles, mock.patch.object(orqchat.L, "ranking") as ranking:
            elegidos = orqchat.candidatos_para(
                "agentic", forzado="claude-juan"
            )

        self.assertEqual([fila["pid"] for fila in elegidos], ["claude-juan"])
        disponibles.assert_called_once_with(
            incluir_bloqueados=True, tarea="agentic"
        )
        ranking.assert_not_called()


class IntencionYCarpetaTests(unittest.TestCase):
    def test_navegacion_jerarquica_elige_repos_y_no_la_repeticion_del_padre(self):
        with tempfile.TemporaryDirectory() as tmp:
            documentos = os.path.join(tmp, "Documentos")
            repos = os.path.join(documentos, "Repos")
            os.makedirs(repos)
            frase = (
                "metete a documentos, despues a repos que es la carpeta "
                "que esta adentro de documentos"
            )
            encontrada = orqchat.resolver_carpeta_mencionada(
                frase, actual=tmp, raices=[tmp]
            )

        self.assertEqual(encontrada, repos)

    def test_clasifica_consulta_accion_y_proyecto_en_lenguaje_natural(self):
        casos = {
            "explicame que es OAuth y para que sirve": "reasoning",
            "revisa la terminal de kitty y diagnostica por que falla": "agentic",
            "termina el bot y audita tantas veces hasta resolver todo": "proyecto",
        }
        for pregunta, esperado in casos.items():
            with self.subTest(pregunta=pregunta):
                self.assertEqual(orqchat.clasificar_intencion(pregunta), esperado)

    def test_arreglar_o_solucionar_un_proyecto_hasta_el_final_es_proyecto(self):
        casos = {
            "arregla el repo completo y no pares hasta que funcione": "proyecto",
            "soluciona el proyecto completo hasta resolver todos los fallos": "proyecto",
            "revisa los cambios del repositorio": "agentic",
        }
        for pregunta, esperado in casos.items():
            with self.subTest(pregunta=pregunta):
                self.assertEqual(orqchat.clasificar_intencion(pregunta), esperado)

    def test_variantes_naturales_de_reintento_recuperan_el_encargo_anterior(self):
        anterior = "termina sincategorematico-bot y ejecuta sus pruebas"
        contexto = [{"rol": "u", "txt": anterior}]

        for reintento in (
            "vuelve a intentarlo por favor",
            "intenta de nuevo por favor",
            "inténtalo nuevamente",
        ):
            with self.subTest(reintento=reintento):
                operativa = orqchat.pregunta_operativa(reintento, contexto)
                self.assertTrue(orqchat._es_reintento(reintento))
                self.assertTrue(operativa.startswith(anterior))

    def test_navegacion_pura_se_distingue_de_una_orden_de_trabajo(self):
        self.assertTrue(
            orqchat.es_navegacion_pura(
                "metete a documentos, despues a repos"
            )
        )
        self.assertFalse(
            orqchat.es_navegacion_pura(
                "metete a documentos, despues a repos y revisa el bot"
            )
        )
        for consulta in (
            "ve si el bot funciona",
            "ve qué errores hay",
            "entra en detalle",
        ):
            with self.subTest(consulta=consulta):
                self.assertFalse(orqchat.es_navegacion_pura(consulta))

    def test_principal_resuelve_navegacion_pura_sin_invocar_ninguna_ia(self):
        with tempfile.TemporaryDirectory() as tmp:
            destino = os.path.join(tmp, "Documentos", "Repos")
            os.makedirs(destino)
            with (
                mock.patch.object(orqchat, "CARPETA", tmp),
                mock.patch.object(orqchat, "readline", None),
                mock.patch("builtins.input", side_effect=[
                    "metete a documentos, despues a repos",
                    "/salir",
                ]),
                mock.patch.object(orqchat, "limpiar_sesiones_viejas"),
                mock.patch.object(orqchat, "logo"),
                mock.patch.object(orqchat, "cabecera"),
                mock.patch.object(orqchat, "cargar_ctx", return_value=[]),
                mock.patch.object(orqchat, "guardar_ctx"),
                mock.patch.object(orqchat, "guardar_carpeta"),
                mock.patch.object(orqchat, "quitar_logo"),
                mock.patch.object(
                    orqchat,
                    "resolver_carpeta_mencionada",
                    return_value=destino,
                ),
                mock.patch.object(orqchat.os, "chdir") as chdir,
                mock.patch.object(orqchat, "responder") as responder,
                mock.patch.object(
                    orqchat, "ejecutar_proyecto_chat"
                ) as ejecutar_proyecto,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                orqchat.principal()

        self.assertEqual(chdir.call_args_list[-1], mock.call(destino))
        responder.assert_not_called()
        ejecutar_proyecto.assert_not_called()

    def test_principal_no_hace_shortcut_de_navegacion_si_no_resolvio_ruta(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orqchat, "CARPETA", tmp),
            mock.patch.object(orqchat, "readline", None),
            mock.patch("builtins.input", side_effect=[
                "metete a la carpeta que te dije",
                "/salir",
            ]),
            mock.patch.object(orqchat, "limpiar_sesiones_viejas"),
            mock.patch.object(orqchat, "logo"),
            mock.patch.object(orqchat, "cabecera"),
            mock.patch.object(orqchat, "cargar_ctx", return_value=[]),
            mock.patch.object(orqchat, "guardar_ctx"),
            mock.patch.object(orqchat, "quitar_logo"),
            mock.patch.object(
                orqchat, "resolver_carpeta_mencionada", return_value=None
            ),
            mock.patch.object(orqchat, "es_navegacion_pura", return_value=True),
            mock.patch.object(orqchat.os, "chdir"),
            mock.patch.object(orqchat, "responder", return_value=None) as responder,
            mock.patch.object(orqchat, "ejecutar_proyecto_chat") as proyecto,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            orqchat.principal()

        responder.assert_called_once()
        proyecto.assert_not_called()

    def test_resuelve_la_carpeta_nombrada_sin_exigir_cd_ni_mayusculas_exactas(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(
                tmp, "Documentos", "Repos", "Sincategorematico-Bot"
            )
            os.makedirs(os.path.join(repo, ".git"))
            encontrada = orqchat.resolver_carpeta_mencionada(
                "metete a documentos, despues a repos y luego a "
                "sincategorematico-bot",
                actual=tmp,
                raices=[tmp],
            )

        self.assertEqual(encontrada, repo)

    def test_frases_genericas_no_cambian_la_carpeta_actual(self):
        with tempfile.TemporaryDirectory() as tmp:
            actual = os.path.join(tmp, "trabajo-activo")
            os.makedirs(actual)
            for nombre in ("proyecto", "tests", "web", "orquesta"):
                os.makedirs(os.path.join(tmp, "copias", nombre))

            for pregunta in (
                "termina el proyecto actual",
                "revisa los tests",
                "abre la web",
                "soluciona orquesta",
            ):
                with self.subTest(pregunta=pregunta):
                    encontrada = orqchat.resolver_carpeta_mencionada(
                        pregunta, actual=actual, raices=[tmp]
                    )
                    self.assertIsNone(encontrada)

    def test_slug_distintivo_unico_si_resuelve_la_carpeta(self):
        with tempfile.TemporaryDirectory() as tmp:
            actual = os.path.join(tmp, "trabajo-activo")
            repo = os.path.join(tmp, "Repos", "sincategorematico-bot")
            os.makedirs(actual)
            os.makedirs(os.path.join(repo, ".git"))

            encontrada = orqchat.resolver_carpeta_mencionada(
                "entra a sincategorematico-bot y termina el bot",
                actual=actual,
                raices=[tmp],
            )

        self.assertEqual(encontrada, repo)

    def test_slug_duplicado_es_ambiguo_y_no_elige_una_copia_al_azar(self):
        with tempfile.TemporaryDirectory() as tmp:
            actual = os.path.join(tmp, "trabajo-activo")
            os.makedirs(actual)
            for contenedor in ("personal", "empresa"):
                os.makedirs(
                    os.path.join(
                        tmp, contenedor, "sincategorematico-bot", ".git"
                    )
                )

            encontrada = orqchat.resolver_carpeta_mencionada(
                "revisa sincategorematico-bot",
                actual=actual,
                raices=[tmp],
            )

        self.assertIsNone(encontrada)


class SesionesClaudeTests(unittest.TestCase):
    def test_modos_solo_lectura_no_heredan_permisos_totales(self):
        with mock.patch.dict(os.environ, {"ORQ_PERMISOS_TOTALES": "1"}):
            claude = orqlib.comando(
                {"provider": "claude"}, "audita", solo_lectura=True
            )
            codex = orqlib.comando(
                {"provider": "gpt"}, "audita", solo_lectura=True
            )

        self.assertNotIn("--dangerously-skip-permissions", claude)
        self.assertIn("plan", claude)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", codex)
        self.assertIn("read-only", codex)

    def test_permisos_totales_son_opt_in_y_el_entorno_local_tiene_prioridad(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            orqlib, "cfg", return_value={}
        ):
            self.assertFalse(orqlib.permisos_activos())
        with mock.patch.dict(
            os.environ, {"ORQ_PERMISOS_TOTALES": "1"}, clear=True
        ), mock.patch.object(orqlib, "cfg", return_value={"_permisos_totales": False}):
            self.assertTrue(orqlib.permisos_activos())

    def test_chrome_solo_se_activa_en_el_perfil_que_lo_autorizo(self):
        with mock.patch.object(
            orqlib, "permisos_activos", return_value=False
        ), mock.patch.object(orqlib, "potencia_maxima", return_value=False):
            normal = orqlib.comando({"provider": "claude"}, "revisa la web")
            navegador = orqlib.comando(
                {"provider": "claude", "chrome": True}, "revisa la web"
            )

        self.assertNotIn("--chrome", normal)
        self.assertIn("--chrome", navegador)

    def test_chrome_auto_espera_a_que_la_extension_oficial_exista(self):
        with mock.patch.object(
            orqlib, "permisos_activos", return_value=False
        ), mock.patch.object(
            orqlib, "potencia_maxima", return_value=False
        ), mock.patch.object(
            orqlib, "chrome_claude_instalado", side_effect=[False, True]
        ):
            antes = orqlib.comando(
                {"provider": "claude", "chrome": "auto"}, "revisa la web"
            )
            despues = orqlib.comando(
                {"provider": "claude", "chrome": "auto"}, "revisa la web"
            )

        self.assertNotIn("--chrome", antes)
        self.assertIn("--chrome", despues)

    def test_comando_claude_distingue_sesion_nueva_de_reanudacion(self):
        perfil = {"provider": "claude", "model": "claude-opus-prueba"}
        with mock.patch.object(
            orqlib, "permisos_activos", return_value=False
        ), mock.patch.object(orqlib, "potencia_maxima", return_value=False):
            nueva = orqlib.comando(
                perfil, "continua", session_id="sesion-nueva", resume=False
            )
            existente = orqlib.comando(
                perfil, "continua", session_id="sesion-existente", resume=True
            )

        self.assertIn("--session-id", nueva)
        self.assertEqual(nueva[nueva.index("--session-id") + 1], "sesion-nueva")
        self.assertNotIn("--resume", nueva)
        self.assertIn("--resume", existente)
        self.assertEqual(
            existente[existente.index("--resume") + 1], "sesion-existente"
        )
        self.assertNotIn("--session-id", existente)

    def test_correr_claude_nuevo_guarda_session_id_en_resultado_y_ledger(self):
        perfil = {"provider": "claude"}
        proceso = mock.Mock(
            stdout=json.dumps(
                {
                    "result": "listo",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            ),
            stderr="",
            returncode=0,
        )

        with (
            mock.patch.object(orqlib.uuid, "uuid4", return_value="sesion-nueva"),
            mock.patch.object(orqlib, "entorno", return_value={}),
            mock.patch.object(orqlib, "comando", return_value=["claude"]) as comando,
            mock.patch.object(orqlib, "_lock_proveedor", return_value=None),
            mock.patch.object(orqlib.subprocess, "run", return_value=proceso),
            mock.patch.object(orqlib, "log") as log,
        ):
            resultado = orqlib.correr(
                "claude-max20", perfil, "termina", tarea="agentic"
            )

        comando.assert_called_once_with(
            perfil, "termina", session_id="sesion-nueva", resume=False
        )
        self.assertEqual(resultado["session_id"], "sesion-nueva")
        self.assertEqual(
            log.call_args.args[0].get("session_id"), "sesion-nueva"
        )

    def test_correr_claude_reanuda_y_conserva_session_id_en_resultado_y_ledger(self):
        perfil = {"provider": "claude"}
        proceso = mock.Mock(
            stdout=json.dumps({"result": "terminado", "usage": {}}),
            stderr="",
            returncode=0,
        )

        with (
            mock.patch.object(orqlib.uuid, "uuid4") as uuid4,
            mock.patch.object(orqlib, "entorno", return_value={}),
            mock.patch.object(orqlib, "comando", return_value=["claude"]) as comando,
            mock.patch.object(orqlib, "_lock_proveedor", return_value=None),
            mock.patch.object(orqlib.subprocess, "run", return_value=proceso),
            mock.patch.object(orqlib, "log") as log,
        ):
            resultado = orqlib.correr(
                "claude-max20",
                perfil,
                "continua",
                tarea="agentic",
                session_id="sesion-existente",
                resume=True,
            )

        uuid4.assert_not_called()
        comando.assert_called_once_with(
            perfil,
            "continua",
            session_id="sesion-existente",
            resume=True,
        )
        self.assertEqual(resultado["session_id"], "sesion-existente")
        self.assertEqual(
            log.call_args.args[0].get("session_id"), "sesion-existente"
        )

    def test_responder_reanudar_pasa_la_misma_sesion_y_perfil_a_correr(self):
        perfil = {"provider": "claude"}
        exito = {
            **respuesta("claude-max20", "trabajo retomado"),
            "session_id": "sesion-existente",
        }
        parcial = {
            "perfil": "claude-max20",
            "session_id": "sesion-existente",
        }

        with (
            mock.patch.object(
                orqchat.L,
                "disponibles",
                return_value={"claude-max20": perfil},
            ),
            mock.patch.object(orqchat.L, "correr", return_value=exito) as correr,
            mock.patch.object(orqchat, "timeout_chat", return_value=1800),
            mock.patch.object(orqchat, "marco_carpeta", return_value="MARCO\n"),
            mock.patch.object(orqchat, "con_contexto", return_value="PREGUNTA"),
            mock.patch.object(orqchat, "_estado_repo", return_value=(None, {})),
            mock.patch.object(orqchat, "limpiar_parcial"),
            mock.patch.object(
                orqchat,
                "Girador",
                side_effect=lambda _texto: contextlib.nullcontext(),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            resultado = orqchat.responder(
                "continua",
                [],
                tarea="agentic",
                forzado=None,
                reanudar=parcial,
            )

        self.assertIs(resultado, exito)
        self.assertEqual(correr.call_args.args[0], "claude-max20")
        self.assertEqual(correr.call_args.kwargs["session_id"], "sesion-existente")
        self.assertIs(correr.call_args.kwargs["resume"], True)


if __name__ == "__main__":
    unittest.main()
