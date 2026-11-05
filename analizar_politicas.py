"""
Informe de la comparación de políticas de reposición (HU-021, tanda 22): lee las
corridas de simular_politicas.py y arma las tablas y la figura del Entregable 4.

Para cada escenario y política: mediana y rango p5-p95 entre semillas de las
ventas perdidas, el nivel de servicio (fill rate: % de la demanda atendida) y el
capital inmovilizado (stock promedio a costo). Como todas las políticas corren
sobre la MISMA demanda de cada semilla, las diferencias se miden PAREADAS
(semilla por semilla), que es lo que vuelve comparables las reglas.

Salidas (en <carpeta_informe>):
  politicas_resumen.csv     total por escenario y política
  politicas_por_clase.csv   lo mismo por clase ABC
  politicas_pareada.csv     pronóstico vs regla actual y vs ABC, semilla por semilla
  politicas_tabla.txt       tabla de texto lista para el documento
  figura_politicas.svg      ventas perdidas vs capital inmovilizado (un punto por semilla)
Sin matplotlib: la figura se escribe como SVG a mano.

Uso: python analizar_politicas.py <carpeta_corridas> <carpeta_informe>
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

POL_ETQ = {"naive": "Regla actual", "abc": "ABC fija", "pronostico": "Por pronóstico (app)"}
ESC_ETQ = {"estable": "Popularidad estable", "cambiante": "Popularidad cambiante"}
COL = {"naive": "#2a78d6", "abc": "#eb6834", "pronostico": "#1baf7a"}
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
INDICADORES = ["perdida_%", "perdida_$", "fill_rate_%", "capital_prom_$", "ganancia_bruta_$", "renglones_por_viaje"]


def q(s, a):
    return float(np.quantile(s, a))


def leer(carpeta):
    archivos = sorted(glob.glob(os.path.join(carpeta, "res_*.csv")))
    if not archivos:
        raise SystemExit(f"No hay corridas (res_*.csv) en {carpeta}")
    return pd.concat([pd.read_csv(f) for f in archivos], ignore_index=True)


def resumen(res, por):
    filas = []
    for clave, g in res.groupby(por):
        f = dict(zip(por, clave)) | {"semillas": g["semilla"].nunique()}
        for col in INDICADORES:
            f[f"{col}_mediana"] = g[col].median()
            f[f"{col}_p5"], f[f"{col}_p95"] = q(g[col], .05), q(g[col], .95)
        filas.append(f)
    return pd.DataFrame(filas)


def pareada(tot):
    piv = tot.pivot_table(index=["escenario", "semilla"], columns="politica",
                          values=["perdida_$", "capital_prom_$", "ganancia_bruta_$"])
    filas = []
    for e in tot["escenario"].unique():
        x = piv.loc[e]
        for base in ["naive", "abc"]:
            red = (1 - x[("perdida_$", "pronostico")] / x[("perdida_$", base)]) * 100
            cap = (x[("capital_prom_$", "pronostico")] / x[("capital_prom_$", base)] - 1) * 100
            gan = (x[("ganancia_bruta_$", "pronostico")] / x[("ganancia_bruta_$", base)] - 1) * 100
            filas.append({"escenario": e, "comparacion": f"pronostico vs {base}", "semillas": len(red),
                          "reduccion_perdidas_mediana_%": red.median(), "reduccion_p5": q(red, .05),
                          "reduccion_p95": q(red, .95), "semillas_que_pierde_menos": int((red > 0).sum()),
                          "capital_cambio_mediana_%": cap.median(), "capital_p5": q(cap, .05),
                          "capital_p95": q(cap, .95), "semillas_con_menos_capital": int((cap < 0).sum()),
                          "ganancia_cambio_mediana_%": gan.median(), "ganancia_p5": q(gan, .05),
                          "ganancia_p95": q(gan, .95)})
    return pd.DataFrame(filas)


def pesos(x):
    return f"${x / 1e6:,.2f} M".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(x, dec=1):
    return f"{x:.{dec}f} %".replace(".", ",")


def tabla_texto(res_tot, res_cl, par):
    """Tabla para el documento: mediana (p5-p95) entre semillas."""
    lineas = []
    for e in ["estable", "cambiante"]:
        t = res_tot[res_tot["escenario"] == e].set_index("politica")
        if t.empty:
            continue
        lineas.append(f"{ESC_ETQ[e]} ({int(t['semillas'].iloc[0])} años simulados; mediana y rango p5-p95)")
        lineas.append(f"{'Política':<22}{'Ventas perdidas':>26}{'Nivel de servicio':>26}{'Capital inmovilizado':>34}")
        for pol in ["naive", "abc", "pronostico"]:
            r = t.loc[pol]
            lineas.append(
                f"{POL_ETQ[pol]:<22}"
                f"{pct(r['perdida_%_mediana']) + ' (' + pct(r['perdida_%_p5']) + '-' + pct(r['perdida_%_p95']) + ')':>26}"
                f"{pct(r['fill_rate_%_mediana']) + ' (' + pct(r['fill_rate_%_p5']) + '-' + pct(r['fill_rate_%_p95']) + ')':>26}"
                f"{pesos(r['capital_prom_$_mediana']) + ' (' + pesos(r['capital_prom_$_p5']) + '-' + pesos(r['capital_prom_$_p95']) + ')':>34}")
        c = res_cl[res_cl["escenario"] == e]
        lineas.append("  Nivel de servicio por clase (mediana):  " + " | ".join(
            f"{POL_ETQ[pol]}: " + ", ".join(
                f"{cl} {pct(c[(c.politica == pol) & (c.clase == cl)]['fill_rate_%_mediana'].iloc[0])}" for cl in "ABC")
            for pol in ["naive", "abc", "pronostico"]))
        for _, r in par[par["escenario"] == e].iterrows():
            base = POL_ETQ[r["comparacion"].split(" vs ")[1]]
            lineas.append(
                f"  Pronóstico vs {base}: pierde {pct(r['reduccion_perdidas_mediana_%'], 0)} menos de ventas "
                f"(p5-p95 {pct(r['reduccion_p5'], 0)} a {pct(r['reduccion_p95'], 0)}; menos en "
                f"{r['semillas_que_pierde_menos']} de {r['semillas']} años) y "
                f"{pct(abs(r['capital_cambio_mediana_%']), 0)} {'menos' if r['capital_cambio_mediana_%'] < 0 else 'más'} "
                f"de capital ({r['semillas_con_menos_capital']} de {r['semillas']} años con menos).")
        lineas.append("")
    lineas.append("Ventas perdidas = demanda no atendida por falta de stock, en pesos, sobre la demanda total.")
    lineas.append("Nivel de servicio = % de las unidades pedidas que se vendieron (fill rate).")
    lineas.append("Capital inmovilizado = stock promedio valuado a costo sin IVA. Días 91 a 365 de cada año.")
    return "\n".join(lineas)


def figura(tot, ruta):
    """Dos paneles (escenarios): x = capital inmovilizado, y = ventas perdidas %; un
    punto por semilla y política, y la mediana marcada. Lo ideal es abajo a la izquierda."""
    W, H, M = 900, 450, {"l": 70, "r": 20, "t": 96, "b": 52}
    paneles = [e for e in ["estable", "cambiante"] if e in set(tot["escenario"])]
    pw = (W - M["l"] - M["r"] - 50 * (len(paneles) - 1)) / len(paneles)
    ph = H - M["t"] - M["b"]
    x_max = tot["capital_prom_$"].max() / 1e6 * 1.08
    x_min = tot["capital_prom_$"].min() / 1e6 * 0.92
    y_max = 5.0 * np.ceil(tot["perdida_%"].max() / 5.0)
    y_ticks = np.arange(0, y_max + 1e-9, 5.0)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="Arial, sans-serif">',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
           f'<text x="{M["l"]}" y="24" font-size="16" fill="{INK}" font-weight="bold">'
           'Ventas perdidas y capital inmovilizado por política de reposición</text>',
           f'<text x="{M["l"]}" y="44" font-size="12" fill="{INK2}">Un punto por año simulado (misma demanda '
           'para las tres políticas); el círculo grande es la mediana. Mejor: abajo a la izquierda.</text>']
    for k, e in enumerate(paneles):
        x0 = M["l"] + k * (pw + 50)
        sx = lambda v: x0 + (v - x_min) / (x_max - x_min) * pw  # noqa: E731
        sy = lambda v: M["t"] + ph - v / y_max * ph            # noqa: E731
        out.append(f'<text x="{x0}" y="{M["t"] - 14}" font-size="13" fill="{INK}" font-weight="bold">{ESC_ETQ[e]}</text>')
        for v in y_ticks[1:]:
            out.append(f'<line x1="{x0}" x2="{x0 + pw}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="{GRID}"/>')
        for v in y_ticks:
            out.append(f'<text x="{x0 - 6}" y="{sy(v) + 4:.1f}" font-size="11" fill="{INK2}" text-anchor="end">'
                       f'{pct(v, 0)}</text>')
        for v in np.linspace(x_min, x_max, 5):
            etq = f"{v:.1f}".replace(".", ",")
            out.append(f'<text x="{sx(v):.1f}" y="{M["t"] + ph + 18}" font-size="11" fill="{INK2}" '
                       f'text-anchor="middle">{etq}</text>')
        out.append(f'<line x1="{x0}" x2="{x0 + pw}" y1="{M["t"] + ph}" y2="{M["t"] + ph}" stroke="{INK2}"/>')
        out.append(f'<text x="{x0 + pw / 2}" y="{M["t"] + ph + 38}" font-size="12" fill="{INK2}" '
                   'text-anchor="middle">Capital inmovilizado promedio (millones de $, a costo)</text>')
        if k == 0:
            out.append(f'<text transform="translate({x0 - 48},{M["t"] + ph / 2}) rotate(-90)" font-size="12" '
                       f'fill="{INK2}" text-anchor="middle">Ventas perdidas (% de la demanda)</text>')
        g = tot[tot["escenario"] == e]
        for pol in ["naive", "abc", "pronostico"]:
            gp = g[g["politica"] == pol]
            for cap, perd in zip(gp["capital_prom_$"], gp["perdida_%"]):
                out.append(f'<circle cx="{sx(cap / 1e6):.1f}" cy="{sy(perd):.1f}" r="3.2" '
                           f'fill="{COL[pol]}" fill-opacity="0.45"/>')
            mx, my = gp["capital_prom_$"].median() / 1e6, gp["perdida_%"].median()
            out.append(f'<circle cx="{sx(mx):.1f}" cy="{sy(my):.1f}" r="7" fill="{COL[pol]}" '
                       f'stroke="#ffffff" stroke-width="2"/>')
    ly = 66
    lx = M["l"]
    for pol in ["naive", "abc", "pronostico"]:
        out.append(f'<circle cx="{lx}" cy="{ly - 4}" r="6" fill="{COL[pol]}"/>')
        out.append(f'<text x="{lx + 12}" y="{ly}" font-size="12" fill="{INK}">{POL_ETQ[pol]}</text>')
        lx += 190
    out.append("</svg>")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


if __name__ == "__main__":
    corridas, salida = sys.argv[1], sys.argv[2]
    os.makedirs(salida, exist_ok=True)
    res = leer(corridas)
    tot = res[res["clase"] == "Total"].copy()
    r_tot = resumen(tot, ["escenario", "politica"])
    r_cl = resumen(res[res["clase"] != "Total"], ["escenario", "politica", "clase"])
    par = pareada(tot)
    r_tot.to_csv(f"{salida}/politicas_resumen.csv", index=False, encoding="utf-8-sig")
    r_cl.to_csv(f"{salida}/politicas_por_clase.csv", index=False, encoding="utf-8-sig")
    par.to_csv(f"{salida}/politicas_pareada.csv", index=False, encoding="utf-8-sig")
    txt = tabla_texto(r_tot, r_cl, par)
    with open(f"{salida}/politicas_tabla.txt", "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    figura(tot, f"{salida}/figura_politicas.svg")
    print(txt)
