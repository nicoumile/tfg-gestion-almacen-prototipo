"""
Migración única (14/10/2026): baja lógica de artículos.

Permite marcar como INACTIVO un artículo que el comerciante no trabaja más (o que
fue reemplazado por otro) desde Artículos -> Editar artículo, sin
borrarlo: sus ventas y compras siguen alimentando el ABC, los márgenes y
el historial.

Esta migración:

  1. Agrega a 'articulos' las columnas activo (TRUE por defecto: todos
     los artículos existentes quedan activos), fecha_baja, motivo_baja y
     reemplazado_por.
  2. Actualiza la vista v_articulos para exponer esas columnas (al final,
     porque Postgres no deja intercalar columnas con CREATE OR REPLACE).
     La definición se toma de schema.sql, así no hay dos copias.
  3. Agrega snapshot_calidad.articulos_inactivos (foto semanal de cuántos
     artículos quedaron fuera del indicador de calidad).

Se puede correr más de una vez sin romper nada. Correrla ANTES de subir
la versión nueva de app.py/db.py (la app nueva lee estas columnas).

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python migracion_articulos_inactivos.py
"""
import os
import re
import sys

import db
from sqlalchemy import text

COLUMNAS = [
    "ALTER TABLE articulos ADD COLUMN IF NOT EXISTS activo BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE articulos ADD COLUMN IF NOT EXISTS fecha_baja DATE",
    "ALTER TABLE articulos ADD COLUMN IF NOT EXISTS motivo_baja TEXT",
    "ALTER TABLE articulos ADD COLUMN IF NOT EXISTS reemplazado_por INTEGER REFERENCES articulos(id)",
]

CHECK_MOTIVO = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'articulos_motivo_baja_check') THEN
        ALTER TABLE articulos ADD CONSTRAINT articulos_motivo_baja_check
            CHECK (motivo_baja IN ('discontinuado', 'reemplazado', 'temporal'));
    END IF;
END $$;
"""


def vista_desde_schema():
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(ruta, encoding="utf-8") as f:
        sql = f.read()
    m = re.search(r"CREATE OR REPLACE VIEW v_articulos AS.*?\) sub;", sql, re.S)
    if not m or "sub.reemplazado_por" not in m.group(0):
        print("*** No encontré la vista v_articulos actualizada en schema.sql. "
              "¿Copiaste el schema.sql nuevo a la misma carpeta? ***")
        sys.exit(1)
    return m.group(0)


def probar_conexion():
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("Conexion OK.\n")
    except Exception as e:
        print("\n*** No se pudo conectar a la base. Detalle completo: ***")
        print(f"{type(e).__name__}: {e}")
        sys.exit(1)


def main():
    vista = vista_desde_schema()
    probar_conexion()

    print("PASO 1: columnas de baja lógica en articulos")
    with db.engine.begin() as conn:
        for q in COLUMNAS:
            conn.execute(text(q))
        conn.execute(text(CHECK_MOTIVO))
        total = conn.execute(text("SELECT COUNT(*) FROM articulos WHERE activo")).scalar()
    print(f"Listo. {total} artículos activos (todos los existentes arrancan activos).\n")

    print("PASO 2: vista v_articulos")
    with db.engine.begin() as conn:
        conn.execute(text(vista))
    print("Vista actualizada.\n")

    print("PASO 3: snapshot_calidad.articulos_inactivos")
    with db.engine.begin() as conn:
        db._asegurar_tabla_snapshot(conn)  # crea la tabla si no existe y agrega la columna
    print("Listo.\n")

    print("Migración completa.")


if __name__ == "__main__":
    main()
