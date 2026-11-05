"""
Base de datos simulada oficial del TFG (Entregable 3) -- versión regenerada el 18/10/2026.

Reconstruye la base simulada siguiendo los criterios documentados en
claude/criterios-base-datos-simulada-entregable3.md (proyecto Seminario II),
porque el .dump original del 04/10/2026 no se conservó:

- Catálogo real del comerciante (foto del 04/10/2026: catalogo_real_export.csv, 497
  artículos con sus 4 combos reales), 4 proveedores reales
  (proveedores_export.csv) y componentes reales de cada combo
  (promo_componentes_export.csv). IDs reales preservados.
- 12 meses: 05/10/2025 a 04/10/2026. Seed fijo 42.
- 30-50 tickets por día (calibrado por cantidad de operaciones), con
  estacionalidad: más venta viernes y sábado, picos los días 1-10 y 15-20,
  caída en la última semana del mes, más ruido diario.
- Popularidad: peso por categoría + ruido individual (log-normal) -> Pareto.
- Combos vendidos mezclados con ventas sueltas de sus componentes.
- Medios de pago 70 % efectivo, 20 % tarjeta, 10 % transferencia.
- Reposición semanal por proveedor (política pareja, "naive"): si el stock
  cae por debajo de max(stock_mínimo, 2), se repone hasta
  max(stock_inicial, umbral x 4, 8). Aumentos de costo escalonados por
  trimestre y proveedor (3-8 % cada uno, sobre el costo ORIGINAL del
  catálogo, sin componer sobre el costo ya actualizado) + ruido chico por
  factura.
- Proveedor por categoría: Bebidas -> un proveedor de bebidas;
  Fiambres y lácteos -> un proveedor de lácteos; resto alternado entre
  otros proveedores.
- Una venta solo se registra si hay stock (lo que no hay es venta perdida
  por quiebre y no queda en la base, igual que en la realidad).
- Artículos con costo $0 en el catálogo real se preservan tal cual (caso
  real de calidad de datos).
- Simplificación: el stock del export se usa como stock INICIAL del período.

Ventas y compras se cargan ejercitando db.py real (confirmar_venta_carrito,
crear_factura), para disparar los mismos triggers que la aplicación.
Uso (corrida en seco, tanda 9): DATA_DIR=. DB_NAME=almacen_sim python generar_demanda_verdadera.py demanda_verdadera.csv
Repite la simulación con la misma semilla SIN escribir en la base y guarda la demanda
verdadera (vendido + perdido por quiebre) por día y artículo. Controlado: lo vendido coincide
exactamente con la base simulada (33.668 renglones, 0 diferencias).
"""
import os, math, random, datetime as dt
from collections import defaultdict
import numpy as np
import pandas as pd
from sqlalchemy import text

import db

SEED = 42
SECO = True                     # corrida en seco: no escribe nada, anota la demanda verdadera
DEMANDA = defaultdict(float)    # (fecha, articulo) -> unidades pedidas (vendidas + perdidas)
VENDIDO = defaultdict(float)    # (fecha, articulo) -> unidades vendidas (control contra la base)
INICIO, FIN = dt.date(2025, 10, 5), dt.date(2026, 10, 4)
DATA = os.environ.get("DATA_DIR", "/mnt/user-data/uploads/TFG")
rng = random.Random(SEED)
nrng = np.random.default_rng(SEED)

PESO_CATEGORIA = {
    "Bebidas": 3.0, "Almacén": 2.4, "Golosinas": 2.2, "Fiambres y lácteos": 2.6, "Snacks": 1.6,
    "Limpieza": 1.0, "Uso personal": 0.8, "Condimentos": 0.9, "Tabaco": 0.35, "Mascotas": 0.25, "Librería": 0.25,
}
PROV_CAT = {"Bebidas": 4, "Fiambres y lácteos": 2}
PROV_ALT = [1, 3]

def cargar_maestros():
    cat = pd.read_csv(f"{DATA}/catalogo_real_export.csv", encoding="utf-8-sig")
    prov = pd.read_csv(f"{DATA}/proveedores_export.csv", encoding="utf-8-sig")
    comp = pd.read_csv(f"{DATA}/promo_componentes_export.csv", encoding="utf-8-sig")
    if SECO:
        return cat, comp
    with db.engine.begin() as c:
        for _, p in prov.iterrows():
            c.execute(text("INSERT INTO proveedores (id, nombre, cuit) VALUES (:i,:n,:c)"),
                      {"i": int(p.id), "n": p.nombre, "c": None if pd.isna(p.cuit) else p.cuit})
        for _, a in cat.iterrows():
            costo = float(a.costo_compra_actual or 0)
            if costo > 0:
                margen = float(a.costo_venta_sin_iva) / costo - 1
            else:
                margen = 0.30
            c.execute(text("""INSERT INTO articulos (id, nombre, marca, categoria, costo_compra_actual, margen_ganancia, iva,
                    stock_actual, stock_minimo, fecha_vencimiento, acceso_rapido, unidad_medida, peso_bolsa_gramos, promo_tipo)
                    VALUES (:id,:n,:m,:cat,:costo,:mg,:iva,:st,:smin,:fv,:ar,:um,:pb,:pt)"""),
                      {"id": int(a.id), "n": a.nombre, "m": None if pd.isna(a.marca) else a.marca, "cat": a.categoria,
                       "costo": costo, "mg": margen, "iva": float(a.iva),
                       "st": 0 if a.promo_tipo == "virtual" else max(float(a.stock_actual), 0),
                       "smin": float(a.stock_minimo), "fv": None if pd.isna(a.fecha_vencimiento) else a.fecha_vencimiento,
                       "ar": bool(a.acceso_rapido), "um": a.unidad_medida,
                       "pb": None if pd.isna(a.peso_bolsa_gramos) else float(a.peso_bolsa_gramos),
                       "pt": None if pd.isna(a.promo_tipo) else a.promo_tipo})
        for _, r in comp.iterrows():
            c.execute(text("INSERT INTO promocion_componentes (promocion_articulo_id, componente_articulo_id, cantidad_por_unidad) VALUES (:p,:c,:q)"),
                      {"p": int(r.promocion_id), "c": int(r.componente_articulo_id), "q": float(r.cantidad_por_unidad)})
        for t in ["proveedores", "articulos"]:
            c.execute(text(f"SELECT setval(pg_get_serial_sequence('{t}','id'), (SELECT MAX(id) FROM {t}))"))
        c.execute(text("DELETE FROM historial_cambios"))
    return cat, comp

def factor_dia(d):
    f = 1.0
    if d.weekday() == 4: f *= 1.25
    if d.weekday() == 5: f *= 1.35
    if d.weekday() == 6: f *= 0.85
    if 1 <= d.day <= 10: f *= 1.15
    elif 15 <= d.day <= 20: f *= 1.08
    elif d.day >= 24: f *= 0.82
    return f

def main():
    cat, comp = cargar_maestros()
    normales = cat[cat.categoria != "Promocion"].copy()
    combos = cat[cat.categoria == "Promocion"]
    pesos = np.array([PESO_CATEGORIA.get(c, 1.0) for c in normales.categoria]) * nrng.lognormal(0, 0.7, len(normales))
    pesos = pesos / pesos.sum()
    ids = normales.id.astype(int).tolist()
    kg = set(normales[normales.unidad_medida == "kg"].id.astype(int))
    st0 = {int(a.id): max(float(a.stock_actual), 0) for _, a in normales.iterrows()}
    smin = {int(a.id): float(a.stock_minimo) for _, a in normales.iterrows()}
    costo0 = {int(a.id): float(a.costo_compra_actual or 0) for _, a in normales.iterrows()}
    prov_de = {}
    alt = 0
    for _, a in normales.sort_values("id").iterrows():
        if a.categoria in PROV_CAT: prov_de[int(a.id)] = PROV_CAT[a.categoria]
        else: prov_de[int(a.id)] = PROV_ALT[alt % 2]; alt += 1
    comp_de = defaultdict(list)
    for _, r in comp.iterrows(): comp_de[int(r.promocion_id)].append((int(r.componente_articulo_id), float(r.cantidad_por_unidad)))
    # aumentos trimestrales por proveedor (acumulados sobre el costo original)
    aumento = {p: [1.0] for p in [1, 2, 3, 4]}
    for p in aumento:
        for _ in range(4): aumento[p].append(aumento[p][-1] * (1 + rng.uniform(0.03, 0.08)))

    stock = dict(st0)
    def precio(aid):
        with db.engine.connect() as c:
            return float(c.execute(text("SELECT costo_venta_con_iva FROM v_articulos WHERE id=:i"), {"i": aid}).scalar() or 0)
    precios = {}
    def precio_cache(aid, d):
        k = (aid, d.isocalendar()[1])
        if k not in precios: precios[k] = precio(aid)
        return precios[k]

    n_tickets = n_filas = n_fact = perdidas = 0
    d = INICIO; nfac = 1
    while d <= FIN:
        # reposición semanal (lunes): una factura por proveedor con lo que esté bajo el umbral
        if d.weekday() == 0 or d == INICIO:
            trimestre = min(4, (d - INICIO).days // 91)
            por_prov = defaultdict(list)
            for aid in ids:
                umbral = max(smin[aid], 2)
                if stock[aid] < umbral:
                    objetivo = max(st0[aid], umbral * 4, 8)
                    cant = math.ceil(objetivo - stock[aid])
                    p = prov_de[aid]
                    costo = round(costo0[aid] * aumento[p][trimestre] * (1 + rng.uniform(-0.01, 0.01)), 2) if costo0[aid] > 0 else 0
                    por_prov[p].append({"articulo_id": aid, "cantidad": cant, "costo_unitario": costo})
            for p, items in por_prov.items():
                (None if SECO else db.crear_factura(p, f"A-0001-{nfac:08d}", d, items)); nfac += 1; n_fact += 1
                for it in items: stock[it["articulo_id"]] += it["cantidad"]
            precios.clear()
        lam = rng.uniform(30, 50) * factor_dia(d) * rng.uniform(0.9, 1.1)
        n = max(5, int(round(lam)))
        for _ in range(n):
            medio = rng.choices(["Efectivo", "Tarjeta", "Transferencia"], [70, 20, 10])[0]
            items = []
            if rng.random() < 0.05:
                pid = int(rng.choice(combos.id.tolist()))
                for c_, q_ in comp_de[pid]: DEMANDA[(d, c_)] += q_
                if all(stock[c] >= q for c, q in comp_de[pid]):
                    for c_, q_ in comp_de[pid]: VENDIDO[(d, c_)] += q_
                    for c, q in comp_de[pid]: stock[c] -= q
                    items.append({"articulo_id": pid, "cantidad": 1, "precio_unitario": precio_cache(pid, d)})
                else: perdidas += 1
            k = rng.choices([1, 2, 3, 4], [25, 30, 25, 20])[0] if not items else rng.choice([0, 1])
            elegidos = set()
            for aid in nrng.choice(ids, size=k, p=pesos, replace=False):
                aid = int(aid)
                q = rng.choice([0.25, 0.5, 0.5, 1.0]) if aid in kg else rng.choices([1, 2, 3], [78, 17, 5])[0]
                DEMANDA[(d, aid)] += q
                if stock[aid] >= q:
                    VENDIDO[(d, aid)] += q
                    stock[aid] -= q
                    items.append({"articulo_id": aid, "cantidad": q, "precio_unitario": precio_cache(aid, d)})
                else: perdidas += 1
            if items:
                (None if SECO else db.confirmar_venta_carrito(items, d, medio)); n_tickets += 1; n_filas += len(items)
        d += dt.timedelta(days=1)
        if d.day == 1: print(d, n_tickets, n_filas, n_fact, flush=True)
    print(f"Tickets {n_tickets} | filas de venta {n_filas} | facturas {n_fact} | unidades/ventas perdidas por quiebre {perdidas}")

if __name__ == "__main__":
    main()
    import sys
    out = sys.argv[1]
    pd.DataFrame([(k[0], k[1], v, VENDIDO.get(k, 0.0)) for k, v in DEMANDA.items()],
                 columns=["fecha", "articulo_id", "demanda", "vendido"]).to_csv(out, index=False)
    print("demanda verdadera ->", out)
