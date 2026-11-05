"""
Lista de compras (Sprint 2, HU-015). Reemplaza la "Sugerencia de
Reposición" (promedio de 30 días x 15 días de cobertura, igual para todo).

Usa el pronóstico de demanda por artículo (pronostico.py) y separa dos
formas de comprar, según cómo compra el comerciante en la realidad:

1. COMPRA DIARIA (articulos.reposicion_dias = 1; ej.: el pan, que el comerciante compra
   todos los días en la panadería y lo que sobra lo regala):
   problema del vendedor de diarios (newsvendor). Cantidad óptima Q* tal que
   P(demanda <= Q*) = cociente crítico = (precio - costo) / (precio - recupero),
   con recupero = valor_recupero_pct x costo (hoy 0: lo que sobra se regala).
   Precio y costo con el MISMO tratamiento de IVA: precio cobrado (con IVA)
   y costo de compra con IVA, que es lo que paga un monotributista. Para un
   responsable inscripto (los dos sin IVA) el cociente da exactamente igual,
   porque el IVA se cancela. Con precio $2.000 y costo $1.000 + 21 % -> cociente
   (2.000 - 1.210) / 2.000 = 0,395 -> Q* = percentil 40 de la demanda (antes
   mezclaba precio con IVA y costo sin IVA: daba 0,50 y sobraba pan). Si el artículo tiene nivel_servicio propio, se usa ese en lugar
   del cociente (ej.: el comerciante prefiere no quedarse sin pan aunque sobre algo).
   Muestra el sobrante esperado y la probabilidad de agotarse.

2. VIAJE AL MAYORISTA (el resto): política de revisión periódica (R, S).
   R = días hasta el próximo viaje (parámetro frecuencia_viaje_dias, o
   reposicion_dias del artículo), L ~ 0 (el comerciante retira en el momento).
   S = D(R) + z x sigma; sigma = dispersión x raíz(D(R)) (Poisson corregido
   por la dispersión medida en la evaluación de cada clase; 1 si todavía no
   hay evaluación). z según el nivel de servicio de la clase ABC (A 95 %,
   B 90 %, C 80 %, editables en Configuración) o el del artículo.
   Comprar = S - stock, redondeado hacia arriba al bulto (articulo_alias.factor
   cuando el proveedor factura por caja). Si el producto vence antes del
   próximo viaje (vida_util_dias < R), se cubre solo su vida útil.
   Se agrupa por el último proveedor al que se le compró, con costo estimado.

Excluye combos (se compran sus componentes) y sueltos fraccionados (se
compra la bolsa), igual que el pronóstico. Stock negativo se toma como 0 y
se marca para revisar (indica compras sin cargar).
"""
from __future__ import annotations

import math
from statistics import NormalDist

import pandas as pd
from sqlalchemy import text

N = NormalDist()


def datos_articulos(engine):
    """Una consulta: stock, costo, precio, parámetros de reposición, bulto y último proveedor.
    vida_util_efectiva (tanda 18): la vida útil del artículo o, si no tiene, la de su categoría."""
    meta = pd.read_sql(text("""
        SELECT a.id AS articulo_id, a.nombre, a.categoria, a.unidad_medida,
               GREATEST(a.stock_actual, 0) AS stock, a.stock_actual,
               a.costo_compra_actual AS costo, va.costo_venta_con_iva AS precio,
               a.costo_compra_actual * (1 + COALESCE(a.iva, 0.21)) AS costo_c_iva,
               a.reposicion_dias, a.vida_util_dias, a.nivel_servicio, a.articulo_fraccion_de,
               -- lo comprado hoy (día argentino): en lo que vence en el día es lo único del
               -- stock que todavía se vende hoy (ver calcular, compra diaria)
               (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS dia_stock,
               COALESCE((SELECT SUM(d.cantidad) FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
                          WHERE d.articulo_id = a.id AND d.cantidad > 0
                            AND f.fecha = (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date), 0)
                 AS comprado_hoy,
               -- tamaño "efectivo" de cada venta, E[s²]/E[s] (últimos 90 días): para lo que se vende
               -- por kg, la varianza de la venta acumulada es media x este valor (Poisson compuesta)
               (SELECT SUM(v.cantidad * v.cantidad) / NULLIF(SUM(v.cantidad), 0)
                  FROM ventas v WHERE v.articulo_id = a.id AND v.cantidad > 0
                   AND v.fecha >= (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - 90) AS tam_venta,
               COALESCE((SELECT MAX(al.factor) FROM articulo_alias al
                         WHERE al.articulo_id = a.id AND al.factor > 1), 1) AS bulto,
               COALESCE(
                 (SELECT trim(p.nombre) FROM detalle_factura d
                    JOIN facturas f ON f.id = d.factura_id
                    JOIN proveedores p ON p.id = f.proveedor_id
                   WHERE d.articulo_id = a.id
                   ORDER BY f.fecha DESC, f.id DESC LIMIT 1),
                 -- sin factura todavía (ej. consignación): el proveedor asignado
                 (SELECT trim(p.nombre) FROM articulo_proveedor_precio app
                    JOIN proveedores p ON p.id = app.proveedor_id
                   WHERE app.articulo_id = a.id ORDER BY app.actualizado_en DESC LIMIT 1)
               ) AS proveedor,
               -- tanda 14: todos sus proveedores son en consignación (se paga al vender)
               COALESCE((SELECT bool_and(COALESCE((to_jsonb(p) ->> 'consignacion')::boolean, FALSE))
                           FROM articulo_proveedor_precio app JOIN proveedores p ON p.id = app.proveedor_id
                          WHERE app.articulo_id = a.id), FALSE) AS consignacion
        FROM articulos a
        JOIN v_articulos va ON va.id = a.id
        WHERE a.activo
    """), engine)
    meta["vida_util_efectiva"] = pd.to_numeric(meta["vida_util_dias"], errors="coerce")
    with engine.connect() as conn:
        hay = conn.execute(text("SELECT to_regclass('public.vida_util_categoria') IS NOT NULL")).scalar()
        por_cat = dict(conn.execute(text("SELECT categoria, dias FROM vida_util_categoria")).all()) if hay else {}
    if por_cat:
        meta["vida_util_efectiva"] = meta["vida_util_efectiva"].fillna(meta["categoria"].map(por_cat))
    return meta


def _f(x, defecto=0.0):
    """Número o el defecto si es None/NaN (en pandas NaN es 'verdadero', no sirve `x or 0`)."""
    return float(x) if pd.notna(x) else defecto


def _vida(a):
    """Días disponibles para la venta: los del artículo o los de su categoría (tanda 18)."""
    v = a.get("vida_util_efectiva") if hasattr(a, "get") else None
    return v if v is not None and pd.notna(v) else a["vida_util_dias"]


def _es_dia(dia, otro):
    """True si las dos fechas son el mismo día (date, Timestamp o texto); False si falta una."""
    if dia is None or otro is None or pd.isna(dia) or pd.isna(otro):
        return False
    return pd.Timestamp(dia).date() == pd.Timestamp(otro).date()


def _txt(x, defecto):
    return str(x) if pd.notna(x) and str(x).strip() else defecto


def _redondear(cantidad, unidad, bulto):
    if cantidad <= 0:
        return 0.0
    if bulto and bulto > 1:
        return float(math.ceil(cantidad / bulto - 1e-9) * bulto)
    if unidad == "kg":
        return math.ceil(cantidad * 10 - 1e-9) / 10
    return float(math.ceil(cantidad - 1e-9))


def _tam(a):
    """E[s²]/E[s] de las ventas del artículo si se vende por kg (1 por unidad:
    ahí la venta acumulada es Poisson). Sin historia de ventas: 1."""
    if a.get("unidad_medida") != "kg":
        return 1.0
    t = _f(a.get("tam_venta"), 1.0)
    return t if t > 0 else 1.0


def _desvio_diario(Y, aid, mu, minimo_dias=7, ventana=28, tam=1.0):
    """Desvío de la demanda diaria del artículo: empírico si hay al menos
    `minimo_dias` días desde que empezó a vender (últimos 28), si no Poisson
    (compuesta por el tamaño de venta `tam` para lo que se vende por kg)."""
    piso = math.sqrt(max(mu * tam, 1e-9))
    if Y is not None and aid in Y.columns:
        s = Y[aid]
        s = s[s.index >= s[s > 0].index.min()] if (s > 0).any() else s.iloc[0:0]
        s = s.iloc[-ventana:]
        if len(s) >= minimo_dias:
            return max(float(s.std(ddof=1)), piso)
    return piso


def calcular(pron, meta, params, dispersion=None, Y=None, dia_compra_diaria=None):
    """
    pron: DataFrame largo de pronostico.pronostico_vigente (desde hoy).
    meta: datos_articulos(). params: db.obtener_parametros().
    dispersion: dict clase -> dispersión (de la evaluación); 1 si falta.
    Y: demanda diaria histórica (para el desvío de los de compra diaria).
    dia_compra_diaria: fecha para la que se arma la compra diaria (hoy o mañana).
    Devuelve (diaria, viaje): dos DataFrames listos para mostrar.
    """
    dispersion = dispersion or {}
    R_viaje = int(params.get("frecuencia_viaje_dias", 7))
    recupero = float(params.get("valor_recupero_pct", 0.0))
    nivel_clase = {c: float(params.get(f"nivel_servicio_{c}", d)) for c, d in zip("ABC", (0.95, 0.90, 0.80))}
    fechas = sorted(pron["fecha_objetivo"].unique())
    dia_compra_diaria = dia_compra_diaria or fechas[0]
    pv = pron.pivot_table(index="articulo_id", columns="fecha_objetivo", values="demanda", aggfunc="sum")
    clase = pron.groupby("articulo_id")["clase"].first()
    # tanda 20: clase heredada de un combo (heredar_clase_combos), para avisarlo
    clase_por = pron.groupby("articulo_id")["clase_por"].first() if "clase_por" in pron.columns else pd.Series(dtype=object)
    m = meta.set_index("articulo_id")

    diaria, viaje = [], []
    for aid in pv.index.intersection(m.index):
        a = m.loc[aid]
        cl = clase.get(aid, "C")
        serie = pv.loc[aid]
        R = int(a["reposicion_dias"]) if pd.notna(a["reposicion_dias"]) else R_viaje
        nivel_art = float(a["nivel_servicio"]) if pd.notna(a["nivel_servicio"]) else None

        if R == 1:
            mu = float(serie.get(dia_compra_diaria, 0.0))
            if mu <= 0.05:
                continue
            sd = _desvio_diario(Y, aid, mu, tam=_tam(a))
            # mismo criterio de IVA en precio y costo (ver encabezado)
            precio, costo = _f(a["precio"]), _f(a["costo_c_iva"])
            s_rec = recupero * costo
            critico = (precio - costo) / (precio - s_rec) if precio > s_rec else 0.5
            nivel = nivel_art if nivel_art is not None else min(max(critico, 0.01), 0.99)
            q = max(0.0, N.inv_cdf(nivel) * sd + mu)
            q = _redondear(q - 0.5, a["unidad_medida"], a["bulto"]) if a["unidad_medida"] != "kg" else _redondear(q, "kg", 1)
            # Lo que hay en stock cuenta como disponible si todavía se puede
            # vender (vida útil > 1 día o no perecedero). Lo que se regala o se
            # tira se registra como "Sobrante del día" en Registrar Venta, y eso
            # lo saca del stock: el stock queda igual a lo que se guarda para vender.
            # Con vida útil de 1 día solo se vende el día que se compró: para la
            # compra de hoy cuenta lo comprado hoy que sigue en stock (lo de ayer
            # se vende primero), para la de mañana nada.
            vida = _vida(a)
            aprovecha = pd.isna(vida) or int(vida) > 1
            stock = max(_f(a["stock"]), 0.0)
            if aprovecha:
                usable = stock
            elif _es_dia(a.get("dia_stock"), dia_compra_diaria):
                usable = min(stock, _f(a.get("comprado_hoy")))
            else:
                usable = 0.0
            comprar = max(0.0, q - usable)
            # sobrante esperado y probabilidad de agotarse (normal)
            zq = (q - mu) / sd
            sobrante = sd * (zq * N.cdf(zq) + N.pdf(zq))
            diaria.append({
                "articulo_id": aid, "Artículo": a["nombre"], "Clase": cl,
                "Venta esperada": mu, "Tener para el día": q, "Ya tenés": usable,
                "Comprar": comprar, "Sobrante esperado": sobrante,
                "Prob. de agotarse": 1 - N.cdf(zq),
                "Criterio": "nivel propio" if nivel_art is not None else f"óptimo económico ({critico:.0%})",
                "Costo estimado": comprar * _f(a["costo_c_iva"]), "Proveedor": _txt(a["proveedor"], "Sin proveedor"),
            })
            continue

        vida_a = _vida(a)
        vence_antes = pd.notna(vida_a) and int(vida_a) < R
        horizonte = int(vida_a) if vence_antes else R
        D = float(serie.iloc[:horizonte].sum())
        if D <= 0.05:
            continue
        nivel = nivel_art if nivel_art is not None else nivel_clase.get(cl, 0.8)
        sigma = float(dispersion.get(cl) or 1.0) * math.sqrt(D * _tam(a))
        S = D + N.inv_cdf(nivel) * sigma
        necesidad = S - float(a["stock"])
        comprar = _redondear(necesidad, a["unidad_medida"], a["bulto"])
        if comprar <= 0:
            continue
        cobertura = (float(a["stock"]) / (D / horizonte)) if D > 0 else None
        consignacion = bool(a.get("consignacion", False)) if hasattr(a, "get") else False
        viaje.append({
            "articulo_id": aid, "Artículo": a["nombre"], "Clase": cl,
            "Venta esperada": D, "Stock": float(a["stock_actual"]), "Comprar": comprar,
            "Bulto": float(a["bulto"]) if _f(a["bulto"], 1) > 1 else float("nan"),
            "Alcanza (días)": cobertura,
            # con IVA: es la plata que hay que llevar al mayorista (el costo de
            # compra se guarda sin IVA, como en el resto del sistema)
            # en consignación no se paga al reponer: no suma a la plata del viaje
            "Costo estimado": 0.0 if consignacion else comprar * _f(a["costo_c_iva"]),
            "Proveedor": _txt(a["proveedor"], "Sin proveedor"),
            "Aviso": " · ".join(x for x in (
                "en consignación: pedírselo al proveedor, se paga al vender" if consignacion else "",
                "stock negativo: revisar compras sin cargar" if float(a["stock_actual"]) < 0 else "",
                f"vence en {int(vida_a)} días: cubre solo eso" if vence_antes else "",
                "sin stock" if float(a["stock_actual"]) <= 0 else "",
                f"clase {cl} por {clase_por.get(aid)}" if isinstance(clase_por.get(aid), str) else "",
            ) if x),
        })

    cols_d = ["articulo_id", "Artículo", "Clase", "Venta esperada", "Tener para el día", "Ya tenés", "Comprar",
              "Sobrante esperado", "Prob. de agotarse", "Criterio", "Costo estimado", "Proveedor"]
    cols_v = ["articulo_id", "Proveedor", "Artículo", "Clase", "Comprar", "Stock", "Venta esperada",
              "Alcanza (días)", "Costo estimado", "Bulto", "Aviso"]
    d = pd.DataFrame(diaria, columns=cols_d).sort_values("Artículo")
    v = pd.DataFrame(viaje, columns=cols_v)
    if not v.empty:
        v = v.assign(_c=v["Clase"].map({"A": 0, "B": 1, "C": 2}), _a=v["Alcanza (días)"].fillna(0)) \
             .sort_values(["Proveedor", "_c", "_a"]).drop(columns=["_c", "_a"])
    return d.reset_index(drop=True), v.reset_index(drop=True)


def _num(x, unidad="unidad"):
    return f"{x:.1f}".replace(".", ",") if unidad == "kg" or x != int(x) else f"{int(x)}"


def texto_whatsapp(viaje, diaria=None, titulo="Lista de compras"):
    """Texto plano para copiar y mandar por WhatsApp, agrupado por proveedor."""
    lineas = [f"*{titulo}*"]
    if diaria is not None and not diaria.empty and (diaria["Comprar"] > 0).any():
        lineas.append("\n_Compra del día_")
        for _, r in diaria[diaria["Comprar"] > 0].iterrows():
            lineas.append(f"• {_num(r['Comprar'])} x {r['Artículo']}")
    for prov, g in viaje.groupby("Proveedor", sort=True):
        lineas.append(f"\n_{prov}_")
        for _, r in g.iterrows():
            extra = f" ({int(r['Comprar'] // r['Bulto'])} bulto/s)" if pd.notna(r.get("Bulto")) and r.get("Bulto") else ""
            lineas.append(f"• {_num(r['Comprar'])} x {r['Artículo']}{extra}")
    total = float(viaje["Costo estimado"].sum()) + (float(diaria["Costo estimado"].sum()) if diaria is not None else 0)
    lineas.append(f"\nCosto estimado (con IVA): ${total:,.0f}".replace(",", "."))
    return "\n".join(lineas)


def candidatos_compra_diaria(engine, dias=14, minimo_compras=3):
    """Artículos que se compran seguido (>= minimo_compras facturas en los últimos
    `dias` días) y todavía no están marcados como compra diaria: sugerencias
    para que el comerciante confirme. También los de categorías perecederas típicas."""
    return pd.read_sql(text("""
        WITH compras AS (
            SELECT d.articulo_id, COUNT(DISTINCT f.fecha) AS dias_con_compra
            FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
            WHERE f.fecha >= (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - CAST(:dias AS INTEGER)
            GROUP BY d.articulo_id
        )
        SELECT a.id AS articulo_id, a.nombre, a.categoria, COALESCE(c.dias_con_compra, 0) AS dias_con_compra
        FROM articulos a LEFT JOIN compras c ON c.articulo_id = a.id
        WHERE a.activo AND a.reposicion_dias IS NULL AND a.categoria <> 'Promocion'
          AND (COALESCE(c.dias_con_compra, 0) >= :minimo
               OR a.nombre ILIKE 'pan %' OR a.nombre ILIKE 'factura%' OR a.nombre ILIKE 'medialuna%')
        ORDER BY dias_con_compra DESC, a.nombre
    """), engine, params={"dias": dias, "minimo": minimo_compras})


# ----------------------------------------------------------------------------
# ALERTAS DE QUIEBRE (Sprint 2, HU-016) -- tarjeta del Dashboard
# ----------------------------------------------------------------------------
UMBRAL_ADELANTAR_VIAJE = 3   # artículos clase A en rojo para sugerir adelantar el viaje


def ultimo_viaje(engine):
    """Fecha de la última compra de mercadería que no es de compra diaria
    (mayorista o distribuidor). Aproxima el último viaje hasta que exista el
    registro de viajes (Sprint 3). None si no hay compras.
    Con el registro de viajes (tanda 17): el más reciente entre el último
    viaje planificado registrado y la última compra."""
    with engine.connect() as c:
        registrado = None
        if c.execute(text("SELECT to_regclass('public.viajes_compra') IS NOT NULL")).scalar():
            registrado = c.execute(text("SELECT MAX(fecha) FROM viajes_compra WHERE tipo = 'planificado'")).scalar()
    por_factura = pd.read_sql(text("""
        SELECT MAX(f.fecha) AS fecha
        FROM facturas f JOIN detalle_factura d ON d.factura_id = f.id
        JOIN articulos a ON a.id = d.articulo_id
        WHERE (to_jsonb(a) ->> 'reposicion_dias') IS NULL
           OR (to_jsonb(a) ->> 'reposicion_dias')::int <> 1
    """), engine)["fecha"].iloc[0]
    fechas = [pd.Timestamp(f).date() for f in (registrado, por_factura) if f is not None and pd.notna(f)]
    return max(fechas) if fechas else None


def proximo_viaje_estimado(ultimo, hoy, frecuencia):
    """Último viaje + frecuencia; si ya pasó (o no hay registro), hoy."""
    if ultimo is None or pd.isna(ultimo):
        return hoy
    prox = pd.Timestamp(ultimo).date() + pd.Timedelta(days=int(frecuencia))
    return max(prox, hoy)


def _gamma_q(a, x):
    """Q(a, x): gamma incompleta superior regularizada (Numerical Recipes,
    serie para x < a + 1, fracción continua si no). Sin scipy."""
    if x <= 0:
        return 1.0
    ln = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        ap, suma = a, 1.0 / a
        d = suma
        for _ in range(500):
            ap += 1
            d *= x / ap
            suma += d
            if abs(d) < abs(suma) * 1e-12:
                break
        return max(0.0, 1.0 - suma * math.exp(ln))
    b = x + 1 - a
    c, d = 1e300, 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        h *= d * c
        if abs(d * c - 1) < 1e-12:
            break
    return min(1.0, math.exp(ln) * h)


def _prob_agotar(stock, media, tam=1.0, continua=False, dispersion=1.0):
    """P(venta > stock): probabilidad de quedarse sin stock.

    Por unidad: venta ~ binomial negativa de media `media` y varianza
    dispersion² x media (tanda 18). Con dispersion = 1 es exactamente la
    Poisson de antes. La dispersión es la misma que usa el stock de seguridad
    de la lista de compras (la mide la evaluación del pronóstico; 1 mientras no
    hay evaluación). Motivo: en la base simulada (40 trayectorias), la Poisson
    subestimaba a la mitad los riesgos bajos y medios (decía 1,7 % en promedio
    y pasaba 3,6 %); con la dispersión medida el error de calibración bajó de
    1,9 a 0,6-1,0 puntos (claude/robustez-calibracion-metricas-24oct.md).

    Por kg (`continua`): la venta es una suma de ventas de distinto tamaño
    (Poisson compuesta), con media `media` y varianza media x tam x dispersion²
    (tam = E[s²]/E[s]); se aproxima con una Gamma de esa media y varianza.
    Con stock 0 por kg devuelve 1 (el cajón está vacío: no es una predicción)."""
    if media <= 0:
        return 0.0
    d2 = max(float(dispersion or 1.0), 1.0) ** 2
    if continua:
        if stock <= 0:
            return 1.0
        escala = max(tam, 1e-9) * d2
        return _gamma_q(media / escala, stock / escala)
    n = int(math.floor(max(stock, 0.0) + 1e-9))
    if d2 <= 1.0 + 1e-9:
        termino = acum = math.exp(-media)
        for k in range(1, n + 1):
            termino *= media / k
            acum += termino
        return max(0.0, 1.0 - acum)
    # binomial negativa: p = media / varianza, r = media x p / (1 - p)
    p = 1.0 / d2
    r = media * p / (1.0 - p)
    termino = acum = math.exp(r * math.log(p))       # P(0) = p^r
    for k in range(1, n + 1):
        termino *= (k - 1 + r) / k * (1.0 - p)
        acum += termino
    return min(1.0, max(0.0, 1.0 - acum))


def _cdf_venta(x, media, tam=1.0, continua=False, dispersion=1.0):
    """P(venta <= x), con la misma distribución que _prob_agotar."""
    if media <= 0:
        return 1.0
    if continua:
        if x <= 0:
            return 0.0
        escala = max(tam, 1e-9) * max(float(dispersion or 1.0), 1.0) ** 2
        return 1.0 - _gamma_q(media / escala, x / escala)
    return 1.0 - _prob_agotar(x, media, tam, False, dispersion)


def _sobrante_esperado(c, media, tam=1.0, continua=False, dispersion=1.0):
    """E[max(c - venta, 0)]: lo que se espera que quede sin vender de c unidades."""
    if c <= 0:
        return 0.0
    if media <= 0:
        return float(c)
    if continua:
        pasos = 60
        xs = [c * i / pasos for i in range(pasos + 1)]
        fs = [_cdf_venta(x, media, tam, True, dispersion) for x in xs]
        return sum((fs[i] + fs[i + 1]) / 2 * (c / pasos) for i in range(pasos))
    # discreto: E[(c - D)+] = suma_{j=0}^{c-1} P(D <= j) (+ la parte fraccionaria de c)
    n = int(math.floor(c + 1e-9))
    total = sum(_cdf_venta(j, media, tam, False, dispersion) for j in range(n))
    if c - n > 1e-9:
        total += (c - n) * _cdf_venta(n, media, tam, False, dispersion)
    return total


def riesgo_vencimiento(lotes, pron, dispersion=None, tam_por_art=None):
    """
    Tanda 18. Para cada lote con fecha (db.lotes_vencimiento): cuánto se
    espera vender antes de que venza, la probabilidad de que quede algo sin
    vender y las unidades que se espera tirar. Es la misma distribución de la
    venta que usan las alertas de quiebre (binomial negativa con la
    dispersión de la clase; Gamma para lo que va por kg), mirada del otro
    lado: en vez de faltar, sobra.
    Se supone que se vende primero lo más viejo: antes de este lote se tienen
    que vender los 'anteriores' que quedan.
    pron: pronóstico vigente (pronostico_vigente, horizonte de 14 días). Más
    allá del horizonte se extiende con el promedio diario de esos 14 días.
    Devuelve el DataFrame de lotes con: venta_hasta_vencer, prob_sobra,
    vencerian (unidades), vencerian_costo ($).
    """
    if lotes is None or lotes.empty:
        return lotes
    dispersion = dispersion or {}
    tam_por_art = tam_por_art or {}
    pv = pron.pivot_table(index="articulo_id", columns="fecha_objetivo", values="demanda", aggfunc="sum") \
        if pron is not None and not pron.empty else pd.DataFrame()
    pv = pv.reindex(columns=sorted(pv.columns)) if not pv.empty else pv
    clase = pron.groupby("articulo_id")["clase"].first() if pron is not None and not pron.empty else pd.Series(dtype=object)
    out = []
    for l in lotes.itertuples():
        d = l.dias_para_vencer
        fila = {"venta_hasta_vencer": float("nan"), "prob_sobra": float("nan"), "vencerian": float("nan")}
        if pd.isna(d):
            out.append(fila)
            continue
        if d < 0:
            # según la fecha ya venció: lo que queda se tira (o la fecha está mal cargada)
            fila.update(venta_hasta_vencer=0.0, prob_sobra=1.0, vencerian=float(l.quedan))
            out.append(fila)
            continue
        k = int(d)
        serie = pv.loc[l.articulo_id].fillna(0.0).values if (not pv.empty and l.articulo_id in pv.index) else []
        media = float(sum(serie[:k]))
        if k > len(serie) and len(serie):
            media += (k - len(serie)) * float(sum(serie)) / len(serie)
        kg = l.unidad_medida == "kg"
        tam = float(tam_por_art.get(l.articulo_id, 1.0)) if kg else 1.0
        disp = float(dispersion.get(clase.get(l.articulo_id, "C")) or 1.0)
        c0, c1 = float(l.anteriores), float(l.anteriores + l.quedan)
        venc = (_sobrante_esperado(c1, media, tam, kg, disp) - _sobrante_esperado(c0, media, tam, kg, disp))
        # quede algo de este lote = la venta no alcanza a cubrir hasta su última unidad
        prob = (_cdf_venta(c1 - 1e-6, media, tam, True, disp) if kg
                else 1.0 - _prob_agotar(math.ceil(c1 - 1e-9) - 1, media, tam, False, disp))
        fila.update(venta_hasta_vencer=media, prob_sobra=min(max(prob, 0.0), 1.0),
                    vencerian=min(max(venc, 0.0), float(l.quedan)))
        out.append(fila)
    r = lotes.reset_index(drop=True).join(pd.DataFrame(out))
    r["vencerian_costo"] = r["vencerian"] * r["costo"].astype(float)
    return r


PROB_ALERTA = 0.5   # se marca cuando es más probable agotarse que no


def alertas_quiebre(pron, meta, params, hoy, proximo_viaje, dispersion=None):
    """
    Semáforo de quiebre de stock por artículo (no de compra diaria):
      - prob_hasta_viaje: probabilidad de quedarse sin stock antes del próximo
        viaje (venta acumulada pronosticada ~ binomial negativa con la
        dispersión de la clase, la misma de la lista de compras; Poisson si
        todavía no hay evaluación).
      - estado: 'rojo' si esa probabilidad es >= 50 %; 'amarillo' si llega al
        viaje pero es probable agotarse antes del siguiente (hay que comprarlo
        en este viaje); 'verde' si alcanza.
      - Se queda sin stock: primer día en que agotarse pasa a ser más probable
        que no.
      - en_riesgo_$: venta esperada que se perdería hasta el viaje, a precio de
        venta (E[max(venta - stock, 0)], aproximado).
    Los artículos con stock negativo no se semaforizan: el stock no es
    confiable y se resuelven en 'Pendientes de ajuste'.
    Devuelve (tabla, resumen).
    """
    R = int(params.get("frecuencia_viaje_dias", 7))
    dias_viaje = max(0, (pd.Timestamp(proximo_viaje) - pd.Timestamp(hoy)).days)
    fechas = sorted(pron["fecha_objetivo"].unique())
    pv = pron.pivot_table(index="articulo_id", columns="fecha_objetivo", values="demanda", aggfunc="sum")
    pv = pv.reindex(columns=fechas).fillna(0.0)
    clase = pron.groupby("articulo_id")["clase"].first()
    m = meta.set_index("articulo_id")
    filas, negativos = [], 0
    for aid in pv.index.intersection(m.index):
        a = m.loc[aid]
        if pd.notna(a["reposicion_dias"]) and int(a["reposicion_dias"]) == 1:
            continue  # compra diaria: se repone todos los días
        serie = pv.loc[aid].values
        if serie[: dias_viaje + R].sum() <= 0:
            continue
        stock = _f(a["stock_actual"])
        if stock < 0:
            negativos += 1
            continue
        acum = serie.cumsum()
        media_viaje = float(serie[:dias_viaje].sum())
        kg = a["unidad_medida"] == "kg"
        tam = _tam(a)
        disp = float((dispersion or {}).get(clase.get(aid, "C")) or 1.0)
        p_viaje = _prob_agotar(stock, media_viaje, tam, kg, disp)
        p_sig = _prob_agotar(stock, float(serie[: dias_viaje + R].sum()), tam, kg, disp)
        estado = "rojo" if p_viaje >= PROB_ALERTA else "amarillo" if p_sig >= PROB_ALERTA else "verde"
        dq = next((d for d in range(len(acum)) if _prob_agotar(stock, acum[d], tam, kg, disp) >= PROB_ALERTA), None)
        if stock <= 0:
            dq = 0
        riesgo_u = max(0.0, media_viaje - stock) if estado == "rojo" else 0.0
        filas.append({
            "articulo_id": aid, "Artículo": a["nombre"], "Clase": clase.get(aid, "C"),
            "Stock": stock, "Venta por día": float(serie[:7].mean()),
            "Prob. de agotarse antes del viaje": p_viaje,
            "dias_quiebre": dq,
            "Se queda sin stock": (pd.Timestamp(hoy) + pd.Timedelta(days=dq)).date() if dq is not None else None,
            "estado": estado, "en_riesgo_u": riesgo_u, "en_riesgo_$": riesgo_u * _f(a["precio"]),
            "Proveedor": _txt(a["proveedor"], "Sin proveedor"),
        })
    t = pd.DataFrame(filas)
    base = {"rojo": 0, "rojo_A": 0, "amarillo": 0, "sin_stock": 0, "riesgo_$": 0.0,
            "adelantar": False, "dias_viaje": dias_viaje, "stock_negativo": negativos}
    if t.empty:
        return t, base
    t["_o"] = t["estado"].map({"rojo": 0, "amarillo": 1, "verde": 2})
    t["_c"] = t["Clase"].map({"A": 0, "B": 1, "C": 2})
    t = t.sort_values(["_o", "_c", "dias_quiebre", "en_riesgo_$"], ascending=[True, True, True, False],
                      na_position="last").drop(columns=["_o", "_c"]).reset_index(drop=True)
    rojo = t["estado"] == "rojo"
    rojo_A = int((rojo & (t["Clase"] == "A")).sum())
    base.update({
        "rojo": int(rojo.sum()), "rojo_A": rojo_A,
        "amarillo": int((t["estado"] == "amarillo").sum()),
        "sin_stock": int((rojo & (t["Stock"] <= 0)).sum()),
        "riesgo_$": float(t.loc[rojo, "en_riesgo_$"].sum()),
        "adelantar": rojo_A >= UMBRAL_ADELANTAR_VIAJE and dias_viaje > 1,
    })
    return t, base


# ----------------------------------------------------------------------------
# Lista guardada contra lo que se compró (tanda 17, protocolo de validación)
# ----------------------------------------------------------------------------
# La lista con la que el comerciante se fue al mayorista se compara con las facturas
# de los días que la lista cubre: desde ese día hasta el día anterior al
# próximo viaje (frecuencia_viaje_dias; 7 -> ese día y los 6 siguientes), o
# hasta el día anterior a la lista siguiente si se guardó otra antes. Así
# entran también los distribuidores que entregan en el local durante la
# semana, que la lista incluye (agrupa por proveedor). Funciones puras sobre
# DataFrames: las usan la app (db.comparar_lista_compra) y validacion.py
# (que lee el respaldo, sin conectarse a la base).
VENTANA_LISTA_DIAS = 6   # días después del de la lista (con frecuencia 7)

ESTADOS_LISTA = ["Comprado igual", "Comprado, otra cantidad", "No se compró",
                 "Sacado de la lista", "Sacado, pero se compró", "Agregado (no estaba en la lista)"]


def ventanas_listas(listas, dias=VENTANA_LISTA_DIAS):
    """listas: DataFrame con id y fecha. Devuelve id, fecha, desde, hasta (fechas de factura que cuentan).
    dias: días después del de la lista que todavía cuentan (frecuencia de viaje - 1)."""
    if listas is None or listas.empty:
        return pd.DataFrame(columns=["id", "fecha", "desde", "hasta"])
    v = listas[["id", "fecha"]].copy()
    v["fecha"] = pd.to_datetime(v["fecha"]).dt.date
    v = v.sort_values("fecha").reset_index(drop=True)
    tope = pd.Timedelta(days=int(dias)).to_pytimedelta()
    un_dia = pd.Timedelta(days=1).to_pytimedelta()
    siguiente = list(v["fecha"].iloc[1:]) + [None]
    v["desde"] = v["fecha"]
    v["hasta"] = [f + tope if s is None else min(f + tope, s - un_dia) for f, s in zip(v["fecha"], siguiente)]
    return v


def _igual(comprado, llevado, unidad):
    tolerancia = max(0.10 * llevado, 0.1 if unidad == "kg" else 1.0)
    return abs(comprado - llevado) <= tolerancia + 1e-9


def comparar_lista(det, compras, excluir=()):
    """
    det: renglones de UNA lista (articulo_id, nombre, unidad_medida, sugerido,
         cantidad, incluido, costo_c_iva).
    compras: lo facturado en la ventana de esa lista, por artículo
         (articulo_id, nombre, unidad_medida, comprado, monto_c_iva).
    excluir: artículos que no se compran en el viaje (compra diaria,
         consignación): no cuentan como agregados.
    Devuelve (detalle, resumen).
    """
    det = det.copy()
    det["llevado"] = det["incluido"].astype(bool) & (det["cantidad"].astype(float) > 0)
    comp = compras.set_index("articulo_id") if not compras.empty else pd.DataFrame(
        columns=["nombre", "unidad_medida", "comprado", "monto_c_iva"])
    det["comprado"] = det["articulo_id"].map(comp["comprado"]).fillna(0.0).astype(float) if not comp.empty else 0.0
    det["monto_c_iva"] = det["articulo_id"].map(comp["monto_c_iva"]).fillna(0.0).astype(float) if not comp.empty else 0.0

    def estado(r):
        if r["llevado"]:
            if r["comprado"] <= 0:
                return "No se compró"
            return "Comprado igual" if _igual(r["comprado"], float(r["cantidad"]), r["unidad_medida"]) \
                else "Comprado, otra cantidad"
        return "Sacado, pero se compró" if r["comprado"] > 0 else "Sacado de la lista"

    det["estado"] = det.apply(estado, axis=1) if not det.empty else pd.Series(dtype=object)
    agregados = comp[~comp.index.isin(det["articulo_id"]) & ~comp.index.isin(list(excluir))].reset_index() \
        if not comp.empty else pd.DataFrame(columns=["articulo_id", "nombre", "unidad_medida", "comprado", "monto_c_iva"])
    agregados = agregados.assign(sugerido=0.0, cantidad=0.0, incluido=False, llevado=False,
                                 estado="Agregado (no estaba en la lista)")
    detalle = pd.concat([det, agregados], ignore_index=True)

    llevados = det[det["llevado"]]
    n_llev = len(llevados)
    cambiados = int((det["llevado"] & ((det["cantidad"].astype(float) - det["sugerido"].astype(float)).abs() > 1e-9)).sum()
                    + (~det["llevado"]).sum())
    cuenta = detalle["estado"].value_counts()
    resumen = {
        "sugeridos": int(len(det)),
        "llevados": n_llev,
        "cambiados_al_armar": cambiados,
        "comprados_de_la_lista": int((llevados["comprado"] > 0).sum()),
        "pct_lista_comprada": (float((llevados["comprado"] > 0).mean()) if n_llev else None),
        "pct_comprado_igual": (float(cuenta.get("Comprado igual", 0) / n_llev) if n_llev else None),
        "agregados": int(len(agregados)),
        "costo_lista": float((llevados["cantidad"].astype(float) * llevados["costo_c_iva"].astype(float).fillna(0)).sum()),
        "monto_comprado": float(detalle["monto_c_iva"].sum()),
        "monto_agregados": float(agregados["monto_c_iva"].astype(float).sum()) if len(agregados) else 0.0,
        "hubo_compras": bool(len(comp)),
    }
    resumen.update({f"n_{e}": int(cuenta.get(e, 0)) for e in ESTADOS_LISTA})
    return detalle, resumen


# ----------------------------------------------------------------------------
# Tanda 20: los componentes de un combo heredan la clase del combo
# ----------------------------------------------------------------------------
# Un combo se vende solo si están TODOS sus componentes: queda expuesto al
# componente con menos stock de seguridad (comparativa ABC del 04/10: con la
# política por clase, los quiebres de combo subieron por ese "eslabón más
# débil"). Para decidir cuánto comprar, cada componente toma la MEJOR clase
# entre la suya y la de los combos que integra. Solo sube de clase, nunca
# baja. No cambia la elección del modelo de pronóstico (que sigue por la
# clase propia): solo el nivel de servicio de la lista, las alertas y el
# riesgo de vencimiento.
ORDEN_CLASE = {"A": 0, "B": 1, "C": 2}


def heredar_clase_combos(pron, herencia):
    """
    pron: DataFrame largo del pronóstico (articulo_id, ..., clase).
    herencia: DataFrame componente_articulo_id, combo, clase_combo
        (db.clase_combos). Devuelve una copia con 'clase' mejorada,
        'clase_propia' y 'clase_por' (nombre del combo que la subió, o None).
    """
    out = pron.copy()
    out["clase_propia"] = out["clase"]
    out["clase_por"] = None
    if herencia is None or herencia.empty or out.empty:
        return out
    h = herencia.assign(_o=herencia["clase_combo"].map(ORDEN_CLASE)).sort_values(["_o", "combo"])
    mejor = h.drop_duplicates("componente_articulo_id").set_index("componente_articulo_id")
    propia = out["clase"].map(ORDEN_CLASE)
    heredada = out["articulo_id"].map(mejor["_o"])
    sube = heredada.notna() & (heredada < propia)
    out.loc[sube, "clase"] = out.loc[sube, "articulo_id"].map(mejor["clase_combo"])
    out.loc[sube, "clase_por"] = out.loc[sube, "articulo_id"].map(mejor["combo"])
    return out
