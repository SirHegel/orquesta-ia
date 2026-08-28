import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


SCANNER = pathlib.Path(__file__).resolve().parents[1] / "tools" / "scan-secretos.sh"


class ScannerStagedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        (self.repo / "tools").mkdir()
        shutil.copy2(SCANNER, self.repo / "tools" / "scan-secretos.sh")
        self.git("init", "-q")
        self.git("config", "user.email", "tests@orquesta.local")
        self.git("config", "user.name", "Orquesta tests")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "base.txt")
        self.git("commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True
        )

    def scan(self, env=None):
        return subprocess.run(
            ["bash", "tools/scan-secretos.sh", "--staged"],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
        )

    def scan_args(self, *args):
        return subprocess.run(
            ["bash", "tools/scan-secretos.sh", *args], cwd=self.repo,
            capture_output=True, text=True,
        )

    def test_lee_el_blob_staged_y_no_imprime_el_secreto(self):
        patron_prueba = "sk-" + "proj-" + "A" * 32
        ruta = self.repo / "solo-indice.txt"
        ruta.write_text(patron_prueba + "\n", encoding="utf-8")
        self.git("add", ruta.name)
        ruta.write_text("el working tree ya no lo contiene\n", encoding="utf-8")

        resultado = self.scan()

        self.assertEqual(resultado.returncode, 1)
        self.assertNotIn(patron_prueba, resultado.stdout + resultado.stderr)

    def test_nombres_con_salto_y_tokens_modernos_no_eluden_el_scan(self):
        patron_prueba = "github_" + "pat_" + "B" * 32
        ruta = self.repo / "nombre\npartido.txt"
        ruta.write_text(patron_prueba + "\n", encoding="utf-8")
        self.git("add", ruta.name)

        resultado = self.scan()

        self.assertEqual(resultado.returncode, 1)
        self.assertNotIn(patron_prueba, resultado.stdout + resultado.stderr)

    def test_blob_binario_con_nul_tambien_se_revisa(self):
        patron_prueba = ("sk-" + "proj-" + "C" * 32).encode()
        ruta = self.repo / "binario.dat"
        ruta.write_bytes(b"cabecera\x00" + patron_prueba + b"\n")
        self.git("add", ruta.name)

        resultado = self.scan()

        self.assertEqual(resultado.returncode, 1)
        self.assertNotIn(patron_prueba.decode(), resultado.stdout + resultado.stderr)

    def test_ruta_sensible_anidada_y_rename_se_bloquean(self):
        anidada = self.repo / "nested" / ".envrc"
        anidada.parent.mkdir()
        anidada.write_text("sin contenido secreto\n", encoding="utf-8")
        self.git("add", str(anidada.relative_to(self.repo)))
        self.assertEqual(self.scan().returncode, 1)

        self.git("reset", "-q")
        origen = self.repo / "normal.json"
        origen.write_text("{}\n", encoding="utf-8")
        self.git("add", origen.name)
        self.git("commit", "-qm", "normal")
        origen.rename(self.repo / "profiles.json")
        self.git("add", "-A")
        self.assertEqual(self.scan().returncode, 1)

    def test_git_index_file_heredado_no_contamina_el_scanner(self):
        entorno = dict(os.environ)
        entorno["GIT_INDEX_FILE"] = "/dev/null"
        resultado = self.scan(entorno)
        self.assertEqual(resultado.returncode, 0)

    def test_indice_real_corrupto_falla_cerrado(self):
        (self.repo / ".git" / "index").write_bytes(b"indice-corrupto")
        resultado = self.scan()
        self.assertEqual(resultado.returncode, 2)

    def test_fsmonitor_local_no_se_ejecuta(self):
        marca = self.repo / "fsmonitor-ejecutado"
        hook = self.repo / "fsmonitor"
        hook.write_text(f"#!/bin/sh\ntouch {marca}\n", encoding="utf-8")
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))

        resultado = self.scan_args("--todo", "--repo", str(self.repo))

        self.assertEqual(resultado.returncode, 0, resultado.stdout + resultado.stderr)
        self.assertFalse(marca.exists())

    def test_nombre_hostil_no_inyecta_controles_en_salida(self):
        patron_prueba = "github_" + "pat_" + "E" * 32
        ruta = self.repo / "malo\x1b]52;c;ataque\x07.env"
        ruta.write_text(patron_prueba + "\n", encoding="utf-8")
        self.git("add", ruta.name)
        resultado = self.scan()
        self.assertEqual(resultado.returncode, 1)
        self.assertNotIn("\x1b]52", resultado.stdout + resultado.stderr)
        self.assertNotIn(patron_prueba, resultado.stdout + resultado.stderr)

    def test_todo_incluye_archivos_no_trackeados_y_commit_revisa_su_arbol(self):
        patron_prueba = "CLIENT_" + "SECRET=" + "D" * 32
        ruta = self.repo / "nuevo.txt"
        ruta.write_text(patron_prueba + "\n", encoding="utf-8")
        todo = self.scan_args("--todo", "--repo", str(self.repo))
        self.assertEqual(todo.returncode, 1)
        self.assertNotIn(patron_prueba, todo.stdout + todo.stderr)

        self.git("add", ruta.name)
        self.git("commit", "-qm", "secreto sintetico")
        commit = self.git("rev-parse", "HEAD").stdout.strip()
        ruta.write_text("limpio\n", encoding="utf-8")
        self.git("add", ruta.name)
        self.git("commit", "-qm", "quitar del arbol actual")
        historico = self.scan_args("--commit", commit, "--repo", str(self.repo))
        self.assertEqual(historico.returncode, 1)
        self.assertNotIn(patron_prueba, historico.stdout + historico.stderr)

    def test_commit_revisa_mensaje_sin_mostrar_el_marcador(self):
        prefijo = "".join(chr(x) for x in (
            67, 76, 73, 69, 78, 84, 95, 83, 69, 67, 82, 69, 84, 61,
        ))
        marcador_mensaje = prefijo + "M" * 32
        self.git("commit", "--allow-empty", "-qm", marcador_mensaje)
        commit = self.git("rev-parse", "HEAD").stdout.strip()

        resultado = self.scan_args(
            "--commit", commit, "--repo", str(self.repo)
        )

        self.assertEqual(resultado.returncode, 1)
        self.assertIn("metadata del commit", resultado.stdout + resultado.stderr)
        self.assertNotIn(
            marcador_mensaje, resultado.stdout + resultado.stderr
        )


if __name__ == "__main__":
    unittest.main()
