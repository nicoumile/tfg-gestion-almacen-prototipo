"""
Desempeño de proveedores (HU-025, Sprint 3, 10/2026).

Pregunta para el comerciante: ¿qué me deja cada proveedor, cuánta plata
me tiene inmovilizada, cada cuánto le compro, cuánto me aumenta y si sus
productos se me quedan sin stock?

Indicadores por proveedor, en un período [desde, hasta]:
- Compras: monto facturado s/IVA, % del total, facturas y cada cuántos días
  se le compra (promedio entre fechas de factura de toda la historia).
- Lo que generan sus productos: venta neta de IVA, ganancia real y margen
  sobre venta (mismo cálculo que Márgenes reales: costo guardado al vender,
  incluye lo vendido dentro de combos, sin consumo interno).
- Stock: valor a costo hoy de sus productos, días de cobertura (stock /
  costo vendido por día) y GMROI anualizado = ganancia anualizada / stock a
  costo (cuántos pesos de ganancia deja por año cada peso invertido en su
  mercadería; indicador clásico de retail).
- Aumento de costo: variación mensual equivalente entre la primera y la
  última compra de cada artículo a ese proveedor (al menos 7 días entre
  ambas), promediada ponderando por lo comprado.
- Precio frente a otros: para artículos que vende más de un proveedor, cuánto
  más caro es en promedio que el más barato (0 % = siempre el más barato).
- Días sin stock: % de los días-artículo del período en que sus productos
  arrancaron el día sin stock y no vendieron (la misma reconstrucción de stock
  del pronóstico, tanda 9). Mide disponibilidad, no culpa: el quiebre puede
  ser del proveedor o de no haberle comprado a tiempo.

Atribución de cada artículo a sus proveedores: según la cantidad comprada a
cada uno hasta `hasta` (si un artículo se compró 70 % a A y 30 % a B, sus
ventas, ganancia y stock se reparten 70/30). Los que nunca tuvieron factura
pero están asignados a un proveedor (ej. consignación) se le atribuyen a ese;
los demás quedan en "Sin factura de compra" (stock de la carga inicial).

Límite: no se mide el plazo de entrega ni el cumplimiento de pedidos porque
no se registran pedidos (el comerciante compra en el momento, en el mayorista o al
preventista). Sin puntaje compuesto: los indicadores se muestran por separado
(decisión del Sprint 3).
"""
import datetime as dt

import numpy as np
import pandas as pd
from sqlalchemy import text

SIN_PROVEEDOR = "Sin factura de compra"


def _consulta(engine, hasta):
    compras = pd.read_sql(text("""
        SELECT f.proveedor_id, f.id AS factura_id, f.fecha, d.articulo_id,
               d.cantidad::float AS cantidad, d.costo_unitario::float AS costo_unitario
        FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
        WHERE f.fecha <= :hasta
    """), engine, params={"hasta": hasta})
    proveedores = pd.read_sql(text("""
        SELECT id AS proveedor_id, trim(nombre) AS proveedor,
               COALESCE((to_jsonb(p) ->> 'consignacion')::boolean, FALSE) AS consignacion
        FROM proveedores p
    """), engine)
    asignados = pd.read_sql(text("""
        SELECT articulo_id, proveedor_id, costo_compra::float AS costo_compra FROM articulo_proveedor_precio
    """), engine)
    articulos = pd.read_sql(text("""
        SELECT id AS articulo_id, nombre, categoria, iva::float AS iva,
               GREATEST(stock_actual, 0)::float AS stock, costo_compra_actual::float AS costo
        FROM articulos WHERE categoria IS DISTINCT FROM 'Promocion'
    """), engine)
    return compras, proveedores, asignados, articulos


def _ventas(engine, desde, hasta):
    return pd.read_sql(text("""
        SELECT va.articulo_id, SUM(va.monto_atribuido)::float AS ingreso_con_iva,
               SUM(va.costo_atribuido)::float AS costo_vendido
        FROM v_ventas_analitica va JOIN articulos a ON a.id = va.articulo_id
        WHERE a.categoria IS DISTINCT FROM 'Promocion' AND va.es_consumo_interno = FALSE
          AND va.fecha >= :desde AND va.fecha <= :hasta
        GROUP BY va.articulo_id
    """), engine, params={"desde": desde, "hasta": hasta})


def participaciones(compras, asignados, articulos):
    """DataFrame (articulo_id, proveedor_id, share): qué parte de cada artículo es de cada proveedor."""
    if not compras.empty:
        q = compras.groupby(["articulo_id", "proveedor_id"])["cantidad"].sum().reset_index()
        q = q[q["cantidad"] > 0]
        q["share"] = q["cantidad"] / q.groupby("articulo_id")["cantidad"].transform("sum")
        q = q[["articulo_id", "proveedor_id", "share"]]
    else:
        q = pd.DataFrame(columns=["articulo_id", "proveedor_id", "share"])
    sin_compra = asignados[~asignados["articulo_id"].isin(q["articulo_id"])][["articulo_id", "proveedor_id"]].copy()
    if not sin_compra.empty:
        sin_compra["share"] = 1.0 / sin_compra.groupby("articulo_id")["proveedor_id"].transform("count")
    huerfanos = articulos[~articulos["articulo_id"].isin(set(q["articulo_id"]) | set(sin_compra["articulo_id"]))]
    h = pd.DataFrame({"articulo_id": huerfanos["articulo_id"], "proveedor_id": -1, "share": 1.0})
    partes = [x for x in (q, sin_compra, h) if not x.empty]
    return pd.concat(partes, ignore_index=True) if partes else q


def aumento_mensual(compras, min_dias=7):
    """{proveedor_id: (aumento mensual equivalente ponderado, artículos medidos)}."""
    res = {}
    if compras.empty:
        return res
    c = compras[compras["costo_unitario"] > 0].sort_values(["fecha", "factura_id"])
    for pid, g in c.groupby("proveedor_id"):
        tasas, pesos = [], []
        for _, ga in g.groupby("articulo_id"):
            primera, ultima = ga.iloc[0], ga.iloc[-1]
            dias = (pd.Timestamp(ultima["fecha"]) - pd.Timestamp(primera["fecha"])).days
            if dias < min_dias:
                continue
            tasas.append((ultima["costo_unitario"] / primera["costo_unitario"]) ** (30.0 / dias) - 1)
            pesos.append(float((ga["cantidad"] * ga["costo_unitario"]).sum()))
        if tasas:
            res[pid] = (float(np.average(tasas, weights=pesos)), len(tasas))
    return res


def precio_vs_mejor(asignados):
    """{proveedor_id: (sobreprecio promedio frente al más barato, artículos comparables)}."""
    a = asignados[asignados["costo_compra"] > 0]
    multi = a[a.groupby("articulo_id")["proveedor_id"].transform("count") > 1].copy()
    if multi.empty:
        return {}
    multi["sobre"] = multi["costo_compra"] / multi.groupby("articulo_id")["costo_compra"].transform("min") - 1
    return {pid: (float(g["sobre"].mean()), int(len(g))) for pid, g in multi.groupby("proveedor_id")}


def dias_sin_stock(engine, desde, hasta, compartes):
    """{proveedor_id: (% días-artículo sin stock en el período, días-artículo medidos)} con la
    reconstrucción de stock del pronóstico. Vacío si no se puede calcular (nunca rompe)."""
    try:
        import pronostico as pron
        Y, _, articulos = pron.preparar(engine, hoy=pd.Timestamp(hasta) + pd.Timedelta(days=1))
        if Y.empty:
            return {}
        obs, _ = pron.observados(engine, Y, articulos)
        fechas = pd.DatetimeIndex(Y.index)
        en = (fechas >= pd.Timestamp(desde)) & (fechas <= pd.Timestamp(hasta))
        inicio = pron._inicio(Y.values, fechas, articulos.set_index("articulo_id")["alta"]
                              .reindex(Y.columns).pipe(pd.to_datetime).tolist())
        vivo = np.arange(len(fechas))[:, None] >= inicio[None, :]
        censur = (~obs) & vivo & en[:, None]
        medidos = vivo & en[:, None]
        por_art = pd.DataFrame({"articulo_id": Y.columns, "sin_stock": censur.sum(axis=0),
                                "dias": medidos.sum(axis=0)})
        m = compartes.merge(por_art, on="articulo_id")
        m["sin_stock"] *= m["share"]
        m["dias"] *= m["share"]
        g = m.groupby("proveedor_id")[["sin_stock", "dias"]].sum()
        return {pid: (float(r["sin_stock"] / r["dias"]) if r["dias"] > 0 else float("nan"), float(r["dias"]))
                for pid, r in g.iterrows()}
    except Exception:  # noqa: BLE001 -- indicador complementario
        return {}


def desempeno(engine, desde, hasta=None, con_dias_sin_stock=True):
    """
    Una fila por proveedor (más "Sin factura de compra" si hay ventas o stock sin proveedor).
    Devuelve (tabla, detalle): detalle = participaciones por artículo con ventas, ganancia y stock.
    """
    hasta = hasta or dt.date.today()
    desde, hasta = pd.Timestamp(desde).date(), pd.Timestamp(hasta).date()
    dias_periodo = max((hasta - desde).days + 1, 1)
    compras, proveedores, asignados, articulos = _consulta(engine, hasta)
    ventas = _ventas(engine, desde, hasta)

    partes = participaciones(compras, asignados, articulos)
    det = (partes.merge(articulos, on="articulo_id", how="inner")
           .merge(ventas, on="articulo_id", how="left").fillna({"ingreso_con_iva": 0.0, "costo_vendido": 0.0}))
    det["venta_neta"] = det["share"] * det["ingreso_con_iva"] / (1 + det["iva"].fillna(0.21))
    det["costo_vendido"] = det["share"] * det["costo_vendido"]
    det["ganancia"] = det["venta_neta"] - det["costo_vendido"]
    det["stock_costo"] = det["share"] * det["stock"] * det["costo"].fillna(0)

    agg = det.groupby("proveedor_id").agg(
        articulos=("articulo_id", "nunique"), venta_neta=("venta_neta", "sum"),
        costo_vendido=("costo_vendido", "sum"), ganancia=("ganancia", "sum"), stock_costo=("stock_costo", "sum"),
    )
    articulos_vendidos = det[det["venta_neta"] > 0].groupby("proveedor_id")["articulo_id"].nunique()
    agg["articulos_vendidos"] = articulos_vendidos.reindex(agg.index).fillna(0).astype(int)

    cp = compras[(compras["fecha"] >= desde) & (compras["fecha"] <= hasta)].copy()
    cp["monto"] = cp["cantidad"] * cp["costo_unitario"]
    compras_p = cp.groupby("proveedor_id").agg(compras=("monto", "sum"), facturas=("factura_id", "nunique"))

    frecuencia = {}
    for pid, g in compras.groupby("proveedor_id"):
        f = sorted(pd.to_datetime(g["fecha"]).unique())
        if len(f) >= 2:
            frecuencia[pid] = float(np.mean(np.diff(f).astype("timedelta64[D]").astype(float)))

    aum, vs_mejor = aumento_mensual(compras), precio_vs_mejor(asignados)
    sin_stock = dias_sin_stock(engine, desde, hasta, partes) if con_dias_sin_stock else {}

    nombres = dict(zip(proveedores["proveedor_id"], proveedores["proveedor"]))
    consig = dict(zip(proveedores["proveedor_id"], proveedores["consignacion"]))
    filas = []
    for pid in sorted(set(agg.index) | set(compras_p.index)):
        a = agg.loc[pid] if pid in agg.index else None
        c = compras_p.loc[pid] if pid in compras_p.index else None
        venta = float(a["venta_neta"]) if a is not None else 0.0
        ganancia = float(a["ganancia"]) if a is not None else 0.0
        stock_c = float(a["stock_costo"]) if a is not None else 0.0
        costo_dia = (float(a["costo_vendido"]) if a is not None else 0.0) / dias_periodo
        filas.append({
            "proveedor_id": pid, "proveedor": nombres.get(pid, SIN_PROVEEDOR),
            "consignacion": bool(consig.get(pid, False)),
            "compras": float(c["compras"]) if c is not None else 0.0,
            "facturas": int(c["facturas"]) if c is not None else 0,
            "cada_dias": frecuencia.get(pid, float("nan")),
            "articulos": int(a["articulos"]) if a is not None else 0,
            "articulos_vendidos": int(a["articulos_vendidos"]) if a is not None else 0,
            "venta_neta": venta, "ganancia": ganancia,
            "margen": ganancia / venta if venta > 0 else float("nan"),
            "stock_costo": stock_c,
            "cobertura_dias": stock_c / costo_dia if costo_dia > 0 else float("nan"),
            "gmroi": (ganancia * 365.0 / dias_periodo) / stock_c if stock_c > 0 else float("nan"),
            "aumento_mensual": aum.get(pid, (float("nan"), 0))[0],
            "articulos_aumento": aum.get(pid, (float("nan"), 0))[1],
            "sobreprecio": vs_mejor.get(pid, (float("nan"), 0))[0],
            "articulos_comparables": vs_mejor.get(pid, (float("nan"), 0))[1],
            "dias_sin_stock": sin_stock.get(pid, (float("nan"), 0))[0],
        })
    tabla = pd.DataFrame(filas)
    if tabla.empty:
        return tabla, det
    total_compras = tabla["compras"].sum()
    tabla["pct_compras"] = tabla["compras"] / total_compras if total_compras > 0 else float("nan")
    total_ganancia = tabla["ganancia"].sum()
    tabla["pct_ganancia"] = tabla["ganancia"] / total_ganancia if total_ganancia else float("nan")
    tabla = tabla.sort_values(["ganancia", "compras"], ascending=False).reset_index(drop=True)
    det["proveedor"] = det["proveedor_id"].map(nombres).fillna(SIN_PROVEEDOR)
    return tabla, det
