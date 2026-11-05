"""
Migración única (tanda 17, 10/2026): instrumentos del protocolo de validación.

QUÉ HACE (todo en UNA transacción; si algo falla no cambia nada)
  1. Tabla listas_compra + listas_compra_detalle: la lista con la que el comerciante se
     va al mayorista (botón "Me voy al mayorista con esta lista" en
     🛒 Lista de compras). Guarda lo que sugirió la app y lo que él dejó
     (cantidades corregidas, renglones destildados). Una lista por día: si se
     vuelve a guardar el mismo día, la reemplaza.
  2. Tabla viajes_compra: el registro de viajes (fecha, planificado o de
     urgencia, horas, con qué lista fue).
  No cambia stock, costos, precios, ventas ni facturas.

Se puede correr más de una vez. Para deshacerlo (borra lo registrado):
    python migracion_tanda17.py --revertir

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python respaldo_produccion.py
    python migracion_tanda17.py
"""
import sys

from sqlalchemy import text

import db

SQL = """
CREATE TABLE IF NOT EXISTS listas_compra (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL UNIQUE,                       -- día en que se armó (una por día)
    persona_id INTEGER REFERENCES personas(id),
    renglones INTEGER NOT NULL DEFAULT 0,             -- renglones que se llevó
    costo_estimado NUMERIC(14,2) NOT NULL DEFAULT 0,  -- con IVA, de lo que se llevó
    nota TEXT,
    creado_en TIMESTAMP NOT NULL DEFAULT now(),
    actualizado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS listas_compra_detalle (
    lista_id INTEGER NOT NULL REFERENCES listas_compra(id) ON DELETE CASCADE,
    articulo_id INTEGER NOT NULL REFERENCES articulos(id),
    clase TEXT,
    proveedor TEXT,
    stock NUMERIC(12,3),
    venta_esperada NUMERIC(12,3),
    sugerido NUMERIC(12,3) NOT NULL,                  -- lo que calculó la app
    cantidad NUMERIC(12,3) NOT NULL,                  -- lo que quedó en la lista (0 si lo sacó)
    incluido BOOLEAN NOT NULL,
    costo_c_iva NUMERIC(14,2),
    PRIMARY KEY (lista_id, articulo_id)
);
CREATE TABLE IF NOT EXISTS viajes_compra (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL,
    tipo TEXT NOT NULL CHECK (tipo IN ('planificado', 'urgencia')),
    horas NUMERIC(4,1) CHECK (horas IS NULL OR (horas >= 0 AND horas <= 24)),
    lista_id INTEGER REFERENCES listas_compra(id) ON DELETE SET NULL,   -- NULL = no usó lista
    nota TEXT,
    persona_id INTEGER REFERENCES personas(id),
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_viajes_compra_fecha ON viajes_compra (fecha);
"""


def aplicar():
    with db.engine.begin() as con:
        existia = con.execute(text("SELECT to_regclass('public.viajes_compra') IS NOT NULL")).scalar()
        if existia:
            print("Ya estaba aplicada: la tabla viajes_compra existe. No se cambió nada.")
            return
        for sentencia in [s for s in SQL.split(";") if s.strip()]:
            con.execute(text(sentencia))
    print("Listo: tablas listas_compra, listas_compra_detalle y viajes_compra creadas. "
          "Ningún stock, costo, precio ni venta cambió.")


def revertir():
    with db.engine.begin() as con:
        con.execute(text("DROP TABLE IF EXISTS viajes_compra"))
        con.execute(text("DROP TABLE IF EXISTS listas_compra_detalle"))
        con.execute(text("DROP TABLE IF EXISTS listas_compra"))
    print("Quitadas las tablas de listas guardadas y viajes.")


if __name__ == "__main__":
    revertir() if "--revertir" in sys.argv else aplicar()
