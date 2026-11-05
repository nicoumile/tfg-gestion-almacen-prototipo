"""
Evaluación del pronóstico de demanda por artículo (Sprint 2, HU-014) para el
documento del TFG. Usa exactamente el mismo código que la app (pronostico.py).

Qué hace:
  1. Arma la serie diaria de demanda por artículo hasta la fecha de corte.
  2. Backtest de origen móvil: en cada uno de los últimos 60 días pronostica
     los 7 siguientes (un viaje de compras) usando solo datos anteriores, con
     cada configuración candidata. Clase ABC recalculada en cada origen.
  3. Elige la configuración por clase (menor WAPE de 7 días). Los días sin
     stock no cuentan como demanda 0 al ajustar (demanda censurada,
     pronostico.dias_observados). El error se mide contra lo vendido: con
     quiebres subestima la demanda (ver experimento_censura.py, que mide
     contra la demanda verdadera de la base simulada).
  3b. Control del sesgo de selección: elige con la primera mitad de los días
     de prueba y mide con la segunda (pronostico.evaluacion_separada).
  4. Agrega dos referencias que NO son pronósticos realizables, para ubicar
     los números:
       - "oráculo": el promedio diario de todo el período, futuro incluido;
       - "piso Poisson": el error que tendría un pronóstico perfecto de la
         media si la venta fuera puramente aleatoria (ruido irreducible).

Salidas (en la carpeta indicada, por defecto 'Claude outputs'):
  evaluacion_pronostico_metricas.csv   todas las configuraciones x clase
  evaluacion_pronostico_resumen.csv    elegido vs método anterior vs referencias
  evaluacion_pronostico_fuera_de_muestra.csv  error con días que la elección no vio
  figura_evaluacion_pronostico.png     gráfico para el documento

Uso (desde la carpeta TFG):
  python evaluar_pronostico.py                     # base de .streamlit/secrets.toml, hasta hoy
  python evaluar_pronostico.py 2026-10-04          # fecha de corte (base simulada)
  DB_NAME=almacen_sim python evaluar_pronostico.py 2026-10-04
"""
import os
import sys

import numpy as np
import pandas as pd

import db
import pronostico as p

ETIQUETA = {
    "promedio_30": "Promedio 30 días (método anterior)",
    "ses_0.02": "Nivel suavizado α=0,02 (~100 días)",
    "ses_0.05": "Nivel suavizado α=0,05 (~40 días)",
    "ses_0.10": "Nivel suavizado α=0,10 (~20 días)",
    "croston_0.05": "Croston-SBA α=0,05",
    "croston_0.10": "Croston-SBA α=0,10",
}


def referencias(Y, I, dias=60, h=7, semilla=0):
    """WAPE de 7 días del oráculo y del piso Poisson, por clase (mismos orígenes)."""
    Yv, Iv = Y.values, I.values
    T = len(Y)
    media = Yv.mean(axis=0)
    rng = np.random.default_rng(semilla)
    acc = {}
    for t in range(max(28, T - dias - h + 1), T - h + 1):
        real = Yv[t:t + h].sum(axis=0)
        vivos = (Yv[:t] > 0).any(axis=0)
        clase = p.clases_abc(Iv[:t][-p.VENTANA_ABC:])
        F = media * h
        sim = rng.poisson(F)
        for c in "ABC":
            m = vivos & (clase == c)
            a = acc.setdefault(c, np.zeros(4))
            a += [np.abs(real[m] - F[m]).sum(), real[m].sum(), np.abs(sim[m] - F[m]).sum(), sim[m].sum()]
    return {c: {"oraculo": a[0] / a[1] * 100, "piso_poisson": a[2] / a[3] * 100} for c, a in acc.items()}


def figura(resumen, ruta, dias_hist, origenes):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    series = [("Método anterior (promedio 30 días)", "wape_anterior", "#2a78d6"),
              ("Modelo elegido por clase", "wape_elegido", "#eb6834"),
              ("Referencia no realizable (oráculo)", "wape_oraculo", "#1baf7a")]
    clases = resumen["clase"].tolist()
    x = np.arange(len(clases))
    ancho = 0.26
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, ax = plt.subplots(figsize=(9, 4.6))
    for i, (nombre, col, color) in enumerate(series):
        vals = resumen[col].values
        barras = ax.bar(x + (i - 1) * (ancho + 0.02), vals, ancho, color=color, label=nombre,
                        edgecolor="white", linewidth=2)
        for b, v in zip(barras, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 1, f"{v:.1f}".replace(".", ","),
                    ha="center", va="bottom", fontsize=9, color="#0b0b0b")
    ax.plot([], [], " ")  # sin segundo eje
    for j, c in enumerate(clases):
        piso = resumen.loc[resumen["clase"] == c, "wape_piso"].iloc[0]
        ax.hlines(piso, j - 0.45, j + 0.45, colors="#52514e", linestyles=(0, (4, 3)), linewidth=1.2)
    ax.plot([], [], color="#52514e", ls=(0, (4, 3)), lw=1.2, label="Piso por ruido aleatorio (Poisson)")
    ax.set_xticks(x, [f"Clase {c}" for c in clases])
    ax.set_ylabel("Error del pronóstico de 7 días (WAPE, %)")
    ax.set_ylim(0, max(resumen[["wape_anterior", "wape_elegido", "wape_oraculo"]].max()) * 1.18)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left", fontsize=9, ncol=2)
    ax.set_title(f"Error del pronóstico de la demanda semanal por artículo, por clase ABC\n"
                 f"(backtest de origen móvil: {origenes} días de prueba, {dias_hist} días de historia)",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(ruta, dpi=200)
    plt.close(fig)


def main(corte=None, salida=None):
    salida = salida or os.path.join(os.path.dirname(os.path.abspath(__file__)), "Claude outputs")
    os.makedirs(salida, exist_ok=True)
    corte = corte or str(pd.Timestamp.today().date())
    print(f"Base: {db.engine.url.database} | corte: {corte}")
    Y, I, articulos = p.preparar(db.engine, corte)
    print(f"Serie: {Y.shape[0]} días x {Y.shape[1]} artículos")
    ev = p.evaluar(db.engine, hoy=corte)
    if not ev["evaluada"]:
        print(f"No alcanza la historia para evaluar ({ev['dias_historia']} días; hacen falta "
              f"{28 + 7 + p.MIN_ORIGENES_EVALUACION - 1}).")
        return
    met = ev["metricas"]
    met.assign(configuracion=met["modelo"].map(ETIQUETA)).to_csv(
        os.path.join(salida, "evaluacion_pronostico_metricas.csv"), index=False, encoding="utf-8-sig")

    ref = referencias(Y, I)
    filas = []
    for c in "ABC":
        el = ev["eleccion"][c]
        g = met[met["clase"] == c].set_index("modelo")
        if el not in g.index or "promedio_30" not in g.index:
            continue
        filas.append({
            "clase": c, "articulos": int(g.loc["promedio_30", "articulos"]), "modelo_elegido": ETIQUETA[el],
            "wape_anterior": g.loc["promedio_30", "wape_h_%"], "wape_elegido": g.loc[el, "wape_h_%"],
            "mejora_%": round((1 - g.loc[el, "wape_h_%"] / g.loc["promedio_30", "wape_h_%"]) * 100, 1),
            "sesgo_elegido_%": g.loc[el, "sesgo_%"],
            "wape_oraculo": round(ref[c]["oraculo"], 1), "wape_piso": round(ref[c]["piso_poisson"], 1),
        })
    resumen = pd.DataFrame(filas)
    resumen["brecha_cerrada_%"] = ((resumen["wape_anterior"] - resumen["wape_elegido"])
                                   / (resumen["wape_anterior"] - resumen["wape_oraculo"]) * 100).round(0)
    resumen.to_csv(os.path.join(salida, "evaluacion_pronostico_resumen.csv"), index=False, encoding="utf-8-sig")
    pd.set_option("display.width", 220)
    print("\nWAPE 7 días (%) por configuración y clase:")
    print(met.pivot(index="modelo", columns="clase", values="wape_h_%").rename(index=ETIQUETA).to_string())
    print("\nResumen:")
    print(resumen.to_string(index=False))
    fuera = ev.get("fuera_de_muestra")
    if fuera is not None and not fuera.empty:
        fuera.to_csv(os.path.join(salida, "evaluacion_pronostico_fuera_de_muestra.csv"), index=False,
                     encoding="utf-8-sig")
        print("\nControl del sesgo de selección (elige con la 1.ª mitad, mide con la 2.ª):")
        print(fuera.to_string(index=False))
    print(f"\nDemanda censurada: {ev.get('censura')}")
    print("\nDesvío del error semanal (insumo del stock de seguridad):")
    print(ev["sigma"].to_string(index=False))
    figura(resumen, os.path.join(salida, "figura_evaluacion_pronostico.png"), ev["dias_historia"], ev["origenes"])
    print(f"\nArchivos en: {salida}")


if __name__ == "__main__":
    main(*(sys.argv[1:3]))
