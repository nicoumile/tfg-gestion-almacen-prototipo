"""
Controles de la tanda 23: en la compra diaria, "Ya tenés" de lo que vence en el día
(vida útil de 1 día, como el pan) cuenta lo comprado hoy que sigue en stock, solo para
la compra de hoy. No usa la base: arma un artículo de prueba y llama a lista_compras.calcular.

Uso: python superados/pruebas/controles_tanda23.py <carpeta_codigo>
"""
import datetime as dt
import os
import sys

carpeta = os.path.abspath(sys.argv[1])
sys.path.insert(0, carpeta); os.chdir(carpeta)
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import lista_compras as lc  # noqa: E402

fallas = 0
HOY = dt.date(2026, 10, 27)
MANANA = HOY + dt.timedelta(days=1)
PARAMS = {"frecuencia_viaje_dias": 7, "valor_recupero_pct": 0.0}


def ok(n, c, d=""):
    global fallas
    fallas += not c
    print(("OK    " if c else "FALLA ") + n + (f"  -- {d}" if d else ""))


def pron(media=5.0):
    fechas = [HOY + dt.timedelta(days=i) for i in range(14)]
    return pd.DataFrame({"articulo_id": 1, "fecha_objetivo": fechas, "demanda": media, "clase": "A"})


def meta(stock, comprado_hoy, vida=1, dia_stock=HOY):
    return pd.DataFrame([{
        "articulo_id": 1, "nombre": "PAN DE PRUEBA", "categoria": "Almacén", "unidad_medida": "unidad",
        "stock": max(stock, 0), "stock_actual": stock, "costo": 1000.0, "precio": 2000.0, "costo_c_iva": 1210.0,
        "reposicion_dias": 1, "vida_util_dias": vida, "vida_util_efectiva": vida, "nivel_servicio": 0.8,
        "articulo_fraccion_de": None, "tam_venta": np.nan, "bulto": 1.0, "proveedor": "Panadería",
        "consignacion": False, "dia_stock": dia_stock, "comprado_hoy": comprado_hoy,
    }])


def fila(m, dia):
    d, _ = lc.calcular(pron(), m, PARAMS, {}, None, dia_compra_diaria=dia)
    return d.iloc[0]


r = fila(meta(1, 0), HOY)
ok("vida 1: lo que quedó de ayer no cuenta", r["Ya tenés"] == 0 and r["Comprar"] == r["Tener para el día"],
   f"ya tenés {r['Ya tenés']:g}, comprar {r['Comprar']:g}")
r = fila(meta(8, 10), HOY)
ok("vida 1, para hoy: cuenta lo comprado hoy que queda", r["Ya tenés"] == 8, f"{r['Ya tenés']:g}")
ok("vida 1, para hoy: no pide volver a comprar si alcanza", r["Comprar"] == max(0, r["Tener para el día"] - 8),
   f"comprar {r['Comprar']:g}")
r = fila(meta(12, 10), HOY)
ok("vida 1: lo de ayer se vende primero (cuenta hasta lo comprado hoy)", r["Ya tenés"] == 10, f"{r['Ya tenés']:g}")
r = fila(meta(8, 10), MANANA)
ok("vida 1, para mañana: lo comprado hoy no cuenta", r["Ya tenés"] == 0, f"{r['Ya tenés']:g}")
r = fila(meta(8, 10, dia_stock=HOY - dt.timedelta(days=1)), HOY)
ok("vida 1: si el stock es de otro día, no cuenta", r["Ya tenés"] == 0, f"{r['Ya tenés']:g}")
r = fila(meta(-3, 10), HOY)
ok("vida 1: stock negativo no suma", r["Ya tenés"] == 0, f"{r['Ya tenés']:g}")
r = fila(meta(4, 0, vida=3), MANANA)
ok("vida 3: cuenta todo el stock, como antes", r["Ya tenés"] == 4, f"{r['Ya tenés']:g}")
m = meta(8, 10).drop(columns=["dia_stock", "comprado_hoy"])
r = fila(m, HOY)
ok("sin las columnas nuevas (simulación, pruebas viejas): vida 1 no cuenta, como antes", r["Ya tenés"] == 0)

print(f"\n{'TODO OK' if not fallas else f'{fallas} FALLAS'}")
sys.exit(1 if fallas else 0)
