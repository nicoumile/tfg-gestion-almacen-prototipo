"""
Comparación de políticas de reposición (HU-021, tanda 22, 27/10/2026).

Criterio de aceptación (Entregable 3): "dada la misma demanda simulada, cuando
se comparan las políticas actual, ABC y por pronóstico, entonces se informan
las ventas perdidas, el nivel de servicio y el capital inmovilizado de cada una".

Banco de pruebas: demanda_sim.generar(semilla, escenario) da la DEMANDA
VERDADERA (lo que los clientes quieren llevar) de un año, por día y artículo,
sobre el catálogo real. La misma demanda se resuelve contra el stock de cada
política: la única diferencia entre corridas es la regla de compra.

Mecánica:
- venta del día = min(demanda, stock); lo que falta es venta perdida.
- revisión de compras los lunes (y el día 0): un viaje por semana, como hace
  el comerciante; la compra entra ese mismo día.
- stock inicial = el del catálogo real exportado.
- calentamiento: los primeros 90 días todas las políticas usan la regla actual
  (hace falta historia para el ABC y el pronóstico). Se evalúa del día 91 al 365.
- las políticas ABC y por pronóstico ven solo lo VENDIDO (lo que registraría la
  app), nunca la demanda verdadera.

Políticas:
 1 naive (regla actual): la que usó el generador de la base simulada oficial:
   si stock < max(stock_mínimo, 2), reponer hasta max(stock_inicial, umbral x 4, 8).
 2 abc (cobertura fija por clase, sin pronóstico): S = venta diaria promedio de
   90 días x (días de seguridad A 15 / B 8 / C 3 + 7 del viaje); la clase ABC se
   recalcula cada lunes con los ingresos de 90 días.
 3 pronostico (la app): pronostico.pronosticar con el modelo elegido por clase y
   la corrección de demanda censurada, y lista_compras.calcular con el nivel de
   servicio A 95 / B 90 / C 80. Elección de modelo y dispersión del error se
   reestiman con el backtest de la app (60 días, horizonte 7) al terminar el
   calentamiento y cada 8 semanas, con lo observado hasta ese día.
Usa SIN CAMBIOS el código de la app (pronostico.py y lista_compras.py).

Antecedente: la misma comparación se corrió el 23-24/10 fuera del repo, con el
pronóstico de la tanda 16 (claude/robustez-calibracion-metricas-24oct.md; ahí
figuraba como "HU-018" con la numeración vieja).

Uso: python analisis/simular_politicas.py <escenario> <semillas> <carpeta_salida>
     python analisis/simular_politicas.py estable 1,2,3 resultados_politicas
Escenarios: estable | cambiante (ver demanda_sim.py). ~1 min por semilla.
Después: python analisis/analizar_politicas.py resultados_politicas <carpeta_informe>
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))  # raíz del repositorio
import os
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
POLITICAS = ["naive", "abc", "pronostico"]


def maestros(ids):
    cat, _ = catalogo()
    a = cat.set_index("id").loc[ids]
    costo0 = a["costo_compra_actual"].fillna(0).astype(float).values
    # aumentos trimestrales de 3-8 % por proveedor (generador): en promedio ~ +11 % en el año.
    # Se valoriza a precio y costo promedio del año: factor 1,11 a los dos.
    f = 1.11
    return {
        "nombre": a["nombre"].values, "categoria": a["categoria"].values,
        "kg": (a["unidad_medida"] == "kg").values, "unidad": a["unidad_medida"].values,
        "st0": np.clip(a["stock_actual"].astype(float).values, 0, None),
        "smin": a["stock_minimo"].astype(float).values,
        "iva": a["iva"].astype(float).values,
        "costo": costo0 * f,                                           # sin IVA
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
    """Lo que hace la app (pronostico.evaluar) con lo observado hasta hoy:
    modelo elegido y dispersión del error por clase."""
    Y = pd.DataFrame(Yh, index=fechas, columns=ids)
    I = pd.DataFrame(Ih, index=fechas, columns=ids)
    dias = min(60, len(Y) - 28 - 7 + 1)
    bt = p.backtest(Y, I, cats_dict, dias=dias, horizonte=7, obs=obs)
    elec = p.elegir_por_clase(p.metricas(bt))
    for c in "ABC":
        elec.setdefault(c, p.CONFIG_POR_DEFECTO)
    sig = p.error_por_clase(bt, elec)
    disp = {r.clase: (float(r.dispersion) if pd.notna(r.dispersion) else 1.0) for r in sig.itertuples()}
    return elec, disp


def compra_naive(stock, m):
    umbral = np.maximum(m["smin"], 2)
    objetivo = np.maximum.reduce([m["st0"], umbral * 4, np.full(len(stock), 8.0)])
    return np.where(stock < umbral, np.ceil(objetivo - stock), 0.0)


def compra_abc(stock, sold, t, m):
    clase = p.clases_abc(sold[:t][-p.VENTANA_ABC:] * m["precio"])
    v = sold[max(0, t - 90):t].mean(axis=0)
    S = v * np.array([SEGURIDAD_ABC.get(c, 3) + 7 for c in clase])
    return np.where(S > stock, redondear(S - stock, m["kg"]), 0.0)


def simular(D, info, politica):
    """Un año de operación con la regla `politica` contra la demanda D (DIAS x n)."""
    ids, fechas = info["ids"], info["fechas"]
    n = len(ids)
    m = maestros(ids)
    cats = list(m["categoria"])
    cats_dict = dict(zip(ids, cats))
    stock = m["st0"].copy()
    sold = np.zeros((DIAS, n)); lost = np.zeros((DIAS, n))
    ini = np.zeros((DIAS, n))            # stock al final del día anterior (como la reconstrucción de la app)
    disp_dia = np.zeros((DIAS, n))       # stock disponible ese día (después de la compra)
    compras = []                         # (día, renglones, unidades, costo)
    elec, disp, ultimo_eval = None, None, None
    meta_base = pd.DataFrame({
        "articulo_id": ids, "nombre": m["nombre"], "categoria": cats, "unidad_medida": m["unidad"],
        "costo": m["costo"], "precio": m["precio"], "costo_c_iva": m["costo"] * (1 + m["iva"]),
        "reposicion_dias": np.nan, "vida_util_dias": np.nan, "nivel_servicio": np.nan,
        "articulo_fraccion_de": np.nan, "tam_venta": np.where(m["kg"], TAM_KG, np.nan), "bulto": 1.0,
        "proveedor": "Sim", "consignacion": False,
    })
    params = {"frecuencia_viaje_dias": 7, "valor_recupero_pct": 0.0,
              **{f"nivel_servicio_{c}": v for c, v in NIVEL.items()}}

    for t in range(DIAS):
        d = fechas[t]
        ini[t] = stock
        if d.weekday() == 0 or t == 0:
            pol = politica if t >= CALENTAMIENTO else "naive"
            if pol == "naive":
                q = compra_naive(stock, m)
            elif pol == "abc":
                q = compra_abc(stock, sold, t, m)
            else:
                Yh, Ih = sold[:t], sold[:t] * m["precio"]
                obs = obs_censura(Yh, ini[:t])
                if elec is None or (t - ultimo_eval) >= REEVALUAR_CADA:
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
                q = np.zeros(n)
                if not viaje.empty:
                    q = pd.Series(viaje["Comprar"].values, index=viaje["articulo_id"].values) \
                          .reindex(ids).fillna(0).values
            if q.sum() > 0:
                compras.append((t, int((q > 0).sum()), float(q.sum()), float((q * m["costo"]).sum())))
            stock = stock + q
        disp_dia[t] = stock
        v = np.minimum(D[t], np.clip(stock, 0, None))
        sold[t] = v
        lost[t] = D[t] - v
        stock = stock - v

    return {"sold": sold, "lost": lost, "disp": disp_dia, "compras": compras, "maestros": m,
            "elec_final": elec, "disp_final": disp}


def resumir(r, D, info, politica):
    """Indicadores de la ventana evaluada (días 91-365), total y por clase de reporte
    (clase fija por ingreso de la DEMANDA VERDADERA del año, igual para todas las
    políticas, para que cada clase reúna los mismos artículos en las tres)."""
    m = r["maestros"]
    ev = slice(CALENTAMIENTO, DIAS)
    precio, costo = m["precio"], m["costo"]
    clase_rep = p.clases_abc(D * precio)
    renglones = [c[1] for c in r["compras"] if c[0] >= CALENTAMIENTO]
    filas = []
    for c in ["Total", "A", "B", "C"]:
        mk = np.ones(len(precio), bool) if c == "Total" else (clase_rep == c)
        dem_v = (D[ev][:, mk] * precio[mk]).sum()
        perd_v = (r["lost"][ev][:, mk] * precio[mk]).sum()
        sold_u, dem_u = r["sold"][ev][:, mk].sum(), D[ev][:, mk].sum()
        gan = (r["sold"][ev][:, mk] * (precio[mk] / (1 + m["iva"][mk]) - costo[mk])).sum()
        cap = (np.clip(r["disp"][ev][:, mk], 0, None) * costo[mk]).sum(axis=1).mean()
        filas.append({"semilla": info["semilla"], "escenario": info["escenario"], "politica": politica,
                      "clase": c, "articulos": int(mk.sum()), "demanda_$": dem_v, "perdida_$": perd_v,
                      "perdida_%": perd_v / dem_v * 100 if dem_v else np.nan,
                      "fill_rate_%": sold_u / dem_u * 100 if dem_u else np.nan,
                      "ganancia_bruta_$": gan, "capital_prom_$": cap,
                      "renglones_por_viaje": float(np.mean(renglones)) if renglones else 0.0})
    return filas


def correr(semilla, escenario, salida):
    D, info = generar(semilla, escenario)
    filas, elecs = [], []
    for pol in POLITICAS:
        t0 = time.time()
        r = simular(D, info, pol)
        filas += resumir(r, D, info, pol)
        if r["elec_final"]:
            elecs.append({"semilla": semilla, "escenario": escenario, "politica": pol,
                          **{f"modelo_{c}": r["elec_final"][c] for c in "ABC"},
                          **{f"disp_{c}": r["disp_final"].get(c) for c in "ABC"}})
        print(f"  {escenario} s{semilla} {pol}: {time.time() - t0:.0f}s", flush=True)
    tag = f"{escenario}_s{semilla}"
    pd.DataFrame(filas).to_csv(f"{salida}/res_{tag}.csv", index=False)
    if elecs:
        pd.DataFrame(elecs).to_csv(f"{salida}/elec_{tag}.csv", index=False)


if __name__ == "__main__":
    escenario, semillas, salida = sys.argv[1], [int(s) for s in sys.argv[2].split(",")], sys.argv[3]
    os.makedirs(salida, exist_ok=True)
    for s in semillas:
        correr(s, escenario, salida)
