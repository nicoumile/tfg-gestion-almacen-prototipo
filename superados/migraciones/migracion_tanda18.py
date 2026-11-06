"""
Migración única (tanda 18, 10/2026): vencimientos por lote implícito.

QUÉ HACE (todo en UNA transacción; si algo falla no cambia nada)
  1. Tabla vida_util_categoria: días disponibles para la venta por categoría
     (ej.: Fiambres y lácteos 10). Se usa para los artículos que no tienen
     su propia vida útil cargada. Arranca vacía: no cambia nada hasta que se
     carguen valores en ⚙️ Configuración.
  2. Columna detalle_factura.fecha_vencimiento (opcional): la fecha real de
     un renglón de factura, para cuando la mercadería vino con menos vida que
     la habitual. Si está vacía, el vencimiento se estima como fecha de la
     factura + días disponibles.
  No cambia stock, costos, precios, ventas ni facturas.

Se puede correr más de una vez. Para deshacerlo (borra lo cargado en esas dos cosas):
    python superados/migraciones/migracion_tanda18.py --revertir

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python analisis/respaldo_produccion.py
    python superados/migraciones/migracion_tanda18.py
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
import sys

from sqlalchemy import text

import db

SQL = """
CREATE TABLE IF NOT EXISTS vida_util_categoria (
    categoria TEXT PRIMARY KEY,
    dias INTEGER NOT NULL CHECK (dias BETWEEN 1 AND 3650),
    actualizado_en TIMESTAMP NOT NULL DEFAULT now()
);
ALTER TABLE detalle_factura ADD COLUMN IF NOT EXISTS fecha_vencimiento DATE
"""


def aplicar():
    with db.engine.begin() as con:
        existia = con.execute(text("""
            SELECT to_regclass('public.vida_util_categoria') IS NOT NULL
               AND EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'detalle_factura' AND column_name = 'fecha_vencimiento')
        """)).scalar()
        if existia:
            print("Ya estaba aplicada: la tabla vida_util_categoria y la columna existen. No se cambió nada.")
            return
        for sentencia in [s for s in SQL.split(";") if s.strip()]:
            con.execute(text(sentencia))
    print("Listo: tabla vida_util_categoria y columna detalle_factura.fecha_vencimiento creadas. "
          "Ningún stock, costo, precio ni venta cambió.")


def revertir():
    with db.engine.begin() as con:
        con.execute(text("ALTER TABLE detalle_factura DROP COLUMN IF EXISTS fecha_vencimiento"))
        con.execute(text("DROP TABLE IF EXISTS vida_util_categoria"))
    print("Quitadas la tabla vida_util_categoria y la columna detalle_factura.fecha_vencimiento.")


if __name__ == "__main__":
    revertir() if "--revertir" in sys.argv else aplicar()
