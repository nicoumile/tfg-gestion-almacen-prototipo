"""
Migración única (03/10/2026): exponer articulos.iva en v_articulos.

Esta es la otra mitad del problema de "el precio redondo queda con
centavos de más" (la primera mitad fue la precisión del margen,
migracion_precision_margen.py). v_articulos ya usaba a.iva puertas
adentro para calcular costo_venta_con_iva, pero no lo exponía como
columna -- así que "Actualizar precio / margen" (app.py) no tenía forma
de leer el IVA real del artículo, y tenía que adivinarlo dividiendo
costo_venta_con_iva / costo_venta_sin_iva. El problema es que esos dos
valores ya vienen redondeados a 2 decimales cada uno desde la vista, así
que esa división arrastraba un error de redondeo chico pero real -- y
ese IVA "adivinado" (no el real) es el que se usaba para despejar el
margen necesario para llegar al precio que se quería fijar. Resultado:
aunque el margen ya se guardaba con precisión de sobra (después de la
migración anterior), el precio recalculado podía quedar a un centavo
del valor redondo buscado, porque el cálculo arrancaba de un IVA
ligeramente distinto al real.

Se agrega "iva" al final de la lista de columnas de v_articulos (hay que
agregarla al final: Postgres no deja que un CREATE OR REPLACE VIEW
cambie el orden ni el tipo de columnas existentes, solo agregar nuevas
al final). No hace falta tocar v_ventas_analitica -- esa vista no
expone columnas de v_articulos, lee directo de articulos.

Se puede correr más de una vez sin romper nada.

Se corre localmente, igual que los scripts anteriores:

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python superados/migraciones/migracion_iva_en_vista.py
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


def paso_1_iva_en_vista():
    print("=" * 60)
    print("PASO 1: exponer iva en v_articulos")
    print("=" * 60)
    with db.engine.begin() as conn:
        fila = conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'v_articulos' AND column_name = 'iva'
        """)).first()

        if fila:
            print("v_articulos ya expone la columna iva. Salteado.\n")
        else:
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
                    sub.promo_tipo,
                    sub.iva
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
            print("v_articulos recreada -- ahora expone la columna iva.\n")


def main():
    probar_conexion()
    paso_1_iva_en_vista()
    print("=" * 60)
    print("Migración completa.")
    print("=" * 60)


if __name__ == "__main__":
    main()
