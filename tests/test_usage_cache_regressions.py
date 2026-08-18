import json
import os
import tempfile
import time
import unittest
from unittest import mock

import orqlib


def evento_claude(timestamp, entrada, salida, cache=0):
    return json.dumps(
        {
            "timestamp": timestamp,
            "message": {
                "usage": {
                    "input_tokens": entrada,
                    "output_tokens": salida,
                    "cache_read_input_tokens": cache,
                }
            },
        }
    )


def totales(resultado):
    return {
        campo: sum(periodo.get(campo, 0) for periodo in resultado["dias"].values())
        for campo in ("entrada", "salida", "cache", "msgs")
    }


class CacheUsoRealTests(unittest.TestCase):
    def test_releer_refrescar_y_ampliar_un_jsonl_no_duplica_consumo(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "cuenta")
            sesiones = os.path.join(home, "projects", "proyecto")
            os.makedirs(sesiones)
            sesion = os.path.join(sesiones, "sesion.jsonl")
            with open(sesion, "w", encoding="utf-8") as archivo:
                archivo.write(evento_claude("2026-08-18T12:00:00Z", 10, 2, 4) + "\n")

            perfil = {"provider": "claude", "home": home}
            cache = os.path.join(tmp, "uso_real.json")
            lock = os.path.join(tmp, ".lock")
            with mock.patch.object(orqlib, "CACHE_REAL", cache), mock.patch.object(
                orqlib, "LOCK", lock
            ):
                primero = orqlib.uso_real("claude-prueba", perfil)
                sin_cambios = orqlib.uso_real("claude-prueba", perfil)
                refrescado = orqlib.uso_real("claude-prueba", perfil, refrescar=True)

                self.assertEqual(totales(primero), totales(sin_cambios))
                self.assertEqual(totales(primero), totales(refrescado))

                mtime = os.path.getmtime(sesion)
                with open(sesion, "a", encoding="utf-8") as archivo:
                    archivo.write(
                        evento_claude("2026-08-18T12:30:00Z", 5, 3, 1) + "\n"
                    )
                os.utime(sesion, (mtime + 2, mtime + 2))

                ampliado = orqlib.uso_real("claude-prueba", perfil)
                ampliado_otra_vez = orqlib.uso_real("claude-prueba", perfil)

        self.assertEqual(
            totales(ampliado),
            {"entrada": 15, "salida": 5, "cache": 5, "msgs": 2},
        )
        self.assertEqual(totales(ampliado), totales(ampliado_otra_vez))

    @unittest.skipUnless(hasattr(time, "tzset"), "tzset no esta disponible")
    def test_timestamp_z_se_agrupa_en_la_hora_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            sesion = os.path.join(tmp, "sesion.jsonl")
            with open(sesion, "w", encoding="utf-8") as archivo:
                archivo.write(evento_claude("2026-08-18T02:15:00Z", 7, 1) + "\n")

            anterior = os.environ.get("TZ")
            try:
                os.environ["TZ"] = "America/Bogota"
                time.tzset()
                dias, horas = orqlib._uso_archivo_claude(sesion)
            finally:
                if anterior is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = anterior
                time.tzset()

        self.assertIn("2026-08-17", dias)
        self.assertIn("2026-08-17T21", horas)
        self.assertNotIn("2026-08-18T02", horas)


if __name__ == "__main__":
    unittest.main()
