import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import orqlib


SCANNER = pathlib.Path(orqlib.__file__).resolve().parent / "tools" / "scan-secretos.sh"
HOOK = pathlib.Path(orqlib.__file__).resolve().parent / "tools" / "git-hooks" / "pre-push"


class PublicacionSeguraTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.repo = base / "repo"
        self.remote = base / "remote.git"
        self.orq = base / "orquesta"
        (self.orq / "tools").mkdir(parents=True)
        shutil.copy2(SCANNER, self.orq / "tools" / "scan-secretos.sh")
        (self.orq / "tools" / "git-hooks").mkdir()
        shutil.copy2(HOOK, self.orq / "tools" / "git-hooks" / "pre-push")
        self.git_at(base, "init", "--bare", "-q", str(self.remote))
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "tests@orquesta.local")
        self.git("config", "user.name", "Orquesta tests")
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "base")
        self.git("push", "-qu", "origin", "main")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def git_at(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )

    def git(self, *args):
        return self.git_at(self.repo, *args)

    def test_publica_cambio_limpio_en_origin(self):
        (self.repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            resultado = orqlib.publicar_repo(
                str(self.repo), mensaje="test: publicacion segura"
            )

        self.assertTrue(resultado["ok"], resultado)
        local = self.git("rev-parse", "HEAD").stdout.strip()
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(local, remoto)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_bloquea_secreto_antes_de_stage_commit_y_push(self):
        anterior = self.git("rev-parse", "HEAD").stdout.strip()
        secreto = "CLIENT_SECRET=" + "E" * 32
        (self.repo / "config.txt").write_text(secreto + "\n", encoding="utf-8")
        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["fase"], "seguridad")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), anterior)
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto, anterior)
        self.assertNotIn(secreto, str(resultado))

    def test_proceso_ia_hereda_hook_que_bloquea_push_directo(self):
        anterior = self.git("rev-parse", "HEAD").stdout.strip()
        secreto = "API_KEY=" + "F" * 32
        (self.repo / "credencial.txt").write_text(secreto + "\n", encoding="utf-8")
        self.git("add", "credencial.txt")
        self.git("commit", "-qm", "commit que debe bloquearse")
        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            env = orqlib.entorno("codex", {"provider": "gpt"})
        push = subprocess.run(
            ["git", "push", "origin", "main"], cwd=self.repo, env=env,
            capture_output=True, text=True,
        )

        self.assertNotEqual(push.returncode, 0)
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto, anterior)
        self.assertNotIn(secreto, push.stdout + push.stderr)


if __name__ == "__main__":
    unittest.main()
