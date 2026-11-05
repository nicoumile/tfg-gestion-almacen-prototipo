"""
Controles de la tanda 14: consignación (FUSION FUNGA) y desempeño de proveedores.
La base tiene que tener migracion_tanda14.py aplicada. ESCRIBE en la base (una venta
y un pago): usar solo una copia local.

Uso: python pruebas/controles_tanda14.py <carpeta_codigo> <base>
"""
import os, sys
carpeta, base = os.path.abspath(sys.argv[1]), sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
config.usar_base(base)
sys.path.insert(0, carpeta); os.chdir(carpeta)
import datetime as dt
import pandas as pd
from sqlalchemy import text
import db, lista_compras as lc, proveedores as anprov
fallas = 0
def ok(n, c, d=""):
    global fallas; fallas += not c; print(("OK    " if c else "FALLA ") + n + (f"  -- {d}" if d else ""))
def q(sql, **p):
    with db.engine.begin() as c:
        r = c.execute(text(sql), p); return r.scalar() if r.returns_rows else None
db.set_persona_actual(1, "Dueño")
pid = q("SELECT id FROM proveedores WHERE consignacion")
ok("FUSION FUNGA en consignación", pid is not None)
arts = db.listar_articulos(incluir_inactivos=True)
c = arts[arts["en_consignacion"]]
ok("sus 8 artículos figuran en consignación (y ninguno más)", len(c) == 8 and set(c["marca"].str.strip()) == {"FUSION FUNGA"}, f"{len(c)}")
v = db.valor_stock()
esperado = float((c["stock_actual"].clip(lower=0) * c["costo_compra_actual"]).sum())
ok("valor en consignación separado de lo invertido", abs(v["valor_consignacion"] - esperado) < 0.01, f"${v['valor_consignacion']:,.0f}")
total = float((arts["stock_actual"].astype(float).clip(lower=0) * arts["costo_compra_actual"].astype(float))[~((arts["categoria"]=="Promocion")&(arts["promo_tipo"]=="virtual"))].sum())
ok("invertido + consignación = total de antes", abs(v["valor_costo"] + v["valor_consignacion"] - total) < 0.01)
# corte inicial de la migración: al día al 22/10 10:41 -> no se debe nada de lo vendido antes
corte = q("SELECT hasta_venta_id FROM liquidaciones_consignacion WHERE proveedor_id=:p AND monto_pagado=0", p=pid)
maxv = q("SELECT max(id) FROM ventas WHERE creado_en <= '2026-10-22 13:41'")
ok("la migración deja un corte inicial en $0 al 22/10 10:41", corte == maxv, f"hasta la venta #{corte}")
_, tot0, _, _ = db.consignacion_pendiente(pid)
despues = q("""SELECT COALESCE(SUM(costo_atribuido),0) FROM v_ventas_analitica WHERE venta_id > :c AND articulo_id IN
               (SELECT articulo_id FROM articulo_proveedor_precio WHERE proveedor_id=:p)""", c=corte, p=pid)
ok("con el corte, se le debe solo lo vendido después", abs(tot0 - float(despues)) < 0.01, f"${tot0:,.0f}")
# sin liquidaciones (otro proveedor que recién se marca): desde una fecha
q("DELETE FROM liquidaciones_consignacion WHERE proveedor_id=:p", p=pid)
det, tot, pri, ult = db.consignacion_pendiente(pid, dt.date(2026, 9, 1))
ventas_fusion = q("""SELECT COALESCE(SUM(costo_atribuido),0) FROM v_ventas_analitica WHERE articulo_id IN
                     (SELECT articulo_id FROM articulo_proveedor_precio WHERE proveedor_id=:p) AND fecha >= '2026-09-01'""", p=pid)
ok("se le debe = costo de lo vendido de sus artículos", abs(tot - float(ventas_fusion)) < 0.01, f"${tot:,.0f} en {len(det)} artículos")
# vender uno y liquidar
lid = db.registrar_liquidacion(pid, pri, ult, tot, tot - 1000, nota="contó 1 menos")
det2, tot2, _, _ = db.consignacion_pendiente(pid)
ok("después de pagar, no se debe nada", tot2 == 0 and det2.empty)
prod = int(c[c["stock_actual"] > 0]["id"].iloc[0])
precio = float(arts.set_index("id").loc[prod, "costo_venta_con_iva"])
db.confirmar_venta_carrito([{"articulo_id": prod, "cantidad": 2, "precio_unitario": precio, "nombre": "x"}], dt.date.today(), "Efectivo")
det3, tot3, _, _ = db.consignacion_pendiente(pid)
costo = float(arts.set_index("id").loc[prod, "costo_compra_actual"])
ok("una venta nueva de 2: se le deben 2 x costo", abs(tot3 - 2 * costo) < 0.01, f"${tot3:,.0f}")
ok("el pago queda en el historial con la diferencia", "diferencia" in q("SELECT detalle FROM historial_cambios WHERE accion='LIQUIDACION' ORDER BY id DESC LIMIT 1"))
db.registrar_corte_inicial(pid)
ok("botón 'está al día': después del corte no se debe nada", db.consignacion_pendiente(pid)[1] == 0)
ok("el pago NO es un gasto", q("SELECT count(*) FROM gastos WHERE descripcion ILIKE '%%liquid%%' OR descripcion ILIKE '%%fusion%%'") == 0)
# lista de compras: sus artículos sin costo de viaje
meta = lc.datos_articulos(db.engine)
mc = meta[meta["articulo_id"].isin(c["id"])]
ok("lista de compras: sabe que son de consignación y de quién", mc["consignacion"].all() and (mc["proveedor"] == "FUSION FUNGA").all())
fechas = pd.date_range(dt.date.today(), periods=14)
pronos = pd.DataFrame([{"articulo_id": int(a), "fecha_objetivo": f, "demanda": 1.0, "clase": "A"} for a in c["id"] for f in fechas])
_, viaje = lc.calcular(pronos, meta, db.obtener_parametros())
vc = viaje[viaje["articulo_id"].isin(c["id"])]
ok("en el viaje aparecen para pedírselos, con costo $0 y aviso", not vc.empty and (vc["Costo estimado"] == 0).all()
   and vc["Aviso"].str.contains("consignación").all(), f"{len(vc)} renglones")
# desempeño
tab, det = anprov.desempeno(db.engine, "2026-09-28", dt.date.today(), con_dias_sin_stock=False)
fila = tab[tab["proveedor"] == "FUSION FUNGA"]
ok("desempeño: FUSION FUNGA aparece, marcada en consignación", len(fila) == 1 and bool(fila["consignacion"].iloc[0]))
ok("desempeño: la ganancia total cuadra con Márgenes reales",
   abs(tab["ganancia"].sum() - db.margenes_reales("2026-09-28", dt.date.today())["ganancia_real"].sum()) < 1)
sys.exit(1 if fallas else 0)
