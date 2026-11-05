"""
Migración única (tanda 14, 10/2026): proveedores en consignación.

QUÉ ES
  FUSION FUNGA deja mercadería que se le paga recién cuando se vende: pasa
  cada tanto, cuenta y cobra lo vendido. La mercadería entra al stock como
  cualquier compra, pero la plata no es del comerciante hasta que se vende.

QUÉ HACE (todo en UNA transacción; si algo falla no cambia nada)
  1. proveedores.consignacion (sí/no, por defecto no).
  2. Tabla liquidaciones_consignacion: cada vez que el proveedor cobra, qué
     ventas cubrió (hasta qué venta) y cuánto se le pagó.
  3. Crea el proveedor FUSION FUNGA en consignación (si no existe) y le
     asigna los artículos de marca FUSION FUNGA, con su costo actual. No
     cambia stock, costos, precios ni ventas.
  4. Lo deja AL DÍA al 22/10/2026 10:41 (Nico confirmó que no se le debía
     nada): un corte inicial de $0 que cubre las ventas hechas hasta ese
     momento. Lo vendido después es lo que se le debe.

Se puede correr más de una vez. Para deshacerlo:
    python migracion_tanda14.py --revertir
(quita la tabla de liquidaciones y la columna; el proveedor y sus artículos
quedan como proveedor común).

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python respaldo_produccion.py
    python migracion_tanda14.py
"""
import sys

from sqlalchemy import text

import db

PROVEEDOR = "FUSION FUNGA"
# "al día" según Nico: 22/10/2026 10:41 hora argentina = 13:41 UTC (ventas.creado_en se guarda en UTC)
AL_DIA_UTC = "2026-10-22 13:41:00"

TABLA = """
CREATE TABLE IF NOT EXISTS liquidaciones_consignacion (
    id SERIAL PRIMARY KEY,
    proveedor_id INTEGER NOT NULL REFERENCES proveedores(id),
    fecha DATE NOT NULL DEFAULT CURRENT_DATE,        -- día en que se le pagó
    desde_venta_id INTEGER,                          -- primera venta que cubre (informativo)
    hasta_venta_id INTEGER NOT NULL,                 -- cubre las ventas con id <= este
    monto_calculado NUMERIC(14,2) NOT NULL,          -- lo que calculó la app
    monto_pagado NUMERIC(14,2) NOT NULL,             -- lo que se le pagó de verdad
    nota TEXT,
    creado_en TIMESTAMP NOT NULL DEFAULT now()
)
"""


def aplicar():
    with db.engine.begin() as con:
        con.execute(text("ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS consignacion BOOLEAN NOT NULL DEFAULT FALSE"))
        con.execute(text(TABLA))
        prov = con.execute(text("SELECT id, consignacion FROM proveedores WHERE upper(trim(nombre)) = :n "
                                "ORDER BY id LIMIT 1"), {"n": PROVEEDOR}).first()
        cambio = prov is None or not prov[1]
        if prov is None:
            prov_id = con.execute(text("INSERT INTO proveedores (nombre, consignacion) VALUES (:n, TRUE) RETURNING id"),
                                  {"n": PROVEEDOR}).scalar()
            print(f"  + proveedor {PROVEEDOR} (#{prov_id}) en consignación")
        else:
            prov_id = prov[0]
            if not prov[1]:
                con.execute(text("UPDATE proveedores SET consignacion = TRUE WHERE id = :i"), {"i": prov_id})
            print(f"  = proveedor {PROVEEDOR} (#{prov_id}) marcado en consignación")
        nuevos = con.execute(text("""
            INSERT INTO articulo_proveedor_precio (articulo_id, proveedor_id, costo_compra)
            SELECT a.id, :p, a.costo_compra_actual FROM articulos a
            WHERE upper(trim(a.marca)) = :n
            ON CONFLICT (articulo_id, proveedor_id) DO NOTHING
            RETURNING articulo_id
        """), {"p": prov_id, "n": PROVEEDOR}).all()
        nombres = con.execute(text("SELECT nombre FROM articulos WHERE upper(trim(marca)) = :n ORDER BY nombre"),
                              {"n": PROVEEDOR}).scalars().all()
        print(f"  {len(nombres)} artículos de {PROVEEDOR} ({len(nuevos)} asignados ahora): " + ", ".join(nombres))
        tiene_liq = con.execute(text("SELECT count(*) FROM liquidaciones_consignacion WHERE proveedor_id = :p"),
                                {"p": prov_id}).scalar()
        if not tiene_liq:
            hasta = con.execute(text("SELECT COALESCE(MAX(id), 0) FROM ventas WHERE creado_en <= :t"),
                                {"t": AL_DIA_UTC}).scalar()
            con.execute(text("""
                INSERT INTO liquidaciones_consignacion
                    (proveedor_id, fecha, desde_venta_id, hasta_venta_id, monto_calculado, monto_pagado, nota)
                VALUES (:p, DATE '2026-10-22', NULL, :h, 0, 0, 'Corte inicial: al día al 22/10/2026 10:41 (sin deuda)')
            """), {"p": prov_id, "h": hasta})
            print(f"  + corte inicial: al día al 22/10/2026 10:41 (cubre hasta la venta #{hasta})")
            cambio = True
        if not cambio and not nuevos:
            print("Ya estaba aplicada. No se cambió nada.")
            return
        db.registrar_historial("proveedores", prov_id, "EDICION",
                               f"{PROVEEDOR}: proveedor en consignación (se paga lo vendido); "
                               f"{len(nombres)} artículos asignados", conn=con)
    print("Listo. Ningún stock, costo, precio ni venta cambió.")


def revertir():
    with db.engine.begin() as con:
        con.execute(text("DROP TABLE IF EXISTS liquidaciones_consignacion"))
        con.execute(text("ALTER TABLE proveedores DROP COLUMN IF EXISTS consignacion"))
    print("Consignación quitada (tabla de liquidaciones y columna). El proveedor queda como común.")


if __name__ == "__main__":
    revertir() if "--revertir" in sys.argv else aplicar()
