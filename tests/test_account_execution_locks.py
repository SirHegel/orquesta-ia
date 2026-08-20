import json
import os
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

import orqlib


def respuesta_ok():
    return mock.Mock(
        stdout=json.dumps({"result": "ok", "usage": {}, "is_error": False}),
        stderr="",
        returncode=0,
    )


class RastreadorRunner:
    def __init__(self, esperar_dos=False):
        self.esperar_dos = esperar_dos
        self.mutex = threading.Lock()
        self.primera = threading.Event()
        self.liberar = threading.Event()
        self.dos_activas = threading.Event()
        self.llamadas = 0
        self.activas = 0
        self.max_activas = 0

    def __call__(self, *_args, **_kwargs):
        with self.mutex:
            self.llamadas += 1
            numero = self.llamadas
            self.activas += 1
            self.max_activas = max(self.max_activas, self.activas)
            if self.activas == 2:
                self.dos_activas.set()
        if self.esperar_dos:
            self.dos_activas.wait(1)
        elif numero == 1:
            self.primera.set()
            self.liberar.wait(1)
        with self.mutex:
            self.activas -= 1
        return respuesta_ok()


class LocksCuentaClaudeTests(unittest.TestCase):
    def _ejecutar_dos(self, tmp, perfiles, runner):
        resultados = []

        def ejecutar(pid, perfil):
            resultados.append(orqlib.correr(
                pid, perfil, "hola", tarea="reasoning", carpeta=tmp
            ))

        with (
            mock.patch.object(orqlib, "BASE", tmp),
            mock.patch.object(orqlib, "comando", return_value=["claude"]),
            mock.patch.object(orqlib, "entorno", return_value={}),
            mock.patch.object(orqlib.subprocess, "run", side_effect=runner),
            mock.patch.object(orqlib, "log"),
        ):
            primero = threading.Thread(target=ejecutar, args=("claude-a", perfiles[0]))
            segundo = threading.Thread(target=ejecutar, args=("claude-b", perfiles[1]))
            primero.start()
            if not runner.esperar_dos:
                self.assertTrue(runner.primera.wait(1))
            segundo.start()
            if not runner.esperar_dos:
                time.sleep(0.1)
                self.assertEqual(runner.llamadas, 1)
                runner.liberar.set()
            primero.join(2)
            segundo.join(2)
            self.assertFalse(primero.is_alive())
            self.assertFalse(segundo.is_alive())
        self.assertEqual(len(resultados), 2)
        self.assertTrue(all(r["rc"] == 0 for r in resultados))

    def test_dos_perfiles_con_la_misma_home_se_serializan(self):
        with tempfile.TemporaryDirectory() as tmp:
            cuenta = os.path.join(tmp, "cuenta")
            os.mkdir(cuenta)
            runner = RastreadorRunner()
            perfiles = [
                {"provider": "claude", "home": cuenta},
                {"provider": "claude", "home": os.path.join(cuenta, ".")},
            ]
            self._ejecutar_dos(tmp, perfiles, runner)

        self.assertEqual(runner.llamadas, 2)
        self.assertEqual(runner.max_activas, 1)

    def test_cuentas_con_homes_distintas_pueden_correr_en_paralelo(self):
        with tempfile.TemporaryDirectory() as tmp:
            cuenta_a = os.path.join(tmp, "a")
            cuenta_b = os.path.join(tmp, "b")
            os.mkdir(cuenta_a)
            os.mkdir(cuenta_b)
            runner = RastreadorRunner(esperar_dos=True)
            perfiles = [
                {"provider": "claude", "home": cuenta_a},
                {"provider": "claude", "home": cuenta_b},
            ]
            self._ejecutar_dos(tmp, perfiles, runner)

        self.assertEqual(runner.llamadas, 2)
        self.assertEqual(runner.max_activas, 2)

    def test_path_es_canonico_estable_opaco_y_el_archivo_es_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            cuenta = os.path.join(tmp, "cuenta")
            os.mkdir(cuenta)
            perfil = {"provider": "claude", "home": cuenta}
            with mock.patch.object(orqlib, "BASE", tmp):
                ruta_a = orqlib.ruta_lock_cuenta_claude("claude-a", perfil)
                ruta_b = orqlib.ruta_lock_cuenta_claude(
                    "otro-id", {"provider": "claude", "home": cuenta + "/."}
                )
                self.assertEqual(ruta_a, ruta_b)
                self.assertNotIn("cuenta", os.path.basename(ruta_a))
                archivo = orqlib._lock_cuenta_claude("claude-a", perfil)
                archivo.close()

            self.assertEqual(stat.S_IMODE(os.stat(ruta_a).st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(os.stat(os.path.dirname(ruta_a)).st_mode), 0o700
            )


if __name__ == "__main__":
    unittest.main()
