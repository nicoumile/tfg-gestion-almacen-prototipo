"""
Migración única (tanda 21, 10/2026): Desafío Recreo (control de vencimientos en góndola).

QUÉ HACE (todo en UNA transacción; si algo falla no cambia nada)
  1. controles_vencimiento: cada control de góndola (artículo, fecha más cercana
     vista, cantidad opcional, "no hay", conteo). Es el dato real de vencimiento
     y, con el conteo, de exactitud del inventario.
  2. desafio_tareas: la tarea de cada día (qué artículos tocaba controlar o contar,
     y el dato a arreglar).
  3. desafio_eventos: las estrellitas ganadas (controles, tarea completa, racha,
     boletín, rescates, juego de las monedas, datos arreglados, listas, viajes,
     cierres del día).
  4. desafio_participantes: quién juega (arranca solo el dueño, el comerciante).
  5. desafio_premios: el premio de cada hito (lo escribe el comerciante).
  6. cierres_caja: el cierre del día guardado (arqueo de efectivo y pan).
  7. Parámetros: artículos por día (6), conteos por día (2), comodines por semana
     (1), juego de las monedas (desde $10.000, 2 monedas, artículo MONEDAS PIRATA
     si existe).
  No cambia stock, costos, precios, ventas ni facturas.

Se puede correr más de una vez. Para deshacerlo (borra lo registrado):
    python migracion_tanda21.py --revertir

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python respaldo_produccion.py
    python migracion_tanda21.py
"""
import sys

from sqlalchemy import text

import db

SQL = """
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
)
"""

PARAMS = [
    ("desafio_articulos_dia", 6, "Desafío Recreo: artículos a controlar por día"),
    ("desafio_conteos_dia", 2, "Desafío Recreo: de esos, cuántos además se cuentan"),
    ("desafio_comodines_semana", 1, "Desafío Recreo: días que se pueden saltear por semana sin cortar la racha"),
    ("monedas_umbral", 10000, "Juego de las monedas: se ofrece desde una compra de este monto ($)"),
    ("monedas_por_juego", 2, "Juego de las monedas: monedas que se regalan habitualmente"),
]


def aplicar():
    with db.engine.begin() as con:
        if con.execute(text("SELECT to_regclass('public.controles_vencimiento') IS NOT NULL")).scalar():
            print("Ya estaba aplicada: la tabla controles_vencimiento existe. No se cambió nada.")
            return
        for sentencia in [s for s in SQL.split(";") if s.strip()]:
            con.execute(text(sentencia))
        for clave, valor, desc in PARAMS:
            con.execute(text("""INSERT INTO parametros (clave, valor, descripcion) VALUES (:c, :v, :d)
                                ON CONFLICT (clave) DO NOTHING"""), {"c": clave, "v": valor, "d": desc})
        monedas = con.execute(text("SELECT id FROM articulos WHERE upper(trim(nombre)) = 'MONEDAS PIRATA' "
                                   "AND activo ORDER BY id LIMIT 1")).scalar()
        if monedas:
            con.execute(text("""INSERT INTO parametros (clave, valor, descripcion) VALUES ('monedas_articulo_id', :v,
                                'Juego de las monedas: artículo que se regala') ON CONFLICT (clave) DO NOTHING"""),
                        {"v": monedas})
        duenos = con.execute(text("SELECT id, nombre FROM personas WHERE rol = 'dueño' AND activo "
                                  "AND lower(nombre) LIKE 'dueño%' ORDER BY id")).all()
        for pid, nombre in duenos:
            con.execute(text("INSERT INTO desafio_participantes (persona_id) VALUES (:p) ON CONFLICT DO NOTHING"),
                        {"p": pid})
    print("Listo: tablas del Desafío Recreo y del cierre del día creadas"
          + (f"; participa {', '.join(n for _, n in duenos)}" if duenos else "; sin participantes (se eligen en 🔒 Configuración)")
          + (f"; monedas: artículo #{monedas}" if monedas else "; monedas: elegir el artículo en 🔒 Configuración")
          + ". Ningún stock, costo, precio ni venta cambió.")


def revertir():
    with db.engine.begin() as con:
        for t in ("cierres_caja", "desafio_premios", "desafio_participantes", "desafio_eventos", "desafio_tareas", "controles_vencimiento"):
            con.execute(text(f"DROP TABLE IF EXISTS {t}"))
        con.execute(text("DELETE FROM parametros WHERE clave IN ('desafio_articulos_dia', 'desafio_conteos_dia', 'desafio_comodines_semana', "
                         "'monedas_umbral', 'monedas_por_juego', 'monedas_articulo_id')"))
    print("Quitadas las tablas y parámetros del Desafío Recreo.")


if __name__ == "__main__":
    revertir() if "--revertir" in sys.argv else aplicar()
