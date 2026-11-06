"""
Variantes de la comparación de políticas (Anexo A): qué aporta cada pieza.

Usa simular_politicas.py sin cambiarlo y solo varía un componente por corrida,
contra la misma demanda de cada semilla:
- pronostico:     la app (modelo elegido, censura corregida, niveles A 95 / B 90 / C 80).
- prom30_zs:      la misma fórmula S = D + z x sigma, con el promedio de 30 días como D.
- sin_censura:    la app sin corregir la demanda censurada.
- sin_seguridad:  la app con z = 0 (nivel 50 %): solo la venta esperada.
- *_bajo/_alto:   niveles más bajos o más altos, para trazar la curva pérdida-capital.
- abc_x<m>:       ABC fija con los días de seguridad multiplicados por m.

Uso: python analisis/simular_variantes.py <escenario> <semillas> <carpeta_salida>
Después: python analisis/analizar_variantes.py <carpeta_salida>
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))  # raíz del repositorio
import os
import sys
import time

import pandas as pd

import pronostico as p
import simular_politicas as sp
from demanda_sim import generar

NIVELES = {"base": {"A": 0.95, "B": 0.90, "C": 0.80},
           "bajo": {"A": 0.85, "B": 0.75, "C": 0.60},
           "alto": {"A": 0.98, "B": 0.95, "C": 0.90},
           "cero": {"A": 0.50, "B": 0.50, "C": 0.50}}
SEGURIDAD = {"A": 15, "B": 8, "C": 3}
evaluar_original = sp.evaluar_app
censura_original = sp.obs_censura


def evaluar_promedio(Yh, Ih, fechas, cats_dict, ids, obs):
    """Igual que evaluar_app, pero el modelo es siempre el promedio de 30 días."""
    Y = pd.DataFrame(Yh, index=fechas, columns=ids)
    I = pd.DataFrame(Ih, index=fechas, columns=ids)
    bt = p.backtest(Y, I, cats_dict, dias=min(60, len(Y) - 28 - 7 + 1), horizonte=7, configs=("promedio_30",), obs=obs)
    elec = {c: "promedio_30" for c in "ABC"}
    sig = p.error_por_clase(bt, elec)
    return elec, {r.clase: (float(r.dispersion) if pd.notna(r.dispersion) else 1.0) for r in sig.itertuples()}


def sin_censura(Yh, ini):
    return (Yh >= 0)


def preparar(nombre):
    sp.evaluar_app, sp.obs_censura = evaluar_original, censura_original
    sp.NIVEL = NIVELES["base"]
    sp.SEGURIDAD_ABC = dict(SEGURIDAD)
    if nombre.startswith("abc_x"):
        m = float(nombre[5:])
        sp.SEGURIDAD_ABC = {c: v * m for c, v in SEGURIDAD.items()}
        return "abc"
    if nombre.startswith("prom30_zs"):
        sp.evaluar_app = evaluar_promedio
    if nombre == "sin_censura":
        sp.obs_censura = sin_censura
    if nombre == "sin_seguridad":
        sp.NIVEL = NIVELES["cero"]
    for k in ("bajo", "alto"):
        if nombre.endswith("_" + k):
            sp.NIVEL = NIVELES[k]
    return "pronostico"


VARIANTES = ["pronostico", "pronostico_bajo", "pronostico_alto", "prom30_zs", "prom30_zs_bajo", "prom30_zs_alto",
             "sin_censura", "sin_seguridad", "abc_x0.25", "abc_x0.5", "abc_x0.75", "abc_x1", "abc_x1.5"]


def correr(semilla, escenario, salida):
    D, info = generar(semilla, escenario)
    filas = []
    for v in VARIANTES:
        t0 = time.time()
        politica = preparar(v)
        r = sp.simular(D, info, politica)
        filas += [{**f, "politica": v} for f in sp.resumir(r, D, info, politica)]
        print(f"  {escenario} s{semilla} {v}: {time.time() - t0:.0f}s", flush=True)
    pd.DataFrame(filas).to_csv(f"{salida}/var_{escenario}_s{semilla}.csv", index=False)


if __name__ == "__main__":
    escenario, semillas, salida = sys.argv[1], [int(s) for s in sys.argv[2].split(",")], sys.argv[3]
    os.makedirs(salida, exist_ok=True)
    for s in semillas:
        correr(s, escenario, salida)
