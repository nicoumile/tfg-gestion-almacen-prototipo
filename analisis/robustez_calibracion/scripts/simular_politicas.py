"""
Comparación de políticas de reposición (HU-018) repetida sobre varias
trayectorias de demanda (semillas y escenarios), con el registro necesario
para medir la calibración de las probabilidades. Usa SIN CAMBIOS el código de
la app: pronostico.pronosticar / backtest / metricas / elegir_por_clase /
error_por_clase, y lista_compras.calcular / _prob_agotar.

Mecánica (igual que HU-018 del 23/10):
- venta del día = min(demanda, stock); lo que falta es venta perdida.
- revisión de compras los lunes (y el día 0); la compra entra ese mismo día.
- stock inicial = el del catálogo real exportado.
- calentamiento: los primeros 90 días todas las políticas usan la naive.
  Se evalúa del día 91 al 365.
- las políticas 2-4 ven solo lo VENDIDO (lo que registraría la app).

Diferencia con HU-018 (a propósito, para reproducir la app):
- elección del modelo por clase y dispersión del error se reestiman con el
  backtest de la app (60 días, horizonte 7) al terminar el calentamiento y
  cada 8 semanas, con los datos observados hasta ese día. HU-018 las dejó
  fijas, y como dispersión usó el desvío del error en unidades (1,79/1,41/1,34)
  en lugar de la dispersión relativa a Poisson que usa la app.

Políticas:
 1 naive: si stock < max(stock_mínimo, 2), reponer hasta max(stock_inicial, umbral x 4, 8).
 2 abc: S = venta diaria promedio 90 días x (días de seguridad A15/B8/C3 + 7), clase ABC
   recalculada cada lunes con ingresos de 90 días.
 3 pronostico: lista de compras de la app (pronóstico + nivel de servicio A95/B90/C80).
 4 pronostico_sin_censura: igual que 3, sin corregir la demanda censurada.
"""
import math
import sys
import time

import numpy as np
import pandas as pd

import lista_compras as lc
import pronostico as p
from demanda_sim import DIAS, catalogo, generar

CALENTAMIENTO = 90
REEVALUAR_CADA = 56          # días (8 semanas)
NIVEL = {"A": 0.95, "B": 0.90, "C": 0.80}
SEGURIDAD_ABC = {"A": 15, "B": 8, "C": 3}
TAM_KG = 0.694               # E[s²]/E[s] de los tamaños de venta por kg del generador
POLITICAS = ["naive", "abc", "pronostico", "pronostico_sin_censura"]
# Parámetros fijos (solo para reproducir HU-018): (elección por clase, dispersión por clase)
FIJOS = None
FIJOS_HU018 = ({"A": "croston_0.05", "B": "ses_0.02", "C": "ses_0.02"}, {"A": 1.79, "B": 1.41, "C": 1.34})


def maestros(ids):
    cat, _ = catalogo()
    a = cat.set_index("id").loc[ids]
    costo0 = a["costo_compra_actual"].fillna(0).astype(float).values
    # aumentos trimestrales de 3-8 % por proveedor (generador): en promedio ~ +11 % en el año.
    # Se valoriza a precio y costo promedio del año (igual que HU-018): factor 1,11 a los dos.
    f = 1.11
    return {
        "nombre": a["nombre"].values, "categoria": a["categoria"].values,
        "kg": (a["unidad_medida"] == "kg").values, "unidad": a["unidad_medida"].values,
        "st0": np.clip(a["stock_actual"].astype(float).values, 0, None),
        "smin": a["stock_minimo"].astype(float).values,
        "iva": a["iva"].astype(float).values,
        "costo": costo0 * f,                                      # sin IVA
        "precio": a["costo_venta_con_iva"].astype(float).values * f,   # con IVA
    }


def redondear(q, kg):
    return np.where(kg, np.ceil(np.round(q * 10, 9)) / 10, np.ceil(np.round(q, 9)))


def obs_censura(Yh, ini):
    """Días observados, con el criterio de pronostico.dias_observados: censurado =
    arrancó el día sin stock y no vendió; artículos con menos de 7 días con stock
    no se corrigen. (Acá el stock se conoce exacto: siempre es 'confiable'.)"""
    T, n = Yh.shape
    inicio = p._inicio(Yh)
    despues = np.arange(T)[:, None] >= inicio[None, :]
    cens = (ini <= 1e-6) & (Yh <= 0) & despues
    con_stock = (despues & ~cens).sum(axis=0)
    poca = cens.any(axis=0) & (con_stock < p.MIN_DIAS_OBSERVADOS)
    cens[:, poca] = False
    return ~cens


def evaluar_app(Yh, Ih, fechas, cats_dict, ids, obs):
    """Lo que hace la app (pronostico.evaluar) con lo observado hasta hoy."""
    Y = pd.DataFrame(Yh, index=fechas, columns=ids)
    I = pd.DataFrame(Ih, index=fechas, columns=ids)
    T = len(Y)
    dias = min(60, T - 28 - 7 + 1)
    bt = p.backtest(Y, I, cats_dict, dias=dias, horizonte=7, obs=obs)
    met = p.metricas(bt)
    elec = p.elegir_por_clase(met)
    for c in "ABC":
        elec.setdefault(c, p.CONFIG_POR_DEFECTO)
    sig = p.error_por_clase(bt, elec)
    disp = {r.clase: (float(r.dispersion) if pd.notna(r.dispersion) else 1.0) for r in sig.itertuples()}
    return elec, disp


def simular(D, info, politica, registrar=True):
    ids = info["ids"]
    fechas = info["fechas"]
    n = len(ids)
    m = maestros(ids)
    kg = m["kg"]
    cats = list(m["categoria"])
    cats_dict = dict(zip(ids, cats))
    stock = m["st0"].copy()
    sold = np.zeros((DIAS, n)); lost = np.zeros((DIAS, n))
    ini = np.zeros((DIAS, n))            # stock al final del día anterior (como la reconstrucción de la app)
    disp_dia = np.zeros((DIAS, n))       # stock disponible ese día (después de la compra)
    compras = []                         # (dia, renglones, unidades, costo)
    calib = []                           # registros de calibración (lunes, política con pronóstico)
    alertas = []                         # estados diarios para la P(agotarse) de las alertas
    elec, disp = None, None
    F_sem, clase_sem, D7_sem = None, None, None
    ultimo_lunes = None
    meta_base = pd.DataFrame({
        "articulo_id": ids, "nombre": m["nombre"], "categoria": cats, "unidad_medida": m["unidad"],
        "costo": m["costo"], "precio": m["precio"], "costo_c_iva": m["costo"] * (1 + m["iva"]),
        "reposicion_dias": np.nan, "vida_util_dias": np.nan, "nivel_servicio": np.nan,
        "articulo_fraccion_de": np.nan, "tam_venta": np.where(kg, TAM_KG, np.nan), "bulto": 1.0,
        "proveedor": "Sim", "consignacion": False,
    })
    params = {"frecuencia_viaje_dias": 7, "valor_recupero_pct": 0.0,
              **{f"nivel_servicio_{c}": v for c, v in NIVEL.items()}}
    usar_censura = politica == "pronostico"

    for t in range(DIAS):
        d = fechas[t]
        ini[t] = stock
        es_revision = d.weekday() == 0 or t == 0
        if es_revision:
            pol = politica if t >= CALENTAMIENTO else "naive"
            q = np.zeros(n)
            if pol == "naive":
                umbral = np.maximum(m["smin"], 2)
                objetivo = np.maximum.reduce([m["st0"], umbral * 4, np.full(n, 8.0)])
                q = np.where(stock < umbral, np.ceil(objetivo - stock), 0.0)
            elif pol == "abc":
                Ih = sold[:t] * m["precio"]
                clase = p.clases_abc(Ih[-p.VENTANA_ABC:])
                v = sold[max(0, t - 90):t].mean(axis=0)
                S = v * np.array([SEGURIDAD_ABC.get(c, 3) + 7 for c in clase])
                q = np.where(S > stock, redondear(S - stock, kg), 0.0)
            else:
                Yh, Ih = sold[:t], sold[:t] * m["precio"]
                obs = obs_censura(Yh, ini[:t]) if usar_censura else np.ones(Yh.shape, dtype=bool)
                if FIJOS is not None:
                    elec, disp = dict(FIJOS[0]), dict(FIJOS[1])
                elif elec is None or (t - ultimo_eval) >= REEVALUAR_CADA:
                    elec, disp = evaluar_app(Yh, Ih, fechas[:t], cats_dict, ids, obs)
                    ultimo_eval = t
                fut = fechas[t:t + 7] if t + 7 <= DIAS else pd.date_range(d, periods=7)
                res = {cfg: p.pronosticar(Yh, cats, fut, cfg, Ih, None, obs) for cfg in set(elec.values())}
                clase = next(iter(res.values()))[1]["clase"]
                F = np.zeros((7, n))
                for c in "ABC":
                    mk = clase == c
                    F[:, mk] = res[elec[c]][0][:, mk]
                pron = pd.DataFrame({
                    "articulo_id": np.repeat(ids, 7), "fecha_objetivo": np.tile(np.asarray(fut), n),
                    "demanda": F.T.ravel(), "clase": np.repeat(clase, 7)})
                meta = meta_base.assign(stock=np.clip(stock, 0, None), stock_actual=stock)
                _, viaje = lc.calcular(pron, meta, params, disp)
                if not viaje.empty:
                    q = pd.Series(viaje["Comprar"].values, index=viaje["articulo_id"].values).reindex(ids).fillna(0).values
                F_sem, clase_sem = F, clase
            if q.sum() > 0:
                compras.append((t, int((q > 0).sum()), float(q.sum()), float((q * m["costo"]).sum())))
            stock = stock + q
            # calibración: estado después de comprar, para la semana que empieza
            if registrar and pol in ("pronostico", "pronostico_sin_censura") and t + 7 <= DIAS:
                D7 = F_sem.sum(axis=0)
                tam = np.where(kg, TAM_KG, 1.0)
                sigma = np.array([disp.get(c, 1.0) for c in clase_sem]) * np.sqrt(np.maximum(D7, 1e-9) * tam)
                dem_sem = D[t:t + 7].sum(axis=0)
                vivo = D7 > 0.05
                for j in np.where(vivo)[0]:
                    calib.append((t, ids[j], clase_sem[j], D7[j], sigma[j], stock[j], dem_sem[j],
                                  lc._prob_agotar(stock[j], D7[j], tam[j], bool(kg[j]))))
            ultimo_lunes = t
        disp_dia[t] = stock
        # estados para las alertas (P de agotarse hasta el próximo lunes), política con pronóstico
        if registrar and F_sem is not None and politica in ("pronostico",) and t >= CALENTAMIENTO:
            k = t - ultimo_lunes
            resto = 7 - k
            if t + resto <= DIAS and resto > 0:
                media = F_sem[k:].sum(axis=0)
                dem_resto = D[t:t + resto].sum(axis=0)
                tam = np.where(kg, TAM_KG, 1.0)
                for j in np.where(media > 0.05)[0]:
                    alertas.append((t, ids[j], clase_sem[j], resto, media[j], stock[j], dem_resto[j],
                                    lc._prob_agotar(stock[j], media[j], tam[j], bool(kg[j]))))
        v = np.minimum(D[t], np.clip(stock, 0, None))
        sold[t] = v
        lost[t] = D[t] - v
        stock = stock - v

    return {"sold": sold, "lost": lost, "disp": disp_dia, "compras": compras, "maestros": m,
            "calib": pd.DataFrame(calib, columns=["dia", "articulo_id", "clase", "D7", "sigma", "stock",
                                                  "demanda_7", "p_agotar_poisson"]),
            "alertas": pd.DataFrame(alertas, columns=["dia", "articulo_id", "clase", "dias_restantes", "media",
                                                      "stock", "demanda_resto", "p_agotar_poisson"]),
            "elec_final": elec, "disp_final": disp}


def resumir(r, D, info, politica):
    """Indicadores de la ventana evaluada (días 91-365), total y por clase de reporte
    (clase fija por ingreso de la DEMANDA VERDADERA del año, igual para todas las políticas)."""
    m = r["maestros"]
    ev = slice(CALENTAMIENTO, DIAS)
    precio, costo = m["precio"], m["costo"]
    clase_rep = p.clases_abc(D * precio)
    filas = []
    for c in ["Total", "A", "B", "C"]:
        mk = np.ones(len(precio), bool) if c == "Total" else (clase_rep == c)
        dem_v = (D[ev][:, mk] * precio[mk]).sum()
        perd_v = (r["lost"][ev][:, mk] * precio[mk]).sum()
        sold_u = r["sold"][ev][:, mk].sum(); dem_u = D[ev][:, mk].sum()
        gan = (r["sold"][ev][:, mk] * (precio[mk] / (1 + m["iva"][mk]) - costo[mk])).sum()
        cap = (np.clip(r["disp"][ev][:, mk], 0, None) * costo[mk]).sum(axis=1).mean()
        filas.append({"semilla": info["semilla"], "escenario": info["escenario"], "politica": politica, "clase": c,
                      "articulos": int(mk.sum()), "demanda_$": dem_v, "perdida_$": perd_v,
                      "perdida_%": perd_v / dem_v * 100 if dem_v else np.nan,
                      "fill_rate_%": sold_u / dem_u * 100 if dem_u else np.nan,
                      "ganancia_bruta_$": gan, "capital_prom_$": cap})
    renglones = [c[1] for c in r["compras"] if c[0] >= CALENTAMIENTO]
    for f in filas:
        f["renglones_por_viaje"] = float(np.mean(renglones)) if renglones else 0.0
    return filas


def correr(semilla, escenario, salida):
    D, info = generar(semilla, escenario)
    filas, calibs, alertas, elecs = [], [], [], []
    for pol in POLITICAS:
        t0 = time.time()
        r = simular(D, info, pol)
        filas += resumir(r, D, info, pol)
        if len(r["calib"]):
            calibs.append(r["calib"].assign(semilla=semilla, escenario=escenario, politica=pol))
        if len(r["alertas"]):
            alertas.append(r["alertas"].assign(semilla=semilla, escenario=escenario, politica=pol))
        if r["elec_final"]:
            elecs.append({"semilla": semilla, "escenario": escenario, "politica": pol,
                          **{f"modelo_{c}": r["elec_final"][c] for c in "ABC"},
                          **{f"disp_{c}": r["disp_final"].get(c) for c in "ABC"}})
        print(f"  {escenario} s{semilla} {pol}: {time.time() - t0:.0f}s", flush=True)
    tag = f"{escenario}_s{semilla}"
    pd.DataFrame(filas).to_csv(f"{salida}/res_{tag}.csv", index=False)
    if calibs: pd.concat(calibs).to_parquet(f"{salida}/calib_{tag}.parquet")
    if alertas: pd.concat(alertas).to_parquet(f"{salida}/alertas_{tag}.parquet")
    if elecs: pd.DataFrame(elecs).to_csv(f"{salida}/elec_{tag}.csv", index=False)


if __name__ == "__main__":
    escenario, semillas, salida = sys.argv[1], [int(s) for s in sys.argv[2].split(",")], sys.argv[3]
    import os
    os.makedirs(salida, exist_ok=True)
    for s in semillas:
        correr(s, escenario, salida)
