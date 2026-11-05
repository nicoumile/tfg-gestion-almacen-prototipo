-- ============================================================
-- Migración: Fraccionamiento (abrir bolsa -> producto suelto)
--            + venta por fracción directa (peso_bolsa_gramos)
-- Ejecutar UNA sola vez contra la conexión DIRECTA de Neon (sin -pooler).
-- ============================================================

-- Columnas nuevas en articulos
ALTER TABLE articulos ADD COLUMN articulo_fraccion_de INTEGER REFERENCES articulos(id);
ALTER TABLE articulos ADD COLUMN factor_fraccion NUMERIC(12,4);
ALTER TABLE articulos ADD COLUMN peso_bolsa_gramos NUMERIC(12,2);

-- Recrear la vista para exponer las columnas nuevas
DROP VIEW v_articulos;

CREATE VIEW v_articulos AS
SELECT
    a.id,
    a.nombre,
    a.marca,
    a.categoria,
    a.costo_compra_actual,
    ROUND(a.costo_compra_actual * (1 + a.margen_ganancia), 2) AS costo_venta_sin_iva,
    ROUND(a.costo_compra_actual * (1 + a.margen_ganancia) * (1 + a.iva), 2) AS costo_venta_con_iva,
    a.margen_ganancia * 100 AS porcentaje_ganancia,
    a.stock_actual,
    a.stock_minimo,
    a.fecha_vencimiento,
    a.acceso_rapido,
    a.unidad_medida,
    a.articulo_fraccion_de,
    a.factor_fraccion,
    a.peso_bolsa_gramos,
    CASE WHEN a.stock_actual <= a.stock_minimo THEN TRUE ELSE FALSE END AS stock_bajo,
    CASE WHEN a.fecha_vencimiento IS NOT NULL
              AND a.fecha_vencimiento <= (CURRENT_DATE + INTERVAL '15 days')
         THEN TRUE ELSE FALSE END AS vencimiento_cercano
FROM articulos a;

-- Tabla de auditoría de fraccionamientos
CREATE TABLE fraccionamientos (
    id SERIAL PRIMARY KEY,
    articulo_bolsa_id INTEGER NOT NULL REFERENCES articulos(id),
    articulo_derivado_id INTEGER NOT NULL REFERENCES articulos(id),
    cantidad_bolsas NUMERIC(12,2) NOT NULL CHECK (cantidad_bolsas > 0),
    cantidad_generada NUMERIC(12,4) NOT NULL CHECK (cantidad_generada > 0),
    costo_derivado_resultante NUMERIC(12,4) NOT NULL CHECK (costo_derivado_resultante >= 0),
    fecha DATE NOT NULL DEFAULT CURRENT_DATE,
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);

-- Trigger: al insertar un fraccionamiento, mueve stock y costo
CREATE OR REPLACE FUNCTION fn_aplicar_fraccionamiento() RETURNS TRIGGER AS $$
BEGIN
    UPDATE articulos
    SET stock_actual = stock_actual - NEW.cantidad_bolsas,
        actualizado_en = now()
    WHERE id = NEW.articulo_bolsa_id;

    UPDATE articulos
    SET stock_actual = stock_actual + NEW.cantidad_generada,
        costo_compra_actual = NEW.costo_derivado_resultante,
        actualizado_en = now()
    WHERE id = NEW.articulo_derivado_id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_aplicar_fraccionamiento
AFTER INSERT ON fraccionamientos
FOR EACH ROW EXECUTE FUNCTION fn_aplicar_fraccionamiento();

-- Trigger: al eliminar un fraccionamiento, revierte el movimiento
CREATE OR REPLACE FUNCTION fn_revertir_fraccionamiento() RETURNS TRIGGER AS $$
BEGIN
    UPDATE articulos
    SET stock_actual = stock_actual + OLD.cantidad_bolsas,
        actualizado_en = now()
    WHERE id = OLD.articulo_bolsa_id;

    UPDATE articulos
    SET stock_actual = stock_actual - OLD.cantidad_generada,
        actualizado_en = now()
    WHERE id = OLD.articulo_derivado_id;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_revertir_fraccionamiento
AFTER DELETE ON fraccionamientos
FOR EACH ROW EXECUTE FUNCTION fn_revertir_fraccionamiento();
