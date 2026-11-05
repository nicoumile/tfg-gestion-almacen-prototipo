"""
Prueba de concepto del componente predictivo (pronóstico de demanda agregado).

Serie: unidades totales vendidas por día (v_ventas_analitica, combos resueltos
en sus componentes), base simulada oficial (05/10/2025 a 04/10/2026).
Entrenamiento: primeros 305 días. Evaluación: últimos 60 días.
Referencia: promedio histórico de ventas diarias (pronóstico plano).
Modelo: regresión lineal con estacionalidad por día de semana y por bloque del
mes (días 1-10, 11-20, 21-fin), mínimos cuadrados.
Métrica: MAE (error absoluto medio, unidades/día).

Uso: DB_NAME=almacen_sim python pronostico_demanda.py [salida.png]
"""
import sys
import numpy as np
import pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as md
import db

MES = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

def serie_diaria():
    df = db.obtener_ventas_analiticas()
    s = df.groupby("fecha")["cantidad"].sum().astype(float)
    s.index = pd.to_datetime(s.index)
    return s.asfreq("D", fill_value=0.0)

def features(idx):
    X = pd.DataFrame(index=idx)
    for k in range(1, 7):
        X[f"dow_{k}"] = (idx.dayofweek == k).astype(float)
    bloque = np.where(idx.day <= 10, 0, np.where(idx.day <= 20, 1, 2))
    X["bloque_2"] = (bloque == 1).astype(float)
    X["bloque_3"] = (bloque == 2).astype(float)
    X["const"] = 1.0
    return X

def main(salida="figura2_pronostico.png"):
    s = serie_diaria()
    X = features(s.index)
    n_tr = 305
    y_tr, y_te = s.values[:n_tr], s.values[n_tr:n_tr + 60]
    beta = np.linalg.lstsq(X.values[:n_tr], y_tr, rcond=None)[0]
    pred = X.values[n_tr:n_tr + 60] @ beta
    ref = np.full(len(y_te), y_tr.mean())
    mae_ref = np.abs(y_te - ref).mean()
    mae_mod = np.abs(y_te - pred).mean()
    mejora = (1 - mae_mod / mae_ref) * 100
    print(f"Días: {len(s)} | entrenamiento {n_tr} | evaluación {len(y_te)}")
    print(f"MAE referencia {mae_ref:.1f} | MAE modelo {mae_mod:.1f} | mejora {mejora:.1f} %")

    fechas = s.index[n_tr:n_tr + 60]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.plot(fechas, y_te, color="#262626", lw=1.8, label="Ventas reales (unidades/día)")
    ax.plot(fechas, ref, color="#8c8c8c", lw=1.8, ls="--", label="Referencia: promedio histórico (plano)")
    ax.plot(fechas, pred, color="#1f4e79", lw=2.0, label="Modelo: estacionalidad (día de semana + bloque del mes)")
    ax.set_ylabel("Unidades vendidas / día")
    ax.xaxis.set_major_locator(md.WeekdayLocator(byweekday=0))
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{md.num2date(x).day:02d}-{MES[md.num2date(x).month]}"))
    for sp in ["top", "right"]: ax.spines[sp].set_visible(False)
    ax.grid(axis="y", alpha=.3)
    ax.legend(frameon=False, loc="upper left")
    ax.set_title(f"Pronóstico de demanda diaria total — período de evaluación\n"
                 f"({fechas[0].day} de {MES[fechas[0].month]}. al {fechas[-1].day} de {MES[fechas[-1].month]}. de {fechas[-1].year}, 60 días)")
    c = lambda v: f"{v:.1f}".replace(".", ",")
    fig.text(0.5, 0.01, f"MAE en el período de evaluación: referencia {c(mae_ref)} unidades/día; modelo con estacionalidad "
             f"{c(mae_mod)} unidades/día — una mejora del {c(mejora)} %.", ha="center", color="#8b1a1a", style="italic")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(salida, dpi=200)
    return mae_ref, mae_mod, mejora

if __name__ == "__main__":
    main(*(sys.argv[1:2]))
