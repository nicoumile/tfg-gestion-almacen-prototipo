"""
Pronóstico de demanda por artículo (Sprint 2, HU-014).

Todo se calcula en pandas/numpy a partir de 3 consultas (ventas analíticas,
artículos, fraccionamientos), sin una consulta por artículo.

1. Serie de demanda diaria por artículo (demanda_diaria):
   - Fuente: v_ventas_analitica (los combos ya vienen resueltos en sus
     componentes reales, que son los que se compran).
   - Incluye consumo interno: consume stock aunque no facture.
   - Fraccionados: la venta del producto suelto se pasa a su bolsa de origen
     (demanda_bolsa += demanda_suelto / factor_fraccion), porque lo que se
     compra es la bolsa.
   - Reemplazos: la historia de un artículo dado de baja y reemplazado se
     suma al artículo que lo reemplaza.
   - Calendario continuo: los días sin venta valen 0. Antes de la primera
     venta de un artículo no hay historia (no se cuentan como ceros).
   - Censura: un día sin stock se registra como demanda 0 (no se vendió porque
     no había). dias_observados marca esos días y los modelos no los cuentan
     al ajustar, para no subestimar a los que más se quiebran.

2. Modelos (pronosticar), todos ajustados por calendario (día de semana y
   bloque del mes, índices estimados sobre la venta total del almacén)
   salvo la línea base:
   - "promedio_30": promedio de los últimos 30 días (el método anterior de
     la app) -> línea base para comparar.
   - "ses_<alfa>": nivel propio del artículo con suavizado exponencial simple
     sobre la demanda desestacionalizada (memoria efectiva ~2/alfa días).
   - "croston_<alfa>": Croston con corrección de Syntetos-Boylan (SBA), para
     venta intermitente (muchos días sin venta).
   - La historia de cada artículo cuenta desde su alta en el sistema (o desde
     el primer día de datos): los días sin venta son demanda 0.
   - Artículo nuevo (alta posterior al inicio de los datos) con menos de 28
     días: se mezcla con el promedio de su categoría con un peso de
     credibilidad días/(días + K_CATEGORIA). Nunca vendido: pronóstico 0.
   La configuración se ELIGE POR CLASE ABC con la evaluación (no se fija de
   antemano): en la base simulada gana la memoria larga porque la
   popularidad simulada es fija; con datos reales la demanda cambia y la
   evaluación puede elegir otra. Sin historia suficiente: CONFIG_POR_DEFECTO.

3. Evaluación (backtest): origen móvil sobre los últimos N días. En cada
   origen se recalcula todo (índices, niveles, clase ABC) solo con datos
   anteriores a ese día y se pronostican los H días siguientes. Métricas por
   clase ABC: WAPE y sesgo sobre la demanda acumulada de H días (la que usa
   la lista de compras) y MAE diario.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from sqlalchemy import text

# Cambiarla cuando cambie la forma o el cálculo de lo que devuelve este
# módulo: la app la pasa a sus cachés, así un deploy nuevo no reutiliza
# resultados calculados por la versión anterior.
VERSION_DATOS = "2026-10-26-hora-ar"
ADI_INTERMITENTE = 1.32      # umbral de Syntetos, Boylan y Croston (2005)
DIAS_HISTORIA_PLENA = 28
K_CATEGORIA = 1          # días de historia que "vale" el promedio de la categoría (ver nota en pronosticar)
VENTANA_ABC = 90
VENTANA_INDICES = 182
ALFA_NIVEL = 0.10
ALFA_CROSTON = 0.10


# ----------------------------------------------------------------------------
# 1. Datos
# ----------------------------------------------------------------------------

def cargar_insumos(engine, hasta=None):
    """Tres consultas: ventas analíticas, artículos y nada más (los
    fraccionamientos no hacen falta: la relación bolsa/suelto está en articulos)."""
    # Tanda 18: lo que se tiró porque venció (medio 'Vencido') no es demanda.
    params = {}
    filtro = "WHERE medio_pago IS DISTINCT FROM 'Vencido'"
    if hasta is not None:
        filtro += " AND fecha <= :hasta"
        params["hasta"] = hasta
    ventas = pd.read_sql(text(f"""
        SELECT fecha, articulo_id, cantidad, monto_atribuido, es_consumo_interno
        FROM v_ventas_analitica {filtro}
    """), engine, params=params)
    articulos = pd.read_sql(text("""
        SELECT id AS articulo_id, nombre, categoria, activo, reemplazado_por,
               articulo_fraccion_de, factor_fraccion, stock_actual, unidad_medida,
               creado_en::date AS alta,
               -- vía jsonb para no fallar en una base sin migracion_lista_compras.py
               (to_jsonb(articulos) ->> 'reposicion_dias')::int AS reposicion_dias
        FROM articulos
    """), engine)
    return ventas, articulos


def _destino_reemplazo(articulos):
    """articulo_id -> artículo vigente que lo reemplaza (sigue la cadena)."""
    sig = dict(zip(articulos["articulo_id"], articulos["reemplazado_por"]))
    destino = {}
    for aid in sig:
        actual, vistos = aid, set()
        while pd.notna(sig.get(actual)) and actual not in vistos:
            vistos.add(actual)
            actual = int(sig[actual])
        destino[aid] = actual
    return destino


def demanda_diaria(ventas, articulos, fecha_fin=None):
    """
    Devuelve (Y, ingreso) como DataFrames días x artículo (índice = fechas
    continuas). Y = unidades demandadas del artículo que se COMPRA.
    """
    if ventas.empty:
        return pd.DataFrame(), pd.DataFrame()
    v = ventas.copy()
    # Compra diaria (pan, perecederos): el consumo interno es el SOBRANTE que
    # se regala o se tira, no demanda. Para el resto, consumo interno (ej.:
    # el dueño se lleva algo) sí consume stock y se cuenta.
    if "reposicion_dias" in articulos.columns and "es_consumo_interno" in v.columns:
        diarios = set(articulos.loc[articulos["reposicion_dias"] == 1, "articulo_id"])
        # Tanda 16: también su producto fraccionado (ej.: "pan de ayer", que se
        # pasa en Fraccionar): lo que se tira de ahí es sobrante del pan, no venta.
        if diarios and "articulo_fraccion_de" in articulos.columns:
            diarios |= set(articulos.loc[articulos["articulo_fraccion_de"].isin(diarios), "articulo_id"])
        if diarios:
            v = v[~(v["es_consumo_interno"].fillna(False).astype(bool) & v["articulo_id"].isin(diarios))]
    v["fecha"] = pd.to_datetime(v["fecha"])
    v["cantidad"] = v["cantidad"].astype(float)
    v["monto_atribuido"] = v["monto_atribuido"].fillna(0).astype(float)

    # Fraccionados -> bolsa de origen (en unidades de bolsa)
    frac = articulos.dropna(subset=["articulo_fraccion_de"])
    if not frac.empty:
        padre = dict(zip(frac["articulo_id"], frac["articulo_fraccion_de"].astype(int)))
        factor = dict(zip(frac["articulo_id"], frac["factor_fraccion"].astype(float)))
        es_der = v["articulo_id"].isin(padre)
        f = v.loc[es_der, "articulo_id"].map(factor).replace(0, np.nan)
        v.loc[es_der, "cantidad"] = v.loc[es_der, "cantidad"] / f.fillna(1)
        v.loc[es_der, "articulo_id"] = v.loc[es_der, "articulo_id"].map(padre)

    # Reemplazos -> artículo vigente
    destino = _destino_reemplazo(articulos)
    v["articulo_id"] = v["articulo_id"].map(lambda a: destino.get(a, a))

    fin = pd.Timestamp(fecha_fin) if fecha_fin is not None else v["fecha"].max()
    fechas = pd.date_range(v["fecha"].min(), fin, freq="D")
    Y = v.pivot_table(index="fecha", columns="articulo_id", values="cantidad", aggfunc="sum")
    I = v.pivot_table(index="fecha", columns="articulo_id", values="monto_atribuido", aggfunc="sum")
    Y = Y.reindex(fechas).fillna(0.0)
    I = I.reindex(index=fechas, columns=Y.columns).fillna(0.0)
    return Y, I


def movimientos_stock(engine):
    """
    Movimientos diarios de stock por artículo (todos, hasta hoy): compras (+),
    ventas y consumo interno (−; los combos virtuales mueven a sus
    componentes), fraccionamientos (bolsa −, suelto +) y correcciones
    manuales de stock (las que deja el Historial: "Stock corregido a mano en
    'X': a -> b"). No incluye el armado de combos físicos (raro; queda como
    límite). Devuelve DataFrame fecha, articulo_id, delta.
    """
    with engine.connect() as conn:
        hay_desglose = conn.execute(text("SELECT to_regclass('venta_componentes') IS NOT NULL")).scalar()
    if hay_desglose:
        ventas_sql = """
            SELECT v.fecha, COALESCE(vc.componente_articulo_id, v.articulo_id) AS articulo_id,
                   -SUM(v.cantidad * COALESCE(vc.cantidad_por_unidad, 1)) AS delta
            FROM ventas v
            LEFT JOIN venta_componentes vc ON vc.venta_id = v.id AND vc.mueve_stock
            WHERE v.articulo_id IS NOT NULL
            GROUP BY 1, 2"""
    else:  # base sin la migración de la tanda 5: composición actual de los combos virtuales
        ventas_sql = """
            SELECT v.fecha, COALESCE(pc.componente_articulo_id, v.articulo_id) AS articulo_id,
                   -SUM(v.cantidad * COALESCE(pc.cantidad_por_unidad, 1)) AS delta
            FROM ventas v
            JOIN articulos a ON a.id = v.articulo_id
            LEFT JOIN promocion_componentes pc
                   ON pc.promocion_articulo_id = a.id AND a.promo_tipo = 'virtual'
            GROUP BY 1, 2"""
    partes = [
        ventas_sql,
        """SELECT f.fecha, d.articulo_id, SUM(d.cantidad) AS delta
           FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id GROUP BY 1, 2""",
        """SELECT fecha, articulo_bolsa_id AS articulo_id, -SUM(cantidad_bolsas) AS delta
           FROM fraccionamientos GROUP BY 1, 2""",
        """SELECT fecha, articulo_derivado_id AS articulo_id, SUM(cantidad_generada) AS delta
           FROM fraccionamientos GROUP BY 1, 2""",
        # fecha en hora argentina: el historial se guarda en UTC (tanda 20)
        r"""SELECT (fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS fecha, registro_id AS articulo_id,
                  SUM((regexp_match(detalle, ':\s*(-?[0-9.]+)\s*->\s*(-?[0-9.]+)\s*$'))[2]::numeric
                      - (regexp_match(detalle, ':\s*(-?[0-9.]+)\s*->\s*(-?[0-9.]+)\s*$'))[1]::numeric) AS delta
           FROM historial_cambios
           WHERE tabla = 'articulos' AND detalle LIKE 'Stock corregido a mano en %%'
             AND detalle ~ ':\s*-?[0-9.]+\s*->\s*-?[0-9.]+\s*$'
           GROUP BY 1, 2""",
    ]
    mov = pd.concat([pd.read_sql(text(sql.replace("%%", "%")), engine) for sql in partes], ignore_index=True)
    mov["fecha"] = pd.to_datetime(mov["fecha"])
    mov["delta"] = mov["delta"].astype(float)
    return mov


MIN_DIAS_OBSERVADOS = 7   # días con stock que hacen falta para corregir la censura de un artículo


def dias_observados(Y, articulos, movimientos, tolerancia=1e-6, minimo_observados=MIN_DIAS_OBSERVADOS):
    """
    Máscara (días x artículo, mismas filas y columnas que Y) de los días en
    que la demanda se OBSERVÓ. Un día es censurado cuando el artículo no
    vendió y arrancó el día sin stock: no se vendió porque no había, no
    porque nadie lo quisiera (con datos de venta, un día sin stock se ve
    igual que un día sin demanda).
    El stock al final de cada día se reconstruye hacia atrás desde el stock
    actual: stock(d) = stock_hoy − Σ movimientos posteriores a d.
    Solo se corrigen los artículos cuyo stock reconstruido nunca queda
    negativo: si queda negativo faltan compras cargadas y el stock no es
    confiable (se dejan como antes). Los días anteriores al comienzo de la
    historia del artículo (_inicio: su alta o su primera venta) no se corrigen.
    Tampoco se corrigen los artículos con menos de `minimo_observados` días
    con stock: el pronóstico quedaría apoyado en uno o dos días (ej.: vendió 1
    el primer día y después estuvo 3 semanas sin stock -> "1 por día").
    Devuelve (obs, info): obs ndarray bool; info con los conteos.
    """
    T, n = Y.shape
    obs = np.ones((T, n), dtype=bool)
    info = {"articulos": n, "corregidos": 0, "no_confiables": 0, "dias_censurados": 0}
    if T == 0 or n == 0:
        return obs, info
    fechas = pd.DatetimeIndex(Y.index)
    a = articulos.set_index("articulo_id")
    stock_hoy = a["stock_actual"].astype(float)
    alta = pd.to_datetime(a["alta"])
    m = movimientos[movimientos["articulo_id"].isin(Y.columns)]
    piv = m.pivot_table(index="fecha", columns="articulo_id", values="delta", aggfunc="sum")
    # movimientos POSTERIORES al final de cada día de la serie (incluye los
    # que caen después del último día de la serie, hasta hoy)
    todas = piv.index.union(fechas).sort_values()
    piv = piv.reindex(index=todas, columns=Y.columns).fillna(0.0)
    posteriores = piv.iloc[::-1].cumsum().iloc[::-1].shift(-1).fillna(0.0)  # Σ_{t>d}
    fin_dia = (stock_hoy.reindex(Y.columns).fillna(0.0).values[None, :] - posteriores.values)
    fin_dia = pd.DataFrame(fin_dia, index=todas, columns=Y.columns)
    inicio_dia = fin_dia.shift(1)
    # antes del primer movimiento conocido, el stock al empezar el día es el del final
    inicio_dia = inicio_dia.fillna(fin_dia)
    ini = inicio_dia.reindex(fechas).values
    fin = fin_dia.reindex(fechas).values
    confiable = (np.nanmin(fin, axis=0) >= -tolerancia) & (np.nanmin(ini, axis=0) >= -tolerancia)
    sin_stock = (ini <= tolerancia) & (Y.values <= 0)
    # mismo comienzo de historia que usa el pronóstico (_inicio): alta del
    # artículo, o su primera venta si la venta es anterior al alta registrada
    inicio = _inicio(Y.values, fechas, alta.reindex(Y.columns).tolist())
    despues_alta = np.arange(T)[:, None] >= inicio[None, :]
    censurado = sin_stock & confiable[None, :] & despues_alta
    con_stock = (despues_alta & ~censurado).sum(axis=0)
    poca_evidencia = censurado.any(axis=0) & (con_stock < minimo_observados)
    censurado[:, poca_evidencia] = False
    obs = ~censurado
    info.update({"corregidos": int((censurado.any(axis=0)).sum()),
                 "no_confiables": int((~confiable).sum()),
                 "poca_evidencia": int(poca_evidencia.sum()),
                 "dias_censurados": int(censurado.sum())})
    return obs, info


def articulos_objetivo(articulos, columnas):
    """Artículos que se pronostican: activos, que no son combos ni sueltos fraccionados."""
    a = articulos.set_index("articulo_id")
    ok = a["activo"].fillna(True) & (a["categoria"] != "Promocion") & a["articulo_fraccion_de"].isna()
    return [c for c in columnas if c in a.index and ok.get(c, False)]


# ----------------------------------------------------------------------------
# 2. Piezas del modelo
# ----------------------------------------------------------------------------

def bloque_mes(fechas):
    d = pd.DatetimeIndex(fechas).day
    return np.where(d <= 10, 0, np.where(d <= 20, 1, 2))


def indices_calendario(total):
    """
    Índices multiplicativos (media 1) de día de semana y bloque del mes,
    estimados sobre la venta total diaria. Con menos de 4 semanas -> todos 1.
    """
    total = total.iloc[-VENTANA_INDICES:]
    if len(total) < 28 or total.mean() <= 0:
        return np.ones(7), np.ones(3)
    idx = pd.DatetimeIndex(total.index)
    media = total.mean()
    dow = np.array([total[idx.dayofweek == k].mean() / media if (idx.dayofweek == k).any() else 1.0
                    for k in range(7)])
    desest = total.values / dow[idx.dayofweek]
    b = bloque_mes(idx)
    m2 = desest.mean()
    blo = np.array([desest[b == k].mean() / m2 if (b == k).any() else 1.0 for k in range(3)])
    dow = np.where(np.isfinite(dow) & (dow > 0), dow, 1.0)
    blo = np.where(np.isfinite(blo) & (blo > 0), blo, 1.0)
    return dow, blo


def factor_calendario(fechas, dow, blo):
    idx = pd.DatetimeIndex(fechas)
    return dow[idx.dayofweek] * blo[bloque_mes(idx)]


def _inicio(Y, fechas=None, alta=None):
    """
    Fila desde la que cuenta la historia de cada artículo: su alta en el
    sistema (articulos.creado_en) o, si la venta es anterior al alta
    registrada (base regenerada), su primera venta; nunca antes del primer
    día de la serie. Un artículo cargado en el catálogo antes de que
    empezaran a registrarse ventas tiene historia desde el día 1, aunque
    tarde en venderse (esos días sin venta son demanda 0, no "sin datos").
    Sin `alta`: primera venta. len(Y) si nunca vendió y no tiene alta.
    """
    T = Y.shape[0]
    primera = np.argmax(Y > 0, axis=0)
    primera[~(Y > 0).any(axis=0)] = T
    if alta is None or fechas is None:
        return primera
    a = pd.to_datetime(pd.Series(alta)).values
    pos_alta = np.searchsorted(pd.DatetimeIndex(fechas).values, a, side="left")
    valida = ~pd.isna(a) & (pos_alta <= primera)
    # El día del alta suele ser incompleto (el artículo se cargó a mitad del
    # día): para un artículo nuevo la historia arranca al día siguiente.
    inicio = np.where(valida, np.where(pos_alta > 0, pos_alta + 1, 0), primera)
    return np.clip(inicio, 0, T)


def ses(Z, inicio, alfa=ALFA_NIVEL, obs=None):
    """Suavizado exponencial simple columna por columna (vectorizado en el
    tiempo). El nivel arranca con el promedio de los primeros 7 días de cada
    artículo. Devuelve el nivel al final de la serie.
    obs (opcional, días x artículo): False en los días censurados (sin stock):
    ese día no actualiza el nivel, en vez de empujarlo hacia 0."""
    T, n = Z.shape
    obs = np.ones((T, n), dtype=bool) if obs is None else obs
    nivel = np.full(n, np.nan)
    for j in np.unique(inicio[inicio < T]):
        cols = inicio == j
        bloque, ok = Z[j:j + 7, cols], obs[j:j + 7, cols]
        cuenta = ok.sum(axis=0)
        nivel[cols] = np.where(cuenta > 0, (bloque * ok).sum(axis=0) / np.maximum(cuenta, 1), bloque.mean(axis=0))
    for t in range(T):
        activos = (inicio <= t) & ~np.isnan(nivel) & obs[t]
        if t >= 7:
            nivel[activos] = nivel[activos] + alfa * (Z[t, activos] - nivel[activos])
    return np.nan_to_num(nivel)


def croston_sba(Y, inicio, alfa=ALFA_CROSTON, obs=None):
    """Croston-SBA: suaviza tamaño de la venta (z) e intervalo entre ventas (p)
    solo cuando hay venta. Pronóstico diario plano = (1 - alfa/2) * z / p.
    obs (opcional): un día censurado (sin stock) no cuenta en el intervalo
    entre ventas: no había qué vender."""
    T, n = Y.shape
    obs = np.ones((T, n), dtype=bool) if obs is None else obs
    z = np.full(n, np.nan)
    p = np.full(n, np.nan)
    q = np.ones(n)  # días desde la última venta (o desde el inicio, antes de la 1.ª)
    for t in range(T):
        activos = (inicio <= t) & obs[t]
        hay = activos & (Y[t] > 0)
        nuevos = hay & np.isnan(z)
        z[nuevos] = Y[t, nuevos]
        p[nuevos] = q[nuevos]
        upd = hay & ~nuevos
        z[upd] += alfa * (Y[t, upd] - z[upd])
        p[upd] += alfa * (q[upd] - p[upd])
        q[hay] = 1.0
        q[activos & ~hay] += 1.0
    f = (1 - alfa / 2) * z / p
    return np.nan_to_num(f)


def adi(Y, inicio):
    """Intervalo medio entre ventas (días), desde la primera venta."""
    T = Y.shape[0]
    dias = np.maximum(T - inicio, 1)
    n_ventas = (Y > 0).sum(axis=0)
    return np.where(n_ventas > 0, dias / np.maximum(n_ventas, 1), np.inf)


def clases_abc(I, corte_a=80.0, corte_b=95.0):
    """Clase ABC por ingreso de la ventana (mismo criterio que db.clasificacion_abc)."""
    s = I.sum(axis=0)
    clase = np.full(len(s), "C", dtype=object)
    if s.sum() <= 0:
        return clase
    orden = np.argsort(-s)
    acum = np.cumsum(s[orden]) / s.sum() * 100
    c = np.where(acum <= corte_a, "A", np.where(acum <= corte_b, "B", "C"))
    clase[orden] = c
    clase[s <= 0] = "C"
    return clase


# ----------------------------------------------------------------------------
# 3. Pronóstico
# ----------------------------------------------------------------------------
# Configuraciones candidatas: (familia, parámetro). Todas, salvo la línea
# base, se ajustan por calendario (día de semana x bloque del mes).
#   promedio_30          -> método actual de la app (línea base, sin calendario)
#   ses_<alfa>           -> nivel suavizado (memoria efectiva ~ 2/alfa días)
#   croston_<alfa>       -> Croston-SBA (tamaño e intervalo entre ventas)
CONFIGS = {
    "promedio_30": ("promedio", 30),
    "ses_0.02": ("ses", 0.02),
    "ses_0.05": ("ses", 0.05),
    "ses_0.10": ("ses", 0.10),
    "croston_0.05": ("croston", 0.05),
    "croston_0.10": ("croston", 0.10),
}
# Si no hay historia suficiente para evaluar (base nueva), se usa esta.
# Memoria efectiva ~40 días: más lenta que el promedio de 30 días al ruido,
# pero sin quedar atada a meses viejos mientras el surtido del comerciante cambia.
CONFIG_POR_DEFECTO = "ses_0.05"
MIN_ORIGENES_EVALUACION = 14


def pronosticar(Yh, categorias, fechas_futuras, config=CONFIG_POR_DEFECTO, Ih=None, alta=None, obs=None):
    """
    Yh: ndarray (T, n), historia hasta el día anterior al primer día a
    pronosticar. alta: fecha de alta de cada artículo (o None). obs: máscara
    de días observados (dias_observados; None = todos). Devuelve (F, info): F ndarray (h, n) con la demanda diaria
    pronosticada; info con el modelo usado por artículo, la clase ABC, etc.
    """
    obs = np.ones(Yh.shape, dtype=bool) if obs is None else obs
    familia, par = CONFIGS[config]
    T, n = Yh.shape
    h = len(fechas_futuras)
    fechas_hist = pd.date_range(end=pd.Timestamp(fechas_futuras[0]) - pd.Timedelta(days=1), periods=T)
    inicio = _inicio(Yh, fechas_hist, alta)
    dias_hist = T - inicio
    vendio = (Yh > 0).any(axis=0)
    total = pd.Series(Yh.sum(axis=1), index=fechas_hist)
    dow, blo = indices_calendario(total)
    cal_hist = factor_calendario(fechas_hist, dow, blo)
    cal_fut = factor_calendario(fechas_futuras, dow, blo)

    ventana = min(30, T)
    # días observados de la ventana desde el inicio de cada artículo (los censurados no cuentan)
    en_ventana = (np.arange(T - ventana, T)[:, None] >= inicio[None, :]) & obs[-ventana:]
    prom30 = (Yh[-ventana:] * obs[-ventana:]).sum(axis=0) / np.clip(en_ventana.sum(axis=0), 1, None)

    if familia == "promedio":
        base = np.tile(prom30, (h, 1))
    elif familia == "ses":
        base = cal_fut[:, None] * ses(Yh / cal_hist[:, None], inicio, par, obs)[None, :]
    else:
        base = cal_fut[:, None] * croston_sba(Yh, inicio, par, obs)[None, :]
    usado = np.full(n, config, dtype=object)

    # Nunca vendido: sin historia no hay demanda que proyectar (si es un
    # artículo recién dado de alta, empieza a pronosticarse con su 1.ª venta).
    sin_ventas = ~vendio
    base[:, sin_ventas] = 0.0
    usado[sin_ventas] = "sin_ventas"

    # Artículo NUEVO (dado de alta después de que empezó la serie) con menos
    # de 28 días: mezcla su propio pronóstico con el promedio diario de su
    # categoría, con peso proporcional a los días que lleva. Referencia:
    # artículos de la categoría con historia plena; si no hay, con al menos
    # 7 días; si tampoco, solo lo propio. Que la BASE sea nueva (pocos días
    # de datos para todos) no convierte en nuevos a los artículos.
    corta = vendio & (inicio > 0) & (dias_hist < DIAS_HISTORIA_PLENA)
    if corta.any():
        cats = np.asarray(categorias, dtype=object)
        plenos = dias_hist >= DIAS_HISTORIA_PLENA
        referencia = plenos if plenos.any() else (dias_hist >= 7)
        # Peso de credibilidad: la categoría vale lo mismo que K días de
        # historia propia (con 9 días y K=1: 90 % propio / 10 % categoría).
        # Calibrado con datos reales (12-18/10/2026, artículos nuevos): darle
        # más peso a la categoría subestimaba los nuevos (sesgo -68 % con la
        # mezcla lineal a 28 días, -52 % con K=3, -41 % con K=1, -26 % sin
        # mezcla). K=1 queda como resguardo mínimo ante un solo día atípico.
        w = dias_hist / (dias_hist + K_CATEGORIA)
        for j in np.where(corta)[0]:
            m = referencia & (cats == cats[j])
            m[j] = False
            if not m.any():
                continue
            ref = prom30[m].mean() * cal_fut
            base[:, j] = w[j] * base[:, j] + (1 - w[j]) * ref
            usado[j] = f"{usado[j]}+categoria"

    F = np.clip(np.nan_to_num(base), 0, None)
    clase = clases_abc(Ih[-VENTANA_ABC:]) if Ih is not None else np.full(n, "C", dtype=object)
    info = {"modelo": usado, "adi": adi(Yh, inicio), "dias_historia": dias_hist,
            "dow": dow, "bloque": blo, "clase": clase, "prom30": prom30}
    return F, info


# ----------------------------------------------------------------------------
# 4. Evaluación (backtest de origen móvil)
# ----------------------------------------------------------------------------

def backtest(Y, I, categorias_por_art, dias=60, horizonte=7, configs=tuple(CONFIGS), paso=1, alta_por_art=None,
             obs=None):
    """
    Y, I: DataFrames días x artículo (de demanda_diaria), filtrados a los
    artículos objetivo. Para cada origen t de los últimos `dias` días usa
    SOLO Y[:t] (sin fuga de datos) y pronostica t..t+horizonte-1. La clase
    ABC también se calcula en cada origen con los 90 días previos.
    obs: máscara de días observados; con ella los modelos no aprenden de los
    días sin stock. Cada fila marca si la semana a pronosticar tuvo algún
    día censurado (`censurado_h`), para analizar (ver metricas).
    Devuelve un DataFrame largo por origen, artículo y configuración.
    """
    fechas = Y.index
    obs = np.ones(Y.shape, dtype=bool) if obs is None else obs
    Yv, Iv = Y.values, I.values
    cats = [categorias_por_art.get(a, "") for a in Y.columns]
    alta = [alta_por_art.get(a) for a in Y.columns] if alta_por_art else None
    T = len(fechas)
    filas = []
    for t in range(max(28, T - dias - horizonte + 1), T - horizonte + 1, paso):
        fut = fechas[t:t + horizonte]
        real = Yv[t:t + horizonte]
        hist = Yv[:t]
        vivos = (hist > 0).any(axis=0)
        censurado_h = ~obs[t:t + horizonte].all(axis=0)
        for cfg in configs:
            F, info = pronosticar(hist, cats, fut, cfg, Iv[:t], alta, obs[:t])
            filas.append(pd.DataFrame({
                "origen": fechas[t], "articulo_id": Y.columns, "clase": info["clase"], "modelo": cfg,
                "real_h": real.sum(axis=0), "pron_h": F.sum(axis=0),
                "abs_diario": np.abs(real - F).sum(axis=0), "real_diario": real.sum(axis=0),
                "censurado_h": censurado_h,
            })[vivos])
    if not filas:
        return pd.DataFrame(columns=["origen", "articulo_id", "clase", "modelo", "real_h", "pron_h",
                                     "abs_diario", "real_diario", "censurado_h"])
    return pd.concat(filas, ignore_index=True)


def metricas(bt, por=("modelo", "clase"), excluir_censurados=False):
    """WAPE y sesgo de la demanda acumulada del horizonte, y WAPE diario,
    contra lo VENDIDO (lo único que se registra). Con quiebres, lo vendido
    es menos que la demanda: un pronóstico que corrige la censura aparece
    sobreestimando aunque acierte la demanda (en la base simulada, contra la
    demanda verdadera el sesgo de la clase A es -4,5 % y contra lo vendido
    +15,6 %). Dejar afuera las semanas con quiebre (excluir_censurados) no lo
    arregla: esas semanas son justamente las de más demanda, y lo que queda
    sesga igual. Por eso se mide en todas las semanas, como antes."""
    if excluir_censurados and "censurado_h" in bt.columns:
        bt = bt[~bt["censurado_h"].astype(bool)]
    g = bt.assign(abs_h=(bt["real_h"] - bt["pron_h"]).abs(), err_h=bt["pron_h"] - bt["real_h"])
    r = g.groupby(list(por)).agg(
        articulos=("articulo_id", "nunique"), origenes=("origen", "nunique"), real=("real_h", "sum"),
        abs_h=("abs_h", "sum"), err_h=("err_h", "sum"), abs_d=("abs_diario", "sum"), real_d=("real_diario", "sum"),
    )
    r["wape_h_%"] = (r["abs_h"] / r["real"] * 100).round(1)
    r["sesgo_%"] = (r["err_h"] / r["real"] * 100).round(1)
    r["wape_diario_%"] = (r["abs_d"] / r["real_d"] * 100).round(1)
    return r[["articulos", "origenes", "wape_h_%", "sesgo_%", "wape_diario_%"]].reset_index()


def elegir_por_clase(met):
    """Configuración con menor WAPE del horizonte por clase (desempate: menor |sesgo|)."""
    eleccion = {}
    for c, grp in met.groupby("clase"):
        grp = grp.assign(abs_sesgo=grp["sesgo_%"].abs()).sort_values(["wape_h_%", "abs_sesgo"])
        eleccion[c] = grp.iloc[0]["modelo"]
    return eleccion


def evaluacion_separada(bt):
    """
    Error informado SIN sesgo de selección. Elegir el modelo con el mismo
    backtest con el que después se informa el error da un número optimista
    (se elige justamente el que mejor le fue en esos días, en parte por
    suerte). Acá se elige con la PRIMERA mitad de los orígenes y se mide con
    la SEGUNDA, que la elección no vio. Devuelve por clase: modelo elegido
    en la 1.ª mitad, su WAPE en la 1.ª mitad (lo que se habría informado) y en
    la 2.ª, el WAPE del método anterior en la 2.ª, y el modelo que habría
    sido mejor en la 2.ª (para ver si la elección se sostiene).
    """
    origenes = np.sort(bt["origen"].unique())
    if len(origenes) < 4:
        return pd.DataFrame()
    corte = origenes[len(origenes) // 2]
    m1 = metricas(bt[bt["origen"] < corte])
    m2 = metricas(bt[bt["origen"] >= corte])
    ele = elegir_por_clase(m1)
    mejor2 = elegir_por_clase(m2)
    filas = []
    for c, cfg in sorted(ele.items()):
        g1, g2 = m1[m1["clase"] == c].set_index("modelo"), m2[m2["clase"] == c].set_index("modelo")
        if cfg not in g2.index:
            continue
        filas.append({
            "clase": c, "modelo_elegido": cfg,
            "wape_seleccion_%": g1.loc[cfg, "wape_h_%"], "wape_fuera_%": g2.loc[cfg, "wape_h_%"],
            "sesgo_fuera_%": g2.loc[cfg, "sesgo_%"],
            "wape_anterior_fuera_%": g2.loc["promedio_30", "wape_h_%"] if "promedio_30" in g2.index else np.nan,
            "mejor_en_2a_mitad": mejor2.get(c),
            "origenes_seleccion": int(g1.loc[cfg, "origenes"]), "origenes_fuera": int(g2.loc[cfg, "origenes"]),
        })
    r = pd.DataFrame(filas)
    if not r.empty:
        r["mejora_fuera_%"] = ((1 - r["wape_fuera_%"] / r["wape_anterior_fuera_%"]) * 100).round(1)
    return r


def error_por_clase(bt, eleccion):
    """Desvío estándar del error del acumulado del horizonte, por clase, con
    la configuración elegida (insumo del stock de seguridad de HU-015)."""
    filas = []
    for c, cfg in eleccion.items():
        e = bt[(bt["clase"] == c) & (bt["modelo"] == cfg)]
        # Dispersión relativa a Poisson: sqrt(media((real - pron)^2 / pron)).
        # 1 = el error es el de una venta puramente aleatoria; >1 = más
        # variable. La lista de compras usa sigma = dispersion * sqrt(D).
        m = e[e["pron_h"] > 0.05]
        disp = float(np.sqrt(((m["real_h"] - m["pron_h"]) ** 2 / m["pron_h"]).mean())) if len(m) > 1 else np.nan
        filas.append({"clase": c, "modelo": cfg,
                      "sigma_error_h": float((e["real_h"] - e["pron_h"]).std(ddof=1)) if len(e) > 1 else np.nan,
                      "dispersion": disp})
    return pd.DataFrame(filas)


# ----------------------------------------------------------------------------
# 5. Uso desde la app
# ----------------------------------------------------------------------------

def observados(engine, Y, articulos):
    """dias_observados con los movimientos de la base; si algo falla (base
    vieja, datos raros) no corrige nada: mismo resultado que antes."""
    try:
        return dias_observados(Y, articulos, movimientos_stock(engine))
    except Exception as e:  # noqa: BLE001 -- la corrección es una mejora, nunca debe romper el pronóstico
        return np.ones(Y.shape, dtype=bool), {"error": f"{type(e).__name__}: {e}"}


def preparar(engine, hoy=None):
    """
    Serie de demanda hasta AYER (incluido), solo artículos objetivo. El día
    en curso no entra: está incompleto y contarlo como un día entero
    subestima (a las 20 h el pan llevaba 2 bolsas de las ~7 del día).
    """
    hoy = pd.Timestamp(hoy or dt.date.today())
    ayer = hoy - pd.Timedelta(days=1)
    ventas, articulos = cargar_insumos(engine, hasta=ayer.date())
    Y, I = demanda_diaria(ventas, articulos, fecha_fin=ayer)
    if Y.empty:
        return Y, I, articulos
    obj = articulos_objetivo(articulos, Y.columns)
    return Y[obj], I[obj], articulos


def evaluar(engine, hoy=None, dias=60, horizonte=7, corregir_censura=True):
    """
    Corre el backtest con los datos disponibles hasta `hoy` y elige la
    configuración por clase. Si no alcanza la historia para al menos
    MIN_ORIGENES_EVALUACION orígenes, devuelve la configuración por defecto
    para todas las clases y evaluada=False.
    """
    Y, I, articulos = preparar(engine, hoy)
    T = len(Y)
    dias_posibles = T - 28 - horizonte + 1
    if Y.empty or dias_posibles < MIN_ORIGENES_EVALUACION:
        censura = observados(engine, Y, articulos)[1] if (corregir_censura and not Y.empty) else {}
        return {"evaluada": False, "dias_historia": T, "eleccion": {c: CONFIG_POR_DEFECTO for c in "ABC"},
                "metricas": pd.DataFrame(), "sigma": pd.DataFrame(), "censura": censura}
    cats = dict(zip(articulos["articulo_id"], articulos["categoria"]))
    altas = dict(zip(articulos["articulo_id"], articulos["alta"]))
    obs, censura = observados(engine, Y, articulos) if corregir_censura else (None, {})
    bt = backtest(Y, I, cats, dias=min(dias, dias_posibles), horizonte=horizonte, alta_por_art=altas, obs=obs)
    met = metricas(bt)
    eleccion = elegir_por_clase(met)
    for c in "ABC":
        eleccion.setdefault(c, CONFIG_POR_DEFECTO)
    return {"evaluada": True, "dias_historia": T, "eleccion": eleccion, "metricas": met,
            "sigma": error_por_clase(bt, eleccion), "origenes": int(bt["origen"].nunique()),
            "fuera_de_muestra": evaluacion_separada(bt), "censura": censura}


def pronostico_vigente(engine, horizonte=14, hoy=None, eleccion=None, corregir_censura=True):
    """
    Pronóstico diario de los próximos `horizonte` días (desde HOY, con datos
    hasta ayer: el pronóstico de hoy no usa la venta parcial de hoy) para
    cada artículo objetivo, con la configuración elegida para su clase.
    Devuelve DataFrame largo: articulo_id, fecha_objetivo, demanda, modelo, clase.
    """
    hoy = pd.Timestamp(hoy or dt.date.today())
    Y, I, articulos = preparar(engine, hoy)
    columnas = ["articulo_id", "fecha_objetivo", "demanda", "modelo", "clase"]
    if Y.empty:
        return pd.DataFrame(columns=columnas)
    meta = articulos.set_index("articulo_id").loc[list(Y.columns)]
    cats, alta = meta["categoria"].tolist(), meta["alta"].tolist()
    fut = pd.date_range(hoy, periods=horizonte)
    eleccion = eleccion or {c: CONFIG_POR_DEFECTO for c in "ABC"}
    obs = observados(engine, Y, articulos)[0] if corregir_censura else None
    res = {cfg: pronosticar(Y.values, cats, fut, cfg, I.values, alta, obs) for cfg in set(eleccion.values())}
    clase = next(iter(res.values()))[1]["clase"]
    F = np.zeros((horizonte, Y.shape[1]))
    modelo = np.empty(Y.shape[1], dtype=object)
    for j in range(Y.shape[1]):
        Fc, info = res[eleccion.get(clase[j], CONFIG_POR_DEFECTO)]
        F[:, j] = Fc[:, j]
        modelo[j] = info["modelo"][j]
    largo = pd.DataFrame({
        "articulo_id": np.repeat(Y.columns.values, horizonte),
        "fecha_objetivo": np.tile(fut.date, Y.shape[1]),
        "demanda": F.T.reshape(-1),
        "modelo": np.repeat(modelo, horizonte),
        "clase": np.repeat(clase, horizonte),
    })
    return largo[columnas]


# ----------------------------------------------------------------------------
# 6. Seguimiento: ¿se cumplió el pronóstico? (validación)
# ----------------------------------------------------------------------------

def reconstruir(engine, hoy=None, dias=28, eleccion=None, corregir_censura=True):
    """
    Pronóstico que el modelo ACTUAL habría hecho para cada uno de los últimos
    `dias` días, usando solo las ventas hasta el día anterior (origen móvil,
    horizonte 1 día, sin fuga de datos), contra lo vendido ese día.
    Limitación: usa el modelo de hoy, que pudo ajustarse mirando esos días;
    los pronósticos GUARDADOS (pronostico_snapshot) son la medida estricta.
    Devuelve: fecha, articulo_id, clase, pronostico, real.
    """
    Y, I, articulos = preparar(engine, hoy)
    cols = ["fecha", "articulo_id", "clase", "pronostico", "real"]
    if Y.empty or len(Y) < 2:
        return pd.DataFrame(columns=cols)
    meta = articulos.set_index("articulo_id").loc[list(Y.columns)]
    cats, alta = meta["categoria"].tolist(), meta["alta"].tolist()
    eleccion = eleccion or {c: CONFIG_POR_DEFECTO for c in "ABC"}
    Yv, Iv, fechas = Y.values, I.values, Y.index
    obs = observados(engine, Y, articulos)[0] if corregir_censura else np.ones(Yv.shape, dtype=bool)
    filas = []
    for t in range(max(1, len(Y) - dias), len(Y)):
        fut = fechas[t:t + 1]
        res = {cfg: pronosticar(Yv[:t], cats, fut, cfg, Iv[:t], alta, obs[:t]) for cfg in set(eleccion.values())}
        clase = next(iter(res.values()))[1]["clase"]
        F = np.array([res[eleccion.get(c, CONFIG_POR_DEFECTO)][0][0, j] for j, c in enumerate(clase)])
        inicio = _inicio(Yv[:t], fechas[:t], alta)
        vivo = (inicio < t) & ((Yv[:t] > 0).any(axis=0) | (F > 0))
        filas.append(pd.DataFrame({"fecha": fechas[t].date(), "articulo_id": Y.columns, "clase": clase,
                                   "pronostico": F, "real": Yv[t]})[vivo])
    return pd.concat(filas, ignore_index=True) if filas else pd.DataFrame(columns=cols)


def resumen_seguimiento(df):
    """
    Por clase: lo vendido vs lo pronosticado en el período, el sesgo y el
    error SEMANAL (WAPE de la suma por artículo y semana, contada hacia atrás
    desde el último día). El error diario por artículo es naturalmente muy
    alto con ventas chicas (un día 0, otro 3); para comprar importa la semana.
    """
    cols = ["clase", "dias", "articulos", "vendido", "pronosticado", "sesgo_%", "error_semanal_%"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    d = df.copy()
    d["fecha"] = pd.to_datetime(d["fecha"])
    d["semana"] = ((d["fecha"].max() - d["fecha"]).dt.days // 7)
    sem = d.groupby(["clase", "articulo_id", "semana"])[["real", "pronostico"]].sum().reset_index()
    sem["abs_err"] = (sem["real"] - sem["pronostico"]).abs()
    r = d.groupby("clase").agg(dias=("fecha", "nunique"), articulos=("articulo_id", "nunique"),
                               vendido=("real", "sum"), pronosticado=("pronostico", "sum"))
    r["sesgo_%"] = ((r["pronosticado"] - r["vendido"]) / r["vendido"].where(r["vendido"] > 0) * 100).round(1)
    e = sem.groupby("clase")[["abs_err", "real"]].sum()
    r["error_semanal_%"] = (e["abs_err"] / e["real"].where(e["real"] > 0) * 100).round(1)
    return r.reset_index()[cols]
