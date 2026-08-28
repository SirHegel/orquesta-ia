import hashlib
import pathlib
import shutil
import struct
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

    def instalar_commit_graph_sin_padre(self, oid):
        """Crea un commit-graph válido que declara el tip como raíz."""
        tree = self.git("rev-parse", f"{oid}^{{tree}}").stdout.strip()
        tiempo = int(self.git("show", "-s", "--format=%ct", oid).stdout)
        oid_bytes = bytes.fromhex(oid)
        tree_bytes = bytes.fromhex(tree)
        largo_hash = len(oid_bytes)
        version_hash = 1 if largo_hash == 20 else 2
        inicio = 8 + 4 * 12
        offset_fanout = inicio
        offset_oids = offset_fanout + 256 * 4
        offset_datos = offset_oids + largo_hash
        fin = offset_datos + largo_hash + 16
        cabecera = b"CGPH" + bytes((1, version_hash, 3, 0))
        tabla = b"".join((
            b"OIDF", struct.pack(">Q", offset_fanout),
            b"OIDL", struct.pack(">Q", offset_oids),
            b"CDAT", struct.pack(">Q", offset_datos),
            b"\0" * 4, struct.pack(">Q", fin),
        ))
        primer_byte = oid_bytes[0]
        fanout = b"".join(
            struct.pack(">I", int(indice >= primer_byte))
            for indice in range(256)
        )
        padres_vacios = struct.pack(">II", 0x70000000, 0x70000000)
        generacion_fecha = struct.pack(
            ">II", (1 << 2) | (tiempo >> 32), tiempo & 0xFFFFFFFF
        )
        contenido = (
            cabecera + tabla + fanout + oid_bytes + tree_bytes
            + padres_vacios + generacion_fecha
        )
        self.assertEqual(len(contenido), fin)
        resumen = hashlib.sha1 if largo_hash == 20 else hashlib.sha256
        destino = self.repo / ".git" / "objects" / "info" / "commit-graph"
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(contenido + resumen(contenido).digest())

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

    def test_rama_existente_actualiza_tracking_y_preserva_fetch_head(self):
        tracking_antes = self.git(
            "rev-parse", "refs/remotes/origin/main"
        ).stdout.strip()
        fetch_head = self.repo / ".git" / "FETCH_HEAD"
        fetch_head.write_text("sentinel local\n", encoding="utf-8")
        (self.repo / "tracking.txt").write_text("nuevo\n", encoding="utf-8")

        resultado = orqlib.publicar_repo(
            str(self.repo), mensaje="test: tracking existente"
        )

        self.assertTrue(resultado["ok"], resultado)
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        tracking_despues = self.git(
            "rev-parse", "refs/remotes/origin/main"
        ).stdout.strip()
        self.assertEqual(tracking_despues, remoto)
        self.assertNotEqual(tracking_despues, tracking_antes)
        self.assertEqual(fetch_head.read_text(encoding="utf-8"), "sentinel local\n")

    def test_tracking_cas_no_pisa_un_fetch_concurrente_despues_del_push(self):
        tracking_antes = self.git(
            "rev-parse", "refs/remotes/origin/main"
        ).stdout.strip()
        tree = self.git("rev-parse", "HEAD^{tree}").stdout.strip()
        competidor = self.git(
            "commit-tree", tree, "-p", tracking_antes, "-m", "competidor"
        ).stdout.strip()
        (self.repo / "cas.txt").write_text("publicable\n", encoding="utf-8")
        git_real = orqlib._git
        intercalado = False

        def intercalar_tracking(carpeta, *args, **kwargs):
            nonlocal intercalado
            resultado_git = git_real(carpeta, *args, **kwargs)
            if not intercalado and args[:1] == ("push",):
                intercalado = True
                self.git(
                    "update-ref", "refs/remotes/origin/main", competidor,
                    tracking_antes,
                )
            return resultado_git

        with mock.patch.object(
                orqlib, "_git", side_effect=intercalar_tracking):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertTrue(intercalado)
        self.assertFalse(resultado["ok"], resultado)
        self.assertTrue(resultado["publicado"])
        self.assertEqual(resultado["fase"], "tracking")
        self.assertEqual(
            self.git("rev-parse", "refs/remotes/origin/main").stdout.strip(),
            competidor,
        )
        self.assertEqual(
            self.git_at(self.remote, "rev-parse", "refs/heads/main").stdout.strip(),
            self.git("rev-parse", "HEAD").stdout.strip(),
        )

    def test_publica_borrado_y_rename_limpios(self):
        (self.repo / "borrar.txt").write_text("adios\n", encoding="utf-8")
        (self.repo / "anterior.txt").write_text("mover\n", encoding="utf-8")
        self.git("add", "borrar.txt", "anterior.txt")
        self.git("commit", "-qm", "archivos que cambiaran")
        self.git("push", "-q", "origin", "main")
        (self.repo / "borrar.txt").unlink()
        (self.repo / "anterior.txt").rename(self.repo / "nuevo.txt")

        resultado = orqlib.publicar_repo(
            str(self.repo), mensaje="test: delete y rename"
        )

        self.assertTrue(resultado["ok"], resultado)
        remoto = self.git_at(
            self.remote, "ls-tree", "-r", "--name-only", "refs/heads/main"
        ).stdout.splitlines()
        self.assertIn("nuevo.txt", remoto)
        self.assertNotIn("anterior.txt", remoto)
        self.assertNotIn("borrar.txt", remoto)

    def test_snapshot_primera_rama_fija_oid_exacto_y_upstream(self):
        self.git("switch", "-qc", "feature-snapshot")
        (self.repo / "feature.txt").write_text("snapshot\n", encoding="utf-8")
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(self.repo), "test: primera rama snapshot"
        )

        resultado = orqlib.publicar_repo(
            str(self.repo), identidad_esperada=snapshot["cwd_identidad"],
            identidad_repo_esperada=snapshot["repo_identidad"],
            snapshot_esperado=snapshot,
        )

        self.assertTrue(resultado["ok"], resultado)
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/feature-snapshot"
        ).stdout.strip()
        upstream = self.git(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        ).stdout.strip()
        self.assertEqual(remoto, snapshot["commit_oid"])
        self.assertEqual(upstream, "origin/feature-snapshot")

    def test_rama_nueva_sin_cambios_publica_tip_y_fija_upstream(self):
        base_oid = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("switch", "-qc", "feature-sin-cambios")

        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertTrue(resultado["ok"], resultado)
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/feature-sin-cambios"
        ).stdout.strip()
        upstream = self.git(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        ).stdout.strip()
        self.assertEqual(remoto, base_oid)
        self.assertEqual(upstream, "origin/feature-sin-cambios")

    def test_repo_unborn_publica_commit_raiz_directo_con_upstream(self):
        repo = self.repo.parent / "unborn-directo"
        remoto_git = self.repo.parent / "unborn-directo.git"
        self.git_at(self.repo.parent, "init", "--bare", "-q", str(remoto_git))
        repo.mkdir()
        self.git_at(repo, "init", "-q", "-b", "main")
        self.git_at(repo, "config", "user.email", "tests@orquesta.local")
        self.git_at(repo, "config", "user.name", "Orquesta tests")
        self.git_at(repo, "remote", "add", "origin", str(remoto_git))
        (repo / "README.md").write_text("raiz\n", encoding="utf-8")

        resultado = orqlib.publicar_repo(str(repo), mensaje="test: commit raiz")

        self.assertTrue(resultado["ok"], resultado)
        local = self.git_at(repo, "rev-parse", "HEAD").stdout.strip()
        remoto = self.git_at(
            remoto_git, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        upstream = self.git_at(
            repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
            "@{upstream}",
        ).stdout.strip()
        self.assertEqual(remoto, local)
        self.assertEqual(upstream, "origin/main")

    def test_repo_unborn_snapshot_publica_oid_probado_con_upstream(self):
        repo = self.repo.parent / "unborn-snapshot"
        remoto_git = self.repo.parent / "unborn-snapshot.git"
        self.git_at(self.repo.parent, "init", "--bare", "-q", str(remoto_git))
        repo.mkdir()
        self.git_at(repo, "init", "-q", "-b", "main")
        self.git_at(repo, "remote", "add", "origin", str(remoto_git))
        (repo / "README.md").write_text("snapshot raiz\n", encoding="utf-8")
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(repo), "test: snapshot raiz"
        )

        resultado = orqlib.publicar_repo(
            str(repo), identidad_esperada=snapshot["cwd_identidad"],
            identidad_repo_esperada=snapshot["repo_identidad"],
            snapshot_esperado=snapshot,
        )

        self.assertIsNone(snapshot["base_oid"])
        self.assertTrue(snapshot["creado"])
        self.assertTrue(resultado["ok"], resultado)
        remoto = self.git_at(
            remoto_git, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        upstream = self.git_at(
            repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
            "@{upstream}",
        ).stdout.strip()
        self.assertEqual(remoto, snapshot["commit_oid"])
        self.assertEqual(upstream, "origin/main")

    def test_repo_unborn_rechaza_rama_ya_existente_en_destino(self):
        repo = self.repo.parent / "unborn-con-remoto"
        repo.mkdir()
        self.git_at(repo, "init", "-q", "-b", "main")
        self.git_at(repo, "remote", "add", "origin", str(self.remote))
        (repo / "README.md").write_text("no sobrescribir\n", encoding="utf-8")
        remoto_antes = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(repo), "test: unborn no reemplaza remoto"
        )

        resultado = orqlib.publicar_repo(
            str(repo), identidad_esperada=snapshot["cwd_identidad"],
            identidad_repo_esperada=snapshot["repo_identidad"],
            snapshot_esperado=snapshot,
        )

        self.assertFalse(resultado["ok"], resultado)
        self.assertEqual(resultado["fase"], "sincronizacion")
        remoto_despues = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto_despues, remoto_antes)

    def test_bloquea_secreto_antes_de_stage_commit_y_push(self):
        anterior = self.git("rev-parse", "HEAD").stdout.strip()
        patron_prueba = "CLIENT_" + "SECRET=" + "E" * 32
        (self.repo / "config.txt").write_text(patron_prueba + "\n", encoding="utf-8")
        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["fase"], "seguridad")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), anterior)
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto, anterior)
        self.assertNotIn(patron_prueba, str(resultado))

    def test_proceso_ia_hereda_hook_que_bloquea_push_directo(self):
        anterior = self.git("rev-parse", "HEAD").stdout.strip()
        patron_prueba = "API_" + "KEY=" + "F" * 32
        (self.repo / "credencial.txt").write_text(patron_prueba + "\n", encoding="utf-8")
        self.git("add", "credencial.txt")
        self.git("commit", "-qm", "commit que debe bloquearse")
        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            env = orqlib.entorno("codex", {"provider": "gpt"})
        raiz_falsa = self.repo.parent / "orquesta-falsa"
        (raiz_falsa / "tools").mkdir(parents=True)
        scanner_falso = raiz_falsa / "tools" / "scan-secretos.sh"
        scanner_falso.write_text(
            "#!/usr/bin/bash\nexit 0\n", encoding="utf-8"
        )
        scanner_falso.chmod(0o700)
        env["ORQ_HOME"] = str(raiz_falsa)
        push = subprocess.run(
            ["git", "push", "origin", "main"], cwd=self.repo, env=env,
            capture_output=True, text=True,
        )

        self.assertNotEqual(push.returncode, 0)
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto, anterior)
        self.assertNotIn(patron_prueba, push.stdout + push.stderr)

    def test_hook_rechaza_objetos_no_commit_y_refs_que_no_son_ramas(self):
        ruta_blob = self.repo / "objeto.txt"
        ruta_blob.write_text("contenido inocuo\n", encoding="utf-8")
        oid_blob = self.git("hash-object", "-w", ruta_blob.name).stdout.strip()
        oid_tree = self.git("rev-parse", "HEAD^{tree}").stdout.strip()
        oid_commit = self.git("rev-parse", "HEAD").stdout.strip()
        hook = self.orq / "tools" / "git-hooks" / "pre-push"

        casos = (
            (oid_blob, "refs/heads/blob-no-commit"),
            (oid_tree, "refs/heads/tree-no-commit"),
            (oid_commit, "refs/tags/etiqueta-no-permitida"),
        )
        for oid_local, ref_remota in casos:
            with self.subTest(ref_remota=ref_remota):
                cero = "0" * len(oid_local)
                entrada = (
                    f"refs/heads/main {oid_local} {ref_remota} {cero}\n"
                )
                resultado = subprocess.run(
                    [str(hook)], cwd=self.repo, input=entrada,
                    capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(resultado.returncode, 0)

        # Borrar una ref no introduce objetos y conserva el contrato anterior.
        cero = "0" * len(oid_commit)
        borrado = subprocess.run(
            [str(hook)], cwd=self.repo,
            input=(f"(delete) {cero} refs/tags/antigua {oid_commit}\n"),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(borrado.returncode, 0, borrado.stderr)

    def test_commit_graph_no_oculta_ancestro_a_publicacion_ni_hook(self):
        self.git("switch", "-qc", "feature-graph")
        prefijo = "".join(chr(x) for x in (
            67, 76, 73, 69, 78, 84, 95, 83, 69, 67, 82, 69, 84, 61,
        ))
        marcador_historial = prefijo + "G" * 32
        ruta = self.repo / "graph.txt"
        ruta.write_text(marcador_historial + "\n", encoding="utf-8")
        self.git("add", "graph.txt")
        self.git("commit", "-qm", "marcador en ancestro")
        oid_ancestro = self.git("rev-parse", "HEAD").stdout.strip()
        ruta.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "tip sin marcador")
        oid_tip = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("update-ref", "refs/remotes/origin/feature-graph", oid_tip)
        self.instalar_commit_graph_sin_padre(oid_tip)

        self.assertEqual(self.git("rev-list", oid_tip).stdout.splitlines(), [oid_tip])
        recorrido_seguro = self.git(
            "-c", "core.commitGraph=false", "rev-list", oid_tip
        ).stdout.splitlines()
        self.assertIn(oid_ancestro, recorrido_seguro)

        resultado = orqlib.publicar_repo(str(self.repo))
        self.assertFalse(resultado["ok"], resultado)
        self.assertEqual(resultado["fase"], "seguridad")

        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            env = orqlib.entorno("codex", {"provider": "gpt"})
        push = subprocess.run(
            ["/usr/bin/git", "push", "origin", "feature-graph"],
            cwd=self.repo, env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(push.returncode, 0)
        remoto = subprocess.run(
            ["/usr/bin/git", "--git-dir", str(self.remote), "show-ref",
             "--verify", "--quiet", "refs/heads/feature-graph"],
            check=False,
        )
        self.assertNotEqual(remoto.returncode, 0)
        self.assertNotIn(marcador_historial, push.stdout + push.stderr)

    def test_hook_admite_oids_sha256_si_git_los_soporta(self):
        repo = self.repo.parent / "sha256"
        remoto_git = self.repo.parent / "sha256.git"
        inicio = subprocess.run(
            ["/usr/bin/git", "init", "--object-format=sha256", "-q", "-b", "main",
             str(repo)], capture_output=True, text=True,
        )
        if inicio.returncode != 0:
            self.skipTest("Git local no soporta repositorios SHA-256")
        self.git_at(
            self.repo.parent, "init", "--object-format=sha256", "--bare", "-q",
            str(remoto_git),
        )
        self.git_at(repo, "config", "user.email", "tests@orquesta.local")
        self.git_at(repo, "config", "user.name", "Orquesta tests")
        self.git_at(repo, "remote", "add", "origin", str(remoto_git))
        (repo / "README.md").write_text("base sha256\n", encoding="utf-8")
        self.git_at(repo, "add", "README.md")
        self.git_at(repo, "commit", "-qm", "base")
        self.git_at(repo, "-c", "core.hooksPath=/dev/null", "push", "-qu", "origin", "main")
        oid_base = self.git_at(repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(oid_base), 64)

        patron_prueba = "API_" + "KEY=" + "Q" * 32
        (repo / "credencial.txt").write_text(patron_prueba + "\n", encoding="utf-8")
        self.git_at(repo, "add", "credencial.txt")
        self.git_at(repo, "commit", "-qm", "marcador sha256")
        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            env = orqlib.entorno("codex", {"provider": "gpt"})
        push = subprocess.run(
            ["/usr/bin/git", "push", "origin", "main"], cwd=repo, env=env,
            capture_output=True, text=True,
        )

        self.assertNotEqual(push.returncode, 0)
        self.assertEqual(
            self.git_at(remoto_git, "rev-parse", "refs/heads/main").stdout.strip(),
            oid_base,
        )
        self.assertNotIn(patron_prueba, push.stdout + push.stderr)

    def test_rechaza_filtro_local_antes_de_git_status_add_o_commit(self):
        marca = self.repo.parent / "filtro-ejecutado"
        filtro = self.repo.parent / "filtro-hostil"
        filtro.write_text(
            f"#!/bin/sh\ntouch {marca}\n/bin/cat\n", encoding="utf-8"
        )
        filtro.chmod(0o700)
        (self.repo / ".gitattributes").write_text(
            "*.txt filter=hostil\n", encoding="utf-8"
        )
        (self.repo / "dato.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".gitattributes", "dato.txt")
        self.git("commit", "-qm", "atributos sin filtro configurado")
        self.git("config", "filter.hostil.clean", str(filtro))
        (self.repo / "dato.txt").write_text("cambio\n", encoding="utf-8")

        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["fase"], "seguridad")
        self.assertFalse(marca.exists())

    def test_rechaza_core_sshcommand_local_sin_ejecutarlo(self):
        marca = self.repo.parent / "ssh-ejecutado"
        falso = self.repo.parent / "ssh-hostil"
        falso.write_text(f"#!/bin/sh\ntouch {marca}\nexit 1\n", encoding="utf-8")
        falso.chmod(0o700)
        self.git("config", "core.sshCommand", str(falso))

        with mock.patch.object(orqlib, "BASE", str(self.orq)):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["fase"], "seguridad")
        self.assertFalse(marca.exists())

    def test_cambio_de_pushurl_no_redirige_el_oid_ya_auditado(self):
        atacante = self.repo.parent / "remoto-atacante.git"
        self.git_at(self.repo.parent, "init", "--bare", "-q", str(atacante))
        (self.repo / "destino.txt").write_text("original\n", encoding="utf-8")
        git_real = orqlib._git
        cambiado = False

        def cambiar_origin_despues_de_capturarlo(carpeta, *args, **kwargs):
            nonlocal cambiado
            resultado = git_real(carpeta, *args, **kwargs)
            if (not cambiado
                    and args == ("remote", "get-url", "--push", "origin")):
                cambiado = True
                self.git("config", "remote.origin.pushurl", str(atacante))
            return resultado

        with mock.patch.object(
                orqlib, "_git", side_effect=cambiar_origin_despues_de_capturarlo):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertTrue(cambiado)
        self.assertTrue(resultado["ok"], resultado)
        publicado = self.git_at(
            self.remote, "show", "refs/heads/main:destino.txt"
        ).stdout
        self.assertEqual(publicado, "original\n")
        ausente = subprocess.run(
            ["/usr/bin/git", "--git-dir", str(atacante), "show-ref",
             "--verify", "--quiet", "refs/heads/main"], check=False,
        )
        self.assertNotEqual(ausente.returncode, 0)

    def test_snapshot_rechaza_includeif_de_worktree_antes_del_checkout(self):
        marca = self.repo.parent / "smudge-ejecutado"
        filtro = self.repo.parent / "filtro-smudge"
        filtro.write_text(
            f"#!/bin/sh\ntouch {marca}\n/bin/cat\n", encoding="utf-8"
        )
        filtro.chmod(0o700)
        incluido = self.repo.parent / "config-worktree"
        incluido.write_text(
            f"[filter \"hostil\"]\n\tsmudge = {filtro}\n", encoding="utf-8"
        )
        self.git(
            "config", "--local",
            "includeIf.gitdir:/tmp/orq-verify-*/checkout/.path", str(incluido),
        )

        with self.assertRaisesRegex(OSError, "configuracion Git ejecutable"):
            orqlib.preparar_snapshot_verificacion(
                str(self.repo), "test: include condicional"
            )

        self.assertFalse(marca.exists())

    def test_config_que_cambia_tras_auditoria_no_ejecuta_filtro(self):
        marca = self.repo.parent / "clean-ejecutado-en-carrera"
        filtro = self.repo.parent / "filtro-clean-carrera"
        filtro.write_text(
            f"#!/bin/sh\ntouch {marca}\n/bin/cat\n", encoding="utf-8"
        )
        filtro.chmod(0o700)
        (self.repo / ".gitattributes").write_text(
            "*.txt filter=hostil\n", encoding="utf-8"
        )
        (self.repo / "dato.txt").write_text("contenido\n", encoding="utf-8")
        auditoria_real = orqlib._config_git_ejecutable
        inyectado = False

        def cambiar_config(carpeta, cwd_fd=None):
            nonlocal inyectado
            resultado = auditoria_real(carpeta, cwd_fd=cwd_fd)
            if resultado is False and not inyectado:
                inyectado = True
                self.git("config", "filter.hostil.clean", str(filtro))
            return resultado

        with mock.patch.object(
                orqlib, "_config_git_ejecutable", side_effect=cambiar_config):
            with self.assertRaisesRegex(OSError, "configuracion Git cambio"):
                orqlib.preparar_snapshot_verificacion(
                    str(self.repo), "test: carrera de configuracion"
                )

        self.assertTrue(inyectado)
        self.assertFalse(marca.exists())

    def test_snapshot_truncado_falla_cerrado_sin_keyerror(self):
        for incompleto in ({"version": 1}, {
            "version": 1, "base_oid": "a" * 40, "tree_oid": "b" * 40,
            "commit_oid": "c" * 40, "rama": "main", "cwd_rel": "",
            "cwd_identidad": [1, 2], "repo_identidad": [1, 2],
        }):
            with self.subTest(claves=sorted(incompleto)):
                resultado = orqlib.publicar_repo(
                    str(self.repo), snapshot_esperado=incompleto
                )
                self.assertFalse(resultado["ok"])
                self.assertEqual(resultado["fase"], "seguridad")

    def test_materializacion_fallida_antes_de_adquirir_repo_no_deja_temporal(self):
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(self.repo), "test: fallo temprano sin temporal"
        )
        temporal = self.repo.parent
        antes = set(temporal.glob("orq-verify-*"))
        movido = self.repo.parent / "repo-movido"
        self.repo.rename(movido)

        with mock.patch.object(orqlib, "TEMP_ROOT", str(temporal)):
            with self.assertRaises(OSError):
                with orqlib.materializar_snapshot_verificacion(snapshot):
                    self.fail("no debe materializar un repo reemplazado")

        self.assertEqual(set(temporal.glob("orq-verify-*")), antes)

    def test_fallo_de_worktree_add_no_deja_directorio_temporal_vacio(self):
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(self.repo), "test: worktree add fallido"
        )
        temporal = self.repo.parent
        antes = set(temporal.glob("orq-verify-*"))
        # Al usar esta raiz temporal dedicada, su primer descendiente debe
        # satisfacer el mismo contrato 0700 que exige produccion bajo /tmp.
        self.repo.chmod(0o700)
        git_real = orqlib._git

        def fallar_worktree_add(carpeta, *args, **kwargs):
            if args[:2] == ("worktree", "add"):
                return subprocess.CompletedProcess(
                    ["git", *args], 1, stdout="", stderr="fallo simulado"
                )
            return git_real(carpeta, *args, **kwargs)

        with mock.patch.object(orqlib, "TEMP_ROOT", str(temporal)), \
                mock.patch.object(
                    orqlib, "_git", side_effect=fallar_worktree_add):
            with self.assertRaisesRegex(OSError, "crear el checkout"):
                with orqlib.materializar_snapshot_verificacion(snapshot):
                    self.fail("no debe entregar un checkout incompleto")

        self.assertEqual(set(temporal.glob("orq-verify-*")), antes)

    def test_fallo_al_validar_padre_temporal_no_deja_nombre_ni_fd(self):
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(self.repo), "test: validar padre temporal"
        )
        temporal = self.repo.parent
        antes = set(temporal.glob("orq-verify-*"))

        for operacion in ("fstat", "fchmod"):
            with self.subTest(operacion=operacion):
                fds_antes = {p.name for p in pathlib.Path("/proc/self/fd").iterdir()}
                with mock.patch.object(orqlib, "TEMP_ROOT", str(temporal)), \
                        mock.patch.object(
                            orqlib.os, operacion,
                            side_effect=OSError("fallo simulado")):
                    with self.assertRaisesRegex(OSError, "fallo simulado"):
                        with orqlib.materializar_snapshot_verificacion(snapshot):
                            self.fail("no debe entregar un temporal sin validar")
                fds_despues = {
                    p.name for p in pathlib.Path("/proc/self/fd").iterdir()
                }
                self.assertEqual(fds_despues, fds_antes)
                self.assertEqual(set(temporal.glob("orq-verify-*")), antes)

    def test_snapshot_rechaza_skip_worktree_y_assume_unchanged(self):
        for opcion, limpiar in (
            ("--skip-worktree", "--no-skip-worktree"),
            ("--assume-unchanged", "--no-assume-unchanged"),
        ):
            with self.subTest(opcion=opcion):
                self.git("update-index", opcion, "README.md")
                try:
                    with self.assertRaisesRegex(
                            OSError, "skip-worktree|assume-unchanged"):
                        orqlib.preparar_snapshot_verificacion(
                            str(self.repo), "test: flags de indice"
                        )
                finally:
                    self.git("update-index", limpiar, "README.md")

    def test_rama_nueva_escanea_secreto_borrado_de_un_commit_anterior(self):
        self.git("switch", "-qc", "feature-historial")
        # Se construye en tiempo de ejecución para que el propio repositorio de
        # pruebas no almacene un literal con aspecto de credencial. El commit
        # temporal sí recibe el patrón completo que debe detectar el scanner.
        prefijo = "".join(chr(valor) for valor in (
            67, 76, 73, 69, 78, 84, 95, 83, 69, 67, 82, 69, 84, 61,
        ))
        marcador_sintetico = prefijo + "H" * 32
        ruta = self.repo / "temporal.txt"
        ruta.write_text(marcador_sintetico + "\n", encoding="utf-8")
        self.git("add", "temporal.txt")
        self.git("commit", "-qm", "incluye secreto historico")
        ruta.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "borra secreto del tip")

        resultado = orqlib.publicar_repo(str(self.repo))

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["fase"], "seguridad")
        remoto = subprocess.run(
            ["/usr/bin/git", "--git-dir", str(self.remote), "show-ref",
             "--verify", "--quiet", "refs/heads/feature-historial"],
            check=False,
        )
        self.assertNotEqual(remoto.returncode, 0)
        self.assertNotIn(marcador_sintetico, str(resultado))

    def test_ref_origin_falsa_y_refspec_custom_no_ocultan_ancestro(self):
        self.git("switch", "-qc", "feature-ref-falsa")
        prefijo = "".join(chr(x) for x in (
            67, 76, 73, 69, 78, 84, 95, 83, 69, 67, 82, 69, 84, 61,
        ))
        marcador_historial = prefijo + "R" * 32
        ruta = self.repo / "historico.txt"
        ruta.write_text(marcador_historial + "\n", encoding="utf-8")
        self.git("add", "historico.txt")
        self.git("commit", "-qm", "secreto que refs locales no deben ocultar")
        oid_marcador_historial = self.git("rev-parse", "HEAD").stdout.strip()
        ruta.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "borra secreto del tip")
        oid_tip = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("update-ref", "refs/remotes/origin/feature-ref-falsa", oid_tip)
        self.git("config", "--unset-all", "remote.origin.fetch")
        self.git(
            "config", "--add", "remote.origin.fetch",
            "+refs/heads/main:refs/remotes/origin/main",
        )

        resultado = orqlib.publicar_repo(str(self.repo))

        self.assertFalse(resultado["ok"], resultado)
        self.assertEqual(resultado["fase"], "seguridad")
        remoto = subprocess.run(
            ["/usr/bin/git", "--git-dir", str(self.remote), "cat-file", "-e",
             oid_marcador_historial], check=False, capture_output=True,
        )
        self.assertNotEqual(remoto.returncode, 0)

    def test_exceso_remoto_despues_del_preflight_limpia_namespace(self):
        base_oid = self.git("rev-parse", "HEAD").stdout.strip()
        self.git(
            "push", "-q", "origin", f"{base_oid}:refs/heads/extra-remota"
        )
        git_real = orqlib._git
        preflight_ocultado = False

        def ocultar_extra_en_primer_ls(carpeta, *args, **kwargs):
            nonlocal preflight_ocultado
            resultado = git_real(carpeta, *args, **kwargs)
            if (not preflight_ocultado
                    and args[:3] == ("ls-remote", "--heads", "--")):
                preflight_ocultado = True
                lineas = [
                    linea for linea in resultado.stdout.splitlines()
                    if linea.endswith("\trefs/heads/main")
                ]
                return subprocess.CompletedProcess(
                    resultado.args, resultado.returncode,
                    stdout="\n".join(lineas) + ("\n" if lineas else ""),
                    stderr=resultado.stderr,
                )
            return resultado

        with mock.patch.object(orqlib, "MAX_REFS_REMOTAS", 1), \
                mock.patch.object(
                    orqlib, "_git", side_effect=ocultar_extra_en_primer_ls):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertTrue(preflight_ocultado)
        self.assertFalse(resultado["ok"], resultado)
        self.assertEqual(resultado["fase"], "fetch")
        residuo = self.git(
            "for-each-ref", "--format=%(refname)", "refs/orq-audit/"
        ).stdout
        self.assertEqual(residuo, "")

    def test_replace_no_oculta_ancestro_sensible_al_publicar(self):
        base_oid = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("switch", "-qc", "feature-replace")
        prefijo = "".join(chr(x) for x in (
            67, 76, 73, 69, 78, 84, 95, 83, 69, 67, 82, 69, 84, 61,
        ))
        marcador_historial = prefijo + "P" * 32
        ruta = self.repo / "replace.txt"
        ruta.write_text(marcador_historial + "\n", encoding="utf-8")
        self.git("add", "replace.txt")
        self.git("commit", "-qm", "secreto reemplazado localmente")
        oid_marcador_historial = self.git("rev-parse", "HEAD").stdout.strip()
        ruta.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "tip limpio")
        self.git("replace", oid_marcador_historial, base_oid)

        resultado = orqlib.publicar_repo(str(self.repo))

        self.assertFalse(resultado["ok"], resultado)
        self.assertEqual(resultado["fase"], "seguridad")
        remoto = subprocess.run(
            ["/usr/bin/git", "--git-dir", str(self.remote), "cat-file", "-e",
             oid_marcador_historial], check=False, capture_output=True,
        )
        self.assertNotEqual(remoto.returncode, 0)

    def test_snapshot_rechaza_grafts_y_limites_shallow(self):
        base_oid = self.git("rev-parse", "HEAD").stdout.strip()
        grafts = self.repo / ".git" / "info" / "grafts"
        shallow = self.repo / ".git" / "shallow"
        for ruta, contenido in ((grafts, base_oid + "\n"),
                                (shallow, base_oid + "\n")):
            with self.subTest(ruta=ruta.name):
                ruta.write_text(contenido, encoding="utf-8")
                try:
                    with self.assertRaisesRegex(OSError, "shallow|grafts"):
                        orqlib.preparar_snapshot_verificacion(
                            str(self.repo), "test: historial completo"
                        )
                finally:
                    ruta.unlink()

    def test_snapshot_detecta_graft_transitorio_durante_el_escaneo(self):
        grafts = self.repo / ".git" / "info" / "grafts"
        base_oid = self.git("rev-parse", "HEAD").stdout.strip()
        escaner_real = orqlib._escanear_commit_fd
        inyectado = False

        def escanear_con_graft_transitorio(repo_fd, commit_oid):
            nonlocal inyectado
            grafts.write_text(base_oid + "\n", encoding="utf-8")
            inyectado = True
            try:
                return escaner_real(repo_fd, commit_oid)
            finally:
                grafts.unlink()

        with mock.patch.object(
                orqlib, "_escanear_commit_fd",
                side_effect=escanear_con_graft_transitorio):
            with self.assertRaisesRegex(OSError, "topologia Git cambio"):
                orqlib.preparar_snapshot_verificacion(
                    str(self.repo), "test: graft transitorio"
                )

        self.assertTrue(inyectado)
        self.assertFalse(grafts.exists())

    def test_snapshot_rechaza_linked_worktree_con_error_explicito(self):
        enlazado = self.repo.parent / "linked"
        self.git("worktree", "add", "-q", "-b", "linked-test", str(enlazado))
        try:
            with self.assertRaisesRegex(OSError, "linked worktrees"):
                orqlib.preparar_snapshot_verificacion(
                    str(enlazado), "test: linked worktree no soportado"
                )
        finally:
            self.git("worktree", "remove", "--force", str(enlazado))

    def test_publicacion_conserva_el_repo_adquirido_entre_todas_las_fases(self):
        (self.repo / "estable.txt").write_text("cambio seguro\n", encoding="utf-8")
        movido = self.repo.parent / "repo-adquirido"
        reemplazo = self.repo.parent / "repo-reemplazo"
        reemplazo.mkdir()
        self.git_at(reemplazo, "init", "-q", "-b", "main")
        (reemplazo / "identity").write_text("FUERA\n", encoding="utf-8")
        git_real = orqlib._git
        intercambiado = False

        def intercalar(carpeta, *args, **kwargs):
            nonlocal intercambiado
            resultado = git_real(carpeta, *args, **kwargs)
            if (not intercambiado and kwargs.get("cwd_fd") is not None
                    and args[:3] == ("fetch", "--quiet", "--no-tags")):
                intercambiado = True
                self.repo.rename(movido)
                self.repo.symlink_to(reemplazo, target_is_directory=True)
            return resultado

        with mock.patch.object(orqlib, "_git", side_effect=intercalar):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertTrue(intercambiado)
        self.assertTrue(resultado["ok"], resultado)
        self.assertFalse((reemplazo / "estable.txt").exists())
        remoto = self.git_at(
            self.remote, "show", "refs/heads/main:estable.txt"
        ).stdout
        self.assertEqual(remoto, "cambio seguro\n")

    def test_publicacion_acepta_subdirectorio_y_liga_cwd_mas_raiz_verificados(self):
        subdirectorio = self.repo / "src"
        subdirectorio.mkdir()
        (subdirectorio / "app.py").write_text("print('ok')\n", encoding="utf-8")
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(subdirectorio), "test: cwd adquirido"
        )

        resultado = orqlib.publicar_repo(
            str(subdirectorio), mensaje="test: cwd adquirido",
            identidad_esperada=snapshot["cwd_identidad"],
            identidad_repo_esperada=snapshot["repo_identidad"],
            snapshot_esperado=snapshot,
        )

        self.assertTrue(resultado["ok"], resultado)
        publicado = self.git_at(
            self.remote, "show", "refs/heads/main:src/app.py"
        ).stdout
        self.assertEqual(publicado, "print('ok')\n")
        remoto_oid = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto_oid, snapshot["commit_oid"])

    def test_publicacion_rechaza_reemplazo_entre_verificacion_y_publicacion(self):
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(self.repo), "test: no publicar reemplazo"
        )
        original = self.repo.parent / "repo-verificado"
        reemplazo = self.repo.parent / "repo-reemplazo-identidad"
        reemplazo.mkdir()
        self.repo.rename(original)
        reemplazo.rename(self.repo)

        with mock.patch.object(orqlib, "_publicar_repo_adquirido") as publicar:
            resultado = orqlib.publicar_repo(
                str(self.repo),
                identidad_esperada=snapshot["cwd_identidad"],
                identidad_repo_esperada=snapshot["repo_identidad"],
                snapshot_esperado=snapshot,
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["fase"], "seguridad")
        publicar.assert_not_called()

    def test_push_publica_el_oid_auditado_aunque_la_rama_local_cambie(self):
        base_oid = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "no-publicar.txt").write_text(
            "commit fuera de la auditoria\n", encoding="utf-8"
        )
        self.git("add", "no-publicar.txt")
        self.git("commit", "-qm", "commit que no debe viajar")
        oid_sustituto = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("reset", "--hard", "-q", base_oid)
        (self.repo / "seguro.txt").write_text("cambio auditado\n", encoding="utf-8")

        git_real = orqlib._git
        refspecs = []

        def mover_rama(carpeta, *args, **kwargs):
            if args[:1] == ("push",):
                refspecs.append(args[-1])
                self.git("update-ref", "refs/heads/main", oid_sustituto)
            return git_real(carpeta, *args, **kwargs)

        with mock.patch.object(orqlib, "_git", side_effect=mover_rama):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(len(refspecs), 1)
        oid_auditado, destino = refspecs[0].split(":", 1)
        self.assertNotEqual(oid_auditado, oid_sustituto)
        self.assertEqual(destino, "refs/heads/main")
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto, oid_auditado)
        ausente = subprocess.run(
            ["/usr/bin/git", "--git-dir", str(self.remote), "cat-file", "-e",
             f"{remoto}:no-publicar.txt"],
            check=False, capture_output=True, text=True,
        )
        self.assertNotEqual(ausente.returncode, 0)

    def test_lease_rechaza_cambio_remoto_despues_del_inventario(self):
        base_oid = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "competidor.txt").write_text("B\n", encoding="utf-8")
        self.git("add", "competidor.txt")
        self.git("commit", "-qm", "commit competidor remoto")
        oid_competidor = self.git("rev-parse", "HEAD").stdout.strip()
        self.git(
            "push", "-q", "origin",
            f"{oid_competidor}:refs/heads/objeto-competidor",
        )
        self.git("reset", "--hard", "-q", base_oid)
        (self.repo / "seguro.txt").write_text("snapshot A\n", encoding="utf-8")
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(self.repo), "test: lease remoto"
        )
        git_real = orqlib._git
        movido = False

        def mover_remoto_antes_de_push(carpeta, *args, **kwargs):
            nonlocal movido
            if not movido and args[:1] == ("push",):
                movido = True
                self.git_at(
                    self.remote, "update-ref", "refs/heads/main", oid_competidor
                )
            return git_real(carpeta, *args, **kwargs)

        with mock.patch.object(orqlib, "_git", side_effect=mover_remoto_antes_de_push):
            resultado = orqlib.publicar_repo(
                str(self.repo), identidad_esperada=snapshot["cwd_identidad"],
                identidad_repo_esperada=snapshot["repo_identidad"],
                snapshot_esperado=snapshot,
            )

        self.assertTrue(movido)
        self.assertFalse(resultado["ok"], resultado)
        self.assertEqual(resultado["fase"], "push")
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto, oid_competidor)
        self.assertNotEqual(remoto, snapshot["commit_oid"])

    def test_ruta_directa_rechaza_tip_final_no_descendiente_del_remoto(self):
        remoto_antes = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        tree = self.git("rev-parse", "HEAD^{tree}").stdout.strip()
        root_ajeno = self.git(
            "commit-tree", tree, "-m", "root no relacionado"
        ).stdout.strip()
        (self.repo / "pendiente.txt").write_text("cambio\n", encoding="utf-8")
        git_real = orqlib._git
        intercambiado = False

        def mover_head_antes_del_stage(carpeta, *args, **kwargs):
            nonlocal intercambiado
            if not intercambiado and args == ("add", "-A"):
                intercambiado = True
                self.git(
                    "update-ref", "refs/heads/main", root_ajeno, remoto_antes
                )
            return git_real(carpeta, *args, **kwargs)

        with mock.patch.object(
                orqlib, "_git", side_effect=mover_head_antes_del_stage):
            resultado = orqlib.publicar_repo(str(self.repo))

        self.assertTrue(intercambiado)
        self.assertFalse(resultado["ok"], resultado)
        self.assertEqual(resultado["fase"], "sincronizacion")
        remoto_despues = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto_despues, remoto_antes)

    def test_snapshot_rechaza_edicion_persistente_despues_de_las_pruebas(self):
        (self.repo / "app.py").write_text("RESULTADO = True\n", encoding="utf-8")
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(self.repo), "test: contenido probado"
        )
        (self.repo / "app.py").write_text("RESULTADO = False\n", encoding="utf-8")

        resultado = orqlib.publicar_repo(
            str(self.repo), identidad_esperada=snapshot["cwd_identidad"],
            identidad_repo_esperada=snapshot["repo_identidad"],
            snapshot_esperado=snapshot,
        )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["fase"], "seguridad")
        self.assertIn("contenido cambio", resultado["detalle"])
        remoto = self.git_at(
            self.remote, "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remoto, snapshot["base_oid"])

    def test_snapshot_no_hereda_helper_ignorado_que_gobierna_las_pruebas(self):
        (self.repo / ".gitignore").write_text("helper.py\n", encoding="utf-8")
        (self.repo / "test_app.py").write_text(
            "import helper, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_resultado(self): self.assertTrue(helper.RESULTADO)\n",
            encoding="utf-8",
        )
        self.git("add", ".gitignore", "test_app.py")
        self.git("commit", "-qm", "prueba que declara dependencia ignorada")
        self.git("push", "-q", "origin", "main")
        (self.repo / "helper.py").write_text("RESULTADO = True\n", encoding="utf-8")
        original = orqlib.ejecutar_comando_verificacion(
            "python3 -m unittest test_app.py", str(self.repo), 20
        )
        self.assertEqual(original["rc"], 0, original["resultado"])

        snapshot = orqlib.preparar_snapshot_verificacion(
            str(self.repo), "test: sin dependencias ignoradas"
        )
        with orqlib.materializar_snapshot_verificacion(snapshot) as (
                ruta, cwd_fd):
            self.assertFalse((pathlib.Path(ruta) / "helper.py").exists())
            aislada = orqlib.ejecutar_comando_verificacion(
                "python3 -m unittest test_app.py", ruta, 20, cwd_fd=cwd_fd
            )

        self.assertNotEqual(aislada["rc"], 0)

    def test_snapshot_rechaza_symlink_tracked_hacia_dependencia_externa(self):
        externo = self.repo.parent / "helper-externo.py"
        externo.write_text("RESULTADO = True\n", encoding="utf-8")
        (self.repo / "helper.py").symlink_to(externo)
        (self.repo / "test_symlink.py").write_text(
            "import helper, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_resultado(self): self.assertTrue(helper.RESULTADO)\n",
            encoding="utf-8",
        )
        self.git("add", "helper.py", "test_symlink.py")
        self.git("commit", "-qm", "dependencia enlazada no reproducible")
        tree_antes = self.git("rev-parse", "HEAD^{tree}").stdout.strip()
        externo.write_text("RESULTADO = False\n", encoding="utf-8")
        tree_despues = self.git("rev-parse", "HEAD^{tree}").stdout.strip()

        self.assertEqual(tree_antes, tree_despues)
        with self.assertRaisesRegex(OSError, "symlinks"):
            orqlib.preparar_snapshot_verificacion(
                str(self.repo), "test: rechazar symlink externo"
            )

    def test_preparacion_usa_base_oid_literal_si_head_se_mueve(self):
        base_oid = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "otro.txt").write_text("B\n", encoding="utf-8")
        self.git("add", "otro.txt")
        self.git("commit", "-qm", "commit B")
        oid_b = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("reset", "--hard", "-q", base_oid)
        tree_base = self.git("rev-parse", f"{base_oid}^{{tree}}").stdout.strip()
        tree_real = orqlib._tree_publicable_fd
        movido = False

        def mover_head(repo_fd, oid_base):
            nonlocal movido
            if not movido:
                movido = True
                self.git("update-ref", "refs/heads/main", oid_b)
            return tree_real(repo_fd, oid_base)

        with mock.patch.object(orqlib, "_tree_publicable_fd", side_effect=mover_head):
            snapshot = orqlib.preparar_snapshot_verificacion(
                str(self.repo), "test: base literal"
            )

        self.assertTrue(movido)
        self.assertEqual(snapshot["base_oid"], base_oid)
        self.assertEqual(snapshot["tree_oid"], tree_base)
        self.assertEqual(snapshot["commit_oid"], base_oid)

    def test_cambio_del_indice_despues_de_write_tree_no_entra_al_commit(self):
        ruta = self.repo / "resultado.txt"
        ruta.write_text("CONTENIDO_PROBADO\n", encoding="utf-8")
        snapshot = orqlib.preparar_snapshot_verificacion(
            str(self.repo), "test: indice congelado"
        )
        git_real = orqlib._git
        alterado = False

        def mutar_indice(carpeta, *args, **kwargs):
            nonlocal alterado
            resultado = git_real(carpeta, *args, **kwargs)
            if (not alterado and args == ("write-tree",)
                    and kwargs.get("indice") is None):
                alterado = True
                ruta.write_text("CONTENIDO_NO_PROBADO\n", encoding="utf-8")
                self.git("add", "resultado.txt")
            return resultado

        with mock.patch.object(orqlib, "_git", side_effect=mutar_indice):
            resultado = orqlib.publicar_repo(
                str(self.repo), identidad_esperada=snapshot["cwd_identidad"],
                identidad_repo_esperada=snapshot["repo_identidad"],
                snapshot_esperado=snapshot,
            )

        self.assertTrue(alterado)
        self.assertTrue(resultado["ok"], resultado)
        publicado = self.git_at(
            self.remote, "show", "refs/heads/main:resultado.txt"
        ).stdout
        self.assertEqual(publicado, "CONTENIDO_PROBADO\n")


if __name__ == "__main__":
    unittest.main()
