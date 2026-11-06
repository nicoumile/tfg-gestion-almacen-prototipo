"""
Migración única (10/2026): baja de la "venta genérica" (venta sin producto).

La venta genérica permitía cargar solo monto + medio de pago, sin artículo
(articulo_id, cantidad y precio_unitario en NULL). Quedó obsoleta: la app
ya no la ofrece y ese tipo de venta queda afuera de todo el núcleo
analítico (ABC, márgenes, reposición, calidad de datos), porque no se sabe
qué se vendió.

Esta migración hace que la base EXIJA artículo, cantidad y precio en cada
venta, para que no pueda volver a entrar una venta sin producto:

  1. Verifica cuántas ventas sin producto quedan. Si hay alguna, las lista
     y se DETIENE sin tocar nada: primero hay que resolverlas desde la app
     (Registrar Venta -> Editar o eliminar una venta: elegir el artículo y
     guardar, o eliminarla).
  2. Si no queda ninguna: pone articulo_id, cantidad y precio_unitario
     como NOT NULL y elimina la restricción chk_venta_consistente (que
     existía solo para admitir el caso "los tres en NULL").

Los triggers de stock no se tocan: sus chequeos "IF articulo_id IS NOT
NULL" quedan siempre verdaderos y no afectan en nada.

Se puede correr más de una vez sin romper nada.

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python superados/migraciones/migracion_quitar_venta_generica.py
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


def paso_1_verificar_ventas_sin_producto():
    print("=" * 60)
    print("PASO 1: ventas sin producto que quedan en la base")
    print("=" * 60)
    with db.engine.connect() as conn:
        filas = conn.execute(text("""
            SELECT id, fecha, monto_total, medio_pago
            FROM ventas WHERE articulo_id IS NULL
            ORDER BY fecha, id
        """)).fetchall()

    if not filas:
        print("No queda ninguna. Se puede continuar.\n")
        return

    total = sum(float(f.monto_total) for f in filas)
    print(f"Quedan {len(filas)} venta(s) sin producto, por ${total:,.2f} en total:\n")
    for f in filas:
        print(f"  #{f.id}  {f.fecha}  ${float(f.monto_total):,.2f}  {f.medio_pago}")
    print(
        "\nNo se modificó nada. Resolvelas desde la app (Registrar Venta -> "
        "Editar o eliminar una venta): elegí el artículo y guardá, o eliminala. "
        "Después volvé a correr este script."
    )
    sys.exit(0)


def paso_2_exigir_producto():
    print("=" * 60)
    print("PASO 2: exigir artículo, cantidad y precio en cada venta")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE ventas DROP CONSTRAINT IF EXISTS chk_venta_consistente"))
        conn.execute(text("ALTER TABLE ventas ALTER COLUMN articulo_id SET NOT NULL"))
        conn.execute(text("ALTER TABLE ventas ALTER COLUMN cantidad SET NOT NULL"))
        conn.execute(text("ALTER TABLE ventas ALTER COLUMN precio_unitario SET NOT NULL"))
    print("Listo: ya no se pueden cargar ventas sin producto.\n")


def main():
    probar_conexion()
    paso_1_verificar_ventas_sin_producto()
    paso_2_exigir_producto()
    print("=" * 60)
    print("Migración completa.")
    print("=" * 60)


if __name__ == "__main__":
    main()
