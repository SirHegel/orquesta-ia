import contextlib
import io
import json
import sys
import unittest
from unittest import mock


# orqweb interpreta argv[1] como puerto cuando se ejecuta como programa. Al
# importarlo como modulo de pruebas fijamos argv para no depender del runner.
with mock.patch.object(sys, "argv", ["orqweb.py"]):
    import orqweb


def handler(path, body=None, **headers):
    solicitud = object.__new__(orqweb.H)
    solicitud.path = path
    contenido = json.dumps(body if body is not None else {}).encode("utf-8")
    solicitud.rfile = io.BytesIO(contenido)
    solicitud.headers = {
        "Host": f"127.0.0.1:{orqweb.PUERTO}",
        "Content-Type": "application/json",
        "Content-Length": str(len(contenido)),
        **headers,
    }
    solicitud._j = mock.Mock(name="respuesta_json")
    return solicitud


class SeguridadHttpTests(unittest.TestCase):
    def setUp(self):
        with orqweb.JLOCK:
            orqweb.JOBS.clear()

    def tearDown(self):
        with orqweb.JLOCK:
            orqweb.JOBS.clear()

    def test_get_y_post_rechazan_host_no_local_antes_de_despachar(self):
        get = handler("/api/state", Host="orquesta.evil:8787")
        post = handler(
            "/api/run",
            {"prompt": "no ejecutar"},
            Host="orquesta.evil:8787",
        )
        post._ruta = mock.Mock(name="ruta")

        with mock.patch.object(orqweb, "estado") as estado, mock.patch.object(
            orqweb.threading, "Thread"
        ) as hilo:
            get.do_GET()
            post.do_POST()

        self.assertEqual(get._j.call_args.args[0], 403)
        self.assertEqual(post._j.call_args.args[0], 403)
        estado.assert_not_called()
        post._ruta.assert_not_called()
        hilo.assert_not_called()

    def test_panel_reporta_configuracion_rechazada_sin_traceback(self):
        error = orqweb.L.ErrorConfiguracion(
            "profiles.json no es un archivo privado legible"
        )
        get = handler("/api/state")
        post = handler("/api/run", {"prompt": "no ejecutar"})
        post._ruta = mock.Mock(side_effect=error)

        with mock.patch.object(orqweb, "estado", side_effect=error):
            get.do_GET()
            post.do_POST()

        self.assertEqual(get._j.call_args.args, (500, {"error": str(error)}))
        self.assertEqual(post._j.call_args.args, (500, {"error": str(error)}))

    def test_post_rechaza_origin_ajeno_antes_de_despachar(self):
        solicitud = handler(
            "/api/run",
            {"prompt": "no ejecutar"},
            Origin="https://sitio.evil",
        )
        solicitud._ruta = mock.Mock(name="ruta")

        with mock.patch.object(orqweb.threading, "Thread") as hilo:
            solicitud.do_POST()

        self.assertEqual(solicitud._j.call_args.args[0], 403)
        solicitud._ruta.assert_not_called()
        hilo.assert_not_called()

    def test_post_exige_json_pero_acepta_charset(self):
        incorrecta = handler(
            "/api/run",
            {"prompt": "no ejecutar"},
            **{"Content-Type": "text/plain"},
        )
        incorrecta._ruta = mock.Mock(name="ruta_incorrecta")
        correcta = handler(
            "/api/run",
            {"prompt": "solo validar"},
            **{"Content-Type": "application/json; charset=UTF-8"},
        )
        correcta._ruta = mock.Mock(name="ruta_correcta")

        with mock.patch.object(orqweb.threading, "Thread") as hilo:
            incorrecta.do_POST()
            correcta.do_POST()

        self.assertEqual(incorrecta._j.call_args.args[0], 415)
        incorrecta._ruta.assert_not_called()
        correcta._ruta.assert_called_once_with({"prompt": "solo validar"})
        hilo.assert_not_called()

    def test_id_de_cuenta_invalido_se_rechaza_sin_escribir_ni_crear_job(self):
        solicitud = handler(
            "/api/account",
            {"id": "../../state/intruso", "provider": "claude"},
        )

        with (
            mock.patch.object(orqweb.L, "bloqueo", return_value=contextlib.nullcontext()) as bloqueo,
            mock.patch.object(orqweb.L, "cfg", return_value={"profiles": {}}) as cfg,
            mock.patch.object(orqweb.L, "guardar_cfg") as guardar,
            mock.patch.object(orqweb.os, "makedirs") as crear_directorio,
            mock.patch.object(orqweb.os, "chmod") as chmod,
            mock.patch.object(orqweb.threading, "Thread") as hilo,
        ):
            solicitud.do_POST()

        self.assertEqual(solicitud._j.call_args.args[0], 400)
        self.assertIn("id invalido", solicitud._j.call_args.args[1]["error"])
        bloqueo.assert_not_called()
        cfg.assert_not_called()
        guardar.assert_not_called()
        crear_directorio.assert_not_called()
        chmod.assert_not_called()
        hilo.assert_not_called()

    def test_formato_de_id_admite_nombres_seguros_y_rechaza_escape(self):
        for pid in ("claude-polidinamica", "codex_personal", "a", "a" * 64):
            with self.subTest(pid=pid):
                self.assertTrue(orqweb.L.id_perfil_valido(pid))

        for pid in (
            "",
            ".oculto",
            "../escape",
            "con/barra",
            "ConMayusculas",
            "con espacio",
            "a" * 65,
        ):
            with self.subTest(pid=pid):
                self.assertFalse(orqweb.L.id_perfil_valido(pid))

    def test_json_debe_ser_objeto_y_run_valida_tipos_sin_crear_hilos(self):
        no_objeto = handler("/api/run", [])
        prompt_numero = handler("/api/run", {"prompt": 42})
        timeout_invalido = handler(
            "/api/run", {"prompt": "validar", "timeout": "no-numero"}
        )
        timeout_lista = handler("/api/run", {"prompt": "validar", "timeout": []})
        perfil_lista = handler("/api/run", {"prompt": "validar", "perfil": []})

        with mock.patch.object(orqweb.threading, "Thread") as hilo:
            no_objeto.do_POST()
            prompt_numero.do_POST()
            timeout_invalido.do_POST()
            timeout_lista.do_POST()
            perfil_lista.do_POST()

        self.assertEqual(no_objeto._j.call_args.args[0], 400)
        self.assertEqual(prompt_numero._j.call_args.args[0], 400)
        self.assertEqual(timeout_invalido._j.call_args.args[0], 400)
        self.assertEqual(timeout_lista._j.call_args.args[0], 400)
        self.assertEqual(perfil_lista._j.call_args.args[0], 400)
        hilo.assert_not_called()

    def test_navegador_inyectado_no_se_guarda_ni_llega_a_bash(self):
        perfil = {"provider": "claude", "home": "/tmp/cuenta"}
        solicitud = handler(
            "/api/login-launch",
            {"id": "claude-prueba", "navegador": "; ORQ_SENTINEL; #"},
        )

        with mock.patch.object(orqweb.L, "cfg", return_value={
            "profiles": {"claude-prueba": perfil}
        }), mock.patch.object(orqweb.L, "guardar_cfg") as guardar, mock.patch.object(
            orqweb.L, "lanzar_login"
        ) as lanzar:
            solicitud.do_POST()

        self.assertEqual(solicitud._j.call_args.args[0], 400)
        guardar.assert_not_called()
        lanzar.assert_not_called()

        with mock.patch.object(orqweb.L.subprocess, "Popen") as proceso:
            ok, _ = orqweb.L.lanzar_login(
                "claude-prueba", {**perfil, "navegador": "; ORQ_SENTINEL; #"}
            )
        self.assertFalse(ok)
        proceso.assert_not_called()

    def test_verificar_id_inexistente_no_se_convierte_en_fanout_global(self):
        solicitud = handler("/api/verificar", {"id": "cuenta-ausente"})
        with mock.patch.object(orqweb.L, "cfg", return_value={
            "profiles": {"cuenta-real": {"provider": "claude"}}
        }), mock.patch.object(orqweb.threading, "Thread") as hilo:
            solicitud.do_POST()

        self.assertEqual(solicitud._j.call_args.args[0], 404)
        hilo.assert_not_called()
        self.assertEqual(orqweb.JOBS, {})

    def test_cupo_activo_devuelve_429_sin_crear_otro_hilo(self):
        for indice in range(orqweb.MAX_ACTIVE_JOBS):
            self.assertTrue(orqweb._reservar_job(
                f"activo-{indice}", estado="corriendo"
            ))
        solicitud = handler("/api/run", {"prompt": "no debe arrancar"})
        with mock.patch.object(orqweb.threading, "Thread") as hilo:
            solicitud.do_POST()

        self.assertEqual(solicitud._j.call_args.args[0], 429)
        hilo.assert_not_called()
        self.assertEqual(len(orqweb.JOBS), orqweb.MAX_ACTIVE_JOBS)

    def test_run_recupera_cupo_si_falla_el_arranque_del_hilo(self):
        intentos = orqweb.MAX_ACTIVE_JOBS + 2
        ids = [f"run-falla-{indice}" for indice in range(intentos)]

        with mock.patch.object(
            orqweb.uuid, "uuid4",
            side_effect=[mock.Mock(hex=jid) for jid in ids],
        ), mock.patch.object(orqweb.threading, "Thread") as hilo:
            hilo.return_value.start.side_effect = RuntimeError("detalle sensible")
            for _indice in range(intentos):
                solicitud = handler("/api/run", {"prompt": "probar arranque"})
                solicitud.do_POST()
                self.assertEqual(
                    solicitud._j.call_args.args,
                    (500, {"error": "no se pudo iniciar el trabajo"}),
                )

        self.assertEqual(hilo.return_value.start.call_count, intentos)
        self.assertEqual(set(orqweb.JOBS), set(ids))
        self.assertTrue(all(
            job["estado"] == "error" for job in orqweb.JOBS.values()
        ))
        self.assertTrue(orqweb._reservar_job("run-posterior", estado="encolado"))

    def test_verificar_recupera_cupo_si_falla_el_arranque_del_hilo(self):
        intentos = orqweb.MAX_ACTIVE_JOBS + 2
        ids = [f"ver-falla-{indice}" for indice in range(intentos)]
        perfiles = {"cuenta-real": {"provider": "claude"}}

        with mock.patch.object(
            orqweb.uuid, "uuid4",
            side_effect=[mock.Mock(hex=jid) for jid in ids],
        ), mock.patch.object(
            orqweb.L, "cfg", return_value={"profiles": perfiles}
        ), mock.patch.object(orqweb.threading, "Thread") as hilo:
            hilo.return_value.start.side_effect = RuntimeError("detalle sensible")
            for _indice in range(intentos):
                solicitud = handler("/api/verificar", {"id": "cuenta-real"})
                solicitud.do_POST()
                self.assertEqual(
                    solicitud._j.call_args.args,
                    (500, {"error": "no se pudo iniciar el trabajo"}),
                )

        self.assertEqual(hilo.return_value.start.call_count, intentos)
        self.assertEqual(set(orqweb.JOBS), set(ids))
        self.assertTrue(all(
            job["estado"] == "error" for job in orqweb.JOBS.values()
        ))
        self.assertTrue(orqweb._reservar_job("ver-posterior", estado="encolado"))

    def test_get_job_devuelve_snapshot_inmutable_bajo_lock(self):
        self.assertTrue(orqweb._reservar_job(
            "snapshot", estado="corriendo", respuestas=[{"texto": "antes"}]
        ))
        solicitud = handler("/api/job/snapshot")
        solicitud.do_GET()
        respuesta = solicitud._j.call_args.args[1]

        with orqweb.JLOCK:
            orqweb.JOBS["snapshot"]["respuestas"][0]["texto"] = "despues"

        self.assertEqual(respuesta["respuestas"][0]["texto"], "antes")

    def test_jobs_terminados_se_evictionan_sin_borrar_activos(self):
        with mock.patch.object(orqweb, "MAX_JOBS", 2):
            self.assertTrue(orqweb._reservar_job("activo", estado="corriendo"))
            self.assertTrue(orqweb._reservar_job("viejo", estado="listo"))
            self.assertTrue(orqweb._reservar_job("nuevo", estado="encolado"))

        self.assertIn("activo", orqweb.JOBS)
        self.assertIn("nuevo", orqweb.JOBS)
        self.assertNotIn("viejo", orqweb.JOBS)

    def test_fanout_limita_workers_a_cota_fija(self):
        perfiles = {
            f"cuenta-{i}": {"provider": "claude"}
            for i in range(orqweb.MAX_JOB_WORKERS + 3)
        }
        usados = []

        class Pool:
            def __init__(self, max_workers):
                usados.append(max_workers)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def map(self, funcion, elementos):
                return [funcion(x) for x in elementos]

        self.assertTrue(orqweb._reservar_job("fan", estado="encolado"))
        with mock.patch.object(orqweb.L, "disponibles", return_value=perfiles), \
                mock.patch.object(orqweb.L, "correr", return_value={
                    "perfil": "x", "texto": "ok", "tokens": 1,
                }), mock.patch.object(orqweb, "ThreadPoolExecutor", Pool):
            orqweb.ejecutar_job("fan", "prueba", "reasoning", "fan", None, 10)

        self.assertEqual(usados, [orqweb.MAX_JOB_WORKERS])
        self.assertEqual(orqweb.JOBS["fan"]["estado"], "listo")

    def test_fanout_acota_cada_resultado_dentro_del_worker(self):
        perfiles = {
            f"cuenta-{i}": {"provider": "claude"}
            for i in range(orqweb.MAX_JOB_WORKERS + 2)
        }
        tamanos_al_salir_del_worker = []

        class Pool:
            def __init__(self, max_workers):
                self.max_workers = max_workers

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def map(self, funcion, elementos):
                salida = []
                for elemento in elementos:
                    par = funcion(elemento)
                    tamanos_al_salir_del_worker.append(
                        len(json.dumps(par[0], ensure_ascii=False))
                    )
                    salida.append(par)
                return salida

        def resultado_grande(pid, *_args):
            return {
                "perfil": pid,
                "texto": "x" * (orqweb.MAX_JOB_RESULT_CHARS * 8),
                "detalle": "y" * (orqweb.MAX_JOB_RESULT_CHARS * 8),
                "tokens": 7,
            }

        self.assertTrue(orqweb._reservar_job("fan-grande", estado="encolado"))
        with mock.patch.object(orqweb.L, "disponibles", return_value=perfiles), \
                mock.patch.object(orqweb.L, "correr", side_effect=resultado_grande), \
                mock.patch.object(orqweb, "ThreadPoolExecutor", Pool):
            orqweb.ejecutar_job(
                "fan-grande", "prueba", "reasoning", "fan", None, 10
            )

        self.assertEqual(len(tamanos_al_salir_del_worker), len(perfiles))
        self.assertTrue(all(
            tam <= orqweb.MAX_JOB_RESULT_CHARS + 1024
            for tam in tamanos_al_salir_del_worker
        ))
        retenido = json.dumps(orqweb.JOBS["fan-grande"], ensure_ascii=False)
        self.assertLessEqual(len(retenido), orqweb.MAX_JOB_RESULT_CHARS + 8192)
        self.assertEqual(orqweb.JOBS["fan-grande"]["total"], 7 * len(perfiles))


if __name__ == "__main__":
    unittest.main()
