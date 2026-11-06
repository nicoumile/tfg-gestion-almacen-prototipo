"""
Migración única (tanda 5, 10/2026): cantidades con 3 decimales y desglose de
combos guardado al vender. Ver claude/auditoria-app-20oct2026.md (A9 y C3).

QUÉ HACE (todo en UNA transacción: si algo falla o la verificación no da,
no queda NADA cambiado):

  1. Cantidades con 3 decimales (antes 2): ventas.cantidad,
     detalle_factura.cantidad, articulos.stock_actual, articulos.stock_minimo
     y fraccionamientos.cantidad_bolsas. Ej.: 150 g de una bolsa de 420 g son
     0,357 bolsas; antes se guardaba 0,36. Los valores ya cargados no cambian
     (2,50 pasa a 2,500).

  2. Tabla nueva venta_componentes: por cada venta de un combo, qué
     componentes llevaba, en qué cantidad, a qué precio y costo de ese
     momento, y si descontó stock de los componentes (combo virtual) o del
     combo armado (físico). La llena el trigger de ventas al vender. Para las
     ventas de combo ya cargadas se completa con la composición y los precios
     de HOY, que es exactamente lo que la app venía usando: ningún número
     cambia al migrar.
     Desde ahora, v_ventas_analitica (ABC, márgenes, pronóstico) lee ese
     desglose. Subir el precio de un componente o cambiar la composición de
     un combo ya no reescribe las ventas pasadas, y borrar o editar una venta
     vieja de combo devuelve el stock a los componentes que realmente llevó.

  3. Verifica antes de confirmar: mismas filas en todas las tablas, mismo
     stock, misma vista de artículos y MISMA v_ventas_analitica fila por fila
     que antes de migrar. Si algo no coincide, deshace todo.

  4. Guarda las definiciones viejas (vistas, funciones y trigger) en la tabla
     migracion_respaldo_objetos, para poder volver atrás con:
         python superados/migraciones/migracion_tanda5.py --revertir

Se puede correr más de una vez: si ya está aplicada, avisa y no hace nada.
La app actual funciona igual con la base migrada.

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python analisis/respaldo_produccion.py
    python superados/migraciones/migracion_tanda5.py
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
import sys

import db
from sqlalchemy import text

MIGRACION = "tanda5"

COLUMNAS = [  # (tabla, columna)
    ("ventas", "cantidad"),
    ("detalle_factura", "cantidad"),
    ("articulos", "stock_actual"),
    ("articulos", "stock_minimo"),
    ("fraccionamientos", "cantidad_bolsas"),
]

TABLA_DESGLOSE = """
CREATE TABLE venta_componentes (
    venta_id INTEGER NOT NULL REFERENCES ventas(id) ON DELETE CASCADE,
    componente_articulo_id INTEGER NOT NULL REFERENCES articulos(id),
    cantidad_por_unidad NUMERIC(12,4) NOT NULL CHECK (cantidad_por_unidad > 0),
    -- precio de lista con IVA y costo del componente x cantidad por combo,
    -- AL MOMENTO DE VENDER (sin redondear: son los pesos con los que se
    -- reparte el ingreso y el costo del combo entre sus componentes)
    precio_ref NUMERIC NOT NULL,
    costo_ref NUMERIC NOT NULL,
    -- TRUE: combo virtual, la venta descontó stock de los componentes.
    -- FALSE: combo armado (físico), la venta descontó stock del combo.
    mueve_stock BOOLEAN NOT NULL,
    PRIMARY KEY (venta_id, componente_articulo_id)
)
"""

INDICE_DESGLOSE = "CREATE INDEX idx_venta_componentes_componente ON venta_componentes (componente_articulo_id)"

# Ventas de combo ya cargadas: composición y precios de HOY (lo mismo que usaba
# la vista hasta ahora), así ningún número cambia al migrar.
COMPLETAR_DESGLOSE = """
INSERT INTO venta_componentes
    (venta_id, componente_articulo_id, cantidad_por_unidad, precio_ref, costo_ref, mueve_stock)
SELECT v.id, comp.id, pc.cantidad_por_unidad,
       comp.costo_compra_actual * (1 + comp.margen_ganancia) * (1 + comp.iva) * pc.cantidad_por_unidad,
       comp.costo_compra_actual * pc.cantidad_por_unidad,
       COALESCE(promo.promo_tipo, '') = 'virtual'
FROM ventas v
JOIN articulos promo ON promo.id = v.articulo_id AND promo.categoria = 'Promocion'
JOIN promocion_componentes pc ON pc.promocion_articulo_id = promo.id
JOIN articulos comp ON comp.id = pc.componente_articulo_id
"""

VISTA_ANALITICA = """
CREATE VIEW v_ventas_analitica AS
WITH pesos AS (
    -- Desglose guardado al vender (venta_componentes): el combo se reparte
    -- con los precios y costos de ESE momento, no con los de hoy.
    SELECT vc.venta_id, vc.componente_articulo_id, vc.cantidad_por_unidad,
           vc.precio_ref AS peso, vc.costo_ref AS peso_costo
    FROM venta_componentes vc
)
SELECT
    v.id AS venta_id, v.fecha, v.medio_pago, v.es_consumo_interno,
    a.id AS articulo_id, a.nombre AS articulo,
    v.cantidad AS cantidad,
    v.monto_total AS monto_atribuido,
    ROUND(COALESCE(v.costo_unitario, a.costo_compra_actual) * v.cantidad, 2) AS costo_atribuido,
    NULL::INTEGER AS promocion_id,
    NULL::TEXT AS promocion_nombre,
    ROUND(v.precio_lista * v.cantidad, 2) AS lista_atribuida
FROM ventas v
JOIN articulos a ON a.id = v.articulo_id
WHERE a.categoria IS DISTINCT FROM 'Promocion'

UNION ALL

SELECT
    v.id AS venta_id, v.fecha, v.medio_pago, v.es_consumo_interno,
    comp.id AS articulo_id, comp.nombre AS articulo,
    (p.cantidad_por_unidad * v.cantidad) AS cantidad,
    ROUND(
        COALESCE(
            v.monto_total * p.peso / NULLIF(SUM(p.peso) OVER (PARTITION BY v.id), 0),
            v.monto_total / COUNT(*) OVER (PARTITION BY v.id)
        ), 2
    ) AS monto_atribuido,
    CASE
        WHEN v.costo_unitario IS NULL
            THEN ROUND(p.peso_costo * v.cantidad, 2)
        ELSE ROUND(
            COALESCE(
                v.costo_unitario * v.cantidad * p.peso_costo
                    / NULLIF(SUM(p.peso_costo) OVER (PARTITION BY v.id), 0),
                v.costo_unitario * v.cantidad / COUNT(*) OVER (PARTITION BY v.id)
            ), 2)
    END AS costo_atribuido,
    v.articulo_id AS promocion_id,
    promo.nombre AS promocion_nombre,
    ROUND(
        COALESCE(
            v.precio_lista * v.cantidad * p.peso / NULLIF(SUM(p.peso) OVER (PARTITION BY v.id), 0),
            v.precio_lista * v.cantidad / COUNT(*) OVER (PARTITION BY v.id)
        ), 2
    ) AS lista_atribuida
FROM ventas v
JOIN articulos promo ON promo.id = v.articulo_id AND promo.categoria = 'Promocion'
JOIN pesos p ON p.venta_id = v.id
JOIN articulos comp ON comp.id = p.componente_articulo_id
"""

FN_APLICAR_VENTA = """
CREATE OR REPLACE FUNCTION fn_aplicar_venta() RETURNS TRIGGER AS $$
DECLARE
    v_es_promo BOOLEAN;
    v_promo_tipo TEXT;
BEGIN
    IF NEW.articulo_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT TRUE, promo_tipo INTO v_es_promo, v_promo_tipo FROM articulos
    WHERE id = NEW.articulo_id AND categoria = 'Promocion';

    -- Combo: se guarda qué llevó, a qué precio y costo de este momento.
    IF v_es_promo THEN
        INSERT INTO venta_componentes
            (venta_id, componente_articulo_id, cantidad_por_unidad, precio_ref, costo_ref, mueve_stock)
        SELECT NEW.id, comp.id, pc.cantidad_por_unidad,
               comp.costo_compra_actual * (1 + comp.margen_ganancia) * (1 + comp.iva) * pc.cantidad_por_unidad,
               comp.costo_compra_actual * pc.cantidad_por_unidad,
               COALESCE(v_promo_tipo, '') = 'virtual'
        FROM promocion_componentes pc
        JOIN articulos comp ON comp.id = pc.componente_articulo_id
        WHERE pc.promocion_articulo_id = NEW.articulo_id;
    END IF;

    IF v_promo_tipo = 'virtual' THEN
        -- combo de stock compartido: descuenta de los componentes
        UPDATE articulos a
        SET stock_actual = a.stock_actual - (vc.cantidad_por_unidad * NEW.cantidad),
            actualizado_en = now()
        FROM venta_componentes vc
        WHERE vc.venta_id = NEW.id AND a.id = vc.componente_articulo_id;
    ELSE
        UPDATE articulos
        SET stock_actual = stock_actual - NEW.cantidad,
            actualizado_en = now()
        WHERE id = NEW.articulo_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# Devuelve el stock de una venta según lo que REALMENTE movió al venderse
# (desglose guardado); si no tiene desglose, como antes.
FN_DEVOLVER_STOCK = """
CREATE OR REPLACE FUNCTION fn_devolver_stock_venta(p_venta_id INTEGER, p_articulo_id INTEGER,
                                                   p_cantidad NUMERIC) RETURNS VOID AS $$
DECLARE
    v_hay_desglose BOOLEAN;
    v_mueve BOOLEAN;
    v_promo_tipo TEXT;
BEGIN
    IF p_articulo_id IS NULL THEN
        RETURN;
    END IF;
    SELECT COUNT(*) > 0, BOOL_OR(mueve_stock) INTO v_hay_desglose, v_mueve
    FROM venta_componentes WHERE venta_id = p_venta_id;

    IF v_hay_desglose THEN
        IF v_mueve THEN
            UPDATE articulos a
            SET stock_actual = a.stock_actual + (vc.cantidad_por_unidad * p_cantidad),
                actualizado_en = now()
            FROM venta_componentes vc
            WHERE vc.venta_id = p_venta_id AND a.id = vc.componente_articulo_id;
        ELSE
            UPDATE articulos SET stock_actual = stock_actual + p_cantidad, actualizado_en = now()
            WHERE id = p_articulo_id;
        END IF;
        RETURN;
    END IF;

    -- sin desglose (no es combo): igual que siempre
    SELECT promo_tipo INTO v_promo_tipo FROM articulos
    WHERE id = p_articulo_id AND categoria = 'Promocion';
    IF v_promo_tipo = 'virtual' THEN
        UPDATE articulos a
        SET stock_actual = a.stock_actual + (pc.cantidad_por_unidad * p_cantidad),
            actualizado_en = now()
        FROM promocion_componentes pc
        WHERE pc.promocion_articulo_id = p_articulo_id AND a.id = pc.componente_articulo_id;
    ELSE
        UPDATE articulos SET stock_actual = stock_actual + p_cantidad, actualizado_en = now()
        WHERE id = p_articulo_id;
    END IF;
END;
$$ LANGUAGE plpgsql
"""

# BEFORE DELETE (antes era AFTER): tiene que leer el desglose ANTES de que el
# ON DELETE CASCADE lo borre.
FN_REVERTIR_VENTA = """
CREATE OR REPLACE FUNCTION fn_revertir_venta() RETURNS TRIGGER AS $$
BEGIN
    PERFORM fn_devolver_stock_venta(OLD.id, OLD.articulo_id, OLD.cantidad);
    RETURN OLD;
END;
$$ LANGUAGE plpgsql
"""

TRIGGER_REVERTIR = [
    "DROP TRIGGER trg_revertir_venta ON ventas",
    "CREATE TRIGGER trg_revertir_venta BEFORE DELETE ON ventas "
    "FOR EACH ROW EXECUTE FUNCTION fn_revertir_venta()",
]

FN_EDITAR_VENTA = """
CREATE OR REPLACE FUNCTION fn_editar_venta() RETURNS TRIGGER AS $$
DECLARE
    v_es_promo BOOLEAN;
    v_promo_tipo TEXT;
BEGIN
    -- 1) devolver lo que movió la venta tal como estaba (con SU desglose)
    PERFORM fn_devolver_stock_venta(OLD.id, OLD.articulo_id, OLD.cantidad);

    -- 2) si cambió el artículo, el desglose pasa a ser el del artículo nuevo
    IF OLD.articulo_id IS DISTINCT FROM NEW.articulo_id THEN
        DELETE FROM venta_componentes WHERE venta_id = NEW.id;
        IF NEW.articulo_id IS NOT NULL THEN
            SELECT TRUE, promo_tipo INTO v_es_promo, v_promo_tipo FROM articulos
            WHERE id = NEW.articulo_id AND categoria = 'Promocion';
            IF v_es_promo THEN
                INSERT INTO venta_componentes
                    (venta_id, componente_articulo_id, cantidad_por_unidad, precio_ref, costo_ref, mueve_stock)
                SELECT NEW.id, comp.id, pc.cantidad_por_unidad,
                       comp.costo_compra_actual * (1 + comp.margen_ganancia) * (1 + comp.iva) * pc.cantidad_por_unidad,
                       comp.costo_compra_actual * pc.cantidad_por_unidad,
                       COALESCE(v_promo_tipo, '') = 'virtual'
                FROM promocion_componentes pc
                JOIN articulos comp ON comp.id = pc.componente_articulo_id
                WHERE pc.promocion_articulo_id = NEW.articulo_id;
            END IF;
        END IF;
    END IF;

    -- 3) aplicar la venta como quedó
    IF NEW.articulo_id IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM venta_componentes WHERE venta_id = NEW.id AND mueve_stock) THEN
            UPDATE articulos a
            SET stock_actual = a.stock_actual - (vc.cantidad_por_unidad * NEW.cantidad),
                actualizado_en = now()
            FROM venta_componentes vc
            WHERE vc.venta_id = NEW.id AND a.id = vc.componente_articulo_id;
        ELSE
            UPDATE articulos SET stock_actual = stock_actual - NEW.cantidad, actualizado_en = now()
            WHERE id = NEW.articulo_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

RESPALDO_OBJETOS = """
CREATE TABLE IF NOT EXISTS migracion_respaldo_objetos (
    migracion TEXT NOT NULL,
    objeto TEXT NOT NULL,
    tipo TEXT NOT NULL,
    sql TEXT NOT NULL,
    guardado_en TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (migracion, objeto)
)
"""

# Huellas para verificar que la migración no cambió ningún dato ni ningún
# número calculado. round(x, 4) normaliza 2,50 / 2,500 a 2,5000.
HUELLAS = {
    "v_ventas_analitica": """
        SELECT md5(string_agg(concat_ws('|', venta_id, fecha, medio_pago, es_consumo_interno, articulo_id,
                                        round(cantidad, 4), monto_atribuido, costo_atribuido,
                                        promocion_id, lista_atribuida), ';'
                              ORDER BY venta_id, articulo_id))
        FROM v_ventas_analitica
    """,
    "v_articulos": """
        SELECT md5(string_agg(concat_ws('|', id, nombre, costo_compra_actual, costo_venta_sin_iva, costo_venta_con_iva,
                                        round(porcentaje_ganancia, 4), round(stock_actual, 4), round(stock_minimo, 4),
                                        stock_bajo, vencimiento_cercano, activo), ';' ORDER BY id))
        FROM v_articulos
    """,
    "stock": "SELECT concat_ws('|', COUNT(*), round(SUM(stock_actual), 4), round(SUM(stock_minimo), 4)) FROM articulos",
    "ventas": "SELECT concat_ws('|', COUNT(*), round(SUM(cantidad), 4), SUM(monto_total), MAX(id)) FROM ventas",
    "compras": "SELECT concat_ws('|', COUNT(*), round(SUM(cantidad), 4), SUM(costo_unitario)) FROM detalle_factura",
    "fraccionamientos": "SELECT concat_ws('|', COUNT(*), round(SUM(cantidad_bolsas), 4)) FROM fraccionamientos",
}


def _escala(conn, tabla, columna):
    return conn.execute(text("""
        SELECT numeric_scale FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :t AND column_name = :c
    """), {"t": tabla, "c": columna}).scalar()


def estado(conn):
    escalas = {f"{t}.{c}": _escala(conn, t, c) for t, c in COLUMNAS}
    tiene_desglose = conn.execute(text("SELECT to_regclass('public.venta_componentes')")).scalar() is not None
    if all(e == 3 for e in escalas.values()) and tiene_desglose:
        return "aplicada", escalas
    if all(e == 2 for e in escalas.values()) and not tiene_desglose:
        return "sin_aplicar", escalas
    return "parcial", escalas


def huellas(conn):
    return {k: conn.execute(text(q)).scalar() for k, q in HUELLAS.items()}


def filas_por_tabla(conn):
    tablas = conn.execute(text("""
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
          AND tablename NOT IN ('venta_componentes', 'migracion_respaldo_objetos') ORDER BY 1
    """)).scalars().all()
    return {t: conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() for t in tablas}


def migrar():
    with db.engine.begin() as conn:
        est, escalas = estado(conn)
        if est == "aplicada":
            print("La migración ya estaba aplicada. No se hizo nada.")
            return
        if est == "parcial":
            print(f"*** La base está en un estado intermedio, no sigo: {escalas} ***")
            sys.exit(1)

        print("Tomando huellas de los datos antes de migrar ...")
        antes, filas_antes = huellas(conn), filas_por_tabla(conn)
        ventas_combo = conn.execute(text("""
            SELECT COUNT(*) FROM ventas v JOIN articulos a ON a.id = v.articulo_id WHERE a.categoria = 'Promocion'
        """)).scalar()

        # Respaldo de las definiciones viejas (para --revertir)
        conn.execute(text(RESPALDO_OBJETOS))
        conn.execute(text("DELETE FROM migracion_respaldo_objetos WHERE migracion = :m"), {"m": MIGRACION})
        viejos = {
            "v_articulos": ("vista", "CREATE VIEW v_articulos AS " +
                            conn.execute(text("SELECT pg_get_viewdef('v_articulos'::regclass, true)")).scalar()),
            "v_ventas_analitica": ("vista", "CREATE VIEW v_ventas_analitica AS " +
                                   conn.execute(text("SELECT pg_get_viewdef('v_ventas_analitica'::regclass, true)")).scalar()),
        }
        for fn in ("fn_aplicar_venta", "fn_revertir_venta", "fn_editar_venta"):
            viejos[fn] = ("funcion", conn.execute(text(
                "SELECT pg_get_functiondef(CAST(:f AS regproc))"), {"f": fn}).scalar())
        for trg in ("trg_revertir_venta", "trg_editar_venta"):
            viejos[trg] = ("trigger", conn.execute(text(
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgname = :t"), {"t": trg}).scalar())
        for objeto, (tipo, sql) in viejos.items():
            conn.execute(text("INSERT INTO migracion_respaldo_objetos (migracion, objeto, tipo, sql) "
                              "VALUES (:m, :o, :t, :s)"), {"m": MIGRACION, "o": objeto, "t": tipo, "s": sql})

        # 1) Cantidades con 3 decimales (las dos vistas dependen de estas columnas)
        vista_articulos = viejos["v_articulos"][1]
        if vista_articulos.count("::numeric(12,2) AS stock_actual") != 1:
            print("*** No reconozco la definición de v_articulos; no sigo. ***")
            sys.exit(1)
        conn.execute(text("DROP VIEW v_ventas_analitica"))
        conn.execute(text("DROP VIEW v_articulos"))
        # el trigger de edición compara OLD.cantidad / NEW.cantidad en su WHEN:
        # depende de la columna, hay que sacarlo y volver a crearlo igual
        conn.execute(text("DROP TRIGGER trg_editar_venta ON ventas"))
        for tabla, columna in COLUMNAS:
            conn.execute(text(f"ALTER TABLE {tabla} ALTER COLUMN {columna} TYPE NUMERIC(12,3)"))
        conn.execute(text(viejos["trg_editar_venta"][1]))
        conn.execute(text(vista_articulos.replace("::numeric(12,2) AS stock_actual",
                                                  "::numeric(12,3) AS stock_actual")))

        # 2) Desglose de combos
        conn.execute(text(TABLA_DESGLOSE))
        conn.execute(text(INDICE_DESGLOSE))
        conn.execute(text(COMPLETAR_DESGLOSE))
        conn.execute(text(VISTA_ANALITICA))
        conn.execute(text(FN_APLICAR_VENTA))
        conn.execute(text(FN_DEVOLVER_STOCK))
        conn.execute(text(FN_REVERTIR_VENTA))
        for q in TRIGGER_REVERTIR:
            conn.execute(text(q))
        conn.execute(text(FN_EDITAR_VENTA))

        # 3) Verificación (si algo no da, la excepción deshace toda la transacción)
        despues, filas_despues = huellas(conn), filas_por_tabla(conn)
        distintos = [k for k in antes if antes[k] != despues[k]]
        if filas_antes != filas_despues:
            distintos.append("filas por tabla")
        desglose = conn.execute(text("SELECT COUNT(DISTINCT venta_id) FROM venta_componentes")).scalar()
        combos_con_componentes = conn.execute(text("""
            SELECT COUNT(*) FROM ventas v JOIN articulos a ON a.id = v.articulo_id
            WHERE a.categoria = 'Promocion'
              AND EXISTS (SELECT 1 FROM promocion_componentes pc WHERE pc.promocion_articulo_id = a.id)
        """)).scalar()
        if desglose != combos_con_componentes:
            distintos.append(f"desglose ({desglose} ventas de combo con desglose, se esperaban {combos_con_componentes})")
        if distintos:
            raise RuntimeError(f"La verificación no dio en: {distintos}. No se cambió nada.")
        print(f"Verificación OK: {len(filas_despues)} tablas con las mismas filas, mismo stock, misma vista de "
              f"artículos y misma v_ventas_analitica fila por fila.")
        print(f"Desglose guardado para {desglose} de {ventas_combo} ventas de combo "
              f"(las que tienen componentes cargados).")
    print("Migración completa.")


def revertir():
    with db.engine.begin() as conn:
        est, escalas = estado(conn)
        if est != "aplicada":
            print(f"La migración no está aplicada ({est}); no hay nada que revertir.")
            return
        objetos = dict(conn.execute(text(
            "SELECT objeto, sql FROM migracion_respaldo_objetos WHERE migracion = :m"), {"m": MIGRACION}).all())
        faltan = {"v_articulos", "v_ventas_analitica", "fn_aplicar_venta", "fn_revertir_venta",
                  "fn_editar_venta", "trg_revertir_venta", "trg_editar_venta"} - set(objetos)
        if faltan:
            print(f"*** Faltan definiciones guardadas: {faltan}. Restaurar desde el respaldo. ***")
            sys.exit(1)
        stock_antes = conn.execute(text(HUELLAS["stock"])).scalar()
        conn.execute(text("DROP VIEW v_ventas_analitica"))
        conn.execute(text("DROP VIEW v_articulos"))
        conn.execute(text("DROP TRIGGER trg_revertir_venta ON ventas"))
        for fn in ("fn_aplicar_venta", "fn_revertir_venta", "fn_editar_venta"):
            conn.execute(text(objetos[fn]))
        conn.execute(text(objetos["trg_revertir_venta"]))
        conn.execute(text("DROP FUNCTION IF EXISTS fn_devolver_stock_venta(INTEGER, INTEGER, NUMERIC)"))
        conn.execute(text("DROP TABLE venta_componentes"))
        conn.execute(text("DROP TRIGGER trg_editar_venta ON ventas"))
        for tabla, columna in COLUMNAS:
            conn.execute(text(f"ALTER TABLE {tabla} ALTER COLUMN {columna} TYPE NUMERIC(12,2)"))
        conn.execute(text(objetos["trg_editar_venta"]))
        conn.execute(text(objetos["v_articulos"]))
        conn.execute(text(objetos["v_ventas_analitica"]))
        conn.execute(text("DELETE FROM migracion_respaldo_objetos WHERE migracion = :m"), {"m": MIGRACION})
        stock_despues = conn.execute(text(HUELLAS["stock"])).scalar()
        print(f"Revertida. Stock antes/después: {stock_antes} / {stock_despues} "
              "(puede cambiar en el 3er decimal: las cantidades vuelven a 2 decimales).")


def main():
    url = db.engine.url
    print(f"Conectando a: host={url.host!r} db={url.database!r} user={url.username!r}")
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        print(f"\n*** No se pudo conectar a la base: {type(e).__name__}: {e} ***")
        sys.exit(1)
    try:
        if "--revertir" in sys.argv:
            revertir()
        else:
            migrar()
    except Exception as e:
        detalle = str(getattr(e, "orig", e)).strip().splitlines()[0]
        print(f"\n*** Falló: {type(e).__name__}: {detalle} ***")
        print("*** La transacción se deshizo: la base quedó EXACTAMENTE como estaba. ***")
        sys.exit(1)


if __name__ == "__main__":
    main()
