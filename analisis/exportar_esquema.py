"""
Saca la ESTRUCTURA (sin datos) del último respaldo de producción a un archivo
de texto, para revisar las tablas, vistas y triggers reales antes de una
migración. No se conecta a Neon: lee el neondb.dump del respaldo más nuevo.

Uso (desde la raíz del repositorio):  python analisis/exportar_esquema.py
Deja: respaldos/<respaldo más nuevo>/esquema_produccion.sql
"""
import glob
import os
import subprocess
import sys

from respaldo_produccion import buscar_binario

respaldos = sorted(glob.glob(os.path.join("respaldos", "prod_*", "neondb.dump")))
if not respaldos:
    sys.exit("No encontré ningún respaldo con neondb.dump en la carpeta respaldos/.")
dump = respaldos[-1]
destino = os.path.join(os.path.dirname(dump), "esquema_produccion.sql")

binarios = buscar_binario("pg_restore")
if not binarios:
    sys.exit("No encontré pg_restore (viene con PostgreSQL o pgAdmin 4).")
version, pg_restore = binarios[0]
r = subprocess.run([pg_restore, "--schema-only", "--no-owner", "--no-privileges", "-f", destino, dump],
                   capture_output=True, text=True)
if r.returncode != 0:
    sys.exit(f"pg_restore falló:\n{r.stderr}")
print(f"Listo: {destino}  (pg_restore v{version}, desde {dump})")
