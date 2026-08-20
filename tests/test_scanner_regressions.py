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
        secreto = "sk-" + "proj-" + "A" * 32
        ruta = self.repo / "solo-indice.txt"
        ruta.write_text(secreto + "\n", encoding="utf-8")
        self.git("add", ruta.name)
        ruta.write_text("el working tree ya no lo contiene\n", encoding="utf-8")

        resultado = self.scan()

        self.assertEqual(resultado.returncode, 1)
        self.assertNotIn(secreto, resultado.stdout + resultado.stderr)

    def test_nombres_con_salto_y_tokens_modernos_no_eluden_el_scan(self):
        secreto = "github_" + "pat_" + "B" * 32
        ruta = self.repo / "nombre\npartido.txt"
        ruta.write_text(secreto + "\n", encoding="utf-8")
        self.git("add", ruta.name)

        resultado = self.scan()

        self.assertEqual(resultado.returncode, 1)
        self.assertNotIn(secreto, resultado.stdout + resultado.stderr)

    def test_blob_binario_con_nul_tambien_se_revisa(self):
        secreto = ("sk-" + "proj-" + "C" * 32).encode()
        ruta = self.repo / "binario.dat"
        ruta.write_bytes(b"cabecera\x00" + secreto + b"\n")
        self.git("add", ruta.name)

        resultado = self.scan()

        self.assertEqual(resultado.returncode, 1)
        self.assertNotIn(secreto.decode(), resultado.stdout + resultado.stderr)

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

    def test_indice_ilegible_falla_cerrado(self):
        entorno = dict(os.environ)
        entorno["GIT_INDEX_FILE"] = "/dev/null"
        resultado = self.scan(entorno)
        self.assertEqual(resultado.returncode, 2)

    def test_todo_incluye_archivos_no_trackeados_y_commit_revisa_su_arbol(self):
        secreto = "CLIENT_SECRET=" + "D" * 32
        ruta = self.repo / "nuevo.txt"
        ruta.write_text(secreto + "\n", encoding="utf-8")
        todo = self.scan_args("--todo", "--repo", str(self.repo))
        self.assertEqual(todo.returncode, 1)
        self.assertNotIn(secreto, todo.stdout + todo.stderr)

        self.git("add", ruta.name)
        self.git("commit", "-qm", "secreto sintetico")
        commit = self.git("rev-parse", "HEAD").stdout.strip()
        ruta.write_text("limpio\n", encoding="utf-8")
        self.git("add", ruta.name)
        self.git("commit", "-qm", "quitar del arbol actual")
        historico = self.scan_args("--commit", commit, "--repo", str(self.repo))
        self.assertEqual(historico.returncode, 1)
        self.assertNotIn(secreto, historico.stdout + historico.stderr)


if __name__ == "__main__":
    unittest.main()
