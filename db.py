"""
Capa de acceso a datos para el sistema de gestión del almacén.
Toda la lógica de negocio (cálculo de precios, alertas, etc.) vive acá,
separada de la interfaz (app.py), para poder reusarla después desde
otro frontend, un script de carga automática, o tests.
"""
import contextvars
import datetime as dt
import hashlib
import hmac
import math
import re
import os
import secrets
import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text, event
from sqlalchemy.exc import IntegrityError

def _config(key, default=None):
    """
    Busca la config primero en st.secrets (panel "Secrets" de Streamlit
    Community Cloud, o archivo .streamlit/secrets.toml en local) y si no
    está definida ahí, en variables de entorno. Las credenciales reales
    NUNCA van hardcodeadas acá ni se suben al repo — ver
    .streamlit/secrets.toml.example para el formato.
    """
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        # No hay secrets.toml configurado (por ej. corriendo local sin uno) -> cae a env vars.
        pass
    return os.environ.get(key, default)

def get_engine():
    """
    Arma la conexión a partir de st.secrets / variables de entorno, con
    defaults razonables para desarrollo local. En producción (Streamlit
    Community Cloud), configurar como secret: DB_HOST, DB_PORT, DB_NAME,
    DB_USER, DB_PASSWORD.
    """
    host = _config("DB_HOST", "localhost")
    port = _config("DB_PORT", "5432")
    name = _config("DB_NAME", "almacen")
    user = _config("DB_USER", "postgres")
    password = _config("DB_PASSWORD", "postgres")
    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"
    engine = create_engine(url, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def _fijar_search_path(dbapi_connection, connection_record):
        """
        Fuerza 'search_path' a un valor sano en cada conexión nueva que
        abre el pool. Necesario porque, al pasar por un connection pooler
        (ej. el pooler de Neon en modo transacción), una conexión física
        puede reutilizarse entre distintos clientes sin resetear el
        estado de sesión — si algo dejó el search_path vacío o raro en esa
        conexión de fondo, cualquier tabla sin calificar (ej. "articulos"
        en vez de "public.articulos") deja de encontrarse. Esto lo corrige
        apenas se abre la conexión, sin depender de qué haya pasado antes.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute('SET search_path TO "$user", public')
        # Huso horario (tanda 20). Antes se fijaba el de Argentina, pero con el
        # pooler de Neon (modo transacción) ese SET no llegaba a las consultas
        # siguientes: en producción todo se guardó en UTC (creado_en,
        # actualizado_en, historial_cambios.fecha: 99,6 % de las ventas
        # desde el 19/10 tienen creado_en = hora argentina + 3 h). Se fija
        # UTC para que valga lo mismo en todos lados (y en las copias de
        # prueba): las fechas y horas se GUARDAN en UTC y se pasan a hora
        # argentina al mostrarlas (a_hora_argentina / SQL_HORA_AR). Las
        # columnas de fecha (ventas.fecha, facturas.fecha...) las pone la
        # app con el día argentino, y "hoy" adentro de una consulta es
        # HOY_SQL, no CURRENT_DATE (que en UTC es "mañana" después de las 21 h).
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.close()

    return engine

engine = get_engine()

# Tanda 20: las fechas y horas se guardan en UTC (ver get_engine).
HOY_SQL = "(now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date"   # "hoy" en Argentina, en SQL


def SQL_HORA_AR(columna):
    """Expresión SQL que pasa un TIMESTAMP guardado en UTC a hora argentina."""
    return f"(({columna}) AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires')"


def a_hora_argentina(serie):
    """Serie de pandas con TIMESTAMP guardados en UTC -> hora argentina (sin zona, para mostrar)."""
    t = pd.to_datetime(serie)
    return t.dt.tz_localize("UTC").dt.tz_convert("America/Argentina/Buenos_Aires").dt.tz_localize(None)


def _cant_txt(valor):
    """Cantidad para textos del historial: 2 decimales como siempre ("0.25",
    "13.00") y 3 solo cuando hacen falta ("0.357"), desde que la base guarda 3."""
    x = float(valor)
    return f"{x:.2f}" if round(x, 2) == round(x, 3) else f"{x:.3f}"

def _registrar_si_stock_negativo(conn, articulo_id, contexto):
    """
    El stock SÍ puede quedar negativo a propósito (se vendió algo cuya
    factura de compra todavía no se cargó; se ajusta solo cuando esa
    factura entra) -- no se bloquea ninguna operación por esto. Lo único
    que hace esta función es chequear el stock resultante después de la
    operación y, si quedó negativo, dejar constancia en historial_cambios
    y devolver un texto de aviso para mostrar en la app (no bloqueante).
    Devuelve None si el stock quedó en 0 o positivo.
    """
    fila = conn.execute(
        text("SELECT nombre, stock_actual FROM articulos WHERE id = :id"),
        {"id": articulo_id}
    ).mappings().first()
    if not fila or float(fila["stock_actual"]) >= 0:
        return None

    aviso = (
        f"⚠️ '{fila['nombre']}' quedó con stock {float(fila['stock_actual']):g} "
        f"({contexto}) -- se ajusta solo cuando cargues la factura de compra correspondiente."
    )
    registrar_historial(
        "articulos", articulo_id, "STOCK_NEGATIVO",
        f"Stock negativo ({contexto}) en '{fila['nombre']}': {_cant_txt(fila['stock_actual'])}",
        conn=conn
    )
    return aviso

def _componentes_promocion_virtual(conn, articulo_id):
    """Si articulo_id es una promoción 'virtual', devuelve los ids de sus
    componentes; para cualquier otro artículo (incluidas las promociones
    'fisica'), lista vacía."""
    fila = conn.execute(
        text("SELECT categoria, promo_tipo FROM articulos WHERE id = :id"),
        {"id": articulo_id}
    ).mappings().first()
    if not fila or fila["categoria"] != CATEGORIA_PROMOCION or fila["promo_tipo"] != "virtual":
        return []
    return conn.execute(
        text("SELECT componente_articulo_id FROM promocion_componentes WHERE promocion_articulo_id = :id"),
        {"id": articulo_id}
    ).scalars().all()

def _avisos_stock_negativo_venta(conn, articulo_id, contexto):
    """
    Lista de avisos de stock negativo relevantes para una venta de
    articulo_id. Si articulo_id es un combo de stock compartido, el stock
    que realmente se movió (ver fn_aplicar_venta / fn_editar_venta) es el
    de sus componentes, así que se chequean ellos en vez del artículo-
    promoción (que no tiene stock propio). Siempre devuelve una lista
    (vacía si no hay nada que avisar).
    """
    componentes = _componentes_promocion_virtual(conn, articulo_id)
    if componentes:
        avisos = []
        for comp_id in componentes:
            aviso = _registrar_si_stock_negativo(conn, comp_id, contexto)
            if aviso:
                avisos.append(aviso)
        return avisos
    aviso = _registrar_si_stock_negativo(conn, articulo_id, contexto)
    return [aviso] if aviso else []

# ------------------------------------------------------------
# HISTORIAL DE CAMBIOS (auditoría liviana)
# ------------------------------------------------------------
# ------------------------------------------------------------
# PERSONA QUE ATIENDE (Sprint 2)
# La persona que ingresó (usuario y contraseña, Sprint 2 / tanda 10) queda
# guardada acá durante la ejecución de la página, para
# que registrar_historial() y las ventas sepan quién hizo cada cosa sin
# tener que pasar el dato por cada función. Se usa una ContextVar (y no
# una variable global) porque Streamlit atiende varias sesiones a la vez
# en hilos distintos: así cada sesión ve solo su propia persona.
# ------------------------------------------------------------
_persona_actual = contextvars.ContextVar("persona_actual", default=None)


def set_persona_actual(persona_id=None, nombre=None):
    """Fija quién atiende en esta sesión (None, None = nadie elegido)."""
    _persona_actual.set({"id": persona_id, "nombre": nombre} if persona_id else None)


def persona_actual():
    """Devuelve {'id', 'nombre'} de quien atiende, o None si no se eligió."""
    return _persona_actual.get()


def registrar_historial(tabla, registro_id, accion, detalle, conn=None):
    """
    Deja constancia legible de un cambio sensible (precio, stock corregido
    a mano, venta eliminada, promoción armada). Si se pasa 'conn', inserta
    dentro de esa misma transacción ya abierta (para que quede todo o nada
    junto con el cambio real); si no, abre una transacción propia.
    'usuario' es quien atiende en la sesión (ver set_persona_actual); queda
    NULL en scripts de mantenimiento o si no se eligió a nadie.
    """
    query = text("""
        INSERT INTO historial_cambios (tabla, registro_id, accion, detalle, usuario)
        VALUES (:tabla, :registro_id, :accion, :detalle, :usuario)
    """)
    persona = persona_actual()
    params = {"tabla": tabla, "registro_id": registro_id, "accion": accion, "detalle": detalle,
              "usuario": persona["nombre"] if persona else None}
    if conn is not None:
        conn.execute(query, params)
    else:
        with engine.begin() as c:
            c.execute(query, params)

def listar_historial(desde=None, hasta=None, tabla=None, limite=500):
    """Historial con la fecha en hora argentina (se guarda en UTC; tanda 20).
    desde/hasta son días argentinos."""
    fecha_ar = SQL_HORA_AR("fecha")
    query = (f"SELECT id, {fecha_ar} AS fecha, tabla, registro_id, accion, detalle, usuario "
             "FROM historial_cambios WHERE 1=1")
    params = {}
    if desde:
        query += f" AND {fecha_ar}::date >= :desde"
        params["desde"] = desde
    if hasta:
        query += f" AND {fecha_ar}::date <= :hasta"
        params["hasta"] = hasta
    if tabla:
        query += " AND tabla = :tabla"
        params["tabla"] = tabla
    query += " ORDER BY fecha DESC LIMIT :limite"
    params["limite"] = limite
    return pd.read_sql(text(query), engine, params=params)

# ------------------------------------------------------------
# PROVEEDORES
# ------------------------------------------------------------
def listar_proveedores():
    return pd.read_sql("SELECT * FROM proveedores ORDER BY nombre", engine)

def crear_proveedor(nombre, cuit=None, telefono=None):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO proveedores (nombre, cuit, telefono) VALUES (:n, :c, :t)"),
            {"n": nombre, "c": cuit, "t": telefono}
        )

# ------------------------------------------------------------
# ARTICULOS
# ------------------------------------------------------------
def listar_articulos(incluir_inactivos=False):
    """
    Catálogo. Por defecto solo los artículos activos: es lo que se usa para
    vender, cargar compras, armar promociones y sugerir reposición. Con
    incluir_inactivos=True trae también los dados de baja (Artículos ->
    Editar artículo, para poder reactivarlos, y la edición de ventas viejas).
    """
    filtro = "" if incluir_inactivos else "WHERE v.activo "
    # en_consignacion (tanda 14): todos sus proveedores son en consignación (la mercadería
    # está en el local pero se paga cuando se vende). Vía jsonb para no fallar en una base
    # sin migracion_tanda14.py.
    return pd.read_sql(f"""
        SELECT v.*, COALESCE(c.en_consignacion, FALSE) AS en_consignacion
        FROM v_articulos v
        LEFT JOIN (
            SELECT app.articulo_id,
                   bool_and(COALESCE((to_jsonb(p) ->> 'consignacion')::boolean, FALSE)) AS en_consignacion
            FROM articulo_proveedor_precio app JOIN proveedores p ON p.id = app.proveedor_id
            GROUP BY app.articulo_id
        ) c ON c.articulo_id = v.id
        {filtro}ORDER BY v.nombre""", engine)

MOTIVOS_BAJA = {
    "discontinuado": "Discontinuado (no se trabaja más)",
    "reemplazado": "Reemplazado por otro artículo",
    "temporal": "Temporal (sin proveedor / fuera de temporada)",
}

def crear_articulo(nombre, categoria, costo_compra, margen, iva, stock_inicial, stock_minimo, fecha_vencimiento, acceso_rapido=False, unidad_medida="unidad", marca=None, peso_bolsa_gramos=None):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO articulos
                (nombre, marca, categoria, costo_compra_actual, margen_ganancia, iva,
                 stock_actual, stock_minimo, fecha_vencimiento, acceso_rapido, unidad_medida, peso_bolsa_gramos)
            VALUES (:nombre, :marca, :categoria, :costo, :margen, :iva, :stock, :stock_min, :venc, :rapido, :unidad, :peso_bolsa)
        """), {
            "nombre": nombre, "marca": marca, "categoria": categoria, "costo": costo_compra,
            "margen": margen, "iva": iva, "stock": stock_inicial,
            "stock_min": stock_minimo, "venc": fecha_vencimiento, "rapido": acceso_rapido,
            "unidad": unidad_medida, "peso_bolsa": peso_bolsa_gramos
        })

def buscar_articulo_por_nombre_exacto(nombre, excluir_id=None):
    """
    Chequeo de duplicados: busca artículos existentes con el mismo nombre
    (sin importar mayúsculas). excluir_id sirve para cuando se está editando
    un artículo y no hay que compararlo contra sí mismo.
    """
    query = "SELECT id, nombre FROM articulos WHERE LOWER(nombre) = LOWER(:n)"
    params = {"n": nombre}
    if excluir_id is not None:
        query += " AND id != :excluir_id"
        params["excluir_id"] = excluir_id
    return pd.read_sql(text(query), engine, params=params)

def listar_categorias():
    """
    Categorías ya usadas en el catálogo, normalizadas (primera letra en
    mayúscula y el resto en minúscula, sin espacios de más) y sin
    repetidos -- para armar el desplegable de categoría en "Nuevo
    artículo" y "Editar artículo" (evita que se sigan cargando variantes
    tipo "BEBIDAS", "Bebidas", "bebidas " como si fueran categorías
    distintas).
    """
    df = pd.read_sql(
        "SELECT DISTINCT categoria FROM articulos "
        "WHERE categoria IS NOT NULL AND TRIM(categoria) != ''",
        engine
    )
    normalizadas = {
        c.strip().capitalize() for c in df["categoria"] if c and c.strip()
    }
    # "Promocion" es una categoría interna (se la ponemos nosotros a cada
    # promoción creada) -- no tiene sentido ofrecerla como categoría para
    # un artículo común en "Nuevo artículo"/"Editar artículo".
    normalizadas.discard(CATEGORIA_PROMOCION)
    return sorted(normalizadas)

def normalizar_categorias_existentes():
    """
    Migración de datos (se corre una sola vez, o cada vez que se quiera
    volver a limpiar): pasa TODAS las categorías ya cargadas en articulos
    al formato "primera letra mayúscula, el resto minúscula", sin espacios
    de más. Devuelve la cantidad de filas modificadas.

    Al normalizar, categorías que solo diferían en mayúsculas/espacios
    (ej. "BEBIDAS " y "Bebidas") quedan unificadas en un mismo valor --
    es el efecto buscado, no un bug.
    """
    with engine.begin() as conn:
        filas = conn.execute(text(
            "SELECT id, categoria FROM articulos "
            "WHERE categoria IS NOT NULL AND TRIM(categoria) != ''"
        )).mappings().all()

        cambios = 0
        for fila in filas:
            actual = fila["categoria"]
            normalizada = actual.strip().capitalize()
            if normalizada != actual:
                conn.execute(
                    text("UPDATE articulos SET categoria = :c, actualizado_en = now() WHERE id = :id"),
                    {"c": normalizada, "id": fila["id"]}
                )
                cambios += 1
        return cambios

def tiene_movimientos(articulo_id):
    """
    True si el artículo ya tiene compras, ventas o fraccionamientos
    registrados. Se usa para advertir antes de cambiar su unidad de medida:
    el stock y los movimientos ya cargados quedaron calculados con la
    unidad vieja, cambiarla no los recalcula.
    """
    query = text("""
        SELECT
            EXISTS(SELECT 1 FROM detalle_factura WHERE articulo_id = :id)
            OR EXISTS(SELECT 1 FROM ventas WHERE articulo_id = :id)
            OR EXISTS(
                SELECT 1 FROM fraccionamientos
                WHERE articulo_bolsa_id = :id OR articulo_derivado_id = :id
            ) AS tiene
    """)
    with engine.connect() as conn:
        return bool(conn.execute(query, {"id": articulo_id}).scalar())

def promociones_que_lo_usan(articulo_id):
    """Promociones activas que tienen a este artículo como componente."""
    return pd.read_sql(text("""
        SELECT p.id, p.nombre FROM promocion_componentes pc
        JOIN articulos p ON p.id = pc.promocion_articulo_id
        WHERE pc.componente_articulo_id = :id AND p.activo
        ORDER BY p.nombre
    """), engine, params={"id": articulo_id})

def cambiar_estado_articulo(articulo_id, activo, motivo_baja=None, reemplazado_por=None):
    """
    Baja lógica / reactivación. Al dar de baja guarda la fecha (hoy, hora
    argentina de la base) y el motivo; al reactivar limpia los tres campos.
    Si el artículo ya estaba inactivo y solo se cambia el motivo o el
    reemplazo, la fecha de baja original se conserva. Deja registro en el
    historial de cambios. No toca el stock: si le queda, sigue apareciendo
    en Calidad de datos como "Inactivo con stock" hasta que se liquide.
    """
    if not activo and motivo_baja not in MOTIVOS_BAJA:
        raise ValueError("Motivo de baja inválido.")
    if reemplazado_por is not None and int(reemplazado_por) == int(articulo_id):
        raise ValueError("Un artículo no puede reemplazarse a sí mismo.")
    if activo:
        motivo_baja, reemplazado_por = None, None
    elif motivo_baja != "reemplazado":
        reemplazado_por = None
    with engine.begin() as conn:
        anterior = conn.execute(
            text("SELECT nombre, activo, motivo_baja, reemplazado_por FROM articulos WHERE id = :id"),
            {"id": articulo_id}
        ).mappings().first()
        if anterior is None:
            return
        sin_cambios = (
            bool(anterior["activo"]) == bool(activo)
            and anterior["motivo_baja"] == motivo_baja
            and anterior["reemplazado_por"] == (None if reemplazado_por is None else int(reemplazado_por))
        )
        if sin_cambios:
            return
        conn.execute(text("""
            UPDATE articulos
            SET activo = :activo, motivo_baja = :motivo, reemplazado_por = :reemp,
                fecha_baja = CASE WHEN :activo THEN NULL
                                  ELSE COALESCE(fecha_baja, (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date) END,
                actualizado_en = now()
            WHERE id = :id
        """), {"activo": bool(activo), "motivo": motivo_baja,
               "reemp": None if reemplazado_por is None else int(reemplazado_por),
               "id": articulo_id})
        if activo:
            detalle = f"Artículo '{anterior['nombre']}' reactivado"
        else:
            detalle = f"Artículo '{anterior['nombre']}' dado de baja: {MOTIVOS_BAJA[motivo_baja]}"
            if reemplazado_por is not None:
                nuevo = conn.execute(text("SELECT nombre FROM articulos WHERE id = :id"),
                                     {"id": int(reemplazado_por)}).scalar()
                detalle += f" -> '{nuevo}'"
        registrar_historial("articulos", articulo_id, "EDICION", detalle, conn=conn)

def actualizar_articulo(articulo_id, nombre, marca, categoria, unidad_medida,
                         fecha_vencimiento, nuevo_stock_actual):
    """
    Edición completa de los datos descriptivos de un artículo (nombre,
    marca, categoría, unidad de medida, vencimiento) y una corrección
    manual de su stock actual -- por ejemplo, después de un control físico.
    A diferencia de las compras/ventas, esto pisa el stock directamente,
    sin afectar el historial de movimientos ya registrado.
    """
    with engine.begin() as conn:
        anterior = conn.execute(
            text("SELECT nombre, stock_actual, unidad_medida FROM articulos WHERE id = :id"),
            {"id": articulo_id}
        ).mappings().first()

        conn.execute(text("""
            UPDATE articulos
            SET nombre = :nombre, marca = :marca, categoria = :categoria,
                unidad_medida = :unidad, fecha_vencimiento = :venc,
                stock_actual = :stock, actualizado_en = now()
            WHERE id = :id
        """), {
            "nombre": nombre, "marca": marca, "categoria": categoria,
            "unidad": unidad_medida, "venc": fecha_vencimiento,
            "stock": nuevo_stock_actual, "id": articulo_id
        })

        # El cambio de unidad cambia cómo se vende (gramos o unidades): queda registrado.
        if anterior and anterior["unidad_medida"] != unidad_medida:
            registrar_historial(
                "articulos", articulo_id, "EDICION",
                f"Unidad de medida cambiada en '{anterior['nombre']}': "
                f"{anterior['unidad_medida']} -> {unidad_medida}",
                conn=conn
            )
        if anterior and float(anterior["stock_actual"]) != float(nuevo_stock_actual):
            registrar_historial(
                "articulos", articulo_id, "EDICION",
                f"Stock corregido a mano en '{anterior['nombre']}': "
                f"{_cant_txt(anterior['stock_actual'])} -> {nuevo_stock_actual}",
                conn=conn
            )

def eliminar_articulo(articulo_id):
    """
    Borra un artículo. Falla si tiene compras o ventas asociadas (para no
    perder historial ni descuadrar reportes) — en ese caso hay que borrar
    primero esos movimientos, o dejar el artículo aunque no se use más.
    Devuelve (True, None) si se borró, o (False, mensaje) si no se pudo.
    """
    try:
        with engine.begin() as conn:
            a = conn.execute(text("SELECT nombre, marca, categoria FROM articulos WHERE id = :id"),
                             {"id": articulo_id}).mappings().first()
            conn.execute(text("DELETE FROM articulos WHERE id = :id"), {"id": articulo_id})
            if a is not None:
                que = "Promoción" if a["categoria"] == CATEGORIA_PROMOCION else "Artículo"
                registrar_historial(
                    "articulos", articulo_id, "ELIMINACION",
                    f"{que} #{articulo_id} eliminado: {a['nombre']}" + (f" ({a['marca']})" if a["marca"] else ""),
                    conn=conn,
                )
        return True, None
    except IntegrityError:
        return False, (
            "No se puede eliminar: este artículo ya tiene compras o ventas registradas. "
            "Borrar esas facturas/ventas primero, o dejarlo sin usar en vez de eliminarlo."
        )

def actualizar_precio_articulo(articulo_id, nuevo_costo=None, nuevo_margen=None,
                                nuevo_peso_bolsa_gramos=None, actualizar_peso_bolsa=False):
    """
    actualizar_peso_bolsa=True fuerza a escribir nuevo_peso_bolsa_gramos aunque sea
    None (para poder LIMPIAR el campo a NULL) — a diferencia de costo/margen, que
    solo se tocan si se pasa un valor.
    """
    sets, params = [], {"id": articulo_id}
    if nuevo_costo is not None:
        sets.append("costo_compra_actual = :costo")
        params["costo"] = nuevo_costo
    if nuevo_margen is not None:
        sets.append("margen_ganancia = :margen")
        params["margen"] = nuevo_margen
    if actualizar_peso_bolsa:
        sets.append("peso_bolsa_gramos = :peso_bolsa")
        params["peso_bolsa"] = nuevo_peso_bolsa_gramos
    if not sets:
        return
    sets.append("actualizado_en = now()")
    query = f"UPDATE articulos SET {', '.join(sets)} WHERE id = :id"
    with engine.begin() as conn:
        anterior = conn.execute(
            text("SELECT nombre, costo_compra_actual, margen_ganancia FROM articulos WHERE id = :id"),
            {"id": articulo_id}
        ).mappings().first()

        conn.execute(text(query), params)

        if anterior:
            cambios = []
            if nuevo_costo is not None and float(anterior["costo_compra_actual"]) != float(nuevo_costo):
                cambios.append(f"costo ${anterior['costo_compra_actual']} -> ${nuevo_costo}")
            if nuevo_margen is not None and float(anterior["margen_ganancia"]) != float(nuevo_margen):
                cambios.append(
                    f"margen {float(anterior['margen_ganancia']) * 100:.1f}% -> {float(nuevo_margen) * 100:.1f}%"
                )
            if cambios:
                registrar_historial(
                    "articulos", articulo_id, "EDICION",
                    f"Precio/margen actualizado en '{anterior['nombre']}': " + "; ".join(cambios),
                    conn=conn
                )

def alertas_stock_bajo():
    return pd.read_sql(
        "SELECT * FROM v_articulos WHERE stock_bajo = TRUE AND activo ORDER BY stock_actual ASC", engine
    )

def alertas_vencimiento():
    return pd.read_sql(
        "SELECT * FROM v_articulos WHERE vencimiento_cercano = TRUE AND activo ORDER BY fecha_vencimiento ASC", engine
    )

def articulos_stock_negativo():
    """
    Artículos vendidos (o fraccionados/armados) de más respecto de lo que
    la base tenía cargado como comprado -- pendientes de que se cargue la
    factura de compra que los ajuste. No es un error a corregir a mano,
    es información para hacer seguimiento.
    """
    return pd.read_sql(
        # Los combos de stock compartido no tienen stock propio: el suyo se calcula
        # de los componentes y repetiría el problema del componente (tanda 12).
        "SELECT * FROM v_articulos WHERE stock_actual < 0 AND promo_tipo IS DISTINCT FROM 'virtual' "
        "ORDER BY stock_actual ASC", engine
    )

# ------------------------------------------------------------
# FACTURAS / COMPRAS
# ------------------------------------------------------------
def _ajustar_margen_precio_redondo(conn, articulo_id, redondeo=100):
    """
    Después de que una compra actualizó costo_compra_actual de un
    artículo (actualiza_precio=True), recalcula el margen para que el
    precio de venta final (con IVA) quede en un múltiplo de `redondeo`
    (por defecto $100) -- así ninguna carga de factura deja un precio
    con vuelto chico. El costo no se toca acá (ya lo dejó la compra);
    solo se ajusta el margen para "afinar" el precio final.

    Fix margen 0 (23/10/2026): antes, si redondear al $100 MÁS CERCANO
    dejaba el precio por debajo del costo con IVA (margen chico + redondeo
    para abajo), se clampeaba el margen a 0% y el artículo quedaba
    vendiéndose al costo con IVA. Peor: una vez en 0%, cada factura
    siguiente lo volvía a dejar en 0% (4537,50 -> redondeo 4500 -> clamp).
    Ahora, en ese caso se redondea al múltiplo de $100 de ARRIBA: el margen
    queda chico pero positivo, nunca 0 por efecto del redondeo.

    Devuelve un aviso (string) si el artículo quedó con margen muy bajo o
    venía con margen 0% (revisión manual del precio), o None si no.
    """
    fila = conn.execute(text("""
        SELECT nombre, costo_compra_actual, margen_ganancia, iva
        FROM articulos WHERE id = :id
    """), {"id": articulo_id}).mappings().first()
    if not fila or float(fila["costo_compra_actual"]) <= 0:
        return None  # sin costo cargado no hay precio que redondear

    nombre = fila["nombre"]
    costo = float(fila["costo_compra_actual"])
    margen_actual = float(fila["margen_ganancia"])
    iva = float(fila["iva"])

    costo_con_iva = costo * (1 + iva)
    precio_bruto = costo_con_iva * (1 + max(margen_actual, 0.0))
    precio_redondo = round(precio_bruto / redondeo) * redondeo
    if precio_redondo < costo_con_iva + 1:  # +$1: que no quede en el costo por centavos
        # El redondeo al más cercano cayó al costo o por debajo: se sube al
        # múltiplo de arriba en vez de dejar el margen en 0%.
        precio_redondo = (math.floor((costo_con_iva + 1) / redondeo) + 1) * redondeo
    if precio_redondo <= 0:
        return None

    margen_nuevo = precio_redondo / costo_con_iva - 1
    aviso = None
    if margen_actual <= 0:
        aviso = (
            f"⚠️ '{nombre}': venía con margen {margen_actual * 100:.2f}% (sin ganancia). "
            f"Con el costo nuevo el precio se redondeó para arriba a ${precio_redondo:,.0f} "
            f"(margen {margen_nuevo * 100:.2f}%) -- revisá el margen en "
            f"'Actualizar precio / margen'."
        )
    elif margen_nuevo < margen_actual / 2:
        aviso = (
            f"⚠️ '{nombre}': el redondeo dejó el margen en {margen_nuevo * 100:.2f}% "
            f"(antes {margen_actual * 100:.2f}%) -- revisá el precio de venta a mano en "
            f"'Actualizar precio / margen'."
        )

    conn.execute(text("""
        UPDATE articulos SET margen_ganancia = :margen, actualizado_en = now()
        WHERE id = :id
    """), {"margen": margen_nuevo, "id": articulo_id})

    if abs(margen_nuevo - margen_actual) > 1e-9:
        registrar_historial(
            "articulos", articulo_id, "REPRECIO_FACTURA",
            f"'{nombre}': margen ajustado de {margen_actual * 100:.2f}% a {margen_nuevo * 100:.2f}% "
            f"para que el precio de venta quede redondo (costo nuevo ${costo:,.2f})",
            conn=conn
        )
    return aviso

def crear_factura(proveedor_id, numero, fecha, items, origen="manual", archivo_original=None, ref_externa=None):
    """
    items: lista de dicts {articulo_id, cantidad, costo_unitario,
    actualiza_precio (opcional, default True)}.

    Inserta la factura y su detalle. El trigger trg_aplicar_compra sube
    el stock siempre, y -- solo para los ítems con actualiza_precio=True
    (el caso normal) -- actualiza costo_compra_actual del artículo y el
    costo vigente de ese proveedor. Para esos mismos ítems, después se
    reajusta el margen acá (_ajustar_margen_precio_redondo) para que el
    precio de venta final quede en un múltiplo de $100.

    Un ítem con actualiza_precio=False (compra puntual: descuento del
    proveedor, oferta única) suma stock y queda registrado en la factura
    con el costo real que se pagó, pero no toca el costo de referencia
    ni el precio de venta del artículo -- la próxima compra "normal"
    sigue partiendo del costo de siempre.

    ref_externa (opcional): identificador en el sistema de origen cuando la
    factura se importa (ej. 'planilla:C-00012'). Es único en la base, así
    que importar dos veces la misma factura falla en vez de duplicar stock.

    Devuelve (factura_id, avisos): avisos es una lista de strings (por
    si algún margen se tuvo que clampear a 0%) para mostrar en la UI.
    """
    avisos = []
    with engine.begin() as conn:
        if ref_externa is None:
            factura_id = conn.execute(text("""
                INSERT INTO facturas (proveedor_id, numero, fecha, origen, archivo_original)
                VALUES (:prov, :num, :fecha, :origen, :archivo)
                RETURNING id
            """), {
                "prov": proveedor_id, "num": numero, "fecha": fecha,
                "origen": origen, "archivo": archivo_original
            }).scalar()
        else:
            factura_id = conn.execute(text("""
                INSERT INTO facturas (proveedor_id, numero, fecha, origen, archivo_original, ref_externa)
                VALUES (:prov, :num, :fecha, :origen, :archivo, :ref)
                RETURNING id
            """), {
                "prov": proveedor_id, "num": numero, "fecha": fecha,
                "origen": origen, "archivo": archivo_original, "ref": ref_externa
            }).scalar()

        avisos = _insertar_items_factura(conn, factura_id, items)
    return factura_id, avisos


def _insertar_items_factura(conn, factura_id, items):
    """Inserta los ítems DENTRO de la transacción `conn` (los triggers suben stock y
    costo) y reajusta el precio redondo de los que actualizan precio. Devuelve avisos."""
    avisos = []
    con_venc = _hay_vencimiento_en_factura(conn)
    for item in items:
        actualiza = item.get("actualiza_precio", True)
        venc = item.get("fecha_vencimiento") if con_venc else None
        if venc is not None and not pd.isna(venc):
            conn.execute(text("""
                INSERT INTO detalle_factura (factura_id, articulo_id, cantidad, costo_unitario, actualiza_precio,
                                             fecha_vencimiento)
                VALUES (:fid, :aid, :cant, :costo, :actualiza, :venc)
            """), {
                "fid": factura_id, "aid": item["articulo_id"],
                "cant": item["cantidad"], "costo": item["costo_unitario"],
                "actualiza": actualiza, "venc": venc,
            })
        else:
            conn.execute(text("""
                INSERT INTO detalle_factura (factura_id, articulo_id, cantidad, costo_unitario, actualiza_precio)
                VALUES (:fid, :aid, :cant, :costo, :actualiza)
            """), {
                "fid": factura_id, "aid": item["articulo_id"],
                "cant": item["cantidad"], "costo": item["costo_unitario"],
                "actualiza": actualiza
            })
        if actualiza:
            aviso = _ajustar_margen_precio_redondo(conn, item["articulo_id"])
            if aviso:
                avisos.append(aviso)
        # Tanda 12: si esta mercadería ya se había sumado a mano al recibirla, se
        # descuenta esa suma para que el stock no quede contado dos veces.
        ya_sumado = float(item.get("descontar_ya_sumado") or 0)
        if ya_sumado > 0:
            fila = conn.execute(text("""
                UPDATE articulos SET stock_actual = stock_actual - :x, actualizado_en = now()
                WHERE id = :id RETURNING nombre, stock_actual
            """), {"x": ya_sumado, "id": item["articulo_id"]}).mappings().first()
            registrar_historial(
                "articulos", item["articulo_id"], "AJUSTE_FACTURA",
                f"'{fila['nombre']}': factura #{factura_id} -- se descontaron {_cant_txt(ya_sumado)} que ya "
                f"se habían sumado a mano al recibir la mercadería (stock {_cant_txt(fila['stock_actual'])})",
                conn=conn,
            )
    return avisos


def _hay_vencimiento_en_factura(conn):
    """¿La base ya tiene detalle_factura.fecha_vencimiento? (migracion_tanda18.py).
    Sin la migración la app sigue funcionando: la fecha simplemente no se guarda."""
    return bool(conn.execute(text("""
        SELECT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'detalle_factura' AND column_name = 'fecha_vencimiento')
    """)).scalar())


# ------------------------------------------------------------
# VENCIMIENTOS POR LOTE IMPLÍCITO (tanda 18, 10/2026)
# Sin control de lotes a mano: cada renglón de factura es un lote. Vence en la
# fecha cargada en el renglón (opcional) o, si no, en fecha de la factura +
# días disponibles para la venta (articulos.vida_util_dias, o el valor de su
# categoría en vida_util_categoria). Se supone que se vende primero lo más
# viejo, así que el stock de hoy son las compras más recientes.
# ------------------------------------------------------------
def hay_tanda18():
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('public.vida_util_categoria') IS NOT NULL")).scalar()) \
            and _hay_vencimiento_en_factura(conn)


def vida_util_por_categoria():
    """categoria -> días disponibles para la venta (vacío sin la migración)."""
    if not hay_tanda18():
        return {}
    with engine.connect() as conn:
        return {c: int(d) for c, d in conn.execute(text("SELECT categoria, dias FROM vida_util_categoria"))}


def guardar_vida_util_categorias(valores):
    """valores: dict categoria -> días (None o 0 = quitar). Devuelve cuántas cambiaron."""
    if not hay_tanda18():
        raise ValueError("Falta correr migracion_tanda18.py.")
    actuales = vida_util_por_categoria()
    cambios = 0
    with engine.begin() as conn:
        for cat, dias in valores.items():
            dias = int(dias) if dias is not None and not pd.isna(dias) and int(dias) > 0 else None
            if dias is not None and not (1 <= dias <= 3650):
                raise ValueError(f"{cat}: los días tienen que estar entre 1 y 3650.")
            if actuales.get(cat) == dias:
                continue
            if dias is None:
                conn.execute(text("DELETE FROM vida_util_categoria WHERE categoria = :c"), {"c": cat})
            else:
                conn.execute(text("""
                    INSERT INTO vida_util_categoria (categoria, dias) VALUES (:c, :d)
                    ON CONFLICT (categoria) DO UPDATE SET dias = EXCLUDED.dias, actualizado_en = now()
                """), {"c": cat, "d": dias})
            registrar_historial("vida_util_categoria", None, "VIDA_UTIL_CATEGORIA",
                                f"{cat}: {actuales.get(cat, '-')} -> {dias if dias is not None else '-'} días",
                                conn=conn)
            cambios += 1
    return cambios


def vida_util_efectiva(articulos_df):
    """Serie articulo -> días disponibles (propia o de la categoría; NaN si no hay)."""
    por_cat = vida_util_por_categoria()
    propia = pd.to_numeric(articulos_df.get("vida_util_dias"), errors="coerce")
    de_cat = articulos_df["categoria"].map(por_cat)
    return propia.fillna(de_cat)


def hay_desafio():
    """True si la base tiene la migración de la tanda 21 (Desafío Recreo)."""
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('public.controles_vencimiento') IS NOT NULL")).scalar())


def ultimo_control_por_articulo(articulo_ids=None):
    """Tanda 21: {articulo_id: {"fecha", "vence"}} con el último control de góndola
    que vio una fecha (descarta las repreguntas que no coincidieron). Vacío sin la migración."""
    if not hay_desafio():
        return {}
    filtro = "AND articulo_id = ANY(:ids)" if articulo_ids is not None else ""
    df = pd.read_sql(text(f"""
        SELECT DISTINCT ON (articulo_id) articulo_id, fecha, vence
        FROM controles_vencimiento
        WHERE hay_stock AND vence IS NOT NULL AND COALESCE(coincide, TRUE) {filtro}
        ORDER BY articulo_id, fecha DESC, id DESC
    """), engine, params={"ids": [int(i) for i in (articulo_ids or [])]})
    return {int(r.articulo_id): {"fecha": r.fecha, "vence": r.vence} for r in df.itertuples()}


def dias_disponibles(articulo_ids):
    """articulo_id -> días disponibles para la venta (propios o de la categoría).
    Solo los que tienen; vacío sin la migración."""
    ids = [int(i) for i in articulo_ids]
    if not ids or not hay_tanda18():
        return {}
    a = pd.read_sql(text("SELECT id, categoria, vida_util_dias FROM articulos WHERE id = ANY(:ids)"),
                    engine, params={"ids": ids})
    v = vida_util_efectiva(a)
    return {int(i): int(d) for i, d in zip(a["id"], v) if pd.notna(d)}


def lotes_vencimiento(hoy=None):
    """
    Lotes con stock y fecha de vencimiento (real o estimada), uno por renglón
    de factura. Columnas: articulo_id, nombre, categoria, unidad_medida,
    factura_id, fecha_compra, cantidad_lote, quedan (lo que queda de ese lote
    hoy), vence, fuente ('factura' si la fecha se cargó, 'estimada' si sale de
    los días disponibles), dias_para_vencer, anteriores (unidades de lotes más
    viejos que también quedan: se venden antes), precio, costo.
    El stock que no se explica con facturas (carga inicial) queda como un
    lote 'sin fecha' (vence NaT) si el artículo tiene vencimiento, para que se
    vea cuánto stock no se puede fechar. Excluye combos, compra diaria (se
    maneja aparte) y artículos sin stock.
    """
    if not hay_tanda18():
        return pd.DataFrame()
    if hoy is None:
        with engine.connect() as conn:
            hoy = conn.execute(text("SELECT (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date")).scalar()   # hoy en Argentina
    hoy = pd.Timestamp(hoy)
    arts = pd.read_sql(text("""
        SELECT a.id AS articulo_id, a.nombre, a.categoria, a.unidad_medida, a.stock_actual::float AS stock,
               a.vida_util_dias, a.fecha_vencimiento AS venc_articulo,
               va.costo_venta_con_iva::float AS precio, a.costo_compra_actual::float AS costo
        FROM articulos a JOIN v_articulos va ON va.id = a.id
        WHERE a.activo AND a.categoria IS DISTINCT FROM 'Promocion' AND a.stock_actual > 0
          AND COALESCE((to_jsonb(a) ->> 'reposicion_dias')::int, 0) <> 1
    """), engine)
    if arts.empty:
        return pd.DataFrame()
    arts["vida"] = vida_util_efectiva(arts)
    lineas = pd.read_sql(text("""
        SELECT d.articulo_id, d.factura_id, f.fecha AS fecha_compra, d.id AS detalle_id,
               d.cantidad::float AS cantidad, d.fecha_vencimiento
        FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
        WHERE d.articulo_id = ANY(:ids)
    """), engine, params={"ids": arts["articulo_id"].astype(int).tolist()})
    lineas["fecha_compra"] = pd.to_datetime(lineas["fecha_compra"])
    lineas["fecha_vencimiento"] = pd.to_datetime(lineas["fecha_vencimiento"])
    con_fecha = set(lineas.loc[lineas["fecha_vencimiento"].notna(), "articulo_id"])
    # Tanda 21: la fecha vista en la góndola (Desafío Recreo) es la de la compra más vieja que queda
    gondola = ultimo_control_por_articulo(arts["articulo_id"].astype(int).tolist())
    arts = arts[arts["vida"].notna() | arts["venc_articulo"].notna() | arts["articulo_id"].isin(con_fecha)
                | arts["articulo_id"].isin(list(gondola))]
    filas = []
    por_art = {a: g for a, g in lineas.groupby("articulo_id")}
    for a in arts.itertuples():
        g = por_art.get(a.articulo_id)
        g = g.sort_values(["fecha_compra", "detalle_id"], ascending=False) if g is not None else lineas.iloc[0:0]
        resto = float(a.stock)
        lotes = []
        for l in g.itertuples():
            if resto <= 1e-9:
                break
            q = min(float(l.cantidad), resto)
            resto -= q
            if pd.notna(l.fecha_vencimiento):
                vence, fuente = l.fecha_vencimiento, "factura"
            elif pd.notna(a.vida):
                vence, fuente = l.fecha_compra + pd.Timedelta(days=int(a.vida)), "estimada"
            elif pd.notna(a.venc_articulo):
                vence, fuente = pd.Timestamp(a.venc_articulo), "artículo"
            else:
                vence, fuente = pd.NaT, "sin fecha"
            lotes.append((l.factura_id, l.fecha_compra, float(l.cantidad), q, vence, fuente))
        if resto > 1e-9:
            # stock que no se explica con facturas (carga inicial o correcciones)
            vence = pd.Timestamp(a.venc_articulo) if pd.notna(a.venc_articulo) else pd.NaT
            lotes.append((None, pd.NaT, None, resto, vence, "artículo" if pd.notna(vence) else "sin fecha"))
        # los lotes más viejos se venden antes: 'anteriores' = lo que queda de lotes más viejos
        lotes = lotes[::-1]
        ctrl = gondola.get(int(a.articulo_id))
        if ctrl and lotes:
            fid, fc, cant, q, vence, fuente = lotes[0]
            # la fecha más cercana que se vio corresponde a la compra más vieja que queda, si ya
            # existía cuando se miró (una compra posterior al control no pudo verse)
            if pd.isna(fc) or fc <= pd.Timestamp(ctrl["fecha"]):
                lotes[0] = (fid, fc, cant, q, pd.Timestamp(ctrl["vence"]), "góndola")
        acum = 0.0
        for fid, fc, cant, q, vence, fuente in lotes:
            filas.append({"articulo_id": a.articulo_id, "nombre": a.nombre, "categoria": a.categoria,
                          "unidad_medida": a.unidad_medida, "factura_id": fid, "fecha_compra": fc,
                          "cantidad_lote": cant, "quedan": q, "vence": vence, "fuente": fuente,
                          "anteriores": acum, "precio": a.precio, "costo": a.costo})
            acum += q
    df = pd.DataFrame(filas)
    if df.empty:
        return df
    df["dias_para_vencer"] = (pd.to_datetime(df["vence"]) - hoy).dt.days
    return df.sort_values(["vence", "nombre"], na_position="last").reset_index(drop=True)


# ------------------------------------------------------------
# STOCK SUMADO A MANO ANTES DE CARGAR LA FACTURA (tanda 12, 10/2026)
# Caso real (10/2026): al recibir la mercadería se subía el stock a mano y
# días después se cargaba la factura, que lo volvía a sumar (Cremoso La
# Pampa, lácteos La Paulina, gaseosas...). Se detecta con el historial.
# ------------------------------------------------------------
_RE_CORRECCION = re.compile(r": (-?[0-9.]+) -> (-?[0-9.]+)$")
DIAS_SUMA_A_MANO = 7


def sumas_a_mano_recientes(articulo_ids, dias=DIAS_SUMA_A_MANO):
    """
    {articulo_id: [{"fecha", "suma"}, ...]} con las correcciones manuales de
    stock HACIA ARRIBA de los últimos `dias` días para esos artículos, de la
    más nueva a la más vieja. Sirve para avisar, al cargar una factura, que
    esa mercadería quizás ya se sumó a mano.
    """
    ids = sorted({int(i) for i in articulo_ids})
    if not ids:
        return {}
    with engine.connect() as conn:
        filas = conn.execute(text("""
            SELECT registro_id, (fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires') AS fecha, detalle FROM historial_cambios
            WHERE tabla = 'articulos' AND accion = 'EDICION'
              AND detalle LIKE 'Stock corregido a mano%%'
              AND registro_id = ANY(:ids)
              AND fecha >= now() - make_interval(days => :dias)
            ORDER BY fecha DESC
        """), {"ids": ids, "dias": int(dias)}).all()
    resultado = {}
    for art_id, fecha, detalle in filas:
        m = _RE_CORRECCION.search(detalle or "")
        if not m:
            continue
        suma = float(m.group(2)) - float(m.group(1))
        if suma > 0:
            resultado.setdefault(int(art_id), []).append({"fecha": fecha, "suma": round(suma, 3)})
    return resultado


def posible_suma_duplicada(cantidad, sumas):
    """La suma a mano más reciente que se parece a la cantidad de la factura
    (entre la mitad y el doble), o None. Las demás no se consideran: una suma
    chica de hace días suele ser un conteo, no esta mercadería."""
    for s in sumas or []:
        if 0.5 * float(cantidad) <= s["suma"] <= 2 * float(cantidad):
            return s
    return None


# ------------------------------------------------------------
# CANTIDADES FUERA DE LO NORMAL AL VENDER (tanda 12, 10/2026)
# Caso real (21/10/2026): 140 "unidades" de PALETA a $14.000 ($1.960.000)
# por tipear gramos en un artículo por unidad. En los datos reales la venta
# más grande respecto de lo habitual del artículo es 10 veces (caramelos,
# latas), y solo el 0,1 % de los renglones supera $60.000.
# ------------------------------------------------------------
VECES_LO_HABITUAL = 20
IMPORTE_RENGLON_ALTO = 150_000
MIN_VENTAS_HABITUAL = 3


def cantidades_habituales(articulo_ids, dias=120):
    """{articulo_id: (mediana de la cantidad por renglón, cantidad de ventas)} de los
    últimos `dias` días (sin consumo interno)."""
    ids = sorted({int(i) for i in articulo_ids})
    if not ids:
        return {}
    with engine.connect() as conn:
        filas = conn.execute(text("""
            SELECT articulo_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY cantidad), count(*)
            FROM ventas
            WHERE articulo_id = ANY(:ids) AND NOT es_consumo_interno
              AND fecha >= (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - :dias
            GROUP BY articulo_id
        """), {"ids": ids, "dias": int(dias)}).all()
    return {int(a): (float(m), int(n)) for a, m, n in filas}


def controlar_cantidades(items, habituales):
    """
    Renglones del carrito que conviene revisar antes de confirmar. Devuelve una
    lista de textos (vacía si todo es normal). Un renglón se marca si:
    - vale $150.000 o más, o
    - la cantidad es 20 veces o más lo habitual del artículo (con al menos 3
      ventas para saber qué es lo habitual), o
    - sin historial: 50 o más unidades, o 10 kg o más.
    """
    revisar = []
    for it in items:
        cant = float(it["cantidad"])
        subtotal = cant * float(it["precio_unitario"])
        es_kg = it.get("unidad_medida") == "kg"
        cant_txt = f"{cant * 1000:,.0f} g".replace(",", ".") if es_kg else f"{cant:g}".replace(".", ",")
        motivo = None
        med, n = habituales.get(int(it["articulo_id"]), (None, 0))
        if med and n >= MIN_VENTAS_HABITUAL and cant >= VECES_LO_HABITUAL * med:
            hab_txt = f"{med * 1000:,.0f} g".replace(",", ".") if es_kg else f"{med:g}".replace(".", ",")
            motivo = f"lo habitual es {hab_txt} por venta"
        elif n < MIN_VENTAS_HABITUAL and ((es_kg and cant >= 10) or (not es_kg and cant >= 50)):
            motivo = "es mucho para una sola venta"
        elif subtotal >= IMPORTE_RENGLON_ALTO:
            motivo = "es un importe muy alto para un renglón"
        if motivo:
            importe = f"${subtotal:,.0f}".replace(",", ".")
            revisar.append(f"**{it['nombre']}**: {cant_txt} por {importe} -- {motivo}")
    return revisar


def agregar_items_factura(factura_id, items):
    """
    Suma ítems a una factura ya guardada (renglones que faltaron al cargarla),
    en vez de crear otra factura con el mismo número. Mismo efecto que
    crear_factura sobre stock, costos y precio redondo; la factura conserva
    su fecha y número. Deja constancia en el historial.
    Devuelve la lista de avisos (igual que crear_factura).
    """
    if not items:
        raise ValueError("No hay ítems para agregar.")
    with engine.begin() as conn:
        f = conn.execute(text("""
            SELECT f.numero, f.fecha, p.nombre AS proveedor
            FROM facturas f JOIN proveedores p ON p.id = f.proveedor_id WHERE f.id = :id
        """), {"id": factura_id}).mappings().first()
        if f is None:
            raise ValueError("Esa factura ya no existe (¿se eliminó?).")
        avisos = _insertar_items_factura(conn, factura_id, items)
        nombres = dict(conn.execute(
            text("SELECT id, nombre FROM articulos WHERE id = ANY(:ids)"),
            {"ids": [int(i["articulo_id"]) for i in items]}).all())
        total = sum(float(i["cantidad"]) * float(i["costo_unitario"]) for i in items)
        registrar_historial(
            "facturas", factura_id, "ITEMS_AGREGADOS",
            f"Factura #{factura_id} ({f['proveedor']} nº {f['numero'] or 's/n'} del {f['fecha']:%d/%m/%Y}): "
            f"se agregaron {len(items)} ítem(s) por ${total:,.2f}: "
            + ", ".join(f"{nombres.get(int(i['articulo_id']), i['articulo_id'])} +{_cant_txt(i['cantidad'])}"
                        for i in items),
            conn=conn,
        )
    return avisos


def resumen_factura(factura_id):
    """Datos de una factura ya guardada para mostrarla antes de sumarle ítems, o None."""
    with engine.connect() as conn:
        f = conn.execute(text("""
            SELECT f.id, f.proveedor_id, p.nombre AS proveedor, f.numero, f.fecha, f.origen,
                   COUNT(d.id) AS items, COALESCE(SUM(d.cantidad * d.costo_unitario), 0) AS total,
                   COALESCE(array_agg(d.articulo_id) FILTER (WHERE d.id IS NOT NULL), '{}') AS articulos
            FROM facturas f
            JOIN proveedores p ON p.id = f.proveedor_id
            LEFT JOIN detalle_factura d ON d.factura_id = f.id
            WHERE f.id = :id
            GROUP BY f.id, p.nombre
        """), {"id": factura_id}).mappings().first()
    return dict(f) if f else None

def listar_facturas():
    return pd.read_sql("""
        SELECT f.id, p.nombre AS proveedor, f.numero, f.fecha, f.origen,
               (SELECT COALESCE(SUM(d.cantidad * d.costo_unitario), 0)
                FROM detalle_factura d WHERE d.factura_id = f.id) AS total
        FROM facturas f JOIN proveedores p ON p.id = f.proveedor_id
        ORDER BY f.fecha DESC, f.id DESC
    """, engine)

def detalle_de_factura(factura_id):
    return pd.read_sql(text("""
        SELECT d.id, a.nombre AS articulo, d.cantidad, d.costo_unitario,
               (d.cantidad * d.costo_unitario) AS subtotal,
               d.actualiza_precio
        FROM detalle_factura d JOIN articulos a ON a.id = d.articulo_id
        WHERE d.factura_id = :fid
    """), engine, params={"fid": factura_id})

def ultimo_costo_factura(articulo_id):
    """
    Última compra que fijó el costo de referencia del artículo (renglón con
    actualiza_precio): {factura_id, fecha, proveedor, costo_unitario}, o None.
    Sirve para avisar en "Actualizar precio / margen" cuando el costo que se
    está por guardar no coincide con el de la factura (fix 23/10/2026: el
    costo de la Coca retornable 2L se pisó a mano con $3.750 después de que
    la factura #47 lo había dejado en $1.571,40, y quedó con margen 0 %).
    """
    with engine.connect() as conn:
        fila = conn.execute(text("""
            SELECT f.id AS factura_id, f.fecha, p.nombre AS proveedor, d.costo_unitario
            FROM detalle_factura d
            JOIN facturas f ON f.id = d.factura_id
            LEFT JOIN proveedores p ON p.id = f.proveedor_id
            WHERE d.articulo_id = :id AND d.actualiza_precio
            ORDER BY f.fecha DESC, d.id DESC
            LIMIT 1
        """), {"id": articulo_id}).mappings().first()
    return dict(fila) if fila else None


def eliminar_factura(factura_id):
    """
    Borra la factura y todos sus ítems (ON DELETE CASCADE). El trigger
    trg_revertir_compra resta automáticamente el stock que esa compra
    había sumado. No revierte el costo_compra_actual del artículo: si
    era la última compra cargada, conviene revisar el precio a mano.
    Deja constancia en el historial con proveedor, número, fecha e ítems.
    """
    with engine.begin() as conn:
        f = conn.execute(text("""
            SELECT f.numero, f.fecha, p.nombre AS proveedor
            FROM facturas f LEFT JOIN proveedores p ON p.id = f.proveedor_id
            WHERE f.id = :id
        """), {"id": factura_id}).mappings().first()
        if f is None:
            return
        items = conn.execute(text("""
            SELECT a.nombre, d.cantidad, d.costo_unitario
            FROM detalle_factura d LEFT JOIN articulos a ON a.id = d.articulo_id
            WHERE d.factura_id = :id ORDER BY d.id
        """), {"id": factura_id}).mappings().all()
        conn.execute(text("DELETE FROM facturas WHERE id = :id"), {"id": factura_id})
        total = sum(float(i["cantidad"]) * float(i["costo_unitario"]) for i in items)
        detalle = (f"Factura #{factura_id} eliminada: {f['proveedor']} nº {f['numero'] or 's/n'} "
                   f"del {f['fecha']:%d/%m/%Y}, {len(items)} ítem(s), total ${total:,.2f}. "
                   "Stock restado: " + ", ".join(f"{i['nombre']} -{_cant_txt(i['cantidad'])}" for i in items))
        registrar_historial("facturas", factura_id, "ELIMINACION", detalle, conn=conn)

# ------------------------------------------------------------
# FUZZY MATCHING DE ARTICULOS (Hito 5)
# ------------------------------------------------------------
def buscar_alias_exacto(proveedor_id, nombre_factura):
    """Si ya vimos antes ese nombre exacto de ese proveedor, devuelve el artículo directo."""
    query = text("""
        SELECT a.* FROM articulo_alias al
        JOIN articulos a ON a.id = al.articulo_id
        WHERE al.proveedor_id = :prov AND al.alias = :nombre
    """)
    with engine.connect() as conn:
        row = conn.execute(query, {"prov": proveedor_id, "nombre": nombre_factura}).mappings().first()
    return dict(row) if row else None

def sugerir_articulos_similares(nombre_texto, limite=5, umbral=0.2):
    """
    Busca artículos existentes con nombre parecido (trigram similarity),
    para no crear duplicados cuando un proveedor nombra el producto distinto.
    Devuelve columnas: id, nombre, similitud (0 a 1, más alto = más parecido).
    """
    query = text("""
        SELECT id, nombre, ROUND(similarity(nombre, :texto)::numeric, 3) AS similitud
        FROM articulos
        WHERE similarity(nombre, :texto) > :umbral AND activo
        ORDER BY similitud DESC
        LIMIT :limite
    """)
    return pd.read_sql(query, engine, params={"texto": nombre_texto, "umbral": umbral, "limite": limite})

def guardar_alias(articulo_id, proveedor_id, alias, factor=None):
    """
    Guarda la relación 'este proveedor llama así a este artículo' para reconocerlo la próxima vez.

    factor (opcional, importador de facturas): cuántas unidades del artículo
    representa UNA unidad facturada por el proveedor (caja de 60 -> 60).
    Si no se pasa, se guarda/conserva el valor que ya tenga (1 por defecto),
    así "Cargar Factura" sigue funcionando igual que antes.
    """
    with engine.begin() as conn:
        if factor is None:
            conn.execute(text("""
                INSERT INTO articulo_alias (articulo_id, proveedor_id, alias)
                VALUES (:aid, :prov, :alias)
                ON CONFLICT (proveedor_id, alias) DO UPDATE SET articulo_id = EXCLUDED.articulo_id
            """), {"aid": articulo_id, "prov": proveedor_id, "alias": alias})
        else:
            conn.execute(text("""
                INSERT INTO articulo_alias (articulo_id, proveedor_id, alias, factor)
                VALUES (:aid, :prov, :alias, :factor)
                ON CONFLICT (proveedor_id, alias)
                DO UPDATE SET articulo_id = EXCLUDED.articulo_id, factor = EXCLUDED.factor
            """), {"aid": articulo_id, "prov": proveedor_id, "alias": alias, "factor": factor})


# ------------------------------------------------------------
# IMPORTADOR DE FACTURAS (planilla del lector automático)
# ------------------------------------------------------------
def _solo_digitos(valor):
    return "".join(ch for ch in str(valor or "") if ch.isdigit())

def buscar_proveedor_por_cuit(cuit):
    """Proveedor cuyo CUIT coincide (comparando solo dígitos: '30-00000000-0' = '30000000000')."""
    digitos = _solo_digitos(cuit)
    if len(digitos) != 11:
        return None
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT * FROM proveedores
            WHERE regexp_replace(COALESCE(cuit, ''), '\\D', '', 'g') = :d
            ORDER BY id LIMIT 1
        """), {"d": digitos}).mappings().first()
    return dict(row) if row else None

def asignar_cuit_proveedor(proveedor_id, cuit):
    """Completa el CUIT de un proveedor ya cargado sin CUIT (así la próxima factura se reconoce sola)."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE proveedores SET cuit = :c WHERE id = :id"), {"c": cuit, "id": proveedor_id})

def crear_proveedor_devolviendo_id(nombre, cuit=None, telefono=None):
    """Igual que crear_proveedor, pero devuelve el id (lo necesita el importador)."""
    with engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO proveedores (nombre, cuit, telefono) VALUES (:n, :c, :t) RETURNING id"),
            {"n": nombre, "c": cuit, "t": telefono}
        ).scalar()

def alias_con_factor(proveedor_id, nombre_factura):
    """
    Como buscar_alias_exacto, pero devuelve también el factor de conversión del alias.
    Devuelve dict con las columnas del artículo + 'factor', o None.
    """
    query = text("""
        SELECT a.*, al.factor AS factor FROM articulo_alias al
        JOIN articulos a ON a.id = al.articulo_id
        WHERE al.proveedor_id = :prov AND al.alias = :nombre
    """)
    with engine.connect() as conn:
        row = conn.execute(query, {"prov": proveedor_id, "nombre": nombre_factura}).mappings().first()
    return dict(row) if row else None

def factura_importada(ref_externa):
    """id de la factura ya importada con esa referencia externa, o None."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id FROM facturas WHERE ref_externa = :r"), {"r": ref_externa}
        ).scalar()

def _partes_numero(numero):
    """'01171-00041579' -> (1171, 41579): punto de venta y número sin ceros a la izquierda."""
    return tuple(int(g) for g in re.findall(r"\d+", str(numero or "")))


def factura_existente(proveedor_id, numero):
    """
    id de una factura ya cargada (a mano o importada) con el mismo proveedor y número, o None.
    Reconoce el mismo número escrito distinto: con o sin ceros a la izquierda en cada parte
    ('1171-41579' = '01171-00041579') o con todos los dígitos juntos ('0117100041579').
    """
    digitos = _solo_digitos(numero)
    if not digitos:
        return None
    partes = _partes_numero(numero)
    with engine.connect() as conn:
        filas = conn.execute(text("""
            SELECT id, numero FROM facturas
            WHERE proveedor_id = :p AND numero ~ '[0-9]'
            ORDER BY id
        """), {"p": proveedor_id}).all()
    for fid, num in filas:
        if _partes_numero(num) == partes or _solo_digitos(num).lstrip("0") == digitos.lstrip("0"):
            return fid
    return None

def historial_precios(articulo_id=None):
    query = "SELECT * FROM v_historial_precios"
    params = {}
    if articulo_id:
        query += " WHERE articulo_id = :aid"
        params["aid"] = articulo_id
    query += " ORDER BY fecha DESC"
    return pd.read_sql(text(query), engine, params=params)

def comparar_precios_proveedores(articulo_id=None):
    """
    Costo vigente de cada artículo por cada proveedor que lo facturó, con
    cuál es el más barato (v_comparacion_proveedores). Sin articulo_id,
    trae solo los artículos con más de un proveedor cargado (los únicos
    donde tiene sentido comparar).
    """
    if articulo_id is not None:
        return pd.read_sql(
            text("SELECT * FROM v_comparacion_proveedores WHERE articulo_id = :aid ORDER BY costo_compra"),
            engine, params={"aid": articulo_id}
        )
    return pd.read_sql(
        "SELECT * FROM v_comparacion_proveedores WHERE cantidad_proveedores > 1 "
        "ORDER BY articulo, costo_compra",
        engine
    )

def variaciones_de_precio_relevantes(umbral_pct=5):
    """Devuelve solo las variaciones de precio por encima de un umbral (subas o bajas)."""
    df = historial_precios()
    if df.empty:
        return df
    # La primera compra de cada artículo/proveedor no tiene una compra
    # anterior con qué comparar, así que variacion_pct queda vacía (None/NaN)
    # para esas filas. Hay que descartarlas ANTES de calcular abs(), porque
    # abs() no sabe qué hacer con un valor vacío.
    df = df.dropna(subset=["variacion_pct"]).copy()
    if df.empty:
        return df
    df["variacion_pct"] = pd.to_numeric(df["variacion_pct"], errors="coerce")
    df = df.dropna(subset=["variacion_pct"])
    return df[df["variacion_pct"].abs() >= umbral_pct]

# ------------------------------------------------------------
# CONFIGURACION DE VENTA RAPIDA (qué artículos aparecen como botón)
# ------------------------------------------------------------
def buscar_articulos_para_venta(texto):
    """
    Busca por nombre o marca en TODO el catálogo (no solo los favoritos).
    Así cualquier producto se puede vender rápido con un par de letras,
    sin depender de que alguien lo haya marcado como favorito de antemano.
    Sin límite de resultados: una búsqueda como "coca" tiene que mostrar
    todas las variantes (lata, botella, retornable, etc.), no solo las
    primeras.
    """
    return pd.read_sql(text("""
        SELECT * FROM v_articulos
        WHERE (nombre ILIKE :patron OR marca ILIKE :patron) AND activo
        ORDER BY nombre
    """), engine, params={"patron": f"%{texto}%"})

def articulos_mas_vendidos(limite=20, dias=30):
    """
    Los artículos activos que más veces se vendieron en los últimos `dias`
    días (cantidad de renglones de venta, no unidades: lo que más seguido se
    pide en el mostrador), con las mismas columnas que listar_articulos().
    Es el atajo de Registrar Venta cuando no se escribió nada en el buscador.
    Sin consumo interno ni sobrante. Vacío si todavía no hay ventas.
    """
    return pd.read_sql(text("""
        SELECT va.* FROM v_articulos va
        JOIN (
            SELECT articulo_id, COUNT(*) AS veces
            FROM ventas
            WHERE fecha >= (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - CAST(:dias AS INTEGER) AND NOT es_consumo_interno
            GROUP BY articulo_id
        ) v ON v.articulo_id = va.id
        WHERE va.activo
        ORDER BY v.veces DESC, va.nombre
        LIMIT :limite
    """), engine, params={"dias": dias, "limite": limite})

# ------------------------------------------------------------
# FRACCIONAMIENTO (abrir una bolsa cerrada -> producto suelto)
# ------------------------------------------------------------
def listar_articulos_bolsa():
    """Artículos que se compran/venden en unidades enteras — candidatos a 'abrir' en Fraccionar."""
    return pd.read_sql("SELECT * FROM v_articulos WHERE unidad_medida = 'unidad' AND activo ORDER BY nombre", engine)

def obtener_derivado(articulo_bolsa_id):
    """Producto suelto ya vinculado a esta bolsa, si existe. None si todavía no se creó ninguno."""
    query = text("SELECT * FROM v_articulos WHERE articulo_fraccion_de = :id LIMIT 1")
    with engine.connect() as conn:
        row = conn.execute(query, {"id": articulo_bolsa_id}).mappings().first()
    return dict(row) if row else None

def crear_producto_fraccionado(articulo_bolsa_id, nombre, tipo, factor_fraccion, marca=None, categoria=None):
    """
    Crea el producto "suelto" vinculado a una bolsa. tipo: 'kg' (se vende por
    peso) o 'paquetito' (se vende por unidad). El costo inicial (y por lo
    tanto el precio de venta) se calcula a partir del costo y margen/IVA
    vigentes de la bolsa, dividido por factor_fraccion, para que quede
    consistente con el precio de la bolsa entera desde el primer momento.
    Devuelve el id del artículo derivado creado.
    """
    query = text("SELECT costo_compra_actual, margen_ganancia, iva FROM articulos WHERE id = :id")
    with engine.connect() as conn:
        bolsa = conn.execute(query, {"id": articulo_bolsa_id}).mappings().first()
    if not bolsa:
        raise ValueError("La bolsa indicada no existe.")

    unidad_medida_derivado = "kg" if tipo == "kg" else "unidad"
    costo_inicial = float(bolsa["costo_compra_actual"]) / float(factor_fraccion)

    with engine.begin() as conn:
        nuevo_id = conn.execute(text("""
            INSERT INTO articulos
                (nombre, marca, categoria, costo_compra_actual, margen_ganancia, iva,
                 stock_actual, stock_minimo, unidad_medida, articulo_fraccion_de, factor_fraccion)
            VALUES (:nombre, :marca, :categoria, :costo, :margen, :iva,
                    0, 0, :unidad, :bolsa_id, :factor)
            RETURNING id
        """), {
            "nombre": nombre, "marca": marca, "categoria": categoria, "costo": costo_inicial,
            "margen": bolsa["margen_ganancia"], "iva": bolsa["iva"], "unidad": unidad_medida_derivado,
            "bolsa_id": articulo_bolsa_id, "factor": factor_fraccion
        }).scalar()
    return nuevo_id

def fraccionar(articulo_bolsa_id, cantidad_bolsas):
    """
    "Abre" cantidad_bolsas bolsas cerradas: genera stock del producto suelto
    vinculado y recalcula su costo (costo vigente de la bolsa ÷ factor de
    fracción). Requiere que ya exista un derivado (crear_producto_fraccionado
    primero). El trigger trg_aplicar_fraccionamiento mueve el stock de ambos
    artículos. Devuelve (id_del_fraccionamiento, aviso_o_None) -- aviso si
    la bolsa quedó con stock negativo (no se bloquea, queda pendiente de
    ajustar con la próxima factura).
    """
    derivado = obtener_derivado(articulo_bolsa_id)
    if not derivado:
        raise ValueError("Esta bolsa todavía no tiene un producto fraccionado vinculado.")

    query = text("SELECT costo_compra_actual FROM articulos WHERE id = :id")
    with engine.connect() as conn:
        bolsa = conn.execute(query, {"id": articulo_bolsa_id}).mappings().first()

    factor = float(derivado["factor_fraccion"])
    cantidad_generada = float(cantidad_bolsas) * factor
    costo_derivado_resultante = float(bolsa["costo_compra_actual"]) / factor

    with engine.begin() as conn:
        fraccionamiento_id = conn.execute(text("""
            INSERT INTO fraccionamientos
                (articulo_bolsa_id, articulo_derivado_id, cantidad_bolsas, cantidad_generada, costo_derivado_resultante,
                 fecha)
            VALUES (:bolsa, :derivado, :cant_bolsas, :cant_generada, :costo,
                    (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date)   -- el día argentino (tanda 20)
            RETURNING id
        """), {
            "bolsa": articulo_bolsa_id, "derivado": int(derivado["id"]),
            "cant_bolsas": cantidad_bolsas, "cant_generada": cantidad_generada, "costo": costo_derivado_resultante
        }).scalar()
        aviso = _registrar_si_stock_negativo(conn, articulo_bolsa_id, "fraccionamiento")
    return fraccionamiento_id, aviso

def listar_fraccionamientos(limite=20):
    return pd.read_sql(text("""
        SELECT f.id, ab.nombre AS bolsa, f.cantidad_bolsas, ad.nombre AS producto_suelto,
               f.cantidad_generada, ad.unidad_medida, f.fecha
        FROM fraccionamientos f
        JOIN articulos ab ON ab.id = f.articulo_bolsa_id
        JOIN articulos ad ON ad.id = f.articulo_derivado_id
        ORDER BY f.fecha DESC, f.id DESC
        LIMIT :limite
    """), engine, params={"limite": limite})

def eliminar_fraccionamiento(fraccionamiento_id):
    """Borra el fraccionamiento; el trigger trg_revertir_fraccionamiento repone/ajusta el stock."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fraccionamientos WHERE id = :id"), {"id": fraccionamiento_id})

# ------------------------------------------------------------
# PROMOCIONES
# Cada promoción es un artículo más (categoria = 'Promocion'): se vende
# igual que cualquier producto, desde el mismo buscador y carrito de
# "Registrar Venta". promocion_componentes define de qué artículos y en
# qué cantidad está armada.
# ------------------------------------------------------------
CATEGORIA_PROMOCION = "Promocion"

def crear_promocion(nombre, precio_venta_con_iva, componentes, iva=0.21, tipo="fisica"):
    """
    componentes: lista de dicts {articulo_id, cantidad_por_unidad}.
    Calcula el costo de la promoción como la suma de (costo de compra
    actual de cada componente * cantidad_por_unidad), y despeja el margen
    de ganancia necesario para que el precio de venta final (con IVA) sea
    el pedido -- misma fórmula que "Actualizar precio/margen".

    tipo:
      'fisica'  -- se arma/renueva a mano (armar_promocion). Arranca con
                   stock_actual = 0 hasta la primera armada.
      'virtual' -- combo de stock compartido: no se arma, el stock
                   disponible se calcula solo a partir de sus componentes
                   (ver v_articulos) y vender el combo descuenta
                   directo el stock de esos componentes.

    Devuelve el id del artículo-promoción creado.
    """
    if not componentes:
        raise ValueError("Una promoción necesita al menos un artículo componente.")
    if tipo not in ("fisica", "virtual"):
        raise ValueError("Tipo de promoción inválido (debe ser 'fisica' o 'virtual').")

    with engine.begin() as conn:
        ids = [int(c["articulo_id"]) for c in componentes]
        costos = {
            int(r["id"]): float(r["costo_compra_actual"])
            for r in conn.execute(
                text("SELECT id, costo_compra_actual FROM articulos WHERE id = ANY(:ids)"),
                {"ids": ids}
            ).mappings()
        }
        costo_total = sum(
            costos.get(int(c["articulo_id"]), 0.0) * float(c["cantidad_por_unidad"])
            for c in componentes
        )
        margen = (precio_venta_con_iva / (costo_total * (1 + iva)) - 1) if costo_total > 0 else 0.0
        margen = max(margen, 0.0)

        promocion_id = conn.execute(text("""
            INSERT INTO articulos
                (nombre, categoria, costo_compra_actual, margen_ganancia, iva,
                 stock_actual, stock_minimo, unidad_medida, promo_tipo)
            VALUES (:nombre, :categoria, :costo, :margen, :iva, 0, 0, 'unidad', :tipo)
            RETURNING id
        """), {
            "nombre": nombre, "categoria": CATEGORIA_PROMOCION,
            "costo": round(costo_total, 2), "margen": margen, "iva": iva, "tipo": tipo
        }).scalar()

        for c in componentes:
            conn.execute(text("""
                INSERT INTO promocion_componentes (promocion_articulo_id, componente_articulo_id, cantidad_por_unidad)
                VALUES (:promo, :comp, :cant)
            """), {"promo": promocion_id, "comp": int(c["articulo_id"]), "cant": c["cantidad_por_unidad"]})

        registrar_historial(
            "articulos", promocion_id, "PROMOCION_CREADA",
            f"Promoción '{nombre}' creada con {len(componentes)} componente(s), "
            f"precio de venta ${precio_venta_con_iva}",
            conn=conn
        )
    return promocion_id

def editar_promocion(promocion_articulo_id, nuevo_tipo, nuevo_precio_venta_con_iva, nuevos_componentes,
                     nuevo_nombre=None, devolver_armadas=False):
    """
    Reemplaza la composición completa de una promoción existente (y de
    paso permite cambiar su tipo y su precio de venta). nuevos_componentes
    tiene la misma forma que en crear_promocion: lista de dicts
    {articulo_id, cantidad_por_unidad} -- la lista entera reemplaza a la
    anterior (no es un merge).

    El costo de la promoción se recalcula con el costo de compra ACTUAL
    de los componentes nuevos, y el margen se despeja de nuevo para que
    el precio de venta final (con IVA) sea el pedido -- misma fórmula que
    crear_promocion / "Actualizar precio/margen". Si no se toca el precio
    (se manda el mismo que ya tenía), el cliente sigue pagando lo mismo
    aunque haya cambiado la composición.

    Si nuevo_tipo es distinto del tipo que tenía, el stock PROPIO de la
    promoción se reinicia a 0: al pasar a 'virtual' ese valor deja de
    leerse (se calcula en vivo a partir de los componentes), y al volver
    a 'fisica' conviene arrancar limpio en vez de heredar un número viejo
    que ya no tiene sentido -- hay que "armar" de nuevo desde ahí.

    devolver_armadas: solo cuenta al pasar de 'fisica' a 'virtual' con
    combos ya armados. Si es True, los componentes de esos combos (con la
    composición VIEJA, que es con la que se armaron) vuelven al stock de
    cada artículo: es "desarmarlos". Si es False, se pierden del stock
    (por ejemplo, si ya no existen físicamente).

    Las ventas ya hechas no cambian: desde la tanda 5 cada venta de combo
    guarda su propio desglose (venta_componentes).

    Devuelve True si el tipo cambió (y por lo tanto se reinició el stock),
    False si no.
    """
    if not nuevos_componentes:
        raise ValueError("Una promoción necesita al menos un artículo componente.")
    if nuevo_tipo not in ("fisica", "virtual"):
        raise ValueError("Tipo de promoción inválido (debe ser 'fisica' o 'virtual').")

    with engine.begin() as conn:
        fila = conn.execute(
            text("SELECT nombre, promo_tipo, iva, stock_actual FROM articulos WHERE id = :id AND categoria = :cat"),
            {"id": promocion_articulo_id, "cat": CATEGORIA_PROMOCION}
        ).mappings().first()
        if not fila:
            raise ValueError("Esa promoción no existe.")

        tipo_anterior = fila["promo_tipo"]
        iva = float(fila["iva"])
        nombre_promo = fila["nombre"]
        nombre_final = (nuevo_nombre or "").strip() or nombre_promo
        stock_armado = float(fila["stock_actual"] or 0)

        ids = [int(c["articulo_id"]) for c in nuevos_componentes]
        costos = {
            int(r["id"]): float(r["costo_compra_actual"])
            for r in conn.execute(
                text("SELECT id, costo_compra_actual FROM articulos WHERE id = ANY(:ids)"),
                {"ids": ids}
            ).mappings()
        }
        costo_total = sum(
            costos.get(int(c["articulo_id"]), 0.0) * float(c["cantidad_por_unidad"])
            for c in nuevos_componentes
        )
        margen = (nuevo_precio_venta_con_iva / (costo_total * (1 + iva)) - 1) if costo_total > 0 else 0.0
        margen = max(margen, 0.0)

        tipo_cambio = nuevo_tipo != tipo_anterior

        devueltos = []
        if tipo_cambio and tipo_anterior == "fisica" and stock_armado > 0 and devolver_armadas:
            viejos = conn.execute(text("""
                SELECT pc.componente_articulo_id AS id, pc.cantidad_por_unidad AS cant, a.nombre
                FROM promocion_componentes pc JOIN articulos a ON a.id = pc.componente_articulo_id
                WHERE pc.promocion_articulo_id = :id
            """), {"id": promocion_articulo_id}).mappings().all()
            for v in viejos:
                cant = round(float(v["cant"]) * stock_armado, 3)
                conn.execute(text("""
                    UPDATE articulos SET stock_actual = stock_actual + :c, actualizado_en = now() WHERE id = :a
                """), {"c": cant, "a": int(v["id"])})
                devueltos.append(f"{v['nombre']} +{_cant_txt(cant)}")

        if tipo_cambio:
            conn.execute(text("""
                UPDATE articulos
                SET nombre = :nombre, costo_compra_actual = :costo, margen_ganancia = :margen,
                    promo_tipo = :tipo, stock_actual = 0, actualizado_en = now()
                WHERE id = :id
            """), {
                "nombre": nombre_final, "costo": round(costo_total, 2), "margen": margen, "tipo": nuevo_tipo,
                "id": promocion_articulo_id
            })
        else:
            conn.execute(text("""
                UPDATE articulos
                SET nombre = :nombre, costo_compra_actual = :costo, margen_ganancia = :margen,
                    actualizado_en = now()
                WHERE id = :id
            """), {"nombre": nombre_final, "costo": round(costo_total, 2), "margen": margen,
                   "id": promocion_articulo_id})

        conn.execute(
            text("DELETE FROM promocion_componentes WHERE promocion_articulo_id = :id"),
            {"id": promocion_articulo_id}
        )
        for c in nuevos_componentes:
            conn.execute(text("""
                INSERT INTO promocion_componentes (promocion_articulo_id, componente_articulo_id, cantidad_por_unidad)
                VALUES (:promo, :comp, :cant)
            """), {"promo": promocion_articulo_id, "comp": int(c["articulo_id"]), "cant": c["cantidad_por_unidad"]})

        detalle_hist = (
            f"Promoción '{nombre_promo}' editada: {len(nuevos_componentes)} componente(s), "
            f"precio de venta ${nuevo_precio_venta_con_iva}"
        )
        if nombre_final != nombre_promo:
            detalle_hist += f" -- nombre nuevo '{nombre_final}'"
        if tipo_cambio:
            detalle_hist += f" -- tipo cambiado de '{tipo_anterior}' a '{nuevo_tipo}' (stock propio reiniciado a 0)"
            if stock_armado > 0 and tipo_anterior == "fisica":
                detalle_hist += (f"; {stock_armado:g} combo(s) armado(s) desarmado(s), vuelven al stock: "
                                 + ", ".join(devueltos)) if devueltos else (
                                 f"; {stock_armado:g} combo(s) armado(s) dados por perdidos (no se devolvieron)")
        registrar_historial("articulos", promocion_articulo_id, "PROMOCION_EDITADA", detalle_hist, conn=conn)

    return tipo_cambio

def listar_promociones():
    return pd.read_sql(
        text("SELECT * FROM v_articulos WHERE categoria = :cat ORDER BY nombre"),
        engine, params={"cat": CATEGORIA_PROMOCION}
    )

def obtener_componentes_promocion(promocion_articulo_id):
    return pd.read_sql(text("""
        SELECT pc.componente_articulo_id AS articulo_id, a.nombre, a.marca,
               pc.cantidad_por_unidad, a.stock_actual AS stock_disponible, a.unidad_medida
        FROM promocion_componentes pc
        JOIN articulos a ON a.id = pc.componente_articulo_id
        WHERE pc.promocion_articulo_id = :promo
        ORDER BY a.nombre
    """), engine, params={"promo": promocion_articulo_id})

def detalle_stock_virtual(promocion_articulo_id):
    """
    Solo para promociones 'virtual'. Para cada componente, cuántos combos
    alcanzaría a vender si ese fuera el único límite (stock_disponible //
    cantidad_por_unidad) -- la columna 'es_limitante' marca el/los que
    determinan el stock de combo mostrado en la promoción (el mínimo).
    Sirve para explicar en la UI *por qué* el combo tiene el stock que
    muestra, sin tener que ir a mirar cada artículo por separado.
    """
    df = pd.read_sql(text("""
        SELECT a.nombre, a.marca, pc.cantidad_por_unidad,
               a.stock_actual AS stock_disponible, a.unidad_medida
        FROM promocion_componentes pc
        JOIN articulos a ON a.id = pc.componente_articulo_id
        WHERE pc.promocion_articulo_id = :promo
        ORDER BY a.nombre
    """), engine, params={"promo": promocion_articulo_id})
    if df.empty:
        return df
    df["combos_posibles"] = (df["stock_disponible"] / df["cantidad_por_unidad"]).apply(
        lambda x: math.floor(x) if pd.notna(x) else 0
    )
    minimo = df["combos_posibles"].min()
    df["es_limitante"] = df["combos_posibles"] == minimo
    return df

def armar_promocion(promocion_articulo_id, cantidad_a_armar):
    """
    Arma (o renueva) cantidad_a_armar unidades de la promoción: descuenta de
    cada componente su cantidad_por_unidad * cantidad_a_armar, y suma esa
    cantidad al stock de la promoción. Si algún componente no tiene stock
    suficiente, igual se aplica (el stock de ese componente queda negativo,
    pendiente de la próxima factura) -- devuelve la lista de avisos
    correspondientes para mostrar en la app.
    Solo aplica a promociones 'fisica'. Una promoción 'virtual' comparte
    el stock de sus componentes en vivo -- no hace falta (ni se puede)
    armarla.
    """
    if cantidad_a_armar <= 0:
        raise ValueError("La cantidad a armar debe ser mayor a 0.")

    with engine.connect() as conn:
        tipo = conn.execute(
            text("SELECT promo_tipo FROM articulos WHERE id = :id"), {"id": promocion_articulo_id}
        ).scalar()
    if tipo == "virtual":
        raise ValueError(
            "Esta promoción es un combo de stock compartido: se vende directo desde "
            "'Registrar Venta', no hace falta (ni corresponde) armarla."
        )

    componentes = obtener_componentes_promocion(promocion_articulo_id)
    if componentes.empty:
        raise ValueError("Esta promoción no tiene componentes definidos.")

    avisos = []
    with engine.begin() as conn:
        nombre_promo = conn.execute(
            text("SELECT nombre FROM articulos WHERE id = :id"), {"id": promocion_articulo_id}
        ).scalar()
        for _, comp in componentes.iterrows():
            a_descontar = float(comp["cantidad_por_unidad"]) * float(cantidad_a_armar)
            conn.execute(text("""
                UPDATE articulos SET stock_actual = stock_actual - :cant, actualizado_en = now()
                WHERE id = :id
            """), {"cant": a_descontar, "id": int(comp["articulo_id"])})
            aviso = _registrar_si_stock_negativo(conn, int(comp["articulo_id"]), f"armado de promoción '{nombre_promo}'")
            if aviso:
                avisos.append(aviso)

        conn.execute(text("""
            UPDATE articulos SET stock_actual = stock_actual + :cant, actualizado_en = now()
            WHERE id = :id
        """), {"cant": cantidad_a_armar, "id": promocion_articulo_id})

        registrar_historial(
            "articulos", promocion_articulo_id, "PROMOCION_ARMADA",
            f"Promoción '{nombre_promo}': +{cantidad_a_armar} unidad(es) armada(s), "
            f"componentes descontados: " + ", ".join(
                f"{comp['nombre']} -{float(comp['cantidad_por_unidad']) * float(cantidad_a_armar):g}"
                for _, comp in componentes.iterrows()
            ),
            conn=conn
        )
    return avisos

def eliminar_promocion(promocion_articulo_id):
    """Borra la promoción (y su composición, por ON DELETE CASCADE). Reusa eliminar_articulo."""
    return eliminar_articulo(promocion_articulo_id)

# ------------------------------------------------------------
# VENTAS
# ------------------------------------------------------------
def registrar_venta(articulo_id, cantidad, precio_unitario, fecha, medio_pago="Efectivo",
                     ticket_id=None, es_consumo_interno=False, registrado_por=None):
    """
    Venta de un producto puntual (botón rápido). Baja stock automáticamente
    (puede quedar negativo, no se bloquea -- ver _registrar_si_stock_negativo).
    Si es_consumo_interno=True, se guarda con monto_total=0 (no cuenta como
    ingreso) -- ver confirmar_consumo_interno, que además registra el costo
    real como gasto aparte.
    Si articulo_id es un combo de stock compartido, el trigger descuenta a
    sus componentes (no a la promoción) -- por eso los avisos de stock
    negativo se chequean también sobre ellos.
    registrado_por: id de la persona que atiende (tabla personas). Si no se
    pasa, se usa la persona elegida en la sesión (set_persona_actual).
    Devuelve la lista de avisos de stock negativo (vacía si no hay ninguno).
    """
    with engine.begin() as conn:
        return _insertar_venta(conn, articulo_id, cantidad, precio_unitario, fecha, medio_pago,
                               ticket_id, es_consumo_interno, registrado_por)

def _insertar_venta(conn, articulo_id, cantidad, precio_unitario, fecha, medio_pago="Efectivo",
                    ticket_id=None, es_consumo_interno=False, registrado_por=None):
    """Inserta una fila de venta DENTRO de la transacción `conn` (no hace commit).
    Devuelve los avisos de stock negativo. La usan registrar_venta y la
    confirmación del carrito, para que un ticket se grabe entero o no se grabe."""
    if registrado_por is None and persona_actual():
        registrado_por = persona_actual()["id"]
    # La base guarda la cantidad con 3 decimales: el monto se calcula sobre ESA
    # cantidad (150 g de una bolsa de 420 g = 0,357), así cantidad x precio = monto.
    cantidad = round(float(cantidad), 3)
    monto_total = 0.0 if es_consumo_interno else round(cantidad * float(precio_unitario), 2)
    conn.execute(text("""
        INSERT INTO ventas
            (articulo_id, cantidad, precio_unitario, monto_total, medio_pago, fecha, ticket_id,
             es_consumo_interno, registrado_por)
        VALUES (:aid, :cant, :precio, :monto, :medio, :fecha, :ticket, :consumo, :quien)
    """), {
        "aid": articulo_id, "cant": cantidad, "precio": precio_unitario,
        "monto": monto_total, "medio": medio_pago, "fecha": fecha, "ticket": ticket_id,
        "consumo": es_consumo_interno, "quien": registrado_por
    })
    return _avisos_stock_negativo_venta(conn, articulo_id, "venta")

def crear_ticket(fecha, medio_pago, conn=None):
    """Crea el ticket. Con `conn`, dentro de esa transacción; si no, en una propia."""
    query = text("INSERT INTO ventas_ticket (fecha, medio_pago) VALUES (:fecha, :medio) RETURNING id")
    params = {"fecha": fecha, "medio": medio_pago}
    if conn is not None:
        return conn.execute(query, params).scalar()
    with engine.begin() as c:
        return c.execute(query, params).scalar()

def confirmar_venta_carrito(items, fecha, medio_pago):
    """
    Confirma de una sola vez todos los ítems acumulados en el carrito.
    items: lista de dicts {articulo_id, cantidad, precio_unitario}.
    Recién acá se mueve el stock de cada ítem (no al agregarlos al carrito).
    Todo el ticket va en UNA transacción: si cualquier ítem falla (o se corta
    la conexión a mitad), no queda grabado nada -- ni el ticket, ni las
    ventas anteriores, ni el movimiento de stock -- y se puede reintentar
    sin duplicar.
    Devuelve (ticket_id, monto_total_del_ticket, lista_de_avisos_de_stock_negativo).
    """
    total = 0.0
    avisos = []
    with engine.begin() as conn:
        ticket_id = crear_ticket(fecha, medio_pago, conn=conn)
        for item in items:
            avisos.extend(_insertar_venta(
                conn, item["articulo_id"], item["cantidad"], item["precio_unitario"],
                fecha, medio_pago, ticket_id=ticket_id
            ))
            total += float(item["cantidad"]) * float(item["precio_unitario"])
    return ticket_id, round(total, 2), avisos

def confirmar_consumo_interno(items, fecha):
    """
    Mismo formato que confirmar_venta_carrito (items: lista de dicts
    {articulo_id, cantidad, precio_unitario, nombre}), pero para mercadería
    que se consume puertas adentro (ej: el dueño se lleva algo, se rompe un
    producto) en vez de venderse: descuenta stock igual que una venta común,
    NO computa ingreso (cada fila de venta queda con monto_total=0) y en
    cambio genera un único gasto agrupado con el costo real de lo
    consumido, para que quede reflejado en los reportes como egreso.
    Igual que la venta, todo (ticket, ventas, gasto e historial) va en UNA
    transacción: se graba entero o no se graba nada.
    Devuelve (ticket_id, costo_total_del_consumo, lista_de_avisos_de_stock_negativo).
    """
    costo_total = 0.0
    detalles = []
    avisos = []
    with engine.begin() as conn:
        ticket_id = crear_ticket(fecha, "Consumo interno", conn=conn)
        for item in items:
            costo_unitario = conn.execute(
                text("SELECT costo_compra_actual FROM articulos WHERE id = :id"),
                {"id": item["articulo_id"]}
            ).scalar()
            costo_total += float(costo_unitario or 0) * float(item["cantidad"])
            detalles.append(f"{item['cantidad']:g} x {item.get('nombre', item['articulo_id'])}")

        for item in items:
            avisos.extend(_insertar_venta(
                conn, item["articulo_id"], item["cantidad"], item["precio_unitario"],
                fecha, medio_pago="Consumo interno", ticket_id=ticket_id, es_consumo_interno=True
            ))

        if costo_total > 0:
            registrar_gasto(
                "Consumo interno", round(costo_total, 2), fecha,
                descripcion="Consumo interno: " + ", ".join(detalles), conn=conn
            )
        registrar_historial(
            "ventas", ticket_id, "CONSUMO_INTERNO",
            f"Consumo interno registrado (ticket #{ticket_id}): " + ", ".join(detalles) +
            f" -- costo ${round(costo_total, 2)}",
            conn=conn
        )
    return ticket_id, round(costo_total, 2), avisos

def listar_ventas(desde=None, hasta=None):
    query = """
        SELECT v.id, v.ticket_id, v.fecha, a.nombre AS articulo,
               v.cantidad, a.unidad_medida, v.precio_unitario, v.monto_total AS total, v.medio_pago,
               v.es_consumo_interno, p.nombre AS registrado_por
        FROM ventas v
        LEFT JOIN articulos a ON a.id = v.articulo_id
        LEFT JOIN personas p ON p.id = v.registrado_por
        WHERE 1=1
    """
    params = {}
    if desde:
        query += " AND v.fecha >= :desde"
        params["desde"] = desde
    if hasta:
        query += " AND v.fecha <= :hasta"
        params["hasta"] = hasta
    query += " ORDER BY v.fecha DESC, v.id DESC"
    return pd.read_sql(text(query), engine, params=params)

def detalle_de_ticket(ticket_id):
    return pd.read_sql(text("""
        SELECT v.id, a.nombre AS articulo, v.cantidad,
               a.unidad_medida, v.precio_unitario, v.monto_total AS total
        FROM ventas v LEFT JOIN articulos a ON a.id = v.articulo_id
        WHERE v.ticket_id = :tid
        ORDER BY v.id
    """), engine, params={"tid": ticket_id})

def eliminar_venta(venta_id):
    """Borra la venta (deja constancia en el historial); el trigger trg_revertir_venta repone el stock automáticamente."""
    with engine.begin() as conn:
        venta = conn.execute(text("SELECT * FROM ventas WHERE id = :id"), {"id": venta_id}).mappings().first()
        conn.execute(text("DELETE FROM ventas WHERE id = :id"), {"id": venta_id})
        if venta:
            registrar_historial(
                "ventas", venta_id, "ELIMINACION",
                f"Venta #{venta_id} eliminada: {_cant_txt(venta['cantidad'])} x articulo_id={venta['articulo_id']}, "
                f"total ${venta['monto_total']}, fecha {venta['fecha']}",
                conn=conn
            )

def editar_venta(venta_id, fecha, medio_pago, articulo_id, cantidad, precio_unitario,
                  es_consumo_interno=False):
    """
    Actualiza una venta ya cargada (siempre de un artículo puntual: la
    "venta genérica" sin producto se dio de baja en 10/2026 y no quedan
    ventas sin artículo). monto_total se recalcula como cantidad x precio,
    salvo consumo interno, que queda en 0.
    El trigger trg_editar_venta ajusta el stock automáticamente (puede
    quedar negativo, no se bloquea), aunque el artículo sea un combo de
    stock compartido (ahí ajusta a los componentes, no a la promoción).
    Devuelve la lista de avisos de stock negativo (vacía si no hay ninguno).
    """
    if articulo_id is None or cantidad is None or precio_unitario is None:
        raise ValueError("Elegí un artículo, una cantidad y un precio para la venta.")
    monto_total = 0.0 if es_consumo_interno else round(float(cantidad) * float(precio_unitario), 2)
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE ventas
            SET articulo_id = :aid, cantidad = :cant, precio_unitario = :precio,
                monto_total = :monto, medio_pago = :medio, fecha = :fecha,
                es_consumo_interno = :consumo
            WHERE id = :id
        """), {
            "id": venta_id, "aid": articulo_id, "cant": cantidad, "precio": precio_unitario,
            "monto": monto_total, "medio": medio_pago, "fecha": fecha, "consumo": es_consumo_interno
        })
        return _avisos_stock_negativo_venta(conn, articulo_id, "edición de venta")

def obtener_venta(venta_id):
    query = text("SELECT * FROM ventas WHERE id = :id")
    with engine.connect() as conn:
        row = conn.execute(query, {"id": venta_id}).mappings().first()
    return dict(row) if row else None

# ------------------------------------------------------------
# GASTOS
# ------------------------------------------------------------
def registrar_gasto(categoria, monto, fecha, descripcion=None, conn=None):
    """Con `conn`, se inserta dentro de esa transacción; si no, en una propia."""
    query = text("""
        INSERT INTO gastos (categoria, monto, fecha, descripcion)
        VALUES (:cat, :monto, :fecha, :desc)
    """)
    params = {"cat": categoria, "monto": monto, "fecha": fecha, "desc": descripcion}
    if conn is not None:
        conn.execute(query, params)
        return
    with engine.begin() as c:
        c.execute(query, params)

def listar_gastos(desde=None, hasta=None):
    query = "SELECT * FROM gastos WHERE 1=1"
    params = {}
    if desde:
        query += " AND fecha >= :desde"
        params["desde"] = desde
    if hasta:
        query += " AND fecha <= :hasta"
        params["hasta"] = hasta
    query += " ORDER BY fecha DESC"
    return pd.read_sql(text(query), engine, params=params)

def eliminar_gasto(gasto_id):
    """
    Borra un gasto (por ejemplo, uno cargado dos veces). No afecta stock
    ni costos: solo cambia el reporte de ganancias vs gastos. Deja
    registro en el historial de cambios con los datos del gasto borrado.
    """
    with engine.begin() as conn:
        g = conn.execute(
            text("SELECT fecha, categoria, monto, descripcion FROM gastos WHERE id = :id"),
            {"id": gasto_id}
        ).mappings().first()
        if g is None:
            return
        conn.execute(text("DELETE FROM gastos WHERE id = :id"), {"id": gasto_id})
        detalle = (f"Gasto #{gasto_id} eliminado: {g['categoria']} ${g['monto']} "
                   f"del {g['fecha']:%d/%m/%Y}")
        if g["descripcion"]:
            detalle += f" ({g['descripcion']})"
        registrar_historial("gastos", gasto_id, "ELIMINACION", detalle, conn=conn)

# ------------------------------------------------------------
# REPORTE GASTOS VS GANANCIAS
# ------------------------------------------------------------
def reporte_ganancias_vs_gastos(desde, hasta):
    ventas = listar_ventas(desde, hasta)
    gastos = listar_gastos(desde, hasta)

    ingresos_totales = ventas["total"].sum() if not ventas.empty else 0.0

    # Costo de mercadería vendida. Excluye consumo interno: ese costo ya se cuenta una vez
    # como gasto (categoría "Consumo interno"); contarlo de nuevo acá
    # duplicaría la pérdida en la ganancia neta.
    # Desde 12/10/2026 sale de v_ventas_analitica: usa el costo vigente al
    # momento de cada venta (si está guardado) y resuelve los combos en
    # sus componentes, igual que Márgenes reales -- así los dos reportes
    # dan el mismo costo de mercadería.
    query = text("""
        SELECT COALESCE(SUM(costo_atribuido), 0) AS costo_mercaderia
        FROM v_ventas_analitica
        WHERE fecha BETWEEN :desde AND :hasta AND es_consumo_interno = FALSE
    """)
    with engine.connect() as conn:
        costo_mercaderia = conn.execute(query, {"desde": desde, "hasta": hasta}).scalar()
    costo_mercaderia = float(costo_mercaderia or 0)

    gastos_totales = gastos["monto"].sum() if not gastos.empty else 0.0
    ganancia_bruta = ingresos_totales - costo_mercaderia
    ganancia_neta = ganancia_bruta - gastos_totales

    return {
        "ingresos_totales": ingresos_totales,
        "costo_mercaderia": costo_mercaderia,
        "ganancia_bruta": ganancia_bruta,
        "gastos_totales": gastos_totales,
        "ganancia_neta": ganancia_neta,
    }

# ------------------------------------------------------------
# VENTAS ANALÍTICAS (desglosa combos en sus componentes)
# Base para el núcleo analítico pendiente (ABC, márgenes reales,
# pronóstico de demanda): esas vistas deberían leer de acá, no de
# 'ventas' cruda, para no quedar ciegas a lo vendido en promoción.
# ------------------------------------------------------------
def obtener_ventas_analiticas(desde=None, hasta=None):
    """Filas de v_ventas_analitica (una venta de combo ya resuelta en sus
    componentes reales, con ingreso y costo repartidos)."""
    query = "SELECT * FROM v_ventas_analitica WHERE 1=1"
    params = {}
    if desde:
        query += " AND fecha >= :desde"
        params["desde"] = desde
    if hasta:
        query += " AND fecha <= :hasta"
        params["hasta"] = hasta
    return pd.read_sql(text(query), engine, params=params)

def ventas_por_articulo_real(desde=None, hasta=None):
    """
    Agregado por artículo a partir de v_ventas_analitica: cantidad
    vendida, ingreso y costo ya atribuidos (incluyendo lo vendido dentro
    de combos), con el margen resultante. Es el "¿cuánto se vendió de
    este producto realmente?" correcto -- a diferencia de agrupar
    'ventas' directamente, que esconde lo vendido vía promoción.
    """
    df = obtener_ventas_analiticas(desde, hasta)
    if df.empty:
        return df
    agg = df.groupby(["articulo_id", "articulo"], as_index=False).agg(
        cantidad=("cantidad", "sum"),
        ingreso_atribuido=("monto_atribuido", "sum"),
        costo_atribuido=("costo_atribuido", "sum"),
    )
    agg["margen_atribuido"] = agg["ingreso_atribuido"] - agg["costo_atribuido"]
    return agg.sort_values("ingreso_atribuido", ascending=False).reset_index(drop=True)

# ------------------------------------------------------------
# NÚCLEO ANALÍTICO -- FASE 1 (04/10/2026)
# Clasificación ABC, stock inmovilizado y márgenes reales -- los primeros
# 3 de los 6 componentes del núcleo analítico que exige la tesis (ver
# claude/roadmap-nucleo-analitico.md, documento del proyecto Seminario II,
# para el diseño completo de las 3 fases). Las tres funciones de acá abajo
# leen de v_ventas_analitica (vía ventas_por_articulo_real/obtener_ventas_
# analiticas), no de 'ventas' cruda, para no quedar ciegas a lo vendido
# dentro de combos/promociones -- el mismo motivo por el que existe esa
# vista (ver schema.sql).
# ------------------------------------------------------------

def clasificacion_abc(desde=None, hasta=None, corte_a=80.0, corte_b=95.0):
    """
    Clasificación ABC por contribución a las ventas (análisis de Pareto),
    sobre el ingreso real atribuido a cada artículo (incluye lo vendido
    dentro de combos). Clase A: artículos cuyo % acumulado de ingreso,
    ordenados de mayor a menor venta, cae hasta corte_a (80% por defecto).
    Clase B: hasta corte_b (95% por defecto). Clase C: el resto.

    Solo clasifica artículos con AL MENOS una venta en el período -- los
    que no vendieron nada no tienen "contribución" que clasificar; esos
    son candidatos a stock_inmovilizado() (función de abajo), que es el
    complemento natural de esta clasificación, no un caso más de la C.

    No se recalcula ni se guarda en la base: se recalcula al vuelo cada
    vez que se pide, sobre el rango de fechas elegido en la UI, porque la
    clase de un artículo cambia con el tiempo (estacionalidad, artículos
    nuevos, etc.) -- guardarla fija la volvería obsoleta.
    """
    df = ventas_por_articulo_real(desde, hasta)
    if df.empty:
        return df
    df = df.sort_values("ingreso_atribuido", ascending=False).reset_index(drop=True)
    total_ingreso = df["ingreso_atribuido"].sum()
    df["pct_ingreso"] = (df["ingreso_atribuido"] / total_ingreso * 100) if total_ingreso else 0.0
    df["pct_acumulado"] = df["pct_ingreso"].cumsum()
    df["clase"] = "C"
    df.loc[df["pct_acumulado"] <= corte_a, "clase"] = "A"
    df.loc[(df["pct_acumulado"] > corte_a) & (df["pct_acumulado"] <= corte_b), "clase"] = "B"
    # Los artículos dados de baja se SIGUEN clasificando: sus ventas del
    # período ocurrieron, y sacarlas cambiaría el % de todos los demás. Se
    # marcan para que se lean como lo que son (demanda que ya pasó al
    # reemplazo o que se deja de atender).
    estado = pd.read_sql("SELECT id AS articulo_id, activo FROM articulos", engine)
    df = df.merge(estado, on="articulo_id", how="left")
    df["activo"] = df["activo"].fillna(True).astype(bool)
    return df

def resumen_abc(desde=None, hasta=None, corte_a=80.0, corte_b=95.0, df=None):
    """Cantidad de artículos e ingreso total por clase A/B/C -- para los stat tiles de la UI.
    Si ya se calculó clasificacion_abc() para el mismo período, pasarla en df evita
    repetir la consulta."""
    if df is None:
        df = clasificacion_abc(desde, hasta, corte_a, corte_b)
    if df.empty:
        return df
    resumen = df.groupby("clase", as_index=False).agg(
        cantidad_articulos=("articulo_id", "count"),
        ingreso_total=("ingreso_atribuido", "sum"),
    )
    resumen["pct_articulos"] = resumen["cantidad_articulos"] / resumen["cantidad_articulos"].sum() * 100
    resumen["pct_ingreso"] = resumen["ingreso_total"] / resumen["ingreso_total"].sum() * 100
    orden = {"A": 0, "B": 1, "C": 2}
    return resumen.sort_values("clase", key=lambda s: s.map(orden)).reset_index(drop=True)

# ------------------------------------------------------------
# VALOR DE LA MERCADERÍA EN STOCK (18/10/2026)
# Foto de HOY (no depende de un rango de fechas): cuánta plata hay
# invertida en mercadería. Por eso va en el Dashboard, en Artículos y
# junto al stock inmovilizado -- no en Ganancias y Gastos, que es todo
# "del período".
# ------------------------------------------------------------
def resumen_valor_stock(articulos):
    """
    Valor de la mercadería a partir de un DataFrame con columnas de
    v_articulos (stock_actual, costo_compra_actual, costo_venta_con_iva,
    iva, categoria, promo_tipo). Un solo cálculo para el Dashboard, el
    listado de Artículos (sobre lo filtrado) y el stock inmovilizado.

    Criterios:
    - valor_costo = stock x costo de compra actual (la plata invertida).
    - valor_venta = stock x precio de lista con IVA (lo que entraría si se
      vendiera todo); ganancia_potencial = valor de venta sin IVA - costo.
    - Stock negativo cuenta como 0: no es mercadería que se tenga.
    - Combos 'virtual' se excluyen: su stock es el de sus componentes, que
      ya se cuentan (sería contar dos veces lo mismo). Los combos 'fisica'
      ya armados sí cuentan.
    - Los inactivos con stock cuentan: la mercadería está físicamente.
    - Artículos con stock y costo $0 se informan aparte: el total sale
      subvaluado mientras no se les cargue el costo.
    """
    vacio = {"valor_costo": 0.0, "valor_venta": 0.0, "ganancia_potencial": 0.0,
             "articulos_con_stock": 0, "sin_costo": 0, "valor_consignacion": 0.0}
    if articulos is None or articulos.empty:
        return vacio
    df = articulos
    if "promo_tipo" in df.columns:
        df = df[~((df["categoria"] == CATEGORIA_PROMOCION) & (df["promo_tipo"] == "virtual"))]
    stock = pd.to_numeric(df["stock_actual"], errors="coerce").fillna(0).clip(lower=0)
    costo = pd.to_numeric(df["costo_compra_actual"], errors="coerce").fillna(0)
    precio = pd.to_numeric(df["costo_venta_con_iva"], errors="coerce").fillna(0)
    iva = pd.to_numeric(df["iva"], errors="coerce").fillna(0.21) if "iva" in df.columns else 0.21
    # Consignación (tanda 14): está en el local y se vende, pero no es plata invertida
    # (se paga cuando se vende). Va aparte de valor_costo; sí cuenta para el valor de
    # venta y la ganancia potencial.
    consig = df["en_consignacion"].fillna(False).astype(bool) if "en_consignacion" in df.columns else False
    valor_consignacion = float((stock * costo)[consig].sum()) if "en_consignacion" in df.columns else 0.0
    valor_costo = float((stock * costo).sum()) - valor_consignacion
    valor_venta = float((stock * precio).sum())
    valor_venta_sin_iva = float((stock * precio / (1 + iva)).sum())
    return {
        "valor_costo": valor_costo,
        "valor_venta": valor_venta,
        "ganancia_potencial": valor_venta_sin_iva - valor_costo - valor_consignacion,
        "valor_consignacion": valor_consignacion,
        "articulos_con_stock": int((stock > 0).sum()),
        "sin_costo": int(((stock > 0) & (costo <= 0)).sum()),
    }

def valor_stock():
    """Valor de toda la mercadería en stock hoy (activos e inactivos con stock)."""
    return resumen_valor_stock(listar_articulos(incluir_inactivos=True))

# Un artículo que nunca se vendió y se dio de alta hace menos de esto se
# considera "recién cargado": se lista, pero no suma en lo inmovilizado.
DIAS_RECIEN_CARGADO = 7

def registro_ventas():
    """Desde cuándo hay ventas registradas en la app y cuántos días son
    (contando hoy). El stock inmovilizado no puede evaluar un plazo más
    largo que este: sin historia, "60 días sin venta" no se puede saber."""
    with engine.connect() as conn:
        primera, hoy = conn.execute(text("SELECT MIN(fecha), (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date FROM ventas")).one()
    return {"primera_venta": primera, "dias": (hoy - primera).days if primera else 0}

def stock_inmovilizado(dias_sin_venta=60):
    """
    Artículos con stock que no se mueven -- complemento directo de la
    clasificación ABC: son los que esa clasificación no llega a ver porque
    no vendieron nada. Devuelve dos grupos (columna 'grupo'):

    - 'sin_venta_reciente': se vendieron alguna vez, pero no en los
      últimos dias_sin_venta días.
    - 'nunca': nunca se vendieron desde que se cargaron. NO depende de
      dias_sin_venta: antes se los contaba desde la fecha de alta y se
      los mostraba recién cuando pasaban esos días, y como el catálogo se
      cargó todo junto el 28/09/2026, con 60 días no aparecía ninguno
      (171 artículos, ~31% de la mercadería, quedaban invisibles). Se marca
      'recien_cargado' si el alta tiene menos de DIAS_RECIEN_CARGADO días.

    'en_consignacion' se informa para no sumarla como plata parada: el
    total de mercadería (valor_stock) tampoco la incluye.

    "Venta" incluye lo vendido dentro de combos y el consumo interno (sale
    del stock igual). Para una bolsa que se fracciona, también cuenta lo
    vendido de su suelto y el propio fraccionamiento: la bolsa se mueve
    aunque nunca se venda entera.
    """
    query = text("""
        WITH mov AS (
            -- lo tirado por vencido (tanda 18) no es movimiento: el artículo sigue parado
            SELECT articulo_id, MAX(fecha) AS fecha
            FROM v_ventas_analitica WHERE medio_pago IS DISTINCT FROM 'Vencido' GROUP BY articulo_id
            UNION ALL
            SELECT s.articulo_fraccion_de, MAX(v.fecha)
            FROM v_ventas_analitica v JOIN articulos s ON s.id = v.articulo_id
            WHERE s.articulo_fraccion_de IS NOT NULL AND v.medio_pago IS DISTINCT FROM 'Vencido'
            GROUP BY s.articulo_fraccion_de
            UNION ALL
            SELECT articulo_bolsa_id, MAX(fecha)
            FROM fraccionamientos GROUP BY articulo_bolsa_id
        ),
        ultima AS (
            SELECT articulo_id, MAX(fecha) AS fecha_ultima_venta FROM mov GROUP BY articulo_id
        ),
        -- igual que listar_articulos(): la mercadería en consignación no es plata invertida
        consig AS (
            SELECT app.articulo_id,
                   bool_and(COALESCE((to_jsonb(p) ->> 'consignacion')::boolean, FALSE)) AS en_consignacion
            FROM articulo_proveedor_precio app JOIN proveedores p ON p.id = app.proveedor_id
            GROUP BY app.articulo_id
        )
        SELECT
            a.id AS articulo_id, a.nombre AS articulo, a.categoria,
            a.stock_actual, a.costo_compra_actual,
            ROUND(a.stock_actual * a.costo_compra_actual, 2) AS valor_inmovilizado,
            a.activo,
            a.creado_en::date AS fecha_alta,
            (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - a.creado_en::date AS dias_desde_alta,
            ultima.fecha_ultima_venta,
            ultima.fecha_ultima_venta IS NULL AS nunca_vendido,
            (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - COALESCE(ultima.fecha_ultima_venta, a.creado_en::date) AS dias_sin_venta,
            CASE WHEN ultima.fecha_ultima_venta IS NULL THEN 'nunca' ELSE 'sin_venta_reciente' END AS grupo,
            (ultima.fecha_ultima_venta IS NULL
             AND (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - a.creado_en::date < :gracia) AS recien_cargado,
            COALESCE(consig.en_consignacion, FALSE) AS en_consignacion
        FROM articulos a
        LEFT JOIN ultima ON ultima.articulo_id = a.id
        LEFT JOIN consig ON consig.articulo_id = a.id
        WHERE a.categoria IS DISTINCT FROM 'Promocion'
          AND a.stock_actual > 0
          AND (ultima.fecha_ultima_venta IS NULL
               OR ultima.fecha_ultima_venta <= (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - CAST(:dias AS INTEGER))
        ORDER BY valor_inmovilizado DESC
    """)
    return pd.read_sql(query, engine, params={"dias": int(dias_sin_venta), "gracia": DIAS_RECIEN_CARGADO})

# Umbrales de "Márgenes reales" (13/10/2026), elegidos a criterio como
# punto de partida -- a recalibrar con datos reales del comercio.
MARGEN_BAJO_PP = 5.0     # "deja poco": margen sobre venta X puntos debajo del promedio del negocio
UMBRAL_PRECIO = 1.0      # diferencias de cobro menores a $1 (redondeos) no se informan

def margenes_reales(desde=None, hasta=None):
    """
    Cuánto dejó realmente cada artículo en el período, a partir de
    v_ventas_analitica (incluye lo vendido dentro de combos).

    Pregunta principal (13/10/2026): "¿cuánto te dejó cada producto?"
    - ganancia_real = ingreso neto de IVA - costo
    - margen_real_sobre_venta = ganancia / ingreso neto (margen bruto, la
      métrica habitual en retail -- la que se muestra al comerciante)
    - margen_real_sobre_costo = ganancia / costo (markup: misma fórmula que
      articulos.margen_ganancia, por eso es la que se compara contra el
      margen configurado en el detalle técnico)
    Las dos nociones se calculan por separado para no mezclarlas (punto que
    la cátedra pidió precisar con fuente: Horngren et al., markup vs. margen
    bruto -- ver claude/fase1-nucleo-analitico-abc-margenes.md).

    Control secundario: ¿se cobró el precio del sistema de ese día?
    - dif_precio = (cobrado - precio de lista guardado al vender), en pesos
      sin IVA, solo sobre ventas que tienen precio de lista guardado. En el
      carrito el precio lo pone el sistema, así que normalmente da ~0; lo
      distinto de cero sale de ventas editadas a mano o redondeos.

    Por qué ya no se compara contra "costo x margen configurado" (versión del
    12/10): ese margen es el de HOY, y cualquier cambio de precio hacía
    aparecer las ventas anteriores como pérdidas que no existieron (caso
    real: AGUA BIDON GARUA 5L, $3.700 hasta el 05/10 y $5.000 después).

    El ingreso de v_ventas_analitica viene CON IVA; se netea con el IVA real
    de cada artículo. Excluye el consumo interno (monto $0: sumaría costo
    sin ingreso). El costo es el vigente al vender (ventas.costo_unitario);
    las ventas anteriores al 12/10/2026 usan el costo actual.
    """
    lista = "va.lista_atribuida"
    query = text(f"""
        SELECT
            va.articulo_id, va.articulo, a.categoria, a.unidad_medida,
            a.iva, a.margen_ganancia AS margen_configurado,
            SUM(va.cantidad) AS cantidad,
            SUM(CASE WHEN va.promocion_id IS NOT NULL THEN va.cantidad ELSE 0 END) AS cantidad_en_combo,
            SUM(va.monto_atribuido) AS ingreso_con_iva,
            SUM(va.costo_atribuido) AS costo_real,
            SUM({lista}) AS lista_con_iva,
            SUM(CASE WHEN {lista} IS NOT NULL THEN va.monto_atribuido END) AS cobrado_controlado_con_iva,
            SUM(CASE WHEN {lista} IS NOT NULL THEN va.cantidad END) AS cantidad_controlada
        FROM v_ventas_analitica va
        JOIN articulos a ON a.id = va.articulo_id
        WHERE a.categoria IS DISTINCT FROM 'Promocion'
          AND va.es_consumo_interno = FALSE
        """ + ("AND va.fecha >= :desde " if desde else "") + ("AND va.fecha <= :hasta " if hasta else "") + """
        GROUP BY va.articulo_id, va.articulo, a.categoria, a.unidad_medida, a.iva, a.margen_ganancia
    """)
    params = {}
    if desde:
        params["desde"] = desde
    if hasta:
        params["hasta"] = hasta
    df = pd.read_sql(query, engine, params=params)
    if df.empty:
        return df

    for col in ["iva", "margen_configurado", "cantidad", "cantidad_en_combo", "ingreso_con_iva",
                "costo_real", "lista_con_iva", "cobrado_controlado_con_iva", "cantidad_controlada"]:
        df[col] = df[col].astype(float)
    df["ingreso_neto"] = df["ingreso_con_iva"] / (1 + df["iva"])
    df["ganancia_real"] = df["ingreso_neto"] - df["costo_real"]

    costo_seguro = df["costo_real"].replace(0, float("nan"))
    ingreso_seguro = df["ingreso_neto"].replace(0, float("nan"))
    df["margen_real_sobre_costo"] = df["ganancia_real"] / costo_seguro
    df["margen_real_sobre_venta"] = df["ganancia_real"] / ingreso_seguro
    # Desvío contra el margen configurado HOY (solo para el detalle técnico:
    # si el margen se cambió durante el período, el desvío lo refleja).
    df["desvio_pp"] = (df["margen_real_sobre_costo"] - df["margen_configurado"]) * 100

    cant = df["cantidad"].replace(0, float("nan"))
    df["pct_en_combo"] = df["cantidad_en_combo"] / cant
    # Control de precio (solo ventas con precio de lista guardado)
    df["dif_precio"] = (df["cobrado_controlado_con_iva"] - df["lista_con_iva"]) / (1 + df["iva"])
    cant_ctrl = df["cantidad_controlada"].replace(0, float("nan"))
    df["precio_lista_prom"] = df["lista_con_iva"] / cant_ctrl
    df["precio_cobrado_prom"] = df["cobrado_controlado_con_iva"] / cant_ctrl

    return df.sort_values("ingreso_neto", ascending=False).reset_index(drop=True)

def margenes_reales_por_categoria(desde=None, hasta=None, df=None):
    """Mismo cálculo que margenes_reales(), agregado por categoría (suma primero, después se
    recalcula el % -- nunca promediar los % de cada artículo, eso pesa igual a un
    artículo que vendió $100 que a uno que vendió $1.000.000). Si ya se calculó
    margenes_reales() para el mismo período, pasarlo en df evita repetir la consulta."""
    if df is None:
        df = margenes_reales(desde, hasta)
    if df.empty:
        return df
    agg = df.assign(categoria=df["categoria"].fillna("Sin categoría")).groupby("categoria", as_index=False).agg(
        cantidad=("cantidad", "sum"),
        ingreso_con_iva=("ingreso_con_iva", "sum"),
        ingreso_neto=("ingreso_neto", "sum"),
        costo_real=("costo_real", "sum"),
        ganancia_real=("ganancia_real", "sum"),
    )
    costo_seguro = agg["costo_real"].replace(0, float("nan"))
    ingreso_seguro = agg["ingreso_neto"].replace(0, float("nan"))
    agg["margen_real_sobre_costo"] = agg["ganancia_real"] / costo_seguro
    agg["margen_real_sobre_venta"] = agg["ganancia_real"] / ingreso_seguro
    return agg.sort_values("ganancia_real", ascending=False).reset_index(drop=True)

def margenes_por_combo(desde=None, hasta=None):
    """
    Cuánto deja cada combo/promoción: ingreso neto, costo, ganancia y margen
    sobre venta, sumando sus componentes (v_ventas_analitica ya reparte la
    venta del combo entre ellos). Sin consumo interno.
    """
    query = text("""
        SELECT va.promocion_id, va.promocion_nombre AS combo,
               SUM(va.monto_atribuido / (1 + a.iva)) AS ingreso_neto,
               SUM(va.costo_atribuido) AS costo_real,
               COUNT(DISTINCT va.venta_id) AS ventas
        FROM v_ventas_analitica va
        JOIN articulos a ON a.id = va.articulo_id
        WHERE va.promocion_id IS NOT NULL AND va.es_consumo_interno = FALSE
        """ + ("AND va.fecha >= :desde " if desde else "") + ("AND va.fecha <= :hasta " if hasta else "") + """
        GROUP BY va.promocion_id, va.promocion_nombre
    """)
    params = {}
    if desde:
        params["desde"] = desde
    if hasta:
        params["hasta"] = hasta
    df = pd.read_sql(query, engine, params=params)
    if df.empty:
        return df
    # unidades de combo vendidas (de 'ventas': en la vista cada fila es un componente)
    q_unid = "SELECT articulo_id AS promocion_id, SUM(cantidad) AS unidades FROM ventas WHERE es_consumo_interno = FALSE"
    if desde:
        q_unid += " AND fecha >= :desde"
    if hasta:
        q_unid += " AND fecha <= :hasta"
    q_unid += " GROUP BY articulo_id"
    unidades = pd.read_sql(text(q_unid), engine, params=params)
    df = df.merge(unidades, on="promocion_id", how="left")
    for col in ["ingreso_neto", "costo_real", "unidades"]:
        df[col] = df[col].astype(float)
    df["ganancia_real"] = df["ingreso_neto"] - df["costo_real"]
    df["margen_real_sobre_venta"] = df["ganancia_real"] / df["ingreso_neto"].replace(0, float("nan"))
    return df.sort_values("ingreso_neto", ascending=False).reset_index(drop=True)

def margen_promedio(df):
    """Margen sobre venta del conjunto (ganancia total / ingreso neto total), o None."""
    if df is None or df.empty or df["ingreso_neto"].sum() == 0:
        return None
    return float(df["ganancia_real"].sum() / df["ingreso_neto"].sum())

def articulos_que_dejan_poco(df):
    """
    "Venden mucho pero dejan poco": artículos en la mitad superior por
    ingreso del período cuyo margen sobre venta está MARGEN_BAJO_PP puntos
    o más por debajo del promedio del negocio. Ordenados por ingreso.
    """
    prom = margen_promedio(df)
    if prom is None:
        return df.iloc[0:0]
    corte_ingreso = df["ingreso_neto"].median()
    bajos = df[(df["ingreso_neto"] >= corte_ingreso)
               & (df["margen_real_sobre_venta"] < prom - MARGEN_BAJO_PP / 100)]
    return bajos.sort_values("ingreso_neto", ascending=False)

def pista_margen(fila, promedio):
    """
    Explicación en lenguaje simple de por qué un artículo deja poco. Son
    pistas, no diagnósticos: el sistema sabe cuánto dejó, no por qué.
    """
    if fila["pct_en_combo"] >= 0.5:
        return ("La mayor parte se vendió dentro de un combo, y el precio del combo le deja menos "
                "ganancia que venderlo suelto. Revisá si la promo todavía conviene.")
    margen_cfg = fila["margen_configurado"] / (1 + fila["margen_configurado"])  # markup -> margen sobre venta
    if margen_cfg < promedio - MARGEN_BAJO_PP / 100:
        return (f"El margen cargado ya es bajo ({margen_cfg:.0%} sobre la venta). "
                "Si no es a propósito (producto gancho, precio de competencia), revisá el margen en Artículos.")
    return ("El margen cargado es normal, pero el realizado fue menor: puede que el precio se haya "
            "cambiado durante el período o que haya ventas editadas a mano.")

def resumen_margenes(desde, hasta):
    """
    Resumen para la tarjeta del Dashboard: ingreso neto, ganancia real,
    margen sobre venta, el artículo que más dejó y la diferencia total de
    cobro contra el precio de lista (control). None si no hay ventas.
    """
    df = margenes_reales(desde, hasta)
    if df.empty:
        return None
    mejor = df.sort_values("ganancia_real", ascending=False).iloc[0]
    return {
        "ingreso_neto": float(df["ingreso_neto"].sum()),
        "ganancia_real": float(df["ganancia_real"].sum()),
        "margen": margen_promedio(df),
        "mejor_articulo": mejor["articulo"],
        "mejor_ganancia": float(mejor["ganancia_real"]),
        "dif_precio": float(df["dif_precio"].fillna(0).sum()),
        "cantidad_dejan_poco": len(articulos_que_dejan_poco(df)),
    }

def porcentaje_ventas_sin_costo_historico(desde=None, hasta=None):
    """
    % de las ventas del período registradas antes de que se empezara a
    guardar el costo de cada venta (usan el costo actual). None si la
    columna todavía no existe (migración no corrida) -- la app no se rompe.
    """
    try:
        query = "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE costo_unitario IS NULL) AS sin_costo FROM ventas WHERE 1=1"
        params = {}
        if desde:
            query += " AND fecha >= :desde"
            params["desde"] = desde
        if hasta:
            query += " AND fecha <= :hasta"
            params["hasta"] = hasta
        with engine.connect() as conn:
            fila = conn.execute(text(query), params).first()
        if not fila or not fila.total:
            return 0.0
        return 100.0 * fila.sin_costo / fila.total
    except Exception:
        return None

# ------------------------------------------------------------
# CALIDAD DE DATOS (12/10/2026)
# Indicador de confiabilidad de los datos sobre los que corre el núcleo
# analítico: el ABC, los márgenes y la reposición valen tanto como los
# datos que los alimentan. Cada artículo pasa por 5 chequeos; el
# indicador es el % de artículos que no cae en NINGUNO.
#
# Se cuenta por artículo y no por alerta: un artículo con 2 problemas
# cuenta una sola vez, así el número responde "¿de cuántos productos me
# puedo fiar?" y no se infla cuando un mismo artículo acumula errores.
# Limitación: todas las alertas pesan igual, y "sin alerta" no garantiza
# que el dato sea correcto, solo que no cayó en ninguna regla (un desvío
# chico de stock no se detecta) -- el control que valida al resto es el
# conteo físico por muestreo.
# ------------------------------------------------------------

# Umbrales documentados -- cambiar acá, se muestran solos en la UI.
UMBRALES_CALIDAD = {
    "dias_sin_venta": 60,        # stock > 0 y sin ventas en N días
    "desvios_anomalia": 3,       # venta > media + k desvíos del historial del artículo
    "min_ventas_historial": 8,   # ventas previas mínimas para evaluar atipicidad
    "ventana_reciente_dias": 14, # ventana donde se buscan ventas atípicas
}
SEMAFORO_CALIDAD = {"verde": 90, "amarillo": 75}  # % de artículos sin alertas

# clave: (nombre visible, acción sugerida)
CHEQUEOS_CALIDAD = {
    "stock_negativo": ("Stock negativo", "Cargar la factura de compra pendiente o hacer conteo"),
    "stock_sin_movimiento": ("Stock sin movimiento", "Verificar si está en góndola o depósito"),
    "venta_atipica": ("Venta atípica", "Revisar si la cantidad o el producto están bien cargados"),
    "precio_bajo_costo": ("Precio sin ganancia", "Corregir el margen en Artículos"),
    "sin_costo": ("Sin costo cargado", "Cargar el costo (factura o Artículos)"),
    "inactivo_con_stock": ("Inactivo con stock",
                           "Liquidar el remanente, o hacer conteo y dejar el stock en 0"),
}

# Artículos dados de baja (articulos.activo = FALSE, 14/10/2026): salen del
# indicador, porque ya no se gestionan y sus datos (costo, margen) no se
# van a mantener. Excepción: si les queda stock distinto de cero siguen
# adentro, con su propia alerta ("Inactivo con stock", o "Stock negativo")
# -- hay mercadería o un descuadre que resolver. Así dar de baja no sirve
# para esconder un problema: solo sale del indicador lo que quedó en cero.

def _alertas_calidad_sql():
    """
    Los chequeos que se resuelven directo en SQL sobre 'articulos'. A los
    inactivos solo se les aplican stock negativo e inactivo con stock.
    """
    u = UMBRALES_CALIDAD
    query = text("""
        WITH base AS (
            SELECT a.id AS articulo_id, a.nombre, a.marca, a.categoria,
                   a.stock_actual, a.costo_compra_actual, a.margen_ganancia,
                   a.activo, a.fecha_baja,
                   a.creado_en::date AS fecha_alta,
                   ultima.fecha_ultima_venta
            FROM articulos a
            LEFT JOIN (
                SELECT articulo_id, MAX(fecha) AS fecha_ultima_venta
                FROM v_ventas_analitica WHERE medio_pago IS DISTINCT FROM 'Vencido' GROUP BY articulo_id
            ) ultima ON ultima.articulo_id = a.id
            WHERE a.categoria IS DISTINCT FROM 'Promocion'
        )
        SELECT articulo_id, 'stock_negativo' AS chequeo,
               'Stock en sistema: ' || TRIM(TO_CHAR(stock_actual, 'FM999999990.##')) AS detalle
        FROM base WHERE stock_actual < 0

        UNION ALL
        -- Si nunca se vendió, se cuenta desde la fecha de alta del artículo:
        -- así un artículo recién cargado no aparece como "sin movimiento".
        SELECT articulo_id, 'stock_sin_movimiento',
               'Stock ' || TRIM(TO_CHAR(stock_actual, 'FM999999990.##')) ||
               CASE WHEN fecha_ultima_venta IS NULL
                    THEN ', nunca se vendió (alta hace ' || ((now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - fecha_alta) || ' días)'
                    ELSE ', última venta hace ' || ((now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - fecha_ultima_venta) || ' días'
               END
        FROM base
        WHERE activo AND stock_actual > 0
          AND COALESCE(fecha_ultima_venta, fecha_alta) <= (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date - (:dias || ' days')::interval

        UNION ALL
        -- El precio de venta se calcula como costo * (1 + margen). Margen <= 0
        -- = se vende al costo o por debajo. Antes se miraba solo "< 0", pero la
        -- app clampeaba todo a 0% y este chequeo nunca veía esos casos (23/10/2026).
        SELECT articulo_id, 'precio_bajo_costo',
               'Margen cargado: ' || TO_CHAR(margen_ganancia * 100, 'FM9999990.0') || '%'
        FROM base WHERE activo AND margen_ganancia <= 0 AND costo_compra_actual > 0

        UNION ALL
        SELECT articulo_id, 'sin_costo', 'Costo de compra en $0'
        FROM base WHERE activo AND costo_compra_actual <= 0

        UNION ALL
        -- Dado de baja pero con mercadería: hay que liquidarla o ajustar.
        SELECT articulo_id, 'inactivo_con_stock',
               'Stock ' || TRIM(TO_CHAR(stock_actual, 'FM999999990.##')) ||
               ', inactivo desde ' || COALESCE(TO_CHAR(fecha_baja, 'DD/MM/YYYY'), '?')
        FROM base WHERE NOT activo AND stock_actual > 0
    """)
    return pd.read_sql(query, engine, params={"dias": u["dias_sin_venta"]})

def _alertas_venta_atipica(hoy):
    """
    Ventas de los últimos N días muy por encima de lo habitual para ESE
    artículo (media + k desvíos de su historial previo). Típico de un
    error de carga: 50 en vez de 5, o el producto equivocado. Se hace en
    pandas porque necesita media y desvío por artículo. Lee de
    v_ventas_analitica para incluir lo vendido dentro de combos.
    """
    u = UMBRALES_CALIDAD
    ventas = obtener_ventas_analiticas()
    vacio = pd.DataFrame(columns=["articulo_id", "chequeo", "detalle"])
    if ventas.empty:
        return vacio
    ventas["fecha"] = pd.to_datetime(ventas["fecha"])
    ventas["cantidad"] = ventas["cantidad"].astype(float)
    desde = pd.Timestamp(hoy) - pd.Timedelta(days=u["ventana_reciente_dias"])

    previas = ventas[ventas["fecha"] < desde]
    stats = previas.groupby("articulo_id")["cantidad"].agg(["mean", "std", "count"])
    stats = stats[stats["count"] >= u["min_ventas_historial"]]
    recientes = ventas[ventas["fecha"] >= desde].join(stats, on="articulo_id", how="inner")
    if recientes.empty:
        return vacio
    limite = recientes["mean"] + u["desvios_anomalia"] * recientes["std"].fillna(0)
    atip = recientes[recientes["cantidad"] > limite]
    atip = atip.sort_values("cantidad", ascending=False).drop_duplicates("articulo_id")
    return pd.DataFrame({
        "articulo_id": atip["articulo_id"],
        "chequeo": "venta_atipica",
        "detalle": [f"Vendió {c:g} el {f:%d/%m} (lo habitual ≈ {m:.1f})"
                    for c, f, m in zip(atip["cantidad"], atip["fecha"], atip["mean"])],
    })

def calidad_de_datos(hoy):
    """
    Corre los 6 chequeos y devuelve un dict con:
      - alertas: DataFrame (una fila por artículo y chequeo) con articulo,
        categoria, tipo, detalle y accion
      - articulos_activos (los evaluados: activos + inactivos con stock
        distinto de cero), articulos_con_alerta, indicador (% sin alertas)
      - articulos_inactivos: dados de baja que quedaron fuera del indicador
      - estado: 'verde' | 'amarillo' | 'rojo'
    """
    todos = pd.read_sql(
        "SELECT id AS articulo_id, nombre, marca, categoria, activo, stock_actual FROM articulos "
        "WHERE categoria IS DISTINCT FROM 'Promocion'", engine
    )
    evaluados = todos["activo"] | (todos["stock_actual"].astype(float) != 0)
    articulos = todos.loc[evaluados, ["articulo_id", "nombre", "marca", "categoria", "activo"]]
    inactivos_fuera = int((~evaluados).sum())

    venta_atipica = _alertas_venta_atipica(hoy)
    activos_ids = set(articulos.loc[articulos["activo"], "articulo_id"])
    venta_atipica = venta_atipica[venta_atipica["articulo_id"].isin(activos_ids)]
    alertas = pd.concat([_alertas_calidad_sql(), venta_atipica], ignore_index=True)
    alertas = alertas.merge(articulos, on="articulo_id", how="inner")
    alertas["articulo"] = [
        f"{n} ({m})" if pd.notna(m) and m else n
        for n, m in zip(alertas["nombre"], alertas["marca"])
    ]
    alertas["tipo"] = alertas["chequeo"].map(lambda k: CHEQUEOS_CALIDAD[k][0])
    alertas["accion"] = alertas["chequeo"].map(lambda k: CHEQUEOS_CALIDAD[k][1])
    alertas = alertas.sort_values(["tipo", "articulo"]).reset_index(drop=True)

    activos = len(articulos)
    con_alerta = alertas["articulo_id"].nunique()
    indicador = 100.0 if activos == 0 else 100 * (1 - con_alerta / activos)
    if indicador >= SEMAFORO_CALIDAD["verde"]:
        estado = "verde"
    elif indicador >= SEMAFORO_CALIDAD["amarillo"]:
        estado = "amarillo"
    else:
        estado = "rojo"
    return {
        "alertas": alertas[["articulo_id", "articulo", "categoria", "chequeo", "tipo", "detalle", "accion"]],
        "articulos_activos": activos,
        "articulos_inactivos": inactivos_fuera,
        "articulos_con_alerta": con_alerta,
        "indicador": indicador,
        "estado": estado,
    }

_TABLA_SNAPSHOT_OK = False


def _asegurar_tabla_snapshot(conn):
    # Se crea sola la primera vez (también está en schema.sql) -- así no
    # hace falta correr una migración aparte para empezar a juntar historial.
    # Una sola vez por proceso: antes este CREATE/ALTER corría 3 veces en
    # cada apertura del Dashboard (y un ALTER TABLE pide un lock exclusivo).
    global _TABLA_SNAPSHOT_OK
    if _TABLA_SNAPSHOT_OK:
        return
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS snapshot_calidad (
            semana DATE PRIMARY KEY,               -- lunes de la semana
            indicador NUMERIC(5,2) NOT NULL,       -- % de artículos sin alertas
            articulos_activos INTEGER NOT NULL,
            articulos_con_alerta INTEGER NOT NULL,
            actualizado_en TIMESTAMP NOT NULL DEFAULT now()
        )
    """))
    # 14/10/2026: cuántos artículos dados de baja quedaron fuera esa semana.
    conn.execute(text(
        "ALTER TABLE snapshot_calidad ADD COLUMN IF NOT EXISTS articulos_inactivos INTEGER"
    ))
    _TABLA_SNAPSHOT_OK = True

def _version_datos_base():
    """
    Huella del estado de los datos que muestra el Dashboard: un texto que
    cambia cuando cualquier sesión registra, edita o borra una venta, una
    factura, un artículo o la composición de un combo. Se usa como clave de
    caché (app.datos_dashboard): mismo texto = mismos números, así no hace
    falta recalcular todo en cada clic. Una sola consulta liviana.
    Incluye sumas y no solo conteos para detectar también ediciones (cambiar
    la cantidad o el precio de una venta no cambia cuántas hay).
    """
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT concat_ws('|',
              (SELECT concat_ws(':', COUNT(*), MAX(actualizado_en), SUM(stock_actual), SUM(stock_minimo),
                                SUM(costo_compra_actual), SUM(margen_ganancia), SUM(iva),
                                SUM(CASE WHEN activo THEN 1 ELSE 0 END),
                                SUM(COALESCE(fecha_vencimiento - DATE '2000-01-01', 0)),
                                SUM(COALESCE(reposicion_dias, 0)))
                 FROM articulos),
              (SELECT concat_ws(':', COUNT(*), MAX(id), SUM(monto_total), SUM(cantidad), SUM(precio_unitario),
                                SUM(fecha - DATE '2000-01-01'), SUM(articulo_id),
                                SUM(CASE WHEN es_consumo_interno THEN 1 ELSE 0 END),
                                SUM(COALESCE(costo_unitario, 0)), SUM(COALESCE(precio_lista, 0)))
                 FROM ventas),
              (SELECT concat_ws(':', COUNT(*), MAX(id), SUM(cantidad), SUM(costo_unitario), SUM(factura_id))
                 FROM detalle_factura),
              (SELECT concat_ws(':', COUNT(*), MAX(id), SUM(fecha - DATE '2000-01-01'), SUM(proveedor_id))
                 FROM facturas),
              (SELECT concat_ws(':', COUNT(*), SUM(cantidad_por_unidad), SUM(componente_articulo_id))
                 FROM promocion_componentes)
            )
        """)).scalar()


def version_datos():
    """Huella de los datos (ver _version_datos_base) + los controles de góndola (tanda 21):
    un control nuevo cambia la fecha de un lote en el tablero de vencimientos."""
    base = _version_datos_base()
    if hay_desafio():
        with engine.connect() as conn:
            base += "|" + str(conn.execute(text(
                "SELECT concat_ws(':', COUNT(*), MAX(id)) FROM controles_vencimiento")).scalar())
    return base

def guardar_snapshot_calidad(resultado):
    """
    Guarda (o pisa) la foto de la semana actual: queda el último valor
    calculado de cada semana. Como el indicador se calcula cada vez que se
    abre el Dashboard, el historial se arma solo, sin tareas programadas.
    El estado de los datos no se puede reconstruir hacia atrás (el stock
    de hace un mes ya no existe), por eso hay que ir guardándolo.
    """
    with engine.begin() as conn:
        _asegurar_tabla_snapshot(conn)
        conn.execute(text("""
            INSERT INTO snapshot_calidad
                (semana, indicador, articulos_activos, articulos_con_alerta, articulos_inactivos)
            VALUES (date_trunc('week', (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date)::date, :ind, :act, :con, :inact)
            ON CONFLICT (semana) DO UPDATE
            SET indicador = EXCLUDED.indicador,
                articulos_activos = EXCLUDED.articulos_activos,
                articulos_con_alerta = EXCLUDED.articulos_con_alerta,
                articulos_inactivos = EXCLUDED.articulos_inactivos,
                actualizado_en = now()
        """), {"ind": round(resultado["indicador"], 2),
               "act": int(resultado["articulos_activos"]),
               "con": int(resultado["articulos_con_alerta"]),
               "inact": int(resultado.get("articulos_inactivos", 0))})

def historial_calidad():
    """
    Una fila por semana (semana, indicador, artículos evaluados, con
    alerta e inactivos), de la más vieja a la más nueva.
    """
    with engine.begin() as conn:
        _asegurar_tabla_snapshot(conn)
    df = pd.read_sql(
        "SELECT semana, indicador::float AS indicador, articulos_activos, articulos_con_alerta, "
        "articulos_inactivos FROM snapshot_calidad ORDER BY semana", engine
    )
    df["semana"] = pd.to_datetime(df["semana"])
    return df

def indicador_semana_anterior():
    """Valor de la última semana guardada ANTERIOR a la actual, o None."""
    # "hoy" se calcula en hora argentina dentro de la consulta (ver get_engine, tanda 20).
    with engine.begin() as conn:
        _asegurar_tabla_snapshot(conn)
        fila = conn.execute(text("""
            SELECT indicador::float FROM snapshot_calidad
            WHERE semana < date_trunc('week', (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date)::date
            ORDER BY semana DESC LIMIT 1
        """)).first()
    return None if fila is None else float(fila[0])

# ------------------------------------------------------------
# CIERRE DE CAJA (por día)
# ------------------------------------------------------------
def cierre_caja(fecha):
    """
    Resumen de un día puntual para cerrar caja: ingresos reales (excluye
    consumo interno) agrupados por medio de pago, total del día, y los
    gastos (incluido el consumo interno, agrupado aparte) del mismo día.
    """
    ventas_dia = pd.read_sql(text("""
        SELECT medio_pago, COALESCE(SUM(monto_total), 0) AS total, COUNT(*) AS cantidad
        FROM ventas
        WHERE fecha = :fecha AND es_consumo_interno = FALSE
        GROUP BY medio_pago
        ORDER BY total DESC
    """), engine, params={"fecha": fecha})

    total_ingresos = float(ventas_dia["total"].sum()) if not ventas_dia.empty else 0.0
    ventas_efectivo = (float(ventas_dia.loc[ventas_dia["medio_pago"] == MEDIO_EFECTIVO, "total"].sum())
                       if not ventas_dia.empty else 0.0)

    gastos_dia = listar_gastos(desde=fecha, hasta=fecha)
    total_gastos = float(gastos_dia["monto"].sum()) if not gastos_dia.empty else 0.0

    # Consumo interno y sobrante son mercadería (a costo), no plata que salió
    # de la caja: se muestran aparte y no se restan del neto del día.
    if gastos_dia.empty:
        gastos_plata = gastos_dia
        gastos_mercaderia = gastos_dia
    else:
        es_mercaderia = gastos_dia["categoria"].isin(CATEGORIAS_GASTO_MERCADERIA)
        gastos_plata = gastos_dia[~es_mercaderia].reset_index(drop=True)
        gastos_mercaderia = gastos_dia[es_mercaderia].reset_index(drop=True)
    total_gastos_plata = float(gastos_plata["monto"].sum()) if not gastos_plata.empty else 0.0
    total_mercaderia = float(gastos_mercaderia["monto"].sum()) if not gastos_mercaderia.empty else 0.0
    total_consumo_interno = (float(gastos_mercaderia.loc[gastos_mercaderia["categoria"] == "Consumo interno",
                                                         "monto"].sum()) if not gastos_mercaderia.empty else 0.0)

    ventas_detalle = listar_ventas(desde=fecha, hasta=fecha)

    return {
        "por_medio_pago": ventas_dia,
        "total_ingresos": total_ingresos,
        "ventas_efectivo": ventas_efectivo,
        "gastos_detalle": gastos_dia,
        "total_gastos": total_gastos,
        "gastos_plata": gastos_plata,
        "total_gastos_plata": total_gastos_plata,
        "gastos_mercaderia": gastos_mercaderia,
        "total_mercaderia": total_mercaderia,
        "total_consumo_interno": total_consumo_interno,
        "neto_del_dia": total_ingresos - total_gastos_plata,
        "ventas_detalle": ventas_detalle,
    }


# ------------------------------------------------------------
# USUARIOS / PERSONAS (Sprint 2) -- ver migracion_sprint2.py
# Cada persona entra con usuario y contraseña. La contraseña nunca se
# guarda: se guarda un hash PBKDF2-SHA256 con sal aleatoria (biblioteca
# estándar de Python, sin dependencias nuevas). El dueño ve todas las
# pantallas; a "atención" se le habilitan pantallas una por una
# (personas.permisos, con las claves de app.PAGINAS).
# ------------------------------------------------------------
ROLES_PERSONA = {"dueño": "Dueño", "atencion": "Atención"}
_ITERACIONES_CLAVE = 240_000


def validar_clave(clave):
    if not clave or len(clave) < 8:
        raise ValueError("La contraseña tiene que tener al menos 8 caracteres.")


def hash_clave(clave):
    sal = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal, _ITERACIONES_CLAVE)
    return f"pbkdf2_sha256${_ITERACIONES_CLAVE}${sal.hex()}${h.hex()}"


def verificar_clave(clave, guardado):
    try:
        algoritmo, iteraciones, sal, h = guardado.split("$")
        if algoritmo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), bytes.fromhex(sal), int(iteraciones))
        return hmac.compare_digest(calc.hex(), h)
    except (AttributeError, ValueError):
        return False


_HASH_FICTICIO = None

# ------------------------------------------------------------
# BLOQUEO POR INTENTOS FALLIDOS (tanda 12, 10/2026)
# Antes el contador vivía en la sesión del navegador: recargar la página lo
# ponía en cero. Ahora se guarda en la base por usuario (tabla
# intentos_ingreso, ver migracion_tanda12.py). Se cuenta por el nombre de
# usuario TIPEADO, exista o no, para no delatar qué usuarios existen.
# Cada bloqueo seguido dura el doble que el anterior (1, 2, 4... hasta 15
# minutos); un ingreso correcto borra todo.
# ------------------------------------------------------------
MAX_INTENTOS_INGRESO = 5
SEGUNDOS_PRIMER_BLOQUEO = 60
SEGUNDOS_BLOQUEO_MAXIMO = 15 * 60


def _usuario_normalizado(usuario):
    return (usuario or "").strip().lower()[:100]


def segundos_bloqueo(usuario):
    """Segundos que faltan para poder volver a intentar con ese usuario (0 = puede).
    None si la tabla todavía no existe (base sin la migración de la tanda 12)."""
    try:
        with engine.connect() as conn:
            r = conn.execute(text("""
                SELECT GREATEST(0, CEIL(EXTRACT(EPOCH FROM (bloqueado_hasta - now()))))::int
                FROM intentos_ingreso WHERE usuario = :u
            """), {"u": _usuario_normalizado(usuario)}).scalar()
        return int(r or 0)
    except Exception as e:
        if "intentos_ingreso" in str(e):
            return None
        raise


def registrar_intento_fallido(usuario):
    """Suma un intento fallido. Devuelve los segundos de bloqueo si con este se
    bloqueó (0 si no), o None si la tabla no existe."""
    try:
        with engine.begin() as conn:
            fila = conn.execute(text("""
                INSERT INTO intentos_ingreso (usuario, fallidos, bloqueos, ultimo_fallo)
                VALUES (:u, 1, 0, now())
                ON CONFLICT (usuario) DO UPDATE SET
                    -- pasada una hora sin fallar, el contador arranca de nuevo
                    fallidos = CASE WHEN intentos_ingreso.ultimo_fallo < now() - interval '1 hour'
                                    THEN 1 ELSE intentos_ingreso.fallidos + 1 END,
                    bloqueos = CASE WHEN intentos_ingreso.ultimo_fallo < now() - interval '1 day'
                                    THEN 0 ELSE intentos_ingreso.bloqueos END,
                    ultimo_fallo = now()
                RETURNING fallidos, bloqueos
            """), {"u": _usuario_normalizado(usuario)}).mappings().first()
            if fila["fallidos"] < MAX_INTENTOS_INGRESO:
                return 0
            segundos = min(SEGUNDOS_PRIMER_BLOQUEO * 2 ** int(fila["bloqueos"]), SEGUNDOS_BLOQUEO_MAXIMO)
            conn.execute(text("""
                UPDATE intentos_ingreso
                SET fallidos = 0, bloqueos = bloqueos + 1,
                    bloqueado_hasta = now() + make_interval(secs => :s)
                WHERE usuario = :u
            """), {"u": _usuario_normalizado(usuario), "s": segundos})
            registrar_historial("personas", None, "INGRESO_BLOQUEADO",
                                f"Usuario '{_usuario_normalizado(usuario)}' bloqueado {segundos} s "
                                f"por {MAX_INTENTOS_INGRESO} contraseñas incorrectas seguidas", conn=conn)
            return segundos
    except Exception as e:
        if "intentos_ingreso" in str(e):
            return None
        raise


def registrar_ingreso_correcto(usuario):
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM intentos_ingreso WHERE usuario = :u"),
                         {"u": _usuario_normalizado(usuario)})
    except Exception as e:
        if "intentos_ingreso" not in str(e):
            raise


def autenticar(usuario, clave):
    """
    Devuelve el dict de la persona si usuario/contraseña son correctos y
    está activa; si no, None. Ante un usuario inexistente igual calcula un
    hash (ficticio) para que el tiempo de respuesta no delate qué usuarios
    existen.
    """
    global _HASH_FICTICIO
    usuario = (usuario or "").strip().lower()
    with engine.connect() as conn:
        fila = conn.execute(text("""
            SELECT id, nombre, usuario, rol, permisos, activo, clave_hash
            FROM personas WHERE lower(usuario) = :u
        """), {"u": usuario}).mappings().first()
    if not fila or not fila["clave_hash"] or not fila["activo"]:
        if _HASH_FICTICIO is None:
            _HASH_FICTICIO = hash_clave("x" * 12)
        verificar_clave(clave or "", _HASH_FICTICIO)
        return None
    if not verificar_clave(clave or "", fila["clave_hash"]):
        return None
    datos = dict(fila)
    datos.pop("clave_hash")
    datos["permisos"] = list(datos["permisos"] or [])
    return datos


def huella_sesion(persona_id):
    """Huella corta de la contraseña guardada (hash del hash) de una persona
    ACTIVA, o None. La sesión recordada en el navegador la incluye en su firma:
    cambiar la contraseña o desactivar a la persona invalida esas sesiones."""
    with engine.connect() as conn:
        fila = conn.execute(text("SELECT clave_hash, activo FROM personas WHERE id = :id"),
                            {"id": persona_id}).mappings().first()
    if not fila or not fila["activo"] or not fila["clave_hash"]:
        return None
    return hashlib.sha256(fila["clave_hash"].encode("utf-8")).hexdigest()[:16]


def obtener_persona(persona_id):
    """Datos actuales de la persona (sin el hash), o None si no existe."""
    with engine.connect() as conn:
        fila = conn.execute(text("""
            SELECT id, nombre, usuario, rol, permisos, activo FROM personas WHERE id = :id
        """), {"id": persona_id}).mappings().first()
    if not fila:
        return None
    datos = dict(fila)
    datos["permisos"] = list(datos["permisos"] or [])
    return datos


def listar_personas(incluir_inactivas=False):
    query = """SELECT id, nombre, usuario, rol, permisos, activo,
                      (clave_hash IS NOT NULL) AS tiene_clave, creado_en
               FROM personas"""
    if not incluir_inactivas:
        query += " WHERE activo"
    query += " ORDER BY (rol = 'dueño') DESC, nombre"
    return pd.read_sql(text(query), engine)


def _validar_usuario(usuario):
    usuario = (usuario or "").strip().lower()
    if not usuario or not all(ch.isalnum() or ch in "._-" for ch in usuario):
        raise ValueError("El usuario solo puede tener letras, números, punto, guion o guion bajo, sin espacios.")
    return usuario


def crear_persona(nombre, usuario, clave, rol="atencion", permisos=("registrar_venta",)):
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("Escribí el nombre de la persona.")
    usuario = _validar_usuario(usuario)
    if rol not in ROLES_PERSONA:
        raise ValueError("Rol inválido.")
    validar_clave(clave)
    try:
        with engine.begin() as conn:
            pid = conn.execute(text("""
                INSERT INTO personas (nombre, usuario, clave_hash, rol, permisos)
                VALUES (:n, :u, :h, :r, :p) RETURNING id
            """), {"n": nombre, "u": usuario, "h": hash_clave(clave), "r": rol,
                   "p": list(permisos)}).scalar()
            registrar_historial("personas", pid, "PERSONA_CREADA",
                                f"Se creó el usuario '{usuario}' para {nombre} ({ROLES_PERSONA[rol]}); "
                                f"pantallas: {', '.join(permisos) or 'ninguna'}", conn=conn)
    except IntegrityError:
        raise ValueError(f"Ya existe una persona con ese nombre o con el usuario '{usuario}'.")
    return pid


def _quedaria_sin_dueno(conn, persona_id):
    """True si persona_id es el único dueño activo con contraseña."""
    return not conn.execute(text("""
        SELECT 1 FROM personas
        WHERE rol = 'dueño' AND activo AND clave_hash IS NOT NULL AND id <> :id
    """), {"id": persona_id}).first()


def actualizar_persona(persona_id, rol, permisos):
    if rol not in ROLES_PERSONA:
        raise ValueError("Rol inválido.")
    with engine.begin() as conn:
        actual = conn.execute(text("SELECT nombre, rol, permisos FROM personas WHERE id = :id"),
                              {"id": persona_id}).mappings().first()
        if not actual:
            raise ValueError("No existe esa persona.")
        if actual["rol"] == "dueño" and rol != "dueño" and _quedaria_sin_dueno(conn, persona_id):
            raise ValueError("No se puede: tiene que quedar al menos un dueño activo con contraseña.")
        conn.execute(text("UPDATE personas SET rol = :r, permisos = :p WHERE id = :id"),
                     {"r": rol, "p": list(permisos), "id": persona_id})
        registrar_historial("personas", persona_id, "PERMISOS_EDITADOS",
                            f"{actual['nombre']}: rol {ROLES_PERSONA[actual['rol']]} -> {ROLES_PERSONA[rol]}; "
                            f"pantallas {', '.join(actual['permisos'] or []) or 'ninguna'} -> "
                            f"{', '.join(permisos) or 'ninguna'}", conn=conn)


def cambiar_clave(persona_id, clave_nueva):
    validar_clave(clave_nueva)
    with engine.begin() as conn:
        nombre = conn.execute(text(
            "UPDATE personas SET clave_hash = :h WHERE id = :id RETURNING nombre"
        ), {"h": hash_clave(clave_nueva), "id": persona_id}).scalar()
        if nombre:
            registrar_historial("personas", persona_id, "CLAVE_CAMBIADA",
                                f"Se cambió la contraseña de {nombre}", conn=conn)


def cambiar_estado_persona(persona_id, activo):
    with engine.begin() as conn:
        if not activo and _quedaria_sin_dueno(conn, persona_id):
            fila = conn.execute(text("SELECT rol FROM personas WHERE id = :id"), {"id": persona_id}).scalar()
            if fila == "dueño":
                raise ValueError("No se puede desactivar al único dueño activo.")
        nombre = conn.execute(text(
            "UPDATE personas SET activo = :a WHERE id = :id RETURNING nombre"
        ), {"a": bool(activo), "id": persona_id}).scalar()
        if nombre:
            registrar_historial("personas", persona_id,
                                "PERSONA_REACTIVADA" if activo else "PERSONA_DESACTIVADA",
                                f"{nombre} {'puede volver a entrar' if activo else 'ya no puede entrar'} a la app",
                                conn=conn)


def ventas_por_persona(desde=None, hasta=None):
    """Tickets e ítems registrados por persona y día (insumo de la métrica
    de calidad de datos por persona). 'Sin dato' = ventas anteriores a
    Sprint 2 o registradas sin elegir quién atendía."""
    query = """
        SELECT v.fecha, COALESCE(p.nombre, 'Sin dato') AS persona,
               COUNT(DISTINCT v.ticket_id) AS tickets, COUNT(*) AS items,
               SUM(v.monto_total) AS monto
        FROM ventas v LEFT JOIN personas p ON p.id = v.registrado_por
        WHERE 1=1
    """
    params = {}
    if desde:
        query += " AND v.fecha >= :desde"
        params["desde"] = desde
    if hasta:
        query += " AND v.fecha <= :hasta"
        params["hasta"] = hasta
    query += " GROUP BY 1, 2 ORDER BY 1 DESC, 2"
    return pd.read_sql(text(query), engine, params=params)


# ------------------------------------------------------------
# PARÁMETROS (Sprint 2): frecuencia de viaje, capacidad, nivel de servicio
# ------------------------------------------------------------
PARAMETROS_DEFAULT = {
    "frecuencia_viaje_dias": 7,
    "capacidad_litros": 2000,
    "nivel_servicio_A": 0.95,
    "nivel_servicio_B": 0.90,
    "nivel_servicio_C": 0.80,
}


def obtener_parametros():
    """Diccionario clave -> valor (float). Si falta alguno en la base, usa el default."""
    valores = dict(PARAMETROS_DEFAULT)
    with engine.connect() as conn:
        for clave, valor in conn.execute(text("SELECT clave, valor FROM parametros")):
            valores[clave] = float(valor)
    return valores


def obtener_parametro(clave):
    return obtener_parametros().get(clave)


def actualizar_parametro(clave, valor):
    if clave not in PARAMETROS_DEFAULT:
        raise ValueError(f"Parámetro desconocido: {clave}")
    valor = float(valor)
    if clave == "frecuencia_viaje_dias" and not (1 <= valor <= 60):
        raise ValueError("La frecuencia de viaje tiene que estar entre 1 y 60 días.")
    if clave == "capacidad_litros" and valor <= 0:
        raise ValueError("La capacidad tiene que ser mayor que cero.")
    if clave.startswith("nivel_servicio_") and not (0.5 <= valor < 1):
        raise ValueError("El nivel de servicio tiene que estar entre 50 % y 99,9 %.")
    with engine.begin() as conn:
        anterior = conn.execute(text("SELECT valor FROM parametros WHERE clave = :c"), {"c": clave}).scalar()
        if anterior is not None and float(anterior) == valor:
            return False
        conn.execute(text("""
            INSERT INTO parametros (clave, valor) VALUES (:c, :v)
            ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()
        """), {"c": clave, "v": valor})
        registrar_historial("parametros", None, "PARAMETRO_EDITADO",
                            f"{clave}: {float(anterior) if anterior is not None else '-'} -> {valor:g}",
                            conn=conn)
    return True


# ------------------------------------------------------------
# PRONÓSTICOS GUARDADOS (Sprint 2) -- para medir el error ex post
# ------------------------------------------------------------
def existe_snapshot_pronostico(fecha_calculo):
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM pronostico_snapshot WHERE fecha_calculo = :f LIMIT 1"),
                            {"f": fecha_calculo}).first() is not None


def guardar_pronostico_snapshot(df, fecha_calculo):
    """
    df: columnas articulo_id, fecha_objetivo, demanda, modelo.
    Pisa lo que hubiera para esa fecha de cálculo (recalcular el mismo día
    no duplica filas). Devuelve la cantidad de filas guardadas.
    """
    if df is None or df.empty:
        return 0
    filas = [
        {"fc": fecha_calculo, "aid": int(r.articulo_id), "fo": r.fecha_objetivo,
         "d": max(0.0, float(r.demanda)), "m": str(r.modelo)}
        for r in df.itertuples(index=False)
    ]
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM pronostico_snapshot WHERE fecha_calculo = :fc"), {"fc": fecha_calculo})
        conn.execute(text("""
            INSERT INTO pronostico_snapshot (fecha_calculo, articulo_id, fecha_objetivo, demanda, modelo)
            VALUES (:fc, :aid, :fo, :d, :m)
        """), filas)
    return len(filas)


# ------------------------------------------------------------
# REPOSICIÓN POR ARTÍCULO (Sprint 2, HU-015) -- ver migracion_lista_compras.py
# ------------------------------------------------------------
def actualizar_reposicion_articulo(articulo_id, reposicion_dias, vida_util_dias, nivel_servicio):
    """Compra diaria (reposicion_dias=1) o viaje (None), vida útil y nivel de
    servicio propio. Devuelve 1 si cambió algo (y lo deja en el historial), 0 si no."""
    if nivel_servicio is not None and not (0.5 <= nivel_servicio < 1):
        raise ValueError("El nivel de servicio tiene que estar entre 50 % y 99 %.")
    with engine.begin() as conn:
        actual = conn.execute(text("""
            SELECT nombre, reposicion_dias, vida_util_dias, nivel_servicio FROM articulos WHERE id = :id
        """), {"id": articulo_id}).mappings().first()
        if not actual:
            return 0
        nuevo = (reposicion_dias, vida_util_dias, None if nivel_servicio is None else round(nivel_servicio, 3))
        viejo = (actual["reposicion_dias"], actual["vida_util_dias"],
                 None if actual["nivel_servicio"] is None else round(float(actual["nivel_servicio"]), 3))
        if nuevo == viejo:
            return 0
        conn.execute(text("""
            UPDATE articulos SET reposicion_dias = :r, vida_util_dias = :v, nivel_servicio = :n,
                   actualizado_en = now()
            WHERE id = :id
        """), {"r": reposicion_dias, "v": vida_util_dias, "n": nivel_servicio, "id": articulo_id})
        def txt(t):
            r, v, n = t
            return (f"{'compra diaria' if r == 1 else 'viaje'}, vida útil {v or '-'} d, "
                    f"nivel {'auto' if n is None else f'{n:.0%}'}")
        registrar_historial("articulos", articulo_id, "REPOSICION_EDITADA",
                            f"{actual['nombre']}: {txt(viejo)} -> {txt(nuevo)}", conn=conn)
    return 1


# ------------------------------------------------------------
# SEGUIMIENTO DEL PRONÓSTICO (validación)
# ------------------------------------------------------------
def pronosticos_guardados(desde):
    """Para cada artículo y día objetivo, el último pronóstico guardado ANTES o
    el mismo día (ex ante): fecha_objetivo, articulo_id, demanda, fecha_calculo."""
    return pd.read_sql(text("""
        SELECT DISTINCT ON (articulo_id, fecha_objetivo)
               fecha_objetivo, articulo_id, demanda::float AS demanda, fecha_calculo
        FROM pronostico_snapshot
        WHERE fecha_objetivo >= :desde AND fecha_calculo <= fecha_objetivo
        ORDER BY articulo_id, fecha_objetivo, fecha_calculo DESC
    """), engine, params={"desde": desde})


def movimientos_diarios(articulo_id, desde):
    """Por día: comprado (facturas), vendido (sin consumo interno) y consumo
    interno (para compra diaria = sobrante regalado/tirado)."""
    return pd.read_sql(text("""
        WITH c AS (
            SELECT f.fecha, SUM(d.cantidad)::float AS comprado
            FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
            WHERE d.articulo_id = :a AND f.fecha >= :desde GROUP BY f.fecha
        ), v AS (
            SELECT fecha,
                   SUM(cantidad) FILTER (WHERE NOT es_consumo_interno)::float AS vendido,
                   SUM(cantidad) FILTER (WHERE es_consumo_interno)::float AS consumo_interno
            FROM ventas WHERE articulo_id = :a AND fecha >= :desde GROUP BY fecha
        )
        SELECT COALESCE(c.fecha, v.fecha) AS fecha, c.comprado, v.vendido, v.consumo_interno
        FROM c FULL JOIN v ON v.fecha = c.fecha
        ORDER BY 1
    """), engine, params={"a": int(articulo_id), "desde": desde})


def stock_actual_por_id():
    """Stock de cada artículo leído en el momento (id -> stock). Para las
    pantallas que muestran datos del pronóstico, que se guardan en memoria
    hasta una hora: el stock no puede venir de ahí (tanda 16)."""
    with engine.connect() as conn:
        filas = conn.execute(text("SELECT id, stock_actual FROM articulos")).all()
    return {int(i): float(s) for i, s in filas}


def stock_diario_articulo(articulo_id, desde):
    """
    Día por día desde `desde` hasta hoy, para el gráfico de compra diaria
    (tanda 16): con cuánto arrancó el día (lo que quedó del anterior), lo
    comprado, lo vendido, lo tirado/regalado (consumo interno: para compra
    diaria es el sobrante), los ajustes por conteo y con cuánto terminó.

    'fraccionado' va aparte del ajuste: negativo en el artículo que se
    fracciona (ej.: el pan que pasa a "pan de ayer" en Fraccionar), positivo
    en el producto que recibe.

    El stock se reconstruye HACIA ATRÁS desde el stock actual, igual que
    pronostico.dias_observados: fin(d) = stock_hoy - movimientos posteriores
    a d. Movimientos: compras, ventas (las de combos mueven a sus
    componentes), fraccionamientos y correcciones manuales del Historial
    ("Stock corregido a mano en 'X': a -> b"). Así lo que sobró ayer y no se
    tiró aparece como arrastre de hoy, en vez de pintarse como sobrante.
    """
    a = int(articulo_id)
    with engine.connect() as conn:
        hay_desglose = conn.execute(text("SELECT to_regclass('venta_componentes') IS NOT NULL")).scalar()
    ventas_comp = ("LEFT JOIN venta_componentes vc ON vc.venta_id = v.id AND vc.mueve_stock"
                   if hay_desglose else
                   "LEFT JOIN (SELECT NULL::int AS venta_id, NULL::int AS componente_articulo_id, "
                   "NULL::numeric AS cantidad_por_unidad) vc ON FALSE")
    query = text(rf"""
        WITH ven AS (
            SELECT v.fecha,
                   SUM(v.cantidad * COALESCE(vc.cantidad_por_unidad, 1))
                       FILTER (WHERE NOT v.es_consumo_interno) AS vendido,
                   SUM(v.cantidad * COALESCE(vc.cantidad_por_unidad, 1))
                       FILTER (WHERE v.es_consumo_interno) AS tirado
            FROM ventas v {ventas_comp}
            WHERE COALESCE(vc.componente_articulo_id, v.articulo_id) = :a
            GROUP BY v.fecha
        ), com AS (
            SELECT f.fecha, SUM(d.cantidad) AS comprado
            FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
            WHERE d.articulo_id = :a GROUP BY f.fecha
        ), fra AS (
            SELECT fecha, SUM(CASE WHEN articulo_derivado_id = :a THEN cantidad_generada ELSE 0 END)
                        - SUM(CASE WHEN articulo_bolsa_id = :a THEN cantidad_bolsas ELSE 0 END) AS fraccionado
            FROM fraccionamientos WHERE :a IN (articulo_bolsa_id, articulo_derivado_id) GROUP BY fecha
        ), aju AS (
            SELECT (fecha AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS fecha,
                   SUM((regexp_match(detalle, ':\s*(-?[0-9.]+)\s*->\s*(-?[0-9.]+)\s*$'))[2]::numeric
                       - (regexp_match(detalle, ':\s*(-?[0-9.]+)\s*->\s*(-?[0-9.]+)\s*$'))[1]::numeric) AS ajuste
            FROM historial_cambios
            WHERE tabla = 'articulos' AND registro_id = :a
              AND detalle LIKE 'Stock corregido a mano en %'
              AND detalle ~ ':\s*-?[0-9.]+\s*->\s*-?[0-9.]+\s*$'
            GROUP BY 1
        ), dias AS (
            SELECT fecha FROM ven UNION SELECT fecha FROM com
            UNION SELECT fecha FROM fra UNION SELECT fecha FROM aju
        )
        SELECT d.fecha,
               COALESCE(com.comprado, 0)::float AS comprado,
               COALESCE(ven.vendido, 0)::float AS vendido,
               COALESCE(ven.tirado, 0)::float AS tirado,
               COALESCE(fra.fraccionado, 0)::float AS fraccionado,
               COALESCE(aju.ajuste, 0)::float AS ajuste
        FROM dias d
        LEFT JOIN ven ON ven.fecha = d.fecha LEFT JOIN com ON com.fecha = d.fecha
        LEFT JOIN fra ON fra.fecha = d.fecha LEFT JOIN aju ON aju.fecha = d.fecha
        ORDER BY d.fecha
    """)
    mov = pd.read_sql(query, engine, params={"a": a})
    with engine.connect() as conn:
        stock_hoy, hoy = conn.execute(text("SELECT stock_actual, (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date FROM articulos WHERE id = :a"),
                                      {"a": a}).one()
    cols = ["fecha", "inicio", "comprado", "vendido", "tirado", "fraccionado", "ajuste", "fin"]
    if mov.empty:
        return pd.DataFrame(columns=cols)
    mov["fecha"] = pd.to_datetime(mov["fecha"])
    mov["neto"] = mov["comprado"] - mov["vendido"] - mov["tirado"] + mov["fraccionado"] + mov["ajuste"]
    # fin(d) = stock_hoy - Σ netos de los días posteriores a d
    posteriores = mov["neto"][::-1].cumsum()[::-1].shift(-1).fillna(0.0)
    mov["fin"] = float(stock_hoy) - posteriores
    mov["inicio"] = mov["fin"] - mov["neto"]
    # completar los días sin movimientos (el stock pasa igual de un día al otro)
    rango = pd.date_range(max(pd.Timestamp(desde), mov["fecha"].min()), pd.Timestamp(hoy), freq="D")
    if len(rango) == 0:
        return pd.DataFrame(columns=cols)
    base = mov.set_index("fecha")
    previo = base[base.index < rango[0]]
    fin_previo = float(previo["fin"].iloc[-1]) if not previo.empty else float(base["inicio"].iloc[0])
    out = base.reindex(rango)
    for c in ("comprado", "vendido", "tirado", "fraccionado", "ajuste"):
        out[c] = out[c].fillna(0.0)
    out["fin"] = out["fin"].ffill().fillna(fin_previo)
    out["inicio"] = out["fin"] - (out["comprado"] - out["vendido"] - out["tirado"]
                                  + out["fraccionado"] + out["ajuste"])
    return out.reset_index(names="fecha")[cols]


# ------------------------------------------------------------
# REGISTRO RÁPIDO DEL DÍA PARA COMPRA DIARIA (pan, perecederos)
# ------------------------------------------------------------
CATEGORIA_SOBRANTE = "Sobrante"
MEDIO_SOBRANTE = "Sobrante"
# Tanda 18: lo que se tira porque VENCIÓ, separado del sobrante (pan que sobró y se
# regaló). Baja stock como consumo interno, pero NO es demanda (el pronóstico lo
# saca) ni movimiento (el stock inmovilizado no lo cuenta como venta).
CATEGORIA_VENCIDO = "Vencido"
MEDIO_VENCIDO = "Vencido"
MEDIO_EFECTIVO = "Efectivo"
# Gastos que son mercadería del local (se registran a costo), no plata pagada:
# el cierre de caja los muestra aparte y no los resta del neto.
# Tanda 21: lo que se regala a los clientes en el juego de las monedas (Desafío Recreo).
CATEGORIA_REGALO = "Regalo a clientes"
MEDIO_REGALO = "Regalo"
CATEGORIAS_GASTO_MERCADERIA = ("Consumo interno", CATEGORIA_SOBRANTE, CATEGORIA_VENCIDO, CATEGORIA_REGALO)


def ids_compra_diaria():
    """IDs de los artículos marcados como compra diaria (vacío si la base no
    tiene todavía la columna: migracion_lista_compras.py)."""
    with engine.connect() as conn:
        return set(conn.execute(text(
            "SELECT id FROM articulos WHERE (to_jsonb(articulos) ->> 'reposicion_dias')::int = 1"
        )).scalars())


def movimientos_del_dia(articulo_id, fecha):
    """Lo ya cargado ese día: comprado total (todas las facturas), comprado por
    el registro rápido y sobrante registrado por el registro rápido."""
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT
              COALESCE((SELECT SUM(d.cantidad) FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
                        WHERE d.articulo_id = :a AND f.fecha = :f), 0)::float AS comprado_total,
              COALESCE((SELECT SUM(d.cantidad) FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
                        WHERE d.articulo_id = :a AND f.ref_externa = :ref), 0)::float AS comprado_rapido,
              COALESCE((SELECT SUM(v.cantidad) FROM ventas v
                        WHERE v.articulo_id = :a AND v.fecha = :f AND v.es_consumo_interno
                          AND v.medio_pago = 'Sobrante'), 0)::float AS sobrante
        """), {"a": int(articulo_id), "f": fecha, "ref": f"diaria:{int(articulo_id)}:{fecha}"}).mappings().first()
    return dict(r)


def registrar_dia_compra_diaria(articulo_id, fecha, comprado_total, sobrante):
    """
    Deja el día de un artículo de compra diaria con:
      - comprado_total: lo comprado ese día. Si ya hay facturas cargadas a
        mano ese día, el registro rápido solo carga la diferencia (no duplica).
        Se carga como compra al último proveedor del artículo, al costo
        vigente, SIN tocar el costo de referencia ni el precio de venta.
      - sobrante: lo que sobró y se regaló o se tiró. Se registra como
        consumo interno (medio 'Sobrante': baja stock, no es ingreso; para la
        compra diaria el pronóstico lo cuenta como sobrante, no como venta) y
        su costo como gasto de categoría 'Sobrante'.
    Corregir y volver a guardar REEMPLAZA lo cargado por este registro ese día.
    Devuelve una lista de textos con lo que se hizo.
    """
    articulo_id = int(articulo_id)
    ref = f"diaria:{articulo_id}:{fecha}"
    marca = f"[art:{articulo_id}]"
    hechos = []
    persona = persona_actual()
    with engine.begin() as conn:
        art = conn.execute(text("SELECT nombre, costo_compra_actual FROM articulos WHERE id = :a"),
                           {"a": articulo_id}).mappings().first()
        if not art:
            raise ValueError("No existe el artículo.")
        costo = float(art["costo_compra_actual"] or 0)

        # ---- compra ----
        otras = conn.execute(text("""
            SELECT COALESCE(SUM(d.cantidad), 0) FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
            WHERE d.articulo_id = :a AND f.fecha = :f AND f.ref_externa IS DISTINCT FROM :ref
        """), {"a": articulo_id, "f": fecha, "ref": ref}).scalar()
        otras = float(otras or 0)
        if comprado_total is not None:
            if comprado_total < otras:
                raise ValueError(f"{art['nombre']}: ese día ya hay {otras:g} cargado en facturas; "
                                 "para bajar el número corregí esa factura en Cargar Factura.")
            objetivo = comprado_total - otras
            previo = conn.execute(text("SELECT id FROM facturas WHERE ref_externa = :ref"), {"ref": ref}).scalar()
            cant_previa = float(conn.execute(text(
                "SELECT COALESCE(SUM(cantidad),0) FROM detalle_factura WHERE factura_id = :id"), {"id": previo}
            ).scalar() or 0) if previo else 0.0
            if abs(cant_previa - objetivo) > 1e-9:
                if previo:
                    conn.execute(text("DELETE FROM facturas WHERE id = :id"), {"id": previo})  # el trigger resta el stock
                if objetivo > 0:
                    prov = conn.execute(text("""
                        SELECT f.proveedor_id FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
                        WHERE d.articulo_id = :a ORDER BY f.fecha DESC, f.id DESC LIMIT 1
                    """), {"a": articulo_id}).scalar()
                    if prov is None:
                        prov = conn.execute(text("SELECT id FROM proveedores WHERE nombre = 'Compra diaria'")).scalar()
                        if prov is None:
                            prov = conn.execute(text(
                                "INSERT INTO proveedores (nombre) VALUES ('Compra diaria') RETURNING id")).scalar()
                    fid = conn.execute(text("""
                        INSERT INTO facturas (proveedor_id, numero, fecha, origen, ref_externa)
                        VALUES (:p, :n, :f, 'manual', :ref) RETURNING id
                    """), {"p": prov, "n": f"Compra diaria {fecha}", "f": fecha, "ref": ref}).scalar()
                    conn.execute(text("""
                        INSERT INTO detalle_factura (factura_id, articulo_id, cantidad, costo_unitario, actualiza_precio)
                        VALUES (:fid, :a, :c, :costo, FALSE)
                    """), {"fid": fid, "a": articulo_id, "c": objetivo, "costo": costo})
                hechos.append(f"Compra de {art['nombre']} del {fecha}: {comprado_total:g}"
                              + (f" ({otras:g} ya estaban en facturas)" if otras else ""))

        # ---- sobrante ----
        if sobrante is not None:
            if _reemplazar_merma(conn, articulo_id, fecha, sobrante, costo, art["nombre"], MEDIO_SOBRANTE,
                                 CATEGORIA_SOBRANTE, "Sobrante regalado/tirado", persona):
                hechos.append(f"Sobrante de {art['nombre']} del {fecha}: {sobrante:g}")
        if hechos:
            registrar_historial("articulos", articulo_id, "REGISTRO_DIA_COMPRA_DIARIA", " | ".join(hechos), conn=conn)
    return hechos


def _reemplazar_merma(conn, articulo_id, fecha, cantidad, costo, nombre, medio, categoria_gasto, texto, persona):
    """Deja lo registrado ese día para el artículo con `medio` (Sobrante o Vencido)
    en `cantidad`: borra lo anterior (el trigger repone el stock) y lo vuelve a
    cargar como consumo interno, con su costo como gasto de `categoria_gasto`.
    Devuelve True si cambió algo."""
    marca = f"[art:{int(articulo_id)}]"
    previo = float(conn.execute(text("""
        SELECT COALESCE(SUM(cantidad), 0) FROM ventas
        WHERE articulo_id = :a AND fecha = :f AND es_consumo_interno AND medio_pago = :medio
    """), {"a": articulo_id, "f": fecha, "medio": medio}).scalar() or 0)
    if abs(previo - cantidad) <= 1e-9:
        return False
    conn.execute(text("""
        DELETE FROM ventas WHERE articulo_id = :a AND fecha = :f AND es_consumo_interno
          AND medio_pago = :medio
    """), {"a": articulo_id, "f": fecha, "medio": medio})  # el trigger repone el stock
    conn.execute(text("""
        DELETE FROM gastos WHERE fecha = :f AND categoria = :cat AND descripcion LIKE :m
    """), {"f": fecha, "cat": categoria_gasto, "m": f"%{marca}"})
    if cantidad > 0:
        tid = conn.execute(text(
            "INSERT INTO ventas_ticket (fecha, medio_pago) VALUES (:f, :medio) RETURNING id"
        ), {"f": fecha, "medio": medio}).scalar()
        conn.execute(text("""
            INSERT INTO ventas (articulo_id, cantidad, precio_unitario, monto_total, medio_pago, fecha,
                                ticket_id, es_consumo_interno, registrado_por)
            VALUES (:a, :c, 0, 0, :medio, :f, :t, TRUE, :quien)
        """), {"a": articulo_id, "c": cantidad, "f": fecha, "t": tid, "medio": medio,
               "quien": persona["id"] if persona else None})
        if costo * cantidad > 0:
            conn.execute(text("""
                INSERT INTO gastos (fecha, categoria, descripcion, monto)
                VALUES (:f, :cat, :d, :m)
            """), {"f": fecha, "cat": categoria_gasto, "m": round(costo * cantidad, 2),
                   "d": f"{texto}: {cantidad:g} x {nombre} {marca}"})
    return True


def vencido_del_dia(articulo_id, fecha):
    """Lo ya cargado como vencido ese día para el artículo."""
    with engine.connect() as conn:
        return float(conn.execute(text("""
            SELECT COALESCE(SUM(cantidad), 0) FROM ventas
            WHERE articulo_id = :a AND fecha = :f AND es_consumo_interno AND medio_pago = :m
        """), {"a": int(articulo_id), "f": fecha, "m": MEDIO_VENCIDO}).scalar() or 0)


def registrar_vencido(articulo_id, fecha, cantidad):
    """
    Tanda 18: lo que se tiró porque venció. Igual que el sobrante (consumo
    interno que baja stock y gasto a costo, categoría 'Vencido'), pero con su
    propio motivo: no cuenta como demanda para el pronóstico ni como
    movimiento para el stock inmovilizado, y se mide aparte como merma.
    Volver a guardar el mismo día REEMPLAZA lo cargado. Devuelve lista de textos.
    """
    articulo_id = int(articulo_id)
    persona = persona_actual()
    with engine.begin() as conn:
        art = conn.execute(text("SELECT nombre, costo_compra_actual FROM articulos WHERE id = :a"),
                           {"a": articulo_id}).mappings().first()
        if not art:
            raise ValueError("No existe el artículo.")
        costo = float(art["costo_compra_actual"] or 0)
        if not _reemplazar_merma(conn, articulo_id, fecha, float(cantidad), costo, art["nombre"],
                                 MEDIO_VENCIDO, CATEGORIA_VENCIDO, "Vencido (tirado)", persona):
            return []
        hecho = f"Vencido de {art['nombre']} del {fecha}: {float(cantidad):g}"
        registrar_historial("articulos", articulo_id, "REGISTRO_VENCIDO", hecho, conn=conn)
    return [hecho]


def merma_vencido(desde, hasta):
    """Merma por vencimiento en el período: unidades y costo, por artículo."""
    return pd.read_sql(text("""
        SELECT a.id AS articulo_id, a.nombre, a.categoria,
               SUM(v.cantidad)::float AS unidades,
               COALESCE((SELECT SUM(g.monto) FROM gastos g
                          WHERE g.categoria = :cat AND g.fecha BETWEEN :d AND :h
                            AND g.descripcion LIKE '%[art:' || a.id || ']'), 0)::float AS costo
        FROM ventas v JOIN articulos a ON a.id = v.articulo_id
        WHERE v.medio_pago = :m AND v.es_consumo_interno AND v.fecha BETWEEN :d AND :h
        GROUP BY a.id, a.nombre, a.categoria
        ORDER BY costo DESC
    """), engine, params={"cat": CATEGORIA_VENCIDO, "m": MEDIO_VENCIDO, "d": desde, "h": hasta})


# ------------------------------------------------------------
# PROVEEDORES EN CONSIGNACIÓN (tanda 14, 10/2026)
# Caso real: FUSION FUNGA deja mercadería sin papel; pasa cada tanto, cuenta
# y cobra lo vendido. La reposición se carga como una factura (sube stock y
# costo, el margen queda bien porque el costo se toma al vender); lo que se
# le debe es el costo de lo vendido desde la última liquidación. El pago NO
# es un gasto: ese costo ya está en la ganancia (sería contarlo dos veces).
# ------------------------------------------------------------
def hay_consignacion():
    """True si la base tiene la migración de la tanda 14."""
    with engine.connect() as conn:
        return bool(conn.execute(text(
            "SELECT to_regclass('public.liquidaciones_consignacion') IS NOT NULL")).scalar())


def proveedores_consignacion():
    if not hay_consignacion():
        return pd.DataFrame(columns=["id", "nombre"])
    return pd.read_sql("SELECT id, trim(nombre) AS nombre FROM proveedores WHERE consignacion ORDER BY nombre", engine)


def marcar_consignacion(proveedor_id, consignacion):
    with engine.begin() as conn:
        nombre = conn.execute(text("UPDATE proveedores SET consignacion = :c WHERE id = :i RETURNING trim(nombre)"),
                              {"c": bool(consignacion), "i": int(proveedor_id)}).scalar()
        registrar_historial("proveedores", int(proveedor_id), "EDICION",
                            f"{nombre}: {'pasa a' if consignacion else 'deja de ser'} proveedor en consignación",
                            conn=conn)


def ultima_liquidacion(proveedor_id):
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT * FROM liquidaciones_consignacion WHERE proveedor_id = :p
            ORDER BY hasta_venta_id DESC, id DESC LIMIT 1
        """), {"p": int(proveedor_id)}).mappings().first()
    return dict(r) if r else None


def consignacion_pendiente(proveedor_id, desde_fecha=None):
    """
    Lo vendido de ese proveedor que todavía no se le pagó: ventas (incluye las de
    combos y el consumo interno, que también se le debe) de sus artículos
    posteriores a la última liquidación. Sin liquidaciones, desde `desde_fecha`.
    Devuelve (detalle por artículo, total a pagar, primera venta, última venta).
    """
    ult = ultima_liquidacion(proveedor_id)
    filtro = "va.venta_id > :desde_id" if ult else "va.fecha >= :desde_fecha"
    params = {"p": int(proveedor_id), "desde_id": ult["hasta_venta_id"] if ult else 0,
              "desde_fecha": desde_fecha or dt.date(2000, 1, 1)}
    df = pd.read_sql(text(f"""
        SELECT va.articulo_id, MIN(va.articulo) AS articulo,
               SUM(va.cantidad)::float AS cantidad, SUM(va.costo_atribuido)::float AS a_pagar,
               MIN(va.venta_id) AS primera_venta, MAX(va.venta_id) AS ultima_venta,
               MIN(va.fecha) AS desde, MAX(va.fecha) AS hasta
        FROM v_ventas_analitica va
        WHERE va.articulo_id IN (SELECT articulo_id FROM articulo_proveedor_precio WHERE proveedor_id = :p)
          AND {filtro}
        GROUP BY va.articulo_id ORDER BY MIN(va.articulo)
    """), engine, params=params)
    if df.empty:
        return df, 0.0, None, None
    return df, float(df["a_pagar"].sum()), int(df["primera_venta"].min()), int(df["ultima_venta"].max())


def registrar_liquidacion(proveedor_id, desde_venta_id, hasta_venta_id, monto_calculado, monto_pagado,
                          fecha=None, nota=None):
    """Deja constancia de que se le pagó lo vendido hasta la venta `hasta_venta_id`."""
    with engine.begin() as conn:
        lid = conn.execute(text("""
            INSERT INTO liquidaciones_consignacion
                (proveedor_id, fecha, desde_venta_id, hasta_venta_id, monto_calculado, monto_pagado, nota)
            VALUES (:p, COALESCE(:f, (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date), :d, :h, :mc, :mp, :n) RETURNING id
        """), {"p": int(proveedor_id), "f": fecha, "d": desde_venta_id, "h": int(hasta_venta_id),
               "mc": round(float(monto_calculado), 2), "mp": round(float(monto_pagado), 2), "n": nota}).scalar()
        nombre = conn.execute(text("SELECT trim(nombre) FROM proveedores WHERE id = :p"),
                              {"p": int(proveedor_id)}).scalar()
        dif = float(monto_pagado) - float(monto_calculado)
        registrar_historial(
            "proveedores", int(proveedor_id), "LIQUIDACION",
            f"{nombre}: liquidación #{lid} pagada ${float(monto_pagado):,.2f} (calculado ${float(monto_calculado):,.2f}"
            + (f", diferencia ${dif:,.2f}" if abs(dif) >= 0.01 else "") + f"); cubre hasta la venta #{hasta_venta_id}"
            + (f" -- {nota}" if nota else ""), conn=conn)
    return lid


def registrar_corte_inicial(proveedor_id):
    """Proveedor al día: corte de $0 hasta la última venta registrada. Desde ahí se cuenta lo vendido."""
    with engine.connect() as conn:
        hasta = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM ventas")).scalar()
    return registrar_liquidacion(proveedor_id, None, hasta, 0, 0, nota="Corte inicial: al día (sin deuda)")


def listar_liquidaciones(proveedor_id=None):
    if not hay_consignacion():
        return pd.DataFrame()
    q = """SELECT l.*, trim(p.nombre) AS proveedor FROM liquidaciones_consignacion l
           JOIN proveedores p ON p.id = l.proveedor_id"""
    params = {}
    if proveedor_id is not None:
        q += " WHERE l.proveedor_id = :p"
        params["p"] = int(proveedor_id)
    return pd.read_sql(text(q + " ORDER BY l.fecha DESC, l.id DESC"), engine, params=params)


def stock_para_contar(proveedor_id):
    """Sus artículos con el stock de la app, para comparar con lo que cuenta el proveedor."""
    return pd.read_sql(text("""
        SELECT a.id AS articulo_id, a.nombre, a.unidad_medida, a.stock_actual::float AS stock_app,
               a.costo_compra_actual::float AS costo
        FROM articulos a JOIN articulo_proveedor_precio app ON app.articulo_id = a.id
        WHERE app.proveedor_id = :p AND a.activo ORDER BY a.nombre
    """), engine, params={"p": int(proveedor_id)})


# ============================================================
# TANDA 17: lista guardada y registro de viajes (protocolo de validación)
# ============================================================

def hay_registro_viajes():
    """True si la base tiene la migración de la tanda 17."""
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('public.viajes_compra') IS NOT NULL")).scalar())


def guardar_lista_compra(fecha, filas, nota=None):
    """
    Guarda la lista con la que se va al mayorista. filas: DataFrame con
    articulo_id, clase, proveedor, stock, venta_esperada, sugerido, cantidad,
    incluido, costo_c_iva (todos los renglones que sugirió la app, también
    los que se sacaron). Una lista por día: si ya hay una ese día, se
    reemplaza su contenido (conserva el id, así los viajes siguen apuntando).
    Devuelve (lista_id, reemplazo).
    """
    persona = persona_actual()
    f = filas.copy()
    f["llevado"] = f["incluido"].astype(bool) & (f["cantidad"].astype(float) > 0)
    renglones = int(f["llevado"].sum())
    costo = float((f.loc[f["llevado"], "cantidad"].astype(float)
                   * f.loc[f["llevado"], "costo_c_iva"].astype(float).fillna(0)).sum())
    with engine.begin() as conn:
        previa = conn.execute(text("SELECT id FROM listas_compra WHERE fecha = :f"), {"f": fecha}).scalar()
        if previa:
            lid = previa
            conn.execute(text("DELETE FROM listas_compra_detalle WHERE lista_id = :l"), {"l": lid})
            conn.execute(text("""UPDATE listas_compra SET persona_id = :p, renglones = :r, costo_estimado = :c,
                                 nota = :n, actualizado_en = now() WHERE id = :l"""),
                         {"p": persona["id"] if persona else None, "r": renglones, "c": round(costo, 2),
                          "n": nota, "l": lid})
        else:
            lid = conn.execute(text("""INSERT INTO listas_compra (fecha, persona_id, renglones, costo_estimado, nota)
                                       VALUES (:f, :p, :r, :c, :n) RETURNING id"""),
                               {"f": fecha, "p": persona["id"] if persona else None, "r": renglones,
                                "c": round(costo, 2), "n": nota}).scalar()
        filas_sql = [{
            "l": lid, "a": int(r.articulo_id), "cl": r.clase, "pr": r.proveedor,
            "st": None if pd.isna(r.stock) else float(r.stock),
            "ve": None if pd.isna(r.venta_esperada) else float(r.venta_esperada),
            "su": float(r.sugerido), "ca": float(r.cantidad) if r.incluido else 0.0, "inc": bool(r.incluido),
            "co": None if pd.isna(r.costo_c_iva) else round(float(r.costo_c_iva), 2),
        } for r in f.itertuples()]
        if filas_sql:
            conn.execute(text("""
                INSERT INTO listas_compra_detalle
                    (lista_id, articulo_id, clase, proveedor, stock, venta_esperada, sugerido, cantidad, incluido, costo_c_iva)
                VALUES (:l, :a, :cl, :pr, :st, :ve, :su, :ca, :inc, :co)
            """), filas_sql)
        registrar_historial("listas_compra", lid, "EDICION" if previa else "ALTA",
                            f"Lista de compras del {fecha:%d/%m/%Y} {'reemplazada' if previa else 'guardada'}: "
                            f"{renglones} renglones de {len(f)} sugeridos, ${costo:,.0f} estimado con IVA", conn=conn)
    return lid, bool(previa)


def listas_compra(limite=None):
    """Listas guardadas, la más reciente primero."""
    if not hay_registro_viajes():
        return pd.DataFrame(columns=["id", "fecha", "renglones", "costo_estimado", "persona", "nota"])
    q = """SELECT l.id, l.fecha, l.renglones, l.costo_estimado::float AS costo_estimado, p.nombre AS persona, l.nota,
                  l.actualizado_en
           FROM listas_compra l LEFT JOIN personas p ON p.id = l.persona_id ORDER BY l.fecha DESC"""
    if limite:
        q += f" LIMIT {int(limite)}"
    return pd.read_sql(text(q), engine)


def articulos_fuera_del_viaje():
    """Lo que no se compra en el viaje al mayorista: compra diaria (y sus
    fraccionados) y mercadería en consignación. No cuentan como 'agregados'."""
    sql = """
        SELECT a.id FROM articulos a
        LEFT JOIN articulos b ON b.id = a.articulo_fraccion_de
        WHERE COALESCE(a.reposicion_dias, 0) = 1 OR COALESCE(b.reposicion_dias, 0) = 1
    """
    with engine.connect() as conn:
        fuera = set(conn.execute(text(sql)).scalars().all())
    if hay_consignacion():
        fuera |= set(proveedores_consignacion_articulos())
    return fuera


def proveedores_consignacion_articulos():
    """Artículos cuyos proveedores son todos en consignación."""
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT app.articulo_id FROM articulo_proveedor_precio app JOIN proveedores p ON p.id = app.proveedor_id
            GROUP BY app.articulo_id HAVING bool_and(p.consignacion)
        """)).scalars().all()


def compras_en_ventana(desde, hasta):
    """Lo facturado por artículo entre dos fechas de factura (incluidas), con IVA."""
    return pd.read_sql(text("""
        SELECT d.articulo_id, a.nombre, a.unidad_medida, SUM(d.cantidad)::float AS comprado,
               SUM(d.cantidad * d.costo_unitario * (1 + COALESCE(a.iva, 0.21)))::float AS monto_c_iva
        FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id JOIN articulos a ON a.id = d.articulo_id
        WHERE f.fecha BETWEEN :d AND :h
        GROUP BY d.articulo_id, a.nombre, a.unidad_medida
    """), engine, params={"d": desde, "h": hasta})


def dias_ventana_lista():
    """Días después de la lista que cuentan para compararla: frecuencia de viaje - 1."""
    return max(int(float(obtener_parametros().get("frecuencia_viaje_dias", 7))) - 1, 0)


def comparar_lista_compra(lista_id):
    """(detalle, resumen, ventana) de una lista guardada contra las facturas de su ventana."""
    import lista_compras as lc
    listas = listas_compra()
    ventanas = lc.ventanas_listas(listas, dias_ventana_lista())
    v = ventanas[ventanas["id"] == int(lista_id)].iloc[0]
    det = pd.read_sql(text("""
        SELECT d.articulo_id, a.nombre, a.unidad_medida, d.clase, d.proveedor, d.stock::float AS stock,
               d.venta_esperada::float AS venta_esperada, d.sugerido::float AS sugerido,
               d.cantidad::float AS cantidad, d.incluido, d.costo_c_iva::float AS costo_c_iva
        FROM listas_compra_detalle d JOIN articulos a ON a.id = d.articulo_id
        WHERE d.lista_id = :l ORDER BY d.proveedor, a.nombre
    """), engine, params={"l": int(lista_id)})
    compras = compras_en_ventana(v["desde"], v["hasta"])
    detalle, resumen = lc.comparar_lista(det, compras, articulos_fuera_del_viaje())
    return detalle, resumen, {"desde": v["desde"], "hasta": v["hasta"]}


def registrar_viaje(fecha, tipo, horas=None, lista_id=None, nota=None):
    persona = persona_actual()
    with engine.begin() as conn:
        vid = conn.execute(text("""
            INSERT INTO viajes_compra (fecha, tipo, horas, lista_id, nota, persona_id)
            VALUES (:f, :t, :h, :l, :n, :p) RETURNING id
        """), {"f": fecha, "t": tipo, "h": None if horas is None else round(float(horas), 1),
               "l": None if lista_id is None else int(lista_id), "n": nota or None,
               "p": persona["id"] if persona else None}).scalar()
        registrar_historial("viajes_compra", vid, "ALTA",
                            f"Viaje {tipo} del {fecha:%d/%m/%Y}"
                            + (f", {float(horas):g} h" if horas is not None else "")
                            + (f", con la lista #{lista_id}" if lista_id else ", sin lista")
                            + (f" -- {nota}" if nota else ""), conn=conn)
    return vid


def listar_viajes(limite=100):
    if not hay_registro_viajes():
        return pd.DataFrame(columns=["id", "fecha", "tipo", "horas", "lista_id", "lista_fecha", "nota", "persona"])
    return pd.read_sql(text(f"""
        SELECT v.id, v.fecha, v.tipo, v.horas::float AS horas, v.lista_id, l.fecha AS lista_fecha, v.nota,
               p.nombre AS persona
        FROM viajes_compra v LEFT JOIN listas_compra l ON l.id = v.lista_id
        LEFT JOIN personas p ON p.id = v.persona_id
        ORDER BY v.fecha DESC, v.id DESC LIMIT {int(limite)}
    """), engine)


def borrar_viaje(viaje_id):
    """Para corregir un viaje cargado por error (queda en el Historial)."""
    with engine.begin() as conn:
        v = conn.execute(text("SELECT fecha, tipo, horas FROM viajes_compra WHERE id = :i"),
                         {"i": int(viaje_id)}).first()
        if v is None:
            return False
        conn.execute(text("DELETE FROM viajes_compra WHERE id = :i"), {"i": int(viaje_id)})
        registrar_historial("viajes_compra", int(viaje_id), "ELIMINACION",
                            f"Viaje {v[1]} del {v[0]:%d/%m/%Y} borrado (cargado por error)", conn=conn)
    return True



# ============================================================
# TANDA 20: clase ABC de los combos (para que sus componentes la hereden)
# ============================================================

def clase_combos(hasta, dias=90, corte_a=80.0, corte_b=95.0):
    """
    Clase ABC de cada combo como PRODUCTO del mostrador: se ordena lo que se
    vendió en los `dias` días hasta `hasta` (incluido), cada combo con su
    propio importe y cada artículo con su venta directa (sin lo vendido
    dentro de combos, para no contar dos veces), con los mismos cortes que
    la clasificación ABC. Sin consumo interno. Devuelve una fila por
    componente de combo activo: componente_articulo_id, combo_id, combo,
    clase_combo (solo combos con venta en la ventana).
    """
    df = pd.read_sql(text("""
        WITH ingreso AS (
            SELECT v.articulo_id, SUM(v.monto_total)::float AS ingreso
            FROM ventas v
            WHERE NOT v.es_consumo_interno AND v.monto_total > 0
              AND v.fecha > CAST(:hasta AS date) - CAST(:dias AS integer) AND v.fecha <= CAST(:hasta AS date)
            GROUP BY v.articulo_id
        )
        SELECT i.articulo_id, i.ingreso, a.nombre, COALESCE(a.categoria = :promo, FALSE) AS es_combo
        FROM ingreso i JOIN articulos a ON a.id = i.articulo_id
        ORDER BY i.ingreso DESC
    """), engine, params={"hasta": hasta, "dias": int(dias), "promo": CATEGORIA_PROMOCION})
    vacio = pd.DataFrame(columns=["componente_articulo_id", "combo_id", "combo", "clase_combo"])
    if df.empty or not df["es_combo"].any():
        return vacio
    acum = df["ingreso"].cumsum() / df["ingreso"].sum() * 100
    df["clase_combo"] = ["A" if x <= corte_a else ("B" if x <= corte_b else "C") for x in acum]
    combos = df[df["es_combo"]]
    comp = pd.read_sql(text("""
        SELECT pc.promocion_articulo_id AS combo_id, pc.componente_articulo_id
        FROM promocion_componentes pc JOIN articulos p ON p.id = pc.promocion_articulo_id
        WHERE p.activo
    """), engine)
    out = comp.merge(combos.rename(columns={"articulo_id": "combo_id", "nombre": "combo"})[
        ["combo_id", "combo", "clase_combo"]], on="combo_id")
    return out[["componente_articulo_id", "combo_id", "combo", "clase_combo"]] if not out.empty else vacio



# ============================================================
# TANDA 21: juego de las monedas (Desafío Recreo)
# ============================================================

def total_ticket(ticket_id):
    """(fecha, total cobrado) de un ticket de venta; None si no existe."""
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT t.fecha, COALESCE(SUM(v.monto_total), 0)::float AS total
            FROM ventas_ticket t LEFT JOIN ventas v ON v.ticket_id = t.id AND NOT v.es_consumo_interno
            WHERE t.id = :t GROUP BY t.fecha
        """), {"t": int(ticket_id)}).first()
    return (r[0], float(r[1])) if r else None


def regalar_monedas(articulo_id, cantidad, fecha, ticket_venta, conn):
    """Las monedas de chocolate que se lleva el cliente: baja de stock como consumo
    interno (medio 'Regalo') y gasto a costo 'Regalo a clientes', en la transacción
    `conn`. Devuelve (costo, avisos)."""
    art = conn.execute(text("SELECT nombre, costo_compra_actual FROM articulos WHERE id = :a"),
                       {"a": int(articulo_id)}).mappings().first()
    if not art:
        raise ValueError("El artículo de las monedas no existe: elegilo en 🔒 Configuración del desafío.")
    costo = round(float(art["costo_compra_actual"] or 0) * float(cantidad), 2)
    ticket = crear_ticket(fecha, MEDIO_REGALO, conn=conn)
    avisos = _insertar_venta(conn, int(articulo_id), float(cantidad), 0, fecha, medio_pago=MEDIO_REGALO,
                             ticket_id=ticket, es_consumo_interno=True)
    if costo > 0:
        registrar_gasto(CATEGORIA_REGALO, costo, fecha,
                        descripcion=f"Juego de las monedas (venta #{ticket_venta}): {float(cantidad):g} x {art['nombre']}",
                        conn=conn)
    registrar_historial("ventas", ticket, "REGALO_CLIENTE",
                        f"Juego de las monedas con la venta #{ticket_venta}: {float(cantidad):g} x {art['nombre']} "
                        f"(costo ${costo:,.2f})", conn=conn)
    return costo, avisos


# ------------------------------------------------------------
# Tanda 21: stock corregido por conteo (Desafío Recreo) y cierre del día guardado
# ------------------------------------------------------------
def corregir_stock(articulo_id, nuevo_stock, conn=None):
    """Deja el stock del artículo en `nuevo_stock` (después de contarlo). Queda en el
    historial con el mismo texto que la edición a mano ("Stock corregido a mano en
    'X': a -> b"), que es el que leen la reconstrucción del stock y validacion.py."""
    def _hacer(c):
        ant = c.execute(text("SELECT nombre, stock_actual FROM articulos WHERE id = :a"),
                        {"a": int(articulo_id)}).mappings().first()
        if ant is None:
            raise ValueError("No existe el artículo.")
        if abs(float(ant["stock_actual"]) - float(nuevo_stock)) < 1e-9:
            return False
        c.execute(text("UPDATE articulos SET stock_actual = :s, actualizado_en = now() WHERE id = :a"),
                  {"s": round(float(nuevo_stock), 3), "a": int(articulo_id)})
        registrar_historial("articulos", int(articulo_id), "EDICION",
                            f"Stock corregido a mano en '{ant['nombre']}': "
                            f"{_cant_txt(ant['stock_actual'])} -> {_cant_txt(nuevo_stock)}", conn=c)
        return True
    if conn is not None:
        return _hacer(conn)
    with engine.begin() as c:
        return _hacer(c)


def hay_cierres():
    """La tabla del cierre del día existe (migracion_tanda21.py)."""
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('public.cierres_caja') IS NOT NULL")).scalar())


def cierre_guardado(fecha):
    """El cierre guardado de ese día (dict) o None."""
    if not hay_cierres():
        return None
    with engine.connect() as conn:
        r = conn.execute(text("""SELECT c.*, p.nombre AS persona FROM cierres_caja c
                                 LEFT JOIN personas p ON p.id = c.persona_id WHERE c.fecha = :f"""),
                         {"f": fecha}).mappings().first()
    return dict(r) if r else None


def guardar_cierre_caja(fecha, fondo, ventas_efectivo, gastos_caja, contado, pan_sin_sobrante=False):
    """Guarda (o reemplaza) el cierre del día: el arqueo del efectivo y si sobró pan."""
    if contado is None:
        raise ValueError("Contá el cajón y cargá el efectivo contado.")
    esperado = round(float(fondo or 0) + float(ventas_efectivo or 0) - float(gastos_caja or 0), 2)
    persona = persona_actual()
    with engine.begin() as conn:
        ya = conn.execute(text("SELECT 1 FROM cierres_caja WHERE fecha = :f"), {"f": fecha}).scalar()
        conn.execute(text("""
            INSERT INTO cierres_caja (fecha, persona_id, fondo, ventas_efectivo, gastos_caja, esperado, contado,
                                      diferencia, pan_sin_sobrante)
            VALUES (:f, :p, :fo, :v, :g, :e, :c, :d, :pan)
            ON CONFLICT (fecha) DO UPDATE SET persona_id = EXCLUDED.persona_id, fondo = EXCLUDED.fondo,
                ventas_efectivo = EXCLUDED.ventas_efectivo, gastos_caja = EXCLUDED.gastos_caja,
                esperado = EXCLUDED.esperado, contado = EXCLUDED.contado, diferencia = EXCLUDED.diferencia,
                pan_sin_sobrante = EXCLUDED.pan_sin_sobrante, actualizado_en = now()
        """), {"f": fecha, "p": persona["id"] if persona else None, "fo": float(fondo or 0),
               "v": float(ventas_efectivo or 0), "g": float(gastos_caja or 0), "e": esperado,
               "c": float(contado), "d": round(float(contado) - esperado, 2), "pan": bool(pan_sin_sobrante)})
        registrar_historial("cierres_caja", None, "CIERRE_DIA",
                            f"Cierre del {fecha:%d/%m/%Y}{' (reemplazado)' if ya else ''}: esperado ${esperado:,.2f}, "
                            f"contado ${float(contado):,.2f}, diferencia ${float(contado) - esperado:,.2f}"
                            + (", no sobró pan" if pan_sin_sobrante else ""), conn=conn)
    return round(float(contado) - esperado, 2)


def pan_resuelto(fecha):
    """El pan del día quedó registrado: se cargó sobrante o vencido de algún artículo
    de compra diaria (o sus fraccionados), o no se compró pan ese día. Sin la
    respuesta del cierre ("no sobró"), un día con compra y sin sobrante queda abierto."""
    ids = ids_compra_diaria()
    if not ids:
        return True
    with engine.connect() as conn:
        frac = [r[0] for r in conn.execute(text("SELECT id FROM articulos WHERE articulo_fraccion_de = ANY(:i)"),
                                           {"i": list(ids)})]
        todos = list(ids) + frac
        compro = conn.execute(text("""SELECT 1 FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
                                      WHERE f.fecha = :f AND d.articulo_id = ANY(:i) LIMIT 1"""),
                              {"f": fecha, "i": todos}).scalar()
        if not compro:
            return True
        return bool(conn.execute(text("""SELECT 1 FROM ventas WHERE fecha = :f AND articulo_id = ANY(:i)
                                         AND es_consumo_interno AND medio_pago IN (:s, :v) LIMIT 1"""),
                                 {"f": fecha, "i": todos, "s": MEDIO_SOBRANTE, "v": MEDIO_VENCIDO}).scalar())
