"""
Controles de la tanda 18: alertas con binomial negativa, vencimientos por lote implícito
(días disponibles por categoría, fecha opcional en la factura, riesgo de vencer) y motivo
"Vencido" en Sobrante del día. La base tiene que tener migracion_tanda18.py aplicada.
ESCRIBE en la base (facturas, vencidos, días por categoría): usar solo una copia local.

Uso: python pruebas/controles_tanda18.py <carpeta_codigo> <base>
"""
import math
import os
import sys

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

fallas = 0


def ok(n, c, d=""):
    global fallas
    fallas += not c
    print(("OK    " if c else "FALLA ") + n + (f"  -- {d}" if d else ""))


def q(sql, **p):
    with db.engine.connect() as c:
        return c.execute(text(sql), p).all()


def stock(aid):
    return float(q("SELECT stock_actual FROM articulos WHERE id = :a", a=aid)[0][0])


# ---------------- 1. Probabilidad de agotarse ----------------
def poisson_viejo(stock_, media):
    n = int(math.floor(max(stock_, 0.0) + 1e-9))
    t = acc = math.exp(-media)
    for k in range(1, n + 1):
        t *= media / k
        acc += t
    return max(0.0, 1 - acc)


casos = [(s, m) for s in (0, 1, 2, 3, 5, 8, 12) for m in (0.3, 1.0, 2.5, 4.0, 9.0)]
ok("con dispersión 1 da exactamente la Poisson de antes",
   max(abs(lc._prob_agotar(s, m, 1, False, 1.0) - poisson_viejo(s, m)) for s, m in casos) < 1e-12)
ok("con dispersión > 1 el riesgo con colchón es mayor (colas más pesadas)",
   all(lc._prob_agotar(s, m, 1, False, 1.3) >= poisson_viejo(s, m) - 1e-12 for s, m in casos if s > m + 1))
# media y varianza de la binomial negativa implícita (por suma de la distribución)
m, d = 3.0, 1.35
pmf = [lc._prob_agotar(k - 1, m, 1, False, d) - lc._prob_agotar(k, m, 1, False, d) for k in range(0, 200)]
pmf[0] = 1 - lc._prob_agotar(0, m, 1, False, d)
media = sum(k * p for k, p in enumerate(pmf))
var = sum((k - media) ** 2 * p for k, p in enumerate(pmf))
ok("binomial negativa: media y varianza = media y dispersión² x media",
   abs(media - m) < 1e-6 and abs(var - d * d * m) < 1e-4, f"media {media:.4f} var {var:.4f}")

# alertas: sin dispersión, igual que antes; con dispersión no tira error
hoy = pd.Timestamp(q("SELECT CURRENT_DATE")[0][0])
ev = pron.evaluar(db.engine, hoy=str(hoy.date()))
pron_df = pron.pronostico_vigente(db.engine, horizonte=14, hoy=str(hoy.date()), eleccion=ev["eleccion"])
meta = lc.datos_articulos(db.engine)
params = db.obtener_parametros()
t0, _ = lc.alertas_quiebre(pron_df, meta, params, hoy.date(), (hoy + pd.Timedelta(days=4)).date())
t1, _ = lc.alertas_quiebre(pron_df, meta, params, hoy.date(), (hoy + pd.Timedelta(days=4)).date(), {})
ok("alertas con dispersión vacía = sin dispersión", t0.equals(t1))
t2, _ = lc.alertas_quiebre(pron_df, meta, params, hoy.date(), (hoy + pd.Timedelta(days=4)).date(),
                           {"A": 1.3, "B": 1.3, "C": 1.3})
col = "Prob. de agotarse antes del viaje"
mm = t0.set_index("articulo_id")[col].to_frame("p0").join(t2.set_index("articulo_id")[col].to_frame("p2"))
ok("alertas con dispersión 1,3: mismos artículos, probabilidades distintas",
   len(t0) == len(t2) and (mm["p0"] != mm["p2"]).any(), f"{len(t0)} artículos")

# ---------------- 2. Motivo Vencido ----------------
aid = int(q("SELECT id FROM articulos WHERE activo AND stock_actual >= 3 AND categoria <> 'Promocion' "
            "AND costo_compra_actual > 0 ORDER BY id LIMIT 1")[0][0])
s0 = stock(aid)
dia = (hoy - pd.Timedelta(days=1)).date()
h = db.registrar_vencido(aid, dia, 2)
ok("registrar vencido baja el stock", abs(stock(aid) - (s0 - 2)) < 1e-9 and h, str(h))
g = q("SELECT monto FROM gastos WHERE categoria = 'Vencido' AND fecha = :f AND descripcion LIKE :m",
      f=dia, m=f"%[art:{aid}]")
ok("y deja un gasto 'Vencido' a costo", len(g) == 1 and float(g[0][0]) > 0, str(g))
db.registrar_vencido(aid, dia, 1)
ok("guardar de nuevo reemplaza (no suma)", abs(stock(aid) - (s0 - 1)) < 1e-9)
ok("vencido_del_dia lo lee", abs(db.vencido_del_dia(aid, dia) - 1) < 1e-9)
merma = db.merma_vencido(dia, dia)
ok("merma_vencido lo cuenta", aid in set(merma["articulo_id"]))
v, a = pron.cargar_insumos(db.engine)
ok("no cuenta como demanda para el pronóstico",
   v[(v["articulo_id"] == aid) & (pd.to_datetime(v["fecha"]).dt.date == dia)]["cantidad"].sum()
   == float(q("SELECT COALESCE(SUM(cantidad),0) FROM ventas WHERE articulo_id = :a AND fecha = :f "
              "AND medio_pago <> 'Vencido'", a=aid, f=dia)[0][0]))
ult = q("SELECT MAX(fecha) FROM v_ventas_analitica WHERE articulo_id = :a AND medio_pago IS DISTINCT FROM 'Vencido'",
        a=aid)[0][0]
inm = db.stock_inmovilizado(1)
fila = inm[inm["articulo_id"] == aid]
ok("no cuenta como movimiento en el stock inmovilizado",
   (fila.empty and ult is not None and pd.Timestamp(ult).date() >= (hoy - pd.Timedelta(days=1)).date())
   or (not fila.empty and (pd.isna(fila["fecha_ultima_venta"].iloc[0])
                           or pd.Timestamp(fila["fecha_ultima_venta"].iloc[0]).date() == pd.Timestamp(ult).date())),
   f"última venta real {ult}")
db.registrar_vencido(aid, dia, 0)
ok("poner 0 lo borra y repone el stock", abs(stock(aid) - s0) < 1e-9
   and not q("SELECT 1 FROM gastos WHERE categoria='Vencido' AND fecha=:f AND descripcion LIKE :m",
             f=dia, m=f"%[art:{aid}]"))
cierre = db.cierre_de_caja(dia) if hasattr(db, "cierre_de_caja") else None
ok("'Vencido' es gasto de mercadería (no resta del neto de caja)", "Vencido" in db.CATEGORIAS_GASTO_MERCADERIA)

# ---------------- 3. Vencimientos por lote ----------------
cat = q("SELECT categoria FROM articulos WHERE categoria = 'Fiambres y lácteos' LIMIT 1")
cat = cat[0][0] if cat else q("SELECT categoria FROM articulos WHERE activo AND categoria <> 'Promocion' LIMIT 1")[0][0]
db.guardar_vida_util_categorias({cat: 10})
ok("días por categoría guardados", db.vida_util_por_categoria().get(cat) == 10)
L = db.lotes_vencimiento()
st_ = pd.read_sql("SELECT id AS articulo_id, stock_actual::float AS s FROM articulos", db.engine)
cmp_ = L.groupby("articulo_id")["quedan"].sum().reset_index().merge(st_, on="articulo_id")
ok("lotes: en cada artículo, lo que queda en sus lotes = su stock", (cmp_["quedan"] - cmp_["s"]).abs().max() < 1e-9,
   f"{len(L)} lotes, {L['articulo_id'].nunique()} artículos")
ok("lotes: los de la categoría tienen fecha estimada = compra + 10",
   ((L[L["fuente"] == "estimada"]["vence"] - L[L["fuente"] == "estimada"]["fecha_compra"]).dt.days == 10).all())
# factura con fecha real en un renglón de un artículo de esa categoría
art_c = int(q("SELECT id FROM articulos WHERE activo AND categoria = :c ORDER BY id LIMIT 1", c=cat)[0][0])
prov = int(q("SELECT id FROM proveedores ORDER BY id LIMIT 1")[0][0])
venc_real = (hoy + pd.Timedelta(days=3)).date()
s0 = stock(art_c)
fid, _ = db.crear_factura(prov, "PRUEBA-T18", hoy.date(), [
    {"articulo_id": art_c, "cantidad": 4, "costo_unitario": 100.0, "actualiza_precio": False,
     "fecha_vencimiento": venc_real}])
ok("factura con fecha de vencimiento en el renglón: se guarda",
   q("SELECT fecha_vencimiento FROM detalle_factura WHERE factura_id = :f", f=fid)[0][0] == venc_real)
L = db.lotes_vencimiento()
lote = L[(L["articulo_id"] == art_c) & (L["factura_id"] == fid)]
ok("el lote nuevo usa la fecha de la factura y es el último en venderse",
   len(lote) == 1 and lote["fuente"].iloc[0] == "factura" and abs(lote["quedan"].iloc[0] - 4) < 1e-9
   and abs(lote["anteriores"].iloc[0] - s0) < 1e-9, lote[["quedan", "anteriores", "vence", "fuente"]].to_dict("records"))
R = lc.riesgo_vencimiento(L, pron_df, {})
r = R[(R["articulo_id"] == art_c) & (R["factura_id"] == fid)].iloc[0]
ok("riesgo del lote: probabilidad entre 0 y 1 y unidades entre 0 y lo que queda",
   0 <= r["prob_sobra"] <= 1 and 0 <= r["vencerian"] <= 4 + 1e-9, f"p={r['prob_sobra']:.3f} u={r['vencerian']:.2f}")
venc_lotes = R[R["dias_para_vencer"] < 0]
ok("lotes ya vencidos según la fecha: se tiraría todo lo que queda",
   venc_lotes.empty or ((venc_lotes["vencerian"] - venc_lotes["quedan"]).abs() < 1e-9).all())
# lista de compras: con días de categoría < 7, el horizonte se corta
db.guardar_vida_util_categorias({cat: 3})
meta3 = lc.datos_articulos(db.engine)
ok("lista de compras: vida útil efectiva toma la de la categoría",
   (meta3[meta3["categoria"] == cat]["vida_util_efectiva"].dropna() == 3).all()
   and meta3[meta3["categoria"] == cat]["vida_util_dias"].isna().any())
_, viaje3 = lc.calcular(pron_df, meta3, params, {})
av = viaje3[viaje3["Aviso"].str.contains("vence en 3 días", na=False)]
ok("lista de compras: avisa 'vence en 3 días' en esa categoría", not av.empty or viaje3[viaje3["articulo_id"].isin(
    meta3[meta3["categoria"] == cat]["articulo_id"])].empty, f"{len(av)} renglones")
db.guardar_vida_util_categorias({cat: None})
ok("quitar los días de la categoría", cat not in db.vida_util_por_categoria())
with db.engine.begin() as c:
    c.execute(text("DELETE FROM facturas WHERE id = :f"), {"f": fid})
ok("borrar la factura de prueba repone el stock", abs(stock(art_c) - s0) < 1e-9)

# ---------------- 4. Pantallas ----------------
db.guardar_vida_util_categorias({cat: 10})
for pag in ["📊 Dashboard", "⚙️ Configuración", "💵 Registrar Venta", "🧾 Cargar Factura (Compra)"]:
    at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=300)
    at.session_state["persona_id"] = 1
    at.session_state["pagina_nav"] = pag
    at.run()
    ok(f"abre {pag}", not at.exception, str([e.value[:300] for e in at.exception]))
    if pag == "📊 Dashboard":
        ok("tablero: pestaña de vencimientos", any("Vencimientos" in t.label for t in at.tabs))
    if pag == "💵 Registrar Venta":
        at.segmented_control(key="seccion_venta").set_value("🗑️ Sobrante del día").run()
        at.radio(key="sob_motivo").set_value("Venció (se tiró)").run()
        ok("sobrante: motivo 'Venció' sin errores", not at.exception, str([e.value[:300] for e in at.exception]))
        at.selectbox(key="sob_art").set_value(aid).run()
        ok("sobrante vencido: elige artículo y muestra el botón",
           not at.exception and any("Guardar vencido" in b.label for b in at.button))
db.guardar_vida_util_categorias({cat: None})

# ---------------- 5. Cargar Factura: columna "Vence" ----------------
db.guardar_vida_util_categorias({cat: 10})
arts2 = [int(r[0]) for r in q("SELECT id FROM articulos WHERE activo AND categoria = :c ORDER BY id LIMIT 2", c=cat)]
at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=300)
at.session_state["persona_id"] = 1
at.session_state["pagina_nav"] = "🧾 Cargar Factura (Compra)"
at.session_state["items_factura"] = [
    {"articulo_id": a_, "nombre": f"art {a_}", "cantidad": 2.0, "costo_unitario": 100.0, "actualiza_precio": False}
    for a_ in arts2]
at.run()
at.radio(key="modo_cargar_factura").set_value("✍️ A mano").run()
at.text_input(key="fac_numero").set_value("PRUEBA-VENCE").run()
claves = [d.key for d in at.date_input if d.key and d.key.startswith("venc_item_factura_")]
ok("factura: aparece 'Vence' en los renglones con días disponibles", len(claves) == 2, str(claves))
f_fac = at.date_input(key="fac_fecha").value
est = [d.value for d in at.date_input if d.key in claves]
ok("factura: la fecha propuesta es la estimada (fecha + 10)",
   all(v == f_fac + pd.Timedelta(days=10) for v in est), str(est))
otra = (pd.Timestamp(f_fac) + pd.Timedelta(days=4)).date()
at.date_input(key=claves[1]).set_value(otra).run()
[b for b in at.button if "Guardar factura" in b.label][0].click().run()
ok("factura: se guarda sin errores", not at.exception, str([e.value[:300] for e in at.exception]))
fv = q("SELECT d.articulo_id, d.fecha_vencimiento FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id "
       "WHERE f.numero = 'PRUEBA-VENCE' ORDER BY d.id")
ok("factura: la estimada no se guarda (queda vacía) y la cambiada sí",
   len(fv) == 2 and fv[0][1] is None and fv[1][1] == otra, str(fv))
with db.engine.begin() as c:
    c.execute(text("DELETE FROM facturas WHERE numero = 'PRUEBA-VENCE'"))
db.guardar_vida_util_categorias({cat: None})
print(f"\n{'TODO OK' if not fallas else f'{fallas} FALLA(S)'}")
sys.exit(1 if fallas else 0)
