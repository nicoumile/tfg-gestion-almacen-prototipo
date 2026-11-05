"""
Foto del comportamiento de la app sobre una copia de producción.

Uso: python foto.py <carpeta_codigo> <base> <salida.pkl>

1. LECTURA: corre las funciones de reportes, análisis, pronóstico y lista de
   compras y guarda lo que devuelven.
2. ESCRITURA: corre un escenario fijo de operaciones (venta de carrito con
   unidad + kg + combo virtual, consumo interno, factura manual, sobrante del
   día, editar y eliminar venta, armar promo física, gasto) y guarda el estado
   final de las tablas (sin columnas de fecha/hora de creación).
Cada versión del código corre en un proceso aparte, contra su propia copia
recién cargada, así las dos fotos son comparables.
"""
import os
import pickle
import sys
import traceback

carpeta, base, salida = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
config.usar_base(base)
sys.path.insert(0, os.path.abspath(carpeta))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

import db  # noqa: E402
import lista_compras as lc  # noqa: E402
import pronostico as pron  # noqa: E402

HOY = "2026-10-21"          # día siguiente al respaldo: fijo para que sea reproducible
DESDE, HASTA = "2026-09-01", "2026-10-31"
db.set_persona_actual(1, "Dueño")
foto = {"lectura": {}, "escritura": {}, "errores": {}}


def guardar(seccion, nombre, fn):
    try:
        foto[seccion][nombre] = fn()
    except Exception:
        foto["errores"][f"{seccion}.{nombre}"] = traceback.format_exc()


L = "lectura"
guardar(L, "articulos", lambda: db.listar_articulos(incluir_inactivos=True))
guardar(L, "stock_bajo", db.alertas_stock_bajo)
guardar(L, "stock_negativo", db.articulos_stock_negativo)
guardar(L, "facturas", db.listar_facturas)
guardar(L, "ventas", lambda: db.listar_ventas())
guardar(L, "gastos", lambda: db.listar_gastos())
guardar(L, "ganancias_vs_gastos", lambda: db.reporte_ganancias_vs_gastos(DESDE, HASTA))
guardar(L, "ventas_por_articulo_real", lambda: db.ventas_por_articulo_real(DESDE, HASTA))
guardar(L, "abc", lambda: db.clasificacion_abc(DESDE, HASTA))
guardar(L, "resumen_abc", lambda: db.resumen_abc(DESDE, HASTA))
guardar(L, "valor_stock", db.valor_stock)
guardar(L, "margenes", lambda: db.margenes_reales(DESDE, HASTA))
guardar(L, "margenes_categoria", lambda: db.margenes_reales_por_categoria(DESDE, HASTA))
guardar(L, "margenes_combo", lambda: db.margenes_por_combo(DESDE, HASTA))
guardar(L, "resumen_margenes", lambda: db.resumen_margenes(DESDE, HASTA))
guardar(L, "precios_proveedor", db.comparar_precios_proveedores)
guardar(L, "variaciones_precio", lambda: db.variaciones_de_precio_relevantes(5))
guardar(L, "promociones", db.listar_promociones)
guardar(L, "parametros", db.obtener_parametros)
for d in ("2026-10-12", "2026-10-17", "2026-10-18"):
    guardar(L, f"cierre_{d}", lambda d=d: {k: v for k, v in db.cierre_caja(d).items()})
# Dependen de CURRENT_DATE de la base: se guardan, pero solo valen si las dos
# fotos se sacan el mismo día (comparar.py corre las dos seguidas).
guardar(L, "calidad", lambda: db.calidad_de_datos(HOY))
guardar(L, "inmovilizado", lambda: db.stock_inmovilizado(60))

ev = None
try:
    ev = pron.evaluar(db.engine, hoy=HOY)
    foto[L]["pron_evaluacion"] = ev
    foto[L]["pron_vigente"] = pron.pronostico_vigente(db.engine, horizonte=14, hoy=HOY, eleccion=ev["eleccion"])
    foto[L]["pron_reconstruido"] = pron.reconstruir(db.engine, hoy=HOY, dias=28, eleccion=ev["eleccion"])
    params = db.obtener_parametros()
    meta = lc.datos_articulos(db.engine)
    Y, _, _ = pron.preparar(db.engine, HOY)
    disp = dict(zip(ev["sigma"]["clase"], ev["sigma"]["dispersion"])) if ev["evaluada"] else {}
    hoy_d = pd.Timestamp(HOY).date()
    foto[L]["lista_compras"] = lc.calcular(foto[L]["pron_vigente"], meta, params, disp, Y, dia_compra_diaria=hoy_d)
    prox = lc.proximo_viaje_estimado(lc.ultimo_viaje(db.engine), hoy_d, params["frecuencia_viaje_dias"])
    foto[L]["alertas_quiebre"] = lc.alertas_quiebre(foto[L]["pron_vigente"], meta, params, hoy_d, prox)
except Exception:
    foto["errores"]["lectura.pronostico"] = traceback.format_exc()

# ---------------- ESCRITURA: escenario fijo ----------------
E = "escritura"
F = "2026-10-21"
guardar(E, "venta_carrito", lambda: db.confirmar_venta_carrito([
    {"articulo_id": 163, "cantidad": 0.25, "precio_unitario": 20000.0, "nombre": "queso"},   # kg
    {"articulo_id": 3, "cantidad": 2, "precio_unitario": 3500.0, "nombre": "aceite"},       # unidad
    {"articulo_id": 491, "cantidad": 1, "precio_unitario": 15000.0, "nombre": "promo"},     # combo virtual
], F, "Efectivo"))
guardar(E, "consumo_interno", lambda: db.confirmar_consumo_interno([
    {"articulo_id": 112, "cantidad": 1, "precio_unitario": 1000.0, "nombre": "mayonesa"}], F))
guardar(E, "factura_manual", lambda: db.crear_factura(6, "TEST-0001", F, [
    {"articulo_id": 244, "cantidad": 10, "costo_unitario": 1500.0, "actualiza_precio": True},
    {"articulo_id": 309, "cantidad": 5, "costo_unitario": 800.0, "actualiza_precio": False},
], origen="manual"))
guardar(E, "sobrante", lambda: db.registrar_dia_compra_diaria(3, F, None, 1))
with db.engine.connect() as c:
    ultimas = [r[0] for r in c.execute(text("SELECT id FROM ventas ORDER BY id DESC LIMIT 6"))]
guardar(E, "editar_venta", lambda: db.editar_venta(ultimas[-1], F, "Tarjeta débito", articulo_id=3,
                                                   cantidad=3, precio_unitario=3500.0))
guardar(E, "eliminar_venta", lambda: db.eliminar_venta(ultimas[-2]))
with db.engine.connect() as c:
    promo_fisica = c.execute(text("SELECT id FROM articulos WHERE promo_tipo = 'fisica' AND activo "
                                  "ORDER BY (id = 499) DESC, id LIMIT 1")).scalar()
if promo_fisica:  # en 10/2026 el comerciante pasó la PROMO FIAMBRE (499) a stock compartido
    guardar(E, "armar_promo", lambda: db.armar_promocion(promo_fisica, 1))
guardar(E, "gasto", lambda: db.registrar_gasto("Servicios (luz/agua/gas)", 12345.0, F, "test"))

TABLAS = {
    "articulos": "SELECT id, stock_actual, costo_compra_actual, margen_ganancia, activo FROM articulos ORDER BY id",
    "ventas": "SELECT id, articulo_id, ticket_id, fecha, cantidad, precio_unitario, monto_total, medio_pago, "
              "es_consumo_interno, costo_unitario, precio_lista, registrado_por FROM ventas ORDER BY id",
    "ventas_ticket": "SELECT id, fecha, medio_pago FROM ventas_ticket ORDER BY id",
    "facturas": "SELECT id, proveedor_id, numero, fecha, origen, ref_externa FROM facturas ORDER BY id",
    "detalle_factura": "SELECT * FROM detalle_factura ORDER BY id",
    "gastos": "SELECT * FROM gastos ORDER BY id",
    "articulo_proveedor_precio": "SELECT articulo_id, proveedor_id, costo_compra FROM articulo_proveedor_precio "
                                 "ORDER BY articulo_id, proveedor_id",
    "historial": "SELECT id, tabla, registro_id, accion, detalle, usuario FROM historial_cambios ORDER BY id",
}
for nombre, q in TABLAS.items():
    guardar(E, f"tabla_{nombre}", lambda q=q: pd.read_sql(q, db.engine))

with open(salida, "wb") as f:
    pickle.dump(foto, f)
print(f"{carpeta}: {len(foto['lectura'])} lecturas, {len(foto['escritura'])} escrituras, "
      f"{len(foto['errores'])} errores -> {salida}")
for k, v in foto["errores"].items():
    print("ERROR", k, v.strip().splitlines()[-1])
