"""
Migración única (10/2026): casillero "¿actualiza precio?" por ítem de
factura + redondeo automático del precio de venta a la centena.

Hasta ahora, cargar una factura SIEMPRE actualizaba costo_compra_actual
del artículo (y de rebote su precio de venta, vía el margen) con el
costo_unitario de esa línea. Eso está bien para el caso normal, pero no
sirve cuando la compra fue puntual -- un descuento que te hizo el
proveedor una sola vez, una oferta de una sola factura -- porque ese
costo no es el costo "de siempre" del artículo: actualizarlo igual deja
el precio de venta mal calculado para la próxima compra normal.

Esta migración:

  1. Agrega la columna detalle_factura.actualiza_precio (BOOLEAN NOT
     NULL DEFAULT TRUE). Todo lo ya cargado queda en TRUE (se comporta
     exactamente igual que antes) -- el casillero nuevo en "Cargar
     Factura" es lo único que puede dejarlo en FALSE de acá en más.

  2. Reemplaza fn_aplicar_compra: el stock sigue subiendo siempre, pero
     costo_compra_actual y el costo vigente por proveedor
     (articulo_proveedor_precio) solo se actualizan cuando
     actualiza_precio = TRUE.

El reajuste de margen para que el precio final quede en un múltiplo de
$100 (evitar vuelto chico) se hace en Python, en db.crear_factura --
no requiere cambios de esquema, por eso no está en esta migración.

Se puede correr más de una vez sin romper nada.

Se corre localmente, igual que las migraciones anteriores:

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python migracion_actualiza_precio_factura.py
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


def paso_1_columna_actualiza_precio():
    print("=" * 60)
    print("PASO 1: columna detalle_factura.actualiza_precio")
    print("=" * 60)
    with db.engine.begin() as conn:
        fila = conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'detalle_factura' AND column_name = 'actualiza_precio'
        """)).first()

        if fila:
            print("detalle_factura ya tiene la columna actualiza_precio. Salteado.\n")
        else:
            conn.execute(text("""
                ALTER TABLE detalle_factura
                ADD COLUMN actualiza_precio BOOLEAN NOT NULL DEFAULT TRUE
            """))
            print("Columna agregada (todo lo ya cargado queda en TRUE).\n")


def paso_2_trigger_condicional():
    print("=" * 60)
    print("PASO 2: fn_aplicar_compra respeta actualiza_precio")
    print("=" * 60)
    with db.engine.begin() as conn:
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION fn_aplicar_compra() RETURNS TRIGGER AS $$
            DECLARE
                v_proveedor_id INTEGER;
            BEGIN
                UPDATE articulos
                SET stock_actual = stock_actual + NEW.cantidad,
                    actualizado_en = now()
                WHERE id = NEW.articulo_id;

                IF NEW.actualiza_precio THEN
                    UPDATE articulos
                    SET costo_compra_actual = NEW.costo_unitario,
                        actualizado_en = now()
                    WHERE id = NEW.articulo_id;

                    SELECT proveedor_id INTO v_proveedor_id FROM facturas WHERE id = NEW.factura_id;

                    INSERT INTO articulo_proveedor_precio (articulo_id, proveedor_id, costo_compra, actualizado_en)
                    VALUES (NEW.articulo_id, v_proveedor_id, NEW.costo_unitario, now())
                    ON CONFLICT (articulo_id, proveedor_id)
                    DO UPDATE SET costo_compra = EXCLUDED.costo_compra, actualizado_en = now();
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))
        print("fn_aplicar_compra actualizada.\n")


def main():
    probar_conexion()
    paso_1_columna_actualiza_precio()
    paso_2_trigger_condicional()
    print("=" * 60)
    print("Migración completa.")
    print("=" * 60)


if __name__ == "__main__":
    main()
