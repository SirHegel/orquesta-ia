import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import orqlib as L


class ProcessGroupRegressionTests(unittest.TestCase):
    def test_sin_bus_systemd_el_comando_se_ejecuta_directamente(self):
        with mock.patch.dict(
            os.environ,
            {"XDG_RUNTIME_DIR": "/ruta/inexistente-orq",
             "DBUS_SESSION_BUS_ADDRESS": "unix:path=/ruta/inexistente-orq/bus"},
            clear=False,
        ), mock.patch.object(L, "_SYSTEMD_USUARIO_CACHE", {}):
            aislado, scope = L._scope_systemd(["/bin/echo", "provider-ran"])

        self.assertEqual(aislado, ["/bin/echo", "provider-ran"])
        self.assertIsNone(scope)

    def test_timeout_mata_lider_e_hijo_del_proveedor(self):
        with tempfile.TemporaryDirectory() as td:
            pids_path = Path(td) / "pids"
            child_code = """
                import signal, time
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                while True:
                    time.sleep(1)
            """
            parent_code = """
                import os, signal, subprocess, sys, time
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                child = subprocess.Popen([sys.executable, "-c", CHILD])
                with open(PIDS, "w") as f:
                    f.write(f"{os.getpid()} {child.pid}")
                    f.flush()
                    os.fsync(f.fileno())
                while True:
                    time.sleep(1)
            """
            command = [
                sys.executable,
                "-c",
                "CHILD = %r\nPIDS = %r\n%s"
                % (textwrap.dedent(child_code), str(pids_path),
                   textwrap.dedent(parent_code)),
            ]
            profile = {"provider": "gpt", "label": "sintetico"}

            with mock.patch.object(L, "comando", return_value=command), \
                    mock.patch.object(L, "entorno", return_value=dict(os.environ)), \
                    mock.patch.object(L, "log") as log:
                result = L.correr("proceso-prueba", profile, "espera",
                                  timeout=0.3, carpeta=td)

            self.assertEqual(124, result["rc"])
            self.assertIn("timeout tras 0.3s", result["texto"])
            self.assertEqual(124, log.call_args.args[0]["rc"])
            self.assertTrue(pids_path.exists(), "el proceso no alcanzo a iniciar")
            parent_pid, child_pid = map(int, pids_path.read_text().split())

            def sigue_ejecutando(pid):
                stat = Path(f"/proc/{pid}/stat")
                if not stat.exists():
                    return False
                # Un zombie ya no puede ejecutar ni escribir; puede persistir
                # brevemente hasta que PID 1 lo recolecte en un contenedor.
                return stat.read_text().split()[2] != "Z"

            limite = time.monotonic() + 3
            while time.monotonic() < limite and any(
                    sigue_ejecutando(pid) for pid in (parent_pid, child_pid)):
                time.sleep(0.02)

            self.assertFalse(sigue_ejecutando(parent_pid))
            self.assertFalse(sigue_ejecutando(child_pid))

    def test_scope_systemd_mata_hijo_que_se_desacopla_con_setsid(self):
        if not L.shutil.which("systemd-run"):
            self.skipTest("systemd-run no disponible")
        estado = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if estado.returncode != 0:
            self.skipTest("systemd de usuario no disponible")

        with tempfile.TemporaryDirectory() as td:
            heartbeat = Path(td) / "heartbeat"
            pids_path = Path(td) / "pids"
            child_code = textwrap.dedent("""
                import os, signal, time
                PIDS = os.environ["PIDS"]
                HEARTBEAT = os.environ["HEARTBEAT"]
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                with open(PIDS, "a") as f:
                    f.write(f" {os.getpid()}")
                    f.flush(); os.fsync(f.fileno())
                n = 0
                while True:
                    n += 1
                    with open(HEARTBEAT, "w") as f:
                        f.write(str(n)); f.flush(); os.fsync(f.fileno())
                    time.sleep(0.03)
            """)
            parent_code = textwrap.dedent("""
                import os, signal, subprocess, sys, time
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                with open(PIDS, "w") as f:
                    f.write(str(os.getpid())); f.flush(); os.fsync(f.fileno())
                subprocess.Popen(
                    [sys.executable, "-c", CHILD], start_new_session=True,
                    env={**os.environ, "PIDS": PIDS, "HEARTBEAT": HEARTBEAT},
                )
                while True: time.sleep(1)
            """)
            command = [
                sys.executable, "-c",
                "CHILD=%r\nPIDS=%r\nHEARTBEAT=%r\n%s"
                % (child_code, str(pids_path), str(heartbeat), parent_code),
            ]
            profile = {"provider": "gpt", "label": "setsid"}
            with mock.patch.object(L, "comando", return_value=command), \
                    mock.patch.object(L, "entorno", return_value=dict(os.environ)), \
                    mock.patch.object(L, "log"):
                result = L.correr("setsid-prueba", profile, "espera",
                                  timeout=0.35, carpeta=td)

            self.assertEqual(result["rc"], 124)
            self.assertTrue(heartbeat.exists())
            primero = heartbeat.read_text()
            time.sleep(0.25)
            self.assertEqual(heartbeat.read_text(), primero)
            for pid in map(int, pids_path.read_text().split()):
                stat = Path(f"/proc/{pid}/stat")
                self.assertTrue(
                    not stat.exists() or stat.read_text().split()[2] == "Z",
                    f"el proceso desacoplado {pid} sigue ejecutandose",
                )

    def test_fallback_sin_systemd_tambien_mata_hijo_con_setsid(self):
        with tempfile.TemporaryDirectory() as td:
            heartbeat = Path(td) / "heartbeat"
            child = textwrap.dedent("""
                import os, signal, time
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                while True:
                    with open(os.environ["HEARTBEAT"], "w") as f:
                        f.write(str(time.time_ns())); f.flush(); os.fsync(f.fileno())
                    time.sleep(0.03)
            """)
            parent = textwrap.dedent("""
                import os, signal, subprocess, sys, time
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                subprocess.Popen([sys.executable, "-c", CHILD],
                                 start_new_session=True, env=os.environ)
                while True: time.sleep(1)
            """)
            command = [sys.executable, "-c", f"CHILD={child!r}\n{parent}"]
            env = {**os.environ, "HEARTBEAT": str(heartbeat)}
            with mock.patch.dict(
                os.environ, {"ORQ_DISABLE_SYSTEMD_SCOPE": "1"}
            ), mock.patch.object(
                L, "comando", return_value=command
            ), mock.patch.object(
                L, "entorno", return_value=env
            ), mock.patch.object(L, "log"):
                resultado = L.correr(
                    "setsid-portable", {"provider": "gpt"}, "espera",
                    timeout=0.35, carpeta=td,
                )

            self.assertEqual(resultado["rc"], 124)
            self.assertTrue(heartbeat.exists())
            valor = heartbeat.read_text()
            time.sleep(0.25)
            self.assertEqual(heartbeat.read_text(), valor)


if __name__ == "__main__":
    unittest.main()
