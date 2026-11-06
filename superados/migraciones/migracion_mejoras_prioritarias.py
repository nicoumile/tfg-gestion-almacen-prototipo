"""
Migración única para las 6 mejoras prioritarias (30/09/2026):

1. Deja constancia en el historial de cualquier artículo con stock
   negativo ya existente (no lo corrige ni lo bloquea -- es un estado
   válido: se vendió algo cuya factura de compra todavía no se cargó, y
   se ajusta solo cuando esa factura entra). Si una versión anterior de
   este mismo script ya había agregado una restricción que lo impedía, la
   saca.
2. Agrega la columna ventas.es_consumo_interno.
3. Crea la tabla promocion_componentes (Promociones).
4. Crea la tabla historial_cambios (auditoría liviana).
5. Crea la tabla articulo_proveedor_precio + vista v_comparacion_proveedores
   y actualiza el trigger fn_aplicar_compra para que la alimente sola
   (multi-proveedor por artículo). No hace falta backfill: a partir de
   la primera factura nueva que cargues por proveedor, se va completando.

Se puede correr más de una vez sin romper nada: cada paso se fija si ya
está aplicado antes de intentarlo de nuevo.

Se corre localmente (mismo patrón que los scripts anteriores):

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python superados/migraciones/migracion_mejoras_prioritarias.py
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


def tabla_existe(conn, tabla):
    r = conn.execute(text("""
        SELECT 1 FROM information_schema.tables WHERE table_name = :t
    """), {"t": tabla}).first()
    return r is not None


def constraint_existe(conn, nombre):
    r = conn.execute(text("""
        SELECT 1 FROM pg_constraint WHERE conname = :n
    """), {"n": nombre}).first()
    return r is not None


def paso_1_diagnosticar_stock_negativo():
    print("=" * 60)
    print("PASO 1: stock negativo")
    print("=" * 60)
    with db.engine.begin() as conn:
        # Si una corrida anterior de este script llegó a agregar la
        # restricción que bloqueaba el stock negativo, se saca: se decidió
        # que el stock SÍ puede quedar negativo (pendiente de que se cargue
        # la factura de compra correspondiente).
        if constraint_existe(conn, "chk_stock_no_negativo"):
            conn.execute(text("ALTER TABLE articulos DROP CONSTRAINT chk_stock_no_negativo"))
            print("Se sacó la restricción chk_stock_no_negativo (el stock negativo ahora es un estado válido).\n")

        negativos = conn.execute(text(
            "SELECT id, nombre, stock_actual FROM articulos WHERE stock_actual < 0 ORDER BY stock_actual"
        )).mappings().all()

        if not negativos:
            print("No hay artículos con stock negativo por ahora.\n")
        else:
            print(f"Encontrados {len(negativos)} artículo(s) con stock negativo (quedan como están):")
            for a in negativos:
                print(f"  - [{a['id']}] {a['nombre']}: {a['stock_actual']}")
                conn.execute(text("""
                    INSERT INTO historial_cambios (tabla, registro_id, accion, detalle)
                    VALUES ('articulos', :id, 'STOCK_NEGATIVO', :detalle)
                """), {
                    "id": a["id"],
                    "detalle": f"Stock en {a['stock_actual']} (detectado en la migración; pendiente de ajustar con la próxima factura)"
                })
            print("\nQuedan registrados en 'Historial de Cambios' para hacerles seguimiento hasta que se ajusten.\n")


def paso_2_consumo_interno():
    print("=" * 60)
    print("PASO 2: consumo interno")
    print("=" * 60)
    with db.engine.begin() as conn:
        if columna_existe(conn, "ventas", "es_consumo_interno"):
            print("La columna ventas.es_consumo_interno ya existe. Salteado.\n")
        else:
            conn.execute(text(
                "ALTER TABLE ventas ADD COLUMN es_consumo_interno BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            print("Columna ventas.es_consumo_interno agregada.\n")


def paso_3_historial_cambios():
    print("=" * 60)
    print("PASO 3: historial de cambios")
    print("=" * 60)
    with db.engine.begin() as conn:
        if tabla_existe(conn, "historial_cambios"):
            print("La tabla historial_cambios ya existe. Salteado.\n")
        else:
            conn.execute(text("""
                CREATE TABLE historial_cambios (
                    id SERIAL PRIMARY KEY,
                    fecha TIMESTAMP NOT NULL DEFAULT now(),
                    tabla TEXT NOT NULL,
                    registro_id INTEGER,
                    accion TEXT NOT NULL,
                    detalle TEXT NOT NULL
                )
            """))
            conn.execute(text(
                "CREATE INDEX idx_historial_fecha ON historial_cambios (fecha DESC)"
            ))
            print("Tabla historial_cambios creada.\n")


def paso_4_promociones():
    print("=" * 60)
    print("PASO 4: promociones")
    print("=" * 60)
    with db.engine.begin() as conn:
        if tabla_existe(conn, "promocion_componentes"):
            print("La tabla promocion_componentes ya existe. Salteado.\n")
        else:
            conn.execute(text("""
                CREATE TABLE promocion_componentes (
                    id SERIAL PRIMARY KEY,
                    promocion_articulo_id INTEGER NOT NULL REFERENCES articulos(id) ON DELETE CASCADE,
                    componente_articulo_id INTEGER NOT NULL REFERENCES articulos(id),
                    cantidad_por_unidad NUMERIC(12,4) NOT NULL CHECK (cantidad_por_unidad > 0),
                    UNIQUE (promocion_articulo_id, componente_articulo_id)
                )
            """))
            print("Tabla promocion_componentes creada.\n")


def paso_5_multiproveedor():
    print("=" * 60)
    print("PASO 5: multi-proveedor por artículo")
    print("=" * 60)
    with db.engine.begin() as conn:
        if tabla_existe(conn, "articulo_proveedor_precio"):
            print("La tabla articulo_proveedor_precio ya existe. Salteado.")
        else:
            conn.execute(text("""
                CREATE TABLE articulo_proveedor_precio (
                    id SERIAL PRIMARY KEY,
                    articulo_id INTEGER NOT NULL REFERENCES articulos(id) ON DELETE CASCADE,
                    proveedor_id INTEGER NOT NULL REFERENCES proveedores(id) ON DELETE CASCADE,
                    costo_compra NUMERIC(12,2) NOT NULL CHECK (costo_compra >= 0),
                    actualizado_en TIMESTAMP NOT NULL DEFAULT now(),
                    UNIQUE (articulo_id, proveedor_id)
                )
            """))
            print("Tabla articulo_proveedor_precio creada.")

        # Backfill: reconstruye el costo vigente por proveedor a partir del
        # historial de facturas ya cargado, para no arrancar vacío.
        conn.execute(text("""
            INSERT INTO articulo_proveedor_precio (articulo_id, proveedor_id, costo_compra, actualizado_en)
            SELECT DISTINCT ON (df.articulo_id, f.proveedor_id)
                df.articulo_id, f.proveedor_id, df.costo_unitario, f.fecha
            FROM detalle_factura df
            JOIN facturas f ON f.id = df.factura_id
            ORDER BY df.articulo_id, f.proveedor_id, f.fecha DESC, df.id DESC
            ON CONFLICT (articulo_id, proveedor_id) DO UPDATE
                SET costo_compra = EXCLUDED.costo_compra, actualizado_en = EXCLUDED.actualizado_en
        """))
        print("Backfill de precios por proveedor aplicado desde el historial de facturas.")

        conn.execute(text("""
            CREATE OR REPLACE FUNCTION fn_aplicar_compra() RETURNS TRIGGER AS $$
            DECLARE
                v_proveedor_id INTEGER;
            BEGIN
                UPDATE articulos
                SET stock_actual = stock_actual + NEW.cantidad,
                    costo_compra_actual = NEW.costo_unitario,
                    actualizado_en = now()
                WHERE id = NEW.articulo_id;

                SELECT proveedor_id INTO v_proveedor_id FROM facturas WHERE id = NEW.factura_id;

                INSERT INTO articulo_proveedor_precio (articulo_id, proveedor_id, costo_compra, actualizado_en)
                VALUES (NEW.articulo_id, v_proveedor_id, NEW.costo_unitario, now())
                ON CONFLICT (articulo_id, proveedor_id)
                DO UPDATE SET costo_compra = EXCLUDED.costo_compra, actualizado_en = now();

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))
        print("Trigger fn_aplicar_compra actualizado: de ahora en más registra el costo por proveedor solo.")

        conn.execute(text("DROP VIEW IF EXISTS v_comparacion_proveedores"))
        conn.execute(text("""
            CREATE VIEW v_comparacion_proveedores AS
            SELECT
                app.articulo_id,
                a.nombre AS articulo,
                app.proveedor_id,
                p.nombre AS proveedor,
                app.costo_compra,
                app.actualizado_en,
                (app.costo_compra = MIN(app.costo_compra) OVER (PARTITION BY app.articulo_id)) AS es_mas_barato,
                COUNT(*) OVER (PARTITION BY app.articulo_id) AS cantidad_proveedores
            FROM articulo_proveedor_precio app
            JOIN articulos a ON a.id = app.articulo_id
            JOIN proveedores p ON p.id = app.proveedor_id
        """))
        print("Vista v_comparacion_proveedores creada.\n")


def main():
    probar_conexion()
    paso_3_historial_cambios()  # antes que el paso 1, que ya escribe ahí
    paso_1_diagnosticar_stock_negativo()
    paso_2_consumo_interno()
    paso_4_promociones()
    paso_5_multiproveedor()
    print("=" * 60)
    print("Migración completa.")
    print("=" * 60)


if __name__ == "__main__":
    main()
