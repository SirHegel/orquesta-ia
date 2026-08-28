import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import orqchat
import orqlib
import orqrun


class LimitesDeRutasTests(unittest.TestCase):
    def test_home_de_cuenta_rechaza_escape_y_enlace_intermedio(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "instalacion")
            fuera = os.path.join(tmp, "fuera")
            os.makedirs(os.path.join(base, "accounts"))
            os.makedirs(fuera)
            os.symlink(fuera, os.path.join(base, "accounts", "enlace"))
            real_interno = os.path.join(base, "accounts", "real")
            os.makedirs(real_interno)
            os.symlink(real_interno, os.path.join(base, "accounts", "alias"))
            with mock.patch.object(orqlib, "BASE", base), mock.patch.object(
                orqlib, "ACCOUNTS", os.path.join(base, "accounts")
            ):
                with self.assertRaises(ValueError):
                    orqlib.home_de("cuenta", {"home": "../../fuera"})
                with self.assertRaises(ValueError):
                    orqlib.home_de("cuenta", {"home": "accounts/enlace"})
                with self.assertRaises(ValueError):
                    orqlib.home_de("cuenta", {"home": "accounts/alias"})

    def test_clave_no_puede_salir_por_config_ni_symlink_y_queda_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "instalacion")
            home = os.path.join(base, "accounts", "cuenta")
            fuera = os.path.join(tmp, "fuera.txt")
            os.makedirs(home)
            with open(fuera, "w", encoding="utf-8") as archivo:
                archivo.write("intacto")
            enlace = os.path.join(home, "api_key")
            os.symlink(fuera, enlace)
            perfil = {
                "provider": "minimax",
                "home": "accounts/cuenta",
                "api_key_file": enlace,
            }
            with mock.patch.object(orqlib, "BASE", base):
                with self.assertRaises(ValueError):
                    orqlib.guardar_api_key("cuenta", perfil, "x" * 24)
                os.unlink(enlace)
                os.link(fuera, enlace)
                orqlib.guardar_api_key("cuenta", perfil, "y" * 24)
                destino = orqlib.ruta_cuenta(
                    "cuenta", perfil, "api_key_file", "api_key"
                )

            self.assertEqual(stat.S_IMODE(os.stat(destino).st_mode), 0o600)
            self.assertNotEqual(os.stat(destino).st_ino, os.stat(fuera).st_ino)
            with open(fuera, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), "intacto")

    def test_workspace_temporal_exige_ancestro_privado_y_no_sigue_escape(self):
        with tempfile.TemporaryDirectory() as privado:
            proyecto = os.path.join(privado, "proyecto")
            os.makedirs(proyecto)
            self.assertEqual(orqlib.ruta_trabajo_segura(proyecto), proyecto)
            escape = os.path.join(privado, "escape")
            os.symlink("/etc", escape)
            self.assertIsNone(orqlib.ruta_trabajo_segura(escape))

    def test_perfil_no_puede_convertir_ssh_u_otra_credencial_en_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "orquesta")
            home_usuario = os.path.join(tmp, "home")
            os.makedirs(os.path.join(base, "accounts", "cuenta"))
            os.makedirs(os.path.join(home_usuario, ".ssh"))
            with mock.patch.object(orqlib, "BASE", base), mock.patch.object(
                orqlib, "HOME_USUARIO", home_usuario
            ):
                with self.assertRaises(ValueError):
                    orqlib.home_de(
                        "cuenta",
                        {"provider": "minimax", "home": os.path.join(home_usuario, ".ssh")},
                    )
                for nombre in (".credentials.json", "auth.json", "id_ed25519"):
                    perfil = {
                        "provider": "minimax", "home": "accounts/cuenta",
                        "api_key_file": nombre,
                    }
                    self.assertIsNone(orqlib.ruta_api_key("cuenta", perfil))

    def test_endpoint_minimax_ajeno_falla_antes_de_leer_la_clave(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "accounts", "cuenta"))
            perfil = {
                "provider": "minimax", "home": "accounts/cuenta",
                "base_url": "https://atacante.invalid/anthropic",
            }
            with mock.patch.object(orqlib, "BASE", base), mock.patch.object(
                orqlib, "_leer_texto"
            ) as leer:
                with self.assertRaises(ValueError):
                    orqlib.entorno("cuenta", perfil)
            leer.assert_not_called()

    def test_clave_legible_por_grupo_o_mundo_no_llega_al_entorno(self):
        with tempfile.TemporaryDirectory() as base:
            home = os.path.join(base, "accounts", "cuenta")
            os.makedirs(home)
            clave = os.path.join(home, "api_key")
            with open(clave, "w", encoding="utf-8") as archivo:
                archivo.write("secreto-sintetico-no-exportable")
            perfil = {"provider": "minimax", "home": "accounts/cuenta",
                      "api_key_file": "api_key"}
            with mock.patch.object(orqlib, "BASE", base):
                for modo in (0o644, 0o660):
                    with self.subTest(modo=oct(modo)):
                        os.chmod(clave, modo)
                        self.assertFalse(orqlib.autenticado("cuenta", perfil))
                        with self.assertRaises(ValueError):
                            orqlib.entorno("cuenta", perfil)


class ConfiguracionPrivadaTests(unittest.TestCase):
    def test_cfg_ausente_es_la_unica_configuracion_vacia_implicita(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib, "PROFILES", os.path.join(tmp, "profiles.json")
        ):
            self.assertEqual(orqlib.cfg(), {"profiles": {}})

    def test_cfg_rechaza_modo_inseguro_sin_reemplazar_el_archivo(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = os.path.join(tmp, "profiles.json")
            contenido = b'{"profiles":{"cuenta":{"provider":"gpt"}}}\n'
            with open(ruta, "wb") as archivo:
                archivo.write(contenido)
            os.chmod(ruta, 0o644)

            with mock.patch.object(orqlib, "PROFILES", ruta):
                with self.assertRaises(orqlib.ErrorConfiguracion):
                    config = orqlib.cfg()
                    config["profiles"] = {}
                    orqlib.guardar_cfg(config)

            with open(ruta, "rb") as archivo:
                self.assertEqual(archivo.read(), contenido)
            self.assertEqual(stat.S_IMODE(os.stat(ruta).st_mode), 0o644)

    def test_cfg_rechaza_json_invalido_y_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalido = os.path.join(tmp, "invalido.json")
            with open(invalido, "w", encoding="utf-8") as archivo:
                archivo.write("{json truncado")
            os.chmod(invalido, 0o600)
            with mock.patch.object(orqlib, "PROFILES", invalido):
                with self.assertRaisesRegex(
                        orqlib.ErrorConfiguracion, "JSON valido"):
                    orqlib.cfg()

            victima = os.path.join(tmp, "victima.json")
            contenido = '{"profiles":{"intacta":{}}}\n'
            with open(victima, "w", encoding="utf-8") as archivo:
                archivo.write(contenido)
            os.chmod(victima, 0o600)
            enlace = os.path.join(tmp, "profiles.json")
            os.symlink(victima, enlace)
            with mock.patch.object(orqlib, "PROFILES", enlace):
                with self.assertRaises(orqlib.ErrorConfiguracion):
                    orqlib.cfg()
            with open(victima, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), contenido)


class OperacionesAtomicasTests(unittest.TestCase):
    def test_purge_vacia_por_fd_y_conserva_directorios_sin_tocar_victima(self):
        with tempfile.TemporaryDirectory() as base:
            accounts = os.path.join(base, "accounts")
            home = os.path.join(accounts, "cuenta")
            anidado = os.path.join(home, "anidado")
            os.makedirs(anidado, mode=0o700)
            os.chmod(home, 0o700)
            secreto = os.path.join(anidado, "secreto")
            with open(secreto, "w", encoding="utf-8") as archivo:
                archivo.write("borrar")
            os.chmod(secreto, 0o600)
            with mock.patch.object(orqlib, "BASE", base), mock.patch.object(
                orqlib, "ACCOUNTS", accounts
            ):
                self.assertTrue(orqlib.purgar_home_cuenta(
                    "cuenta", {"provider": "gpt", "home": "accounts/cuenta"}
                ))

            self.assertTrue(os.path.isdir(home))
            self.assertTrue(os.path.isdir(anidado))
            self.assertEqual(os.listdir(home), ["anidado"])
            self.assertEqual(os.listdir(anidado), ["secreto"])
            with open(secreto, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), "")

    def test_purge_preserva_y_rechaza_una_entrada_no_regular(self):
        with tempfile.TemporaryDirectory() as base:
            accounts = os.path.join(base, "accounts")
            home = os.path.join(accounts, "cuenta")
            os.makedirs(home, mode=0o700)
            os.chmod(home, 0o700)
            victima = os.path.join(base, "victima")
            with open(victima, "w", encoding="utf-8") as archivo:
                archivo.write("intacto")
            enlace = os.path.join(home, "enlace")
            os.symlink(victima, enlace)
            with mock.patch.object(orqlib, "BASE", base), mock.patch.object(
                orqlib, "ACCOUNTS", accounts
            ):
                resultado = orqlib.purgar_home_cuenta(
                    "cuenta", {"provider": "gpt", "home": "accounts/cuenta"}
                )

            self.assertFalse(resultado)
            self.assertTrue(os.path.islink(enlace))
            with open(victima, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), "intacto")

    def test_purge_no_trunca_inode_sustituto_entre_stat_y_open(self):
        with tempfile.TemporaryDirectory() as base:
            accounts = os.path.join(base, "accounts")
            home = os.path.join(accounts, "cuenta")
            os.makedirs(home, mode=0o700)
            os.chmod(home, 0o700)
            token = os.path.join(home, "token")
            guardado = os.path.join(base, "token-original")
            victima = os.path.join(base, "victima")
            with open(token, "w", encoding="utf-8") as archivo:
                archivo.write("ORIGINAL_ACCOUNT")
            with open(victima, "w", encoding="utf-8") as archivo:
                archivo.write("VICTIM_DATA")
            os.chmod(token, 0o600)
            os.chmod(victima, 0o600)
            open_real = os.open
            intercambiado = False

            def intercalar(nombre, flags, *args, **kwargs):
                nonlocal intercambiado
                if (not intercambiado and nombre == "token"
                        and flags & os.O_ACCMODE == os.O_WRONLY):
                    intercambiado = True
                    os.rename(token, guardado)
                    os.rename(victima, token)
                return open_real(nombre, flags, *args, **kwargs)

            with mock.patch.object(orqlib, "BASE", base), mock.patch.object(
                orqlib, "ACCOUNTS", accounts
            ), mock.patch.object(orqlib.os, "open", side_effect=intercalar):
                resultado = orqlib.purgar_home_cuenta(
                    "cuenta", {"provider": "gpt", "home": "accounts/cuenta"}
                )

            self.assertTrue(intercambiado)
            self.assertFalse(resultado)
            with open(token, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), "VICTIM_DATA")
            with open(guardado, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), "ORIGINAL_ACCOUNT")

    def test_estado_json_no_lee_ni_reemplaza_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            fuera = os.path.join(tmp, "fuera.json")
            enlace = os.path.join(tmp, "estado.json")
            with open(fuera, "w", encoding="utf-8") as archivo:
                archivo.write('{"valor": "intacto"}')
            os.symlink(fuera, enlace)

            self.assertEqual(orqlib._leer(enlace, {"seguro": True}), {"seguro": True})
            with self.assertRaises(OSError):
                orqlib._escribir(enlace, {"valor": "alterado"})
            with open(fuera, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), '{"valor": "intacto"}')

    def test_walker_omite_archivos_y_directorios_enlazados(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = os.path.join(tmp, "sesiones")
            fuera = os.path.join(tmp, "fuera")
            os.makedirs(os.path.join(raiz, "proyecto"))
            os.makedirs(fuera)
            bueno = os.path.join(raiz, "proyecto", "bueno.jsonl")
            secreto = os.path.join(fuera, "secreto.jsonl")
            for ruta in (bueno, secreto):
                with open(ruta, "w", encoding="utf-8") as archivo:
                    archivo.write("{}\n")
            os.symlink(secreto, os.path.join(raiz, "proyecto", "enlace.jsonl"))
            os.symlink(fuera, os.path.join(raiz, "directorio-enlace"))

            encontrados = orqlib._archivos_regulares(
                raiz, lambda nombre: nombre.endswith(".jsonl")
            )

            self.assertEqual([ruta for _, ruta in encontrados], [bueno])

    def test_ledger_rechaza_symlink_y_hardlink_sin_modificar_victima(self):
        with tempfile.TemporaryDirectory() as tmp:
            victima = os.path.join(tmp, "victima.txt")
            ledger = os.path.join(tmp, "ledger.jsonl")
            lock = os.path.join(tmp, ".lock")
            with open(victima, "w", encoding="utf-8") as archivo:
                archivo.write("intacto\n")
            for clase in ("symlink", "hardlink"):
                with self.subTest(clase=clase):
                    try:
                        os.unlink(ledger)
                    except FileNotFoundError:
                        pass
                    if clase == "symlink":
                        os.symlink(victima, ledger)
                    else:
                        os.link(victima, ledger)
                    with mock.patch.object(orqlib, "LEDGER", ledger), \
                            mock.patch.object(orqlib, "LOCK", lock):
                        self.assertEqual(orqlib.ledger_rows(), [])
                        with self.assertRaises(OSError):
                            orqlib.log({"prompt": "no debe anexarse"})
                    with open(victima, encoding="utf-8") as archivo:
                        self.assertEqual(archivo.read(), "intacto\n")

    def test_ledger_omite_linea_gigante_y_conserva_registro_posterior(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "ledger.jsonl")
            esperado = {"fecha": "2026-08-27", "tokens": 7}
            with open(ledger, "w", encoding="utf-8") as archivo:
                archivo.write("x" * (1024 * 1024 + 100) + "\n")
                archivo.write(json.dumps(esperado) + "\n")
            os.chmod(ledger, 0o600)
            with mock.patch.object(orqlib, "LEDGER", ledger):
                self.assertEqual(orqlib.ledger_rows(), [esperado])

    def test_copia_de_imagen_nunca_reemplaza_una_colision(self):
        with tempfile.TemporaryDirectory() as tmp:
            origen = os.path.join(tmp, "origen.bin")
            destino = os.path.join(tmp, "foto.jpg")
            with open(origen, "wb") as archivo:
                archivo.write(b"\xff\xd8\xffimagen-nueva")
            os.chmod(origen, 0o600)
            with open(destino, "wb") as archivo:
                archivo.write(b"imagen-anterior")
            nuevo = orqlib._copiar_regular_unico(origen, destino)
            self.assertEqual(nuevo, os.path.join(tmp, "foto-1.jpg"))
            with open(destino, "rb") as archivo:
                self.assertEqual(archivo.read(), b"imagen-anterior")

    def test_generacion_antigravity_serializa_el_scratch_compartido(self):
        activas = 0
        maximas = 0
        mutex = threading.Lock()

        def simular(*_args, **_kwargs):
            nonlocal activas, maximas
            with mutex:
                activas += 1
                maximas = max(maximas, activas)
            time.sleep(0.08)
            with mutex:
                activas -= 1
            return {"rc": 0}

        with tempfile.TemporaryDirectory() as base, mock.patch.object(
            orqlib, "BASE", base
        ), mock.patch.object(
            orqlib, "_generar_imagen_bloqueada", side_effect=simular
        ):
            hilos = [threading.Thread(target=orqlib.generar_imagen, args=(
                f"agy-{n}", {"provider": "antigravity"}, "imagen"
            )) for n in range(2)]
            for hilo in hilos:
                hilo.start()
            for hilo in hilos:
                hilo.join(2)
        self.assertEqual(maximas, 1)


class ComandosYMemoriaTests(unittest.TestCase):
    def test_parser_remoto_es_lineal_y_coherente_con_el_supervisor(self):
        validos = (
            "/tmp/remoto.git", "file:///tmp/remoto.git",
            "https://github.com/org/repo.git",
            "https://github.com:443/org/repo.git",
            "ssh://git@github.com:22/org/repo.git",
            "git@github.com:org/repo.git",
        )
        invalidos = (
            "ext::helper", "https://usuario@host/repo",
            "https://host", "ssh://usuario con espacio@host/repo",
            "git@@host:ruta", "git@host", "git@host:con espacio",
        )
        for valor in validos:
            with self.subTest(valor=valor):
                self.assertTrue(orqlib._remoto_git_permitido(valor))
                self.assertTrue(orqrun._remoto_literal(valor))
        for valor in invalidos:
            with self.subTest(valor=valor):
                self.assertFalse(orqlib._remoto_git_permitido(valor))
                self.assertFalse(orqrun._remoto_literal(valor))

        adverso = "-@" * 200_000 + ":repo"
        inicio = time.monotonic()
        self.assertFalse(orqlib._remoto_git_permitido(adverso))
        self.assertFalse(orqrun._remoto_literal(adverso))
        self.assertLess(time.monotonic() - inicio, 1.0)

    def test_supervisor_deriva_checkout_desde_tmp_canonico(self):
        oid = "a" * 40
        checkout = "/private/tmp/orq-verify-prueba.123/checkout"
        with mock.patch.object(orqrun, "TEMP_ROOT", "/private/tmp"):
            self.assertTrue(orqrun._git_interno_seguro([
                *orqrun._GIT_INTERNO, "worktree", "add", "--no-checkout",
                "--detach", checkout, oid,
            ]))
            self.assertTrue(orqrun._git_interno_seguro([
                *orqrun._GIT_INTERNO, "worktree", "remove", "--force", checkout,
            ]))
            self.assertFalse(orqrun._git_interno_seguro([
                *orqrun._GIT_INTERNO, "worktree", "add", "--no-checkout",
                "--detach", "/tmp/orq-verify-prueba/checkout", oid,
            ]))

    def test_shell_rechaza_texto_libre_y_solo_despacha_argv_estatico(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqchat, "CARPETA", tmp
        ), mock.patch.object(
            orqchat.L, "_ejecutar_aislado_acotado", return_value=("", "", 0)
        ) as ejecutar, (
            contextlib.redirect_stdout(io.StringIO())
        ):
            self.assertEqual(orqchat.ejecutar_shell_seguro("pwd; touch /tmp/pwn"), 2)
            ejecutar.assert_not_called()
            self.assertEqual(orqchat.ejecutar_shell_seguro("pwd"), 0)

        argv = ejecutar.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/pwd")
        self.assertNotIn("pwd; touch /tmp/pwn", argv)
        self.assertFalse(ejecutar.call_args.kwargs["usar_scope"])

    def test_shell_git_no_ejecuta_fsmonitor_del_repositorio(self):
        with tempfile.TemporaryDirectory() as tmp:
            marca = os.path.join(tmp, "fsmonitor-ejecutado")
            hook = os.path.join(tmp, "fsmonitor")
            with open(hook, "w", encoding="utf-8") as archivo:
                archivo.write(f"#!/bin/sh\ntouch {marca}\n")
            os.chmod(hook, 0o700)
            subprocess.run(
                ["/usr/bin/git", "init", "-q", tmp], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run([
                "/usr/bin/git", "-C", tmp, "-c", "user.name=Pruebas",
                "-c", "user.email=tests@local", "commit", "--allow-empty",
                "-qm", "base",
            ], check=True)
            subprocess.run(
                ["/usr/bin/git", "-C", tmp, "config", "core.fsmonitor", hook],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with mock.patch.object(orqchat, "CARPETA", tmp), (
                contextlib.redirect_stdout(io.StringIO())
            ):
                self.assertEqual(orqchat.ejecutar_shell_seguro("git status"), 0)
            self.assertFalse(os.path.exists(marca))

    def test_shell_y_verificador_rechazan_filtro_git_sin_ejecutarlo(self):
        with tempfile.TemporaryDirectory() as tmp:
            marca = os.path.join(tmp, "filtro-ejecutado")
            filtro = os.path.join(tmp, "filtro")
            with open(filtro, "w", encoding="utf-8") as archivo:
                archivo.write(f"#!/bin/sh\ntouch {marca}\n/bin/cat\n")
            os.chmod(filtro, 0o700)
            subprocess.run(["/usr/bin/git", "init", "-q", tmp], check=True)
            with open(os.path.join(tmp, ".gitattributes"), "w",
                      encoding="utf-8") as archivo:
                archivo.write("*.txt filter=hostil\n")
            with open(os.path.join(tmp, "dato.txt"), "w", encoding="utf-8") as archivo:
                archivo.write("base\n")
            subprocess.run([
                "/usr/bin/git", "-C", tmp, "-c", "user.name=Pruebas",
                "-c", "user.email=tests@local", "add", ".gitattributes", "dato.txt",
            ], check=True)
            subprocess.run([
                "/usr/bin/git", "-C", tmp, "-c", "user.name=Pruebas",
                "-c", "user.email=tests@local", "commit", "-qm", "base",
            ], check=True)
            subprocess.run([
                "/usr/bin/git", "-C", tmp, "config", "filter.hostil.clean", filtro,
            ], check=True)
            with open(os.path.join(tmp, "dato.txt"), "w", encoding="utf-8") as archivo:
                archivo.write("cambio\n")

            with mock.patch.object(orqchat, "CARPETA", tmp), (
                contextlib.redirect_stdout(io.StringIO())
            ):
                self.assertEqual(orqchat.ejecutar_shell_seguro("git status"), 2)
            verificacion = orqlib.ejecutar_comando_verificacion(
                "git status", tmp, 10
            )
            self.assertEqual(verificacion["rc"], 126)
            self.assertFalse(os.path.exists(marca))

    def test_verificador_y_foto_repo_no_ejecutan_fsmonitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            marca = os.path.join(tmp, "fsmonitor-ejecutado")
            hook = os.path.join(tmp, "fsmonitor")
            with open(hook, "w", encoding="utf-8") as archivo:
                archivo.write(f"#!/bin/sh\ntouch {marca}\n")
            os.chmod(hook, 0o700)
            subprocess.run(["/usr/bin/git", "init", "-q", tmp], check=True)
            subprocess.run([
                "/usr/bin/git", "-C", tmp, "-c", "user.name=Pruebas",
                "-c", "user.email=tests@local", "commit", "--allow-empty",
                "-qm", "base",
            ], check=True)
            subprocess.run([
                "/usr/bin/git", "-C", tmp, "config", "core.fsmonitor", hook,
            ], check=True)

            real = orqlib.ejecutar_comando_verificacion("git status", tmp, 10)
            raiz, _ = orqchat._estado_repo(tmp)

            self.assertEqual(real["rc"], 0, real["resultado"])
            self.assertEqual(raiz, tmp)
            self.assertFalse(os.path.exists(marca))

    def test_verificador_conserva_cwd_fd_entre_validacion_y_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            proyecto = os.path.join(tmp, "proyecto")
            movido = os.path.join(tmp, "proyecto-adquirido")
            reemplazo = os.path.join(tmp, "reemplazo")
            marca = os.path.join(tmp, "se-ejecuto-reemplazo")
            os.makedirs(os.path.join(proyecto, "tests"))
            os.makedirs(os.path.join(reemplazo, "tests"))
            with open(os.path.join(proyecto, "tests", "test_probe.py"),
                      "w", encoding="utf-8") as archivo:
                archivo.write("import unittest\nclass T(unittest.TestCase):\n"
                              "    def test_ok(self): self.assertTrue(True)\n")
            with open(os.path.join(reemplazo, "tests", "test_probe.py"),
                      "w", encoding="utf-8") as archivo:
                archivo.write(
                    "import pathlib, unittest\n"
                    "class T(unittest.TestCase):\n"
                    f"    def test_peligro(self): pathlib.Path({marca!r}).write_text('x'); "
                    "self.fail('SE_EJECUTO_REEMPLAZO')\n"
                )
            validar_real = orqlib._operandos_verificacion_seguros
            intercambiado = False

            def intercalar(argv, carpeta, cwd_fd=None):
                nonlocal intercambiado
                permitido = validar_real(argv, carpeta, cwd_fd=cwd_fd)
                if permitido and not intercambiado:
                    intercambiado = True
                    os.rename(proyecto, movido)
                    os.rename(reemplazo, proyecto)
                return permitido

            with mock.patch.object(
                    orqlib, "_operandos_verificacion_seguros",
                    side_effect=intercalar):
                resultado = orqlib.ejecutar_comando_verificacion(
                    "python3 -m unittest discover -s tests", proyecto, 10
                )

            self.assertTrue(intercambiado)
            self.assertEqual(resultado["rc"], 0, resultado["resultado"])
            self.assertFalse(os.path.exists(marca))

    def test_parser_verificacion_rechaza_rutas_externas_y_symlink(self):
        peligrosos = (
            "python3 -m pytest ../../evil=x", "pytest ../../evil=x",
            "eslint /tmp/a.js", "eslint --config /tmp/pwn.js .",
            "cargo test --manifest-path /tmp/Cargo.toml", "go test /tmp/pwn",
            "systemd-analyze verify --root=/ /tmp/x.service",
        )
        for comando in peligrosos:
            with self.subTest(comando=comando):
                self.assertIsNone(orqlib._argv_verificacion(comando))
        with tempfile.TemporaryDirectory() as tmp:
            proyecto = os.path.join(tmp, "proyecto")
            os.mkdir(proyecto)
            fuera = os.path.join(tmp, "fuera.sh")
            with open(fuera, "w", encoding="utf-8") as archivo:
                archivo.write("#!/bin/sh\n")
            os.symlink(fuera, os.path.join(proyecto, "enlace.sh"))
            resultado = orqlib.ejecutar_comando_verificacion(
                "bash -n enlace.sh", proyecto, 10
            )
            self.assertEqual(resultado["rc"], 126)

    def test_salida_infinita_se_corta_al_exceder_limite(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"ORQ_DISABLE_SYSTEMD_SCOPE": "1"}, clear=False
        ):
            inicio = time.monotonic()
            out, err, rc = orqlib._ejecutar_aislado_acotado(
                [sys.executable, os.path.join(orqlib.CODIGO_ORQUESTA, "orqrun.py"),
                 "--fixture", "infinite"],
                {}, tmp, 10, limite_salida=128 * 1024, limite_error=1024,
            )
            duracion = time.monotonic() - inicio
        self.assertEqual(rc, 125)
        self.assertLessEqual(len(out.encode()), 128 * 1024)
        self.assertLess(duracion, 3)
        self.assertIn("excedio", err)

    def test_supervisor_elimina_daemon_aunque_el_lider_ya_salio(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"ORQ_DISABLE_SYSTEMD_SCOPE": "1"}, clear=False
        ):
            marca = os.path.join(tmp, "heartbeat")
            inicio = time.monotonic()
            out, err, rc = orqlib._ejecutar_aislado_acotado(
                [sys.executable, os.path.join(orqlib.CODIGO_ORQUESTA, "orqrun.py"),
                 "--fixture", "leader-exit"], {}, tmp, 3,
                limite_salida=4096, limite_error=4096, usar_scope=False,
            )
            duracion = time.monotonic() - inicio
            time.sleep(0.35)
            self.assertEqual(rc, 0, err)
            self.assertIn("lider terminado", out)
            self.assertFalse(os.path.exists(marca))
            self.assertLess(duracion, 2)

    def test_fallo_al_arrancar_lector_mata_y_recoge_el_proceso_creado(self):
        procesos = []
        popen_real = subprocess.Popen

        def capturar(*args, **kwargs):
            proceso = popen_real(*args, **kwargs)
            procesos.append(proceso)
            return proceso

        class LectorQueFalla:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                raise RuntimeError("fallo sintetico del drainer")

            def join(self, _timeout=None):
                return None

            def is_alive(self):
                return False

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            orqlib.subprocess, "Popen", side_effect=capturar
        ), mock.patch.object(orqlib.threading, "Thread", LectorQueFalla):
            out, err, rc = orqlib._ejecutar_aislado_acotado(
                [sys.executable, os.path.join(orqlib.CODIGO_ORQUESTA, "orqrun.py"),
                 "--fixture", "infinite"],
                {}, tmp, 10, usar_scope=False,
            )

        self.assertEqual(out, "")
        self.assertEqual(rc, 127)
        self.assertIn("fallo sintetico", err)
        self.assertEqual(len(procesos), 1)
        self.assertIsNotNone(procesos[0].poll())
        self.assertTrue(procesos[0].stdout.closed)
        self.assertTrue(procesos[0].stderr.closed)

    def test_terminal_no_se_resuelve_desde_path_heredado(self):
        with tempfile.TemporaryDirectory() as tmp:
            falso = os.path.join(tmp, "kitty")
            with open(falso, "w", encoding="utf-8") as archivo:
                archivo.write("#!/bin/sh\nexit 99\n")
            os.chmod(falso, 0o700)
            with mock.patch.dict(os.environ, {"PATH": tmp}, clear=False):
                _, plantilla = orqlib.terminal_disponible()
            if plantilla is not None:
                self.assertNotEqual(os.path.realpath(plantilla[0]), falso)

    def test_prompt_con_prefijo_de_opcion_queda_detras_del_terminador(self):
        with mock.patch.object(orqlib, "potencia_maxima", return_value=False), \
                mock.patch.object(orqlib, "permisos_activos", return_value=False):
            for proveedor in ("claude", "minimax", "gpt"):
                argv = orqlib.comando({"provider": proveedor}, "--help")
                self.assertEqual(argv[-2:], ["--", "--help"])

    def test_memoria_usa_nombre_opaco_y_no_reemplaza_symlink_precreado(self):
        hecho = "dato que debe permanecer dentro de memoria"
        huella = hashlib.sha256(hecho.encode("utf-8")).hexdigest()[:20]
        with tempfile.TemporaryDirectory() as tmp:
            memoria = os.path.join(tmp, "memoria")
            os.makedirs(memoria)
            fuera = os.path.join(tmp, "fuera.md")
            with open(fuera, "w", encoding="utf-8") as archivo:
                archivo.write("intacto\n")
            os.symlink(fuera, os.path.join(memoria, f"nota-{huella}.md"))
            with mock.patch.object(orqchat, "DIR_MEM", memoria):
                nombre = orqchat.recordar(hecho)

            self.assertEqual(nombre, f"nota-{huella}-1.md")
            self.assertEqual(
                stat.S_IMODE(os.stat(os.path.join(memoria, nombre)).st_mode), 0o600
            )
            with open(fuera, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), "intacto\n")

    def test_borrar_memoria_no_trunca_inode_sustituto_por_carrera(self):
        with tempfile.TemporaryDirectory() as tmp:
            memoria = os.path.join(tmp, "memoria")
            os.makedirs(memoria)
            nota = os.path.join(memoria, "nota.md")
            original = os.path.join(tmp, "nota-original.md")
            victima = os.path.join(tmp, "victima.md")
            with open(nota, "w", encoding="utf-8") as archivo:
                archivo.write("NOTA_ORIGINAL")
            with open(victima, "w", encoding="utf-8") as archivo:
                archivo.write("VICTIMA_INTACTA")
            os.chmod(nota, 0o600)
            os.chmod(victima, 0o600)
            open_real = os.open
            intercambiado = False

            def intercalar(nombre, flags, *args, **kwargs):
                nonlocal intercambiado
                if (not intercambiado and nombre == "nota.md"
                        and (flags & os.O_ACCMODE) == os.O_WRONLY):
                    intercambiado = True
                    os.rename(nota, original)
                    os.rename(victima, nota)
                return open_real(nombre, flags, *args, **kwargs)

            with mock.patch.object(orqchat, "DIR_MEM", memoria), \
                    mock.patch.object(orqlib.os, "open", side_effect=intercalar):
                resultado = orqchat.borrar_memoria("nota")

            self.assertTrue(intercambiado)
            self.assertFalse(resultado)
            with open(nota, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), "VICTIMA_INTACTA")
            with open(original, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), "NOTA_ORIGINAL")

    def test_login_grafico_no_construye_bash_ni_interpola_datos_en_argv(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.object(
            orqlib, "BASE", base
        ), mock.patch.object(
            orqlib, "terminal_disponible",
            return_value=("kitty", ["/usr/bin/kitty", "--title", "LOGIN · ORQUESTA", "-e"]),
        ), mock.patch.dict(os.environ, {
            "PYTHONPATH": "/tmp/ataque", "LD_PRELOAD": "/tmp/ataque.so",
            "NODE_OPTIONS": "--require=/tmp/ataque.js",
        }, clear=False), mock.patch.object(orqlib.subprocess, "Popen") as proceso:
            ok, _ = orqlib.lanzar_login(
                "claude-segura",
                {
                    "provider": "claude",
                    "home": "accounts/claude-segura",
                    "navegador": "firefox",
                },
                titulo="$(touch /tmp/pwn)",
            )

        self.assertTrue(ok)
        argv = proceso.call_args.args[0]
        self.assertNotIn("bash", argv)
        self.assertNotIn("-lc", argv)
        self.assertNotIn("$(touch /tmp/pwn)", argv)
        self.assertNotIn("claude-segura", argv)
        self.assertEqual(
            proceso.call_args.kwargs["env"]["ORQ_LOGIN_PROFILE"], "claude-segura"
        )
        self.assertEqual(argv[0], "/usr/bin/kitty")
        self.assertEqual(argv[-4:-1], ["/usr/bin/python3", "-I", "-S"])
        for nombre in ("PYTHONPATH", "LD_PRELOAD", "NODE_OPTIONS"):
            self.assertNotIn(nombre, proceso.call_args.kwargs["env"])

    def test_capacidad_propaga_oserror_del_consumidor_y_cierra_fd(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd_observado = None
            error = OSError("fallo exacto del consumidor")
            with self.assertRaisesRegex(OSError, "fallo exacto del consumidor"):
                with orqlib._capacidad_trabajo(tmp) as (_ruta, fd):
                    fd_observado = fd
                    raise error
            with self.assertRaises(OSError):
                os.fstat(fd_observado)

    def test_cwd_rechaza_symlink_colocado_despues_de_validar(self):
        with tempfile.TemporaryDirectory() as tmp:
            proyecto = os.path.join(tmp, "proyecto")
            fuera = os.path.join(tmp, "fuera")
            movido = os.path.join(tmp, "movido")
            os.mkdir(proyecto)
            os.mkdir(fuera)
            with open(os.path.join(proyecto, "identity"), "w", encoding="utf-8") as f:
                f.write("AUTORIZADO\n")
            with open(os.path.join(fuera, "identity"), "w", encoding="utf-8") as f:
                f.write("FUERA\n")
            validada = orqlib.ruta_trabajo_segura(proyecto)
            self.assertEqual(validada, proyecto)
            os.rename(proyecto, movido)
            os.symlink(fuera, proyecto)

            out, _err, rc = orqlib._ejecutar_aislado_acotado(
                ["/usr/bin/python3", os.path.join(orqlib.CODIGO_ORQUESTA,
                                                   "orqrun.py"),
                 "--fixture", "cwd-probe"],
                {}, validada, 3, usar_scope=False,
            )

            self.assertNotEqual(rc, 0)
            self.assertNotIn("FUERA", out)

    def test_cwd_fd_conserva_inode_si_el_nombre_cambia_antes_de_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            proyecto = os.path.join(tmp, "proyecto")
            fuera = os.path.join(tmp, "fuera")
            movido = os.path.join(tmp, "movido")
            os.mkdir(proyecto)
            os.mkdir(fuera)
            with open(os.path.join(proyecto, "identity"), "w", encoding="utf-8") as f:
                f.write("AUTORIZADO\n")
            with open(os.path.join(fuera, "identity"), "w", encoding="utf-8") as f:
                f.write("FUERA\n")
            popen_real = orqlib.subprocess.Popen
            cambiado = False

            def intercalar(*args, **kwargs):
                nonlocal cambiado
                if not cambiado:
                    cambiado = True
                    os.rename(proyecto, movido)
                    os.symlink(fuera, proyecto)
                return popen_real(*args, **kwargs)

            with mock.patch.object(orqlib.subprocess, "Popen", side_effect=intercalar):
                out, err, rc = orqlib._ejecutar_aislado_acotado(
                    ["/usr/bin/python3", os.path.join(orqlib.CODIGO_ORQUESTA,
                                                       "orqrun.py"),
                     "--fixture", "cwd-probe"],
                    {}, proyecto, 3, usar_scope=False,
                )

            self.assertEqual(rc, 0, err)
            self.assertEqual(out.strip(), "AUTORIZADO")

    def test_supervisor_rechaza_interfaz_python_y_git_generica(self):
        supervisor = os.path.join(orqlib.CODIGO_ORQUESTA, "orqrun.py")
        with tempfile.TemporaryDirectory() as tmp:
            marca = os.path.join(tmp, "ejecutado")
            python = subprocess.run(
                ["/usr/bin/python3", "-I", "-S", supervisor,
                 "--mode", "python", "-c",
                 f"open({marca!r}, 'w').write('x')"],
                cwd=tmp, capture_output=True, text=True, timeout=5,
            )
            git = subprocess.run(
                ["/usr/bin/python3", "-I", "-S", supervisor,
                 "--mode", "git", "-c", "alias.pwn=!true", "pwn"],
                cwd=tmp, capture_output=True, text=True, timeout=5,
            )
            self.assertNotEqual(python.returncode, 0)
            self.assertNotEqual(git.returncode, 0)
            self.assertFalse(os.path.exists(marca))

    def test_historial_y_contexto_no_truncan_destinos_de_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            victima = os.path.join(tmp, "victima.txt")
            with open(victima, "w", encoding="utf-8") as archivo:
                archivo.write("intacto")
            historial = os.path.join(tmp, "historial")
            os.symlink(victima, historial)
            if orqchat.readline:
                orqchat.readline.clear_history()
                orqchat.readline.add_history("entrada segura")
                with mock.patch.object(orqchat, "HIST", historial):
                    with self.assertRaises(OSError):
                        orqchat.guardar_historial()

            sesiones = os.path.join(tmp, "sesiones")
            os.makedirs(sesiones)
            contexto = os.path.join(sesiones, f"{orqchat.SESION}.ctx.txt")
            os.symlink(victima, contexto)
            with mock.patch.object(orqchat, "DIR_SES", sesiones), mock.patch.object(
                orqchat.subprocess, "run"
            ) as ejecutar, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orqchat.ejecutar_proyecto_chat("tarea", []), 1)
                ejecutar.assert_not_called()
            with open(victima, encoding="utf-8") as archivo:
                self.assertEqual(archivo.read(), "intacto")

    def test_env_de_perfil_no_puede_alterar_loader_path_ni_git_config(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.object(
            orqlib, "BASE", base
        ):
            perfil = {
                "provider": "claude",
                "home": "accounts/claude-segura",
                "env": {"LD_PRELOAD": "/tmp/ataque.so"},
            }
            with self.assertRaises(ValueError):
                orqlib.entorno("claude-segura", perfil)


if __name__ == "__main__":
    unittest.main()
