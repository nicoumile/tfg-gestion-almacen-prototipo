"""
Deja lista una base de DEMOSTRACIÓN (Entregable 4) para quien instala el prototipo.

La base demo (base_demo_entregable4.dump) es la base simulada oficial del
Entregable 3 (12 meses, del 05/10/2025 al 04/10/2026) cargada sobre el esquema
actual. Este script, que se corre una vez después de restaurarla:

1. Corre todas las fechas de la base hacia adelante para que la última venta
   quede ayer. Sin esto, quien la abra meses después ve un mes sin ventas y
   el pronóstico da casi todo cero. Todas las fechas se mueven juntas, así que
   el patrón semanal de la demanda se conserva (solo cambia en qué día de la
   semana caen los picos de la simulación). Los disparadores no se ejecutan:
   el stock y los costos quedan como estaban.
2. Crea (o actualiza) el usuario de demostración, con rol dueño.
3. Carga datos de ejemplo, también simulados (semilla fija), para las
   pantallas que la simulación del pronóstico deja vacías: los gastos fijos
   de cada mes (alquiler, servicios, impuestos, mantenimiento), con la
   descripción "Dato de ejemplo", para Gastos y Reportes; y un segundo precio
   de proveedor para parte de los artículos de almacén, para comparar
   proveedores en Proveedores → Precios. No toca ventas, compras ni stock.

Se puede correr más de una vez: si las fechas ya están al día, no las mueve.
Se niega a correr contra la base de producción (Neon).

Uso (con .streamlit/secrets.toml apuntando a la base demo):
    python preparar_demo.py
    python preparar_demo.py --usuario demo --clave <contraseña de 8 o más caracteres>
"""
import argparse
import datetime as dt
import random
import sys
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text

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


def recortar_marcas_futuras(conn):
    """Las marcas de registro (creado_en, actualizado_en...) se grabaron cuando se armó la base;
    al correr las fechas pueden quedar después de hoy. Se llevan a ahora. Las fechas de negocio
    (ventas, compras, vencimientos) no se tocan."""
    conn.execute(text("SET session_replication_role = replica"))
    for tabla, col, unica in columnas_fecha(conn):
        if col.endswith("_en") and not unica:
            conn.execute(text(f'UPDATE "{tabla}" SET "{col}" = localtimestamp WHERE "{col}" > localtimestamp'))
    conn.execute(text("SET session_replication_role = DEFAULT"))


def _redondear(x, paso=100):
    return round(x / paso) * paso


def cargar_ejemplos(conn, hoy):
    """Gastos mensuales y segundos precios de proveedor de ejemplo (ver paso 3 del docstring).
    Solo carga lo que falta: si ya hay gastos o artículos con dos proveedores, no los toca."""
    rnd = random.Random(42)
    ayer = hoy - dt.timedelta(days=1)

    if not conn.execute(text("SELECT EXISTS (SELECT 1 FROM gastos)")).scalar():
        primera = conn.execute(text("SELECT MIN(fecha) FROM ventas")).scalar()
        mes, n, filas = primera.replace(day=1), 0, []
        while mes <= ayer:
            infl = 1.025 ** n  # suba mensual de precios
            fijos = [(5, "Alquiler", 1_150_000, "Alquiler del local"),
                     (15, "Servicios (luz/agua/gas)", 300_000 * rnd.uniform(0.85, 1.25), "Luz, agua y gas"),
                     (20, "Impuestos", 170_000, "Monotributo e ingresos brutos"),
                     (rnd.randint(2, 27), "Otro", rnd.uniform(25_000, 60_000), "Artículos de limpieza del local")]
            if rnd.random() < 1 / 3:
                fijos.append((rnd.randint(2, 27), "Mantenimiento", rnd.uniform(60_000, 250_000),
                              rnd.choice(["Service de la heladera", "Arreglo de la persiana",
                                          "Cambio de luces", "Arreglo de la balanza"])))
            for dia, cat, monto, desc in fijos:
                f = mes.replace(day=dia)
                if primera <= f <= ayer:
                    filas.append({"f": f, "c": cat, "m": _redondear(monto * infl), "d": f"{desc} (dato de ejemplo)"})
            mes, n = (mes.replace(day=28) + dt.timedelta(days=4)).replace(day=1), n + 1
        conn.execute(text("INSERT INTO gastos (fecha, categoria, monto, descripcion) VALUES (:f, :c, :m, :d)"), filas)
        print(f"Gastos de ejemplo: {len(filas)} (alquiler, servicios, impuestos, mantenimiento y otros).")

    hay_dos = conn.execute(text("SELECT EXISTS (SELECT 1 FROM articulo_proveedor_precio "
                                "GROUP BY articulo_id HAVING COUNT(*) > 1)")).scalar()
    ids = dict(conn.execute(text("SELECT nombre, id FROM proveedores WHERE nombre IN "
                                 "('BARBOSA HNOS S.R.L.', 'EL TRIO DISTRIBUCIONES')")).all())
    if not hay_dos and len(ids) == 2:
        otro = {ids["BARBOSA HNOS S.R.L."]: ids["EL TRIO DISTRIBUCIONES"],
                ids["EL TRIO DISTRIBUCIONES"]: ids["BARBOSA HNOS S.R.L."]}
        precios = conn.execute(text("SELECT articulo_id, proveedor_id, costo_compra FROM articulo_proveedor_precio "
                                    "WHERE proveedor_id IN :p AND costo_compra > 0 ORDER BY articulo_id")
                               .bindparams(bindparam("p", expanding=True)), {"p": list(otro)}).all()
        filas = [{"a": a, "p": otro[p], "c": round(float(c) * rnd.uniform(0.93, 1.09), 2),
                  "t": dt.datetime.combine(ayer - dt.timedelta(days=rnd.randint(0, 30)), dt.time(10))}
                 for a, p, c in precios if rnd.random() < 0.4]
        if filas:
            conn.execute(text("INSERT INTO articulo_proveedor_precio (articulo_id, proveedor_id, costo_compra, "
                              "actualizado_en) VALUES (:a, :p, :c, :t)"), filas)
        print(f"Precios de ejemplo de un segundo proveedor: {len(filas)} artículos.")


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
            sys.exit("La base no tiene ventas: restaurá primero datos/base_demo_entregable4.dump.")
        dias = ((hoy - dt.timedelta(days=1)) - ultima).days
        if dias > 0:
            correr_fechas(conn, dias)
            print(f"Fechas corridas {dias} días: la última venta pasó del {ultima:%d/%m/%Y} "
                  f"al {ultima + dt.timedelta(days=dias):%d/%m/%Y}.")
        else:
            print(f"Las fechas ya están al día (última venta: {ultima:%d/%m/%Y}).")
        recortar_marcas_futuras(conn)

        dueno = conn.execute(text("SELECT id FROM personas WHERE rol = 'dueño' ORDER BY id LIMIT 1")).scalar()
        datos = {"n": NOMBRE_DEMO, "u": args.usuario.strip().lower(), "h": db.hash_clave(args.clave)}
        if dueno is None:
            conn.execute(text("INSERT INTO personas (nombre, usuario, rol, clave_hash) VALUES (:n, :u, 'dueño', :h)"), datos)
        else:
            conn.execute(text("UPDATE personas SET nombre = :n, usuario = :u, clave_hash = :h, activo = TRUE "
                              "WHERE id = :id"), {**datos, "id": dueno})
        cargar_ejemplos(conn, hoy)
    print(f"Listo. Usuario '{datos['u']}' (dueño). Ejecutá: streamlit run app.py")


if __name__ == "__main__":
    main()
