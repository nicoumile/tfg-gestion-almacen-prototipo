"""
Experimento para el TFG (tanda 9), sobre la base simulada: efecto de corregir
la demanda censurada y de separar la elección del modelo de la medición del
error, medido contra la DEMANDA VERDADERA (vendido + perdido por quiebre),
que se conoce porque el generador se puede volver a correr en seco con la
misma semilla (eval_t9/generar_seco.py).

Uso: DB_NAME=almacen_sim python analisis/experimento_censura.py 2026-10-05 demanda_verdadera.csv <carpeta_salida>
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))  # raíz del repositorio
import os
import sys

import numpy as np
import pandas as pd

import db
import pronostico as p

hoy, archivo_dem, salida = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(salida, exist_ok=True)
Y, I, art = p.preparar(db.engine, hoy)
obs, info = p.dias_observados(Y, art, p.movimientos_stock(db.engine))
dem = pd.read_csv(archivo_dem, parse_dates=["fecha"])
D = dem.pivot_table(index="fecha", columns="articulo_id", values="demanda", aggfunc="sum") \
       .reindex(index=Y.index, columns=Y.columns).fillna(0.0)
cats = dict(zip(art["articulo_id"], art["categoria"]))
altas = dict(zip(art["articulo_id"], art["alta"]))
dias = min(60, len(Y) - 28 - 7 + 1)
print(f"Serie {Y.shape}, {dias} orígenes. Días censurados detectados: {info['dias_censurados']} "
      f"({(~obs).mean() * 100:.1f} %). Demanda perdida en el período: "
      f"{(1 - Y.values.sum() / D.values.sum()) * 100:.1f} %")

pos = {f: i for i, f in enumerate(Y.index)}
col = {a: j for j, a in enumerate(Y.columns)}


def con_verdadera(bt):
    o = bt["origen"].map(pos).values
    j = bt["articulo_id"].map(col).values
    Dv = D.values
    bt = bt.copy()
    bt["verdadera_h"] = [Dv[a:a + 7, b].sum() for a, b in zip(o, j)]
    if "censurado_h" not in bt.columns or not bt["censurado_h"].any():
        bt["censurado_h"] = [not obs[a:a + 7, b].all() for a, b in zip(o, j)]
    return bt


bt = {"sin corrección": con_verdadera(p.backtest(Y, I, cats, dias=dias, horizonte=7, alta_por_art=altas)),
      "con corrección": con_verdadera(p.backtest(Y, I, cats, dias=dias, horizonte=7, alta_por_art=altas, obs=obs))}


def contra(b, real):
    """metricas() pero contra la columna `real` (verdadera_h o real_h) en todas las semanas."""
    x = b.assign(real_h=b[real], censurado_h=False)
    return p.metricas(x, excluir_censurados=False)


filas = []
for nombre, b in bt.items():
    for medida, f in (("demanda verdadera", lambda b: contra(b, "verdadera_h")),
                      ("venta registrada, todas las semanas", lambda b: contra(b, "real_h")),
                      ("venta registrada, semanas sin quiebre", lambda b: p.metricas(b))):
        m = f(b)
        m["variante"], m["medido_contra"] = nombre, medida
        filas.append(m)
todo = pd.concat(filas, ignore_index=True)
todo.to_csv(os.path.join(salida, "censura_metricas.csv"), index=False, encoding="utf-8-sig")
pd.set_option("display.width", 230)
for medida in todo["medido_contra"].unique():
    t = todo[todo["medido_contra"] == medida]
    print(f"\n== {medida}: WAPE 7 días % (sesgo %)")
    w = t.pivot_table(index="modelo", columns=["clase", "variante"], values="wape_h_%")
    s = t.pivot_table(index="modelo", columns=["clase", "variante"], values="sesgo_%")
    print((w.round(1).astype(str) + " (" + s.round(1).astype(str) + ")").to_string())

# ¿Qué criterio de medición elige el modelo que de verdad es mejor?
print("\n== Elección por clase según el criterio, y su error contra la demanda verdadera")
res = []
for nombre, b in bt.items():
    verd = contra(b, "verdadera_h")
    for criterio, m in (("contra demanda verdadera (ideal, no disponible en la realidad)", verd),
                        ("venta registrada, todas las semanas", contra(b, "real_h")),
                        ("venta registrada, semanas sin quiebre", p.metricas(b))):
        ele = p.elegir_por_clase(m)
        for c, cfg in ele.items():
            g = verd[(verd["clase"] == c) & (verd["modelo"] == cfg)].iloc[0]
            res.append({"variante": nombre, "criterio": criterio, "clase": c, "elegido": cfg,
                        "wape_verdadero_%": g["wape_h_%"], "sesgo_verdadero_%": g["sesgo_%"]})
res = pd.DataFrame(res)
res.to_csv(os.path.join(salida, "censura_eleccion.csv"), index=False, encoding="utf-8-sig")
print(res.to_string(index=False))

print("\n== Sesgo de selección: error informado (1.ª mitad) vs error fuera de muestra (2.ª mitad)")
sep = []
for nombre, b in bt.items():
    for medida, x in (("venta registrada", b.assign(censurado_h=False)),
                      ("demanda verdadera", b.assign(real_h=b["verdadera_h"], censurado_h=False))):
        r = p.evaluacion_separada(x)
        r["variante"], r["medido_contra"] = nombre, medida
        sep.append(r)
sep = pd.concat(sep, ignore_index=True)
sep.to_csv(os.path.join(salida, "seleccion_separada.csv"), index=False, encoding="utf-8-sig")
print(sep.to_string(index=False))
