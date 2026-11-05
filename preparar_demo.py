"""
Deja lista una base de DEMOSTRACIÓN (Entregable 4) para quien instala el prototipo.

La base demo (base_demo_entregable4.dump) es la base simulada oficial del
Entregable 3 (12 meses, del 05/10/2025 al 04/10/2026) cargada sobre el esquema
actual. Este script, que se corre una vez después de restaurarla:

1. Corre todas las fechas de la base hacia adelante, en semanas enteras, para
   que la última venta quede en los 7 días anteriores a hoy. Sin esto, quien la
   abra meses después ve un mes sin ventas y el pronóstico da casi todo cero.
   Se corre de a semanas para no cambiar el día de la semana de cada venta
   (el pronóstico usa el patrón semanal). Los disparadores no se ejecutan: el
   stock y los costos quedan como estaban.
2. Crea (o actualiza) el usuario de demostración, con rol dueño.

Se puede correr más de una vez: si las fechas ya están al día, no las mueve.
Se niega a correr contra la base de producción (Neon).

Uso (con .streamlit/secrets.toml apuntando a la base demo):
    python preparar_demo.py
    python preparar_demo.py --usuario demo --clave <contraseña de 8 o más caracteres>
"""
import argparse
import datetime as dt
import sys
from zoneinfo import ZoneInfo

from sqlalchemy import text

import db

USUARIO_DEMO, CLAVE_DEMO, NOMBRE_DEMO = "demo", "demo-tfg-2026", "Demo"


def columnas_fecha(conn):
    """(tabla, columna, es_parte_de_clave_unica) de cada columna date/timestamp de las tablas."""
    filas = conn.execute(text("""
        SELECT c.table_name, c.column_name,
               EXISTS (SELECT 1 FROM pg_index i
                        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
                       WHERE i.indisunique AND i.indrelid = format('public.%I', c.table_name)::regclass
                         AND a.attname = c.column_name) AS unica
        FROM information_schema.columns c
        JOIN information_schema.tables t USING (table_schema, table_name)
        WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
          AND (c.data_type = 'date' OR c.data_type LIKE 'timestamp%')
        ORDER BY 1, 2
    """)).all()
    return [(f.table_name, f.column_name, f.unica) for f in filas]


def correr_fechas(conn, dias):
    """Suma `dias` a todas las fechas. En las columnas con clave única se pasa por
    un desplazamiento auxiliar para no chocar con otra fila a mitad del UPDATE."""
    conn.execute(text("SET session_replication_role = replica"))  # sin disparadores
    lejos = 400_000
    for tabla, col, unica in columnas_fecha(conn):
        if unica:
            conn.execute(text(f'UPDATE "{tabla}" SET "{col}" = "{col}" + make_interval(days => :d) '
                              f'WHERE "{col}" IS NOT NULL'), {"d": dias + lejos})
            conn.execute(text(f'UPDATE "{tabla}" SET "{col}" = "{col}" - make_interval(days => :d) '
                              f'WHERE "{col}" IS NOT NULL'), {"d": lejos})
        else:
            conn.execute(text(f'UPDATE "{tabla}" SET "{col}" = "{col}" + make_interval(days => :d) '
                              f'WHERE "{col}" IS NOT NULL'), {"d": dias})
    conn.execute(text("SET session_replication_role = DEFAULT"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--usuario", default=USUARIO_DEMO)
    ap.add_argument("--clave", default=CLAVE_DEMO)
    args = ap.parse_args()

    host = db.engine.url.host or ""
    if "neon" in host.lower() or host.endswith(".tech"):
        sys.exit(f"La base configurada ({host}) parece la de producción. Este script solo corre sobre la base demo.")
    db.validar_clave(args.clave)

    hoy = dt.datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    with db.engine.begin() as conn:
        ultima = conn.execute(text("SELECT MAX(fecha) FROM ventas")).scalar()
        if ultima is None:
            sys.exit("La base no tiene ventas: restaurá primero base_demo_entregable4.dump.")
        semanas = ((hoy - dt.timedelta(days=1)) - ultima).days // 7
        if semanas > 0:
            correr_fechas(conn, semanas * 7)
            print(f"Fechas corridas {semanas} semanas: la última venta pasó del {ultima:%d/%m/%Y} "
                  f"al {ultima + dt.timedelta(weeks=semanas):%d/%m/%Y}.")
        else:
            print(f"Las fechas ya están al día (última venta: {ultima:%d/%m/%Y}).")

        dueno = conn.execute(text("SELECT id FROM personas WHERE rol = 'dueño' ORDER BY id LIMIT 1")).scalar()
        datos = {"n": NOMBRE_DEMO, "u": args.usuario.strip().lower(), "h": db.hash_clave(args.clave)}
        if dueno is None:
            conn.execute(text("INSERT INTO personas (nombre, usuario, rol, clave_hash) VALUES (:n, :u, 'dueño', :h)"), datos)
        else:
            conn.execute(text("UPDATE personas SET nombre = :n, usuario = :u, clave_hash = :h, activo = TRUE "
                              "WHERE id = :id"), {**datos, "id": dueno})
    print(f"Listo. Usuario '{datos['u']}' (dueño). Ejecutá: streamlit run app.py")


if __name__ == "__main__":
    main()
