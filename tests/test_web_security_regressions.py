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


if __name__ == "__main__":
    unittest.main()
