"""
Migración única (tanda 12, 10/2026): tabla para el bloqueo por intentos
fallidos de ingreso.

Hasta ahora, después de 5 contraseñas incorrectas la app bloqueaba un
minuto, pero el contador vivía en la sesión del navegador: recargar la
página lo ponía en cero. Con esta tabla el contador queda en la base, por
usuario, y recargar no lo saltea. Cada bloqueo seguido dura el doble (1, 2,
4 minutos... hasta 15); un ingreso correcto lo borra.

QUÉ HACE: crea la tabla intentos_ingreso (vacía). No toca ninguna otra tabla
ni ningún dato. La app de la tanda 12 funciona también SIN la tabla (vuelve
al bloqueo por sesión de antes), así que el orden migración/subida no
importa, pero conviene correrla antes de subir.

Se puede correr más de una vez. Para sacarla:
    python superados/migraciones/migracion_tanda12.py --revertir

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python analisis/respaldo_produccion.py
    python superados/migraciones/migracion_tanda12.py
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
import sys

from sqlalchemy import text

import db

TABLA = """
CREATE TABLE IF NOT EXISTS intentos_ingreso (
    usuario TEXT PRIMARY KEY,               -- tal como se tipeó (en minúsculas), exista o no
    fallidos INTEGER NOT NULL DEFAULT 0,    -- contraseñas incorrectas seguidas
    bloqueos INTEGER NOT NULL DEFAULT 0,    -- bloqueos seguidos (cada uno dura el doble)
    ultimo_fallo TIMESTAMPTZ,
    bloqueado_hasta TIMESTAMPTZ
)
"""


def existe(con):
    return con.execute(text("SELECT to_regclass('public.intentos_ingreso') IS NOT NULL")).scalar()


def aplicar():
    with db.engine.begin() as con:
        if existe(con):
            print("Ya estaba aplicada: la tabla intentos_ingreso existe. No se cambió nada.")
            return
        con.execute(text(TABLA))
        if not existe(con):
            raise RuntimeError("No se creó la tabla. No se cambió nada.")
    print("Listo: tabla intentos_ingreso creada (vacía). Ningún dato cambió.")


def revertir():
    with db.engine.begin() as con:
        con.execute(text("DROP TABLE IF EXISTS intentos_ingreso"))
    print("Tabla intentos_ingreso quitada. La app vuelve al bloqueo por sesión.")


if __name__ == "__main__":
    revertir() if "--revertir" in sys.argv else aplicar()
