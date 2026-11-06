"""
Migración única (Sprint 2, 10/2026): base para el pronóstico, la lista de
compras y la calidad de datos por persona.

Esta migración (solo AGREGA; no borra ni reescribe datos existentes):

  1. Crea 'personas': los usuarios de la app (nombre, usuario, contraseña
     guardada con hash, rol dueño/atención, pantallas habilitadas, activo).
     Arranca con "Dueño" (dueño, usuario "dueno"). Al final, si el comerciante todavía
     no tiene contraseña, el script la pide por teclado (no se muestra ni
     se guarda en texto plano).
  2. Agrega ventas.registrado_por (-> personas.id). Las ventas ya
     cargadas quedan en NULL ("sin dato").
  3. Agrega historial_cambios.usuario (nombre de quien hizo el cambio).
  4. Agrega articulos.volumen_litros (para la restricción de espacio del
     depósito en la lista de compras). Queda vacío hasta cargarlo.
  5. Crea 'parametros' (clave/valor editables desde Configuración):
     frecuencia de viaje al mayorista, capacidad del depósito en litros y
     nivel de servicio por clase ABC.
  6. Crea 'pronostico_snapshot': guarda cada pronóstico calculado para
     medir después su error real (validación de 3 meses con el comerciante).

Se puede correr más de una vez sin romper nada. Correrla ANTES de subir
la versión nueva de app.py/db.py (la app nueva usa estas tablas). La app
vieja sigue funcionando con la base migrada (todo lo nuevo es opcional).

Antes de correrla en producción, hacer un respaldo:

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python analisis/respaldo_produccion.py
    python superados/migraciones/migracion_sprint2.py
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
import os
import sys
from getpass import getpass

import db
from sqlalchemy import text

PASOS = [
    ("Tabla personas", [
        """
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
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_personas_nombre ON personas (lower(nombre))",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_personas_usuario ON personas (lower(usuario))",
        """
        INSERT INTO personas (nombre, usuario, rol)
        SELECT 'Dueño', 'dueno', 'dueño'
        WHERE NOT EXISTS (SELECT 1 FROM personas)
        """,
    ]),
    ("ventas.registrado_por", [
        """
        ALTER TABLE ventas ADD COLUMN IF NOT EXISTS registrado_por INTEGER
            REFERENCES personas(id) ON DELETE SET NULL
        """,
        "CREATE INDEX IF NOT EXISTS idx_ventas_registrado_por ON ventas (registrado_por)",
    ]),
    ("historial_cambios.usuario", [
        "ALTER TABLE historial_cambios ADD COLUMN IF NOT EXISTS usuario TEXT",
    ]),
    ("articulos.volumen_litros", [
        "ALTER TABLE articulos ADD COLUMN IF NOT EXISTS volumen_litros NUMERIC(10,3)",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'articulos_volumen_no_negativo') THEN
                ALTER TABLE articulos ADD CONSTRAINT articulos_volumen_no_negativo
                    CHECK (volumen_litros IS NULL OR volumen_litros >= 0);
            END IF;
        END $$;
        """,
    ]),
    ("Tabla parametros", [
        """
        CREATE TABLE IF NOT EXISTS parametros (
            clave TEXT PRIMARY KEY,
            valor NUMERIC(12,4) NOT NULL,
            descripcion TEXT,
            actualizado_en TIMESTAMP NOT NULL DEFAULT now()
        )
        """,
        """
        INSERT INTO parametros (clave, valor, descripcion) VALUES
            ('frecuencia_viaje_dias', 7,    'Días entre viajes de compra al mayorista (R)'),
            ('capacidad_litros',      2000, 'Capacidad útil del depósito, en litros'),
            ('nivel_servicio_A',      0.95, 'Nivel de servicio objetivo, clase A'),
            ('nivel_servicio_B',      0.90, 'Nivel de servicio objetivo, clase B'),
            ('nivel_servicio_C',      0.80, 'Nivel de servicio objetivo, clase C')
        ON CONFLICT (clave) DO NOTHING
        """,
    ]),
    ("Tabla pronostico_snapshot", [
        """
        CREATE TABLE IF NOT EXISTS pronostico_snapshot (
            fecha_calculo DATE NOT NULL,
            articulo_id INTEGER NOT NULL REFERENCES articulos(id) ON DELETE CASCADE,
            fecha_objetivo DATE NOT NULL,
            demanda NUMERIC(12,4) NOT NULL CHECK (demanda >= 0),
            modelo TEXT NOT NULL,
            PRIMARY KEY (fecha_calculo, articulo_id, fecha_objetivo)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_pronostico_articulo_objetivo
            ON pronostico_snapshot (articulo_id, fecha_objetivo)
        """,
    ]),
]

ESPERADO = {
    ("personas", "id"), ("ventas", "registrado_por"), ("historial_cambios", "usuario"),
    ("articulos", "volumen_litros"), ("parametros", "clave"), ("pronostico_snapshot", "modelo"),
}


def probar_conexion():
    url = db.engine.url
    print(f"Conectando a: host={url.host!r} db={url.database!r} user={url.username!r}")
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("Conexion OK.\n")
    except Exception as e:
        print("\n*** No se pudo conectar a la base. Detalle completo: ***")
        print(f"{type(e).__name__}: {e}")
        sys.exit(1)


def asegurar_clave_dueno():
    """Si ningún dueño activo tiene contraseña, la pide por teclado y la guarda con hash."""
    with db.engine.connect() as conn:
        fila = conn.execute(text("""
            SELECT id, nombre, usuario FROM personas
            WHERE rol = 'dueño' AND activo ORDER BY id LIMIT 1
        """)).first()
        tiene = conn.execute(text(
            "SELECT 1 FROM personas WHERE rol = 'dueño' AND activo AND clave_hash IS NOT NULL"
        )).first()
    if tiene or not fila:
        return
    pid, nombre, usuario = fila
    print(f"\n{nombre} (usuario '{usuario}') todavía no tiene contraseña para entrar a la app.")
    clave = os.environ.get("MIGRACION_CLAVE_DUENO")
    while not clave:
        c1 = getpass("  Contraseña nueva (mínimo 8 caracteres, no se ve al escribir): ")
        c2 = getpass("  Repetila: ")
        if c1 != c2:
            print("  No coinciden, probá de nuevo.")
        else:
            try:
                db.validar_clave(c1)
                clave = c1
            except ValueError as e:
                print(f"  {e}")
    db.cambiar_clave(pid, clave)
    print(f"  Contraseña guardada (con hash). {nombre} entra con usuario '{usuario}'.")


def main():
    probar_conexion()
    with db.engine.connect() as conn:
        antes = conn.execute(text("SELECT count(*) FROM ventas")).scalar()

    # Todo en una sola transacción: o se aplica completa o no se aplica nada.
    with db.engine.begin() as conn:
        for i, (titulo, sentencias) in enumerate(PASOS, 1):
            print(f"PASO {i}: {titulo}")
            for q in sentencias:
                conn.execute(text(q))

    with db.engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = 'public'
        """)).fetchall()
        despues = conn.execute(text("SELECT count(*) FROM ventas")).scalar()
        personas = conn.execute(text("SELECT nombre || ' (' || rol || ')' FROM personas ORDER BY id")).scalars().all()
        params = conn.execute(text("SELECT clave, valor FROM parametros ORDER BY clave")).fetchall()

    faltan = ESPERADO - {(t, c) for t, c in cols}
    if faltan:
        print(f"\n*** Faltan: {faltan} ***")
        sys.exit(1)
    if antes != despues:
        print(f"\n*** Cambió la cantidad de ventas ({antes} -> {despues}); revisar. ***")
        sys.exit(1)

    asegurar_clave_dueno()
    print("\nVerificación OK.")
    print(f"  Ventas: {despues} (sin cambios; las existentes quedan con registrado_por vacío)")
    print(f"  Personas: {', '.join(personas)}")
    print("  Parámetros: " + ", ".join(f"{c}={float(v):g}" for c, v in params))
    print("\nMigración completa. Ahora sí se puede subir la versión nueva de la app (commit/push).")


if __name__ == "__main__":
    main()
