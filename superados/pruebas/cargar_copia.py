"""
Arma una copia local de producción a partir de un respaldo de respaldo_produccion.py:
schema.sql actual + los CSV de cada tabla (los triggers se desactivan durante la
carga para que el stock quede exactamente como estaba en producción).

Uso: python cargar_copia.py <carpeta_respaldo> <nombre_base>
"""
import csv
import json
import sys
from pathlib import Path

import os

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

HOST, PORT, USER = config.HOST, config.PORT, config.USER
SALTEABLES = {"migracion_respaldo_objetos"}


def main(carpeta, base):
    carpeta = Path(carpeta)
    manifiesto = json.loads((carpeta / "manifiesto.json").read_text(encoding="utf-8"))
    esperadas = manifiesto["filas_por_tabla"]

    admin = psycopg2.connect(host=HOST, port=PORT, user=USER, password=config.PASSWORD, dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{base}"')
        cur.execute(f'CREATE DATABASE "{base}"')
    admin.close()

    # Estructura: la REAL de producción si el respaldo trae esquema_produccion.sql
    # (exportar_esquema.py); si no, schema.sql del repo.
    esquema_prod = carpeta / "esquema_produccion.sql"
    if esquema_prod.exists():
        sql = "\n".join(l for l in esquema_prod.read_text(encoding="utf-8").splitlines()
                        if not l.startswith("\\") and "transaction_timeout" not in l)
        c_esq = psycopg2.connect(host=HOST, port=PORT, user=USER, password=config.PASSWORD, dbname=base)
        with c_esq, c_esq.cursor() as cur:
            cur.execute(sql)
        c_esq.close()
        print("estructura: esquema_produccion.sql (la real de producción)")
    else:
        c_esq = psycopg2.connect(host=HOST, port=PORT, user=USER, password=config.PASSWORD, dbname=base)
        with c_esq, c_esq.cursor() as cur:
            cur.execute((Path(__file__).resolve().parents[2] / "schema.sql").read_text(encoding="utf-8"))
        c_esq.close()
    conn = psycopg2.connect(host=HOST, port=PORT, user=USER, password=config.PASSWORD, dbname=base)
    with conn, conn.cursor() as cur:
        cur.execute("SET session_replication_role = replica")  # sin triggers ni FK durante la carga
        cur.execute("""SELECT tablename FROM pg_tables WHERE schemaname = 'public'""")
        existentes = {r[0] for r in cur.fetchall()}
        # Tablas de control de migraciones: no las usa la app, se saltean si
        # schema.sql no las trae.
        for t in [t for t in esperadas if t not in existentes and t in SALTEABLES]:
            print(f"aviso: se saltea {t} (tabla de control de migraciones, no la usa la app)")
            esperadas.pop(t)
        faltan = [t for t in esperadas if t not in existentes]
        if faltan:
            raise SystemExit(f"schema.sql no tiene estas tablas del respaldo: {faltan}")
        cur.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in esperadas) + " CASCADE")
        for tabla in esperadas:
            ruta = carpeta / "csv" / f"{tabla}.csv"
            with open(ruta, encoding="utf-8-sig", newline="") as f:
                columnas = next(csv.reader(f))
            with open(ruta, encoding="utf-8-sig") as f:
                cols = ", ".join(f'"{c}"' for c in columnas)
                cur.copy_expert(f'COPY "{tabla}" ({cols}) FROM STDIN WITH (FORMAT csv, HEADER true)', f)
        # secuencias al máximo id de cada tabla
        cur.execute("""
            SELECT c.relname, a.attname, pg_get_serial_sequence(c.relname, a.attname)
            FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE c.relkind = 'r' AND c.relnamespace = 'public'::regnamespace
              AND pg_get_serial_sequence(c.relname, a.attname) IS NOT NULL
        """)
        for tabla, col, seq in cur.fetchall():
            cur.execute(f'SELECT setval(%s, COALESCE((SELECT MAX("{col}") FROM "{tabla}"), 0) + 1, false)', (seq,))

    with conn.cursor() as cur:
        mal = []
        for tabla, n in esperadas.items():
            cur.execute(f'SELECT COUNT(*) FROM "{tabla}"')
            real = cur.fetchone()[0]
            if real != n:
                mal.append((tabla, n, real))
    conn.close()
    if mal:
        raise SystemExit(f"Filas distintas a producción: {mal}")
    print(f"OK: {base} con {len(esperadas)} tablas y {sum(esperadas.values()):,} filas, igual que producción.")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
