"""
Generador de DEMANDA VERDADERA por semilla y escenario: banco de pruebas de la
comparación de políticas de reposición (HU-021; creado el 23/10/2026, sumado al
repo en la tanda 22). Misma lógica que generar_dataset_oficial.py (catálogo
real, 30-50 tickets por día con día de semana y época del mes, Pareto por
categoría x ruido log-normal, combos al 5 %, tamaños de venta), pero:

- No escribe en ninguna base ni depende del stock: devuelve la demanda (lo que
  los clientes quieren llevar) por día y artículo. Así la MISMA demanda se
  resuelve contra el stock de cada política y la única diferencia entre
  corridas es la regla de compra. (En el generador original, cuando un combo
  no tenía stock cambiaba el sorteo siguiente; acá el sorteo es independiente.)
- Los combos se cargan como demanda de sus componentes.

Escenarios:
- "estable": popularidad fija todo el año (como la base oficial).
- "cambiante": la popularidad de cada artículo se mueve con el tiempo:
    * paseo aleatorio semanal en logaritmo (desvío 0,07 por semana: ~0,5 en
      el año, o sea que un artículo puede terminar vendiendo la mitad o el
      doble);
    * 8 % de artículos NUEVOS: no existen hasta un día al azar entre el 60 y
      el 300;
    * 8 % que se DISCONTINÚAN: dejan de pedirse desde un día al azar entre el
      60 y el 300;
    * 5 % con un SALTO de popularidad (x2 o x0,5) en un día al azar.
"""
import datetime as dt
import os
import random

import numpy as np
import pandas as pd

INICIO, DIAS = dt.date(2025, 10, 5), 365
DATA = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
PESO_CATEGORIA = {
    "Bebidas": 3.0, "Almacén": 2.4, "Golosinas": 2.2, "Fiambres y lácteos": 2.6, "Snacks": 1.6,
    "Limpieza": 1.0, "Uso personal": 0.8, "Condimentos": 0.9, "Tabaco": 0.35, "Mascotas": 0.25, "Librería": 0.25,
}


def factor_dia(d):
    f = 1.0
    if d.weekday() == 4: f *= 1.25
    if d.weekday() == 5: f *= 1.35
    if d.weekday() == 6: f *= 0.85
    if 1 <= d.day <= 10: f *= 1.15
    elif 15 <= d.day <= 20: f *= 1.08
    elif d.day >= 24: f *= 0.82
    return f


def catalogo():
    cat = pd.read_csv(f"{DATA}/catalogo_real_export.csv", encoding="utf-8-sig")
    comp = pd.read_csv(f"{DATA}/promo_componentes_export.csv", encoding="utf-8-sig")
    return cat, comp


def generar(semilla=42, escenario="estable"):
    """Devuelve (D, info): D ndarray (DIAS, n) de demanda por día y artículo
    (columnas = info['ids']); info con fechas, popularidad, eventos del escenario."""
    rng = random.Random(semilla)
    nrng = np.random.default_rng(semilla)
    cat, comp = catalogo()
    normales = cat[cat.categoria != "Promocion"].reset_index(drop=True)
    combos = cat[cat.categoria == "Promocion"].id.astype(int).tolist()
    ids = normales.id.astype(int).tolist()
    n = len(ids)
    col = {a: j for j, a in enumerate(ids)}
    kg = (normales.unidad_medida == "kg").values
    w0 = np.array([PESO_CATEGORIA.get(c, 1.0) for c in normales.categoria]) * nrng.lognormal(0, 0.7, n)
    comp_de = {}
    for _, r in comp.iterrows():
        comp_de.setdefault(int(r.promocion_id), []).append((col[int(r.componente_articulo_id)], float(r.cantidad_por_unidad)))

    # popularidad por día (DIAS x n)
    fechas = [INICIO + dt.timedelta(days=i) for i in range(DIAS)]
    eventos = {"nuevos": [], "discontinuados": [], "saltos": []}
    if escenario == "estable":
        W = np.tile(w0, (DIAS, 1))
    elif escenario == "cambiante":
        er = np.random.default_rng(semilla + 10_000)   # eventos con su propia semilla
        semanas = DIAS // 7 + 1
        paso = er.normal(0, 0.07, (semanas, n))
        paso[0] = 0
        logw = np.cumsum(paso, axis=0)
        W = w0[None, :] * np.exp(np.repeat(logw, 7, axis=0)[:DIAS])
        idx = er.permutation(n)
        k = int(round(0.08 * n))
        nuevos, disc = idx[:k], idx[k:2 * k]
        saltos = idx[2 * k:2 * k + int(round(0.05 * n))]
        for j in nuevos:
            d0 = int(er.integers(60, 301)); W[:d0, j] = 0; eventos["nuevos"].append((ids[j], d0))
        for j in disc:
            d1 = int(er.integers(60, 301)); W[d1:, j] = 0; eventos["discontinuados"].append((ids[j], d1))
        for j in saltos:
            d2 = int(er.integers(60, 301)); f = 2.0 if er.random() < 0.5 else 0.5
            W[d2:, j] *= f; eventos["saltos"].append((ids[j], d2, f))
    else:
        raise ValueError(escenario)

    D = np.zeros((DIAS, n))
    tickets = 0
    for i, d in enumerate(fechas):
        p = W[i] / W[i].sum()
        vivos = int((p > 0).sum())
        lam = rng.uniform(30, 50) * factor_dia(d) * rng.uniform(0.9, 1.1)
        for _ in range(max(5, int(round(lam)))):
            combo = False
            if rng.random() < 0.05:
                pid = int(rng.choice(combos))
                for j, q in comp_de[pid]:
                    D[i, j] += q
                combo = True
            k = rng.choices([1, 2, 3, 4], [25, 30, 25, 20])[0] if not combo else rng.choice([0, 1])
            for j in nrng.choice(n, size=min(k, vivos), p=p, replace=False):
                D[i, j] += rng.choice([0.25, 0.5, 0.5, 1.0]) if kg[j] else rng.choices([1, 2, 3], [78, 17, 5])[0]
            tickets += 1
    info = {"ids": ids, "fechas": pd.DatetimeIndex(fechas), "kg": kg, "tickets": tickets,
            "escenario": escenario, "semilla": semilla, "eventos": eventos, "popularidad": W}
    return D, info


if __name__ == "__main__":
    import time
    t = time.time()
    D, info = generar(42, "estable")
    print("estable", D.shape, round(D.sum()), info["tickets"], f"{time.time() - t:.1f}s")
    D2, info2 = generar(7, "cambiante")
    print("cambiante", round(D2.sum()), {k: len(v) for k, v in info2["eventos"].items()})
