"""
Respaldo de la base de producción (Neon) del sistema de gestión del almacén.

Uso (desde la carpeta TFG, en PowerShell):

    python analisis/respaldo_produccion.py
    python analisis/respaldo_produccion.py --probar-restauracion   # además restaura en el Postgres local y compara

Qué genera, en respaldos/prod_AAAAMMDD_HHMM/:

  neondb.dump          Respaldo completo (estructura + datos + vistas + funciones + triggers)
                       en formato custom de pg_dump. Es el que se usa para restaurar.
  csv/<tabla>.csv      Copia de cada tabla en CSV (se abre en Excel). Respaldo independiente
                       de pg_dump, tomado en la misma transacción de solo lectura.
  manifiesto.json      Fecha, versión del servidor, filas por tabla, objetos de la base y
                       hash SHA-256 de cada archivo (para verificar integridad más adelante).
  LEEME.txt            Resumen legible y cómo restaurar.

La conexión se toma de .streamlit/secrets.toml (DB_HOST, DB_PORT, DB_NAME, DB_USER,
DB_PASSWORD), igual que db.py, o de variables de entorno con los mismos nombres.
La contraseña nunca se escribe en los archivos del respaldo ni se muestra en pantalla.

Solo lee la base de producción: no modifica nada en Neon.
"""

import argparse
import csv
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path

try:
    import psycopg2
except ImportError:
    sys.exit("Falta psycopg2. Instalalo con:  pip install psycopg2-binary")

BASE = Path(__file__).resolve().parent.parent
SECRETS = BASE / ".streamlit" / "secrets.toml"
CLAVES = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------

def ahora_ar():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-3)))


def leer_config():
    """Lee DB_* de secrets.toml (top level) o de variables de entorno."""
    cfg = {}
    if SECRETS.exists():
        texto = SECRETS.read_text(encoding="utf-8")
        datos = None
        for modulo in ("tomllib", "toml"):
            try:
                m = __import__(modulo)
                datos = m.loads(texto)
                break
            except Exception:
                continue
        if datos is None:  # parser mínimo: líneas CLAVE = "valor" antes de la primera [sección]
            datos = {}
            for linea in texto.splitlines():
                if linea.strip().startswith("["):
                    break  # las claves DB_* están antes de cualquier [sección]
                mm = re.match(r'\s*([A-Z_]+)\s*=\s*"(.*)"\s*$', linea)
                if mm:
                    datos[mm.group(1)] = mm.group(2)
        for k in CLAVES:
            if k in datos:
                cfg[k] = str(datos[k])
    for k in CLAVES:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    faltan = [k for k in CLAVES if not cfg.get(k)]
    if faltan:
        sys.exit(f"Faltan datos de conexión: {', '.join(faltan)} (revisá .streamlit/secrets.toml)")
    return cfg


def host_directo(host):
    """Neon recomienda pg_dump contra el endpoint directo, no el '-pooler' (PgBouncer)."""
    return host.replace("-pooler.", ".", 1)


def es_local(host):
    return host in ("localhost", "127.0.0.1", "::1")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def buscar_binario(nombre):
    """Busca pg_dump / pg_restore: PATH, instalaciones de PostgreSQL y de pgAdmin 4 en Windows.
    Devuelve una lista de (version_mayor, ruta), de mayor a menor versión."""
    exe = nombre + (".exe" if os.name == "nt" else "")
    candidatos = []
    en_path = shutil.which(nombre)
    if en_path:
        candidatos.append(en_path)
    patrones = [
        r"C:\Program Files\PostgreSQL\*\bin",
        r"C:\Program Files (x86)\PostgreSQL\*\bin",
        r"C:\Program Files\pgAdmin 4\runtime",
        r"C:\Program Files\pgAdmin 4\v*\runtime",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "pgAdmin 4", "runtime"),
        "/usr/lib/postgresql/*/bin",
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ]
    for p in patrones:
        for d in glob.glob(p):
            ruta = os.path.join(d, exe)
            if os.path.isfile(ruta):
                candidatos.append(ruta)
    vistos, encontrados = set(), []
    for ruta in candidatos:
        real = os.path.realpath(ruta)
        if real in vistos:
            continue
        vistos.add(real)
        try:
            out = subprocess.run([ruta, "--version"], capture_output=True, text=True, timeout=15).stdout
            mm = re.search(r"(\d+)(?:\.(\d+))?", out)
            if mm:
                encontrados.append((int(mm.group(1)), ruta))
        except Exception:
            pass
    return sorted(encontrados, reverse=True)


def conectar(cfg, host=None, dbname=None):
    h = host or cfg["DB_HOST"]
    return psycopg2.connect(
        host=h,
        port=cfg["DB_PORT"],
        dbname=dbname or cfg["DB_NAME"],
        user=cfg["DB_USER"],
        password=cfg["DB_PASSWORD"],
        sslmode="disable" if es_local(h) else "require",
        connect_timeout=20,
        application_name="respaldo_produccion",
    )


# ----------------------------------------------------------------------------
# Inventario de la base
# ----------------------------------------------------------------------------

SQL_TABLAS = """
SELECT c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY c.relname
"""

SQL_OBJETOS = """
SELECT
  (SELECT count(*) FROM pg_views WHERE schemaname = 'public')                         AS vistas,
  (SELECT count(*) FROM pg_matviews WHERE schemaname = 'public')                      AS vistas_materializadas,
  (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d
                       WHERE d.objid = p.oid AND d.deptype = 'e'))                    AS funciones,
  (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
     JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND NOT t.tgisinternal)                               AS triggers,
  (SELECT count(*) FROM pg_sequences WHERE schemaname = 'public')                     AS secuencias,
  (SELECT string_agg(extname || ' ' || extversion, ', ' ORDER BY extname)
     FROM pg_extension WHERE extname <> 'plpgsql')                                    AS extensiones
"""


def inventario(cur):
    cur.execute(SQL_TABLAS)
    tablas = [r[0] for r in cur.fetchall()]
    filas = {}
    for t in tablas:
        cur.execute(f'SELECT count(*) FROM public."{t}"')
        filas[t] = cur.fetchone()[0]
    cur.execute(SQL_OBJETOS)
    cols = [d[0] for d in cur.description]
    objetos = dict(zip(cols, cur.fetchone()))
    return tablas, filas, objetos


# ----------------------------------------------------------------------------
# Respaldo
# ----------------------------------------------------------------------------

def exportar_csv(conn, tablas, destino):
    destino.mkdir(parents=True, exist_ok=True)
    with conn.cursor() as cur:
        for t in tablas:
            ruta = destino / f"{t}.csv"
            # utf-8-sig: Excel en Windows reconoce acentos y la ñ
            with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
                cur.copy_expert(f'COPY public."{t}" TO STDOUT WITH (FORMAT csv, HEADER true)', f)


def correr_pg_dump(pg_dump, cfg, host, archivo):
    env = dict(os.environ, PGPASSWORD=cfg["DB_PASSWORD"],
               PGSSLMODE="disable" if es_local(host) else "require")
    cmd = [pg_dump, "-h", host, "-p", str(cfg["DB_PORT"]), "-U", cfg["DB_USER"], "-d", cfg["DB_NAME"],
           "--format=custom", "--no-owner", "--no-privileges", "--file", str(archivo)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    return r.returncode, r.stderr.strip()


def listar_dump(pg_restore, archivo):
    r = subprocess.run([pg_restore, "--list", str(archivo)], capture_output=True, text=True)
    if r.returncode != 0:
        return None, r.stderr.strip()
    conteo = {}
    for linea in r.stdout.splitlines():
        if linea.startswith(";") or not linea.strip():
            continue
        partes = linea.split()
        # formato: "id; oid oid TIPO esquema nombre dueño" (TIPO puede tener dos palabras: TABLE DATA)
        if len(partes) >= 4:
            tipo = partes[3] + (" " + partes[4] if partes[3] in ("TABLE", "SEQUENCE") and len(partes) > 4
                                 and partes[4] in ("DATA", "SET") else "")
            conteo[tipo] = conteo.get(tipo, 0) + 1
    return conteo, None


# ----------------------------------------------------------------------------
# Restauración de prueba (opcional)
# ----------------------------------------------------------------------------

def probar_restauracion(pg_restore, archivo, filas_esperadas, args):
    print("\n== Restauración de prueba en el Postgres local ==")
    pwd = os.environ.get("PG_LOCAL_PASSWORD") or getpass(f"Contraseña de Postgres local (usuario {args.local_user}): ")
    local = {"DB_HOST": args.local_host, "DB_PORT": args.local_port, "DB_NAME": "postgres",
             "DB_USER": args.local_user, "DB_PASSWORD": pwd}
    destino = args.local_db
    conn = conectar(local)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (destino,))
        if cur.fetchone():
            cur.execute(f'DROP DATABASE "{destino}"')
        cur.execute(f'CREATE DATABASE "{destino}"')
    conn.close()

    env = dict(os.environ, PGPASSWORD=pwd, PGSSLMODE="disable")
    cmd = [pg_restore, "-h", args.local_host, "-p", str(args.local_port), "-U", args.local_user,
           "-d", destino, "--no-owner", "--no-privileges", str(archivo)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    avisos = r.stderr.strip()

    conn = conectar(local, dbname=destino)
    with conn.cursor() as cur:
        _, filas_rest, objetos_rest = inventario(cur)
    conn.close()

    diferencias = {t: (n, filas_rest.get(t)) for t, n in filas_esperadas.items() if filas_rest.get(t) != n}
    ok = not diferencias
    print(f"Base restaurada: {destino} (en {args.local_host})")
    if avisos:
        print("Avisos de pg_restore (suelen ser inofensivos, p. ej. extensiones propias de Neon):")
        for l in avisos.splitlines()[:15]:
            print("   ", l)
    if ok:
        print(f"OK: las {len(filas_esperadas)} tablas tienen las mismas filas que producción.")
    else:
        print("ATENCIÓN: diferencias de filas (tabla: producción vs restaurada):")
        for t, (a, b) in diferencias.items():
            print(f"    {t}: {a} vs {b}")
    return {"base": destino, "ok": ok, "diferencias": diferencias,
            "objetos": objetos_rest, "avisos": avisos[:2000]}


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Respaldo de la base de producción (Neon).")
    ap.add_argument("--destino", default=str(BASE / "respaldos"), help="Carpeta donde guardar los respaldos")
    ap.add_argument("--solo-csv", action="store_true", help="No usar pg_dump (solo CSV + manifiesto)")
    ap.add_argument("--probar-restauracion", action="store_true",
                    help="Restaurar el .dump en el Postgres local y comparar filas")
    ap.add_argument("--local-host", default="localhost")
    ap.add_argument("--local-port", default="5432")
    ap.add_argument("--local-user", default="postgres")
    ap.add_argument("--local-db", default="almacen_restore_test")
    args = ap.parse_args()

    cfg = leer_config()
    if es_local(cfg["DB_HOST"]):
        print("Aviso: secrets.toml apunta a una base LOCAL, no a Neon. Se respalda esa base.")
    host_dump = host_directo(cfg["DB_HOST"])
    inicio = ahora_ar()
    carpeta = Path(args.destino) / f"prod_{inicio:%Y%m%d_%H%M}"
    carpeta.mkdir(parents=True, exist_ok=True)

    print(f"Conectando a {cfg['DB_NAME']} en {cfg['DB_HOST']} ...")
    conn = conectar(cfg)
    # Foto consistente: transacción de solo lectura, nivel REPEATABLE READ
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    with conn.cursor() as cur:
        cur.execute("SELECT version(), current_setting('server_version_num')::int, pg_database_size(current_database())")
        version_txt, version_num, tamano = cur.fetchone()
        tablas, filas, objetos = inventario(cur)
    servidor_mayor = version_num // 10000
    print(f"Servidor PostgreSQL {servidor_mayor} · {len(tablas)} tablas · {sum(filas.values()):,} filas · "
          f"{tamano / 1e6:.1f} MB")

    print("Exportando tablas a CSV ...")
    exportar_csv(conn, tablas, carpeta / "csv")
    conn.commit()
    conn.close()

    # --- pg_dump ---
    dump = carpeta / f"{cfg['DB_NAME']}.dump"
    dump_info = {"estado": "omitido"}
    pg_restore = None
    if not args.solo_csv:
        dumps = buscar_binario("pg_dump")
        aptos = [(v, r) for v, r in dumps if v >= servidor_mayor]
        if not aptos:
            hallado = ", ".join(f"v{v}" for v, _ in dumps) or "ninguno"
            dump_info = {"estado": "sin_pg_dump",
                         "detalle": f"Se necesita pg_dump {servidor_mayor} o mayor; encontrado: {hallado}"}
            print(f"\nATENCIÓN: {dump_info['detalle']}.")
            print(f"  Instalá PostgreSQL {servidor_mayor} (solo 'Command Line Tools') desde "
                  "https://www.postgresql.org/download/windows/ y volvé a correr el script.")
            print("  Mientras tanto quedó el respaldo en CSV.")
        else:
            version_dump, pg_dump = aptos[0]
            print(f"Corriendo pg_dump v{version_dump} contra {host_dump} ...")
            rc, err = correr_pg_dump(pg_dump, cfg, host_dump, dump)
            if rc != 0 and host_dump != cfg["DB_HOST"]:
                print("  Falló contra el endpoint directo; reintento por el pooler ...")
                rc, err = correr_pg_dump(pg_dump, cfg, cfg["DB_HOST"], dump)
            if rc != 0:
                dump_info = {"estado": "error", "detalle": err[:2000]}
                print("ERROR en pg_dump:\n", err)
            else:
                restores = [r for v, r in buscar_binario("pg_restore") if v >= version_dump]
                pg_restore = restores[0] if restores else None
                toc, err_toc = listar_dump(pg_restore, dump) if pg_restore else (None, "pg_restore no encontrado")
                datos_ok = toc is not None and toc.get("TABLE DATA", 0) == len(tablas)
                dump_info = {"estado": "ok" if datos_ok else "revisar",
                             "pg_dump": f"v{version_dump}", "host": host_dump,
                             "contenido": toc, "detalle": err_toc or err or ""}
                print(f"  Dump OK ({dump.stat().st_size / 1e6:.1f} MB). Contenido: {toc}")
                if not datos_ok:
                    print("  ATENCIÓN: la cantidad de tablas con datos en el dump no coincide; revisar.")

    # --- Restauración de prueba ---
    restauracion = None
    if args.probar_restauracion:
        if dump_info.get("estado") in ("ok", "revisar") and pg_restore:
            try:
                restauracion = probar_restauracion(pg_restore, dump, filas, args)
            except Exception as e:
                restauracion = {"ok": False, "error": str(e)}
                print("No se pudo hacer la restauración de prueba:", e)
        else:
            print("Restauración de prueba omitida: no hay .dump válido.")

    # --- Manifiesto ---
    archivos = {}
    for p in sorted(carpeta.rglob("*")):
        if p.is_file():
            archivos[str(p.relative_to(carpeta)).replace("\\", "/")] = {
                "bytes": p.stat().st_size, "sha256": sha256(p)}
    manifiesto = {
        "fecha": inicio.isoformat(timespec="seconds"),
        "base": cfg["DB_NAME"], "host": cfg["DB_HOST"], "usuario": cfg["DB_USER"],
        "servidor": version_txt, "tamano_base_bytes": tamano,
        "filas_por_tabla": filas, "total_filas": sum(filas.values()),
        "objetos": objetos, "pg_dump": dump_info, "restauracion_prueba": restauracion,
        "archivos": archivos,
    }
    with open(carpeta / "manifiesto.json", "w", encoding="utf-8") as f:
        json.dump(manifiesto, f, ensure_ascii=False, indent=2, default=str)

    lineas = [
        f"Respaldo de producción — {inicio:%d/%m/%Y %H:%M} (hora Argentina)",
        f"Base: {cfg['DB_NAME']} en {cfg['DB_HOST']}",
        f"Servidor: PostgreSQL {servidor_mayor} · tamaño {tamano / 1e6:.1f} MB",
        f"pg_dump: {dump_info.get('estado')} {dump_info.get('pg_dump', '')}",
        f"Restauración de prueba: {'OK' if restauracion and restauracion.get('ok') else ('con diferencias' if restauracion else 'no se hizo')}",
        "",
        "Filas por tabla:",
        *[f"  {t:<32}{n:>10,}" for t, n in filas.items()],
        f"  {'TOTAL':<32}{sum(filas.values()):>10,}",
        "",
        "Objetos: " + ", ".join(f"{k}={v}" for k, v in objetos.items()),
        "",
        "Cómo restaurar (en una base vacía, nunca sobre producción sin pensarlo dos veces):",
        "  createdb -U postgres almacen_restaurada",
        f"  pg_restore -U postgres -d almacen_restaurada --no-owner --no-privileges {dump.name}",
        "Los CSV de la carpeta csv/ se abren en Excel y sirven como respaldo legible.",
        "manifiesto.json guarda el SHA-256 de cada archivo para verificar que no se alteraron.",
        "Este respaldo contiene datos del negocio: no subirlo al repositorio.",
    ]
    (carpeta / "LEEME.txt").write_text("\n".join(lineas), encoding="utf-8")

    print("\n" + "\n".join(lineas[:5]))
    print(f"\nRespaldo guardado en: {carpeta}")
    estado_final = dump_info.get("estado") in ("ok",) or args.solo_csv
    if restauracion is not None and not restauracion.get("ok"):
        estado_final = False
    sys.exit(0 if estado_final else 1)


if __name__ == "__main__":
    main()
