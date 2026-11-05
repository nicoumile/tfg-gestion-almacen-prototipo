"""
Lectura y marcado de la planilla "Registro de facturas" (lector automático
de facturas: Drive + Apps Script + Gemini, en la cuenta de Google del comerciante).

La planilla es el registro completo y auditable de cada factura (cabecera,
renglones, IVA, CAE, correcciones). A la app solo pasa lo que mueve stock y
costos: proveedor, número, fecha y, por cada renglón de producto que es
stock, la cantidad y el costo unitario sin IVA en la unidad del catálogo.

Configuración (st.secrets / .streamlit/secrets.toml):

    PLANILLA_FACTURAS_ID = "id de la planilla (lo que va entre /d/ y /edit en la URL)"

    [gcp_service_account]
    type = "service_account"
    project_id = "..."
    private_key_id = "..."
    private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
    client_email = "app-almacen@....iam.gserviceaccount.com"
    client_id = "..."
    token_uri = "https://oauth2.googleapis.com/token"

La planilla tiene que estar compartida con client_email como Editor (para
poder marcar las facturas importadas).

Las funciones de transformación (preparar_renglones, item_para_app, ...) son
puras -- no tocan Google ni la base -- para poder probarlas sin conexión.
"""
from datetime import date, datetime
from difflib import SequenceMatcher

import streamlit as st

HOJA_COMPROBANTES = "Comprobantes"
HOJA_ITEMS = "Ítems"
COL_FACTURA_APP = "Factura app"   # la agrega la app la primera vez que marca una factura

# Diccionario de productos por proveedor (lo arma el lector; la app le escribe el vínculo con el catálogo)
HOJA_DICC = "Diccionario"
COL_DICC_ID = "id_articulo_app"
COL_DICC_FACTOR = "Factor pack"
COL_DICC_NOMBRE = "Artículo app"   # la agrega la app si no existe (solo para leer la planilla)
SIM_MINIMA_CODIGO = 0.5            # mismo código pero descripción muy distinta -> no se confía en el código


# ------------------------------------------------------------
# Conversión de valores de la planilla
# ------------------------------------------------------------
def num(valor):
    """Número desde la planilla (viene sin formato, pero por las dudas acepta '1.234,56')."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = "".join(ch for ch in str(valor) if ch.isdigit() or ch in ",.-")
    if not s or s == "-":
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def fecha(valor):
    """Fecha desde la planilla: 'dd/mm/aaaa', 'aaaa-mm-dd' o date/datetime."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    s = str(valor or "").strip().split(" ")[0]
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(s, formato).date()
        except ValueError:
            continue
    return None


def texto(valor):
    return "" if valor is None else str(valor).strip()


def filas_a_dicts(valores):
    """[[encabezados], [fila], ...] -> lista de dicts (las filas vacías se descartan)."""
    if not valores:
        return []
    enc = [texto(h) for h in valores[0]]
    salida = []
    for fila in valores[1:]:
        if not any(texto(v) for v in fila):
            continue
        fila = list(fila) + [""] * (len(enc) - len(fila))
        salida.append(dict(zip(enc, fila)))
    return salida


def link_archivo(file_id):
    return f"https://drive.google.com/file/d/{file_id}/view" if texto(file_id) else None


# ------------------------------------------------------------
# Transformación: renglones de la planilla -> ítems de la app
# ------------------------------------------------------------
def pendientes(comprobantes):
    """Comprobantes publicados que todavía no se importaron a la app (más viejos primero)."""
    pend = [c for c in comprobantes if texto(c.get("ID")) and not texto(c.get(COL_FACTURA_APP))]
    return sorted(pend, key=lambda c: (fecha(c.get("Fecha")) or date.min, texto(c.get("ID"))))


def preparar_renglones(items_planilla):
    """
    Deja solo los renglones que mueven stock (Tipo línea = producto y Es stock = SI)
    y calcula lo que necesita la app. El factor sugerido sale de la decisión ya
    tomada en la planilla: Cant. stock / Cant. facturada (ej. 1 caja -> 60 unidades
    = 60; 3,98 kg -> 3,98 kg = 1).
    """
    renglones = []
    for it in items_planilla:
        tipo = texto(it.get("Tipo línea")).lower() or "producto"
        es_stock = texto(it.get("Es stock")).upper() or "SI"
        if tipo != "producto" or es_stock != "SI":
            continue
        facturada = num(it.get("Cant. facturada"))
        stock = num(it.get("Cant. stock"))
        costo_total = num(it.get("Costo total s/IVA"))
        if facturada and stock:
            factor = round(stock / facturada, 4)
        else:
            factor = 1.0
        renglones.append({
            "linea": int(num(it.get("Línea")) or len(renglones) + 1),
            "codigo": texto(it.get("Código")),
            "descripcion": texto(it.get("Descripción")),
            "unidad": texto(it.get("Unidad")),
            "unidad_stock": texto(it.get("Unidad stock")) or "un",
            "cant_facturada": facturada,
            "costo_total_sin_iva": costo_total,
            "factor_sugerido": factor if factor > 0 else 1.0,
        })
    return renglones


def item_para_app(renglon, factor):
    """
    Cantidad y costo unitario sin IVA en la unidad del catálogo:
        cantidad = cantidad facturada × factor
        costo    = costo total sin IVA del renglón ÷ cantidad
    Devuelve (cantidad, costo_unitario) o (None, None) si faltan datos.
    """
    facturada = renglon.get("cant_facturada")
    costo_total = renglon.get("costo_total_sin_iva")
    if not facturada or not factor or costo_total is None:
        return None, None
    cantidad = round(facturada * factor, 3)
    if cantidad <= 0:
        return None, None
    return cantidad, round(costo_total / cantidad, 2)


def digitos(valor):
    return "".join(ch for ch in texto(valor) if ch.isdigit())


def _normalizar(desc):
    return " ".join(texto(desc).upper().split())


def similitud(a, b):
    return SequenceMatcher(None, _normalizar(a), _normalizar(b)).ratio()


def indice_diccionario(filas):
    """
    Filas de la hoja Diccionario (dicts) -> {(cuit, código): entrada, (cuit, '#desc'): entrada}.
    Solo entran las que ya tienen un artículo de la app vinculado.
    """
    indice = {}
    for f in filas:
        art = num(f.get(COL_DICC_ID))
        cuit = digitos(f.get("CUIT Proveedor"))
        if not art or not cuit:
            continue
        entrada = {"articulo_id": int(art), "factor": num(f.get(COL_DICC_FACTOR)),
                   "descripcion": texto(f.get("Descripción")), "codigo": texto(f.get("Código"))}
        if entrada["codigo"]:
            indice[(cuit, entrada["codigo"])] = entrada
        if entrada["descripcion"]:
            indice.setdefault((cuit, "#" + _normalizar(entrada["descripcion"])), entrada)
    return indice


def vinculo_por_codigo(indice, cuit, renglon):
    """
    Artículo de la app que el diccionario tiene para este renglón: primero por código del
    proveedor (si la descripción no es de otro producto), si no por descripción idéntica.
    Devuelve la entrada del índice (con 'por' = 'código' o 'descripción') o None.
    """
    cuit = digitos(cuit)
    cod = texto(renglon.get("codigo"))
    e = indice.get((cuit, cod)) if cod else None
    if e and similitud(e["descripcion"], renglon.get("descripcion")) >= SIM_MINIMA_CODIGO:
        return {**e, "por": "código"}
    e = indice.get((cuit, "#" + _normalizar(renglon.get("descripcion"))))
    return {**e, "por": "descripción"} if e else None


def es_nota(comprobante):
    """'NC' o 'ND' si el comprobante es nota de crédito / débito (Tipo 'NC A', 'ND B', ...); si no, ''."""
    tipo = texto(comprobante.get("Tipo")).upper().replace(".", "").replace(" ", "")
    tipo = tipo.replace("É", "E").replace("Í", "I")
    for pref, cod in (("NC", "NC"), ("NOTADECREDITO", "NC"), ("ND", "ND"), ("NOTADEDEBITO", "ND")):
        if tipo.startswith(pref):
            return cod
    return ""


def referencia(comprobante):
    return f"planilla:{texto(comprobante.get('ID'))}"


# ------------------------------------------------------------
# Acceso a Google Sheets
# ------------------------------------------------------------
def configurado():
    try:
        return "PLANILLA_FACTURAS_ID" in st.secrets and "gcp_service_account" in st.secrets
    except Exception:
        return False


def _planilla():
    import gspread  # importado acá: la app funciona aunque el importador no esté configurado
    cliente = gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))
    return cliente.open_by_key(st.secrets["PLANILLA_FACTURAS_ID"])


@st.cache_data(ttl=60, show_spinner="Leyendo la planilla de facturas…")
def leer_planilla():
    """
    Devuelve (comprobantes, items_por_id). Los números vienen sin formato
    (UNFORMATTED_VALUE) y las fechas como texto dd/mm/aaaa.
    Se cachea 60 s; el botón "Actualizar" de la pantalla limpia el caché.
    """
    libro = _planilla()
    opciones = {"value_render_option": "UNFORMATTED_VALUE", "date_time_render_option": "FORMATTED_STRING"}
    comprobantes = filas_a_dicts(libro.worksheet(HOJA_COMPROBANTES).get_all_values(**opciones))
    items = filas_a_dicts(libro.worksheet(HOJA_ITEMS).get_all_values(**opciones))
    por_id = {}
    for it in items:
        por_id.setdefault(texto(it.get("ID")), []).append(it)
    return comprobantes, por_id


def marcar_importado(comprobante_id, factura_id, valor=None):
    """
    Escribe el id de la factura de la app ('#id') en la columna 'Factura app' del comprobante,
    o el texto `valor` si se pasa (ej. 'NC sin importar'): cualquier valor lo saca de pendientes.
    """
    hoja = _planilla().worksheet(HOJA_COMPROBANTES)
    encabezados = hoja.row_values(1)
    if COL_FACTURA_APP in encabezados:
        col = encabezados.index(COL_FACTURA_APP) + 1
    else:
        col = len(encabezados) + 1
        if col > hoja.col_count:
            hoja.add_cols(col - hoja.col_count)
        hoja.update_cell(1, col, COL_FACTURA_APP)
    ids = hoja.col_values(encabezados.index("ID") + 1)
    if comprobante_id not in ids:
        raise ValueError(f"No encontré el comprobante {comprobante_id} en la hoja '{HOJA_COMPROBANTES}'.")
    fila = ids.index(comprobante_id) + 1
    hoja.update_cell(fila, col, valor if valor else f"#{factura_id}")
    leer_planilla.clear()


@st.cache_data(ttl=60, show_spinner=False)
def leer_diccionario():
    """Índice del Diccionario (ver indice_diccionario). Vacío si la hoja no existe."""
    import gspread
    try:
        hoja = _planilla().worksheet(HOJA_DICC)
    except gspread.WorksheetNotFound:
        return {}
    filas = filas_a_dicts(hoja.get_all_values(value_render_option="UNFORMATTED_VALUE"))
    return indice_diccionario(filas)


def _col_letra(n):
    letras = ""
    while n:
        n, r = divmod(n - 1, 26)
        letras = chr(65 + r) + letras
    return letras


def filas_a_actualizar(filas, cuit, vinculos):
    """
    Cálculo puro de qué escribir: para cada vínculo {codigo, descripcion, articulo_id, nombre,
    factor} busca la fila del proveedor por código (o por descripción si no hay código) y
    devuelve [(n_fila_planilla, {columna: valor}), ...] y la lista de vínculos sin fila.
    filas: dicts de la hoja en orden (la fila 2 de la planilla es filas[0]).
    """
    cuit = digitos(cuit)
    cambios, sin_fila = [], []
    for v in vinculos:
        cod, desc = texto(v.get("codigo")), _normalizar(v.get("descripcion"))
        fila = None
        for i, f in enumerate(filas):
            if digitos(f.get("CUIT Proveedor")) != cuit:
                continue
            if (cod and texto(f.get("Código")) == cod) or (not cod and _normalizar(f.get("Descripción")) == desc):
                fila = i + 2
                break
        if fila is None and cod:  # código no está: probar por descripción exacta
            for i, f in enumerate(filas):
                if digitos(f.get("CUIT Proveedor")) == cuit and _normalizar(f.get("Descripción")) == desc:
                    fila = i + 2
                    break
        if fila is None:
            sin_fila.append(v)
            continue
        valores = {COL_DICC_ID: int(v["articulo_id"]), COL_DICC_NOMBRE: texto(v.get("nombre"))}
        factor = v.get("factor")
        # El lector usa Factor pack como "unidades por bulto": > 1 se vende suelto, 1 entero.
        # Un factor menor a 1 no tiene sentido ahí, así que no se escribe.
        if factor and factor >= 1:
            valores[COL_DICC_FACTOR] = int(factor) if float(factor).is_integer() else round(float(factor), 4)
        cambios.append((fila, valores))
    return cambios, sin_fila


def actualizar_diccionario(cuit, vinculos):
    """
    Escribe en el Diccionario el artículo de la app (id y nombre) y el factor confirmados al
    importar, para que el lector y las próximas importaciones lo reconozcan por código.
    Una sola escritura (batch). Devuelve cuántas filas actualizó y los vínculos sin fila.
    """
    import gspread
    try:
        hoja = _planilla().worksheet(HOJA_DICC)
    except gspread.WorksheetNotFound:
        return 0, list(vinculos)
    valores = hoja.get_all_values(value_render_option="UNFORMATTED_VALUE")
    if not valores:
        return 0, list(vinculos)
    encabezados = [texto(h) for h in valores[0]]
    for col in (COL_DICC_ID, COL_DICC_FACTOR, COL_DICC_NOMBRE):
        if col not in encabezados:
            encabezados.append(col)
            n = len(encabezados)
            if n > hoja.col_count:
                hoja.add_cols(n - hoja.col_count)
            hoja.update_cell(1, n, col)
    cambios, sin_fila = filas_a_actualizar(filas_a_dicts_con_vacias(valores), cuit, vinculos)
    datos = [{"range": f"{_col_letra(encabezados.index(col) + 1)}{fila}", "values": [[valor]]}
             for fila, cols in cambios for col, valor in cols.items()]
    if datos:
        hoja.batch_update(datos, value_input_option="RAW")
    leer_diccionario.clear()
    return len(cambios), sin_fila


def filas_a_dicts_con_vacias(valores):
    """Como filas_a_dicts pero sin descartar filas vacías (para conservar el número de fila)."""
    if not valores:
        return []
    enc = [texto(h) for h in valores[0]]
    return [dict(zip(enc, list(f) + [""] * (len(enc) - len(f)))) for f in valores[1:]]
