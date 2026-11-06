"""
MAE y WAPE sobre la MISMA evaluación del pronóstico (backtest de origen móvil,
60 días de prueba, horizonte 7) de la base simulada oficial.

Tres medidas:
 1. MAE del total diario del almacén (unidades/día): la suma de los pronósticos
    por artículo contra la venta total de cada día del horizonte. Es la
    continuidad con la prueba de concepto agregada del Entregable 2.
 2. MAE por artículo y semana (unidades): |venta de 7 días - pronóstico de 7 días|
    promediado sobre artículos y orígenes, por clase.
 3. WAPE de 7 días por clase (%): suma de errores absolutos / suma de lo vendido.
    WAPE = MAE / venta media del mismo conjunto: ordena los modelos igual.

Uso: DB_NAME=almacen_sim python metricas_mae.py 2026-10-04 salida/
"""
import os
import sys

import numpy as np
import pandas as pd

import db
import pronostico as p

ETIQ = {"promedio_30": "Promedio 30 días (método anterior)", "ses_0.02": "SES α=0,02",
        "ses_0.05": "SES α=0,05", "ses_0.10": "SES α=0,10", "croston_0.05": "Croston-SBA α=0,05",
        "croston_0.10": "Croston-SBA α=0,10"}


def main(corte, salida):
    os.makedirs(salida, exist_ok=True)
    Y, I, art = p.preparar(db.engine, corte)
    obs, _ = p.observados(db.engine, Y, art)
    cats = dict(zip(art["articulo_id"], art["categoria"]))
    altas = dict(zip(art["articulo_id"], art["alta"]))
    T = len(Y)
    bt = p.backtest(Y, I, cats, dias=min(60, T - 28 - 7 + 1), horizonte=7, alta_por_art=altas, obs=obs)
    met = p.metricas(bt)
    elec = p.elegir_por_clase(met)

    # 2 y 3: por clase y configuración
    g = bt.assign(abs_h=(bt["real_h"] - bt["pron_h"]).abs())
    r = g.groupby(["modelo", "clase"]).agg(n=("abs_h", "size"), abs_h=("abs_h", "sum"), real=("real_h", "sum"),
                                           abs_d=("abs_diario", "sum")).reset_index()
    r["mae_semana_u"] = r["abs_h"] / r["n"]
    r["mae_diario_u"] = r["abs_d"] / (r["n"] * 7)
    r["venta_media_semana_u"] = r["real"] / r["n"]
    r["wape_7d_%"] = r["abs_h"] / r["real"] * 100
    r["elegido"] = [elec.get(c) == m for m, c in zip(r["modelo"], r["clase"])]
    r["configuracion"] = r["modelo"].map(ETIQ)
    r.round(3).to_csv(os.path.join(salida, "mae_wape_por_clase_y_modelo.csv"), index=False, encoding="utf-8-sig")

    # control: el WAPE recalculado coincide con el de la app
    chk = met.set_index(["modelo", "clase"])["wape_h_%"]
    dif = (r.set_index(["modelo", "clase"])["wape_7d_%"].round(1) - chk).abs().max()
    print(f"control WAPE vs pronostico.metricas: diferencia máxima {dif:.2f} pp")

    # 1: total diario del almacén (bottom-up), elegido por clase vs método anterior
    Yv, Iv = Y.values, I.values
    cats_l = [cats.get(a, "") for a in Y.columns]
    alta_l = [altas.get(a) for a in Y.columns]
    filas = []
    for t in range(max(28, T - 60 - 7 + 1), T - 7 + 1):
        fut = Y.index[t:t + 7]
        hist = Yv[:t]
        vivos = (hist > 0).any(axis=0)
        res = {cfg: p.pronosticar(hist, cats_l, fut, cfg, Iv[:t], alta_l, obs[:t])
               for cfg in {"promedio_30", *elec.values()}}
        clase = res["promedio_30"][1]["clase"]
        F_el = np.zeros((7, Yv.shape[1]))
        for c in "ABC":
            m = clase == c
            F_el[:, m] = res[elec[c]][0][:, m]
        F_an = res["promedio_30"][0]
        real = Yv[t:t + 7]
        for k in range(7):
            filas.append({"origen": Y.index[t], "dia_h": k + 1, "fecha": fut[k],
                          "real_total": real[k, vivos].sum(),
                          "pron_elegido": F_el[k, vivos].sum(), "pron_anterior": F_an[k, vivos].sum()})
    tot = pd.DataFrame(filas)
    tot.to_csv(os.path.join(salida, "total_diario_backtest.csv"), index=False)
    res_tot = []
    for nombre, col in (("Método anterior (promedio 30 días)", "pron_anterior"), ("Modelo elegido por clase", "pron_elegido")):
        for h, sub in (("1 día", tot[tot.dia_h == 1]), ("1 a 7 días", tot)):
            e = sub[col] - sub["real_total"]
            res_tot.append({"pronostico": nombre, "horizonte": h, "mae_u_dia": e.abs().mean(),
                            "venta_media_u_dia": sub["real_total"].mean(),
                            "wape_%": e.abs().sum() / sub["real_total"].sum() * 100,
                            "sesgo_%": e.sum() / sub["real_total"].sum() * 100})
    res_tot = pd.DataFrame(res_tot).round(2)
    res_tot.to_csv(os.path.join(salida, "mae_total_diario.csv"), index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 200)
    print("\nElección:", elec)
    print("\n1) Total diario del almacén:\n", res_tot.to_string(index=False))
    tab = r[r["modelo"].isin(["promedio_30"]) | r["elegido"]].copy()
    tab["tipo"] = np.where(tab["modelo"] == "promedio_30", "anterior", "elegido")
    print("\n2-3) Por clase:\n", tab[["clase", "tipo", "configuracion", "mae_semana_u", "mae_diario_u",
                                     "venta_media_semana_u", "wape_7d_%"]].sort_values(["clase", "tipo"]).round(2).to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
