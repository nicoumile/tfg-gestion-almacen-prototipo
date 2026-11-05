"""
Migración única (03/10/2026): más precisión en articulos.margen_ganancia.

Hoy se guarda con NUMERIC(5,4) -- 2 decimales de % de ganancia. Cuando en
"Actualizar precio / margen" fijás un precio de venta "redondo" (ej: $1500)
y se despeja el margen necesario para llegar a él, ese margen se redondeaba
a 2 decimales de % antes de guardarlo -- y el precio final recalculado a
partir de ese margen "redondeado" ya no daba exacto (quedaba, por ejemplo,
en $1500.04). Se amplía la columna a NUMERIC(10,8) para que el margen se
pueda guardar con la precisión que hace falta para que el precio final
quede exacto, sin que eso cambie cómo se ve el % en pantalla (sigue
mostrándose con 2 decimales).

Se puede correr más de una vez sin romper nada.

Se corre localmente, igual que los scripts anteriores:

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python migracion_precision_margen.py
"""
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


def paso_1_precision_margen():
    print("=" * 60)
    print("PASO 1: precisión de articulos.margen_ganancia")
    print("=" * 60)
    with db.engine.begin() as conn:
        fila = conn.execute(text("""
            SELECT numeric_precision, numeric_scale FROM information_schema.columns
            WHERE table_name = 'articulos' AND column_name = 'margen_ganancia'
        """)).mappings().first()

        if fila and int(fila["numeric_precision"]) == 10 and int(fila["numeric_scale"]) == 8:
            print("La columna ya tiene la precisión nueva (NUMERIC(10,8)). Salteado.\n")
        else:
            # Postgres no deja cambiar el tipo de una columna de la que
            # dependen vistas (v_articulos Y v_ventas_analitica, esta
            # última a través de comp.margen_ganancia en su CTE "pesos")
            # -- hay que sacarlas, cambiar la columna, y volver a crearlas
            # igual que estaban.
            conn.execute(text("DROP VIEW IF EXISTS v_ventas_analitica"))
            conn.execute(text("DROP VIEW IF EXISTS v_articulos"))
            conn.execute(text("""
                ALTER TABLE articulos ALTER COLUMN margen_ganancia TYPE NUMERIC(10,8)
            """))
            conn.execute(text("""
                CREATE VIEW v_articulos AS
                SELECT
                    sub.id,
                    sub.nombre,
                    sub.marca,
                    sub.categoria,
                    sub.costo_compra_actual,
                    ROUND(sub.costo_compra_actual * (1 + sub.margen_ganancia), 2) AS costo_venta_sin_iva,
                    ROUND(sub.costo_compra_actual * (1 + sub.margen_ganancia) * (1 + sub.iva), 2) AS costo_venta_con_iva,
                    sub.margen_ganancia * 100 AS porcentaje_ganancia,
                    sub.stock_actual,
                    sub.stock_minimo,
                    sub.fecha_vencimiento,
                    sub.acceso_rapido,
                    sub.unidad_medida,
                    sub.articulo_fraccion_de,
                    sub.factor_fraccion,
                    sub.peso_bolsa_gramos,
                    CASE WHEN sub.stock_actual <= sub.stock_minimo THEN TRUE ELSE FALSE END AS stock_bajo,
                    CASE WHEN sub.fecha_vencimiento IS NOT NULL
                              AND sub.fecha_vencimiento <= (CURRENT_DATE + INTERVAL '15 days')
                         THEN TRUE ELSE FALSE END AS vencimiento_cercano,
                    sub.promo_tipo
                FROM (
                    SELECT
                        a.id, a.nombre, a.marca, a.categoria, a.costo_compra_actual,
                        a.margen_ganancia, a.iva,
                        CASE
                            WHEN a.categoria = 'Promocion' AND a.promo_tipo = 'virtual'
                                THEN COALESCE(vp.stock_virtual, 0)
                            ELSE a.stock_actual
                        END::NUMERIC(12,2) AS stock_actual,
                        a.stock_minimo, a.fecha_vencimiento, a.acceso_rapido, a.unidad_medida,
                        a.articulo_fraccion_de, a.factor_fraccion, a.peso_bolsa_gramos, a.promo_tipo
                    FROM articulos a
                    LEFT JOIN (
                        SELECT pc.promocion_articulo_id,
                               FLOOR(MIN(comp.stock_actual / pc.cantidad_por_unidad)) AS stock_virtual
                        FROM promocion_componentes pc
                        JOIN articulos comp ON comp.id = pc.componente_articulo_id
                        GROUP BY pc.promocion_articulo_id
                    ) vp ON vp.promocion_articulo_id = a.id
                ) sub
            """))
            conn.execute(text("""
                CREATE VIEW v_ventas_analitica AS
                WITH pesos AS (
                    SELECT
                        v.id AS venta_id,
                        comp.id AS componente_articulo_id,
                        pc.cantidad_por_unidad,
                        (comp.costo_compra_actual * (1 + comp.margen_ganancia) * (1 + comp.iva))
                            * pc.cantidad_por_unidad AS peso
                    FROM ventas v
                    JOIN articulos promo ON promo.id = v.articulo_id AND promo.categoria = 'Promocion'
                    JOIN promocion_componentes pc ON pc.promocion_articulo_id = promo.id
                    JOIN articulos comp ON comp.id = pc.componente_articulo_id
                )
                SELECT
                    v.id AS venta_id, v.fecha, v.medio_pago, v.es_consumo_interno,
                    a.id AS articulo_id, a.nombre AS articulo,
                    v.cantidad AS cantidad,
                    v.monto_total AS monto_atribuido,
                    ROUND(a.costo_compra_actual * v.cantidad, 2) AS costo_atribuido,
                    NULL::INTEGER AS promocion_id,
                    NULL::TEXT AS promocion_nombre
                FROM ventas v
                JOIN articulos a ON a.id = v.articulo_id
                WHERE a.categoria IS DISTINCT FROM 'Promocion'

                UNION ALL

                SELECT
                    v.id AS venta_id, v.fecha, v.medio_pago, v.es_consumo_interno,
                    comp.id AS articulo_id, comp.nombre AS articulo,
                    (pc.cantidad_por_unidad * v.cantidad) AS cantidad,
                    ROUND(
                        COALESCE(
                            v.monto_total * p.peso / NULLIF(SUM(p.peso) OVER (PARTITION BY v.id), 0),
                            v.monto_total / COUNT(*) OVER (PARTITION BY v.id)
                        ), 2
                    ) AS monto_atribuido,
                    ROUND(comp.costo_compra_actual * pc.cantidad_por_unidad * v.cantidad, 2) AS costo_atribuido,
                    v.articulo_id AS promocion_id,
                    promo.nombre AS promocion_nombre
                FROM ventas v
                JOIN articulos promo ON promo.id = v.articulo_id AND promo.categoria = 'Promocion'
                JOIN promocion_componentes pc ON pc.promocion_articulo_id = promo.id
                JOIN articulos comp ON comp.id = pc.componente_articulo_id
                JOIN pesos p ON p.venta_id = v.id AND p.componente_articulo_id = comp.id
            """))
            print("Columna articulos.margen_ganancia ampliada a NUMERIC(10,8) (y v_articulos / v_ventas_analitica recreadas igual que estaban).\n")


def main():
    probar_conexion()
    paso_1_precision_margen()
    print("=" * 60)
    print("Migración completa.")
    print("=" * 60)


if __name__ == "__main__":
    main()
