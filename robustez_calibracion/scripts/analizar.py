"""
Análisis de las corridas de simular_politicas.py: robustez (intervalos entre
semillas y escenarios) y calibración (nivel de servicio logrado y P(agotarse)).
Uso: python analizar.py out_runs out_analisis
"""
import glob
import math
import os
import sys
from statistics import NormalDist

import numpy as np
import pandas as pd

N = NormalDist()
NIVEL = {"A": 0.95, "B": 0.90, "C": 0.80}
POL_ETQ = {"naive": "Regla actual", "abc": "Regla ABC (cobertura)", "pronostico": "Lista con pronóstico",
           "pronostico_sin_censura": "Pronóstico sin corrección de censura"}
ESC_ETQ = {"estable": "Popularidad estable", "cambiante": "Popularidad cambiante"}
COL = {"naive": "#2a78d6", "abc": "#eb6834", "pronostico": "#1baf7a", "pronostico_sin_censura": "#eda100"}
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def q(s, a):
    return float(np.quantile(s, a))


def robustez(res, salida):
    tot = res[res.clase == "Total"].copy()
    # resumen por escenario y política: mediana y rango p5-p95 entre semillas
    filas = []
    for (e, pol), g in tot.groupby(["escenario", "politica"]):
        f = {"escenario": e, "politica": pol, "semillas": len(g)}
        for col in ["perdida_%", "fill_rate_%", "capital_prom_$", "ganancia_bruta_$", "renglones_por_viaje"]:
            f[f"{col}_mediana"] = g[col].median(); f[f"{col}_p5"] = q(g[col], .05); f[f"{col}_p95"] = q(g[col], .95)
        filas.append(f)
    resumen = pd.DataFrame(filas)
    resumen.to_csv(f"{salida}/robustez_resumen.csv", index=False, encoding="utf-8-sig")

    # diferencias PAREADAS por semilla (misma demanda): pronóstico vs naive y vs ABC
    piv = tot.pivot_table(index=["escenario", "semilla"], columns="politica",
                          values=["perdida_$", "capital_prom_$", "ganancia_bruta_$", "perdida_%"])
    pares = []
    for e in tot.escenario.unique():
        x = piv.loc[e]
        for base in ["naive", "abc"]:
            for comp in ["pronostico", "pronostico_sin_censura"] if base == "naive" else ["pronostico"]:
                red = (1 - x[("perdida_$", comp)] / x[("perdida_$", base)]) * 100
                cap = (x[("capital_prom_$", comp)] / x[("capital_prom_$", base)] - 1) * 100
                gan = (x[("ganancia_bruta_$", comp)] / x[("ganancia_bruta_$", base)] - 1) * 100
                pares.append({"escenario": e, "comparacion": f"{comp} vs {base}", "semillas": len(red),
                              "reduccion_perdidas_mediana_%": red.median(), "p5": q(red, .05), "p95": q(red, .95),
                              "min": red.min(), "max": red.max(), "semillas_que_mejora": int((red > 0).sum()),
                              "capital_cambio_mediana_%": cap.median(), "capital_p5": q(cap, .05), "capital_p95": q(cap, .95),
                              "ganancia_cambio_mediana_%": gan.median(), "ganancia_p5": q(gan, .05), "ganancia_p95": q(gan, .95)})
        # aporte de la corrección de censura
        red = (1 - x[("perdida_$", "pronostico")] / x[("perdida_$", "pronostico_sin_censura")]) * 100
        cap = (x[("capital_prom_$", "pronostico")] / x[("capital_prom_$", "pronostico_sin_censura")] - 1) * 100
        pares.append({"escenario": e, "comparacion": "censura: pronostico vs pronostico_sin_censura",
                      "semillas": len(red), "reduccion_perdidas_mediana_%": red.median(), "p5": q(red, .05),
                      "p95": q(red, .95), "min": red.min(), "max": red.max(),
                      "semillas_que_mejora": int((red > 0).sum()), "capital_cambio_mediana_%": cap.median(),
                      "capital_p5": q(cap, .05), "capital_p95": q(cap, .95)})
    pares = pd.DataFrame(pares)
    pares.to_csv(f"{salida}/robustez_pareada.csv", index=False, encoding="utf-8-sig")

    # por clase (mediana entre semillas)
    cl = res[res.clase != "Total"].groupby(["escenario", "politica", "clase"])[["perdida_%", "fill_rate_%"]] \
        .agg(["median", lambda s: q(s, .05), lambda s: q(s, .95)])
    cl.columns = [f"{a}_{ {'median': 'mediana', '<lambda_0>': 'p5', '<lambda_1>': 'p95'}[b]}" for a, b in cl.columns]
    cl.reset_index().to_csv(f"{salida}/robustez_por_clase.csv", index=False, encoding="utf-8-sig")
    return resumen, pares, cl.reset_index()


def nb_prob_agotar(stock, media, disp):
    """P(X > stock) con X binomial negativa de media `media` y varianza disp²·media
    (Poisson si disp <= 1). Alternativa a la Poisson de las alertas."""
    var = (disp ** 2) * media
    if media <= 0:
        return 0.0
    n = int(math.floor(max(stock, 0) + 1e-9))
    if var <= media * 1.0001:
        t = acc = math.exp(-media)
        for k in range(1, n + 1):
            t *= media / k; acc += t
        return max(0.0, 1 - acc)
    pp = media / var                    # prob. de éxito
    r = media * pp / (1 - pp)
    t = acc = pp ** r                    # P(0)
    for k in range(1, n + 1):
        t *= (k - 1 + r) / k * (1 - pp); acc += t
    return max(0.0, 1 - acc)


def calibracion(salida, carpeta):
    cal = pd.concat([pd.read_parquet(f) for f in glob.glob(f"{carpeta}/calib_*.parquet")], ignore_index=True)
    al = pd.concat([pd.read_parquet(f) for f in glob.glob(f"{carpeta}/alertas_*.parquet")], ignore_index=True)
    el = pd.concat([pd.read_csv(f) for f in glob.glob(f"{carpeta}/elec_*.csv")], ignore_index=True)
    cal = cal[(cal.dia >= 90) & (cal.stock > 1e-9)].copy()
    cal["quiebre"] = cal["demanda_7"] > cal["stock"] + 1e-9
    z = (cal["stock"] - cal["D7"]) / cal["sigma"].clip(lower=1e-9)
    cal["p_normal"] = 1 - z.map(N.cdf)          # lo que supone el stock objetivo (normal con dispersión)
    # nivel de servicio por ciclo: objetivo vs lo que el modelo cree tener (incluye redondeo y stock previo) vs logrado
    ns = cal.groupby(["escenario", "politica", "clase"]).agg(
        ciclos=("quiebre", "size"), logrado=("quiebre", lambda s: 1 - s.mean()),
        creido=("p_normal", lambda s: 1 - s.mean())).reset_index()
    ns["objetivo"] = ns["clase"].map(NIVEL)
    ns.to_csv(f"{salida}/nivel_servicio_por_clase.csv", index=False, encoding="utf-8-sig")

    # alertas: P de agotarse hasta el próximo viaje (Poisson, app) vs alternativa con sobredispersión
    # con stock 0 no hay nada que predecir (ya está sin stock; para lo que va por kg la
    # función devuelve 1 a propósito): esos estados no entran en la calibración
    al = al[(al.dia >= 90) & (al.stock > 1e-9)].copy()
    al["quiebre"] = al["demanda_resto"] > al["stock"] + 1e-9
    d = el[el.politica == "pronostico"].set_index(["escenario", "semilla"])
    def disp_de(r):
        return d.loc[(r.escenario, r.semilla), f"disp_{r.clase}"]
    dl = d.reset_index().melt(id_vars=["escenario", "semilla"], value_vars=["disp_A", "disp_B", "disp_C"],
                              var_name="clase", value_name="disp")
    dl["clase"] = dl["clase"].str[-1]
    al = al.merge(dl, on=["escenario", "semilla", "clase"], how="left")
    al["p_nb"] = nb_vector(al["stock"].values, al["media"].values, al["disp"].fillna(1.0).values)
    # misma alternativa para el registro de los lunes (control)
    cal = cal.merge(dl, on=["escenario", "semilla", "clase"], how="left")
    cal["p_nb"] = nb_vector(cal["stock"].values, cal["D7"].values, cal["disp"].fillna(1.0).values)
    return cal, ns, al


def nb_vector(stock, media, disp):
    """Versión vectorizada de nb_prob_agotar (scipy). Para lo que se vende por kg
    usa el mismo criterio discreto: es una aproximación."""
    from scipy.stats import nbinom, poisson
    k = np.floor(np.clip(stock, 0, None) + 1e-9)
    media = np.clip(media, 1e-12, None)
    var = disp ** 2 * media
    out = poisson.sf(k, media)
    sobre = var > media * 1.0001
    pp = media[sobre] / var[sobre]
    r = media[sobre] * pp / (1 - pp)
    out[sobre] = nbinom.sf(k[sobre], r, pp)
    return out


def confiabilidad(p, y, bins):
    b = np.clip(np.digitize(p, bins) - 1, 0, len(bins) - 2)
    t = pd.DataFrame({"b": b, "p": p, "y": y.astype(float)}).groupby("b").agg(
        n=("y", "size"), p_media=("p", "mean"), frecuencia=("y", "mean")).reset_index()
    ece = float((t["n"] * (t["p_media"] - t["frecuencia"]).abs()).sum() / t["n"].sum())
    brier = float(((p - y.astype(float)) ** 2).mean())
    return t, ece, brier


def figuras(res, pares, ns, cal, al, salida):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": INK2,
                         "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2, "text.color": INK})
    pols = ["naive", "abc", "pronostico", "pronostico_sin_censura"]
    tot = res[res.clase == "Total"]

    # Figura A: ventas perdidas vs capital, cada punto una semilla, por escenario
    fig, axs = plt.subplots(1, 2, figsize=(10, 4.4), sharey=True)
    for ax, e in zip(axs, ["estable", "cambiante"]):
        for pol in pols:
            g = tot[(tot.escenario == e) & (tot.politica == pol)]
            ax.scatter(g["capital_prom_$"] / 1e6, g["perdida_%"], s=36, color=COL[pol], label=POL_ETQ[pol],
                       edgecolor="white", linewidth=1.2, zorder=3)
        ax.set_title(ESC_ETQ[e], fontsize=11)
        ax.set_xlabel("Capital promedio en mercadería (millones de $, a costo)")
        ax.grid(alpha=1, color=GRID, lw=0.8); ax.set_axisbelow(True)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    axs[0].set_ylabel("Ventas perdidas por falta de stock (% de la demanda en $)")
    axs[0].legend(frameon=False, fontsize=9, loc="upper right")
    fig.suptitle("Cada punto es una trayectoria de demanda simulada (20 por escenario): misma demanda para las cuatro reglas",
                 fontsize=10, color=INK2)
    fig.tight_layout()
    fig.savefig(f"{salida}/figura_robustez_politicas.png", dpi=200); plt.close(fig)

    # Figura B: nivel de servicio objetivo vs logrado (lista con pronóstico)
    fig, ax = plt.subplots(figsize=(7.6, 4.6))
    g = ns[ns.politica == "pronostico"]
    x = np.arange(3); w = 0.26
    for i, (e, col) in enumerate([("estable", "#2a78d6"), ("cambiante", "#eb6834")]):
        v = g[g.escenario == e].set_index("clase").loc[list("ABC"), "logrado"] * 100
        bars = ax.bar(x + (i - 0.5) * (w + 0.03), v, w, color=col, label=f"Logrado — {ESC_ETQ[e].lower()}",
                      edgecolor="white", linewidth=2)
        for b, val in zip(bars, v):
            ax.text(b.get_x() + b.get_width() / 2, val + 0.6, f"{val:.1f}".replace(".", ","), ha="center", fontsize=9)
    for j, c in enumerate("ABC"):
        ax.hlines(NIVEL[c] * 100, j - 0.45, j + 0.45, colors=INK, linestyles=(0, (4, 3)), lw=1.4)
    ax.plot([], [], color=INK, ls=(0, (4, 3)), lw=1.4, label="Objetivo configurado")
    ax.set_xticks(x, [f"Clase {c}" for c in "ABC"])
    ax.set_ylim(60, 102)
    ax.set_ylabel("Semanas sin quedarse sin stock (%)")
    ax.legend(frameon=False, fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    ax.grid(axis="y", color=GRID); ax.set_axisbelow(True)
    ax.set_title("Nivel de servicio por clase: objetivo de la lista de compras vs logrado", fontsize=11)
    fig.tight_layout(); fig.savefig(f"{salida}/figura_nivel_servicio.png", dpi=200); plt.close(fig)

    # Figura C: diagramas de confiabilidad de la P(agotarse)
    bins = np.array([0, .02, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, .98, 1.0001])
    fig, axs = plt.subplots(1, 2, figsize=(10, 4.6))
    a_est = al[al.escenario == "estable"]
    c_est = cal[(cal.escenario == "estable") & (cal.politica == "pronostico")]
    paneles = [
        (axs[0], "Alertas de quiebre (hasta el próximo viaje)",
         [("Poisson (la app hoy)", a_est.p_agotar_poisson.values, a_est.quiebre.values, "#2a78d6"),
          ("Con sobredispersión (binomial negativa)", a_est.p_nb.values, a_est.quiebre.values, "#eb6834")]),
        (axs[1], "Stock objetivo de la lista (escala 0-20 %)",
         [("Normal con dispersión (la app hoy)", c_est.p_normal.values, c_est.quiebre.values, "#2a78d6"),
          ("Poisson", c_est.p_agotar_poisson.values, c_est.quiebre.values, "#1baf7a")]),
    ]
    for (ax, titulo, series), lim in zip(paneles, (1.0, 0.2)):
        ax.plot([0, lim], [0, lim], color=INK2, lw=1, ls=(0, (4, 3)), label="Calibración perfecta")
        for nombre, pp, yy, col in series:
            t, ece, br = confiabilidad(pp, yy, bins)
            t = t[t.n >= 50]
            ax.plot(t.p_media, t.frecuencia, color=col, lw=2, marker="o", ms=5,
                    label=f"{nombre}  (ECE {ece * 100:.1f} pp)".replace(".", ","))
        ax.set_xlim(0, lim); ax.set_ylim(0, lim)
        ax.set_xlabel("Probabilidad de agotarse que da el modelo"); ax.set_ylabel("Frecuencia observada de quiebre")
        ax.set_title(titulo, fontsize=10.5)
        ax.legend(frameon=False, fontsize=8.5, loc="upper left")
        ax.grid(color=GRID); ax.set_axisbelow(True)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    fig.suptitle("Calibración: cuando el modelo dice X %, ¿se agota X de cada 100 veces? (escenario estable, 20 semillas)",
                 fontsize=10, color=INK2)
    fig.tight_layout(); fig.savefig(f"{salida}/figura_calibracion.png", dpi=200); plt.close(fig)


def main(carpeta, salida):
    os.makedirs(salida, exist_ok=True)
    res = pd.concat([pd.read_csv(f) for f in glob.glob(f"{carpeta}/res_*.csv")], ignore_index=True)
    resumen, pares, cl = robustez(res, salida)
    cal, ns, al = calibracion(salida, carpeta)
    bins = np.array([0, .02, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, .98, 1.0001])
    filas = []
    for e in ["estable", "cambiante"]:
        a = al[al.escenario == e]; c = cal[(cal.escenario == e) & (cal.politica == "pronostico")]
        for nombre, pp, yy in [("alertas_poisson", a.p_agotar_poisson.values, a.quiebre.values),
                               ("alertas_binomial_negativa", a.p_nb.values, a.quiebre.values),
                               ("stock_objetivo_normal", c.p_normal.values, c.quiebre.values),
                               ("stock_objetivo_poisson", c.p_agotar_poisson.values, c.quiebre.values)]:
            t, ece, br = confiabilidad(pp, yy, bins)
            t.assign(escenario=e, modelo=nombre).to_csv(f"{salida}/confiabilidad_{e}_{nombre}.csv", index=False)
            filas.append({"escenario": e, "modelo": nombre, "registros": len(pp), "prob_media_%": pp.mean() * 100,
                          "frecuencia_%": yy.mean() * 100, "ECE_pp": ece * 100, "Brier": br})
    cal_res = pd.DataFrame(filas)
    cal_res.to_csv(f"{salida}/calibracion_resumen.csv", index=False, encoding="utf-8-sig")
    figuras(res, pares, ns, cal, al, salida)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(resumen[["escenario", "politica", "semillas", "perdida_%_mediana", "perdida_%_p5", "perdida_%_p95",
                   "capital_prom_$_mediana", "ganancia_bruta_$_mediana", "renglones_por_viaje_mediana"]].round(2))
    print(pares.round(1).to_string())
    print(cl.round(1).to_string())
    print(ns.round(3).to_string())
    print(cal_res.round(3).to_string())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
