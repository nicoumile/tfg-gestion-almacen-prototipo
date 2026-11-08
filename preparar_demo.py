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
4. Suma dos artículos con la configuración que tienen en el negocio real (respaldo
   de producción del 06/11/2026), con su historial simulado desde la primera
   venta de la base y cargado con los mismos disparadores que usa la app:
   - PAN NEWEN 500G, de compra diaria (vida útil 1 día, servicio 80 %): cada día
     se compra la venta esperada de ese día de la semana más el margen del
     nivel de servicio, y lo que sobra se registra como sobrante, igual que en
     la pantalla Registrar Venta.
   - MILANESAS DE POLLO CIPOLLEÑAS (vida útil 7 días, servicio 80 %): se
     compran una vez por semana, en bultos de 10, para cubrir la venta esperada
     de la semana con el margen del nivel de servicio, y lo que vence se
     registra como vencido.
   La demanda de cada día de la semana sale de la venta real de octubre de 2026.
   Las milanesas ya estaban en el catálogo de la simulación con muy pocas ventas:
   se reemplazan sus ventas y compras por las simuladas con la configuración real.

Se puede correr más de una vez: si las fechas ya están al día, no las mueve.
Se niega a correr contra la base de producción (Neon).

Uso (con .streamlit/secrets.toml apuntando a la base demo):
    python preparar_demo.py
    python preparar_demo.py --usuario demo --clave <contraseña de 8 o más caracteres>
"""
import argparse
import datetime as dt
import math
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

    simular_articulos_reales(conn, rnd, ayer)


# Configuración real (respaldo de producción del 06/11/2026) y venta media por día de la
# semana (lunes = 0) medida en esas semanas.
ARTICULOS_REALES = [
    dict(nombre="PAN NEWEN 500G", categoria="Almacén", marca=None, costo=1000.00, margen=0.65289256,
         iva=0.21, reposicion=1, vida=1, servicio=0.80, proveedor="PANADERIA NEWEN",
         media={0: 9.0, 1: 10.0, 2: 7.25, 3: 9.25, 4: 7.75, 5: 7.0, 6: 10.25}, k=22.0),
    dict(nombre="MILANESAS DE POLLO CIPOLLEÑAS", categoria="Fiambres y lácteos", marca="CIPOLLEÑAS",
         costo=3850.00, margen=0.39529892, iva=0.21, reposicion=None, vida=7, servicio=0.80,
         proveedor="MILANESAS CIPOLLEÑAS",
         media={0: 2.17, 1: 1.33, 2: 1.17, 3: 2.0, 4: 1.6, 5: 1.2, 6: 1.0}, k=None),
]
Z_80 = 0.8416  # nivel de servicio 80 %
MEDIOS = (("Efectivo", 0.70), ("Tarjeta", 0.20), ("Transferencia", 0.10))


def _poisson(rnd, lam):
    if lam <= 0:
        return 0
    lim, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= lim:
            return k
        k += 1


def _demanda(rnd, art, dia):
    m = art["media"][dia.weekday()]
    if art["k"]:  # binomial negativa: más variable que Poisson, como el pan real
        m *= rnd.gammavariate(art["k"], 1 / art["k"])
    return _poisson(rnd, m)


def simular_articulos_reales(conn, rnd, ayer):
    """Paso 4 del docstring. Solo crea los artículos que no existan."""
    primera = conn.execute(text("SELECT MIN(fecha) FROM ventas")).scalar()
    quien = conn.execute(text("SELECT id FROM personas WHERE rol = 'dueño' ORDER BY id LIMIT 1")).scalar()
    for art in ARTICULOS_REALES:
        prov = conn.execute(text("SELECT id FROM proveedores WHERE nombre = :n"), {"n": art["proveedor"]}).scalar()
        if prov is None:
            prov = conn.execute(text("INSERT INTO proveedores (nombre) VALUES (:n) RETURNING id"),
                                {"n": art["proveedor"]}).scalar()
        cfg = {"n": art["nombre"], "c": art["categoria"], "m": art["marca"], "co": art["costo"],
               "mg": art["margen"], "iva": art["iva"], "r": art["reposicion"], "v": art["vida"],
               "s": art["servicio"]}
        aid = conn.execute(text("SELECT id FROM articulos WHERE nombre = :n"), {"n": art["nombre"]}).scalar()
        if aid is not None:
            ya = conn.execute(text("SELECT 1 FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id "
                                   "WHERE d.articulo_id = :a AND f.proveedor_id = :p LIMIT 1"),
                              {"a": aid, "p": prov}).scalar()
            if ya:
                continue  # ya simulado en una corrida anterior
            # Viene del catálogo con otro historial: se reemplaza por el simulado con la configuración real.
            facts = conn.execute(text("SELECT DISTINCT factura_id FROM detalle_factura WHERE articulo_id = :a"),
                                 {"a": aid}).scalars().all()
            tickets = conn.execute(text("SELECT DISTINCT ticket_id FROM ventas WHERE articulo_id = :a "
                                        "AND ticket_id IS NOT NULL"), {"a": aid}).scalars().all()
            conn.execute(text("DELETE FROM ventas WHERE articulo_id = :a"), {"a": aid})
            conn.execute(text("DELETE FROM detalle_factura WHERE articulo_id = :a"), {"a": aid})
            if facts:
                conn.execute(text("DELETE FROM facturas f WHERE f.id IN :ids AND NOT EXISTS "
                                  "(SELECT 1 FROM detalle_factura d WHERE d.factura_id = f.id)")
                             .bindparams(bindparam("ids", expanding=True)), {"ids": list(facts)})
            if tickets:
                conn.execute(text("DELETE FROM ventas_ticket t WHERE t.id IN :ids AND NOT EXISTS "
                                  "(SELECT 1 FROM ventas v WHERE v.ticket_id = t.id)")
                             .bindparams(bindparam("ids", expanding=True)), {"ids": list(tickets)})
            conn.execute(text("DELETE FROM articulo_proveedor_precio WHERE articulo_id = :a"), {"a": aid})
            conn.execute(text("""
                UPDATE articulos SET categoria = :c, marca = :m, costo_compra_actual = :co, margen_ganancia = :mg,
                       iva = :iva, stock_actual = 0, reposicion_dias = :r, vida_util_dias = :v, nivel_servicio = :s,
                       activo = TRUE
                WHERE id = :id
            """), {**cfg, "id": aid})
        else:
            aid = conn.execute(text("""
                INSERT INTO articulos (nombre, categoria, marca, costo_compra_actual, margen_ganancia, iva,
                                       stock_actual, stock_minimo, unidad_medida, reposicion_dias,
                                       vida_util_dias, nivel_servicio)
                VALUES (:n, :c, :m, :co, :mg, :iva, 0, 0, 'unidad', :r, :v, :s) RETURNING id
            """), cfg).scalar()
        conn.execute(text("INSERT INTO articulo_proveedor_precio (articulo_id, proveedor_id, costo_compra) "
                          "VALUES (:a, :p, :c)"), {"a": aid, "p": prov, "c": art["costo"]})
        precio = round(art["costo"] * (1 + art["margen"]) * (1 + art["iva"]))
        diaria = art["reposicion"] == 1
        marca = f"[art:{aid}]"

        def comprar(dia, cant, vence):
            ref = f"diaria:{aid}:{dia}" if diaria else None
            fid = conn.execute(text("INSERT INTO facturas (proveedor_id, numero, fecha, origen, ref_externa) "
                                    "VALUES (:p, :n, :f, 'manual', :r) RETURNING id"),
                               {"p": prov, "n": (f"Compra diaria {dia}" if diaria else f"E-{dia:%Y%m%d}"),
                                "f": dia, "r": ref}).scalar()
            conn.execute(text("INSERT INTO detalle_factura (factura_id, articulo_id, cantidad, costo_unitario, "
                              "actualiza_precio, fecha_vencimiento) VALUES (:f, :a, :c, :co, :ap, :v)"),
                         {"f": fid, "a": aid, "c": cant, "co": art["costo"], "ap": not diaria, "v": vence})

        def merma(dia, cant, medio, texto):
            tid = conn.execute(text("INSERT INTO ventas_ticket (fecha, medio_pago) VALUES (:f, :m) RETURNING id"),
                               {"f": dia, "m": medio}).scalar()
            conn.execute(text("INSERT INTO ventas (articulo_id, cantidad, precio_unitario, monto_total, medio_pago, "
                              "fecha, ticket_id, es_consumo_interno, registrado_por) "
                              "VALUES (:a, :c, 0, 0, :m, :f, :t, TRUE, :q)"),
                         {"a": aid, "c": cant, "m": medio, "f": dia, "t": tid, "q": quien})
            conn.execute(text("INSERT INTO gastos (fecha, categoria, descripcion, monto) VALUES (:f, :cat, :d, :mo)"),
                         {"f": dia, "cat": medio, "mo": round(art["costo"] * cant, 2),
                          "d": f"{texto}: {cant:g} x {art['nombre']} {marca}"})

        def vender(dia, cant):
            while cant > 0:
                q = min(cant, rnd.choice((1, 1, 1, 2, 2, 3)))
                medio = rnd.choices([m for m, _ in MEDIOS], [w for _, w in MEDIOS])[0]
                tid = conn.execute(text("INSERT INTO ventas_ticket (fecha, medio_pago) VALUES (:f, :m) RETURNING id"),
                                   {"f": dia, "m": medio}).scalar()
                conn.execute(text("INSERT INTO ventas (articulo_id, cantidad, precio_unitario, monto_total, "
                                  "medio_pago, fecha, ticket_id, es_consumo_interno, registrado_por) "
                                  "VALUES (:a, :c, :p, :mt, :m, :f, :t, FALSE, :q)"),
                             {"a": aid, "c": q, "p": precio, "mt": precio * q, "m": medio, "f": dia, "t": tid,
                              "q": quien})
                cant -= q

        lotes, dia, vendidas, compradas, perdidas = [], primera, 0, 0, 0  # lotes: [vence, cantidad]
        while dia <= ayer:
            if diaria:
                m = art["media"][dia.weekday()]
                compra = round(m + Z_80 * math.sqrt(m + m * m / art["k"]))
                comprar(dia, compra, dia + dt.timedelta(days=1))
                venta = min(_demanda(rnd, art, dia), compra)
                vender(dia, venta)
                if compra > venta:
                    merma(dia, compra - venta, "Sobrante", "Sobrante regalado/tirado")
                vendidas, compradas, perdidas = vendidas + venta, compradas + compra, perdidas + compra - venta
            else:
                vencido = sum(c for v, c in lotes if v <= dia)
                if vencido:
                    merma(dia, vencido, "Vencido", "Vencido (tirado)")
                    perdidas += vencido
                lotes = [[v, c] for v, c in lotes if v > dia]
                if (dia - primera).days % 7 == 0:  # una compra por semana
                    m7 = sum(art["media"].values())
                    objetivo = m7 + Z_80 * math.sqrt(m7)
                    stock = sum(c for _, c in lotes)
                    if stock < objetivo:
                        # bultos de 10, lo más cerca posible de lo que falta: con 7 días de vida
                        # útil, redondear para arriba haría vencer casi la mitad
                        cant = 10 * max(1, round((objetivo - stock) / 10))
                        vence = dia + dt.timedelta(days=art["vida"])
                        comprar(dia, cant, vence)
                        lotes.append([vence, cant])
                        compradas += cant
                pedido = min(_demanda(rnd, art, dia), sum(c for _, c in lotes))
                vender(dia, pedido)
                vendidas += pedido
                for lote in lotes:  # sale primero lo que vence antes
                    usar = min(lote[1], pedido)
                    lote[1] -= usar
                    pedido -= usar
                lotes = [lt for lt in lotes if lt[1] > 0]
            dia += dt.timedelta(days=1)
        print(f"{art['nombre']}: historial simulado desde el {primera:%d/%m/%Y}: compradas {compradas}, "
              f"vendidas {vendidas}, {'sobrante' if diaria else 'vencidas'} {perdidas}.")


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
