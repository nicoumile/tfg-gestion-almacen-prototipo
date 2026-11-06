"""
Migración única (Sprint 2, HU-015, 10/2026): datos de reposición por artículo
para la Lista de compras.

Esta migración (solo AGREGA; no borra ni reescribe datos existentes):

  1. articulos.reposicion_dias: cada cuántos días se compra ESE artículo.
     NULL = se compra en el viaje al mayorista (parámetro
     frecuencia_viaje_dias, hoy 7). 1 = compra diaria (ej.: el pan, que el comerciante
     compra todos los días en la panadería).
  2. articulos.vida_util_dias: días que el producto se puede vender antes de
     tirarlo/regalarlo (ej.: pan = 2, política del comerciante). NULL = no perecedero.
  3. articulos.nivel_servicio: nivel de servicio propio del artículo
     (probabilidad de no quedarse sin stock). NULL = el de su clase ABC o,
     para compra diaria, el óptimo económico (newsvendor).
  4. Parámetro valor_recupero_pct: qué fracción del costo se recupera de lo
     que sobra de un perecedero (0 = se regala o se tira, caso actual).

Se puede correr más de una vez sin romper nada. Correrla ANTES de subir la
versión nueva de app.py/db.py. La app anterior sigue funcionando con la
base migrada.

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python analisis/respaldo_produccion.py
    python superados/migraciones/migracion_lista_compras.py
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
import sys

import db
from sqlalchemy import text

SENTENCIAS = [
    "ALTER TABLE articulos ADD COLUMN IF NOT EXISTS reposicion_dias INTEGER",
    "ALTER TABLE articulos ADD COLUMN IF NOT EXISTS vida_util_dias INTEGER",
    "ALTER TABLE articulos ADD COLUMN IF NOT EXISTS nivel_servicio NUMERIC(4,3)",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'articulos_reposicion_dias_check') THEN
            ALTER TABLE articulos ADD CONSTRAINT articulos_reposicion_dias_check
                CHECK (reposicion_dias IS NULL OR reposicion_dias BETWEEN 1 AND 60);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'articulos_vida_util_check') THEN
            ALTER TABLE articulos ADD CONSTRAINT articulos_vida_util_check
                CHECK (vida_util_dias IS NULL OR vida_util_dias >= 1);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'articulos_nivel_servicio_check') THEN
            ALTER TABLE articulos ADD CONSTRAINT articulos_nivel_servicio_check
                CHECK (nivel_servicio IS NULL OR (nivel_servicio >= 0.5 AND nivel_servicio < 1));
        END IF;
    END $$;
    """,
    """
    INSERT INTO parametros (clave, valor, descripcion) VALUES
        ('valor_recupero_pct', 0, 'Fracción del costo que se recupera de lo que sobra de un perecedero (0 = se regala o se tira)')
    ON CONFLICT (clave) DO NOTHING
    """,
]


def main():
    url = db.engine.url
    print(f"Conectando a: host={url.host!r} db={url.database!r} user={url.username!r}")
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            if not conn.execute(text("SELECT to_regclass('public.parametros')")).scalar():
                print("*** Falta correr antes migracion_sprint2.py (no existe la tabla parametros). ***")
                sys.exit(1)
    except Exception as e:
        print(f"\n*** No se pudo conectar a la base: {type(e).__name__}: {e} ***")
        sys.exit(1)

    with db.engine.begin() as conn:
        for q in SENTENCIAS:
            conn.execute(text(q))

    with db.engine.connect() as conn:
        cols = set(conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'articulos'
        """)).scalars())
        recupero = conn.execute(text("SELECT valor FROM parametros WHERE clave = 'valor_recupero_pct'")).scalar()
    faltan = {"reposicion_dias", "vida_util_dias", "nivel_servicio"} - cols
    if faltan or recupero is None:
        print(f"*** Verificación fallida: faltan {faltan or 'el parámetro valor_recupero_pct'} ***")
        sys.exit(1)
    print("Verificación OK: columnas reposicion_dias, vida_util_dias, nivel_servicio y parámetro valor_recupero_pct.")
    print("Migración completa. Ahora sí se puede subir la versión nueva de la app.")


if __name__ == "__main__":
    main()
