"""
Migración única (10/2026): importador de facturas desde la planilla
"Registro de facturas" (lector automático con Gemini, ver
claude/lector-facturas-detalle-articulos.md en el proyecto).

Esta migración:

  1. Agrega articulo_alias.factor (NUMERIC(12,4) NOT NULL DEFAULT 1):
     cuántas unidades del artículo del catálogo representa UNA unidad
     tal como la factura el proveedor. Ej: "BOCADITO MARROC 60 x 14 grs"
     facturado por caja -> factor 60 (la caja se vende suelta, de a una).
     Los alias ya cargados quedan con factor 1 (se comportan igual que
     antes).

  2. Agrega facturas.ref_externa (TEXT) con un índice único parcial:
     identifica de dónde vino una factura importada (ej. "planilla:C-00012")
     para que la misma factura no se pueda importar dos veces, aunque se
     toque el botón dos veces o se pierda la marca en la planilla.

Se puede correr más de una vez sin romper nada.

Se corre localmente, igual que las migraciones anteriores:

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python superados/migraciones/migracion_importador_facturas.py
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
import sys

import db
from sqlalchemy import text


def probar_conexion():
    url = db.engine.url
    print(
        f"Conectando a: host={url.host!r} port={url.port!r} "
        f"db={url.database!r} user={url.username!r}"
    )
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("Conexion OK.\n")
    except Exception as e:
        print("\n*** No se pudo conectar a la base. Detalle completo: ***")
        print(f"{type(e).__name__}: {e}")
        sys.exit(1)


def paso_1_factor_en_alias():
    print("=" * 60)
    print("PASO 1: columna articulo_alias.factor")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text("""
            ALTER TABLE articulo_alias
            ADD COLUMN IF NOT EXISTS factor NUMERIC(12,4) NOT NULL DEFAULT 1
        """))
        existe_check = conn.execute(text("""
            SELECT 1 FROM pg_constraint WHERE conname = 'articulo_alias_factor_positivo'
        """)).first()
        if not existe_check:
            conn.execute(text("""
                ALTER TABLE articulo_alias
                ADD CONSTRAINT articulo_alias_factor_positivo CHECK (factor > 0)
            """))
    print("OK -- los alias existentes quedan con factor 1.\n")


def paso_2_ref_externa_en_facturas():
    print("=" * 60)
    print("PASO 2: columna facturas.ref_externa + índice único")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS ref_externa TEXT"))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_ref_externa
            ON facturas (ref_externa) WHERE ref_externa IS NOT NULL
        """))
    print("OK.\n")


def verificar():
    with db.engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT table_name, column_name FROM information_schema.columns
            WHERE (table_name = 'articulo_alias' AND column_name = 'factor')
               OR (table_name = 'facturas' AND column_name = 'ref_externa')
        """)).fetchall()
    encontrados = {(t, c) for t, c in cols}
    esperados = {("articulo_alias", "factor"), ("facturas", "ref_externa")}
    if encontrados == esperados:
        print("Verificación OK: las dos columnas existen.")
    else:
        print(f"*** Faltan columnas: {esperados - encontrados} ***")
        sys.exit(1)


if __name__ == "__main__":
    probar_conexion()
    paso_1_factor_en_alias()
    paso_2_ref_externa_en_facturas()
    verificar()
