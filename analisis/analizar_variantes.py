"""
Resume las corridas de simular_variantes.py: mediana y p5-p95 de 20 años por
escenario y variante, y diferencias pareadas contra la app.
Uso: python analisis/analizar_variantes.py <carpeta_corridas> <carpeta_salida>
"""
import glob
import os
import sys

import pandas as pd

entrada, salida = sys.argv[1], sys.argv[2]
df = pd.concat([pd.read_csv(f) for f in glob.glob(os.path.join(entrada, "var_*.csv"))], ignore_index=True)
t = df[df["clase"] == "Total"]
res = (t.groupby(["escenario", "politica"])
        .agg(semillas=("semilla", "nunique"), perdida=("perdida_%", "median"), perdida_p5=("perdida_%", lambda s: s.quantile(.05)),
             perdida_p95=("perdida_%", lambda s: s.quantile(.95)), capital_M=("capital_prom_$", lambda s: s.median() / 1e6),
             ganancia_M=("ganancia_bruta_$", lambda s: s.median() / 1e6), servicio=("fill_rate_%", "median")).round(2).reset_index())
res.to_csv(os.path.join(salida, "variantes_resumen.csv"), index=False, encoding="utf-8-sig")
base = t[t["politica"] == "pronostico"].set_index(["escenario", "semilla"])
filas = []
for v, g in t.groupby("politica"):
    g = g.set_index(["escenario", "semilla"])
    d = pd.DataFrame({"perdida_pp": g["perdida_%"] - base["perdida_%"],
                      "capital_%": (g["capital_prom_$"] / base["capital_prom_$"] - 1) * 100,
                      "ganancia_%": (g["ganancia_bruta_$"] / base["ganancia_bruta_$"] - 1) * 100}).reset_index()
    for e, x in d.groupby("escenario"):
        filas.append({"escenario": e, "politica": v, "perdida_pp": x["perdida_pp"].median(), "capital_%": x["capital_%"].median(),
                      "ganancia_%": x["ganancia_%"].median(), "años_pierde_mas": int((x["perdida_pp"] > 0).sum()),
                      "años_mas_capital": int((x["capital_%"] > 0).sum())})
par = pd.DataFrame(filas).round(2)
par.to_csv(os.path.join(salida, "variantes_contra_app.csv"), index=False, encoding="utf-8-sig")
print(res.to_string())
print(par.to_string())
