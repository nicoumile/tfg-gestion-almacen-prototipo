"""
Desafío Recreo (tanda 21, 10/2026): control de vencimientos en la góndola, en forma de juego.

Las facturas no traen fecha de vencimiento: la fecha real se obtiene mirando la
góndola. Cada día la app propone una TAREA de pocos artículos (perecederos,
mucha plata en stock, nunca controlados) y se anota la fecha más cercana que se
ve. Cada control:
  - le da al tablero de ⏰ Vencimientos la fecha real de la compra más vieja que
    queda (db.lotes_vencimiento, fuente "góndola");
  - guarda la fecha de esa compra, para estimar los días disponibles típicos de
    cada categoría (dias_tipicos), que reemplazan a una tabla genérica.

El juego (temática escolar del almacén "Recreo"):
  - estrellitas por control (más en perecederos), por terminar la tarea, por la
    racha y por el boletín de la semana (7 días cumplidos; hay comodines);
  - rescates: un lote que vencía pronto y se vendió antes de vencer;
  - juego de las monedas con los clientes en compras grandes;
  - niveles (Buen alumno, Escolta, Abanderado), insignias y figuritas.
Reglas para que no convenga inventar datos: las estrellitas no dependen de la
fecha anotada; un artículo suma una vez por semana; fechas imposibles se
rechazan; de vez en cuando se vuelve a pedir un artículo ya controlado (si la
fecha nueva es ANTERIOR a la vista, no suma).

Fase 2 (misma tanda): el boletín tiene cuatro MATERIAS.
  - Góndola: la tarea de vencimientos y, en algunos artículos, un CONTEO a ciegas
    (no se muestra el stock de la app). Mide la exactitud del inventario.
  - Orden: "Arreglá un dato", uno por día de la tabla Qué corregir del indicador
    de calidad. Suma solo si la app verifica que quedó arreglado.
  - Excursión: guardar la lista antes de ir al mayorista, anotar el viaje
    (planificado o de urgencia suman igual) y "lista cumplida".
  - Cuentas: el cierre del día guardado (efectivo contado y el pan).
Se premia la acción verificable, nunca el indicador: no suma tener menos alertas,
menos viajes de urgencia ni una caja que cierra justo.
"""
from __future__ import annotations

import datetime as dt
import math
import random

import pandas as pd
from sqlalchemy import text

import db

PUNTOS_CONTROL = 10
PUNTOS_PERECEDERO = 15
PUNTOS_SIN_FECHA = 5       # "No hay" o "No vence": suma menos que anotar una fecha
PUNTOS_TAREA = 20
DIAS_RACHA = 5            # desde 5 días seguidos, las estrellitas del día valen ×1,5
MULT_RACHA = 1.5
PUNTOS_BOLETIN = 100
PUNTOS_JUEGO = 5
JUEGOS_QUE_SUMAN_POR_DIA = 3
PESOS_POR_ESTRELLITA = 1000  # rescate: 1 estrellita por cada $1.000 de costo salvado
DIAS_SIN_REPETIR = 7       # un artículo suma una vez por semana
DIAS_PERECEDERO = 30       # vida útil (propia o de la categoría) hasta la que se considera perecedero
DIAS_HALLAZGO = 15         # una fecha a menos de 15 días es un hallazgo (vence pronto)
PROB_REPREGUNTA = 0.35     # probabilidad de que la tarea del día incluya un repaso
CONTROLES_HELADERA = 10    # insignia "Heladera al día": controles de Fiambres y lácteos en una semana
CONTROLES_MAPA = 3         # "Mapa completo": cada categoría con al menos 3 fechas
JUEGOS_MONEDITA = 10
# Fase 2
PUNTOS_CONTEO = 5          # además del control, por contar a ciegas
PUNTOS_DATO = 15           # dato arreglado y verificado
PUNTOS_DATO_REVISADO = 5   # venta atípica revisada (no se puede verificar)
PUNTOS_LISTA = 10          # lista guardada antes de ir al mayorista
PUNTOS_VIAJE = 10          # viaje anotado (planificado o de urgencia, igual)
PUNTOS_LISTA_CUMPLIDA = 20
PCT_LISTA_CUMPLIDA = 0.8   # de lo llevado en la lista, comprado en su semana
PUNTOS_CIERRE = 10
CONTEOS_CONTADOR = 20
DATOS_LUPA = 10
VIAJES_EXCURSION = 4       # viajes anotados con la lista de la app
CIERRES_ALCANCIA = 10
INDICE_SEMANA_EN_VERDE = 95.0
TOLERANCIA_KG = 0.05       # conteo por kg: coincide si la diferencia es de hasta 5 %
CHEQUEOS_DATO = ("stock_negativo", "sin_costo", "venta_atipica")   # en este orden de prioridad
DIAS_DATO_SIN_REPETIR = 3
CATEGORIA_HELADERA = "Fiambres y lácteos"
# Sin vencimiento que cambie las compras: no entran en la tarea
CATEGORIAS_SIN_VENCIMIENTO = ("Limpieza", "Tabaco", "Librería", "Otros")
# Bebidas que no vencen (o duran años): no se proponen. Cualquier otro artículo sin
# fecha en el envase se saca con el botón «No vence» y no vuelve a la tarea.
PATRON_NO_VENCE = r"(^| )(VINO|WHISKY|FERNET|GIN|VODKA|LICOR|PISCO|VERMOUTH|APERITIVO|AMARGO)( |$)"

NIVELES = [(0, "Alumno nuevo"), (300, "Buen alumno"), (800, "Escolta"), (1400, "Abanderado")]

# Figuritas: (clave, nombre, cómo se gana). La condición se evalúa en estado().
FIGURITAS = [
    ("timbre", "Timbre", "Primer control hecho"),
    ("mochi", "Mochi", "Nivel Buen alumno · 300 estrellitas"),
    ("gato", "Gato del Recreo", f"Heladera al día: {CONTROLES_HELADERA} controles de fiambres y lácteos en una semana"),
    ("guardapolvo", "Guardapolvo", "Boletín de la semana: 7 días cumplidos"),
    ("escolta", "Escolta", "Nivel Escolta · 800 estrellitas"),
    ("abanderado", "Abanderado", "Nivel Abanderado · 1.400 estrellitas"),
    ("monedita", "Monedita", f"Recreo con clientes: {JUEGOS_MONEDITA} juegos de las monedas"),
    ("abaco", "Ábaco", f"Góndola: {CONTEOS_CONTADOR} conteos hechos"),
    ("lupa", "Lupa", f"Orden: {DATOS_LUPA} datos arreglados"),
    ("micro", "Micro de la excursión", f"Excursión: {VIAJES_EXCURSION} viajes anotados con la lista de la app"),
    ("alcancia", "Alcancía", f"Cuentas: {CIERRES_ALCANCIA} cierres del día"),
]
INSIGNIAS = ["Primer control", "Heladera al día", "Primer rescate", "Boletín de la semana", "Mapa completo",
             "Cuadro de honor (4 boletines seguidos)", "Semana en verde", "Libreta completa"]
MATERIAS = [("gondola", "🧺 Góndola"), ("orden", "🔍 Orden"), ("excursion", "🚌 Excursión"), ("cuentas", "🧮 Cuentas")]


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def hay_desafio():
    return db.hay_desafio()


def _ref_dia(d):
    return int(pd.Timestamp(d).strftime("%Y%m%d"))


def lunes(d):
    d = pd.Timestamp(d).date()
    return d - dt.timedelta(days=d.weekday())


def parametros():
    p = db.obtener_parametros()
    return {"articulos_dia": int(p.get("desafio_articulos_dia", 6)),
            "conteos_dia": int(p.get("desafio_conteos_dia", 2)),
            "comodines": int(p.get("desafio_comodines_semana", 1)),
            "umbral": float(p.get("monedas_umbral", 10000)),
            "monedas": float(p.get("monedas_por_juego", 2)),
            "articulo_monedas": int(p["monedas_articulo_id"]) if p.get("monedas_articulo_id") else None}


def guardar_parametros(articulos_dia, comodines, umbral, monedas, articulo_monedas, conteos_dia=None):
    """Los parámetros del desafío (pestaña 🔒 Configuración)."""
    if conteos_dia is None:
        conteos_dia = parametros()["conteos_dia"]
    if not 0 <= int(conteos_dia) <= int(articulos_dia):
        raise ValueError("Los conteos por día tienen que estar entre 0 y los artículos por día.")
    vals = {"desafio_articulos_dia": int(articulos_dia), "desafio_conteos_dia": int(conteos_dia),
            "desafio_comodines_semana": int(comodines),
            "monedas_umbral": float(umbral), "monedas_por_juego": float(monedas),
            "monedas_articulo_id": int(articulo_monedas or 0)}
    if not 1 <= vals["desafio_articulos_dia"] <= 20:
        raise ValueError("Los artículos por día tienen que estar entre 1 y 20.")
    if not 0 <= vals["desafio_comodines_semana"] <= 3:
        raise ValueError("Los comodines por semana tienen que estar entre 0 y 3.")
    if vals["monedas_umbral"] < 0 or vals["monedas_por_juego"] <= 0:
        raise ValueError("El monto y las monedas por juego tienen que ser positivos.")
    cambios = 0
    with db.engine.begin() as conn:
        for clave, valor in vals.items():
            ant = conn.execute(text("SELECT valor FROM parametros WHERE clave = :c"), {"c": clave}).scalar()
            if ant is not None and float(ant) == float(valor):
                continue
            conn.execute(text("""INSERT INTO parametros (clave, valor) VALUES (:c, :v)
                                 ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()"""),
                         {"c": clave, "v": valor})
            db.registrar_historial("parametros", None, "PARAMETRO_EDITADO",
                                   f"{clave}: {float(ant) if ant is not None else '-'} -> {float(valor):g}", conn=conn)
            cambios += 1
    return cambios


# ----------------------------------------------------------------------------
# Participantes y premios
# ----------------------------------------------------------------------------
def participantes():
    return pd.read_sql(text("""
        SELECT p.id AS persona_id, p.nombre, p.usuario, p.rol, COALESCE(dp.activo, FALSE) AS participa
        FROM personas p LEFT JOIN desafio_participantes dp ON dp.persona_id = p.id
        WHERE p.activo ORDER BY p.id
    """), db.engine)


def es_participante(persona_id):
    with db.engine.connect() as conn:
        return bool(conn.execute(text("SELECT activo FROM desafio_participantes WHERE persona_id = :p"),
                                 {"p": int(persona_id)}).scalar())


def guardar_participantes(ids_que_participan):
    ids = {int(i) for i in ids_que_participan}
    with db.engine.begin() as conn:
        todos = conn.execute(text("SELECT id, nombre FROM personas WHERE activo")).all()
        for pid, nombre in todos:
            conn.execute(text("""INSERT INTO desafio_participantes (persona_id, activo) VALUES (:p, :a)
                                 ON CONFLICT (persona_id) DO UPDATE SET activo = EXCLUDED.activo"""),
                         {"p": pid, "a": pid in ids})
        db.registrar_historial("parametros", None, "PARAMETRO_EDITADO",
                               "Desafío Recreo: participan " + (", ".join(n for p, n in todos if p in ids) or "nadie"),
                               conn=conn)


def premios():
    df = pd.read_sql(text("SELECT hito, premio FROM desafio_premios"), db.engine)
    return {r.hito: (r.premio or "") for r in df.itertuples()}


def guardar_premios(premios_por_hito):
    with db.engine.begin() as conn:
        for hito, premio in premios_por_hito.items():
            conn.execute(text("""INSERT INTO desafio_premios (hito, premio) VALUES (:h, :p)
                                 ON CONFLICT (hito) DO UPDATE SET premio = EXCLUDED.premio, actualizado_en = now()"""),
                         {"h": hito, "p": (premio or "").strip() or None})


# ----------------------------------------------------------------------------
# Tarea del día
# ----------------------------------------------------------------------------
def candidatos(hoy):
    """Artículos que tiene sentido controlar: activos, con stock, que vencen (no
    compra diaria, no fraccionados, no las categorías sin vencimiento), con su
    prioridad y el último control."""
    hoy = pd.Timestamp(hoy).date()
    arts = pd.read_sql(text("""
        SELECT a.id AS articulo_id, a.nombre, a.categoria, a.stock_actual::float AS stock,
               COALESCE(a.costo_compra_actual, 0)::float AS costo, a.vida_util_dias,
               (SELECT MAX(c.fecha) FROM controles_vencimiento c WHERE c.articulo_id = a.id) AS ultimo_control
        FROM articulos a
        WHERE a.activo AND a.categoria IS DISTINCT FROM 'Promocion' AND a.stock_actual > 0
          AND a.articulo_fraccion_de IS NULL
          AND COALESCE(a.reposicion_dias, 0) <> 1
          AND COALESCE(a.categoria, '') <> ALL(:sin)
          AND upper(a.nombre) !~ :patron
          AND NOT EXISTS (SELECT 1 FROM controles_vencimiento n WHERE n.articulo_id = a.id AND n.no_vence)
    """), db.engine, params={"sin": list(CATEGORIAS_SIN_VENCIMIENTO), "patron": PATRON_NO_VENCE})
    if arts.empty:
        return arts
    vida = db.vida_util_efectiva(arts) if db.hay_tanda18() else pd.Series(float("nan"), index=arts.index)
    arts["perecedero"] = (arts["categoria"] == CATEGORIA_HELADERA) | (vida <= DIAS_PERECEDERO)
    arts["nunca"] = arts["ultimo_control"].isna()
    arts["valor"] = arts["stock"] * arts["costo"]
    ult = pd.to_datetime(arts["ultimo_control"])
    arts["dias_desde_control"] = (pd.Timestamp(hoy) - ult).dt.days
    return arts


def armar_tarea(persona_id, hoy, n=None):
    """La tarea del día (se arma una sola vez por día y persona y queda guardada)."""
    hoy = pd.Timestamp(hoy).date()
    n = n or parametros()["articulos_dia"]
    with db.engine.connect() as conn:
        ya = conn.execute(text("SELECT COUNT(*) FROM desafio_tareas WHERE fecha = :f AND persona_id = :p"),
                          {"f": hoy, "p": int(persona_id)}).scalar()
    if ya:
        return
    c = candidatos(hoy)
    elegidos, motivos = [], {}
    rnd = random.Random(f"{hoy.isoformat()}-{persona_id}")
    if c.empty:
        c = pd.DataFrame(columns=["articulo_id", "nunca", "dias_desde_control", "perecedero", "valor"])
    elegibles = c[c["nunca"] | (c["dias_desde_control"] >= DIAS_SIN_REPETIR)].copy()
    # mitad perecederos (los que hace más que no se miran primero), el resto por plata en stock
    per = elegibles[elegibles["perecedero"]].sort_values(["nunca", "dias_desde_control", "valor"],
                                                         ascending=[False, False, False])
    for aid in per["articulo_id"].head(math.ceil(n / 2)):
        elegidos.append(int(aid)); motivos[int(aid)] = "perecedero"
    resto = elegibles[~elegibles["articulo_id"].isin(elegidos)].sort_values(["valor", "nunca"], ascending=[False, False])
    for r in resto.itertuples():
        if len(elegidos) >= n:
            break
        elegidos.append(int(r.articulo_id)); motivos[int(r.articulo_id)] = "nunca" if r.nunca else "stock"
    # repaso al azar (mismo resultado todo el día): un artículo ya controlado hace 7-30 días
    if rnd.random() < PROB_REPREGUNTA and elegidos:
        rep = c[(~c["nunca"]) & c["dias_desde_control"].between(DIAS_SIN_REPETIR, 30)
                & ~c["articulo_id"].isin(elegidos)]
        if not rep.empty:
            aid = int(rep.sample(1, random_state=rnd.randint(0, 10**6))["articulo_id"].iloc[0])
            if len(elegidos) >= n:
                motivos.pop(elegidos.pop(), None)
            elegidos.append(aid); motivos[aid] = "repaso"
    # conteo a ciegas en algunos (no en el repaso, que ya es una verificación)
    contables = [a for a in elegidos if motivos[a] != "repaso"]
    a_contar = set(rnd.sample(contables, min(parametros()["conteos_dia"], len(contables))))
    dato = _dato_del_dia(persona_id, hoy, elegidos)
    with db.engine.begin() as conn:
        for i, aid in enumerate(elegidos):
            conn.execute(text("""INSERT INTO desafio_tareas (fecha, persona_id, articulo_id, orden, tipo, motivo, pide_conteo)
                                 VALUES (:f, :p, :a, :o, 'gondola', :m, :c) ON CONFLICT DO NOTHING"""),
                         {"f": hoy, "p": int(persona_id), "a": aid, "o": i, "m": motivos[aid], "c": aid in a_contar})
        if dato:
            conn.execute(text("""INSERT INTO desafio_tareas (fecha, persona_id, articulo_id, orden, tipo, motivo)
                                 VALUES (:f, :p, :a, :o, 'dato', :m) ON CONFLICT DO NOTHING"""),
                         {"f": hoy, "p": int(persona_id), "a": dato[0], "o": len(elegidos), "m": dato[1]})


def _dato_del_dia(persona_id, hoy, excluir):
    """(articulo_id, chequeo) del indicador de calidad para arreglar hoy, o None.
    Prioridad: stock negativo, sin costo, venta atípica. No repite un artículo
    pedido en los últimos días."""
    try:
        alertas = db.calidad_de_datos(hoy)["alertas"]
    except Exception:
        return None
    if alertas is None or alertas.empty:
        return None
    al = alertas[alertas["chequeo"].isin(CHEQUEOS_DATO) & ~alertas["articulo_id"].isin(list(excluir))].copy()
    with db.engine.connect() as conn:
        recientes = {r[0] for r in conn.execute(text("""
            SELECT articulo_id FROM desafio_tareas WHERE persona_id = :p AND tipo = 'dato'
              AND fecha >= :d AND fecha < :h"""), {"p": int(persona_id), "d": hoy - dt.timedelta(days=DIAS_DATO_SIN_REPETIR),
                                                     "h": hoy})}
    al = al[~al["articulo_id"].isin(recientes)]
    if al.empty:
        return None
    al["_o"] = al["chequeo"].map({c: i for i, c in enumerate(CHEQUEOS_DATO)})
    r = al.sort_values(["_o", "articulo_id"]).iloc[0]
    return int(r["articulo_id"]), str(r["chequeo"])


def tarea_del_dia(persona_id, hoy):
    """La tarea de hoy con lo ya controlado (arma la tarea si todavía no existe)."""
    hoy = pd.Timestamp(hoy).date()
    armar_tarea(persona_id, hoy)
    return pd.read_sql(text("""
        SELECT t.orden, t.articulo_id, a.nombre, a.categoria, a.unidad_medida, a.stock_actual::float AS stock,
               COALESCE(a.costo_compra_actual, 0)::float AS costo, t.tipo, t.motivo, t.pide_conteo,
               c.id AS control_id, c.hay_stock, c.no_vence, c.vence, c.cantidad::float AS cantidad,
               c.conteo::float AS conteo, c.conteo_coincide, c.corregido, c.stock_al_controlar::float AS stock_al_controlar,
               COALESCE(e.estrellitas, 0) AS estrellitas,
               ed.id AS dato_id, ed.estrellitas AS dato_estrellitas, ed.detalle AS dato_detalle
        FROM desafio_tareas t
        JOIN articulos a ON a.id = t.articulo_id
        LEFT JOIN LATERAL (SELECT * FROM controles_vencimiento c
                           WHERE t.tipo = 'gondola' AND c.articulo_id = t.articulo_id AND c.fecha = t.fecha
                             AND c.persona_id = t.persona_id
                           ORDER BY c.cuenta DESC, c.id DESC LIMIT 1) c ON TRUE
        LEFT JOIN desafio_eventos e ON e.persona_id = t.persona_id AND e.tipo = 'control' AND e.ref_id = c.id
        LEFT JOIN desafio_eventos ed ON t.tipo = 'dato' AND ed.persona_id = t.persona_id AND ed.tipo = 'dato'
             AND ed.ref_id = CAST(to_char(t.fecha, 'YYYYMMDD') AS BIGINT) * 100000 + t.articulo_id
        WHERE t.fecha = :f AND t.persona_id = :p
        ORDER BY t.orden
    """), db.engine, params={"f": hoy, "p": int(persona_id)})


# ----------------------------------------------------------------------------
# Controles
# ----------------------------------------------------------------------------
def fecha_compra_mas_vieja(conn, articulo_id, stock):
    """Fecha de la compra más vieja que todavía tiene stock (se vende primero lo
    más viejo): se recorren las compras de la más nueva a la más vieja hasta
    cubrir el stock. None si el stock no se explica con facturas."""
    filas = conn.execute(text("""
        SELECT f.fecha, d.cantidad::float FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
        WHERE d.articulo_id = :a ORDER BY f.fecha DESC, d.id DESC
    """), {"a": int(articulo_id)}).all()
    resto, ultima = float(stock or 0), None
    for fecha, cant in filas:
        if resto <= 1e-9:
            break
        resto -= float(cant)
        ultima = fecha
    return ultima if resto <= 1e-9 else None


def _validar_fecha(vence, hoy):
    if vence is None:
        raise ValueError("Anotá la fecha de vencimiento más cercana, o marcá «No hay».")
    vence = pd.Timestamp(vence).date()
    if vence < hoy - dt.timedelta(days=365):
        raise ValueError("Esa fecha es de hace más de un año: revisala.")
    if vence > hoy + dt.timedelta(days=3 * 365):
        raise ValueError("Esa fecha es de dentro de más de 3 años: revisala.")
    return vence


def registrar_control(persona_id, articulo_id, hoy, hay_stock=True, vence=None, cantidad=None, no_vence=False,
                      conteo=None):
    """
    Guarda un control de góndola y da las estrellitas que correspondan.
    `conteo`: cuántos hay en total (góndola + depósito), obligatorio si la tarea
    lo pide (con "No hay" vale 0). Se compara con el stock de la app.
    Devuelve dict: estrellitas (de este control), tarea_completa, bonus (tarea +
    racha), hallazgo ('vencido' | 'pronto' | None), mensajes (lista de textos),
    control_id, conteo, stock_app, conteo_coincide.
    """
    hoy = pd.Timestamp(hoy).date()
    persona_id, articulo_id = int(persona_id), int(articulo_id)
    if no_vence:
        hay_stock, vence, cantidad = True, None, None
    elif hay_stock:
        vence = _validar_fecha(vence, hoy)
        if cantidad is not None and float(cantidad) < 0:
            raise ValueError("La cantidad no puede ser negativa.")
    else:
        vence, cantidad = None, None
    juega = es_participante(persona_id)
    mensajes = []
    with db.engine.begin() as conn:
        art = conn.execute(text("SELECT nombre, categoria, stock_actual::float, vida_util_dias FROM articulos WHERE id = :a"),
                           {"a": articulo_id}).first()
        if art is None:
            raise ValueError("No existe el artículo.")
        t_row = conn.execute(text("""SELECT motivo, pide_conteo FROM desafio_tareas WHERE fecha = :f AND persona_id = :p
                                     AND articulo_id = :a AND tipo = 'gondola'"""),
                             {"f": hoy, "p": persona_id, "a": articulo_id}).first()
        tarea = t_row[0] if t_row else None
        pide_conteo = bool(t_row[1]) if t_row else False
        repaso = tarea == "repaso"
        if not hay_stock:
            conteo = 0.0 if pide_conteo or conteo is not None else None
        elif conteo is not None and float(conteo) < 0:
            raise ValueError("El conteo no puede ser negativo.")
        elif pide_conteo and conteo is None:
            raise ValueError("En este artículo también hay que contar cuántos hay (góndola + depósito).")
        previo = conn.execute(text("""SELECT fecha, vence FROM controles_vencimiento
                                      WHERE articulo_id = :a AND hay_stock AND vence IS NOT NULL AND fecha < :f
                                      ORDER BY fecha DESC, id DESC LIMIT 1"""), {"a": articulo_id, "f": hoy}).first()
        coincide = None
        if repaso and hay_stock and vence is not None and previo is not None and previo[1] >= hoy:
            # lo que se vio antes no puede "rejuvenecer": la fecha más cercana solo se aleja
            # a medida que se venden los lotes viejos (o llega una compra con fecha más lejana)
            coincide = vence >= previo[1]
        reciente = conn.execute(text("""SELECT COUNT(*) FROM controles_vencimiento
                                        WHERE articulo_id = :a AND persona_id = :p AND cuenta
                                          AND fecha > :d"""),
                                {"a": articulo_id, "p": persona_id, "d": hoy - dt.timedelta(days=DIAS_SIN_REPETIR)}).scalar()
        cuenta = juega and (coincide is not False) and (reciente == 0)
        stock = float(art[2] or 0)
        conteo_coincide = None
        if conteo is not None:
            unidad = conn.execute(text("SELECT unidad_medida FROM articulos WHERE id = :a"), {"a": articulo_id}).scalar()
            conteo_coincide = coincide_conteo(float(conteo), stock, unidad)
        lote = fecha_compra_mas_vieja(conn, articulo_id, stock) if (hay_stock and vence is not None) else None
        cid = conn.execute(text("""
            INSERT INTO controles_vencimiento (fecha, persona_id, articulo_id, hay_stock, no_vence, vence, cantidad,
                                               conteo, conteo_coincide,
                                               stock_al_controlar, fecha_compra_lote, repregunta, coincide, cuenta)
            VALUES (:f, :p, :a, :h, :nv, :v, :c, :cn, :cc, :s, :l, :r, :co, :cu) RETURNING id
        """), {"f": hoy, "p": persona_id, "a": articulo_id, "h": bool(hay_stock), "nv": bool(no_vence), "v": vence,
               "c": None if cantidad is None else float(cantidad),
               "cn": None if conteo is None else float(conteo), "cc": conteo_coincide,
               "s": stock, "l": lote, "r": repaso,
               "co": coincide, "cu": cuenta}).scalar()
        db.registrar_historial("articulos", articulo_id, "CONTROL_VENCIMIENTO",
                               f"Control de góndola de '{art[0]}': "
                               + ("el envase no tiene vencimiento" if no_vence else
                                  (f"vence {vence:%d/%m/%Y}" + (f" ({float(cantidad):g})" if cantidad is not None else "")
                                   if hay_stock else "no hay en la góndola"))
                               + ("" if cuenta else " (no suma estrellitas)"), conn=conn)
        puntos = 0
        if cuenta:
            perecedero = art[1] == CATEGORIA_HELADERA or (art[3] is not None and int(art[3]) <= DIAS_PERECEDERO)
            puntos = PUNTOS_PERECEDERO if perecedero else PUNTOS_CONTROL
            if vence is None:
                puntos = PUNTOS_SIN_FECHA
            if conteo is not None and pide_conteo:
                puntos += PUNTOS_CONTEO
            conn.execute(text("""INSERT INTO desafio_eventos (persona_id, fecha, tipo, ref_id, estrellitas, detalle)
                                 VALUES (:p, :f, 'control', :r, :e, :d) ON CONFLICT DO NOTHING"""),
                         {"p": persona_id, "f": hoy, "r": cid, "e": puntos, "d": art[0]})
        elif juega and coincide is False:
            mensajes.append("Esta fecha es anterior a la que se había visto: queda para revisar y no suma estrellitas.")
        elif juega and reciente:
            mensajes.append("Este artículo ya sumó esta semana: el control se guarda igual.")
    hallazgo = None
    if conteo is not None and conteo_coincide is False and hay_stock:
        mensajes.append(f"Contaste {float(conteo):g} y la app tenía {stock:g}. Si estás seguro, corregí el stock "
                        "con el botón de abajo (si no, volvé a contar mañana).")
    if not hay_stock and stock > 0:
        mensajes.append(f"La app dice que hay {stock:g}: si tampoco hay en el depósito, corregí el stock en "
                        "📦 Artículos.")
    if vence is None:
        pass
    elif vence < hoy:
        hallazgo = "vencido"
        mensajes.append("¡Hallazgo! Ya está vencido: si se tira, registralo en 💵 Registrar Venta → 🗑️ Sobrante del día "
                        "→ «Venció (se tiró)».")
    elif (vence - hoy).days <= DIAS_HALLAZGO:
        hallazgo = "pronto"
        mensajes.append(f"¡Hallazgo! Vence en {(vence - hoy).days} días. Si se vende antes, suma un rescate.")
    bonus = cerrar_dia(persona_id, hoy) if juega else 0
    return {"estrellitas": puntos, "bonus": bonus, "tarea_completa": bonus > 0 or tarea_completa(persona_id, hoy),
            "hallazgo": hallazgo, "mensajes": mensajes, "control_id": cid,
            "conteo": None if conteo is None else float(conteo), "stock_app": stock, "conteo_coincide": conteo_coincide}


def coincide_conteo(conteo, stock, unidad=None):
    """El conteo coincide con el stock de la app: por kg, hasta 5 % de diferencia;
    por unidad, la misma cantidad (redondeada)."""
    if str(unidad or "").lower() in ("kg", "kilo", "kilos", "kilogramo"):
        return abs(conteo - stock) <= max(TOLERANCIA_KG * max(abs(conteo), abs(stock)), 0.01)
    return abs(conteo - stock) < 0.5


def corregir_por_conteo(control_id):
    """Deja el stock de la app en lo contado, más lo que se movió desde el conteo
    (ventas o compras cargadas después). Marca el control como corregido."""
    with db.engine.begin() as conn:
        c = conn.execute(text("""SELECT articulo_id, conteo::float, stock_al_controlar::float, corregido
                                 FROM controles_vencimiento WHERE id = :i"""), {"i": int(control_id)}).first()
        if c is None or c[1] is None:
            raise ValueError("Ese control no tiene conteo.")
        if c[3]:
            return None
        ahora = float(conn.execute(text("SELECT stock_actual FROM articulos WHERE id = :a"), {"a": c[0]}).scalar())
        nuevo = round(c[1] + (ahora - c[2]), 3)
        db.corregir_stock(c[0], nuevo, conn=conn)
        conn.execute(text("UPDATE controles_vencimiento SET corregido = TRUE WHERE id = :i"), {"i": int(control_id)})
    return nuevo


def tarea_completa(persona_id, hoy):
    with db.engine.connect() as conn:
        falta = conn.execute(text("""
            SELECT COUNT(*) FROM desafio_tareas t
            WHERE t.fecha = :f AND t.persona_id = :p
              AND ((t.tipo = 'gondola'
                    AND NOT EXISTS (SELECT 1 FROM controles_vencimiento c WHERE c.articulo_id = t.articulo_id
                                    AND c.fecha = t.fecha AND c.persona_id = t.persona_id))
                OR (t.tipo = 'dato'
                    AND NOT EXISTS (SELECT 1 FROM desafio_eventos e WHERE e.persona_id = t.persona_id
                                    AND e.tipo = 'dato'
                                    AND e.ref_id = CAST(to_char(t.fecha, 'YYYYMMDD') AS BIGINT) * 100000 + t.articulo_id)))
        """), {"f": hoy, "p": int(persona_id)}).scalar()
        hay = conn.execute(text("SELECT COUNT(*) FROM desafio_tareas WHERE fecha = :f AND persona_id = :p"),
                           {"f": hoy, "p": int(persona_id)}).scalar()
    return bool(hay) and falta == 0


def cerrar_dia(persona_id, hoy):
    """Si la tarea de hoy quedó completa: +20 y, con racha de 5 días o más, la mitad
    de las estrellitas del día de bonus. Una sola vez por día. Devuelve el bonus dado."""
    if not tarea_completa(persona_id, hoy):
        return 0
    ref = _ref_dia(hoy)
    with db.engine.begin() as conn:
        nuevo = conn.execute(text("""INSERT INTO desafio_eventos (persona_id, fecha, tipo, ref_id, estrellitas, detalle)
                                     VALUES (:p, :f, 'tarea', :r, :e, 'Tarea del día completa')
                                     ON CONFLICT DO NOTHING RETURNING id"""),
                             {"p": int(persona_id), "f": hoy, "r": ref, "e": PUNTOS_TAREA}).scalar()
    if not nuevo:
        return 0
    bonus = PUNTOS_TAREA
    r = racha(persona_id, hoy)["dias"]
    if r >= DIAS_RACHA:
        with db.engine.begin() as conn:
            del_dia = conn.execute(text("""SELECT COALESCE(SUM(estrellitas), 0) FROM desafio_eventos
                                           WHERE persona_id = :p AND fecha = :f AND tipo IN ('control', 'tarea', 'dato')"""),
                                   {"p": int(persona_id), "f": hoy}).scalar()
            extra = int(round(float(del_dia) * (MULT_RACHA - 1)))
            conn.execute(text("""INSERT INTO desafio_eventos (persona_id, fecha, tipo, ref_id, estrellitas, detalle)
                                 VALUES (:p, :f, 'racha', :r, :e, :d) ON CONFLICT DO NOTHING"""),
                         {"p": int(persona_id), "f": hoy, "r": ref, "e": extra, "d": f"Racha de {r} días"})
        bonus += extra
    return bonus


def dias_cumplidos(persona_id, desde=None):
    df = pd.read_sql(text("SELECT fecha FROM desafio_eventos WHERE persona_id = :p AND tipo = 'tarea'"
                          + (" AND fecha >= :d" if desde else "")), db.engine,
                     params={"p": int(persona_id), "d": desde})
    return set(pd.to_datetime(df["fecha"]).dt.date)


def _cubiertos(cumplidos, desde, hasta, comodines):
    """Días entre desde y hasta cubiertos: cumplidos, o un faltante cubierto por el
    comodín de su semana (hasta `comodines` por semana, los primeros que falten)."""
    cubiertos, usados = set(), {}
    d = desde
    while d <= hasta:
        if d in cumplidos:
            cubiertos.add(d)
        else:
            sem = lunes(d)
            if usados.get(sem, 0) < comodines:
                usados[sem] = usados.get(sem, 0) + 1
                cubiertos.add(d)
        d += dt.timedelta(days=1)
    return cubiertos, usados


def racha(persona_id, hoy):
    """Días seguidos con la tarea cumplida, contando hoy solo si ya se cumplió. Un día
    sin cumplir no corta la racha si lo tapa el comodín de su semana (no suma días).
    dict: dias, comodines_usados (en la semana actual)."""
    hoy = pd.Timestamp(hoy).date()
    cumplidos = dias_cumplidos(persona_id, hoy - dt.timedelta(days=180))
    if not cumplidos:
        return {"dias": 0, "comodines_usados": 0}
    comodines = parametros()["comodines"]
    primero = min(cumplidos)
    dias, usados = 0, {}
    d = hoy if hoy in cumplidos else hoy - dt.timedelta(days=1)
    while d >= primero:
        if d in cumplidos:
            dias += 1
        elif usados.get(lunes(d), 0) < comodines:
            usados[lunes(d)] = usados.get(lunes(d), 0) + 1
        else:
            break
        d -= dt.timedelta(days=1)
    return {"dias": dias, "comodines_usados": usados.get(lunes(hoy), 0)}


def otorgar_boletines(persona_id, hoy):
    """Boletín de la semana (+100): cada semana lunes-domingo ya terminada con los 7
    días cubiertos (cumplidos o con comodín). Idempotente. Devuelve las semanas nuevas."""
    hoy = pd.Timestamp(hoy).date()
    cumplidos = dias_cumplidos(persona_id)
    if not cumplidos:
        return []
    comodines = parametros()["comodines"]
    nuevas = []
    sem = lunes(min(cumplidos))
    while sem + dt.timedelta(days=6) < hoy:
        cub, _ = _cubiertos(cumplidos, sem, sem + dt.timedelta(days=6), comodines)
        if len(cub) == 7:
            with db.engine.begin() as conn:
                nuevo = conn.execute(text("""INSERT INTO desafio_eventos (persona_id, fecha, tipo, ref_id, estrellitas, detalle)
                                             VALUES (:p, :f, 'boletin', :r, :e, :d) ON CONFLICT DO NOTHING RETURNING id"""),
                                     {"p": int(persona_id), "f": sem + dt.timedelta(days=6), "r": _ref_dia(sem),
                                      "e": PUNTOS_BOLETIN, "d": f"Boletín de la semana del {sem:%d/%m}"}).scalar()
            if nuevo:
                nuevas.append(sem)
        sem += dt.timedelta(days=7)
    return nuevas


def evaluar_rescates(persona_id, hoy):
    """Rescate: un control que vio una fecha a menos de 15 días, ya pasada esa
    fecha, si lo que había se vendió antes de vencer y no se tiró nada como
    vencido. Vale el costo de esas unidades (1 estrellita cada $1.000). Cada
    control se evalúa una sola vez (también los que no fueron rescate, con 0)."""
    hoy = pd.Timestamp(hoy).date()
    pend = pd.read_sql(text("""
        SELECT c.id, c.fecha, c.articulo_id, c.vence, COALESCE(c.cantidad, c.stock_al_controlar)::float AS unidades,
               a.nombre, COALESCE(a.costo_compra_actual, 0)::float AS costo
        FROM controles_vencimiento c JOIN articulos a ON a.id = c.articulo_id
        WHERE c.persona_id = :p AND c.hay_stock AND c.cuenta AND c.vence IS NOT NULL
          AND c.vence >= c.fecha AND c.vence - c.fecha <= :v AND c.vence < :h
          AND NOT EXISTS (SELECT 1 FROM desafio_eventos e WHERE e.persona_id = c.persona_id
                          AND e.tipo = 'rescate' AND e.ref_id = c.id)
    """), db.engine, params={"p": int(persona_id), "v": DIAS_HALLAZGO, "h": hoy})
    nuevos = []
    for r in pend.itertuples():
        with db.engine.begin() as conn:
            vendido = float(conn.execute(text("""SELECT COALESCE(SUM(cantidad), 0) FROM ventas
                                                 WHERE articulo_id = :a AND NOT es_consumo_interno
                                                   AND fecha BETWEEN :d AND :h"""),
                                         {"a": r.articulo_id, "d": r.fecha, "h": r.vence}).scalar())
            tirado = float(conn.execute(text("""SELECT COALESCE(SUM(cantidad), 0) FROM ventas
                                                WHERE articulo_id = :a AND medio_pago = :m
                                                  AND fecha BETWEEN :d AND :h"""),
                                        {"a": r.articulo_id, "m": db.MEDIO_VENCIDO, "d": r.fecha,
                                         "h": r.vence + dt.timedelta(days=7)}).scalar())
            ok = r.unidades and vendido >= r.unidades - 1e-9 and tirado <= 1e-9
            valor = round(float(r.unidades) * r.costo, 2) if ok else 0.0
            estrellas = int(valor // PESOS_POR_ESTRELLITA) if ok else 0
            conn.execute(text("""INSERT INTO desafio_eventos (persona_id, fecha, tipo, ref_id, estrellitas, valor, detalle)
                                 VALUES (:p, :f, 'rescate', :r, :e, :v, :d) ON CONFLICT DO NOTHING"""),
                         {"p": int(persona_id), "f": r.vence, "r": r.id, "e": estrellas, "v": valor,
                          "d": (f"Rescate: {r.nombre} se vendió antes de vencer" if ok
                                else f"Sin rescate: {r.nombre}")})
            if ok:
                nuevos.append((r.nombre, valor, estrellas))
    return nuevos


# ----------------------------------------------------------------------------
# Juego de las monedas
# ----------------------------------------------------------------------------
def juego_disponible(ticket_id):
    """El ticket califica para el juego: existe, supera el monto y todavía no se jugó."""
    if not hay_desafio():
        return False
    p = parametros()
    if not p["articulo_monedas"]:
        return False
    t = db.total_ticket(ticket_id)
    if t is None or t[1] < p["umbral"]:
        return False
    with db.engine.connect() as conn:
        ya = conn.execute(text("SELECT 1 FROM desafio_eventos WHERE tipo = 'monedas' AND ref_id = :t"),
                          {"t": int(ticket_id)}).scalar()
    return not ya


def registrar_juego(persona_id, ticket_id, monedas, hoy):
    """Se jugó a las monedas con la venta `ticket_id`: las monedas salen del stock
    como regalo y suma +5 estrellitas (las primeras 3 del día). Devuelve las estrellitas."""
    hoy = pd.Timestamp(hoy).date()
    p = parametros()
    if not juego_disponible(ticket_id):
        raise ValueError("Esa venta no califica para el juego (monto menor, ya se anotó o falta el artículo de las monedas).")
    if monedas is None or float(monedas) <= 0:
        raise ValueError("Anotá cuántas monedas se llevó.")
    with db.engine.begin() as conn:
        costo, _ = db.regalar_monedas(p["articulo_monedas"], float(monedas), hoy, ticket_id, conn)
        hoy_juegos = conn.execute(text("""SELECT COUNT(*) FROM desafio_eventos
                                          WHERE persona_id = :p AND tipo = 'monedas' AND fecha = :f AND estrellitas > 0"""),
                                  {"p": int(persona_id), "f": hoy}).scalar()
        estrellas = PUNTOS_JUEGO if (es_participante(persona_id) and hoy_juegos < JUEGOS_QUE_SUMAN_POR_DIA) else 0
        conn.execute(text("""INSERT INTO desafio_eventos (persona_id, fecha, tipo, ref_id, estrellitas, valor, detalle)
                             VALUES (:p, :f, 'monedas', :t, :e, :v, :d)"""),
                     {"p": int(persona_id), "f": hoy, "t": int(ticket_id), "e": estrellas, "v": costo,
                      "d": f"Juego de las monedas con la venta #{ticket_id}: {float(monedas):g} monedas"})
    return estrellas


# ----------------------------------------------------------------------------
# Estado: estrellitas, nivel, semana, insignias, figuritas
# ----------------------------------------------------------------------------
def nivel(estrellitas):
    actual = NIVELES[0]
    for umbral, nombre in NIVELES:
        if estrellitas >= umbral:
            actual = (umbral, nombre)
    sig = next(((u, n) for u, n in NIVELES if u > estrellitas), None)
    return {"nombre": actual[1], "desde": actual[0], "siguiente": sig[1] if sig else None,
            "meta": sig[0] if sig else None}


def estado(persona_id, hoy):
    """Todo lo que muestra la pantalla del desafío para una persona."""
    hoy = pd.Timestamp(hoy).date()
    ev = pd.read_sql(text("SELECT fecha, tipo, ref_id, estrellitas, valor::float AS valor FROM desafio_eventos "
                          "WHERE persona_id = :p"), db.engine, params={"p": int(persona_id)})
    ev["fecha"] = pd.to_datetime(ev["fecha"]).dt.date
    total = int(ev["estrellitas"].sum()) if not ev.empty else 0
    ctrl = pd.read_sql(text("""SELECT c.fecha, c.articulo_id, c.vence, c.hay_stock, a.categoria
                               FROM controles_vencimiento c JOIN articulos a ON a.id = c.articulo_id
                               WHERE c.persona_id = :p AND c.cuenta"""), db.engine, params={"p": int(persona_id)})
    ctrl["fecha"] = pd.to_datetime(ctrl["fecha"]).dt.date
    cumplidos = set(ev.loc[ev["tipo"] == "tarea", "fecha"])
    sem = lunes(hoy)
    semana = []
    for i in range(7):
        d = sem + dt.timedelta(days=i)
        semana.append({"dia": d, "letra": "LMMJVSD"[i], "cumplido": d in cumplidos, "hoy": d == hoy, "futuro": d > hoy})
    rescates = ev[(ev["tipo"] == "rescate") & (ev["estrellitas"] > 0)]
    juegos = ev[ev["tipo"] == "monedas"]
    boletines = sorted(ev.loc[ev["tipo"] == "boletin", "ref_id"].astype(int).tolist())
    # Heladera al día: alguna semana con 10 controles de fiambres y lácteos
    hel = ctrl[ctrl["categoria"] == CATEGORIA_HELADERA].copy()
    heladera = bool(len(hel)) and int(hel.groupby(hel["fecha"].map(lunes)).size().max()) >= CONTROLES_HELADERA
    hel_semana = int((hel["fecha"].map(lunes) == sem).sum()) if len(hel) else 0
    mapa = mapa_completo()
    seguidas = _max_consecutivas([pd.Timestamp(str(b)).date() for b in boletines])
    with db.engine.connect() as conn:
        n_conteos = conn.execute(text("SELECT COUNT(*) FROM controles_vencimiento WHERE persona_id = :p "
                                      "AND conteo IS NOT NULL"), {"p": int(persona_id)}).scalar()
        n_viajes_lista = (conn.execute(text("SELECT COUNT(*) FROM viajes_compra WHERE persona_id = :p "
                                            "AND lista_id IS NOT NULL"), {"p": int(persona_id)}).scalar()
                          if db.hay_registro_viajes() else 0)
    n_datos = int(((ev["tipo"] == "dato") & (ev["estrellitas"] > 0)).sum())
    n_cierres = int((ev["tipo"] == "cierre").sum())
    libreta_hoy = libreta(persona_id, sem, hoy)
    # libreta completa: alguna semana terminada con nota en las cuatro materias, todas 7 o más
    completa = False
    for b in boletines:
        lb = libreta(persona_id, pd.Timestamp(str(b)).date())
        notas = [m["nota"] for m in lb.values()]
        if all(n is not None and n >= 7 for n in notas):
            completa = True
            break
    insignias = {
        "Primer control": len(ctrl) > 0,
        "Heladera al día": heladera,
        "Primer rescate": len(rescates) > 0,
        "Boletín de la semana": len(boletines) > 0,
        "Mapa completo": mapa["completo"],
        "Cuadro de honor (4 boletines seguidos)": seguidas >= 4,
        "Semana en verde": semana_en_verde(),
        "Libreta completa": completa,
    }
    condiciones = {
        "timbre": len(ctrl) > 0, "mochi": total >= 300, "gato": heladera, "guardapolvo": len(boletines) > 0,
        "escolta": total >= 800, "abanderado": total >= 1400, "monedita": len(juegos) >= JUEGOS_MONEDITA,
        "abaco": n_conteos >= CONTEOS_CONTADOR, "lupa": n_datos >= DATOS_LUPA,
        "micro": n_viajes_lista >= VIAJES_EXCURSION, "alcancia": n_cierres >= CIERRES_ALCANCIA,
    }
    return {
        "estrellitas": total, "nivel": nivel(total), "racha": racha(persona_id, hoy), "semana": semana,
        "dias_semana": sum(1 for s in semana if s["cumplido"]),
        "plata_salvada": float(rescates["valor"].fillna(0).sum()), "rescates": len(rescates),
        "juegos": len(juegos), "juegos_semana": int((juegos["fecha"].map(lunes) == sem).sum()) if len(juegos) else 0,
        "costo_juegos": float(juegos["valor"].fillna(0).sum()) if len(juegos) else 0.0,
        "heladera_semana": hel_semana, "mapa": mapa, "insignias": insignias,
        "figuritas": [{"clave": c, "nombre": n, "como": como, "ganada": bool(condiciones[c])} for c, n, como in FIGURITAS],
        "boletines": boletines, "controles": len(ctrl),
        "conteos": n_conteos, "datos": n_datos, "viajes_lista": n_viajes_lista, "cierres": n_cierres,
        "libreta": libreta_hoy,
    }


def _max_consecutivas(lunes_lista):
    if not lunes_lista:
        return 0
    lunes_lista = sorted(set(lunes_lista))
    mejor = act = 1
    for a, b in zip(lunes_lista, lunes_lista[1:]):
        act = act + 1 if (b - a).days == 7 else 1
        mejor = max(mejor, act)
    return mejor


def resumen_semana(persona_id, lunes_semana):
    """Números del boletín de una semana (lunes a domingo)."""
    fin = lunes_semana + dt.timedelta(days=6)
    p = {"p": int(persona_id), "d": lunes_semana, "h": fin}
    with db.engine.connect() as conn:
        r = conn.execute(text("""
            SELECT
              (SELECT COUNT(*) FROM desafio_eventos WHERE persona_id = :p AND tipo = 'tarea' AND fecha BETWEEN :d AND :h),
              (SELECT COUNT(*) FROM controles_vencimiento WHERE persona_id = :p AND fecha BETWEEN :d AND :h),
              (SELECT COUNT(*) FROM controles_vencimiento c JOIN articulos a ON a.id = c.articulo_id
                WHERE c.persona_id = :p AND c.fecha BETWEEN :d AND :h AND a.categoria = :cat),
              (SELECT COUNT(*) FROM controles_vencimiento WHERE persona_id = :p AND fecha BETWEEN :d AND :h
                 AND vence IS NOT NULL AND vence - fecha <= :v),
              (SELECT COALESCE(SUM(valor), 0) FROM desafio_eventos WHERE persona_id = :p AND tipo = 'rescate'
                 AND fecha BETWEEN :d AND :h),
              (SELECT COALESCE(SUM(estrellitas), 0) FROM desafio_eventos WHERE persona_id = :p AND fecha BETWEEN :d AND :h),
              (SELECT COUNT(*) FROM desafio_eventos WHERE persona_id = :p AND tipo = 'monedas' AND fecha BETWEEN :d AND :h)
        """), {**p, "cat": CATEGORIA_HELADERA, "v": DIAS_HALLAZGO}).first()
    return {"tareas": r[0], "controles": r[1], "heladera": r[2], "hallazgos": r[3], "plata_salvada": float(r[4]),
            "estrellitas": int(r[5]), "juegos": r[6]}


# ----------------------------------------------------------------------------
# Días disponibles típicos (lo que buscamos medir)
# ----------------------------------------------------------------------------
def dias_tipicos():
    """Por categoría: controles con fecha y compra conocida, días disponibles
    (vence − fecha de la compra más vieja que quedaba) y su mediana."""
    df = pd.read_sql(text("""
        SELECT a.categoria, c.articulo_id, c.vence, c.fecha_compra_lote
        FROM controles_vencimiento c JOIN articulos a ON a.id = c.articulo_id
        WHERE c.hay_stock AND c.vence IS NOT NULL AND c.fecha_compra_lote IS NOT NULL
          AND COALESCE(c.coincide, TRUE)
    """), db.engine)
    actual = db.vida_util_por_categoria() if db.hay_tanda18() else {}
    if df.empty:
        return pd.DataFrame(columns=["categoria", "controles", "articulos", "mediana", "minimo", "maximo", "configurado"])
    df["dias"] = (pd.to_datetime(df["vence"]) - pd.to_datetime(df["fecha_compra_lote"])).dt.days
    df = df[df["dias"] >= 0]
    g = df.groupby("categoria").agg(controles=("dias", "size"), articulos=("articulo_id", "nunique"),
                                    mediana=("dias", "median"), minimo=("dias", "min"), maximo=("dias", "max")).reset_index()
    g["configurado"] = g["categoria"].map(actual)
    return g.sort_values("controles", ascending=False)


def mapa_completo():
    """Categorías con vencimiento (las que tienen artículos con stock) con al menos 3 fechas vistas."""
    with db.engine.connect() as conn:
        cats = [r[0] for r in conn.execute(text("""
            SELECT DISTINCT categoria FROM articulos
            WHERE activo AND stock_actual > 0 AND categoria IS NOT NULL AND categoria <> 'Promocion'
              AND categoria <> ALL(:sin)
        """), {"sin": list(CATEGORIAS_SIN_VENCIMIENTO)})]
        con = dict(conn.execute(text("""
            SELECT a.categoria, COUNT(*) FROM controles_vencimiento c JOIN articulos a ON a.id = c.articulo_id
            WHERE c.hay_stock AND c.vence IS NOT NULL GROUP BY a.categoria
        """)).all())
    listas = [c for c in cats if con.get(c, 0) >= CONTROLES_MAPA]
    return {"categorias": len(cats), "listas": len(listas), "completo": bool(cats) and len(listas) == len(cats),
            "faltan": sorted(c for c in cats if c not in listas)}


def cumplimiento_semanal(desde=None):
    """Para la validación: por semana y persona, días con la tarea cumplida."""
    df = pd.read_sql(text("SELECT persona_id, fecha FROM desafio_eventos WHERE tipo = 'tarea'"), db.engine)
    if df.empty:
        return df
    df["semana"] = pd.to_datetime(df["fecha"]).dt.date.map(lunes)
    return df.groupby(["semana", "persona_id"]).size().rename("dias_cumplidos").reset_index()



# ----------------------------------------------------------------------------
# Fase 2 · Orden: "Arreglá un dato"
# ----------------------------------------------------------------------------
TEXTO_CHEQUEO = {
    "stock_negativo": ("Stock negativo", "La app tiene stock negativo: contá cuántos hay y corregilo "
                                         "(o cargá la factura que falta)."),
    "sin_costo": ("Sin costo", "No tiene costo cargado: sin costo no hay margen ni capital parado. "
                               "Cargá el costo de la última factura (sin IVA)."),
    "venta_atipica": ("Venta atípica", "Se vendió mucho más que lo normal: revisá en 📋 Últimas ventas que la "
                                       "cantidad esté bien cargada."),
}


def _ref_dato(hoy, articulo_id):
    return _ref_dia(hoy) * 100000 + int(articulo_id)


def verificar_dato(articulo_id, chequeo):
    """True si el chequeo ya no aplica (lo que la app puede verificar)."""
    with db.engine.connect() as conn:
        a = conn.execute(text("SELECT stock_actual::float, COALESCE(costo_compra_actual, 0)::float FROM articulos "
                              "WHERE id = :a"), {"a": int(articulo_id)}).first()
    if chequeo == "stock_negativo":
        return a[0] >= 0
    if chequeo == "sin_costo":
        return a[1] > 0
    return None   # venta atípica: no se puede verificar


def registrar_dato(persona_id, articulo_id, hoy, accion="listo"):
    """El dato del día. accion: 'listo' (la app verifica que quedó arreglado),
    'revisado' (venta atípica: no se puede verificar, suma poco) o 'no_pude' (0
    estrellitas, pero la tarea del día se puede completar). Devuelve el dict de
    registrar_control (estrellitas, bonus, tarea_completa, mensajes)."""
    hoy = pd.Timestamp(hoy).date()
    with db.engine.connect() as conn:
        chequeo = conn.execute(text("""SELECT motivo FROM desafio_tareas WHERE fecha = :f AND persona_id = :p
                                       AND articulo_id = :a AND tipo = 'dato'"""),
                               {"f": hoy, "p": int(persona_id), "a": int(articulo_id)}).scalar()
        nombre = conn.execute(text("SELECT nombre FROM articulos WHERE id = :a"), {"a": int(articulo_id)}).scalar()
    if chequeo is None:
        raise ValueError("Ese artículo no es el dato de hoy.")
    juega = es_participante(persona_id)
    if accion == "listo":
        ok = verificar_dato(articulo_id, chequeo)
        if ok is False:
            raise ValueError(f"Todavía figura como «{TEXTO_CHEQUEO[chequeo][0]}». Arreglalo y volvé a tocar «Listo».")
        puntos = PUNTOS_DATO if ok else PUNTOS_DATO_REVISADO
        detalle = f"Dato arreglado: {nombre} ({TEXTO_CHEQUEO[chequeo][0]})"
    elif accion == "revisado":
        puntos, detalle = PUNTOS_DATO_REVISADO, f"Dato revisado: {nombre} ({TEXTO_CHEQUEO[chequeo][0]})"
    else:
        puntos, detalle = 0, f"Dato sin arreglar: {nombre} ({TEXTO_CHEQUEO[chequeo][0]})"
    puntos = puntos if juega else 0
    with db.engine.begin() as conn:
        nuevo = conn.execute(text("""INSERT INTO desafio_eventos (persona_id, fecha, tipo, ref_id, estrellitas, detalle)
                                     VALUES (:p, :f, 'dato', :r, :e, :d) ON CONFLICT DO NOTHING RETURNING id"""),
                             {"p": int(persona_id), "f": hoy, "r": _ref_dato(hoy, articulo_id), "e": puntos,
                              "d": detalle}).scalar()
    if not nuevo:
        raise ValueError("El dato de hoy ya estaba anotado.")
    bonus = cerrar_dia(persona_id, hoy) if juega else 0
    return {"estrellitas": puntos, "bonus": bonus, "tarea_completa": bonus > 0 or tarea_completa(persona_id, hoy),
            "mensajes": []}


# ----------------------------------------------------------------------------
# Fase 2 · Excursión: lista guardada, viaje anotado, lista cumplida
# ----------------------------------------------------------------------------
def _evento(conn, persona_id, fecha, tipo, ref, estrellas, detalle, valor=None):
    return conn.execute(text("""INSERT INTO desafio_eventos (persona_id, fecha, tipo, ref_id, estrellitas, valor, detalle)
                                VALUES (:p, :f, :t, :r, :e, :v, :d) ON CONFLICT DO NOTHING RETURNING id"""),
                        {"p": int(persona_id), "f": fecha, "t": tipo, "r": int(ref), "e": int(estrellas),
                         "v": valor, "d": detalle}).scalar()


def otorgar_excursiones(persona_id, hoy):
    """Estrellitas de la Excursión, una vez cada una (idempotente): lista guardada
    (+10), viaje anotado (+10, planificado o de urgencia igual) y lista cumplida
    (+20 si, cerrada su semana, se compró al menos el 80 % de lo llevado; si no, se
    anota con 0). Devuelve textos de lo nuevo."""
    if not es_participante(persona_id) or not db.hay_registro_viajes():
        return []
    hoy = pd.Timestamp(hoy).date()
    nuevos = []
    listas = pd.read_sql(text("SELECT id, fecha FROM listas_compra WHERE persona_id = :p"), db.engine,
                         params={"p": int(persona_id)})
    viajes = pd.read_sql(text("SELECT id, fecha, tipo, lista_id FROM viajes_compra WHERE persona_id = :p"), db.engine,
                         params={"p": int(persona_id)})
    with db.engine.begin() as conn:
        for r in listas.itertuples():
            if _evento(conn, persona_id, r.fecha, "lista", r.id, PUNTOS_LISTA, f"Lista del {r.fecha:%d/%m} guardada"):
                nuevos.append(f"+{PUNTOS_LISTA} ⭐ por guardar la lista antes de ir")
        for r in viajes.itertuples():
            if _evento(conn, persona_id, r.fecha, "viaje", r.id, PUNTOS_VIAJE,
                       f"Viaje {r.tipo} del {r.fecha:%d/%m} anotado" + (" con la lista" if pd.notna(r.lista_id) else "")):
                nuevos.append(f"+{PUNTOS_VIAJE} ⭐ por anotar el viaje")
    # lista cumplida: con la semana de la lista ya cerrada
    if not listas.empty:
        import lista_compras as lc
        ventanas = lc.ventanas_listas(db.listas_compra(), db.dias_ventana_lista())
        with db.engine.connect() as conn:
            hechas = {r[0] for r in conn.execute(text("""SELECT ref_id FROM desafio_eventos WHERE persona_id = :p
                                                         AND tipo = 'lista_cumplida'"""), {"p": int(persona_id)})}
        for r in ventanas[ventanas["id"].isin(listas["id"]) & ~ventanas["id"].isin(hechas)].itertuples():
            if r.hasta >= hoy:
                continue
            _, res, _ = db.comparar_lista_compra(r.id)
            pct = res.get("pct_lista_comprada")
            ok = pct is not None and pct >= PCT_LISTA_CUMPLIDA
            with db.engine.begin() as conn:
                if _evento(conn, persona_id, r.hasta, "lista_cumplida", r.id, PUNTOS_LISTA_CUMPLIDA if ok else 0,
                           f"Lista del {r.fecha:%d/%m}: {0 if pct is None else pct * 100:.0f} % comprado",
                           valor=None if pct is None else round(pct * 100, 1)) and ok:
                    nuevos.append(f"+{PUNTOS_LISTA_CUMPLIDA} ⭐ ¡lista cumplida! (lista del {r.fecha:%d/%m})")
    return nuevos


# ----------------------------------------------------------------------------
# Fase 2 · Cuentas: cierre del día
# ----------------------------------------------------------------------------
def evaluar_cierres(persona_id, hoy, dias=7):
    """+10 por cada cierre del día guardado por la persona el mismo día (o a la
    madrugada siguiente) con el efectivo contado y el pan resuelto (sobrante
    cargado, "no sobró" o no se compró pan). No importa si la caja cierra justo.
    Idempotente. Devuelve textos de lo nuevo."""
    if not es_participante(persona_id) or not db.hay_cierres():
        return []
    hoy = pd.Timestamp(hoy).date()
    cierres = pd.read_sql(text(f"""
        SELECT fecha, contado, pan_sin_sobrante, ({db.SQL_HORA_AR('creado_en')})::date AS dia_guardado
        FROM cierres_caja WHERE persona_id = :p AND fecha >= :d
    """), db.engine, params={"p": int(persona_id), "d": hoy - dt.timedelta(days=dias)})
    nuevos = []
    for r in cierres.itertuples():
        fecha = pd.Timestamp(r.fecha).date()
        a_tiempo = 0 <= (pd.Timestamp(r.dia_guardado).date() - fecha).days <= 1
        if r.contado is None or not a_tiempo:
            continue
        if not (r.pan_sin_sobrante or db.pan_resuelto(fecha)):
            continue
        with db.engine.begin() as conn:
            if _evento(conn, persona_id, fecha, "cierre", _ref_dia(fecha), PUNTOS_CIERRE,
                       f"Cierre del día {fecha:%d/%m}"):
                nuevos.append(f"+{PUNTOS_CIERRE} ⭐ por el cierre del {fecha:%d/%m}")
    return nuevos


# ----------------------------------------------------------------------------
# Fase 2 · Libreta: las cuatro materias de una semana
# ----------------------------------------------------------------------------
def _nota(hecho, meta):
    if not meta:
        return None
    return max(1, min(10, int(round(10 * hecho / meta))))


def libreta(persona_id, lunes_semana, hoy=None):
    """Por materia: hecho, meta, nota (1 a 10, None si no corresponde) y texto.
    En la semana en curso la meta son los días transcurridos."""
    hoy = pd.Timestamp(hoy or lunes_semana + dt.timedelta(days=6)).date()
    fin = min(lunes_semana + dt.timedelta(days=6), hoy)
    dias = (fin - lunes_semana).days + 1
    p = {"p": int(persona_id), "d": lunes_semana, "h": fin}
    with db.engine.connect() as conn:
        tareas = conn.execute(text("""SELECT COUNT(*) FROM desafio_eventos WHERE persona_id = :p AND tipo = 'tarea'
                                      AND fecha BETWEEN :d AND :h"""), p).scalar()
        conteos = conn.execute(text("""SELECT COUNT(*), COUNT(*) FILTER (WHERE conteo_coincide)
                                       FROM controles_vencimiento WHERE persona_id = :p AND conteo IS NOT NULL
                                       AND fecha BETWEEN :d AND :h"""), p).first()
        datos_asig = conn.execute(text("""SELECT COUNT(*) FROM desafio_tareas WHERE persona_id = :p AND tipo = 'dato'
                                          AND fecha BETWEEN :d AND :h"""), p).scalar()
        datos_ok = conn.execute(text("""SELECT COUNT(*) FROM desafio_eventos WHERE persona_id = :p AND tipo = 'dato'
                                        AND estrellitas > 0 AND fecha BETWEEN :d AND :h"""), p).scalar()
        viajes = conn.execute(text("""SELECT COUNT(*), COUNT(lista_id) FROM viajes_compra WHERE persona_id = :p
                                      AND fecha BETWEEN :d AND :h"""), p).first() if db.hay_registro_viajes() else (0, 0)
        cierres = conn.execute(text("""SELECT COUNT(*) FROM desafio_eventos WHERE persona_id = :p AND tipo = 'cierre'
                                       AND fecha BETWEEN :d AND :h"""), p).scalar()
    exactos = f" · {conteos[1]} de {conteos[0]} conteos exactos" if conteos[0] else ""
    if viajes[0]:
        nota_exc = 10 if viajes[1] else 7
        txt_exc = f"{viajes[0]} viaje(s), {viajes[1]} con la lista"
    else:
        nota_exc, txt_exc = None, "sin viajes esta semana"
    return {
        "gondola": {"hecho": tareas, "meta": dias, "nota": _nota(tareas, dias),
                    "texto": f"{tareas} de {dias} días con la tarea{exactos}"},
        "orden": {"hecho": datos_ok, "meta": datos_asig, "nota": _nota(datos_ok, datos_asig),
                  "texto": f"{datos_ok} de {datos_asig} datos arreglados" if datos_asig else "sin datos para arreglar"},
        "excursion": {"hecho": viajes[1], "meta": viajes[0], "nota": nota_exc, "texto": txt_exc},
        "cuentas": {"hecho": cierres, "meta": dias, "nota": _nota(cierres, dias),
                    "texto": f"{cierres} de {dias} cierres del día"},
    }


def semana_en_verde():
    """Alguna semana con el indicador de calidad guardado en 95 % o más."""
    try:
        h = db.historial_calidad()
    except Exception:
        return False
    if h is None or h.empty or "indicador" not in h:
        return False
    return bool((pd.to_numeric(h["indicador"], errors="coerce") >= INDICE_SEMANA_EN_VERDE).any())
