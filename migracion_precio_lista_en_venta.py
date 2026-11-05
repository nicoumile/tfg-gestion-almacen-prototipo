"""
Migración única (13/10/2026): guardar el precio de lista en cada venta.

Completa la migración del 12/10 (costo de cada venta). Hasta ahora,
Márgenes reales comparaba cada venta vieja contra el margen de HOY: si
un precio se actualizaba, todas las ventas anteriores aparecían como
"plata perdida" aunque se hubieran cobrado al precio correcto de ese día
(caso real: AGUA BIDON GARUA 5L, vendida a $3.700 hasta el 05/10 y a
$5.000 después).

Esta migración:

  1. Agrega ventas.precio_lista (y ventas.costo_unitario si todavía no se
     corrió migracion_costo_en_venta.py). Las ventas ya cargadas quedan en
     NULL: ese dato no se guardó en su momento.
  2. Actualiza el trigger trg_costo_en_venta: cada venta nueva guarda el
     costo y el precio de lista vigentes en ese momento.
  3. Actualiza la vista v_ventas_analitica (columna nueva lista_atribuida).

Incluye todo lo de migracion_costo_en_venta.py: alcanza con correr esta.
Se puede correr más de una vez sin romper nada.

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python migracion_precio_lista_en_venta.py
"""
import sys

import db
from sqlalchemy import text

FUNCION_TRIGGER = r"""
CREATE OR REPLACE FUNCTION fn_costo_en_venta() RETURNS TRIGGER AS $$
BEGIN
    -- Guarda, en el momento de vender, el costo de compra vigente y el
    -- precio de lista vigente (con IVA) del artículo. Al editar, solo se
    -- recalculan si cambia el artículo (corregir la cantidad no los cambia).
    -- Si la venta ya trae alguno de los dos valores, se respeta.
    IF TG_OP = 'INSERT' OR NEW.articulo_id IS DISTINCT FROM OLD.articulo_id THEN
        IF TG_OP = 'UPDATE' THEN
            NEW.costo_unitario := NULL;
            NEW.precio_lista := NULL;
        END IF;
        IF NEW.costo_unitario IS NULL THEN
            SELECT CASE
                       WHEN a.categoria = 'Promocion' THEN (
                           -- combo: costo de sus componentes en este momento
                           SELECT COALESCE(SUM(c.costo_compra_actual * pc.cantidad_por_unidad), 0)
                           FROM promocion_componentes pc
                           JOIN articulos c ON c.id = pc.componente_articulo_id
                           WHERE pc.promocion_articulo_id = a.id)
                       ELSE a.costo_compra_actual
                   END
              INTO NEW.costo_unitario
            FROM articulos a WHERE a.id = NEW.articulo_id;
        END IF;
        IF NEW.precio_lista IS NULL THEN
            -- mismo cálculo que v_articulos.costo_venta_con_iva
            SELECT ROUND(a.costo_compra_actual * (1 + a.margen_ganancia) * (1 + a.iva), 2)
              INTO NEW.precio_lista
            FROM articulos a WHERE a.id = NEW.articulo_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

CREAR_TRIGGER = """
CREATE TRIGGER trg_costo_en_venta
BEFORE INSERT OR UPDATE OF articulo_id ON ventas
FOR EACH ROW EXECUTE FUNCTION fn_costo_en_venta()
"""

VISTA = r"""
CREATE OR REPLACE VIEW v_ventas_analitica AS
WITH pesos AS (
    SELECT
        v.id AS venta_id,
        comp.id AS componente_articulo_id,
        pc.cantidad_por_unidad,
        (comp.costo_compra_actual * (1 + comp.margen_ganancia) * (1 + comp.iva))
            * pc.cantidad_por_unidad AS peso,
        comp.costo_compra_actual * pc.cantidad_por_unidad AS peso_costo
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
    -- costo vigente al momento de la venta; ventas viejas sin ese dato usan el actual
    ROUND(COALESCE(v.costo_unitario, a.costo_compra_actual) * v.cantidad, 2) AS costo_atribuido,
    NULL::INTEGER AS promocion_id,
    NULL::TEXT AS promocion_nombre,
    -- precio de lista vigente al momento de la venta x cantidad (NULL en ventas viejas)
    ROUND(v.precio_lista * v.cantidad, 2) AS lista_atribuida
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
    -- combo: el costo guardado al vender se reparte entre los componentes
    -- en proporción a su costo; ventas viejas sin ese dato usan el actual
    CASE
        WHEN v.costo_unitario IS NULL
            THEN ROUND(comp.costo_compra_actual * pc.cantidad_por_unidad * v.cantidad, 2)
        ELSE ROUND(
            COALESCE(
                v.costo_unitario * v.cantidad * p.peso_costo
                    / NULLIF(SUM(p.peso_costo) OVER (PARTITION BY v.id), 0),
                v.costo_unitario * v.cantidad / COUNT(*) OVER (PARTITION BY v.id)
            ), 2)
    END AS costo_atribuido,
    v.articulo_id AS promocion_id,
    promo.nombre AS promocion_nombre,
    -- precio de lista del combo, repartido igual que el ingreso
    ROUND(
        COALESCE(
            v.precio_lista * v.cantidad * p.peso / NULLIF(SUM(p.peso) OVER (PARTITION BY v.id), 0),
            v.precio_lista * v.cantidad / COUNT(*) OVER (PARTITION BY v.id)
        ), 2
    ) AS lista_atribuida
FROM ventas v
JOIN articulos promo ON promo.id = v.articulo_id AND promo.categoria = 'Promocion'
JOIN promocion_componentes pc ON pc.promocion_articulo_id = promo.id
JOIN articulos comp ON comp.id = pc.componente_articulo_id
JOIN pesos p ON p.venta_id = v.id AND p.componente_articulo_id = comp.id
"""


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


def paso_1_columnas():
    print("=" * 60)
    print("PASO 1: columnas ventas.costo_unitario y ventas.precio_lista")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS costo_unitario NUMERIC(12,2)"))
        conn.execute(text("ALTER TABLE ventas ADD COLUMN IF NOT EXISTS precio_lista NUMERIC(12,2)"))
        total = conn.execute(text("SELECT COUNT(*) FROM ventas WHERE precio_lista IS NULL")).scalar()
    print(f"Listo. {total} ventas ya cargadas quedan sin precio de lista guardado.\n")


def paso_2_trigger():
    print("=" * 60)
    print("PASO 2: trigger que guarda costo y precio de lista en cada venta nueva")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text(FUNCION_TRIGGER))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_costo_en_venta ON ventas"))
        conn.execute(text(CREAR_TRIGGER))
    print("trg_costo_en_venta actualizado.\n")


def paso_3_vista():
    print("=" * 60)
    print("PASO 3: v_ventas_analitica con precio de lista")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text(VISTA))
    print("Vista actualizada.\n")


def main():
    probar_conexion()
    paso_1_columnas()
    paso_2_trigger()
    paso_3_vista()
    print("=" * 60)
    print("Migración completa.")
    print("=" * 60)


if __name__ == "__main__":
    main()
