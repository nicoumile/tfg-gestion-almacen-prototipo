"""
Migración única (tanda 11, 10/2026): índices para las consultas que más se
repiten. No cambia ningún dato ni ninguna tabla: solo agrega índices, que
la base usa sola para encontrar más rápido las filas.

Hoy las tablas son chicas (unas 1.800 ventas) y la diferencia casi no se
nota. Con un año de ventas (~30.000 renglones) el Dashboard, Reportes, el
Historial de un artículo y el pronóstico dejarían de recorrer la tabla
entera cada vez.

Índices que agrega (si ya existen, no hace nada con ese):
  - ventas(fecha)                 reportes y pronóstico por período
  - ventas(articulo_id)           historial y ventas de un artículo
  - ventas(ticket_id)             detalle de un ticket
  - detalle_factura(factura_id)   renglones de una factura
  - detalle_factura(articulo_id)  compras de un artículo, costo vigente
  - historial_cambios(tabla, registro_id)  historial de un artículo/factura
  - fraccionamientos(articulo_bolsa_id) y (articulo_derivado_id)
  - gastos(fecha)

Se puede correr más de una vez. Para sacarlos:
    python superados/migraciones/migracion_tanda11_indices.py --revertir

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python analisis/respaldo_produccion.py
    python superados/migraciones/migracion_tanda11_indices.py
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
import sys

from sqlalchemy import text

import db

INDICES = [  # (nombre, tabla, columnas)
    ("idx_ventas_fecha", "ventas", "fecha"),
    ("idx_ventas_articulo", "ventas", "articulo_id"),
    ("idx_ventas_ticket", "ventas", "ticket_id"),
    ("idx_detalle_factura_factura", "detalle_factura", "factura_id"),
    ("idx_detalle_factura_articulo", "detalle_factura", "articulo_id"),
    ("idx_historial_registro", "historial_cambios", "tabla, registro_id"),
    ("idx_fracc_bolsa", "fraccionamientos", "articulo_bolsa_id"),
    ("idx_fracc_derivado", "fraccionamientos", "articulo_derivado_id"),
    ("idx_gastos_fecha", "gastos", "fecha"),
]


def existentes(con):
    filas = con.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))
    return {f[0] for f in filas}


def aplicar():
    with db.engine.begin() as con:
        ya = existentes(con)
        nuevos = [i for i in INDICES if i[0] not in ya]
        if not nuevos:
            print("Ya estaba aplicada: los índices existen. No se cambió nada.")
            return
        for nombre, tabla, cols in nuevos:
            con.execute(text(f"CREATE INDEX IF NOT EXISTS {nombre} ON {tabla} ({cols})"))
            print(f"  + {nombre} en {tabla}({cols})")
        faltan = [i[0] for i in INDICES if i[0] not in existentes(con)]
        if faltan:
            raise RuntimeError(f"No se crearon: {faltan}. No se cambió nada.")
        con.execute(text("ANALYZE"))
    print(f"Listo: {len(nuevos)} índices nuevos. Los datos no cambiaron.")


def revertir():
    with db.engine.begin() as con:
        for nombre, _, _ in INDICES:
            con.execute(text(f"DROP INDEX IF EXISTS {nombre}"))
    print("Índices de la tanda 11 quitados.")


if __name__ == "__main__":
    revertir() if "--revertir" in sys.argv else aplicar()
