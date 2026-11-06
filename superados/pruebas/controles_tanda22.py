"""
Controles de la tanda 22: comparación de políticas de reposición (HU-021). No toca
ninguna base: solo usa demanda_sim.py, simular_politicas.py y analizar_politicas.py
con el catálogo exportado (catalogo_real_export.csv y promo_componentes_export.csv).
Tarda ~1 minuto (simula un año con cada política).

Uso: python superados/pruebas/controles_tanda22.py <carpeta_codigo>
"""
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

carpeta = os.path.abspath(sys.argv[1])
sys.path.insert(0, carpeta); sys.path.insert(0, os.path.join(carpeta, "analisis")); os.chdir(carpeta)
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import demanda_sim as ds  # noqa: E402
import simular_politicas as sp  # noqa: E402

fallas = 0


def ok(n, c, d=""):
    global fallas
    fallas += not c
    print(("OK    " if c else "FALLA ") + n + (f"  -- {d}" if d else ""))


# ---------------- la demanda es la misma para todas las políticas ----------------
D, info = ds.generar(3, "estable")
D2, _ = ds.generar(3, "estable")
ok("misma semilla, misma demanda", np.array_equal(D, D2))
ok("otra semilla, otra demanda", not np.array_equal(D, ds.generar(4, "estable")[0]))
Dc, infoc = ds.generar(3, "cambiante")
ok("escenario cambiante: hay nuevos y discontinuados",
   len(infoc["eventos"]["nuevos"]) > 0 and len(infoc["eventos"]["discontinuados"]) > 0)

# ---------------- cada política, un año ----------------
r = {pol: sp.simular(D, info, pol) for pol in sp.POLITICAS}
for pol, x in r.items():
    ok(f"{pol}: vendido + perdido = demanda", np.allclose(x["sold"] + x["lost"], D))
    ok(f"{pol}: no se vende más de lo disponible", bool((x["sold"] <= np.clip(x["disp"], 0, None) + 1e-9).all()))
    ok(f"{pol}: el stock nunca queda negativo", bool((x["disp"] - x["sold"] >= -1e-9).all()))
    ok(f"{pol}: compra solo los lunes (y el día 0)",
       all(t == 0 or info["fechas"][t].weekday() == 0 for t, *_ in x["compras"]))

cal = slice(0, sp.CALENTAMIENTO)
ok("calentamiento: las tres operan igual los primeros 90 días",
   all(np.array_equal(r["naive"]["sold"][cal], r[pol]["sold"][cal]) for pol in ("abc", "pronostico")))
ev = slice(sp.CALENTAMIENTO, ds.DIAS)
ok("después del calentamiento las políticas difieren",
   not np.array_equal(r["naive"]["sold"][ev], r["pronostico"]["sold"][ev])
   and not np.array_equal(r["naive"]["sold"][ev], r["abc"]["sold"][ev]))
ok("la política por pronóstico usa el modelo elegido por la app",
   r["pronostico"]["elec_final"] is not None and set(r["pronostico"]["elec_final"]) >= set("ABC"),
   str(r["pronostico"]["elec_final"]))

# ---------------- indicadores ----------------
filas = pd.DataFrame([f for pol in sp.POLITICAS for f in sp.resumir(r[pol], D, info, pol)])
for pol, g in filas.groupby("politica"):
    tot, cl = g[g.clase == "Total"].iloc[0], g[g.clase != "Total"]
    ok(f"{pol}: pérdida total = suma de las clases", np.isclose(tot["perdida_$"], cl["perdida_$"].sum()))
    ok(f"{pol}: capital total = suma de las clases", np.isclose(tot["capital_prom_$"], cl["capital_prom_$"].sum()))
    ok(f"{pol}: nivel de servicio entre 0 y 100", 0 <= tot["fill_rate_%"] <= 100, f"{tot['fill_rate_%']:.1f}")
ok("misma demanda en pesos para las tres", filas[filas.clase == "Total"]["demanda_$"].nunique() == 1)
ok("las clases de reporte son las mismas para las tres",
   filas[filas.clase == "A"]["articulos"].nunique() == 1)
t = filas[filas.clase == "Total"].set_index("politica")
ok("el pronóstico pierde menos ventas que la regla actual",
   t.loc["pronostico", "perdida_$"] < t.loc["naive", "perdida_$"],
   f"{t.loc['pronostico', 'perdida_%']:.1f} % vs {t.loc['naive', 'perdida_%']:.1f} %")

# ---------------- de punta a punta: simular + analizar ----------------
with tempfile.TemporaryDirectory() as tmp:
    corr, inf = os.path.join(tmp, "corridas"), os.path.join(tmp, "informe")
    os.makedirs(corr)
    filas.to_csv(os.path.join(corr, "res_estable_s3.csv"), index=False)
    p = subprocess.run([sys.executable, "analizar_politicas.py", corr, inf], capture_output=True)
    ok("analizar_politicas.py corre", p.returncode == 0, p.stderr.decode("utf-8", "replace")[-300:])
    esperados = ["politicas_resumen.csv", "politicas_por_clase.csv", "politicas_pareada.csv",
                 "politicas_tabla.txt", "figura_politicas.svg"]
    ok("arma las cinco salidas", all(os.path.exists(os.path.join(inf, f)) for f in esperados))
    try:
        raiz = ET.parse(os.path.join(inf, "figura_politicas.svg")).getroot()
        circulos = sum(1 for e in raiz.iter() if e.tag.endswith("circle"))
        ok("la figura es un SVG válido con un punto por política", circulos >= 3 * 2, f"{circulos} círculos")
    except (ET.ParseError, FileNotFoundError) as e:
        ok("la figura es un SVG válido con un punto por política", False, str(e))
    par = pd.read_csv(os.path.join(inf, "politicas_pareada.csv"))
    ok("la comparación pareada tiene pronóstico vs regla actual y vs ABC",
       set(par["comparacion"]) == {"pronostico vs naive", "pronostico vs abc"})

print(f"\n{'TODO OK' if not fallas else f'{fallas} FALLAS'}")
sys.exit(1 if fallas else 0)
