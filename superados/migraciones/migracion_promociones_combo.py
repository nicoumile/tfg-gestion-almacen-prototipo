"""
Migración única para promociones con stock compartido ("combo virtual")
+ vista analítica de ventas (01/10/2026):

1. Agrega articulos.promo_tipo ('fisica' | 'virtual'). Las promociones ya
   creadas (si hay) quedan como 'fisica' -- mismo comportamiento que tenían
   hasta ahora, nada se rompe.
2. Redefine v_articulos para que una promoción 'virtual' muestre como
   stock_actual el mínimo de "stock disponible / cantidad que necesita"
   entre sus componentes (cuántos combos se pueden vender ahora mismo).
3. Redefine los triggers fn_aplicar_venta / fn_revertir_venta /
   fn_editar_venta para que, cuando el artículo vendido sea una promoción
   'virtual', el movimiento de stock se aplique sobre sus componentes
   (no sobre la promoción, que no tiene stock propio).
4. Crea v_ventas_analitica: resuelve cada venta de promoción en sus
   artículos componentes (reparte ingreso proporcional a su precio
   individual, costo real de cada uno) -- para que clasificación ABC,
   márgenes reales y pronóstico de demanda (núcleo analítico, pendiente)
   no queden ciegos a lo vendido en combo.

Se puede correr más de una vez sin romper nada (cada paso se fija si ya
está aplicado, y las vistas/funciones usan CREATE OR REPLACE).

Se corre localmente, igual que el script anterior:

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python superados/migraciones/migracion_promociones_combo.py
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


def columna_existe(conn, tabla, columna):
    r = conn.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = :t AND column_name = :c
    """), {"t": tabla, "c": columna}).first()
    return r is not None


def paso_1_promo_tipo():
    print("=" * 60)
    print("PASO 1: tipo de promoción (fisica / virtual)")
    print("=" * 60)
    with db.engine.begin() as conn:
        if columna_existe(conn, "articulos", "promo_tipo"):
            print("La columna articulos.promo_tipo ya existe. Salteado.")
        else:
            conn.execute(text("""
                ALTER TABLE articulos ADD COLUMN promo_tipo TEXT
                CHECK (promo_tipo IN ('fisica', 'virtual'))
            """))
            print("Columna articulos.promo_tipo agregada.")

        actualizadas = conn.execute(text("""
            UPDATE articulos SET promo_tipo = 'fisica'
            WHERE categoria = 'Promocion' AND promo_tipo IS NULL
        """)).rowcount
        if actualizadas:
            print(f"{actualizadas} promoción(es) existente(s) marcada(s) como 'fisica' "
                  "(mismo comportamiento que ya tenían).")
        print()


def paso_2_vista_articulos():
    print("=" * 60)
    print("PASO 2: v_articulos (stock en vivo para combos virtuales)")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text("""
            CREATE OR REPLACE VIEW v_articulos AS
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
            ) sub;
        """))
        print("Vista v_articulos actualizada.\n")


def paso_3_triggers_venta():
    print("=" * 60)
    print("PASO 3: triggers de venta (redirigen el stock a los componentes)")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION fn_aplicar_venta() RETURNS TRIGGER AS $$
            DECLARE
                v_promo_tipo TEXT;
            BEGIN
                IF NEW.articulo_id IS NOT NULL THEN
                    SELECT promo_tipo INTO v_promo_tipo FROM articulos
                    WHERE id = NEW.articulo_id AND categoria = 'Promocion';

                    IF v_promo_tipo = 'virtual' THEN
                        UPDATE articulos a
                        SET stock_actual = a.stock_actual - (pc.cantidad_por_unidad * NEW.cantidad),
                            actualizado_en = now()
                        FROM promocion_componentes pc
                        WHERE pc.promocion_articulo_id = NEW.articulo_id
                          AND a.id = pc.componente_articulo_id;
                    ELSE
                        UPDATE articulos
                        SET stock_actual = stock_actual - NEW.cantidad,
                            actualizado_en = now()
                        WHERE id = NEW.articulo_id;
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION fn_revertir_venta() RETURNS TRIGGER AS $$
            DECLARE
                v_promo_tipo TEXT;
            BEGIN
                IF OLD.articulo_id IS NOT NULL THEN
                    SELECT promo_tipo INTO v_promo_tipo FROM articulos
                    WHERE id = OLD.articulo_id AND categoria = 'Promocion';

                    IF v_promo_tipo = 'virtual' THEN
                        UPDATE articulos a
                        SET stock_actual = a.stock_actual + (pc.cantidad_por_unidad * OLD.cantidad),
                            actualizado_en = now()
                        FROM promocion_componentes pc
                        WHERE pc.promocion_articulo_id = OLD.articulo_id
                          AND a.id = pc.componente_articulo_id;
                    ELSE
                        UPDATE articulos
                        SET stock_actual = stock_actual + OLD.cantidad,
                            actualizado_en = now()
                        WHERE id = OLD.articulo_id;
                    END IF;
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql;
        """))
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION fn_editar_venta() RETURNS TRIGGER AS $$
            DECLARE
                v_old_tipo TEXT;
                v_new_tipo TEXT;
            BEGIN
                IF OLD.articulo_id IS NOT NULL THEN
                    SELECT promo_tipo INTO v_old_tipo FROM articulos
                    WHERE id = OLD.articulo_id AND categoria = 'Promocion';

                    IF v_old_tipo = 'virtual' THEN
                        UPDATE articulos a
                        SET stock_actual = a.stock_actual + (pc.cantidad_por_unidad * OLD.cantidad),
                            actualizado_en = now()
                        FROM promocion_componentes pc
                        WHERE pc.promocion_articulo_id = OLD.articulo_id
                          AND a.id = pc.componente_articulo_id;
                    ELSE
                        UPDATE articulos SET stock_actual = stock_actual + OLD.cantidad, actualizado_en = now()
                        WHERE id = OLD.articulo_id;
                    END IF;
                END IF;
                IF NEW.articulo_id IS NOT NULL THEN
                    SELECT promo_tipo INTO v_new_tipo FROM articulos
                    WHERE id = NEW.articulo_id AND categoria = 'Promocion';

                    IF v_new_tipo = 'virtual' THEN
                        UPDATE articulos a
                        SET stock_actual = a.stock_actual - (pc.cantidad_por_unidad * NEW.cantidad),
                            actualizado_en = now()
                        FROM promocion_componentes pc
                        WHERE pc.promocion_articulo_id = NEW.articulo_id
                          AND a.id = pc.componente_articulo_id;
                    ELSE
                        UPDATE articulos SET stock_actual = stock_actual - NEW.cantidad, actualizado_en = now()
                        WHERE id = NEW.articulo_id;
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))
        print("Triggers fn_aplicar_venta / fn_revertir_venta / fn_editar_venta actualizados.\n")


def paso_4_vista_analitica():
    print("=" * 60)
    print("PASO 4: v_ventas_analitica (desglosa combos en sus componentes)")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text("""
            CREATE OR REPLACE VIEW v_ventas_analitica AS
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
            JOIN pesos p ON p.venta_id = v.id AND p.componente_articulo_id = comp.id;
        """))
        print("Vista v_ventas_analitica creada.\n")


def main():
    probar_conexion()
    paso_1_promo_tipo()
    paso_2_vista_articulos()
    paso_3_triggers_venta()
    paso_4_vista_analitica()
    print("=" * 60)
    print("Migración completa.")
    print("=" * 60)


if __name__ == "__main__":
    main()
