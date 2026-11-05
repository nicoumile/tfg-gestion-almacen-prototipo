"""
Diagnóstico de SOLO LECTURA de un artículo: no modifica nada en la base.

Muestra, para entender un resultado raro en Reportes -> Márgenes reales:
  - los datos actuales del artículo (costo, margen, precio de lista)
  - cada venta: fecha, cantidad, precio cobrado y costo guardado
  - las facturas de compra del artículo (costo pagado en cada una)
  - los cambios registrados en el historial (precio, margen, costo)

Uso (desde la carpeta TFG):
    python diagnostico_articulo.py 226
"""
import sys

import pandas as pd
from sqlalchemy import text

import db

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)
pd.set_option("display.max_colwidth", 120)


def main():
    if len(sys.argv) < 2:
        print("Uso: python diagnostico_articulo.py <id_del_articulo>")
        sys.exit(1)
    aid = int(sys.argv[1])

    with db.engine.connect() as conn:
        art = pd.read_sql(text("""
            SELECT id, nombre, categoria, costo_compra_actual,
                   ROUND(porcentaje_ganancia, 2) AS margen_pct, iva,
                   costo_venta_con_iva AS precio_lista, stock_actual
            FROM v_articulos WHERE id = :id
        """), conn, params={"id": aid})
        if art.empty:
            print(f"No existe el artículo {aid}.")
            sys.exit(1)

        tiene_costo = conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'ventas' AND column_name = 'costo_unitario'
        """)).first() is not None
        col_costo = "v.costo_unitario" if tiene_costo else "NULL::numeric"

        ventas = pd.read_sql(text(f"""
            SELECT v.id, v.fecha, v.creado_en, v.cantidad, v.precio_unitario AS precio_cobrado,
                   v.monto_total, v.es_consumo_interno, {col_costo} AS costo_guardado,
                   v.ticket_id
            FROM ventas v WHERE v.articulo_id = :id
            ORDER BY v.fecha, v.id
        """), conn, params={"id": aid})

        en_combos = pd.read_sql(text("""
            SELECT va.fecha, va.promocion_nombre, va.cantidad, va.monto_atribuido
            FROM v_ventas_analitica va
            WHERE va.articulo_id = :id AND va.promocion_id IS NOT NULL
            ORDER BY va.fecha
        """), conn, params={"id": aid})

        compras = pd.read_sql(text("""
            SELECT f.fecha, p.nombre AS proveedor, f.numero, df.cantidad, df.costo_unitario,
                   df.actualiza_precio
            FROM detalle_factura df
            JOIN facturas f ON f.id = df.factura_id
            LEFT JOIN proveedores p ON p.id = f.proveedor_id
            WHERE df.articulo_id = :id
            ORDER BY f.fecha, df.id
        """), conn, params={"id": aid})

        historial = pd.read_sql(text("""
            SELECT fecha, accion, detalle FROM historial_cambios
            WHERE tabla = 'articulos' AND registro_id = :id
            ORDER BY fecha
        """), conn, params={"id": aid})

    print("=" * 70)
    print("ARTÍCULO (datos de hoy)")
    print("=" * 70)
    print(art.to_string(index=False))

    print("\n" + "=" * 70)
    print(f"VENTAS SUELTAS ({len(ventas)})")
    print("=" * 70)
    if ventas.empty:
        print("(ninguna)")
    else:
        print(ventas.to_string(index=False))
        print("\nResumen por precio cobrado:")
        print(ventas.groupby("precio_cobrado")["cantidad"].sum().to_string())

    print("\n" + "=" * 70)
    print(f"VENDIDO DENTRO DE COMBOS ({len(en_combos)})")
    print("=" * 70)
    print("(ninguno)" if en_combos.empty else en_combos.to_string(index=False))

    print("\n" + "=" * 70)
    print(f"FACTURAS DE COMPRA ({len(compras)})")
    print("=" * 70)
    print("(ninguna)" if compras.empty else compras.to_string(index=False))

    print("\n" + "=" * 70)
    print(f"HISTORIAL DE CAMBIOS ({len(historial)})")
    print("=" * 70)
    print("(ninguno)" if historial.empty else historial.to_string(index=False))


if __name__ == "__main__":
    main()
