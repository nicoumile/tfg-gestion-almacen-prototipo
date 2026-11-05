"""
Conexión de las pruebas a un PostgreSQL LOCAL (nunca a producción).

Se configura con variables de entorno (valores por defecto entre paréntesis):
    PRUEBAS_DB_HOST (localhost)  PRUEBAS_DB_PORT (5432)
    PRUEBAS_DB_USER (postgres)   PRUEBAS_DB_PASSWORD (postgres)

Por seguridad, si el host parece de Neon (producción) las pruebas no corren:
escriben ventas, facturas y gastos de prueba.
"""
import os
import sys

HOST = os.environ.get("PRUEBAS_DB_HOST", "localhost")
PORT = int(os.environ.get("PRUEBAS_DB_PORT", "5432"))
USER = os.environ.get("PRUEBAS_DB_USER", "postgres")
PASSWORD = os.environ.get("PRUEBAS_DB_PASSWORD", "postgres")

if "neon" in HOST.lower() or HOST.endswith(".tech"):
    sys.exit(f"PRUEBAS_DB_HOST={HOST} parece la base de producción. Las pruebas solo corren en una base local.")


def usar_base(base):
    """Apunta db.py (que lee DB_* del entorno) a la base de prueba `base`."""
    os.environ.update(DB_HOST=HOST, DB_PORT=str(PORT), DB_NAME=base, DB_USER=USER, DB_PASSWORD=PASSWORD)


def conectar(base):
    import psycopg2
    return psycopg2.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, dbname=base)
