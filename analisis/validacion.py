"""
Indicadores del protocolo de validación con el comerciante (tanda 17).

Lee un RESPALDO de producción (la carpeta que deja respaldo_produccion.py,
con sus CSV) y calcula los indicadores semana por semana (lunes a domingo).
No se conecta a la base ni escribe en ella: todo sale de los CSV, así cada
número se puede volver a calcular con el mismo respaldo.

Uso (cada lunes, después del respaldo):
    python analisis/respaldo_produccion.py
    python analisis/validacion.py                      (usa el respaldo más nuevo de respaldos/)
    python analisis/validacion.py respaldos\\prod_20261102_0930

Deja en respaldos/validacion/ (no va al repositorio: son datos del negocio):
    semanal_<respaldo>.csv     un renglón por semana con los indicadores de flujo
    pronostico_<respaldo>.csv  error real del pronóstico guardado, por semana y clase
    listas_<respaldo>.csv      cada lista guardada contra lo que se compró
    fotos.csv                  una fila por respaldo con lo que es "foto del momento"
                               (stock negativo, capital parado); se va acumulando
y muestra el resumen en pantalla.

Criterios (los del documento "Protocolo de validación con el comerciante"):
- Semana: lunes a domingo, desde el 28/09/2026 (primer día de registro). La
  última semana puede estar incompleta: se informa cuántos días tiene.
- Día abierto: el almacén abre todos los días; se cuentan los días sin
  ninguna venta registrada.
- El día del respaldo no cuenta (está incompleto).
- Días sin stock (clase A): el artículo arrancó el día sin stock y no vendió
  (misma reconstrucción hacia atrás que el pronóstico). Se excluyen los
  artículos cuyo stock reconstruido queda negativo (faltan compras cargadas)
  y se informa cuántos son.
- Error del pronóstico: para cada pronóstico guardado (pronostico_snapshot),
  suma de los 7 días siguientes contra lo vendido esos días, por artículo.
  WAPE = Σ|vendido − pronosticado| / Σ vendido; sesgo = (Σ pronosticado −
  Σ vendido) / Σ vendido. Clase ABC: por ingreso de los 90 días anteriores.
- Pan y compra diaria: artículos marcados compra diaria y sus fraccionados
  (pan de ayer). Tirado = lo cargado en Sobrante del día (consumo interno).
- Capital parado: artículos con stock que nunca se vendieron, sin los recién
  cargados (< 7 días) ni la consignación, a costo; sobre la mercadería total.
- Desafío Recreo (tanda 21): por semana, días con la tarea del día cumplida
  (de los días con tarea armada), controles de góndola hechos y artículos
  distintos con fecha de vencimiento vista. Desde que se corre la migración
  (es una intervención con fecha: comparar semanas antes y después).
- Exactitud del inventario (IRA): conteos a ciegas del Desafío que coinciden
  con el stock de la app (por unidad, la misma cantidad; por kg, hasta 5 %),
  sobre los conteos hechos. Se informa también cuántos terminaron en corrección.
- Datos arreglados: los asignados por "Arreglá un dato" y los verificados.
- Cierre del día: días con el cierre guardado, diferencia de caja promedio (en
  valor absoluto) y días con el pan resuelto.
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))  # raíz del repositorio

import datetime as dt
import glob
import json
import os
import re
import sys

import numpy as np
import pandas as pd

import lista_compras as lc
import pronostico as pron

INICIO = dt.date(2026, 9, 28)
DIAS_RECIEN_CARGADO = 7
HORIZONTE = 7
AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATRON_CORRECCION = re.compile(r":\s*(-?[0-9.]+)\s*->\s*(-?[0-9.]+)\s*$")


# ----------------------------------------------------------------------------
# Lectura del respaldo
# ----------------------------------------------------------------------------
def _bool(s):
    return s.map(lambda x: str(x).strip().lower() in ("t", "true", "1")) if s is not None else s


class Respaldo:
    def __init__(self, carpeta):
        self.carpeta = carpeta
        self.csv = os.path.join(carpeta, "csv")
        with open(os.path.join(carpeta, "manifiesto.json"), encoding="utf-8") as f:
            self.manifiesto = json.load(f)
        self.momento = pd.Timestamp(self.manifiesto["fecha"])           # hora argentina
        self.hoy = self.momento.date()
        self.ultimo_dia = self.hoy - dt.timedelta(days=1)               # último día completo

    def t(self, nombre):
        ruta = os.path.join(self.csv, f"{nombre}.csv")
        if not os.path.exists(ruta):
            return None
        return pd.read_csv(ruta, encoding="utf-8-sig", low_memory=False)


def cargar(carpeta):
    r = Respaldo(carpeta)
    a = r.t("articulos")
    a["activo"] = _bool(a["activo"])
    a["acceso_rapido"] = _bool(a["acceso_rapido"]) if "acceso_rapido" in a else False
    for c in ("stock_actual", "costo_compra_actual", "iva", "factor_fraccion"):
        a[c] = pd.to_numeric(a[c], errors="coerce")
    a["alta"] = pd.to_datetime(a["creado_en"]).dt.date
    if "reposicion_dias" not in a:
        a["reposicion_dias"] = np.nan
    v = r.t("ventas")
    v["fecha"] = pd.to_datetime(v["fecha"]).dt.date
    v["es_consumo_interno"] = _bool(v["es_consumo_interno"])
    for c in ("cantidad", "monto_total"):
        v[c] = pd.to_numeric(v[c], errors="coerce").fillna(0.0)
    vc = r.t("venta_componentes")
    if vc is not None:
        vc["mueve_stock"] = _bool(vc["mueve_stock"])
    f = r.t("facturas")
    f["fecha"] = pd.to_datetime(f["fecha"]).dt.date
    d = r.t("detalle_factura")
    d = d.merge(f[["id", "fecha", "proveedor_id"]].rename(columns={"id": "factura_id"}), on="factura_id")
    h = r.t("historial_cambios")
    h["fecha"] = pd.to_datetime(h["fecha"])                             # UTC, como lo guarda la base
    prov = r.t("proveedores")
    app = r.t("articulo_proveedor_precio")
    fr = r.t("fraccionamientos")
    if fr is not None and not fr.empty:
        fr["fecha"] = pd.to_datetime(fr["fecha"]).dt.date
    return r, {"articulos": a, "ventas": v, "venta_componentes": vc, "facturas": f, "detalle": d,
               "historial": h, "proveedores": prov, "app": app, "fraccionamientos": fr,
               "snapshot_calidad": r.t("snapshot_calidad"), "pronostico_snapshot": r.t("pronostico_snapshot"),
               "listas_compra": r.t("listas_compra"), "listas_compra_detalle": r.t("listas_compra_detalle"),
               "viajes_compra": r.t("viajes_compra"), "promocion_componentes": r.t("promocion_componentes"),
               "desafio_eventos": r.t("desafio_eventos"), "desafio_tareas": r.t("desafio_tareas"),
               "controles_vencimiento": r.t("controles_vencimiento"), "cierres_caja": r.t("cierres_caja")}


# ----------------------------------------------------------------------------
# Insumos compartidos (equivalentes a las vistas y consultas de la app)
# ----------------------------------------------------------------------------
def ventas_analitica(D):
    """Equivalente a v_ventas_analitica: los combos se abren en sus componentes
    (monto repartido por el precio de referencia guardado al vender)."""
    a, v, vc = D["articulos"], D["ventas"], D["venta_componentes"]
    promo = set(a.loc[a["categoria"] == "Promocion", "id"])
    simples = v[~v["articulo_id"].isin(promo)]
    filas = [simples[["id", "fecha", "articulo_id", "cantidad", "monto_total", "es_consumo_interno"]]
             .rename(columns={"id": "venta_id", "monto_total": "monto_atribuido"})]
    combos = v[v["articulo_id"].isin(promo)]
    if not combos.empty:
        if vc is not None and not vc.empty:
            comp = vc[["venta_id", "componente_articulo_id", "cantidad_por_unidad", "precio_ref"]].copy()
        else:
            comp = pd.DataFrame(columns=["venta_id", "componente_articulo_id", "cantidad_por_unidad", "precio_ref"])
        # combos sin desglose guardado: composición actual, monto en partes iguales
        sin = combos[~combos["id"].isin(comp["venta_id"])]
        pc = D["promocion_componentes"]
        if not sin.empty and pc is not None:
            extra = sin[["id", "articulo_id"]].merge(pc, left_on="articulo_id", right_on="promocion_articulo_id")
            comp = pd.concat([comp, extra.rename(columns={"id": "venta_id"})[
                ["venta_id", "componente_articulo_id", "cantidad_por_unidad"]].assign(precio_ref=1.0)])
        comp["cantidad_por_unidad"] = comp["cantidad_por_unidad"].astype(float)
        comp["peso"] = comp["precio_ref"].astype(float).fillna(0) * comp["cantidad_por_unidad"]
        total = comp.groupby("venta_id")["peso"].transform("sum")
        n = comp.groupby("venta_id")["peso"].transform("size")
        comp["w"] = np.where(total > 0, comp["peso"] / total.where(total > 0, 1), 1.0 / n)
        c = combos.merge(comp, left_on="id", right_on="venta_id")
        filas.append(pd.DataFrame({
            "venta_id": c["id"], "fecha": c["fecha"], "articulo_id": c["componente_articulo_id"].astype(int),
            "cantidad": c["cantidad"] * c["cantidad_por_unidad"], "monto_atribuido": c["monto_total"] * c["w"],
            "es_consumo_interno": c["es_consumo_interno"]}))
    return pd.concat(filas, ignore_index=True)


def articulos_pronostico(D):
    a = D["articulos"]
    return a.rename(columns={"id": "articulo_id"})[
        ["articulo_id", "nombre", "categoria", "activo", "reemplazado_por", "articulo_fraccion_de",
         "factor_fraccion", "stock_actual", "unidad_medida", "alta", "reposicion_dias"]]


def movimientos(D):
    """Equivalente a pronostico.movimientos_stock: fecha, articulo_id, delta."""
    v, vc, d, fr, h = D["ventas"], D["venta_componentes"], D["detalle"], D["fraccionamientos"], D["historial"]
    partes = []
    if vc is not None and not vc.empty:
        m = v.merge(vc[vc["mueve_stock"]], left_on="id", right_on="venta_id", how="left")
        aid = m["componente_articulo_id"].fillna(m["articulo_id"]).astype(int)
        q = -m["cantidad"] * m["cantidad_por_unidad"].astype(float).fillna(1.0)
        partes.append(pd.DataFrame({"fecha": m["fecha"], "articulo_id": aid, "delta": q}))
    else:
        partes.append(pd.DataFrame({"fecha": v["fecha"], "articulo_id": v["articulo_id"], "delta": -v["cantidad"]}))
    partes.append(pd.DataFrame({"fecha": d["fecha"], "articulo_id": d["articulo_id"],
                                "delta": d["cantidad"].astype(float)}))
    if fr is not None and not fr.empty:
        partes.append(pd.DataFrame({"fecha": fr["fecha"], "articulo_id": fr["articulo_bolsa_id"],
                                    "delta": -fr["cantidad_bolsas"].astype(float)}))
        partes.append(pd.DataFrame({"fecha": fr["fecha"], "articulo_id": fr["articulo_derivado_id"],
                                    "delta": fr["cantidad_generada"].astype(float)}))
    cor = h[(h["tabla"] == "articulos") & h["detalle"].astype(str).str.startswith("Stock corregido a mano en ")]
    mm = cor["detalle"].astype(str).str.extract(PATRON_CORRECCION)
    cor = cor[mm[0].notna()]
    mm = mm[mm[0].notna()]
    # el historial se guarda en UTC: día argentino, igual que pronostico.movimientos_stock (tanda 20)
    f_ar = cor["fecha"].dt.tz_localize("UTC").dt.tz_convert("America/Argentina/Buenos_Aires").dt.date
    partes.append(pd.DataFrame({"fecha": f_ar, "articulo_id": cor["registro_id"].astype(int),
                                "delta": mm[1].astype(float) - mm[0].astype(float)}))
    mov = pd.concat(partes, ignore_index=True)
    mov["fecha"] = pd.to_datetime(mov["fecha"])
    return mov


def stock_por_dia(Y, articulos, mov):
    """(inicio_dia, confiable) como en pronostico.dias_observados."""
    fechas = pd.DatetimeIndex(Y.index)
    a = articulos.set_index("articulo_id")
    stock_hoy = a["stock_actual"].astype(float).reindex(Y.columns).fillna(0.0)
    m = mov[mov["articulo_id"].isin(Y.columns)]
    piv = m.pivot_table(index="fecha", columns="articulo_id", values="delta", aggfunc="sum")
    todas = piv.index.union(fechas).sort_values()
    piv = piv.reindex(index=todas, columns=Y.columns).fillna(0.0)
    posteriores = piv.iloc[::-1].cumsum().iloc[::-1].shift(-1).fillna(0.0)
    fin = pd.DataFrame(stock_hoy.values[None, :] - posteriores.values, index=todas, columns=Y.columns)
    ini = fin.shift(1).fillna(fin)
    ini, fin = ini.reindex(fechas), fin.reindex(fechas)
    confiable = (fin.min() >= -1e-6) & (ini.min() >= -1e-6)
    return ini, confiable


def semana(f):
    f = pd.Timestamp(f).date()
    return f - dt.timedelta(days=f.weekday())


def consignacion_ids(D):
    prov, app = D["proveedores"], D["app"]
    if prov is None or "consignacion" not in prov.columns or app is None:
        # respaldos anteriores a la tanda 14: la consignación eran los artículos de FUSION FUNGA
        a = D["articulos"]
        return set(a.loc[a["marca"].astype(str).str.strip().str.upper() == "FUSION FUNGA", "id"])
    prov = prov.assign(consignacion=_bool(prov["consignacion"]))
    m = app.merge(prov[["id", "consignacion"]], left_on="proveedor_id", right_on="id")
    g = m.groupby("articulo_id")["consignacion"].all()
    return set(g[g].index)


def diarios_ids(D):
    a = D["articulos"]
    diarios = set(a.loc[pd.to_numeric(a["reposicion_dias"], errors="coerce") == 1, "id"])
    return diarios | set(a.loc[a["articulo_fraccion_de"].isin(diarios), "id"])


# ----------------------------------------------------------------------------
# Indicadores
# ----------------------------------------------------------------------------
def semanal(r, D, VA, Y, I, arts, mov):
    v = D["ventas"]
    v = v[(v["fecha"] >= INICIO) & (v["fecha"] <= r.ultimo_dia)]
    dias = pd.date_range(INICIO, r.ultimo_dia).date
    base = pd.DataFrame({"fecha": dias})
    base["semana"] = base["fecha"].map(semana)
    por_dia = v.groupby("fecha").agg(ventas=("id", "size"),
                                     con_atendio=("registrado_por", lambda s: s.notna().sum()),
                                     tickets=("ticket_id", "nunique"), monto=("monto_total", "sum"))
    base = base.join(por_dia, on="fecha").fillna({"ventas": 0, "con_atendio": 0, "tickets": 0, "monto": 0.0})

    # correcciones manuales de stock (fecha en hora argentina)
    h = D["historial"]
    cor = h[(h["tabla"] == "articulos") & h["detalle"].astype(str).str.startswith("Stock corregido a mano")]
    f_ar = cor["fecha"].dt.tz_localize("UTC").dt.tz_convert("America/Argentina/Buenos_Aires").dt.date
    cor_sem = f_ar.map(semana).value_counts()

    # días sin stock en clase A (clase por ingreso de los 90 días anteriores a cada semana)
    ini, confiable = stock_por_dia(Y, arts, mov)
    inicio_art = pron._inicio(Y.values, pd.DatetimeIndex(Y.index), arts.set_index("articulo_id")
                              .reindex(Y.columns)["alta"].tolist())
    vivos = np.arange(len(Y))[:, None] >= inicio_art[None, :]
    sin_stock = (ini.values <= 1e-6) & (Y.values <= 0) & vivos

    # pan / compra diaria
    dia_ids = diarios_ids(D)
    va = VA[VA["articulo_id"].isin(dia_ids)]
    vendido_pan = va[~va["es_consumo_interno"]].groupby("fecha")["cantidad"].sum()
    tirado_pan = va[va["es_consumo_interno"]].groupby("fecha")["cantidad"].sum()
    det = D["detalle"]
    comprado_pan = det[det["articulo_id"].isin(dia_ids)].groupby("fecha")["cantidad"].sum().astype(float)

    filas = []
    fechas_Y = pd.DatetimeIndex(Y.index).date
    for s, g in base.groupby("semana"):
        dias_sem = len(g)
        fila = {
            "semana": s, "dias": dias_sem,
            "dias_con_ventas": int((g["ventas"] > 0).sum()),
            "dias_sin_ventas": ", ".join(pd.Timestamp(x).strftime("%d/%m") for x in g.loc[g["ventas"] == 0, "fecha"]),
            "ventas_renglones": int(g["ventas"].sum()), "tickets": int(g["tickets"].sum()),
            "monto_vendido": round(float(g["monto"].sum()), 2),
            "pct_con_atendio": round(100 * g["con_atendio"].sum() / g["ventas"].sum(), 1) if g["ventas"].sum() else None,
            "correcciones_manuales_stock": int(cor_sem.get(s, 0)),
        }
        # clase A al comenzar la semana
        idx = [i for i, f in enumerate(fechas_Y) if s <= f <= s + dt.timedelta(days=6)]
        t0 = idx[0] if idx else None
        if t0 is not None and t0 > 0:
            clase = pron.clases_abc(I.values[max(0, t0 - pron.VENTANA_ABC):t0])
            A = (clase == "A") & confiable.values
            if A.any():
                sub = sin_stock[idx][:, A]
                viv = vivos[idx][:, A]
                fila["articulos_A"] = int(A.sum())
                fila["pct_dias_sin_stock_A"] = round(100 * sub.sum() / max(viv.sum(), 1), 1)
            fila["articulos_A_excluidos"] = int(((clase == "A") & ~confiable.values).sum())
        pan_c = sum(float(comprado_pan.get(x, 0)) for x in g["fecha"])
        pan_v = sum(float(vendido_pan.get(x, 0)) for x in g["fecha"])
        pan_t = sum(float(tirado_pan.get(x, 0)) for x in g["fecha"])
        fila.update({"compra_diaria_comprado_por_dia": round(pan_c / dias_sem, 2),
                     "compra_diaria_vendido_por_dia": round(pan_v / dias_sem, 2),
                     "compra_diaria_tirado_por_dia": round(pan_t / dias_sem, 2)})
        filas.append(fila)
    out = pd.DataFrame(filas)

    cal = D["snapshot_calidad"]
    if cal is not None and not cal.empty:
        cal = cal.assign(semana=pd.to_datetime(cal["semana"]).dt.date)
        cal["semana"] = cal["semana"].map(semana)
        out = out.merge(cal[["semana", "indicador", "articulos_con_alerta"]].rename(
            columns={"indicador": "calidad_datos", "articulos_con_alerta": "calidad_articulos_con_alerta"}),
            on="semana", how="left")

    viajes = D["viajes_compra"]
    if viajes is not None and not viajes.empty:
        viajes = viajes.assign(semana=pd.to_datetime(viajes["fecha"]).dt.date.map(semana),
                               horas=pd.to_numeric(viajes["horas"], errors="coerce"))
        g = viajes.groupby("semana").agg(viajes_planificados=("tipo", lambda s: int((s == "planificado").sum())),
                                         viajes_urgencia=("tipo", lambda s: int((s == "urgencia").sum())),
                                         horas_viaje_promedio=("horas", "mean"),
                                         viajes_con_lista=("lista_id", lambda s: int(s.notna().sum())))
        out = out.merge(g.reset_index(), on="semana", how="left")
    out = _desafio_semanal(out, D)
    confiables = {"articulos_stock_no_cierra": int((~confiable).sum()), "articulos_serie": int(len(confiable))}
    return out, confiables


def _desafio_semanal(out, D):
    """Desafío Recreo: cumplimiento de la tarea del día y controles de góndola por semana."""
    tareas, ev, ctrl = D.get("desafio_tareas"), D.get("desafio_eventos"), D.get("controles_vencimiento")
    out = _cierres_semanal(out, D.get("cierres_caja"))
    if tareas is None or tareas.empty:
        return out
    if "tipo" in tareas:
        datos = tareas[tareas["tipo"] == "dato"]
        tareas = tareas[tareas["tipo"] == "gondola"]
    else:
        datos = tareas.iloc[0:0]
    dias_tarea = tareas.assign(fecha=pd.to_datetime(tareas["fecha"]).dt.date)[["fecha", "persona_id"]].drop_duplicates()
    dias_tarea["semana"] = dias_tarea["fecha"].map(semana)
    g = dias_tarea.groupby("semana").size().rename("desafio_dias_con_tarea").to_frame()
    if ev is not None and not ev.empty:
        cumpl = ev[ev["tipo"] == "tarea"].assign(fecha=lambda x: pd.to_datetime(x["fecha"]).dt.date)
        g["desafio_dias_cumplidos"] = cumpl["fecha"].map(semana).value_counts()
    else:
        g["desafio_dias_cumplidos"] = 0
    g["desafio_dias_cumplidos"] = g["desafio_dias_cumplidos"].fillna(0).astype(int)
    g["desafio_pct_cumplido"] = (100 * g["desafio_dias_cumplidos"] / g["desafio_dias_con_tarea"]).round(1)
    if ctrl is not None and not ctrl.empty:
        c = ctrl.assign(semana=pd.to_datetime(ctrl["fecha"]).dt.date.map(semana),
                        con_fecha=ctrl["vence"].notna())
        g["controles_gondola"] = c.groupby("semana").size()
        g["articulos_con_fecha_vista"] = c[c["con_fecha"]].groupby("semana")["articulo_id"].nunique()
        g[["controles_gondola", "articulos_con_fecha_vista"]] = g[["controles_gondola", "articulos_con_fecha_vista"]].fillna(0).astype(int)
        if "conteo" in c:
            cn = c[pd.to_numeric(c["conteo"], errors="coerce").notna()].copy()
            cn["ok"] = _bool(cn["conteo_coincide"].astype(str)) if len(cn) else cn.get("conteo_coincide")
            cn["corr"] = _bool(cn["corregido"].astype(str)) if len(cn) else cn.get("corregido")
            gc = cn.groupby("semana").agg(conteos=("ok", "size"), conteos_exactos=("ok", "sum"),
                                          conteos_corregidos=("corr", "sum"))
            gc["pct_exactitud_inventario"] = (100 * gc["conteos_exactos"] / gc["conteos"]).round(1)
            g = g.join(gc)
    if not datos.empty:
        dd = datos.assign(fecha=pd.to_datetime(datos["fecha"]).dt.date)
        dd["semana"] = dd["fecha"].map(semana)
        g["datos_asignados"] = dd.groupby("semana").size()
        if ev is not None and not ev.empty:
            ok = ev[(ev["tipo"] == "dato") & (pd.to_numeric(ev["estrellitas"], errors="coerce") > 0)]
            g["datos_arreglados"] = pd.to_datetime(ok["fecha"]).dt.date.map(semana).value_counts()
        g[["datos_asignados", "datos_arreglados"]] = g.reindex(columns=["datos_asignados", "datos_arreglados"]).fillna(0).astype(int)
    return out.merge(g.reset_index(), on="semana", how="left")


def _cierres_semanal(out, cierres):
    """Cierre del día guardado: días, diferencia de caja promedio (valor absoluto) y pan resuelto."""
    if cierres is None or cierres.empty:
        return out
    c = cierres.assign(semana=pd.to_datetime(cierres["fecha"]).dt.date.map(semana),
                       dif=pd.to_numeric(cierres["diferencia"], errors="coerce").abs())
    g = c.groupby("semana").agg(cierres_guardados=("dif", "size"), diferencia_caja_promedio_abs=("dif", "mean"))
    g["diferencia_caja_promedio_abs"] = g["diferencia_caja_promedio_abs"].round(2)
    return out.merge(g.reset_index(), on="semana", how="left")


def error_pronostico(r, D, Y, I):
    snap = D["pronostico_snapshot"]
    cols = ["semana", "clase", "origenes", "articulos", "vendido", "pronosticado", "wape_%", "sesgo_%"]
    if snap is None or snap.empty:
        return pd.DataFrame(columns=cols)
    snap = snap.assign(fecha_calculo=pd.to_datetime(snap["fecha_calculo"]),
                       fecha_objetivo=pd.to_datetime(snap["fecha_objetivo"]),
                       demanda=snap["demanda"].astype(float))
    fechas = pd.DatetimeIndex(Y.index)
    filas = []
    for o, g in snap.groupby("fecha_calculo"):
        fin = o + pd.Timedelta(days=HORIZONTE - 1)
        if fin.date() > r.ultimo_dia or o not in fechas:
            continue                                   # todavía no pasaron los 7 días
        g = g[(g["fecha_objetivo"] >= o) & (g["fecha_objetivo"] <= fin)]
        F = g.groupby("articulo_id")["demanda"].sum()
        t0 = fechas.get_loc(o)
        clase = pd.Series(pron.clases_abc(I.values[max(0, t0 - pron.VENTANA_ABC):t0]), index=Y.columns)
        real = Y.loc[o:fin].sum()
        ids = F.index.intersection(Y.columns)
        filas.append(pd.DataFrame({"origen": o.date(), "articulo_id": ids, "clase": clase.reindex(ids).values,
                                   "pronosticado": F.reindex(ids).values, "vendido": real.reindex(ids).values}))
    if not filas:
        return pd.DataFrame(columns=cols)
    e = pd.concat(filas, ignore_index=True)
    e["semana"] = e["origen"].map(semana)
    e["abs"] = (e["vendido"] - e["pronosticado"]).abs()
    total = e.assign(clase="Todas")
    g = pd.concat([e, total]).groupby(["semana", "clase"]).agg(
        origenes=("origen", "nunique"), articulos=("articulo_id", "nunique"),
        vendido=("vendido", "sum"), pronosticado=("pronosticado", "sum"), abs=("abs", "sum")).reset_index()
    g["wape_%"] = (100 * g["abs"] / g["vendido"].where(g["vendido"] > 0)).round(1)
    g["sesgo_%"] = (100 * (g["pronosticado"] - g["vendido"]) / g["vendido"].where(g["vendido"] > 0)).round(1)
    return g[cols]


def listas(r, D, frecuencia=7):
    L, det = D["listas_compra"], D["listas_compra_detalle"]
    if L is None or L.empty:
        return pd.DataFrame()
    a = D["articulos"].set_index("id")
    ven = lc.ventanas_listas(L, max(int(frecuencia) - 1, 0))
    d = D["detalle"].merge(a[["nombre", "unidad_medida", "iva"]], left_on="articulo_id", right_index=True)
    d["monto_c_iva"] = d["cantidad"].astype(float) * d["costo_unitario"].astype(float) * (1 + d["iva"].fillna(0.21))
    excluir = diarios_ids(D) | consignacion_ids(D)
    filas = []
    for v in ven.itertuples():
        dl = det[det["lista_id"] == v.id].merge(a[["nombre", "unidad_medida"]], left_on="articulo_id", right_index=True)
        dl = dl.assign(incluido=_bool(dl["incluido"]), sugerido=dl["sugerido"].astype(float),
                       cantidad=dl["cantidad"].astype(float), costo_c_iva=dl["costo_c_iva"].astype(float))
        c = d[(d["fecha"] >= v.desde) & (d["fecha"] <= v.hasta)].groupby(
            ["articulo_id", "nombre", "unidad_medida"], as_index=False).agg(
            comprado=("cantidad", "sum"), monto_c_iva=("monto_c_iva", "sum"))
        c["comprado"] = c["comprado"].astype(float)
        _, res = lc.comparar_lista(dl, c, excluir)
        abierta = v.hasta >= r.hoy
        filas.append({"lista": v.fecha, "ventana_desde": v.desde, "ventana_hasta": v.hasta,
                      "estado": "abierta" if abierta else "cerrada", **res})
    return pd.DataFrame(filas)


def fotos(r, D, VA):
    a = D["articulos"]
    activos = a[a["activo"] & (a["categoria"] != "Promocion")]
    consig = consignacion_ids(D)
    negativos = activos[activos["stock_actual"] < 0]
    # mercadería (como resumen_valor_stock: sin combos virtuales ni consignación, stock negativo = 0)
    merc = a[~((a["categoria"] == "Promocion") & (a.get("promo_tipo") == "virtual")) & ~a["id"].isin(consig)]
    valor = float((merc["stock_actual"].clip(lower=0) * merc["costo_compra_actual"].fillna(0)).sum())
    # nunca vendidos (como stock_inmovilizado, grupo 'nunca')
    movidos = set(VA["articulo_id"])
    frac = a.dropna(subset=["articulo_fraccion_de"])
    movidos |= set(frac.loc[frac["id"].isin(movidos), "articulo_fraccion_de"].astype(int))
    fr = D["fraccionamientos"]
    if fr is not None and not fr.empty:
        movidos |= set(fr["articulo_bolsa_id"])
    nunca = a[(a["categoria"] != "Promocion") & (a["stock_actual"] > 0) & ~a["id"].isin(movidos)]
    dias_alta = nunca["alta"].map(lambda x: (r.hoy - x).days)
    parado = nunca[(dias_alta >= DIAS_RECIEN_CARGADO) & ~nunca["id"].isin(consig)]
    valor_parado = float((parado["stock_actual"] * parado["costo_compra_actual"].fillna(0)).sum())
    return {
        "respaldo": r.momento.strftime("%Y-%m-%d %H:%M"),
        "articulos_activos": int(len(activos)),
        "stock_negativo": int(len(negativos)),
        "stock_negativo_articulos": "; ".join(negativos["nombre"].astype(str).head(30)),
        "capital_parado_articulos": int(len(parado)),
        "capital_parado_$": round(valor_parado, 2),
        "mercaderia_$": round(valor, 2),
        "capital_parado_%": round(100 * valor_parado / valor, 1) if valor else None,
    }


# ----------------------------------------------------------------------------
def respaldo_mas_nuevo():
    carpetas = sorted(glob.glob(os.path.join(AQUI, "respaldos", "prod_*")))
    carpetas = [c for c in carpetas if os.path.exists(os.path.join(c, "manifiesto.json"))]
    if not carpetas:
        sys.exit("No hay respaldos en respaldos/. Correr antes: python analisis/respaldo_produccion.py")
    return carpetas[-1]


def main(carpeta=None, salida=None):
    carpeta = carpeta or respaldo_mas_nuevo()
    r, D = cargar(carpeta)
    nombre = os.path.basename(os.path.normpath(carpeta))
    salida = salida or os.path.join(os.path.dirname(os.path.normpath(carpeta)), "validacion")
    os.makedirs(salida, exist_ok=True)

    VA = ventas_analitica(D)
    arts = articulos_pronostico(D)
    VAp = VA[VA["fecha"] <= r.ultimo_dia]
    Y, I = pron.demanda_diaria(VAp, arts, fecha_fin=pd.Timestamp(r.ultimo_dia))
    obj = pron.articulos_objetivo(arts, Y.columns)
    Y, I = Y[obj], I[obj]
    mov = movimientos(D)

    sem, conf = semanal(r, D, VA, Y, I, arts, mov)
    err = error_pronostico(r, D, Y, I)
    lis = listas(r, D)
    foto = fotos(r, D, VA)
    foto.update(conf)

    sem.to_csv(os.path.join(salida, f"semanal_{nombre}.csv"), index=False, encoding="utf-8-sig")
    err.to_csv(os.path.join(salida, f"pronostico_{nombre}.csv"), index=False, encoding="utf-8-sig")
    if not lis.empty:
        lis.to_csv(os.path.join(salida, f"listas_{nombre}.csv"), index=False, encoding="utf-8-sig")
    ruta_fotos = os.path.join(salida, "fotos.csv")
    previas = pd.read_csv(ruta_fotos, encoding="utf-8-sig") if os.path.exists(ruta_fotos) else pd.DataFrame()
    if not previas.empty:
        previas = previas[previas["respaldo"] != foto["respaldo"]]
    pd.concat([previas, pd.DataFrame([foto])], ignore_index=True).to_csv(ruta_fotos, index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 200, "display.max_columns", 40)
    print(f"Respaldo {nombre} ({foto['respaldo']}). Días completos: {INICIO:%d/%m} al {r.ultimo_dia:%d/%m}.\n")
    print("== Uso, calidad, reposición y compra diaria, por semana (lunes)")
    vista = sem.copy()
    vista["semana"] = pd.to_datetime(vista["semana"]).dt.strftime("%d/%m")
    print(vista.drop(columns=["dias_sin_ventas", "monto_vendido"], errors="ignore").to_string(index=False))
    sin = sem[sem["dias_sin_ventas"] != ""]
    print("\nDías sin ninguna venta registrada: " +
          ("ninguno" if sin.empty else "; ".join(sin["dias_sin_ventas"])))
    print(f"\n== Foto del momento\n  stock negativo: {foto['stock_negativo']} artículos activos"
          f"\n  capital parado (nunca se vendió): ${foto['capital_parado_$']:,.0f} en {foto['capital_parado_articulos']} "
          f"artículos, {foto['capital_parado_%']} % de ${foto['mercaderia_$']:,.0f} de mercadería"
          f"\n  artículos cuyo stock no cierra (excluidos de días sin stock): {conf['articulos_stock_no_cierra']} de "
          f"{conf['articulos_serie']}")
    print("\n== Error real del pronóstico guardado (7 días)")
    print("  todavía no hay pronósticos con 7 días cumplidos" if err.empty else err.to_string(index=False))
    print("\n== Listas guardadas")
    if lis.empty:
        print("  todavía no hay listas guardadas")
    else:
        print(lis[["lista", "estado", "sugeridos", "llevados", "cambiados_al_armar", "pct_lista_comprada",
                   "pct_comprado_igual", "agregados"]].to_string(index=False))
    print(f"\nArchivos en {salida}")
    return sem, err, lis, foto


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
