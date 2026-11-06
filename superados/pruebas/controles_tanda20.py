"""
Controles de la tanda 20: hora argentina (las fechas y horas se guardan en UTC y se
muestran en hora argentina; "hoy" en SQL es el día argentino) y clase heredada de los
combos en la lista de compras. ESCRIBE en la base (historial, fraccionamiento, ventas de
un combo de prueba): usar solo una copia local.

Uso: python superados/pruebas/controles_tanda20.py <carpeta_codigo> <base>
"""
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

carpeta, base = os.path.abspath(sys.argv[1]), sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
config.usar_base(base)
sys.path.insert(0, carpeta); os.chdir(carpeta)
import pandas as pd  # noqa
from sqlalchemy import text  # noqa
from streamlit.testing.v1 import AppTest  # noqa
import db  # noqa
import lista_compras as lc  # noqa
import pronostico as pron  # noqa

AR = ZoneInfo("America/Argentina/Buenos_Aires")
fallas = 0


def ok(n, c, d=""):
    global fallas
    fallas += not c
    print(("OK    " if c else "FALLA ") + n + (f"  -- {d}" if d else ""))


def q(sql, **p):
    with db.engine.begin() as c:
        r = c.execute(text(sql), p)
        return r.all() if r.returns_rows else None


db.set_persona_actual(1, "Dueño")
ahora_ar = dt.datetime.now(AR).replace(tzinfo=None)
ahora_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

# ---------------- B5: hora argentina ----------------
ok("la sesión de la base está en UTC", q("SHOW TIME ZONE")[0][0] == "UTC")
ok("HOY_SQL es el día argentino", q(f"SELECT {db.HOY_SQL}")[0][0] == ahora_ar.date())
db.registrar_historial("articulos", 3, "EDICION", "prueba tanda 20: hora")
guardada = q("SELECT fecha FROM historial_cambios ORDER BY id DESC LIMIT 1")[0][0]
ok("el historial se guarda en UTC", abs((guardada - ahora_utc).total_seconds()) < 120, str(guardada))
h = db.listar_historial(desde=ahora_ar.date(), hasta=ahora_ar.date())
fila = h[h["detalle"] == "prueba tanda 20: hora"]
ok("listar_historial la muestra en hora argentina",
   len(fila) == 1 and abs((pd.Timestamp(fila["fecha"].iloc[0]).to_pydatetime() - ahora_ar).total_seconds()) < 120,
   str(fila["fecha"].tolist()))

# una corrección hecha a las 23:30 de ayer (hora argentina) = 02:30 UTC de hoy: cuenta para AYER
ayer = ahora_ar.date() - dt.timedelta(days=1)
utc_2330 = dt.datetime.combine(ayer, dt.time(23, 30)) + dt.timedelta(hours=3)
art = q("SELECT id, nombre, stock_actual FROM articulos WHERE activo AND categoria <> 'Promocion' "
        "AND COALESCE(reposicion_dias, 0) <> 1 ORDER BY id LIMIT 1")[0]
s0 = float(art[2])
q("INSERT INTO historial_cambios (fecha, tabla, registro_id, accion, detalle) VALUES (:f, 'articulos', :a, 'EDICION', :d)",
  f=utc_2330, a=art[0], d=f"Stock corregido a mano en '{art[1]}': {s0:.2f} -> {s0:.2f}")
q("INSERT INTO historial_cambios (fecha, tabla, registro_id, accion, detalle) VALUES (:f, 'articulos', :a, 'EDICION', :d)",
  f=utc_2330, a=art[0], d=f"Stock corregido a mano en '{art[1]}': {s0 + 5:.2f} -> {s0 + 10:.2f}")
mov = pron.movimientos_stock(db.engine)
m = mov[(mov["articulo_id"] == art[0]) & (mov["delta"] == 5)]
ok("movimientos_stock: la corrección de las 23:30 cae en el día argentino",
   len(m) == 1 and m["fecha"].iloc[0].date() == ayer, str(m["fecha"].tolist()))
h2 = db.listar_historial(desde=ayer, hasta=ayer)
ok("listar_historial: la de las 23:30 aparece en el día de ayer, no en el de hoy",
   (h2["registro_id"] == art[0]).any() and not (db.listar_historial(desde=ahora_ar.date(), hasta=ahora_ar.date())
                                                 ["detalle"].str.contains(f"{s0 + 5:.2f} ->", regex=False)).any())
sd = db.stock_diario_articulo(art[0], ayer - dt.timedelta(days=2))
fila_ayer = sd[pd.to_datetime(sd["fecha"]).dt.date == ayer]
ok("stock_diario_articulo: el ajuste queda en ayer",
   len(fila_ayer) == 1 and abs(float(fila_ayer["ajuste"].iloc[0]) - 5) < 1e-9, str(fila_ayer.to_dict("records"))[:200])
q("DELETE FROM historial_cambios WHERE fecha = :f AND registro_id = :a", f=utc_2330, a=art[0])

# fraccionamiento con el día argentino
bolsa = q("SELECT articulo_fraccion_de FROM articulos WHERE articulo_fraccion_de IS NOT NULL AND activo LIMIT 1")
if not bolsa:   # copia sin fraccionados: se crea uno
    b0 = q("SELECT id FROM articulos WHERE activo AND categoria <> 'Promocion' AND articulo_fraccion_de IS NULL "
           "AND unidad_medida = 'unidad' ORDER BY id LIMIT 1")[0][0]
    db.crear_producto_fraccionado(b0, "PRUEBA T20 SUELTO", "paquetito", 1)
    bolsa = [(b0,)]
if bolsa:
    b = bolsa[0][0]
    q("UPDATE articulos SET stock_actual = stock_actual + 1 WHERE id = :b", b=b)
    db.fraccionar(b, 1)
    f = q("SELECT fecha FROM fraccionamientos ORDER BY id DESC LIMIT 1")[0][0]
    ok("el fraccionamiento queda con el día argentino", f == ahora_ar.date(), str(f))

# ---------------- B3: clase heredada de los combos ----------------
pron_df = pd.DataFrame({"articulo_id": [1, 1, 2, 3, 4], "clase": ["C", "C", "B", "A", "B"],
                        "fecha_objetivo": [dt.date(2026, 10, 27), dt.date(2026, 10, 28)] + [dt.date(2026, 10, 27)] * 3,
                        "demanda": [1.0] * 5})
herencia = pd.DataFrame({"componente_articulo_id": [1, 1, 3, 4], "combo": ["PROMO X", "PROMO Y", "PROMO X", "PROMO Z"],
                         "clase_combo": ["B", "A", "B", "C"]})
d = lc.heredar_clase_combos(pron_df, herencia).drop_duplicates("articulo_id").set_index("articulo_id")
ok("componente C de un combo A pasa a A (el mejor de sus combos)", d.loc[1, "clase"] == "A" and d.loc[1, "clase_por"] == "PROMO Y")
ok("artículo sin combo queda igual", d.loc[2, "clase"] == "B" and d.loc[2, "clase_por"] is None)
ok("nunca baja de clase (A en un combo B sigue A; B en un combo C sigue B)",
   d.loc[3, "clase"] == "A" and d.loc[4, "clase"] == "B" and d.loc[3, "clase_por"] is None)
ok("guarda la clase propia", d.loc[1, "clase_propia"] == "C")
ok("sin combos no cambia nada", lc.heredar_clase_combos(pron_df, pd.DataFrame())["clase"].tolist() == pron_df["clase"].tolist())

# combo de prueba en la base: muchas ventas del combo, componente de poca venta propia
comp = q("""SELECT a.id FROM articulos a WHERE a.activo AND a.categoria NOT IN ('Promocion') AND a.stock_actual > 0
            AND COALESCE(a.reposicion_dias, 0) <> 1 AND a.articulo_fraccion_de IS NULL
            AND NOT EXISTS (SELECT 1 FROM ventas v WHERE v.articulo_id = a.id) ORDER BY a.id LIMIT 1""")[0][0]
combo = q("""INSERT INTO articulos (nombre, categoria, costo_compra_actual, margen_ganancia, iva, stock_actual,
                                   unidad_medida, promo_tipo, activo)
             VALUES ('PROMO PRUEBA T20', 'Promocion', 1000, 0.3, 0.21, 0, 'unidad', 'virtual', TRUE) RETURNING id""")[0][0]
q("INSERT INTO promocion_componentes (promocion_articulo_id, componente_articulo_id, cantidad_por_unidad) VALUES (:p, :c, 1)",
  p=combo, c=comp)
hoy = ahora_ar.date()
for k in range(1, 30):
    t = q("INSERT INTO ventas_ticket (fecha, medio_pago) VALUES (:f, 'Efectivo') RETURNING id",
          f=hoy - dt.timedelta(days=k))[0][0]
    q("""INSERT INTO ventas (articulo_id, cantidad, precio_unitario, monto_total, medio_pago, fecha, ticket_id,
                             es_consumo_interno) VALUES (:a, 10, 50000, 500000, 'Efectivo', :f, :t, FALSE)""",
      a=combo, f=hoy - dt.timedelta(days=k), t=t)
cc = db.clase_combos(hoy - dt.timedelta(days=1))
fila = cc[cc["combo_id"] == combo]
ok("clase_combos: el combo que más vende es clase A", len(fila) == 1 and fila["clase_combo"].iloc[0] == "A", str(fila.to_dict("records")))
ok("clase_combos: lista su componente", len(fila) == 1 and fila["componente_articulo_id"].iloc[0] == comp)

# la Lista de compras abre y muestra el aviso en el componente (si está en la lista)
at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=300)
at.session_state["persona_id"] = 1
at.session_state["pagina_nav"] = "🛒 Lista de compras"
at.run()
ok("Lista de compras abre sin errores", not at.exception, str([e.value[:300] for e in at.exception]))
at2 = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=300)
at2.session_state["persona_id"] = 1
at2.session_state["pagina_nav"] = "📜 Historial de Cambios"
at2.run()
ok("Historial abre sin errores", not at2.exception, str([e.value[:300] for e in at2.exception]))
tabla = at2.dataframe[0].value if len(at2.dataframe) else pd.DataFrame()
ok("Historial: la última fila tiene la hora argentina",
   not tabla.empty and abs((pd.Timestamp(tabla["Fecha"].iloc[0]).to_pydatetime() - ahora_ar).total_seconds()) < 600,
   str(tabla["Fecha"].head(2).tolist()) if not tabla.empty else "")
sys.exit(1 if fallas else 0)
