-- ============================================================
-- Schema PostgreSQL - Sistema de Gestión de Almacén
-- ============================================================

-- Extensión para búsquedas de texto difusas (matching de artículos
-- entre nombres distintos de proveedores). Se usa más adelante en
-- el Hito 5, pero la dejamos habilitada desde ya.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ------------------------------------------------------------
-- PROVEEDORES
-- ------------------------------------------------------------
CREATE TABLE proveedores (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    cuit TEXT UNIQUE,
    telefono TEXT,
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- ARTICULOS
-- ------------------------------------------------------------
CREATE TABLE articulos (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    marca TEXT,
    categoria TEXT,
    costo_compra_actual NUMERIC(12,2) NOT NULL DEFAULT 0,
    -- NUMERIC(10,8): mucha más precisión que el % que se muestra (2
    -- decimales) a propósito -- así, cuando se fija un precio de venta
    -- "redondo" y se despeja el margen necesario para llegar a él, ese
    -- margen se guarda con la precisión suficiente para que el precio
    -- recalculado dé exacto, en vez de quedar unos centavos arriba o
    -- abajo por el redondeo del margen (ver app.py, _recalcular_margen).
    margen_ganancia NUMERIC(10,8) NOT NULL DEFAULT 0.30,  -- 0.30 = 30%
    iva NUMERIC(5,4) NOT NULL DEFAULT 0.21,               -- 21% por defecto
    stock_actual NUMERIC(12,2) NOT NULL DEFAULT 0,
    stock_minimo NUMERIC(12,2) NOT NULL DEFAULT 0,
    fecha_vencimiento DATE,
    acceso_rapido BOOLEAN NOT NULL DEFAULT FALSE, -- sin uso actualmente (ver app.py)
    -- 'unidad': se compra y vende en unidades enteras (una lata, un paquete).
    -- 'kg': producto fraccionable (fiambres, quesos) — se compra y vende en
    -- kilogramos, permitiendo cantidades como 0.5 (500g). El costo y precio
    -- ya calculados en v_articulos quedan expresados "por kg" en ese caso.
    unidad_medida TEXT NOT NULL DEFAULT 'unidad' CHECK (unidad_medida IN ('unidad', 'kg')),
    -- Fraccionamiento (pantalla "Fraccionar"): si este artículo es el
    -- producto "suelto" que sale de abrir otro artículo (una bolsa cerrada),
    -- articulo_fraccion_de apunta a ese artículo "padre" y factor_fraccion
    -- indica cuánto rinde UNA unidad del padre en este producto (kg por
    -- bolsa si unidad_medida='kg', o paquetitos por bolsa si ='unidad').
    articulo_fraccion_de INTEGER REFERENCES articulos(id),
    factor_fraccion NUMERIC(12,4),
    -- Peso de una unidad/bolsa de ESTE artículo, en gramos. Si está cargado
    -- (solo tiene sentido con unidad_medida = 'unidad'), permite vender una
    -- fracción de peso directamente en "Registrar Venta" sin pasar por
    -- "Fraccionar" ni crear un producto derivado.
    peso_bolsa_gramos NUMERIC(12,2),
    -- Tipo de promoción (solo aplica si categoria = 'Promocion'):
    -- 'fisica'  = se arma/renueva a mano (Promociones -> Armar/Renovar),
    --             tiene su propio stock_actual, igual que cualquier artículo.
    -- 'virtual' = "combo": no se arma, se vende directo y el stock se
    --             calcula en vivo a partir del stock real de sus
    --             componentes (ver v_articulos). Vender un combo
    --             descuenta el stock de los artículos originales, no el
    --             de la promoción -- comparten el mismo contador.
    promo_tipo TEXT CHECK (promo_tipo IN ('fisica', 'virtual')),
    -- Baja lógica (14/10/2026, Artículos -> Editar artículo): un artículo
    -- que no se trabaja más NO se borra (tiene ventas y compras que
    -- alimentan el ABC, los márgenes y el historial), se marca inactivo.
    -- Deja de aparecer en venta, compras, promociones y reposición, y sale
    -- del indicador de calidad salvo que le quede stock (ver db.py).
    -- reemplazado_por: el artículo que lo sustituye (cambio de
    -- presentación, de marca, etc.); la reposición del nuevo suma la
    -- venta reciente del viejo para no arrancar sin historial.
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    fecha_baja DATE,
    motivo_baja TEXT CHECK (motivo_baja IN ('discontinuado', 'reemplazado', 'temporal')),
    reemplazado_por INTEGER REFERENCES articulos(id),
    creado_en TIMESTAMP NOT NULL DEFAULT now(),
    actualizado_en TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX idx_articulos_nombre_trgm ON articulos USING gin (nombre gin_trgm_ops);

-- ------------------------------------------------------------
-- PROMOCIONES
-- Cada promoción se guarda como un artículo más (categoria = 'Promocion'),
-- para venderse exactamente igual que cualquier producto: mismo buscador,
-- mismo carrito, mismo trigger de stock. Esta tabla define de qué
-- artículos y en qué cantidad está armada cada promoción. Se crea acá
-- (y no más abajo, junto al resto de las mejoras) porque v_articulos ya
-- la necesita para calcular el stock de los combos 'virtual'.
-- ------------------------------------------------------------
CREATE TABLE promocion_componentes (
    id SERIAL PRIMARY KEY,
    promocion_articulo_id INTEGER NOT NULL REFERENCES articulos(id) ON DELETE CASCADE,
    componente_articulo_id INTEGER NOT NULL REFERENCES articulos(id),
    cantidad_por_unidad NUMERIC(12,4) NOT NULL CHECK (cantidad_por_unidad > 0),
    UNIQUE (promocion_articulo_id, componente_articulo_id)
);

-- Costo de venta y % de ganancia se calculan siempre a partir del costo
-- de compra y el margen -> los exponemos como vista para no duplicar datos.
-- Para una promoción 'virtual' (combo de stock compartido), stock_actual
-- NO es la columna cruda de articulos (esa queda sin uso, siempre en 0):
-- se recalcula en vivo como el mínimo de "stock disponible / cantidad que
-- necesita" entre sus componentes -- es decir, cuántos combos se pueden
-- vender ahora mismo con el stock real de los artículos originales.
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
    sub.iva,
    sub.activo,
    sub.fecha_baja,
    sub.motivo_baja,
    sub.reemplazado_por
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
        a.articulo_fraccion_de, a.factor_fraccion, a.peso_bolsa_gramos, a.promo_tipo,
        a.activo, a.fecha_baja, a.motivo_baja, a.reemplazado_por
    FROM articulos a
    LEFT JOIN (
        SELECT pc.promocion_articulo_id,
               FLOOR(MIN(comp.stock_actual / pc.cantidad_por_unidad)) AS stock_virtual
        FROM promocion_componentes pc
        JOIN articulos comp ON comp.id = pc.componente_articulo_id
        GROUP BY pc.promocion_articulo_id
    ) vp ON vp.promocion_articulo_id = a.id
) sub;

-- ------------------------------------------------------------
-- ALIAS DE ARTICULOS POR PROVEEDOR
-- Un mismo producto puede figurar con nombres distintos según el
-- proveedor que lo facture (Hito 5). Cada vez que confirmamos manualmente
-- que "tal nombre de la factura" corresponde a "tal artículo interno",
-- lo guardamos acá para reconocerlo automáticamente la próxima vez.
-- ------------------------------------------------------------
CREATE TABLE articulo_alias (
    id SERIAL PRIMARY KEY,
    articulo_id INTEGER NOT NULL REFERENCES articulos(id) ON DELETE CASCADE,
    proveedor_id INTEGER NOT NULL REFERENCES proveedores(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    -- Cuántas unidades del artículo del catálogo representa UNA unidad tal
    -- como la factura el proveedor (ej. caja de 60 bocaditos que se venden
    -- sueltos -> 60). 1 = misma unidad. Agregada 10/2026 para el importador
    -- de facturas desde la planilla, ver migracion_importador_facturas.py.
    factor NUMERIC(12,4) NOT NULL DEFAULT 1 CONSTRAINT articulo_alias_factor_positivo CHECK (factor > 0),
    creado_en TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (proveedor_id, alias)
);

CREATE INDEX idx_alias_trgm ON articulo_alias USING gin (alias gin_trgm_ops);

-- ------------------------------------------------------------
-- FACTURAS (compras)
-- ------------------------------------------------------------
CREATE TABLE facturas (
    id SERIAL PRIMARY KEY,
    proveedor_id INTEGER NOT NULL REFERENCES proveedores(id),
    numero TEXT,
    fecha DATE NOT NULL DEFAULT CURRENT_DATE,
    archivo_original TEXT,   -- path o nombre del archivo escaneado/foto/pdf
    origen TEXT NOT NULL DEFAULT 'manual', -- 'manual' | 'ocr' | 'llm'
    -- Identificador en el sistema de origen cuando la factura se importó
    -- (ej. 'planilla:C-00012' = comprobante de la planilla del lector
    -- automático). Único: evita importar dos veces la misma factura.
    -- Agregada 10/2026, ver migracion_importador_facturas.py.
    ref_externa TEXT,
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_facturas_ref_externa ON facturas (ref_externa) WHERE ref_externa IS NOT NULL;

CREATE TABLE detalle_factura (
    id SERIAL PRIMARY KEY,
    factura_id INTEGER NOT NULL REFERENCES facturas(id) ON DELETE CASCADE,
    articulo_id INTEGER NOT NULL REFERENCES articulos(id),
    cantidad NUMERIC(12,2) NOT NULL CHECK (cantidad > 0),
    costo_unitario NUMERIC(12,2) NOT NULL CHECK (costo_unitario >= 0),
    -- TRUE (default): esta línea actualiza el costo de referencia del
    -- artículo y su precio de venta (el caso normal). FALSE: fue una
    -- compra puntual (descuento del proveedor, oferta única) -- sube
    -- stock y queda el costo real pagado en esta factura, pero no toca
    -- costo_compra_actual ni el precio de venta del artículo. Agregada
    -- 10/2026, ver migracion_actualiza_precio_factura.py.
    actualiza_precio BOOLEAN NOT NULL DEFAULT TRUE,
    -- Tanda 18: fecha de vencimiento real del renglón (opcional). Vacía = se
    -- estima como fecha de la factura + días disponibles para la venta
    -- (articulos.vida_util_dias, o vida_util_categoria). Ver db.lotes_vencimiento.
    fecha_vencimiento DATE
);

-- ------------------------------------------------------------
-- TICKET DE VENTA
-- Agrupa varios ítems cargados juntos en el mismo "carrito" (ej: el
-- cliente se lleva fideos + aceite + 300g de queso en una sola compra).
-- Es solo agrupación para poder mostrar/editar el ticket completo; cada
-- ítem sigue siendo una fila individual en "ventas" con su propio efecto
-- de stock, así no se rompe nada de lo que ya funciona con ventas sueltas.
-- ------------------------------------------------------------
CREATE TABLE ventas_ticket (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL DEFAULT CURRENT_DATE,
    medio_pago TEXT NOT NULL DEFAULT 'Efectivo',
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- VENTAS
-- Cada fila es la venta de un artículo puntual: articulo_id, cantidad y
-- precio_unitario siempre completos. Baja stock automáticamente.
-- (Hasta 10/2026 existía también una "venta genérica" sin producto, con
-- esos tres campos en NULL; se dio de baja porque dejaba ventas fuera del
-- análisis -- ver migracion_quitar_venta_generica.py.)
-- ticket_id agrupa varios ítems cargados en el mismo carrito (opcional).
-- ------------------------------------------------------------
CREATE TABLE ventas (
    id SERIAL PRIMARY KEY,
    articulo_id INTEGER NOT NULL REFERENCES articulos(id),
    ticket_id INTEGER REFERENCES ventas_ticket(id),
    fecha DATE NOT NULL DEFAULT CURRENT_DATE,
    cantidad NUMERIC(12,2) NOT NULL CHECK (cantidad > 0),
    precio_unitario NUMERIC(12,2) NOT NULL CHECK (precio_unitario >= 0),
    monto_total NUMERIC(12,2) NOT NULL CHECK (monto_total >= 0),
    medio_pago TEXT NOT NULL DEFAULT 'Efectivo',
    -- Costo unitario de compra vigente AL MOMENTO de la venta (lo completa
    -- trg_costo_en_venta). Permite calcular el margen real de cada venta
    -- aunque después el costo del artículo cambie. NULL en ventas viejas,
    -- anteriores a 12/10/2026: ahí se usa el costo actual.
    costo_unitario NUMERIC(12,2),
    -- Precio de lista (con IVA) vigente AL MOMENTO de la venta (lo completa
    -- trg_costo_en_venta). Permite controlar si se cobró el precio del
    -- sistema de ese día aunque después el precio cambie. NULL en ventas
    -- anteriores a 13/10/2026.
    precio_lista NUMERIC(12,2),
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- GASTOS (no relacionados a mercadería: alquiler, luz, sueldos, etc.)
-- ------------------------------------------------------------
CREATE TABLE gastos (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL DEFAULT CURRENT_DATE,
    categoria TEXT NOT NULL,
    descripcion TEXT,
    monto NUMERIC(12,2) NOT NULL CHECK (monto >= 0)
);

-- ------------------------------------------------------------
-- FRACCIONAMIENTOS
-- Auditoría de cada vez que se "abre" una bolsa cerrada (articulo_bolsa_id)
-- para generar stock de su producto suelto vinculado (articulo_derivado_id,
-- ver articulos.articulo_fraccion_de). costo_derivado_resultante queda
-- grabado con el costo que tenía el derivado en ESE momento (costo de la
-- bolsa en ese momento ÷ factor_fraccion), para no perder esa foto aunque
-- el costo de la bolsa cambie después.
-- ------------------------------------------------------------
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

-- ============================================================
-- TRIGGERS: actualización automática de stock y costo
-- ============================================================

-- Al insertar un detalle de factura (compra): sube stock y, si el renglón
-- actualiza precio (lo normal), el costo de compra vigente del artículo y
-- el costo de ese proveedor. Única definición (antes había una versión
-- vieja acá, pisada más abajo por esta).
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

CREATE TRIGGER trg_aplicar_compra
AFTER INSERT ON detalle_factura
FOR EACH ROW EXECUTE FUNCTION fn_aplicar_compra();

-- Al insertar una venta: baja el stock del artículo vendido.
-- Si el artículo vendido es una promoción 'virtual' (combo de stock
-- compartido), el movimiento de stock no se aplica sobre la promoción
-- (no tiene stock propio) sino sobre cada uno de sus componentes, en
-- proporción a cuánto de cada uno lleva el combo.
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

CREATE TRIGGER trg_aplicar_venta
AFTER INSERT ON ventas
FOR EACH ROW EXECUTE FUNCTION fn_aplicar_venta();

-- ------------------------------------------------------------
-- Reversión de stock al eliminar una compra (item de factura).
-- Permite borrar una factura completa (o un ítem) sin descuadrar el
-- stock: resta lo que esa línea había sumado.
-- Nota: no revierte el costo_compra_actual del artículo, porque no
-- sabemos cuál era el costo vigente justo antes de esa compra sin
-- recorrer todo el historial. Si se borra la última compra de un
-- artículo, conviene revisar/actualizar su costo manualmente.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_revertir_compra() RETURNS TRIGGER AS $$
BEGIN
    UPDATE articulos
    SET stock_actual = stock_actual - OLD.cantidad,
        actualizado_en = now()
    WHERE id = OLD.articulo_id;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_revertir_compra
AFTER DELETE ON detalle_factura
FOR EACH ROW EXECUTE FUNCTION fn_revertir_compra();

-- ------------------------------------------------------------
-- Reversión de stock al eliminar una venta (si tenía producto asociado).
-- ------------------------------------------------------------
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

CREATE TRIGGER trg_revertir_venta
AFTER DELETE ON ventas
FOR EACH ROW EXECUTE FUNCTION fn_revertir_venta();

-- ------------------------------------------------------------
-- Ajuste de stock al editar una venta (cambia cantidad y/o artículo,
-- y/o artículo).
-- ------------------------------------------------------------
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

CREATE TRIGGER trg_editar_venta
AFTER UPDATE ON ventas
FOR EACH ROW
WHEN (OLD.cantidad IS DISTINCT FROM NEW.cantidad OR OLD.articulo_id IS DISTINCT FROM NEW.articulo_id)
EXECUTE FUNCTION fn_editar_venta();

-- ------------------------------------------------------------
-- Al insertar un fraccionamiento ("abrir" bolsas): resta del stock de la
-- bolsa, suma al stock del derivado y deja el costo del derivado ya
-- recalculado. cantidad_generada y costo_derivado_resultante los calcula
-- la app (db.py) antes de insertar, a partir del costo vigente de la
-- bolsa y el factor_fraccion del derivado — el trigger solo aplica el
-- movimiento, igual que fn_aplicar_compra con costo_unitario.
-- ------------------------------------------------------------
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

-- ------------------------------------------------------------
-- Reversión al eliminar un fraccionamiento: repone stock de la bolsa y
-- resta el stock que se había generado en el derivado. Igual que con
-- trg_revertir_compra, no revierte el costo_compra_actual del derivado
-- (si hacía falta, se ajusta a mano después de borrar).
-- ------------------------------------------------------------
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

-- ============================================================
-- VISTA: historial de precios de compra por artículo/proveedor,
-- con el costo anterior para detectar variaciones (Hito de análisis).
-- ============================================================
CREATE VIEW v_historial_precios AS
SELECT
    df.id,
    a.id AS articulo_id,
    a.nombre AS articulo,
    p.nombre AS proveedor,
    f.fecha,
    df.costo_unitario,
    LAG(df.costo_unitario) OVER (
        PARTITION BY a.id, f.proveedor_id ORDER BY f.fecha, df.id
    ) AS costo_anterior,
    ROUND(
        100.0 * (df.costo_unitario - LAG(df.costo_unitario) OVER (
            PARTITION BY a.id, f.proveedor_id ORDER BY f.fecha, df.id
        )) / NULLIF(LAG(df.costo_unitario) OVER (
            PARTITION BY a.id, f.proveedor_id ORDER BY f.fecha, df.id
        ), 0)
    , 2) AS variacion_pct
FROM detalle_factura df
JOIN facturas f ON f.id = df.factura_id
JOIN articulos a ON a.id = df.articulo_id
JOIN proveedores p ON p.id = f.proveedor_id;

-- ============================================================
-- MEJORAS PRIORITARIAS (30/09/2026):
-- stock negativo tolerado (con aviso), consumo interno, promociones,
-- historial de cambios, multi-proveedor por artículo.
-- Aplicadas sobre una base ya cargada con migracion_mejoras_prioritarias.py.
-- ============================================================

-- Decisión: el stock SÍ puede quedar negativo (a propósito, no es un bug).
-- Pasa cuando un producto ya se vendió pero la factura de compra todavía
-- no se cargó -- el stock queda "pendiente de ajustar" hasta que se cargue
-- esa factura y se repone solo. Bloquearlo en la base sería más rígido de
-- lo que el negocio necesita. En vez de impedirlo, cada operación que deja
-- un artículo en negativo lo registra en historial_cambios (ver
-- db._registrar_si_stock_negativo) para que quede visible y se pueda
-- seguir -- no queda "invisible" como antes de esta mejora.

-- Venta de consumo interno: descuenta stock igual que una venta común,
-- pero no debe contarse como ingreso -- se registra en cambio un gasto
-- aparte con el costo real de lo consumido. monto_total queda en 0 en
-- estas filas (ver db.confirmar_consumo_interno).
ALTER TABLE ventas ADD COLUMN es_consumo_interno BOOLEAN NOT NULL DEFAULT FALSE;

-- (promocion_componentes ya se creó más arriba, junto a la tabla articulos)

-- ------------------------------------------------------------
-- HISTORIAL DE CAMBIOS (auditoría liviana)
-- Registro legible de ediciones sensibles: cambios de precio/margen,
-- correcciones de stock, eliminación de ventas, armado de promociones.
-- Se carga a propósito desde la propia app (db.py) y no por triggers,
-- para poder escribir el detalle en texto plano, legible, sin tener
-- que reconstruirlo después a partir de columnas sueltas.
-- ------------------------------------------------------------
CREATE TABLE historial_cambios (
    id SERIAL PRIMARY KEY,
    fecha TIMESTAMP NOT NULL DEFAULT now(),
    tabla TEXT NOT NULL,
    registro_id INTEGER,
    accion TEXT NOT NULL,
    detalle TEXT NOT NULL
);

CREATE INDEX idx_historial_fecha ON historial_cambios (fecha DESC);

-- ------------------------------------------------------------
-- MULTI-PROVEEDOR POR ARTICULO
-- Histórico del costo de compra vigente por cada artículo Y proveedor
-- (a diferencia de articulos.costo_compra_actual, que solo refleja al
-- último proveedor que lo facturó). Permite comparar precios entre
-- proveedores de un mismo producto.
-- ------------------------------------------------------------
CREATE TABLE articulo_proveedor_precio (
    id SERIAL PRIMARY KEY,
    articulo_id INTEGER NOT NULL REFERENCES articulos(id) ON DELETE CASCADE,
    proveedor_id INTEGER NOT NULL REFERENCES proveedores(id) ON DELETE CASCADE,
    costo_compra NUMERIC(12,2) NOT NULL CHECK (costo_compra >= 0),
    actualizado_en TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (articulo_id, proveedor_id)
);

-- fn_aplicar_compra ahora también deja el costo vigente por proveedor,
-- además de actualizar costo_compra_actual como ya hacía antes.
--
-- 10/2026: el stock sube SIEMPRE con la compra, pero el costo de
-- referencia (costo_compra_actual) y el costo vigente por proveedor
-- (articulo_proveedor_precio) solo se tocan cuando NEW.actualiza_precio
-- es TRUE -- una compra puntual (descuento del proveedor, oferta única)
-- se carga con actualiza_precio = FALSE y no corre esta parte: el
-- artículo sigue vendiéndose al costo/precio de siempre. El reajuste del
-- margen para que el precio final quede en un múltiplo de $100 (ver
-- claude/redondeo-100-y-actualiza-precio-factura.md) se hace aparte, en
-- Python (db.crear_factura), porque necesita leer margen/iva actuales y
-- clampear -- no vale la pena duplicar esa lógica en PL/pgSQL.
-- (fn_aplicar_compra: definida más arriba, junto a su trigger)

-- Comparación entre proveedores: para cada artículo con más de un
-- proveedor cargado, muestra el costo de cada uno y cuál es el más barato.
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
JOIN proveedores p ON p.id = app.proveedor_id;

-- ============================================================
-- SEGUNDA RONDA (01/10/2026): promociones con stock compartido
-- ("combos virtuales") + vista analítica de ventas.
-- Aplicada sobre una base ya cargada con migracion_promociones_combo.py.
-- ============================================================

-- Una venta de promoción queda en 'ventas' como UNA sola fila (necesario
-- para que el ticket y el cierre de caja tengan sentido), pero eso deja
-- ciegos a los análisis que agrupan por artículo (clasificación ABC,
-- márgenes reales, pronóstico de demanda, núcleo analítico pendiente):
-- no ven que Coca y Fernet "se movieron" si solo se vendieron en combo.
--
-- Esta vista resuelve cada venta de promoción en sus componentes reales:
-- reparte el monto cobrado proporcionalmente al precio de venta
-- individual de cada componente (así no se infla ni se esconde la
-- facturación real de cada uno), y calcula el costo real de cada uno
-- por separado. Las ventas de artículos normales pasan tal cual.
--
-- Nota de timing: para una promoción 'fisica' (armado manual), el
-- stock de los componentes ya se descontó antes, al armar -- esta vista
-- solo reparte el INGRESO/COSTO de la venta entre esos componentes para
-- fines analíticos, no vuelve a tocar stock. Para una promoción
-- 'virtual' coinciden en el tiempo: la venta y el movimiento de stock
-- son el mismo evento.
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
JOIN pesos p ON p.venta_id = v.id AND p.componente_articulo_id = comp.id;

-- ------------------------------------------------------------
-- CALIDAD DE DATOS (12/10/2026)
-- Foto semanal del indicador de calidad de datos (Reportes -> Calidad de
-- datos). Una fila por semana; se pisa con el último valor calculado de
-- esa semana. db.py la crea sola si no existe (CREATE TABLE IF NOT
-- EXISTS), así que en una base ya existente no hace falta migración.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS snapshot_calidad (
    semana DATE PRIMARY KEY,               -- lunes de la semana
    indicador NUMERIC(5,2) NOT NULL,       -- % de artículos sin alertas
    articulos_activos INTEGER NOT NULL,
    articulos_con_alerta INTEGER NOT NULL,
    actualizado_en TIMESTAMP NOT NULL DEFAULT now(),
    -- 14/10/2026: artículos dados de baja esa semana (fuera del indicador).
    -- Permite distinguir una suba por depuración del catálogo de una suba
    -- por corrección de datos. NULL en las semanas anteriores.
    articulos_inactivos INTEGER
);

-- ------------------------------------------------------------
-- COSTO Y PRECIO DE LISTA DE CADA VENTA (12-13/10/2026)
-- Guarda en ventas.costo_unitario el costo de compra vigente en el
-- momento de vender (para un combo, la suma del costo de sus
-- componentes) y en ventas.precio_lista el precio de lista con IVA de
-- ese momento. Ver migracion_precio_lista_en_venta.py.
-- ------------------------------------------------------------
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
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_costo_en_venta
BEFORE INSERT OR UPDATE OF articulo_id ON ventas
FOR EACH ROW EXECUTE FUNCTION fn_costo_en_venta();

-- ------------------------------------------------------------
-- SPRINT 2 (10/2026): usuarios con contraseña y permisos por pantalla,
-- quién registró cada venta/cambio, parámetros de reposición, volumen
-- para la restricción de espacio y pronósticos guardados.
-- Mismo SQL que migracion_sprint2.py (en una base ya existente se aplica
-- con ese script, que además pide la contraseña inicial del dueño).
-- ------------------------------------------------------------
-- Tabla personas
CREATE TABLE IF NOT EXISTS personas (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    usuario TEXT NOT NULL,
    clave_hash TEXT,
    rol TEXT NOT NULL DEFAULT 'atencion' CHECK (rol IN ('dueño', 'atencion')),
    -- pantallas habilitadas (claves de app.PAGINAS); el dueño ve todo siempre
    permisos TEXT[] NOT NULL DEFAULT ARRAY['registrar_venta'],
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_personas_nombre ON personas (lower(nombre));
CREATE UNIQUE INDEX IF NOT EXISTS idx_personas_usuario ON personas (lower(usuario));
INSERT INTO personas (nombre, usuario, rol)
SELECT 'Dueño', 'dueno', 'dueño'
WHERE NOT EXISTS (SELECT 1 FROM personas);

-- ventas.registrado_por
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS registrado_por INTEGER
    REFERENCES personas(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_ventas_registrado_por ON ventas (registrado_por);

-- historial_cambios.usuario
ALTER TABLE historial_cambios ADD COLUMN IF NOT EXISTS usuario TEXT;

-- articulos.volumen_litros
ALTER TABLE articulos ADD COLUMN IF NOT EXISTS volumen_litros NUMERIC(10,3);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'articulos_volumen_no_negativo') THEN
        ALTER TABLE articulos ADD CONSTRAINT articulos_volumen_no_negativo
            CHECK (volumen_litros IS NULL OR volumen_litros >= 0);
    END IF;
END $$;

-- Tabla parametros
CREATE TABLE IF NOT EXISTS parametros (
    clave TEXT PRIMARY KEY,
    valor NUMERIC(12,4) NOT NULL,
    descripcion TEXT,
    actualizado_en TIMESTAMP NOT NULL DEFAULT now()
);
INSERT INTO parametros (clave, valor, descripcion) VALUES
    ('frecuencia_viaje_dias', 7,    'Días entre viajes de compra al mayorista (R)'),
    ('capacidad_litros',      2000, 'Capacidad útil del depósito, en litros'),
    ('nivel_servicio_A',      0.95, 'Nivel de servicio objetivo, clase A'),
    ('nivel_servicio_B',      0.90, 'Nivel de servicio objetivo, clase B'),
    ('nivel_servicio_C',      0.80, 'Nivel de servicio objetivo, clase C')
ON CONFLICT (clave) DO NOTHING;

-- Tabla pronostico_snapshot
CREATE TABLE IF NOT EXISTS pronostico_snapshot (
    fecha_calculo DATE NOT NULL,
    articulo_id INTEGER NOT NULL REFERENCES articulos(id) ON DELETE CASCADE,
    fecha_objetivo DATE NOT NULL,
    demanda NUMERIC(12,4) NOT NULL CHECK (demanda >= 0),
    modelo TEXT NOT NULL,
    PRIMARY KEY (fecha_calculo, articulo_id, fecha_objetivo)
);
CREATE INDEX IF NOT EXISTS idx_pronostico_articulo_objetivo
    ON pronostico_snapshot (articulo_id, fecha_objetivo);

-- ------------------------------------------------------------
-- LISTA DE COMPRAS (Sprint 2, HU-015, 10/2026). Mismo SQL que
-- migracion_lista_compras.py.
-- reposicion_dias: NULL = se compra en el viaje al mayorista
--   (parametros.frecuencia_viaje_dias); 1 = compra diaria (ej.: pan).
-- vida_util_dias: días que se puede vender antes de tirarlo/regalarlo.
-- nivel_servicio: propio del artículo (NULL = el de su clase ABC, o el
--   óptimo económico para los de compra diaria).
-- ------------------------------------------------------------
ALTER TABLE articulos ADD COLUMN IF NOT EXISTS reposicion_dias INTEGER
    CONSTRAINT articulos_reposicion_dias_check CHECK (reposicion_dias IS NULL OR reposicion_dias BETWEEN 1 AND 60);
ALTER TABLE articulos ADD COLUMN IF NOT EXISTS vida_util_dias INTEGER
    CONSTRAINT articulos_vida_util_check CHECK (vida_util_dias IS NULL OR vida_util_dias >= 1);
ALTER TABLE articulos ADD COLUMN IF NOT EXISTS nivel_servicio NUMERIC(4,3)
    CONSTRAINT articulos_nivel_servicio_check CHECK (nivel_servicio IS NULL OR (nivel_servicio >= 0.5 AND nivel_servicio < 1));
INSERT INTO parametros (clave, valor, descripcion) VALUES
    ('valor_recupero_pct', 0, 'Fracción del costo que se recupera de lo que sobra de un perecedero (0 = se regala o se tira)')
ON CONFLICT (clave) DO NOTHING;


-- ------------------------------------------------------------
-- TANDA 5 (10/2026): cantidades con 3 decimales y desglose de combos
-- guardado al vender. Mismo SQL que migracion_tanda5.py (en una base ya
-- existente se aplica con ese script, que además completa el desglose de las
-- ventas de combo ya cargadas y verifica que no cambie ningún número).
-- ------------------------------------------------------------
DROP VIEW v_ventas_analitica;
DROP VIEW v_articulos;
DROP TRIGGER trg_editar_venta ON ventas;
ALTER TABLE ventas ALTER COLUMN cantidad TYPE NUMERIC(12,3);
ALTER TABLE detalle_factura ALTER COLUMN cantidad TYPE NUMERIC(12,3);
ALTER TABLE articulos ALTER COLUMN stock_actual TYPE NUMERIC(12,3);
ALTER TABLE articulos ALTER COLUMN stock_minimo TYPE NUMERIC(12,3);
ALTER TABLE fraccionamientos ALTER COLUMN cantidad_bolsas TYPE NUMERIC(12,3);
CREATE TRIGGER trg_editar_venta
AFTER UPDATE ON ventas
FOR EACH ROW
WHEN (OLD.cantidad IS DISTINCT FROM NEW.cantidad OR OLD.articulo_id IS DISTINCT FROM NEW.articulo_id)
EXECUTE FUNCTION fn_editar_venta();

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
    sub.promo_tipo,
    sub.iva,
    sub.activo,
    sub.fecha_baja,
    sub.motivo_baja,
    sub.reemplazado_por
FROM (
    SELECT
        a.id, a.nombre, a.marca, a.categoria, a.costo_compra_actual,
        a.margen_ganancia, a.iva,
        CASE
            WHEN a.categoria = 'Promocion' AND a.promo_tipo = 'virtual'
                THEN COALESCE(vp.stock_virtual, 0)
            ELSE a.stock_actual
        END::NUMERIC(12,3) AS stock_actual,
        a.stock_minimo, a.fecha_vencimiento, a.acceso_rapido, a.unidad_medida,
        a.articulo_fraccion_de, a.factor_fraccion, a.peso_bolsa_gramos, a.promo_tipo,
        a.activo, a.fecha_baja, a.motivo_baja, a.reemplazado_por
    FROM articulos a
    LEFT JOIN (
        SELECT pc.promocion_articulo_id,
               FLOOR(MIN(comp.stock_actual / pc.cantidad_por_unidad)) AS stock_virtual
        FROM promocion_componentes pc
        JOIN articulos comp ON comp.id = pc.componente_articulo_id
        GROUP BY pc.promocion_articulo_id
    ) vp ON vp.promocion_articulo_id = a.id
) sub;
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
);
CREATE INDEX idx_venta_componentes_componente ON venta_componentes (componente_articulo_id);
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
JOIN articulos comp ON comp.id = p.componente_articulo_id;
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
$$ LANGUAGE plpgsql;
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
$$ LANGUAGE plpgsql;
CREATE OR REPLACE FUNCTION fn_revertir_venta() RETURNS TRIGGER AS $$
BEGIN
    PERFORM fn_devolver_stock_venta(OLD.id, OLD.articulo_id, OLD.cantidad);
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER trg_revertir_venta ON ventas;
CREATE TRIGGER trg_revertir_venta BEFORE DELETE ON ventas FOR EACH ROW EXECUTE FUNCTION fn_revertir_venta();
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
$$ LANGUAGE plpgsql;

-- ------------------------------------------------------------
-- Índices para las consultas frecuentes (tanda 11, 10/2026).
-- En una base existente se agregan con migracion_tanda11_indices.py.
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_ventas_fecha ON ventas (fecha);
CREATE INDEX IF NOT EXISTS idx_ventas_articulo ON ventas (articulo_id);
CREATE INDEX IF NOT EXISTS idx_ventas_ticket ON ventas (ticket_id);
CREATE INDEX IF NOT EXISTS idx_detalle_factura_factura ON detalle_factura (factura_id);
CREATE INDEX IF NOT EXISTS idx_detalle_factura_articulo ON detalle_factura (articulo_id);
CREATE INDEX IF NOT EXISTS idx_historial_registro ON historial_cambios (tabla, registro_id);
CREATE INDEX IF NOT EXISTS idx_fracc_bolsa ON fraccionamientos (articulo_bolsa_id);
CREATE INDEX IF NOT EXISTS idx_fracc_derivado ON fraccionamientos (articulo_derivado_id);
CREATE INDEX IF NOT EXISTS idx_gastos_fecha ON gastos (fecha);

-- ------------------------------------------------------------
-- Bloqueo por intentos fallidos de ingreso (tanda 12, 10/2026).
-- En una base existente se crea con migracion_tanda12.py.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intentos_ingreso (
    usuario TEXT PRIMARY KEY,               -- tal como se tipeó (en minúsculas), exista o no
    fallidos INTEGER NOT NULL DEFAULT 0,    -- contraseñas incorrectas seguidas
    bloqueos INTEGER NOT NULL DEFAULT 0,    -- bloqueos seguidos (cada uno dura el doble)
    ultimo_fallo TIMESTAMPTZ,
    bloqueado_hasta TIMESTAMPTZ
);

-- ------------------------------------------------------------
-- Proveedores en consignación (tanda 14, 10/2026): dejan mercadería que se
-- les paga cuando se vende. En una base existente: migracion_tanda14.py.
-- ------------------------------------------------------------
ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS consignacion BOOLEAN NOT NULL DEFAULT FALSE;

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
);

-- ------------------------------------------------------------
-- TANDA 17: lista de compras guardada y registro de viajes
-- (protocolo de validación con el comerciante; migracion_tanda17.py)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listas_compra (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL UNIQUE,                       -- día en que se armó (una por día)
    persona_id INTEGER REFERENCES personas(id),
    renglones INTEGER NOT NULL DEFAULT 0,             -- renglones que se llevó
    costo_estimado NUMERIC(14,2) NOT NULL DEFAULT 0,  -- con IVA, de lo que se llevó
    nota TEXT,
    creado_en TIMESTAMP NOT NULL DEFAULT now(),
    actualizado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS listas_compra_detalle (
    lista_id INTEGER NOT NULL REFERENCES listas_compra(id) ON DELETE CASCADE,
    articulo_id INTEGER NOT NULL REFERENCES articulos(id),
    clase TEXT,
    proveedor TEXT,
    stock NUMERIC(12,3),
    venta_esperada NUMERIC(12,3),
    sugerido NUMERIC(12,3) NOT NULL,                  -- lo que calculó la app
    cantidad NUMERIC(12,3) NOT NULL,                  -- lo que quedó en la lista (0 si lo sacó)
    incluido BOOLEAN NOT NULL,
    costo_c_iva NUMERIC(14,2),
    PRIMARY KEY (lista_id, articulo_id)
);
CREATE TABLE IF NOT EXISTS viajes_compra (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL,
    tipo TEXT NOT NULL CHECK (tipo IN ('planificado', 'urgencia')),
    horas NUMERIC(4,1) CHECK (horas IS NULL OR (horas >= 0 AND horas <= 24)),
    lista_id INTEGER REFERENCES listas_compra(id) ON DELETE SET NULL,   -- NULL = no usó lista
    nota TEXT,
    persona_id INTEGER REFERENCES personas(id),
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_viajes_compra_fecha ON viajes_compra (fecha);

-- ------------------------------------------------------------
-- VENCIMIENTOS (tanda 18): días disponibles para la venta por categoría.
-- Se usa para los artículos sin articulos.vida_util_dias propia. Con la
-- fecha de cada factura da el vencimiento estimado de cada lote (renglón),
-- sin control de lotes a mano. Ver db.lotes_vencimiento.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vida_util_categoria (
    categoria TEXT PRIMARY KEY,
    dias INTEGER NOT NULL CHECK (dias BETWEEN 1 AND 3650),
    actualizado_en TIMESTAMP NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- DESAFÍO RECREO (tanda 21): control de vencimientos en góndola con
-- estrellitas. controles_vencimiento es el dato real de vencimiento y, con
-- el conteo a ciegas, de exactitud del inventario. El resto es el juego
-- (tarea del día, eventos, participantes, premios). cierres_caja guarda el
-- cierre del día (arqueo de efectivo y pan). Ver desafio.py.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS controles_vencimiento (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL,                              -- día del control (hora argentina)
    persona_id INTEGER REFERENCES personas(id),
    articulo_id INTEGER NOT NULL REFERENCES articulos(id),
    hay_stock BOOLEAN NOT NULL DEFAULT TRUE,          -- FALSE = "no hay" en la góndola
    no_vence BOOLEAN NOT NULL DEFAULT FALSE,          -- TRUE = el envase no tiene vencimiento (sale de la tarea)
    vence DATE,                                       -- la fecha más cercana que se vio
    cantidad NUMERIC(12,3),                           -- cuántas vencen esa fecha (opcional)
    conteo NUMERIC(12,3),                             -- cuántos hay en total (si la tarea pedía contar)
    conteo_coincide BOOLEAN,                          -- el conteo coincide con el stock de la app (con tolerancia)
    corregido BOOLEAN NOT NULL DEFAULT FALSE,         -- se corrigió el stock de la app con este conteo
    stock_al_controlar NUMERIC(12,3),                 -- stock de la app en ese momento
    fecha_compra_lote DATE,                           -- compra más vieja que queda (FEFO): para los días típicos
    repregunta BOOLEAN NOT NULL DEFAULT FALSE,        -- control repetido al azar para verificar
    coincide BOOLEAN,                                 -- repregunta: ¿coincidió con el anterior?
    cuenta BOOLEAN NOT NULL DEFAULT TRUE,             -- FALSE = no suma estrellitas (repetido, no coincide)
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_controles_venc_articulo ON controles_vencimiento (articulo_id, fecha DESC);
CREATE TABLE IF NOT EXISTS desafio_tareas (
    fecha DATE NOT NULL,
    persona_id INTEGER NOT NULL REFERENCES personas(id),
    articulo_id INTEGER NOT NULL REFERENCES articulos(id),
    orden INTEGER NOT NULL,
    tipo TEXT NOT NULL DEFAULT 'gondola',             -- gondola (mirar la fecha) / dato (arreglar un dato)
    motivo TEXT NOT NULL,                             -- gondola: perecedero / stock / nunca / repaso. dato: el chequeo
    pide_conteo BOOLEAN NOT NULL DEFAULT FALSE,       -- gondola: además, contar cuántos hay
    PRIMARY KEY (fecha, persona_id, articulo_id)
);
CREATE TABLE IF NOT EXISTS desafio_eventos (
    id SERIAL PRIMARY KEY,
    persona_id INTEGER NOT NULL REFERENCES personas(id),
    fecha DATE NOT NULL,
    tipo TEXT NOT NULL,                               -- control, tarea, racha, boletin, rescate, monedas, dato,
                                                      -- lista, viaje, lista_cumplida, cierre
    ref_id BIGINT NOT NULL,                           -- control, día (aaaammdd), ticket, lista, viaje...
    estrellitas INTEGER NOT NULL DEFAULT 0,
    valor NUMERIC(14,2),                              -- rescate: $ a costo salvados, monedas: costo regalado
    detalle TEXT,
    creado_en TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (persona_id, tipo, ref_id)
);
CREATE TABLE IF NOT EXISTS desafio_participantes (
    persona_id INTEGER PRIMARY KEY REFERENCES personas(id),
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    desde DATE NOT NULL DEFAULT CURRENT_DATE
);
CREATE TABLE IF NOT EXISTS desafio_premios (
    hito TEXT PRIMARY KEY,
    premio TEXT,
    actualizado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cierres_caja (
    fecha DATE PRIMARY KEY,                           -- el día que se cierra (uno por día, se puede volver a guardar)
    persona_id INTEGER REFERENCES personas(id),
    fondo NUMERIC(14,2),                              -- cambio al abrir
    ventas_efectivo NUMERIC(14,2),
    gastos_caja NUMERIC(14,2),                        -- gastos que salieron del cajón
    esperado NUMERIC(14,2),
    contado NUMERIC(14,2),                            -- efectivo contado al cerrar
    diferencia NUMERIC(14,2),                         -- contado - esperado
    pan_sin_sobrante BOOLEAN NOT NULL DEFAULT FALSE,  -- "hoy no sobró pan" (el cero también queda registrado)
    creado_en TIMESTAMP NOT NULL DEFAULT now(),
    actualizado_en TIMESTAMP NOT NULL DEFAULT now()
);
