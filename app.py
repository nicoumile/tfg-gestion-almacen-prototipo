import hashlib
import hmac
import json
import time

import streamlit as st
import pandas as pd
import numpy as np
import altair as alt
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import io
import importlib
import os
import db
import importador_planilla as imp
import pronostico as pron
import lista_compras as lc
import proveedores as anprov  # (no 'proveedores': así se llama una variable de Cargar Factura)
import desafio
import figuritas


def _recargar_modulos_cambiados(*modulos):
    """
    Streamlit vuelve a ejecutar app.py en cada interacción, pero los módulos
    importados (db.py, pronostico.py, ...) quedan cargados en memoria. En
    Streamlit Cloud, después de un push, a veces app.py nuevo convive con un
    db.py o pronostico.py viejo. Regla: la primera vez en cada proceso se
    recargan todos (no se puede saber si lo que hay en memoria es viejo);
    después, solo los que cambiaron en disco desde la última carga.
    db va primero porque los demás pueden usarlo.
    """
    for m in modulos:
        try:
            mtime = os.path.getmtime(m.__file__)
        except (OSError, TypeError):
            continue
        cargado = getattr(m, "_cargado_en", None)
        if cargado is None or mtime > cargado:
            m = importlib.reload(m)
            m._cargado_en = time.time()


_recargar_modulos_cambiados(db, imp, pron, lc, anprov)

ZONA_ARG = ZoneInfo("America/Argentina/Buenos_Aires")

def hoy_argentina():
    """
    'Hoy' pero en hora de Argentina, no en la del servidor donde corre
    Streamlit (que en la nube suele estar en UTC, 3 horas adelantado y
    sin horario de verano). Usar esto -- y no date.today() -- para
    cualquier valor por defecto de fecha: entre las ~21:00 y la
    medianoche hora Argentina, date.today() en un servidor UTC ya "cree"
    que es mañana.
    """
    return datetime.now(ZONA_ARG).date()

def formatear_articulo(df, articulo_id):
    """Nombre + marca entre paréntesis (si tiene), para distinguir artículos con el mismo nombre."""
    fila = df[df["id"] == articulo_id].iloc[0]
    marca = fila.get("marca")
    texto = f"{fila['nombre']} ({marca})" if pd.notna(marca) and marca else fila["nombre"]
    if "activo" in fila and not bool(fila["activo"]):
        texto += " — ⛔ inactivo"
    return texto


def pesos_venta(v):
    """$1.234 (o $1.234,50 si tiene centavos), formato argentino."""
    txt = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if txt.endswith(",00"):
        txt = txt[:-3]
    return ("−" if v < 0 else "") + "$" + txt


@st.dialog("🗑️ Confirmar eliminación")
def confirmar_eliminacion(que, detalle, ejecutar, mensaje_ok):
    """
    Cuadro de confirmación para los botones de borrar. `ejecutar` es una
    función sin argumentos que hace el borrado y puede devolver
    (ok, mensaje_error); si devuelve otra cosa se toma como éxito.
    Se cierra con "Cancelar", la ✕ o la tecla Escape sin borrar nada.
    """
    # Un diálogo (igual que un fragmento) vuelve a correr SOLO esta función, sin
    # pasar por el comienzo de la app donde se fija quién atiende: hay que fijarlo
    # acá para que el historial guarde quién borró.
    db.set_persona_actual(PERSONA["id"], PERSONA["nombre"])
    st.markdown(f"Vas a eliminar **{que}**.".replace("$", "\\$"))
    if detalle:
        st.warning(detalle)
    c1, c2 = st.columns(2)
    if c1.button("Sí, eliminar", type="primary", width="stretch", key="dlg_eliminar_si"):
        resultado = ejecutar()
        ok, error = resultado if isinstance(resultado, tuple) else (True, None)
        if ok:
            avisar("success", mensaje_ok)
            st.rerun()
        else:
            st.error(error)
    if c2.button("Cancelar", width="stretch", key="dlg_eliminar_no"):
        st.rerun()


def pesos_tabla(v):
    """pesos_venta para .style.format de tablas (celda vacía -> "-")."""
    return "-" if pd.isna(v) else pesos_venta(float(v))


# Plata (y porcentajes) en tablas: formato numérico del navegador (en español:
# 1.234,5) con "($)" o "(%)" en el título. En textos y métricas: pesos_venta().
FMT_PESOS = "localized"


def md_pesos(v):
    """pesos_venta para usar dentro de textos con formato (st.caption, st.markdown,
    st.warning...): ahí dos signos $ en la misma línea se toman como fórmula."""
    return pesos_venta(v).replace("$", "\\$")


def etiquetador(df):
    """
    Igual que formatear_articulo, pero arma todas las etiquetas de una vez y
    devuelve una función para usar como format_func. formatear_articulo
    filtra el DataFrame entero por cada opción: en un selector de 560
    artículos eran 560 filtrados (~0,3 s) en cada clic, y en el importador
    eso se repetía por cada renglón de la factura.
    """
    etiquetas = {}
    if not df.empty:
        marca = df["marca"] if "marca" in df.columns else pd.Series(None, index=df.index)
        inactivo = (~df["activo"].astype(bool)) if "activo" in df.columns else pd.Series(False, index=df.index)
        for aid, nombre, m, inact in zip(df["id"], df["nombre"], marca, inactivo):
            texto = f"{nombre} ({m})" if pd.notna(m) and m else nombre
            if inact:
                texto += " — ⛔ inactivo"
            etiquetas[int(aid)] = texto

    def _formato(articulo_id):
        try:
            return etiquetas[int(articulo_id)]
        except (KeyError, TypeError, ValueError):
            return formatear_articulo(df, articulo_id)
    return _formato

def mostrar_importador():
    """
    Herramienta "Desde la planilla" de Cargar Factura: importa las facturas que ya leyó el
    lector automático (Drive + Apps Script + Gemini) y se confirmaron en la planilla
    "Registro de facturas". Solo se vincula cada renglón con un artículo del catálogo; al
    importar, stock, costos y precios se actualizan con la misma db.crear_factura que la
    carga manual. Ver importador_planilla.py.
    """
    st.caption(
        "Facturas que ya leyó el lector automático y se confirmaron en la planilla "
        "\"Registro de facturas\". Elegí a qué artículo de tu catálogo corresponde cada renglón y tocá Importar: "
        "nada se carga hasta ese momento."
    )
    if not imp.configurado():
        st.warning("El importador todavía no está configurado: mientras tanto, cargá las facturas a mano.")
        with st.expander("Datos para quien administra la app"):
            st.markdown(
                "Falta cargar en los *secrets* de la app `PLANILLA_FACTURAS_ID` y la cuenta de "
                "servicio de Google (`[gcp_service_account]`), y compartir la planilla con el mail "
                "de esa cuenta como **Editor**. El formato está en `importador_planilla.py`."
            )
    else:
        resultado = st.session_state.pop("imp_resultado", None)
        if resultado:
            st.success(f"✅ {resultado[0]} importada como factura #{resultado[1]}. Stock y costos actualizados.")
            for aviso in resultado[2]:
                st.warning(aviso)
            if len(resultado) > 3:
                st.warning(resultado[3])

        if st.button("🔄 Actualizar desde la planilla"):
            imp.leer_planilla.clear()
            st.rerun()
        try:
            comprobantes, items_por_id = imp.leer_planilla()
        except Exception as e:
            comprobantes, items_por_id = None, {}
            st.error(f"No pude leer la planilla: {type(e).__name__}: {e}")

        pendientes = imp.pendientes(comprobantes) if comprobantes is not None else []
        if comprobantes is not None and not pendientes:
            st.success("✅ No hay facturas pendientes de importar.")

        if pendientes:
            st.write(f"**{len(pendientes)}** factura(s) pendiente(s) de importar.")
            opciones_comp = {imp.texto(c["ID"]): c for c in pendientes}

            def _etiqueta_comp(cid):
                c = opciones_comp[cid]
                total = imp.num(c.get("Total")) or 0
                return (f"{cid} · {imp.texto(c.get('Proveedor'))} · N° {imp.texto(c.get('Número'))} · "
                        f"{imp.texto(c.get('Fecha'))} · {pesos_venta(total)}")

            comp_id = st.selectbox("Factura a importar", list(opciones_comp), format_func=_etiqueta_comp)
            comp = opciones_comp[comp_id]
            ref = imp.referencia(comp)
            numero = imp.texto(comp.get("Número"))
            fecha_comp = imp.fecha(comp.get("Fecha")) or hoy_argentina()
            cuit = imp.texto(comp.get("CUIT Proveedor"))
            link = imp.link_archivo(comp.get("_id"))
            if link:
                st.markdown(f"[📄 Ver la factura original]({link})")

            # ---------- 1. Proveedor ----------
            st.subheader("1. Proveedor")
            proveedor = db.buscar_proveedor_por_cuit(cuit)
            proveedor_id = None
            if proveedor:
                proveedor_id = int(proveedor["id"])
                st.success(f"✅ **{proveedor['nombre']}** (CUIT {cuit})")
            else:
                st.info(f"No tengo ningún proveedor con CUIT **{cuit or '—'}** "
                        f"(en la factura: {imp.texto(comp.get('Proveedor'))}).")
                provs = db.listar_proveedores()
                c1, c2 = st.columns(2)
                with c1:
                    nombre_nuevo = st.text_input("Crear proveedor nuevo", value=imp.texto(comp.get("Proveedor")),
                                                 key=f"imp_prov_nuevo_{comp_id}")
                    if st.button("➕ Crear proveedor", key=f"imp_crear_prov_{comp_id}"):
                        db.crear_proveedor_devolviendo_id(nombre_nuevo.strip(), cuit or None)
                        st.rerun()
                with c2:
                    if not provs.empty:
                        existente = st.selectbox(
                            "…o es uno que ya tengo (sin CUIT cargado)", provs["id"],
                            format_func=lambda x: provs[provs["id"] == x]["nombre"].values[0],
                            key=f"imp_prov_existente_{comp_id}"
                        )
                        if st.button("Usar este y guardarle el CUIT", key=f"imp_asignar_cuit_{comp_id}"):
                            db.asignar_cuit_proveedor(int(existente), cuit)
                            st.rerun()

            # ---------- Notas de crédito / débito: todavía no se importan ----------
            # Importarlas como compra sumaría stock y pisaría el costo con el precio de la nota.
            nota = imp.es_nota(comp)
            if nota:
                st.warning(
                    f"**{comp_id} es una nota de {'crédito' if nota == 'NC' else 'débito'}** "
                    f"({imp.texto(comp.get('Tipo'))}). El importador todavía no las procesa: importarla como "
                    "compra sumaría stock en vez de restarlo. Si fue devolución de mercadería, ajustá el stock "
                    "a mano en Artículos; si fue un descuento de precio, no hace falta hacer nada en la app."
                )
                if st.button("Marcarla como revisada (sacarla de pendientes)", key=f"imp_nota_{comp_id}"):
                    imp.marcar_importado(comp_id, None, valor=f"{nota} (no se importa)")
                    st.rerun()
                return

            # ---------- 2. ¿Ya está cargada? ----------
            ya_importada = db.factura_importada(ref)
            duplicada = db.factura_existente(proveedor_id, numero) if proveedor_id else None
            bloquear = False
            if ya_importada:
                bloquear = True
                st.warning(f"Esta factura ya se importó (factura #{ya_importada} en la app). "
                           "Solo falta marcarla en la planilla.")
                if st.button("Marcar como importada en la planilla", key=f"imp_marcar_{comp_id}"):
                    imp.marcar_importado(comp_id, ya_importada)
                    st.rerun()
            elif duplicada:
                st.warning(f"⚠️ Ya hay una factura #{duplicada} de este proveedor con el número {numero} "
                           "(¿la cargaste a mano?). Si la importás de nuevo, el stock se suma dos veces.")
                c1, c2 = st.columns(2)
                if c1.button("Es la misma: marcarla como ya cargada", key=f"imp_marcar_dup_{comp_id}"):
                    imp.marcar_importado(comp_id, duplicada)
                    st.rerun()
                bloquear = not c2.checkbox("Es otra factura: importar igual", key=f"imp_forzar_{comp_id}")

            # ---------- 3. Renglones ----------
            renglones = imp.preparar_renglones(items_por_id.get(comp_id, []))
            if proveedor_id and not bloquear:
                st.subheader("2. Artículos")
                st.caption(
                    "Elegí a qué artículo de tu catálogo corresponde cada renglón. Lo que confirmes queda "
                    "guardado para este proveedor: la próxima factura se reconoce sola. "
                    "**Unidades por renglón** = cuántas unidades de tu artículo trae cada unidad facturada "
                    "(caja de 60 que vendés suelta → 60; si vendés el pack entero o es por kg → 1)."
                )
                articulos = db.listar_articulos()

                with st.expander("➕ Crear un artículo nuevo (si alguno todavía no está en el catálogo)"):
                    with st.form(f"imp_nuevo_art_{comp_id}", enter_to_submit=False):
                        nombres_renglones = [r["descripcion"] for r in renglones]
                        base = st.selectbox("Renglón de la factura", nombres_renglones) if nombres_renglones else ""
                        n_nombre = st.text_input("Nombre en tu catálogo", value="",
                                                 placeholder=base or "Nombre del artículo")
                        c1, c2, c3, c4 = st.columns(4)
                        cats = db.listar_categorias()
                        n_cat = c1.selectbox("Categoría", cats + ["(otra)"]) if cats else "(otra)"
                        n_cat_otra = c1.text_input("Categoría nueva") if n_cat == "(otra)" else ""
                        n_unidad = c2.selectbox("Se vende por", ["unidad", "kg"])
                        n_margen = c3.number_input("Margen %", min_value=0.0, value=30.0, step=1.0)
                        n_iva = c4.number_input("IVA %", min_value=0.0, max_value=27.0, value=21.0, step=0.5,
                                                 help="Alícuota de IVA del producto (21 % o 10,5 %). No es el margen.")
                        if st.form_submit_button("Crear artículo"):
                            nombre_final = (n_nombre or base or "").strip()
                            categoria_final = (n_cat_otra if n_cat == "(otra)" else n_cat).strip().capitalize() or None
                            if not nombre_final:
                                st.error("Poné un nombre.")
                            elif not db.buscar_articulo_por_nombre_exacto(nombre_final).empty:
                                st.error(f"Ya existe un artículo llamado '{nombre_final}'.")
                            else:
                                db.crear_articulo(nombre_final, categoria_final, 0, n_margen / 100, n_iva / 100,
                                                  0, 0, None, unidad_medida=n_unidad)
                                avisar("success", f"Artículo '{nombre_final}' creado: ya lo podés elegir abajo. "
                                           "El costo se completa al importar la factura.")
                                st.rerun()

                OMITIR, ELEGIR = -1, 0
                ids_catalogo = [int(x) for x in articulos["id"]]
                etiqueta_catalogo = etiquetador(articulos)  # una vez, no por renglón
                try:
                    diccionario = imp.leer_diccionario()
                except Exception as ex:
                    diccionario = {}
                    st.caption(f"(No pude leer el Diccionario de la planilla: {type(ex).__name__}; "
                               "se reconoce solo por nombre.)")
                todos_articulos = None  # se carga solo si el diccionario apunta a un inactivo
                elecciones = []
                for r in renglones:
                    clave = f"imp_{comp_id}_{r['linea']}"
                    alias_guardado = db.alias_con_factor(proveedor_id, r["descripcion"])
                    alias = alias_guardado
                    # 1º el Diccionario de la planilla (código del proveedor -> artículo de la app),
                    # 2º el alias por nombre exacto, 3º similitud de texto.
                    por_dicc = imp.vinculo_por_codigo(diccionario, cuit, r)
                    if por_dicc:
                        if todos_articulos is None and por_dicc["articulo_id"] not in ids_catalogo:
                            todos_articulos = db.listar_articulos(incluir_inactivos=True)
                        fila_art = (articulos if por_dicc["articulo_id"] in ids_catalogo else todos_articulos)
                        fila_art = fila_art[fila_art["id"] == por_dicc["articulo_id"]]
                        if not fila_art.empty:
                            alias = {**fila_art.iloc[0].to_dict(), "id": por_dicc["articulo_id"],
                                     "factor": float(r["factor_sugerido"]), "_dicc": por_dicc["por"]}
                    with st.container(border=True):
                        c1, c2, c3 = st.columns([2.4, 2.6, 1.2])
                        cant_txt = f"{r['cant_facturada']:g} {r['unidad'].lower()}" if r["cant_facturada"] else "—"
                        c1.markdown(f"**{r['descripcion']}**")
                        c1.caption(f"Cód. {r['codigo'] or '—'} · {cant_txt} · "
                                   f"{md_pesos(r['costo_total_sin_iva'] or 0)} s/IVA")

                        reemplazo_de = None
                        if alias and int(alias["id"]) not in ids_catalogo:
                            # el alias apunta a un artículo dado de baja: si tiene reemplazo
                            # activo se propone ese (el alias se actualiza al importar);
                            # si no, se elige de nuevo
                            repl = alias.get("reemplazado_por")
                            if pd.notna(repl) and int(repl) in ids_catalogo:
                                reemplazo_de = alias["nombre"]
                                alias = {**alias, "id": int(repl), "_reemplazo": True}
                            else:
                                alias = None
                        if alias:
                            sugeridos = [int(alias["id"])]
                            defecto = int(alias["id"])
                            factor_defecto = float(alias["factor"])
                        else:
                            sim = db.sugerir_articulos_similares(r["descripcion"])
                            sugeridos = [int(x) for x in sim["id"] if int(x) in ids_catalogo] if not sim.empty else []
                            defecto = sugeridos[0] if (not sim.empty and float(sim["similitud"].iloc[0]) >= 0.45) else ELEGIR
                            factor_defecto = float(r["factor_sugerido"])
                        opciones = [ELEGIR, OMITIR] + sugeridos + [i for i in ids_catalogo if i not in sugeridos]

                        def _fmt(x, sugeridos=sugeridos, alias=alias):
                            if x == ELEGIR:
                                return "— Elegí el artículo —"
                            if x == OMITIR:
                                return "⏭️ No importar este renglón"
                            marca = "✅ " if alias and x == int(alias["id"]) else ("⭐ " if x in sugeridos else "")
                            return marca + etiqueta_catalogo(x)

                        art = c2.selectbox("Artículo de tu catálogo", opciones, index=opciones.index(defecto),
                                           format_func=_fmt, key=f"{clave}_art")
                        factor = c3.number_input("Unidades por renglón", min_value=0.0001, value=factor_defecto,
                                                 step=1.0, format="%g", key=f"{clave}_factor")
                        cantidad, costo_u = imp.item_para_app(r, factor)
                        actualiza = True
                        if art not in (ELEGIR, OMITIR):
                            unidad_art = articulos[articulos["id"] == art]["unidad_medida"].values[0]
                            if cantidad:
                                c2.caption(f"→ entran **{cantidad:g} {'kg' if unidad_art == 'kg' else 'u.'}** "
                                           f"a **{md_pesos(costo_u)}** c/u (s/IVA)")
                            if reemplazo_de and art == int(alias["id"]):
                                c2.caption(f"🔁 antes se cargaba en {reemplazo_de} (dado de baja); ahora en su reemplazo")
                            elif alias and alias.get("_dicc") == "código":
                                c2.caption(f"📗 reconocido por el código {r['codigo']} (Diccionario)")
                            elif alias:
                                c2.caption("✅ reconocido de facturas anteriores")
                            actualiza = c3.checkbox("Actualiza precio", value=True, key=f"{clave}_act",
                                                    help="Destildalo si fue una compra puntual (oferta única): "
                                                         "suma stock pero no cambia el costo ni el precio de venta.")
                        elecciones.append({"renglon": r, "articulo_id": art, "factor": factor, "cantidad": cantidad,
                                           "costo_unitario": costo_u, "actualiza_precio": actualiza,
                                           "alias": alias_guardado})

                sin_resolver = [e for e in elecciones if e["articulo_id"] == ELEGIR]
                a_importar = [e for e in elecciones if e["articulo_id"] not in (ELEGIR, OMITIR)]
                invalidos = [e for e in a_importar if not e["cantidad"] or not e["costo_unitario"] or e["costo_unitario"] <= 0]

                st.subheader("3. Importar")
                total_imp = sum(e["cantidad"] * e["costo_unitario"] for e in a_importar if e["cantidad"] and e["costo_unitario"])
                c1, c2, c3 = st.columns(3)
                c1.metric("Renglones a importar", f"{len(a_importar)} de {len(elecciones)}")
                c2.metric("Total s/IVA a cargar", pesos_venta(total_imp))
                c3.metric("Neto de la factura", pesos_venta(imp.num(comp.get('Neto')) or 0))
                if sin_resolver:
                    st.info(f"Faltan {len(sin_resolver)} renglón(es) por vincular (o marcar \"No importar\").")
                for e in invalidos:
                    st.error(f"'{e['renglon']['descripcion']}': falta cantidad o costo en la planilla -- "
                             "corregilo ahí o marcá \"No importar\".")

                descuentos_imp = elegir_descuentos_suma_a_mano(
                    [(int(e["articulo_id"]), e["cantidad"] or 0, etiqueta_catalogo(e["articulo_id"])) for e in a_importar],
                    f"imp_{comp_id}") if a_importar and not invalidos else {}
                puede = bool(a_importar) and not sin_resolver and not invalidos
                if st.button("📥 Importar factura", type="primary", disabled=not puede, key=f"imp_go_{comp_id}"):
                    for e in a_importar:
                        alias = e["alias"]
                        if (not alias or int(alias["id"]) != e["articulo_id"] or alias.get("_reemplazo")
                                or abs(float(alias["factor"]) - e["factor"]) > 1e-9):
                            db.guardar_alias(int(e["articulo_id"]), proveedor_id, e["renglon"]["descripcion"], e["factor"])
                    items = [{"articulo_id": int(e["articulo_id"]), "cantidad": e["cantidad"],
                              "costo_unitario": e["costo_unitario"], "actualiza_precio": e["actualiza_precio"],
                              "descontar_ya_sumado": descuentos_imp.get(k, 0)}
                             for k, e in enumerate(a_importar)]
                    try:
                        factura_id, avisos = db.crear_factura(proveedor_id, numero, fecha_comp, items,
                                                              origen="llm", archivo_original=link, ref_externa=ref)
                    except Exception as ex:
                        if "idx_facturas_ref_externa" in str(ex):
                            st.error("Esta factura ya se había importado (otra pestaña o doble clic). No se cargó de nuevo.")
                        else:
                            st.error(f"No se pudo importar: {type(ex).__name__}: {ex}")
                    else:
                        st.session_state["imp_resultado"] = (comp_id, factura_id, avisos)
                        try:
                            imp.marcar_importado(comp_id, factura_id)
                        except Exception as ex:
                            st.session_state["imp_resultado"] += (f"Se importó, pero no pude marcarla en la planilla ({ex}). "
                                                                  "Al volver a abrirla te voy a ofrecer marcarla.",)
                        # El Diccionario de la planilla aprende el vínculo código del proveedor -> artículo
                        # (si falla no pasa nada: la factura ya está cargada y el alias guardado en la app).
                        vinculos = [{"codigo": e["renglon"]["codigo"], "descripcion": e["renglon"]["descripcion"],
                                     "articulo_id": int(e["articulo_id"]), "factor": e["factor"],
                                     "nombre": articulos.loc[articulos["id"] == e["articulo_id"], "nombre"].iloc[0]}
                                    for e in a_importar]
                        try:
                            imp.actualizar_diccionario(cuit, vinculos)
                        except Exception as ex:
                            avisar("warning", f"La factura se importó, pero no pude actualizar el Diccionario "
                                              f"de la planilla ({type(ex).__name__}). La próxima se reconoce igual por nombre.")
                        st.rerun()


st.set_page_config(page_title="Gestión de Almacén", layout="wide")


# ------------------------------------------------------------
# AVISOS QUE SOBREVIVEN AL st.rerun()
# Todo lo que se dibuja antes de un st.rerun() se borra al instante, así que
# un st.success("Venta confirmada") seguido de st.rerun() nunca se llegaba a
# ver (tampoco los avisos de stock negativo o de precio a revisar). avisar()
# los guarda en la sesión y mostrar_avisos() los dibuja arriba de la página
# en la corrida siguiente, una sola vez.
# ------------------------------------------------------------
def avisar(tipo, texto):
    st.session_state.setdefault("_avisos_pendientes", []).append((tipo, texto))


def _estrellitas_excursion():
    """Tanda 21: estrellitas de la Excursión (Desafío Recreo) apenas se guarda la lista o el viaje."""
    try:
        if desafio.hay_desafio():
            for txt in desafio.otorgar_excursiones(PERSONA["id"], hoy_argentina()):
                avisar("success", f"🚌 Desafío Recreo: {txt}")
    except Exception:
        pass  # el juego nunca traba la lista ni el viaje


def mostrar_avisos():
    dibujar = {"success": st.success, "warning": st.warning, "info": st.info, "error": st.error}
    for tipo, texto in st.session_state.pop("_avisos_pendientes", []):
        # "\$": sin escapar, Streamlit toma el texto entre dos "$" como fórmula
        dibujar.get(tipo, st.info)(str(texto).replace("$", "\\$"))

# Colores fijos por clase ABC (Reportes -> Clasificación ABC), misma paleta
# en todo el reporte para que "clase A" siempre se vea del mismo color.
# Elegidos de un set validado para que sean distinguibles incluso con
# daltonismo -- nunca son la única forma de distinguir la clase, siempre
# van acompañados de la letra en una columna/etiqueta, nunca solo el color.
COLOR_CLASE = {"A": "#2a78d6", "B": "#eb6834", "C": "#1baf7a"}

# Clave estable -> nombre visible. La clave es lo que se guarda en
# personas.permisos (así se puede cambiar el texto o el ícono de una
# pantalla sin tocar los permisos ya asignados).
PAGINAS_POR_CLAVE = {
    "dashboard": "📊 Dashboard",
    "articulos": "📦 Artículos",
    "cargar_factura": "🧾 Cargar Factura (Compra)",
    "fraccionar": "⚖️ Fraccionar",
    "promociones": "🎁 Promociones",
    "registrar_venta": "💵 Registrar Venta",
    "gastos": "💸 Gastos",
    "reportes": "📈 Reportes",
    "cierre_caja": "🧮 Cierre de Caja",
    "reposicion": "🛒 Lista de compras",
    "pronostico": "🔮 Pronóstico de demanda",
    "precios_proveedor": "🏭 Proveedores",  # (la clave queda igual: permisos ya guardados)
    "desafio": "🏫 Desafío Recreo",
    "historial": "📜 Historial de Cambios",
    "configuracion": "⚙️ Configuración",
}
PAGINAS = list(PAGINAS_POR_CLAVE.values())
SOLO_DUENO = {"configuracion"}  # nunca se habilita a "atención"
PAGINAS_HABILITABLES = [k for k in PAGINAS_POR_CLAVE if k not in SOLO_DUENO]


# ------------------------------------------------------------
# INGRESO CON USUARIO Y CONTRASEÑA (Sprint 2)
# Quien inicia sesión es quien "atiende": queda en cada venta
# (ventas.registrado_por) y en el historial de cambios. El dueño ve todo;
# a "atención" se le habilitan pantallas desde ⚙️ Configuración.
# La sesión se recuerda en el navegador hasta las 5 de la mañana del día
# siguiente (cookie firmada, ver abajo): recargar la página no obliga a
# volver a entrar.
# ------------------------------------------------------------
MAX_INTENTOS, SEGUNDOS_BLOQUEO = 5, 60

# ---- Sesión recordada en el navegador (tanda 10) ----------------------------
# Cookie "almacen_sesion" = persona.vence.firma. La firma es un HMAC con una
# clave derivada de la contraseña de la base (no hay que configurar nada; se
# puede fijar otra con SESION_SECRETO en los secrets) e incluye una huella de
# la contraseña de la persona: si la cambia o la desactivan, la cookie deja
# de valer. Vence a las 5 de la mañana: cada día hay que entrar una vez.
COOKIE_SESION = "almacen_sesion"
HORA_VENCE_SESION = 5


def _clave_firma():
    try:
        base = st.secrets.get("SESION_SECRETO")
    except Exception:
        base = None
    base = base or db.engine.url.password or db.engine.url.render_as_string(hide_password=False)
    return hashlib.sha256(f"almacen-sesion|{base}".encode("utf-8")).digest()


def _firma_sesion(persona_id, vence, huella):
    return hmac.new(_clave_firma(), f"{persona_id}.{vence}.{huella}".encode("utf-8"),
                    hashlib.sha256).hexdigest()[:40]


def emitir_cookie_sesion(persona_id):
    """Valor de la cookie y su vencimiento (epoch UTC), o None si no se puede."""
    huella = db.huella_sesion(persona_id)
    if not huella:
        return None
    ahora = datetime.now(ZONA_ARG)
    vence_dt = (ahora + timedelta(days=1)).replace(hour=HORA_VENCE_SESION, minute=0, second=0, microsecond=0)
    if ahora.hour < HORA_VENCE_SESION:  # entró de madrugada (antes de las 5): vence a las 5 de hoy
        vence_dt = ahora.replace(hour=HORA_VENCE_SESION, minute=0, second=0, microsecond=0)
    vence = int(vence_dt.timestamp())
    return f"{persona_id}.{vence}.{_firma_sesion(persona_id, vence, huella)}", vence


def persona_de_cookie(valor):
    """persona_id si la cookie es válida, vigente y de una persona activa; si no, None."""
    try:
        pid_txt, vence_txt, firma = (valor or "").split(".")
        pid, vence = int(pid_txt), int(vence_txt)
    except ValueError:
        return None
    if vence < time.time():
        return None
    huella = db.huella_sesion(pid)
    if not huella or not hmac.compare_digest(firma, _firma_sesion(pid, vence, huella)):
        return None
    return pid


def _js_cookie(valor, vence=None):
    """<script> que guarda (o borra, con valor None) la cookie en el navegador."""
    if valor is None:
        asignacion = f"{COOKIE_SESION}=; path=/; max-age=0; SameSite=Lax"
    else:
        expira = datetime.fromtimestamp(vence, tz=ZoneInfo("UTC")).strftime("%a, %d %b %Y %H:%M:%S GMT")
        asignacion = f"{COOKIE_SESION}={valor}; path=/; expires={expira}; SameSite=Lax"
    return ("<script>document.cookie = " + json.dumps(asignacion)
            + " + (location.protocol === 'https:' ? '; Secure' : '');</script>")


def escribir_cookie_pendiente():
    """Si hay una cookie para guardar o borrar, la escribe (va en la barra lateral, no ocupa lugar)."""
    pendiente = st.session_state.pop("_cookie_pendiente", None)
    if pendiente is not None:
        st.html(_js_cookie(*pendiente) if pendiente[0] else _js_cookie(None), unsafe_allow_javascript=True)


def elegir_descuentos_suma_a_mano(renglones, clave):
    """
    renglones: [(articulo_id, cantidad, nombre)] de una factura a punto de guardarse.
    Si en los últimos días se subió a mano el stock de alguno con una cantidad
    parecida, avisa y deja tildar "era esta mercadería". Devuelve {índice: cantidad
    a descontar al guardar}. Caso real (10/2026): Cremoso La Pampa y lácteos La
    Paulina sumados a mano al recibirlos y otra vez al cargar la factura.
    """
    sumas = db.sumas_a_mano_recientes([r[0] for r in renglones])
    candidatos = []
    for idx, (art_id, cantidad, nombre) in enumerate(renglones):
        previa = db.posible_suma_duplicada(cantidad, sumas.get(int(art_id)))
        if previa:
            candidatos.append((idx, art_id, cantidad, nombre, previa))
    if not candidatos:
        return {}
    st.warning(
        "✋ **Esto ya se sumó a mano.** En los últimos días se subió el stock a mano de estos artículos. "
        "Si fue esta misma mercadería, tildá el renglón: al guardar se descuenta esa suma y el stock queda "
        "contado una sola vez. Si fue un conteo, dejalo sin tildar."
    )
    elegidos = {}
    for idx, art_id, cantidad, nombre, previa in candidatos:
        igual = abs(previa["suma"] - float(cantidad)) <= 0.01 * float(cantidad) + 1e-9
        if st.checkbox(
            f"{nombre}: se sumaron a mano {previa['suma']:g} el {pd.to_datetime(previa['fecha']):%d/%m}. "
            "Era esta mercadería: descontarlo al guardar",
            value=igual, key=f"descontar_mano_{clave}_{idx}_{art_id}_{previa['suma']}",
        ):
            elegidos[idx] = previa["suma"]
    return elegidos


@st.cache_data(ttl=600, show_spinner=False)
def cantidades_habituales_cache(ids):
    """Cuánto se vende normalmente por renglón de cada artículo (cambia poco: 10 minutos)."""
    return db.cantidades_habituales(ids)


def minutos_y_segundos(segundos):
    segundos = int(segundos)
    if segundos < 60:
        return f"{segundos} segundos"
    m, r = divmod(segundos, 60)
    return f"{m} minuto{'s' if m > 1 else ''}" + (f" y {r} segundos" if r else "")


def pantalla_ingreso():
    st.title("🔐 Almacén — Ingresar")
    bloqueado_hasta = st.session_state.get("login_bloqueado_hasta", 0)
    with st.form("form_ingreso"):
        usuario = st.text_input("Usuario", key="login_usuario")
        clave = st.text_input("Contraseña", type="password", key="login_clave")
        entrar = st.form_submit_button("Entrar", type="primary", width="stretch")
    if entrar:
        # Tanda 12: el bloqueo se guarda en la base por usuario (recargar la página
        # ya no lo saltea). Si la base no tiene la tabla, queda el de la sesión.
        espera = db.segundos_bloqueo(usuario)
        if espera is None:
            espera = max(0, int(bloqueado_hasta - time.time()) + 1) if time.time() < bloqueado_hasta else 0
        if espera:
            st.error(f"Demasiados intentos con ese usuario. Esperá {minutos_y_segundos(espera)} y probá de nuevo.")
        else:
            persona = db.autenticar(usuario, clave)
            if persona:
                db.registrar_ingreso_correcto(usuario)
                st.session_state["persona_id"] = persona["id"]
                st.session_state["login_intentos"] = 0
                st.session_state.pop("_sin_cookie", None)
                st.session_state["_cookie_pendiente"] = emitir_cookie_sesion(persona["id"]) or (None,)
                st.rerun()
            bloqueo = db.registrar_intento_fallido(usuario)
            if bloqueo is None:  # base sin la tabla: contador de la sesión, como antes
                intentos = st.session_state.get("login_intentos", 0) + 1
                st.session_state["login_intentos"] = intentos
                if intentos >= MAX_INTENTOS:
                    st.session_state["login_bloqueado_hasta"] = time.time() + SEGUNDOS_BLOQUEO
                    st.session_state["login_intentos"] = 0
                    bloqueo = SEGUNDOS_BLOQUEO
            if bloqueo:
                st.error(f"Usuario o contraseña incorrectos. Por seguridad, ese usuario queda bloqueado "
                         f"{minutos_y_segundos(bloqueo)}.")
            else:
                st.error("Usuario o contraseña incorrectos.")
    st.caption("¿Te olvidaste la contraseña o la querés cambiar? Pedíselo al dueño.")
    escribir_cookie_pendiente()  # al salir: borra la sesión recordada
    st.stop()


def cerrar_sesion():
    for k in ("persona_id", "pagina_nav", "carrito_venta"):
        st.session_state.pop(k, None)
    # la cookie del navegador se borra en la próxima corrida; hasta recargar,
    # esta sesión no la vuelve a usar (st.context.cookies es la del ingreso)
    st.session_state["_sin_cookie"] = True
    st.session_state["_cookie_pendiente"] = (None,)


if not st.session_state.get("persona_id") and not st.session_state.get("_sin_cookie"):
    _pid_cookie = persona_de_cookie(st.context.cookies.get(COOKIE_SESION))
    if _pid_cookie:
        st.session_state["persona_id"] = _pid_cookie
if not st.session_state.get("persona_id"):
    pantalla_ingreso()

PERSONA = db.obtener_persona(st.session_state["persona_id"])
if not PERSONA or not PERSONA["activo"]:
    cerrar_sesion()
    st.rerun()
db.set_persona_actual(PERSONA["id"], PERSONA["nombre"])
ES_DUENO = PERSONA["rol"] == "dueño"
PAGINAS_PERMITIDAS = [
    nombre for clave, nombre in PAGINAS_POR_CLAVE.items()
    if ES_DUENO or (clave in PERSONA["permisos"] and clave not in SOLO_DUENO)
]

# Quién atiende + Salir, en una línea (el menú de pantallas va arriba).
with st.sidebar:
    _u1, _u2 = st.columns([3, 2], vertical_alignment="center")
    _u1.markdown(f"👤 **{PERSONA['nombre']}** · {db.ROLES_PERSONA[PERSONA['rol']]}")
    _u2.button("Salir", on_click=cerrar_sesion, key="btn_salir", width="stretch")
    escribir_cookie_pendiente()

if not PAGINAS_PERMITIDAS:
    st.warning("Todavía no tenés ninguna pantalla habilitada. Pedile al dueño que te habilite las que necesites.")
    st.stop()

# Menú agrupado por secciones (st.navigation). Cada pantalla tiene su propia
# dirección (…/venta, …/caja): al recargar se queda en la misma, y se puede
# guardar en favoritos. Las pantallas siguen siendo los bloques
# `if pagina == ...` de más abajo: el menú solo decide cuál se dibuja.
SECCIONES_MENU = {
    "Mostrador": ["registrar_venta", "cierre_caja"],
    "Resumen y análisis": ["dashboard", "reportes", "pronostico"],
    "Compras": ["cargar_factura", "reposicion", "precios_proveedor"],
    "Stock": ["articulos", "fraccionar", "promociones", "desafio"],
    "Administración": ["gastos", "historial", "configuracion"],
}
URL_PAGINA = {
    "dashboard": "tablero", "articulos": "articulos", "cargar_factura": "factura", "fraccionar": "fraccionar",
    "promociones": "promociones", "registrar_venta": "venta", "gastos": "gastos", "reportes": "reportes",
    "cierre_caja": "caja", "reposicion": "lista-de-compras", "pronostico": "pronostico",
    "precios_proveedor": "proveedores", "historial": "historial", "configuracion": "configuracion",
    "desafio": "desafio",
}


def _pantalla_vacia():
    # El contenido de cada pantalla se dibuja más abajo según `pagina`.
    pass


CLAVE_POR_NOMBRE = {v: k for k, v in PAGINAS_POR_CLAVE.items()}
_defecto = "dashboard" if PAGINAS_POR_CLAVE["dashboard"] in PAGINAS_PERMITIDAS else CLAVE_POR_NOMBRE[PAGINAS_PERMITIDAS[0]]
OBJ_PAGINA = {}
menu = {}
for _seccion, _claves in SECCIONES_MENU.items():
    _paginas = []
    for _c in _claves:
        if PAGINAS_POR_CLAVE[_c] not in PAGINAS_PERMITIDAS:
            continue
        _icono, _titulo = PAGINAS_POR_CLAVE[_c].split(" ", 1)
        OBJ_PAGINA[_c] = st.Page(_pantalla_vacia, title=_titulo, icon=_icono, url_path=URL_PAGINA[_c],
                                 default=(_c == _defecto))
        _paginas.append(OBJ_PAGINA[_c])
    if _paginas:
        menu[_seccion] = _paginas
# Atención con una o dos pantallas: sin títulos de sección (no aportan nada)
navegacion = st.navigation(menu if len(OBJ_PAGINA) > 3 else [p for ps in menu.values() for p in ps], expanded=True)

# Pedido de ir a otra pantalla (botones "Ver detalle", "Ir a la lista de
# compras"; también lo usan las pruebas): se pone el nombre de la pantalla en
# st.session_state["pagina_nav"] y acá se cambia.
_pedida = st.session_state.pop("pagina_nav", None)
if _pedida in CLAVE_POR_NOMBRE and CLAVE_POR_NOMBRE[_pedida] in OBJ_PAGINA \
        and OBJ_PAGINA[CLAVE_POR_NOMBRE[_pedida]].url_path != navegacion.url_path:
    st.switch_page(OBJ_PAGINA[CLAVE_POR_NOMBRE[_pedida]])
navegacion.run()
pagina = next(PAGINAS_POR_CLAVE[c] for c, p in OBJ_PAGINA.items() if p.url_path == navegacion.url_path)
mostrar_avisos()


# ------------------------------------------------------------
# PRONÓSTICO DE DEMANDA (Sprint 2, HU-014) -- lógica en pronostico.py
# La evaluación de modelos (backtest) se recalcula cada 6 h y el
# pronóstico cada 1 h; ambos se calculan en pandas a partir de pocas
# consultas, no una por artículo.
# ------------------------------------------------------------
@st.cache_data(ttl=6 * 3600, show_spinner="Evaluando modelos de pronóstico...")
def evaluacion_pronostico(hoy_txt, version=None):
    return pron.evaluar(db.engine, hoy=hoy_txt)


@st.cache_data(ttl=3600, show_spinner="Calculando pronóstico...")
def pronostico_cacheado(hoy_txt, eleccion_items, horizonte=14, version=None):
    return pron.pronostico_vigente(db.engine, horizonte=horizonte, hoy=hoy_txt, eleccion=dict(eleccion_items))


@st.cache_data(ttl=3600, show_spinner="Calculando pronóstico...")
def pronostico_para_comprar(hoy_txt, eleccion_items, horizonte=14, version=None):
    """El pronóstico con la clase que usan las decisiones de compra (tanda 20):
    cada componente de un combo toma la mejor clase entre la suya y la de los
    combos que integra (lista_compras.heredar_clase_combos). La elección del
    modelo sigue por la clase propia."""
    df = pronostico_cacheado(hoy_txt, eleccion_items, horizonte, version=version)
    ayer = (pd.Timestamp(hoy_txt) - pd.Timedelta(days=1)).date()
    return lc.heredar_clase_combos(df, db.clase_combos(ayer, dias=pron.VENTANA_ABC))


@st.cache_data(ttl=3600, show_spinner=False)
def demanda_historica(hoy_txt, version=None):
    Y, _, articulos = pron.preparar(db.engine, hoy_txt)
    return Y, articulos


@st.cache_data(ttl=3600, show_spinner="Comparando pronósticos con lo vendido...")
def seguimiento_cacheado(hoy_txt, eleccion_items, dias=28, version=None):
    """Pronóstico de cada día pasado vs lo vendido. Donde hay pronóstico
    GUARDADO ese día (pronostico_snapshot) se usa ese; si no, el
    reconstruido con los datos hasta el día anterior."""
    rec = pron.reconstruir(db.engine, hoy=hoy_txt, dias=dias, eleccion=dict(eleccion_items))
    if rec.empty:
        return rec.assign(fuente=pd.Series(dtype=str))
    guard = db.pronosticos_guardados(rec["fecha"].min())
    guard = guard.rename(columns={"fecha_objetivo": "fecha", "demanda": "guardado"})[["fecha", "articulo_id", "guardado"]]
    guard["fecha"] = pd.to_datetime(guard["fecha"]).dt.date
    m = rec.merge(guard, on=["fecha", "articulo_id"], how="left")
    m["fuente"] = np.where(m["guardado"].notna(), "guardado", "reconstruido")
    m["pronostico"] = m["guardado"].fillna(m["pronostico"])
    return m.drop(columns=["guardado"])


def asegurar_snapshot_pronostico():
    """Guarda una vez por día el pronóstico vigente (pronostico_snapshot)
    para medir después su error real en la validación con el comerciante. Se dispara
    con la primera sesión del dueño del día; si falla, no interrumpe la app."""
    if not ES_DUENO or st.session_state.get("snapshot_pronostico_ok"):
        return
    hoy_txt = str(hoy_argentina())
    try:
        if not db.existe_snapshot_pronostico(hoy_txt):
            ev = evaluacion_pronostico(hoy_txt, version=pron.VERSION_DATOS)
            df = pronostico_cacheado(hoy_txt, tuple(sorted(ev["eleccion"].items())), version=pron.VERSION_DATOS)
            db.guardar_pronostico_snapshot(df, hoy_txt)
        st.session_state["snapshot_pronostico_ok"] = True
    except Exception as e:  # el pronóstico nunca debe trabar la operación diaria
        st.session_state["snapshot_pronostico_ok"] = True
        st.sidebar.caption(f"⚠️ No se pudo guardar el pronóstico de hoy ({type(e).__name__}).")


asegurar_snapshot_pronostico()

# Solo el dueño cambia contraseñas (la propia acá, y la de cualquiera en
# Configuración). Atención no puede cambiar la suya: se la asigna el dueño.
if ES_DUENO:
    with st.sidebar.expander("🔑 Cambiar mi contraseña"):
        with st.form("form_mi_clave", clear_on_submit=True):
            actual = st.text_input("Contraseña actual", type="password")
            nueva = st.text_input("Nueva (mínimo 8 caracteres)", type="password")
            repetida = st.text_input("Repetir nueva", type="password")
            if st.form_submit_button("Cambiar"):
                if not db.autenticar(PERSONA["usuario"], actual):
                    st.error("La contraseña actual no es correcta.")
                elif nueva != repetida:
                    st.error("Las contraseñas nuevas no coinciden.")
                else:
                    try:
                        db.cambiar_clave(PERSONA["id"], nueva)
                        # la sesión recordada vieja dejó de valer: se guarda una nueva
                        st.session_state["_cookie_pendiente"] = emitir_cookie_sesion(PERSONA["id"]) or (None,)
                        st.success("Contraseña cambiada.")
                    except ValueError as e:
                        st.error(str(e))


def ir_a_lista_compras():
    # Callback del botón de la tarjeta de alertas de quiebre (HU-016).
    if PAGINAS_POR_CLAVE["reposicion"] in PAGINAS_PERMITIDAS:
        st.session_state["pagina_nav"] = PAGINAS_POR_CLAVE["reposicion"]


@st.cache_data(ttl=900, show_spinner="Revisando qué se puede quedar sin stock...")
def alertas_quiebre_cacheadas(hoy_txt, version=None):
    """Semáforo de quiebre (HU-016). Se recalcula cada 15 min: el pronóstico
    cambia poco en el día, pero el stock cambia con cada venta/factura."""
    hoy_d = pd.Timestamp(hoy_txt).date()
    ev = evaluacion_pronostico(hoy_txt, version=version)
    df = pronostico_para_comprar(hoy_txt, tuple(sorted(ev["eleccion"].items())), version=version)
    params = db.obtener_parametros()
    ultimo = lc.ultimo_viaje(db.engine)
    prox = lc.proximo_viaje_estimado(ultimo, hoy_d, params.get("frecuencia_viaje_dias", 7))
    # misma dispersión que la lista de compras (tanda 18): binomial negativa en vez de Poisson
    dispersion = dict(zip(ev["sigma"]["clase"], ev["sigma"]["dispersion"])) if ev["evaluada"] else {}
    tabla, resumen = lc.alertas_quiebre(df, lc.datos_articulos(db.engine), params, hoy_d, prox, dispersion)
    resumen["proximo_viaje"] = prox
    resumen["ultimo_viaje"] = ultimo
    return tabla, resumen


PESTANIAS_REPORTES = ["💰 Ganancias y Gastos", "📊 Clasificación ABC", "📐 Márgenes reales", "🧪 Calidad de datos"]


def ir_a_reportes(pestania=None):
    # Callback de los botones "Ver detalle" del Dashboard: abre Reportes en la
    # pestaña que corresponde (antes caía siempre en la primera).
    if PAGINAS_POR_CLAVE["reportes"] in PAGINAS_PERMITIDAS:
        st.session_state["pagina_nav"] = PAGINAS_POR_CLAVE["reportes"]
        st.session_state["reportes_pestania"] = pestania

ICONO_ESTADO_CALIDAD = {"verde": "🟢", "amarillo": "🟡", "rojo": "🔴"}
TEXTO_ESTADO_CALIDAD = {
    "verde": "Datos confiables",
    "amarillo": "Revisar alertas antes de sacar conclusiones",
    "rojo": "Los reportes pueden estar distorsionados",
}

DIAS_AVISO_VENCIMIENTO = 30   # se muestran los lotes que vencen en los próximos 30 días (o ya vencidos)
PROB_RIESGO_VENCIMIENTO = 0.5  # "en riesgo": es más probable que sobre algo del lote que no


@st.cache_data(ttl=600, show_spinner="Revisando vencimientos...", max_entries=20)
def vencimientos_cacheados(hoy_txt, version):
    """Tanda 18: lotes implícitos (renglones de factura) con fecha de vencimiento
    real o estimada, y su riesgo de vencer sin venderse (lista_compras.riesgo_vencimiento).
    `version` = huella de los datos (se recalcula con cada venta/factura)."""
    lotes = db.lotes_vencimiento(hoy_txt)
    if lotes is None or lotes.empty:
        return pd.DataFrame()
    ev = evaluacion_pronostico(hoy_txt, version=pron.VERSION_DATOS)
    pron_df = pronostico_para_comprar(hoy_txt, tuple(sorted(ev["eleccion"].items())), version=pron.VERSION_DATOS)
    dispersion = dict(zip(ev["sigma"]["clase"], ev["sigma"]["dispersion"])) if ev["evaluada"] else {}
    meta_v = lc.datos_articulos(db.engine)
    tam = {int(r.articulo_id): lc._tam(r._asdict()) for r in meta_v[["articulo_id", "unidad_medida", "tam_venta"]]
           .itertuples(index=False)}
    return lc.riesgo_vencimiento(lotes, pron_df, dispersion, tam)


@st.cache_data(ttl=600, show_spinner="Cargando el tablero...", max_entries=20)
def datos_dashboard(hoy_txt, version):
    """
    Todo lo que muestra el Dashboard menos las alertas de quiebre (que tienen
    su propia caché). Antes eran ~25 consultas a Neon en CADA clic; ahora se
    calcula una vez por cada estado de los datos: `version` es la huella de
    db.version_datos(), que cambia apenas cualquier sesión (otra pestaña,
    quien atiende en el mostrador) registra una venta, una factura o toca
    un artículo. ttl=600 es solo un resguardo.
    El snapshot semanal del indicador de calidad se guarda acá, una vez por
    cada estado de los datos, en lugar de en cada clic.
    """
    hoy = pd.Timestamp(hoy_txt).date()
    calidad = db.calidad_de_datos(hoy)
    try:
        anterior = db.indicador_semana_anterior()
        db.guardar_snapshot_calidad(calidad)
    except Exception:
        anterior = None  # el historial es un extra: si falla, la tarjeta igual se muestra
    activos = db.listar_articulos()
    bajo = db.alertas_stock_bajo()
    return {
        "articulos_activos": len(activos),
        # "stock bajo" = por debajo del mínimo CARGADO: con mínimo 0 (casi todos)
        # era lo mismo que "sin stock", que ya cubren las Alertas de quiebre
        "stock_bajo": bajo[bajo["stock_minimo"].astype(float) > 0].reset_index(drop=True),
        "con_minimo": int((activos["stock_minimo"].astype(float) > 0).sum()),
        "con_vencimiento": int(activos["fecha_vencimiento"].notna().sum()),
        "vencimientos": db.alertas_vencimiento(),
        "stock_negativo": db.articulos_stock_negativo(),
        "calidad": calidad,
        "anterior": anterior,
        "margen_mes": db.resumen_margenes(hoy.replace(day=1), hoy),
        "stock_valor": db.valor_stock(),
        "variaciones": db.variaciones_de_precio_relevantes(umbral_pct=5),
    }


# ============================================================
# DASHBOARD
# ============================================================
if pagina == "📊 Dashboard":
    st.title("📊 Dashboard")

    hoy = hoy_argentina()
    tablero = datos_dashboard(str(hoy), db.version_datos())
    stock_bajo = tablero["stock_bajo"]
    vencimientos = tablero["vencimientos"]
    stock_negativo = tablero["stock_negativo"]
    # Tanda 18: vencimientos por lote implícito (reemplaza la fecha única por artículo)
    lotes_v = vencimientos_cacheados(str(hoy), db.version_datos())
    if not lotes_v.empty:
        lotes_ventana = lotes_v[lotes_v["dias_para_vencer"].notna()
                                & (lotes_v["dias_para_vencer"] <= DIAS_AVISO_VENCIMIENTO)].copy()
        en_riesgo_v = lotes_ventana[(lotes_ventana["dias_para_vencer"] < 0)
                                    | (lotes_ventana["prob_sobra"] >= PROB_RIESGO_VENCIMIENTO)]
    else:
        lotes_ventana = en_riesgo_v = lotes_v

    # Calidad de datos: va primero porque dice cuánto se puede confiar en
    # el resto de los números de esta pantalla. El detalle está en
    # Reportes -> Calidad de datos.
    calidad = tablero["calidad"]
    anterior = tablero["anterior"]
    # Ganancia del mes en curso: "¿cuánto te dejó lo que vendiste?".
    # Detalle en Reportes -> Márgenes reales.
    margen_mes = tablero["margen_mes"]

    def pesos_dash(v, signo=False):
        txt = f"${abs(v):,.0f}".replace(",", ".")
        if signo:
            return ("+" if v > 0 else "−" if v < 0 else "") + txt
        return ("−" if v < 0 else "") + txt

    # Las dos tarjetas analíticas, lado a lado: primero "¿puedo confiar en
    # los datos?", después "¿estoy ganando lo que creo?".
    card_cal, card_mar = st.columns(2)
    with card_cal.container(border=True):
        st.metric(
            "🧪 Calidad de datos",
            f"{calidad['indicador']:.0f}%",
            None if anterior is None else f"{calidad['indicador'] - anterior:+.1f} pts vs semana anterior",
            help="% de artículos sin ninguna alerta de calidad de datos.",
        )
        st.markdown(
            f"**{ICONO_ESTADO_CALIDAD[calidad['estado']]} {TEXTO_ESTADO_CALIDAD[calidad['estado']]}**  \n"
            f"{calidad['articulos_con_alerta']} de {calidad['articulos_activos']} artículos con alguna alerta."
            + (f"  \n⛔ {calidad['articulos_inactivos']} inactivos sin stock, fuera del indicador."
               if calidad["articulos_inactivos"] else "")
        )
        st.button("Ver detalle →", on_click=ir_a_reportes, args=("🧪 Calidad de datos",), key="ir_calidad",
                  help="Abre Reportes → pestaña 🧪 Calidad de datos")

    with card_mar.container(border=True):
        if margen_mes is None:
            st.metric("💰 Ganancia del mes", "—")
            st.markdown("Todavía no hay ventas este mes.")
        else:
            margen_txt = "—" if margen_mes["margen"] is None else f"{margen_mes['margen'] * 100:.1f}%".replace(".", ",")
            st.metric(
                "💰 Ganancia del mes",
                pesos_dash(margen_mes["ganancia_real"]),
                help="Lo cobrado (sin IVA) menos lo que te costó la mercadería vendida, en el mes en curso. "
                     "Sin consumo interno.",
            )
            linea = (f"Margen promedio: **{margen_txt}** sobre "
                     f"{pesos_dash(margen_mes['ingreso_neto'])} vendidos.  \n"
                     f"Lo que más te dejó: **{margen_mes['mejor_articulo']}** "
                     f"({pesos_dash(margen_mes['mejor_ganancia'])}).")
            if margen_mes["cantidad_dejan_poco"]:
                linea += f"  \n🔍 {margen_mes['cantidad_dejan_poco']} producto(s) venden mucho pero dejan poco."
            st.markdown(linea.replace("$", "\\$"))
            if abs(margen_mes["dif_precio"]) >= db.UMBRAL_PRECIO:
                st.caption(("⚠️ Hay ventas cobradas distinto del precio del sistema "
                            f"({pesos_dash(margen_mes['dif_precio'], signo=True)}).").replace("$", "\\$"))
            if calidad["estado"] != "verde":
                st.caption("⚠️ Con la calidad de datos en " + calidad["estado"] +
                           ", estos números pueden no ser exactos (por ejemplo, artículos sin costo cargado).")
        st.button("Ver detalle →", on_click=ir_a_reportes, args=("📐 Márgenes reales",), key="ir_margenes",
                  help="Abre Reportes → pestaña 📐 Márgenes reales")

    # Alertas de quiebre (HU-016): qué se queda sin stock antes del próximo
    # viaje al mayorista, según el pronóstico de demanda. Si el cálculo
    # falla, el resto del Dashboard sigue funcionando.
    try:
        quiebre, rq = alertas_quiebre_cacheadas(str(hoy), version=pron.VERSION_DATOS)
    except Exception as e:
        quiebre, rq = None, None
        st.caption(f"⚠️ No se pudieron calcular las alertas de quiebre ({type(e).__name__}).")
    if rq is not None:
        with st.container(border=True):
            prox_txt = pd.Timestamp(rq["proximo_viaje"]).strftime("%d/%m")
            dias_txt = ("hoy" if rq["dias_viaje"] == 0 else "mañana" if rq["dias_viaje"] == 1
                        else f"en {rq['dias_viaje']} días")
            if rq["rojo"]:
                titulo = (f"🔴 {rq['rojo']} artículo(s) se quedan sin stock antes del próximo viaje "
                          f"({prox_txt}, {dias_txt})")
                if rq["rojo_A"]:
                    titulo += f" — {rq['rojo_A']} de clase A"
            else:
                titulo = f"🟢 Nada se queda sin stock antes del próximo viaje ({prox_txt}, {dias_txt})"
            st.markdown(f"#### 🚨 Alertas de quiebre  \n**{titulo}**")
            partes = []
            if rq["sin_stock"]:
                partes.append(f"{rq['sin_stock']} ya sin stock")
            if rq["riesgo_$"] >= 1:
                partes.append(f"venta en riesgo ≈ {pesos_dash(rq['riesgo_$'])}")
            if rq["amarillo"]:
                partes.append(f"🟡 {rq['amarillo']} llegan al viaje pero hay que comprarlos en él")
            if partes:
                st.markdown(" · ".join(partes).replace("$", "\\$"))
            if rq["adelantar"]:
                st.warning(f"⏩ **Conviene adelantar el viaje:** {rq['rojo_A']} artículos de clase A "
                           "(los que más venden) se agotan antes.")
            if rq["stock_negativo"]:
                st.caption(f"{rq['stock_negativo']} artículo(s) con stock negativo no se evalúan: "
                           "primero hay que corregir su stock (Pendientes de ajuste).")
            st.caption("El próximo viaje se estima como la última compra al mayorista/distribuidor "
                       f"({pd.Timestamp(rq['ultimo_viaje']).strftime('%d/%m') if rq['ultimo_viaje'] is not None and pd.notna(rq['ultimo_viaje']) else 'sin datos'}) "
                       "más la frecuencia de viaje de Configuración. El pan y lo de compra diaria no entra acá.")
            if quiebre is not None and not quiebre.empty and (rq["rojo"] or rq["amarillo"]):
                with st.expander("Ver artículos en riesgo"):
                    icono = {"rojo": "🔴", "amarillo": "🟡", "verde": "🟢"}
                    ver = quiebre[quiebre["estado"] != "verde"].copy()
                    ver.insert(0, "", ver["estado"].map(icono))
                    ver["Prob. de agotarse antes del viaje"] = (ver["Prob. de agotarse antes del viaje"] * 100).round(0)
                    st.dataframe(
                        ver[["", "Artículo", "Clase", "Stock", "Venta por día", "Se queda sin stock",
                             "Prob. de agotarse antes del viaje", "Proveedor"]],
                        hide_index=True, width="stretch",
                        column_config={
                            "Stock": st.column_config.NumberColumn(format="%.0f"),
                            "Venta por día": st.column_config.NumberColumn(format="%.1f",
                                help="Venta esperada por día en la próxima semana (pronóstico)."),
                            "Se queda sin stock": st.column_config.DateColumn(format="DD/MM",
                                help="Primer día en que es más probable haberse quedado sin stock que no."),
                            "Prob. de agotarse antes del viaje": st.column_config.ProgressColumn(
                                "Prob. de agotarse antes del viaje", format="%.0f%%", min_value=0, max_value=100),
                        },
                    )
                    st.caption("🔴 se agota antes del próximo viaje · 🟡 llega al viaje pero no alcanza "
                               "hasta el siguiente: tiene que ir en la lista.")
            if PAGINAS_POR_CLAVE["reposicion"] in PAGINAS_PERMITIDAS:
                st.button("Ir a la Lista de compras →", on_click=ir_a_lista_compras, key="ir_lista_compras")

    # Valor de la mercadería: foto de hoy (no depende de fechas), por eso va
    # acá y no en Ganancias y Gastos.
    stock_valor = tablero["stock_valor"]
    col0, col1, col2, col3, col4 = st.columns(5)
    col0.metric(
        "📦 Mercadería", pesos_dash(stock_valor["valor_costo"]),
        help="Plata invertida en mercadería hoy: stock × costo de compra. A precio de venta serían "
             f"{pesos_dash(stock_valor['valor_venta'])} (ganancia potencial "
             f"{pesos_dash(stock_valor['ganancia_potencial'])} sin IVA). Sin combos virtuales; "
             "el stock negativo cuenta como 0.",
    )
    col1.metric("Artículos activos", tablero["articulos_activos"])
    col2.metric("⚠️ Debajo del mínimo", len(stock_bajo),
                help=f"Artículos con stock mínimo cargado ({tablero['con_minimo']}) que están por debajo. "
                     "Los demás los avisan las Alertas de quiebre, según lo que se espera vender.")
    if not lotes_v.empty:
        col3.metric("⏰ En riesgo de vencer", int(en_riesgo_v["articulo_id"].nunique()),
                    help="Artículos con alguna compra que, según su fecha de vencimiento (real o estimada) y lo que "
                         "se espera vender, es más probable que venza sin venderse que no (o que ya venció).")
    else:
        col3.metric("⏰ En riesgo de vencer", "—",
                    help="Todavía no hay días disponibles para la venta cargados (⚙️ Configuración, por categoría, "
                         "o 🛒 Lista de compras, por artículo) ni fechas en las facturas.")
    col4.metric("📥 Pendientes de ajuste", len(stock_negativo),
                help="Artículos con stock negativo: se vendió más de lo que figura comprado.")
    if stock_valor["sin_costo"]:
        st.caption(f"⚠️ {stock_valor['sin_costo']} artículo(s) con stock no tienen costo cargado: "
                   "el valor real de la mercadería es mayor al que se muestra.")
    if stock_valor.get("valor_consignacion", 0) > 0:
        st.caption(f"🤝 Además hay {md_pesos(stock_valor['valor_consignacion'])} a costo en mercadería en "
                   "consignación: está en el local pero se paga cuando se vende (no es plata invertida).")

    st.divider()
    variaciones = tablero["variaciones"]
    nombres_tabs = [f"📥 Pendientes de ajuste ({len(stock_negativo)})",
                    f"📉 Variaciones de precio ({len(variaciones)})",
                    f"⚠️ Debajo del mínimo ({len(stock_bajo)})"]
    if not lotes_v.empty:
        nombres_tabs.append(f"⏰ Vencimientos ({int(en_riesgo_v['articulo_id'].nunique())})")
    pestanias = st.tabs(nombres_tabs)

    with pestanias[0]:
        st.caption(
            "Se vendió más de lo que figuraba comprado: se ajusta solo al cargar la factura de compra "
            "que falta, o contando y corrigiendo el stock en Artículos → Editar artículo."
        )
        if stock_negativo.empty:
            st.success("No hay artículos pendientes de ajuste.")
        else:
            st.dataframe(
                stock_negativo[["nombre", "marca", "stock_actual", "unidad_medida"]].fillna({"marca": ""}),
                column_config={"nombre": "Artículo", "marca": "Marca",
                               "stock_actual": st.column_config.NumberColumn("Stock en sistema", format="%g"),
                               "unidad_medida": "Unidad"},
                hide_index=True, width="stretch"
            )

    with pestanias[1]:
        if variaciones.empty:
            st.info("Todavía no hay suficiente historial de compras para detectar variaciones.")
        else:
            # Sin colores de fondo: con el tema oscuro, el texto blanco sobre fondo
            # pastel no se leía. La suba/baja se indica con flecha y texto.
            tabla_var = variaciones.sort_values("fecha", ascending=False).copy()
            tabla_var["cambio"] = tabla_var["variacion_pct"].map(lambda v: "🔺 Subió" if v > 0 else "🔻 Bajó")
            st.dataframe(
                tabla_var[["articulo", "proveedor", "fecha", "cambio", "costo_anterior", "costo_unitario", "variacion_pct"]],
                column_config={
                    "articulo": "Artículo", "proveedor": "Proveedor",
                    "fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
                    "cambio": "Cambio",
                    "costo_anterior": st.column_config.NumberColumn("Costo anterior ($)", format=FMT_PESOS),
                    "costo_unitario": st.column_config.NumberColumn("Costo nuevo ($)", format=FMT_PESOS),
                    "variacion_pct": st.column_config.NumberColumn("Variación", format="%+.1f%%"),
                },
                hide_index=True, width="stretch"
            )
            st.caption(
                "Variaciones de 5 % o más: compara cada compra con la anterior del mismo artículo al mismo "
                "proveedor. Si dos artículos 'intercambian' el mismo costo, suele ser un renglón de factura "
                "cargado en el artículo equivocado."
            )

    with pestanias[2]:
        if not tablero["con_minimo"]:
            st.info("Ningún artículo tiene stock mínimo cargado. Si querés un aviso fijo para algún producto, "
                    "cargale el mínimo en Artículos → Editar artículo. Lo que se puede agotar ya lo avisan "
                    "las Alertas de quiebre de arriba.")
        elif stock_bajo.empty:
            st.success(f"Los {tablero['con_minimo']} artículos con mínimo cargado están por encima.")
        else:
            st.dataframe(
                stock_bajo[["nombre", "marca", "stock_actual", "stock_minimo"]].fillna({"marca": ""}),
                column_config={"nombre": "Artículo", "marca": "Marca",
                               "stock_actual": st.column_config.NumberColumn("Stock", format="%g"),
                               "stock_minimo": st.column_config.NumberColumn("Mínimo", format="%g")},
                hide_index=True, width="stretch"
            )

    if not lotes_v.empty:
        with pestanias[3]:
            st.caption(
                "Cada compra (renglón de factura) es un lote. Vence en la fecha cargada en la factura o, si no, en la "
                "fecha de compra + los días disponibles para la venta del artículo (o de su categoría). Se supone que "
                "se vende primero lo más viejo. Con lo que se espera vender, se calcula la probabilidad de que quede "
                "algo sin vender cuando vence y cuántas unidades se tirarían."
            )
            mes_v = hoy.replace(day=1)
            merma_v = db.merma_vencido(mes_v, hoy)
            sin_fecha_v = lotes_v[lotes_v["fuente"] == "sin fecha"]
            c1v, c2v, c3v = st.columns(3)
            c1v.metric("Se tirarían (próx. 30 días), a costo",
                       md_pesos(float(lotes_ventana["vencerian_costo"].fillna(0).sum())),
                       help="Valor esperado: suma, en cada lote, de las unidades que se espera que venzan sin venderse.")
            c2v.metric("Merma por vencido en el mes", md_pesos(float(merma_v["costo"].sum())) if not merma_v.empty
                       else md_pesos(0.0),
                       help="Lo cargado como 'Vencido' en 💵 Registrar Venta → 🗑️ Sobrante del día.")
            c3v.metric("Stock sin fecha", int(sin_fecha_v["articulo_id"].nunique()),
                       help="Artículos con vencimiento cuyo stock (o parte) no viene de una factura cargada "
                            "(ej.: la carga inicial): no se puede fechar.")
            if lotes_ventana.empty:
                st.success(f"Nada vence en los próximos {DIAS_AVISO_VENCIMIENTO} días.")
            else:
                ver_v = lotes_ventana.copy()
                ver_v["estado"] = np.where(
                    ver_v["dias_para_vencer"] < 0, "⛔ Ya venció (según la fecha)",
                    np.where(ver_v["prob_sobra"] >= PROB_RIESGO_VENCIMIENTO, "🟠 Es probable que sobre", "🟢 Se vende"))
                ver_v["fuente"] = ver_v["fuente"].map({"factura": "Factura", "estimada": "Estimada",
                                                       "artículo": "Del artículo", "góndola": "Góndola"}).fillna(ver_v["fuente"])
                ver_v["prob_sobra"] = (ver_v["prob_sobra"] * 100).round(0)
                ver_v = ver_v.sort_values(["dias_para_vencer", "vencerian_costo"], ascending=[True, False])
                st.dataframe(
                    ver_v[["estado", "nombre", "fecha_compra", "quedan", "vence", "fuente", "dias_para_vencer",
                           "venta_hasta_vencer", "prob_sobra", "vencerian", "vencerian_costo"]],
                    column_config={
                        "estado": "Estado", "nombre": "Artículo",
                        "fecha_compra": st.column_config.DateColumn("Comprado", format="DD/MM/YYYY"),
                        "quedan": st.column_config.NumberColumn("Quedan", format="%g"),
                        "vence": st.column_config.DateColumn("Vence", format="DD/MM/YYYY"),
                        "fuente": "Fecha",
                        "dias_para_vencer": st.column_config.NumberColumn("Días", format="%d"),
                        "venta_hasta_vencer": st.column_config.NumberColumn("Venta esperada hasta vencer",
                                                                            format="%.1f"),
                        "prob_sobra": st.column_config.ProgressColumn("Prob. de que sobre", format="%.0f%%",
                                                                      min_value=0, max_value=100),
                        "vencerian": st.column_config.NumberColumn("Se tirarían (u.)", format="%.1f"),
                        "vencerian_costo": st.column_config.NumberColumn("A costo ($)", format=FMT_PESOS),
                    },
                    hide_index=True, width="stretch",
                )
                st.caption(
                    "⛔ Si figura vencido y no lo está, los días disponibles cargados son pocos para ese artículo: "
                    "corregilos (🛒 Lista de compras → Artículos de compra diaria y perecederos) o cargá la fecha "
                    "real en la factura. Si de verdad venció, cargalo como 'Vencido' en 💵 Registrar Venta → "
                    "🗑️ Sobrante del día. 🟠 Conviene ponerlo adelante, en promoción o no volver a comprar tanto."
                )

# ============================================================
# ARTICULOS
# ============================================================
elif pagina == "📦 Artículos":
    st.title("📦 Artículos")

    tab1, tab2, tab3 = st.tabs(["Listado", "Nuevo artículo", "Editar artículo"])

    with tab1:
        mostrar_inactivos = st.toggle(
            "Mostrar también los artículos inactivos", value=False, key="mostrar_inactivos",
            help="Los inactivos se dan de baja y se reactivan desde la pestaña 'Editar artículo'."
        )
        articulos = db.listar_articulos(incluir_inactivos=mostrar_inactivos)
        if articulos.empty:
            st.info("Todavía no hay artículos cargados. Creá el primero en la pestaña 'Nuevo artículo'.")
        else:
            c1, c2 = st.columns([2, 1])
            texto_busqueda = c1.text_input("🔍 Buscar por nombre o marca", placeholder="ej: aceite, Cocinero")
            categorias = ["Todas"] + sorted(articulos["categoria"].dropna().unique().tolist())
            categoria_filtro = c2.selectbox("Categoría", categorias)

            articulos_filtrados = articulos.copy()
            if texto_busqueda:
                coincide_nombre = articulos_filtrados["nombre"].str.contains(texto_busqueda, case=False, na=False)
                coincide_marca = articulos_filtrados["marca"].str.contains(texto_busqueda, case=False, na=False)
                articulos_filtrados = articulos_filtrados[coincide_nombre | coincide_marca]
            if categoria_filtro != "Todas":
                articulos_filtrados = articulos_filtrados[articulos_filtrados["categoria"] == categoria_filtro]

            st.caption(f"{len(articulos_filtrados)} de {len(articulos)} artículos")
            valor_filtrado = db.resumen_valor_stock(articulos_filtrados)
            v1, v2, v3 = st.columns(3)
            def _pesos_art(v):
                return ("−" if v < 0 else "") + f"${abs(v):,.0f}".replace(",", ".")
            v1.metric("📦 Invertido en stock (a costo)", _pesos_art(valor_filtrado["valor_costo"]),
                      help="Stock × costo de compra de los artículos de esta lista (respeta la búsqueda "
                           "y el filtro de categoría). Stock negativo cuenta como 0; sin combos virtuales.")
            v2.metric("A precio de venta", _pesos_art(valor_filtrado["valor_venta"]),
                      help="Lo que entraría si se vendiera todo este stock al precio actual (con IVA).")
            v3.metric("Ganancia potencial", _pesos_art(valor_filtrado["ganancia_potencial"]),
                      help="Precio de venta sin IVA menos costo, de todo este stock.")
            if valor_filtrado["sin_costo"]:
                st.caption(f"⚠️ {valor_filtrado['sin_costo']} artículo(s) de esta lista tienen stock pero "
                           "no tienen costo cargado: el valor real es mayor.")
            if valor_filtrado.get("valor_consignacion", 0) > 0:
                st.caption(f"🤝 No incluye {md_pesos(valor_filtrado['valor_consignacion'])} en consignación "
                           "(se paga cuando se vende).")
            if not mostrar_inactivos:
                inactivos_con_stock = db.resumen_valor_stock(
                    db.listar_articulos(incluir_inactivos=True).query("activo == False"))
                if inactivos_con_stock["valor_costo"] > 0:
                    st.caption(
                        f"ℹ️ No incluye {_pesos_art(inactivos_con_stock['valor_costo'])} en stock de artículos "
                        "inactivos (sí se cuentan en el Dashboard). Activá \"Mostrar también los artículos "
                        "inactivos\" para verlos.".replace("$", "\\$")
                    )
            tabla_listado = articulos_filtrados.copy()
            tabla_listado["estado"] = tabla_listado["activo"].map(
                lambda a: "✅ Activo" if a else "⛔ Inactivo")
            columnas_listado = [
                "id", "nombre", "marca", "categoria", "unidad_medida", "costo_compra_actual",
                "costo_venta_sin_iva", "costo_venta_con_iva", "porcentaje_ganancia",
                "stock_actual", "stock_minimo", "fecha_vencimiento"
            ]
            if mostrar_inactivos:
                columnas_listado = ["estado"] + columnas_listado + ["fecha_baja"]
            st.dataframe(
                tabla_listado[columnas_listado],
                column_config={
                    "estado": "Estado", "id": st.column_config.NumberColumn("N°", format="%d"),
                    "nombre": "Artículo", "marca": "Marca", "categoria": "Categoría", "unidad_medida": "Unidad",
                    "costo_compra_actual": st.column_config.NumberColumn("Costo s/IVA ($)", format=FMT_PESOS),
                    "costo_venta_sin_iva": st.column_config.NumberColumn("Precio s/IVA ($)", format=FMT_PESOS),
                    "costo_venta_con_iva": st.column_config.NumberColumn("Precio de venta ($)", format=FMT_PESOS),
                    "porcentaje_ganancia": st.column_config.NumberColumn("Ganancia sobre costo (%)", format=FMT_PESOS),
                    "stock_actual": st.column_config.NumberColumn("Stock", format="%g"),
                    "stock_minimo": st.column_config.NumberColumn("Mínimo", format="%g"),
                    "fecha_vencimiento": st.column_config.DateColumn("Vence", format="DD/MM/YYYY"),
                    "fecha_baja": st.column_config.DateColumn("Dado de baja", format="DD/MM/YYYY"),
                },
                hide_index=True, width="stretch"
            )

            st.divider()
            st.subheader("Actualizar precio / margen")
            art_sel = st.selectbox(
                "Artículo", articulos["id"],
                format_func=etiquetador(articulos),
                key="sel_actualizar_precio"
            )
            art_actual = articulos[articulos["id"] == art_sel].iloc[0]

            # Costo, margen y precio de venta final estan enlazados: cambiar
            # cualquiera de los tres recalcula uno de los otros dos.
            #   - Se edita el costo -> se recalcula el precio de venta (el margen no se toca).
            #   - Se edita el margen -> se recalcula el precio de venta (a partir del costo actual).
            #   - Se edita el precio de venta -> se recalcula el margen (a partir del costo actual).
            # El costo nunca se recalcula solo: siempre es un dato que se carga a mano.
            # Las claves llevan los valores GUARDADOS del artículo: si algo los
            # cambia por fuera de este formulario (una factura, un repricing,
            # otra pestaña), la clave cambia y los campos se recargan desde la
            # base. Antes la clave era solo el id: el formulario seguía
            # mostrando el costo viejo y "Actualizar" lo volvía a guardar.
            peso_guardado_txt = art_actual.get("peso_bolsa_gramos")
            huella_pm = (
                f"{int(art_sel)}_{float(art_actual['costo_compra_actual']):.2f}_"
                f"{float(art_actual['porcentaje_ganancia']):.6f}_"
                f"{float(peso_guardado_txt) if pd.notna(peso_guardado_txt) else 0:g}"
            )
            costo_key = f"pm_costo_{huella_pm}"
            margen_key = f"pm_margen_{huella_pm}"
            pventa_key = f"pm_pventa_{huella_pm}"
            peso_key = f"peso_bolsa_actualizar_{huella_pm}"
            for _k in [k for k in st.session_state
                       if isinstance(k, str) and k.startswith(("pm_costo_", "pm_margen_", "pm_pventa_",
                                                               "pm_conf0_", "peso_bolsa_actualizar"))
                       and not k.endswith(huella_pm)]:
                del st.session_state[_k]

            # OJO: el IVA se lee directo de la columna articulos.iva (ahora
            # que v_articulos la expone), NO se despeja dividiendo
            # costo_venta_con_iva / costo_venta_sin_iva. Esos dos valores ya
            # vienen redondeados a 2 decimales cada uno desde la vista, así
            # que esa división arrastraba un error de redondeo -- chico,
            # pero suficiente para que el precio final recalculado quedara
            # a un centavo del valor "redondo" que se quería fijar (el
            # mismo síntoma que el fix de precisión del margen, pero esta
            # era la otra mitad del problema).
            iva_actual = float(art_actual["iva"])

            def _recalcular_precio_venta(costo_key=costo_key, margen_key=margen_key, pventa_key=pventa_key, iva=iva_actual):
                costo = st.session_state.get(costo_key, 0.0)
                margen_pct = st.session_state.get(margen_key, 0.0)
                st.session_state[pventa_key] = round(costo * (1 + margen_pct / 100) * (1 + iva), 2)

            def _recalcular_margen(costo_key=costo_key, margen_key=margen_key, pventa_key=pventa_key, iva=iva_actual):
                costo = st.session_state.get(costo_key, 0.0)
                precio_venta = st.session_state.get(pventa_key, 0.0)
                if costo > 0:
                    nuevo_margen_pct = (precio_venta / (costo * (1 + iva)) - 1) * 100
                    # Redondeamos a 6 decimales (no 2) a propósito: solo para
                    # limpiar ruido de punto flotante, no para "aplanar" el
                    # margen. Si acá redondeáramos a 2 decimales como el %
                    # que se ve en pantalla, el precio recalculado a partir
                    # de ese margen "redondeado" ya no da el precio redondo
                    # que el usuario fijó (p. ej. pedís $1500 y termina
                    # guardando un margen que da $1500.04). El campo de %
                    # de ganancia se muestra igual con 2 decimales (ver
                    # format más abajo) -- lo único que cambia es que ahora
                    # el precio final manda de verdad.
                    st.session_state[margen_key] = round(max(nuevo_margen_pct, 0.0), 6)
                # si el costo es $0 no se puede despejar el margen (división por cero)
                # -> se deja el margen como estaba, se avisa abajo.

            # Defensa: estos 3 campos se precargan con lo que ya está guardado en
            # la base, pero los 3 tienen min_value=0.0 -- si por el motivo que sea
            # quedó guardado un valor negativo (ej: un margen negativo de una carga
            # masiva vieja que no clampeaba a 0), Streamlit tira
            # StreamlitValueBelowMinError al intentar precargar ese valor negativo
            # en el widget, y la página ni siquiera llega a renderizar. Por eso se
            # clampea acá el `value=` inicial -- el guardado real abajo ("Guardar
            # cambios") sigue validando/clampeando como corresponde.
            margen_pct_guardado = float(art_actual["porcentaje_ganancia"])
            if margen_pct_guardado < 0:
                st.warning(
                    f"⚠️ Este artículo tenía guardado un % de ganancia negativo "
                    f"({margen_pct_guardado:.2f}%) -- quiere decir que el precio de venta "
                    f"actual queda por debajo del costo. Se muestra en 0% para poder "
                    f"corregirlo: fijá un costo y/o precio de venta acordes abajo y guardá."
                )

            c1, c2, c3 = st.columns(3)
            nuevo_costo = c1.number_input(
                "Nuevo costo de compra", min_value=0.0,
                value=max(float(art_actual["costo_compra_actual"]), 0.0), key=costo_key,
                on_change=_recalcular_precio_venta
            )
            nuevo_margen_pct = c2.number_input(
                "Nuevo % de ganancia (markup sobre costo)", min_value=0.0,
                value=max(margen_pct_guardado, 0.0), key=margen_key,
                step=0.01, format="%.2f",
                on_change=_recalcular_precio_venta,
                help=(
                    "Por debajo se guarda con más precisión que estos 2 decimales que ves "
                    "-- es lo que permite que, si en vez de tocar esto fijás el precio de "
                    "venta de abajo, el precio final quede exacto (ej: $1500 en vez de $1500.04)."
                )
            )
            nuevo_precio_venta = c3.number_input(
                "Precio de venta final (con IVA)", min_value=0.0,
                value=max(float(art_actual["costo_venta_con_iva"]), 0.0), key=pventa_key,
                on_change=_recalcular_margen,
                help="Precio final al público, con IVA incluido. Si lo cambiás, se recalcula el % de ganancia (el costo no se toca)."
            )
            if nuevo_costo == 0:
                st.caption(
                    "⚠️ Con costo de compra en $0 no se puede calcular el % de ganancia a partir "
                    "del precio de venta -- cargá primero el costo real."
                )

            nuevo_peso_bolsa = None
            if art_actual["unidad_medida"] == "unidad":
                peso_bolsa_actual = art_actual.get("peso_bolsa_gramos")
                peso_bolsa_valor = float(peso_bolsa_actual) if pd.notna(peso_bolsa_actual) else 0.0
                nuevo_peso_bolsa_input = st.number_input(
                    "Peso de la bolsa (gramos, opcional)", min_value=0.0, value=peso_bolsa_valor, step=10.0,
                    help=(
                        "Si lo cargás, en 'Registrar Venta' vas a poder elegir vender este artículo "
                        "'Bolsa entera' o 'Por fracción' (ej: 150g de una bolsa de 420g). Dejalo en 0 "
                        "si no aplica."
                    ),
                    key=peso_key
                )
                nuevo_peso_bolsa = nuevo_peso_bolsa_input if nuevo_peso_bolsa_input > 0 else None

            # Fix margen 0 (23/10/2026): si el precio final que se escribe queda por
            # debajo del costo con IVA, _recalcular_margen clampea el % a 0 (el widget
            # no admite negativos) y, al tocar "Actualizar", se guardaba margen 0 SIN
            # AVISO: el precio fijado (ej. $4.500) se perdía y quedaba costo x IVA
            # ($4.537,50). Caso típico: el costo se cargó con IVA incluido. Ahora no
            # se deja guardar un precio bajo el costo, y margen 0 pide confirmación.
            precio_minimo = round(nuevo_costo * (1 + iva_actual), 2)
            bajo_costo = nuevo_costo > 0 and nuevo_precio_venta + 0.005 < precio_minimo
            sin_margen = nuevo_costo > 0 and not bajo_costo and nuevo_margen_pct <= 0
            confirma_sin_margen = True
            if bajo_costo:
                st.error(
                    f"El precio de venta (${nuevo_precio_venta:,.2f}) queda por debajo del costo con "
                    f"IVA (${precio_minimo:,.2f}): se vendería a pérdida y el % de ganancia quedaría "
                    f"en 0. Revisá el costo -- ¿está cargado con IVA incluido? Va **sin IVA**. "
                    f"No se guarda hasta que el precio sea mayor al costo con IVA."
                )
            elif sin_margen:
                st.warning(
                    "Con 0% de ganancia el artículo se vende al costo (solo se le suma el IVA)."
                )
                confirma_sin_margen = st.checkbox(
                    "Sí, quiero dejarlo con 0% de ganancia", key=f"pm_conf0_{huella_pm}"
                )

            if st.button("Actualizar", disabled=bajo_costo or not confirma_sin_margen):
                db.actualizar_precio_articulo(
                    int(art_sel), nuevo_costo=nuevo_costo, nuevo_margen=nuevo_margen_pct / 100,
                    nuevo_peso_bolsa_gramos=nuevo_peso_bolsa,
                    actualizar_peso_bolsa=(art_actual["unidad_medida"] == "unidad")
                )
                avisar("success", "Actualizado.")
                st.rerun()

            st.divider()
            st.subheader("🗑️ Eliminar un artículo")
            art_borrar = st.selectbox(
                "Artículo a eliminar", articulos["id"],
                format_func=etiquetador(articulos),
                key="sel_borrar_articulo"
            )
            st.caption("Solo se puede borrar si no tiene compras ni ventas registradas todavía. "
                       "Si ya tiene movimientos, dalo de baja en 'Editar artículo'.")
            if st.button("Eliminar artículo", type="secondary"):
                confirmar_eliminacion(
                    f"el artículo {formatear_articulo(articulos, art_borrar)}",
                    "Se borra definitivamente (queda registrado en el Historial de cambios).",
                    lambda: db.eliminar_articulo(int(art_borrar)),
                    "Artículo eliminado.",
                )

    with tab2:
        categorias_existentes = db.listar_categorias()
        opciones_categoria_nueva = categorias_existentes + ["Otros"]
        categoria_sel = st.selectbox(
            "Categoría", opciones_categoria_nueva, key="categoria_sel_nuevo",
            help="Elegí una categoría ya cargada, o 'Otros' para escribir una nueva."
        )
        if categoria_sel == "Otros":
            categoria_preview = st.text_input(
                "Nueva categoría", key="categoria_otros_nuevo",
                placeholder="ej: Panificados"
            )
        else:
            categoria_preview = categoria_sel

        # Sugerencia simple (primer paso hacia detección automática): si la
        # categoría sugiere un producto fraccionable, pre-seleccionamos "kg".
        categorias_fraccionables = ["fiamb", "queso", "embutid", "carne", "frutas", "verdura"]
        sugerencia_kg = any(k in categoria_preview.lower() for k in categorias_fraccionables) if categoria_preview else False

        # Fuera del formulario a propósito (igual que la categoría): dentro de un
        # st.form la fecha no aparecía al tildar, y al tocar "Crear artículo" se
        # guardaba la fecha de hoy sin que nadie la hubiera elegido.
        tiene_vencimiento = st.checkbox("¿Tiene fecha de vencimiento?", key="chk_venc_nuevo")

        with st.form("nuevo_articulo", enter_to_submit=False):
            nombre = st.text_input("Nombre del artículo")
            marca = st.text_input("Marca (opcional)", placeholder="ej: Cocinero, Natura, La Serenísima")
            st.caption(f"Categoría: **{categoria_preview or '(sin definir)'}**")
            unidad_medida = st.radio(
                "Unidad de medida",
                ["unidad", "kg"],
                index=1 if sugerencia_kg else 0,
                format_func=lambda u: "Unidades enteras (ej: latas, paquetes)" if u == "unidad" else "Kilogramos — fraccionable (ej: fiambres, quesos)",
                horizontal=True
            )
            if sugerencia_kg:
                st.caption("💡 Sugerido 'kg' según la categoría — podés cambiarlo si no corresponde.")
            c1, c2, c3 = st.columns(3)
            costo_compra = c1.number_input("Costo de compra", min_value=0.0, step=1.0)
            margen_pct = c2.number_input("% de ganancia", min_value=0.0, value=30.0, step=1.0)
            iva_pct = c3.number_input("% IVA", min_value=0.0, max_value=27.0, value=21.0, step=0.5,
                                      help="Alícuota de IVA del producto (21 % o 10,5 %). El margen va en \"% de ganancia\".")
            c4, c5 = st.columns(2)
            unidad_label = "Stock inicial (kg)" if unidad_medida == "kg" else "Stock inicial (unidades)"
            stock_min_label = "Stock mínimo, kg (alertas)" if unidad_medida == "kg" else "Stock mínimo, unidades (alertas)"
            stock_inicial = c4.number_input(unidad_label, min_value=0.0, step=0.1 if unidad_medida == "kg" else 1.0)
            stock_minimo = c5.number_input(stock_min_label, min_value=0.0, step=0.1 if unidad_medida == "kg" else 1.0)
            fecha_venc = (st.date_input("Fecha de vencimiento", value=None, format="DD/MM/YYYY")
                          if tiene_vencimiento else None)

            peso_bolsa_gramos = None
            if unidad_medida == "unidad":
                peso_bolsa_input = st.number_input(
                    "Peso de la bolsa (gramos, opcional)", min_value=0.0, value=0.0, step=10.0,
                    help=(
                        "Si lo cargás, en 'Registrar Venta' vas a poder elegir vender este artículo "
                        "'Bolsa entera' o 'Por fracción' (ej: 150g de una bolsa de 420g). Dejalo en 0 "
                        "si no aplica."
                    )
                )
                peso_bolsa_gramos = peso_bolsa_input if peso_bolsa_input > 0 else None

            submitted = st.form_submit_button("Crear artículo")
            if submitted:
                if not nombre:
                    st.error("El nombre es obligatorio.")
                elif not categoria_preview:
                    st.error("La categoría es obligatoria -- elegí una de la lista o escribí una en 'Otros'.")
                elif tiene_vencimiento and fecha_venc is None:
                    st.error("Elegí la fecha de vencimiento, o destildá \"¿Tiene fecha de vencimiento?\".")
                else:
                    categoria_final = categoria_preview.strip().capitalize()
                    duplicados = db.buscar_articulo_por_nombre_exacto(nombre)
                    ya_confirmado = st.session_state.get("confirmar_duplicado_nombre") == nombre
                    if not duplicados.empty and not ya_confirmado:
                        st.warning(
                            f"⚠️ Ya existe un artículo llamado '{duplicados.iloc[0]['nombre']}'. "
                            "Si tocaste 'Crear artículo' más de una vez sin querer, no lo vuelvas a tocar — "
                            "capaz ya se creó. Si en cambio es un producto distinto con el mismo nombre a propósito, "
                            "tocá 'Crear artículo' una vez más para confirmar."
                        )
                        st.session_state.confirmar_duplicado_nombre = nombre
                    else:
                        db.crear_articulo(
                            nombre, categoria_final, costo_compra, margen_pct / 100, iva_pct / 100,
                            stock_inicial, stock_minimo, fecha_venc, unidad_medida=unidad_medida,
                            marca=marca or None, peso_bolsa_gramos=peso_bolsa_gramos
                        )
                        st.session_state.pop("confirmar_duplicado_nombre", None)
                        avisar("success", f"Artículo '{nombre}' creado.")
                        st.rerun()

    with tab3:
        # Incluye los inactivos: es el único lugar desde donde se reactivan.
        articulos_edit = db.listar_articulos(incluir_inactivos=True)
        if articulos_edit.empty:
            st.info("Todavía no hay artículos cargados.")
        else:
            art_edit_id = st.selectbox(
                "Artículo a editar", articulos_edit["id"],
                format_func=etiquetador(articulos_edit),
                key="sel_editar_articulo"
            )
            art_edit = articulos_edit[articulos_edit["id"] == art_edit_id].iloc[0]
            hay_movimientos = db.tiene_movimientos(int(art_edit_id))

            # ---- Estado: activo / inactivo (baja lógica) ----
            # Fuera del formulario de abajo a propósito: los campos de motivo
            # y reemplazo aparecen o no según el interruptor, y dentro de un
            # st.form la pantalla no se actualiza hasta que se envía.
            with st.container(border=True):
                estaba_activo = bool(art_edit["activo"])
                if not estaba_activo:
                    fecha_baja_txt = (pd.to_datetime(art_edit["fecha_baja"]).strftime("%d/%m/%Y")
                                      if pd.notna(art_edit.get("fecha_baja")) else "?")
                    motivo_txt = db.MOTIVOS_BAJA.get(art_edit.get("motivo_baja"), "sin motivo")
                    reemp_txt = ""
                    if pd.notna(art_edit.get("reemplazado_por")):
                        reemp_id = int(art_edit["reemplazado_por"])
                        if reemp_id in set(articulos_edit["id"]):
                            reemp_txt = f" → **{formatear_articulo(articulos_edit, reemp_id)}**"
                    st.warning(f"⛔ Inactivo desde el {fecha_baja_txt} · {motivo_txt}{reemp_txt}")

                activo_nuevo = st.toggle(
                    "Artículo activo", value=estaba_activo, key=f"activo_{art_edit_id}",
                    help=(
                        "Apagalo si el comerciante no trabaja más este artículo o lo reemplazó por otro. "
                        "No se borra nada: sus ventas y compras siguen en los reportes. Deja de "
                        "aparecer en Registrar Venta, Cargar Factura, Promociones y Reposición, y "
                        "sale del indicador de calidad de datos (salvo que le quede stock)."
                    )
                )
                motivo_nuevo, reemplazo_nuevo = None, None
                if not activo_nuevo:
                    claves_motivo = list(db.MOTIVOS_BAJA.keys())
                    motivo_guardado = art_edit.get("motivo_baja")
                    motivo_nuevo = st.selectbox(
                        "Motivo de la baja", claves_motivo,
                        index=claves_motivo.index(motivo_guardado) if motivo_guardado in claves_motivo else 0,
                        format_func=lambda k: db.MOTIVOS_BAJA[k], key=f"motivo_baja_{art_edit_id}"
                    )
                    if motivo_nuevo == "reemplazado":
                        candidatos = articulos_edit[
                            articulos_edit["activo"] & (articulos_edit["id"] != art_edit_id)
                        ]
                        ids_candidatos = candidatos["id"].tolist()
                        reemp_guardado = art_edit.get("reemplazado_por")
                        idx_reemp = (ids_candidatos.index(int(reemp_guardado))
                                     if pd.notna(reemp_guardado) and int(reemp_guardado) in ids_candidatos
                                     else None)
                        reemplazo_nuevo = st.selectbox(
                            "Reemplazado por", ids_candidatos, index=idx_reemp,
                            format_func=etiquetador(candidatos),
                            placeholder="Elegí el artículo nuevo", key=f"reemplazo_{art_edit_id}",
                            help=("El pronóstico y la lista de compras del artículo nuevo van a sumar lo que "
                                  "vendió este, para no arrancar sin historial.")
                        )

                    stock_que_queda = float(art_edit["stock_actual"])
                    es_combo_virtual = (art_edit["categoria"] == db.CATEGORIA_PROMOCION
                                        and art_edit.get("promo_tipo") == "virtual")
                    if stock_que_queda != 0 and not es_combo_virtual:
                        st.caption(
                            f"📦 Le quedan {stock_que_queda:g} en stock. Va a seguir apareciendo en "
                            "Calidad de datos como **Inactivo con stock** hasta que se liquide o se "
                            "ajuste a 0 con un conteo (campo 'Stock actual', más abajo)."
                        )
                    promos = db.promociones_que_lo_usan(int(art_edit_id))
                    if not promos.empty:
                        st.caption(
                            "🎁 Es componente de: " + ", ".join(promos["nombre"]) +
                            ". Esas promociones siguen activas -- revisalas en Promociones."
                        )

                cambio_estado = (
                    activo_nuevo != estaba_activo
                    or (not activo_nuevo and (
                        motivo_nuevo != art_edit.get("motivo_baja")
                        or (reemplazo_nuevo or None) != (int(art_edit["reemplazado_por"])
                                                         if pd.notna(art_edit.get("reemplazado_por")) else None)
                    ))
                )
                if cambio_estado:
                    etiqueta = "Reactivar artículo" if activo_nuevo else "Confirmar baja"
                    if st.button(etiqueta, type="primary", key=f"btn_estado_{art_edit_id}"):
                        if motivo_nuevo == "reemplazado" and reemplazo_nuevo is None:
                            st.error("Elegí por qué artículo se reemplaza, o cambiá el motivo.")
                        else:
                            db.cambiar_estado_articulo(
                                int(art_edit_id), activo_nuevo, motivo_nuevo, reemplazo_nuevo
                            )
                            avisar("success", "Artículo reactivado." if activo_nuevo else "Artículo dado de baja.")
                            st.rerun()

            categorias_existentes = db.listar_categorias()
            opciones_categoria_editar = categorias_existentes + ["Otros"]
            categoria_cruda = art_edit["categoria"] if pd.notna(art_edit["categoria"]) else ""
            categoria_actual_normalizada = categoria_cruda.strip().capitalize()
            if categoria_actual_normalizada and categoria_actual_normalizada in categorias_existentes:
                indice_categoria = categorias_existentes.index(categoria_actual_normalizada)
            else:
                indice_categoria = len(opciones_categoria_editar) - 1  # "Otros"
            categoria_sel_editar = st.selectbox(
                "Categoría", opciones_categoria_editar, index=indice_categoria,
                key=f"categoria_sel_editar_{art_edit_id}",
                help="Elegí una categoría ya cargada, o 'Otros' para escribir una nueva."
            )
            if categoria_sel_editar == "Otros":
                valor_otros = categoria_actual_normalizada if indice_categoria == len(opciones_categoria_editar) - 1 else ""
                nueva_categoria = st.text_input(
                    "Nueva categoría", value=valor_otros,
                    key=f"categoria_otros_editar_{art_edit_id}", placeholder="ej: Panificados"
                )
            else:
                nueva_categoria = categoria_sel_editar

            # Fuera del formulario (mismo motivo que en Nuevo artículo): dentro del
            # form, al tildarlo la fecha no aparecía y el primer "Guardar" grababa
            # el artículo sin vencimiento.
            tiene_venc_actual = pd.notna(art_edit.get("fecha_vencimiento"))
            tiene_vencimiento = st.checkbox(
                "¿Tiene fecha de vencimiento?", value=bool(tiene_venc_actual),
                key=f"chk_venc_editar_{art_edit_id}"
            )

            with st.form("editar_articulo_form", enter_to_submit=False):
                # Cada campo lleva el id del artículo en su clave: con una clave fija,
                # Streamlit conservaba lo elegido para el artículo anterior y al guardar
                # se lo pasaba a este (así Cremoso y Tybo quedaron "por unidad", 21/10/2026).
                nuevo_nombre = st.text_input("Nombre", value=art_edit["nombre"], key=f"nombre_editar_{art_edit_id}")
                c1, c2 = st.columns(2)
                marca_actual = art_edit["marca"] if pd.notna(art_edit["marca"]) else ""
                nueva_marca = c1.text_input("Marca", value=marca_actual, key=f"marca_editar_{art_edit_id}")
                c2.caption("Categoría")
                c2.write(f"**{nueva_categoria or '(sin definir)'}**")

                if hay_movimientos:
                    st.caption(
                        "⚠️ Este artículo ya tiene compras, ventas o fraccionamientos "
                        "registrados. Cambiar la unidad de medida no recalcula ese "
                        "historial -- hacelo solo si sabés lo que implica."
                    )
                nueva_unidad = st.radio(
                    "Unidad de medida", ["unidad", "kg"],
                    index=0 if art_edit["unidad_medida"] == "unidad" else 1,
                    format_func=lambda u: "Unidades enteras (ej: latas, paquetes)" if u == "unidad" else "Kilogramos — fraccionable (ej: fiambres, quesos)",
                    horizontal=True, key=f"unidad_editar_{art_edit_id}"
                )

                fecha_venc_nueva = None
                if tiene_vencimiento:
                    valor_fecha = pd.to_datetime(art_edit["fecha_vencimiento"]).date() if tiene_venc_actual else None
                    fecha_venc_nueva = st.date_input(
                        "Fecha de vencimiento", value=valor_fecha, format="DD/MM/YYYY",
                        key=f"fecha_venc_editar_{art_edit_id}"
                    )

                st.divider()
                unidad_txt = "kg" if art_edit["unidad_medida"] == "kg" else "unidades"
                stock_actual_valor = float(art_edit["stock_actual"])
                es_promo_virtual = (
                    art_edit["categoria"] == db.CATEGORIA_PROMOCION and art_edit.get("promo_tipo") == "virtual"
                )
                if es_promo_virtual:
                    st.info(
                        f"🔗 Este es un combo de stock compartido: su stock ({stock_actual_valor:g}) se "
                        "calcula solo a partir de sus artículos componentes y no se corrige a mano acá "
                        "-- corregí el stock del artículo original que corresponda."
                    )
                    nuevo_stock = stock_actual_valor
                else:
                    if stock_actual_valor < 0:
                        st.info(
                            f"📥 Este artículo tiene stock {stock_actual_valor:g} -- se vendió más de lo "
                            "que figuraba comprado. Es normal mientras no se cargue la factura de compra "
                            "correspondiente (se ajusta solo); si preferís corregirlo a mano con un control "
                            "físico, hacelo acá abajo."
                        )
                    nuevo_stock = st.number_input(
                        f"Stock actual ({unidad_txt})", key=f"stock_editar_{art_edit_id}",
                        min_value=min(0.0, stock_actual_valor),
                        value=stock_actual_valor,
                        step=0.1 if art_edit["unidad_medida"] == "kg" else 1.0,
                        help=(
                            "Corrige el stock a mano -- por ejemplo, después de un control "
                            "físico. No modifica compras ni ventas ya registradas."
                        )
                    )
                    st.caption("📦 ¿Llegó mercadería con factura? No la sumes acá: cargá la factura en "
                               "Cargar Factura. Si hacés las dos cosas, el stock queda sumado dos veces.")

                guardar = st.form_submit_button("Guardar cambios")
                if guardar:
                    if not nuevo_nombre:
                        st.error("El nombre es obligatorio.")
                    elif tiene_vencimiento and fecha_venc_nueva is None:
                        st.error("Elegí la fecha de vencimiento, o destildá \"¿Tiene fecha de vencimiento?\".")
                    else:
                        duplicados = db.buscar_articulo_por_nombre_exacto(
                            nuevo_nombre, excluir_id=int(art_edit_id)
                        )
                        if not duplicados.empty:
                            st.error(
                                f"Ya existe otro artículo llamado '{duplicados.iloc[0]['nombre']}'. "
                                "Elegí otro nombre."
                            )
                        else:
                            nueva_categoria_final = (nueva_categoria or "").strip().capitalize() or None
                            db.actualizar_articulo(
                                int(art_edit_id), nuevo_nombre, nueva_marca or None,
                                nueva_categoria_final, nueva_unidad, fecha_venc_nueva,
                                nuevo_stock
                            )
                            avisar("success", "Artículo actualizado.")
                            st.rerun()

# ============================================================
# CARGAR FACTURA (COMPRA)
# ============================================================
elif pagina == "🧾 Cargar Factura (Compra)":
    st.title("🧾 Cargar Factura de Compra")
    MODO_PLANILLA = "📥 Desde la planilla (lector automático)"
    MODO_MANUAL = "✍️ A mano"
    modo_factura = st.radio(
        "¿Cómo cargás la factura?", [MODO_PLANILLA, MODO_MANUAL],
        horizontal=True, key="modo_cargar_factura"
    )

    if modo_factura == MODO_PLANILLA:
        mostrar_importador()
    else:
        st.caption(
            "Carga manual: para facturas que no pasaron por el escáner, o para corregir algo puntual."
        )

        proveedores = db.listar_proveedores()
        articulos = db.listar_articulos()

        with st.expander("➕ Agregar proveedor nuevo"):
            with st.form("nuevo_proveedor", enter_to_submit=False):
                nombre_prov = st.text_input("Nombre del proveedor")
                cuit = st.text_input("CUIT (opcional)")
                tel = st.text_input("Teléfono (opcional)")
                if st.form_submit_button("Crear proveedor"):
                    if nombre_prov:
                        db.crear_proveedor(nombre_prov, cuit or None, tel or None)
                        avisar("success", "Proveedor creado.")
                        st.rerun()
                    else:
                        st.error("El nombre es obligatorio.")

        if proveedores.empty:
            st.warning("Primero cargá al menos un proveedor (arriba).")
        elif articulos.empty:
            st.warning("Primero cargá al menos un artículo en la sección 'Artículos'.")
        else:
            # ¿Se están sumando ítems a una factura ya guardada? Puede venir de
            # "Facturas cargadas" (botón) o de escribir un número que ya existe.
            agregar_a = None
            forzada = st.session_state.get("fac_agregar_a")
            info_destino = db.resumen_factura(int(forzada)) if forzada else None
            if forzada and info_destino is None:
                st.session_state.pop("fac_agregar_a", None)
            if info_destino is not None:
                agregar_a = int(forzada)
                proveedor_id = int(info_destino["proveedor_id"])
                numero_factura, fecha_factura = info_destino["numero"], info_destino["fecha"]
                st.info(
                    f"➕ Agregando ítems a la factura **#{agregar_a}** de **{info_destino['proveedor']}** "
                    f"nº {info_destino['numero'] or 's/n'} del {info_destino['fecha']:%d/%m/%Y} "
                    f"(ya tiene {info_destino['items']} ítem(s) por {md_pesos(float(info_destino['total']))} s/IVA)."
                )
                st.button("✕ Cancelar: cargar una factura nueva", key="fac_cancelar_agregar",
                          on_click=lambda: st.session_state.pop("fac_agregar_a", None))
            else:
                proveedor_id = st.selectbox(
                    "Proveedor", proveedores["id"],
                    format_func=lambda x: proveedores[proveedores["id"] == x]["nombre"].values[0],
                    key="fac_proveedor"
                )
                if "consignacion" in proveedores.columns and bool(
                        proveedores.set_index("id").loc[proveedor_id].get("consignacion", False)):
                    st.info("🤝 Proveedor en consignación: cargá lo que dejó con el precio que te cobra por unidad. "
                            "Suma stock como cualquier factura, pero se le paga recién lo vendido "
                            "(Proveedores → Consignación). Si no deja papel, usá un número como CONSIG-dd/mm.")
                numero_factura = st.text_input("Número de factura", key="fac_numero")
                fecha_factura = st.date_input("Fecha de la factura", value=hoy_argentina(), key="fac_fecha",
                                              format="DD/MM/YYYY")
                repetida = db.factura_existente(int(proveedor_id), numero_factura) if numero_factura else None
                if repetida:
                    info_destino = db.resumen_factura(int(repetida))
                    st.warning(
                        f"⚠️ Ya hay una factura **#{repetida}** de este proveedor con ese número, del "
                        f"{info_destino['fecha']:%d/%m/%Y} ({info_destino['items']} ítem(s) por "
                        f"{md_pesos(float(info_destino['total']))} s/IVA). Si la guardás de nuevo, "
                        "el stock se suma dos veces."
                    )
                    destino = st.radio(
                        "¿Qué querés hacer?",
                        ["agregar", "nueva"],
                        format_func=lambda d: (f"Agregar estos ítems a la factura #{repetida} (faltaron al cargarla)"
                                               if d == "agregar" else "Es otra factura: guardarla aparte"),
                        key=f"fac_destino_{repetida}"
                    )
                    if destino == "agregar":
                        agregar_a = int(repetida)
                        st.caption(f"Se mantiene la fecha de la factura original ({info_destino['fecha']:%d/%m/%Y}).")
                    else:
                        info_destino = None
            if agregar_a is not None:
                with st.expander(f"Ver lo que ya tiene la factura #{agregar_a}"):
                    st.dataframe(
                        db.detalle_de_factura(agregar_a).drop(columns=["id"]),
                        column_config={
                            "articulo": "Artículo", "cantidad": "Cantidad",
                            "costo_unitario": st.column_config.NumberColumn("Costo unitario ($)", format=FMT_PESOS),
                            "subtotal": st.column_config.NumberColumn("Subtotal ($)", format=FMT_PESOS),
                        },
                        hide_index=True, width="stretch",
                    )

            st.subheader("Ítems de la factura")
            if "items_factura" not in st.session_state:
                st.session_state.items_factura = []

            def _error_item_factura(cantidad, costo_unitario):
                # Costo $0 no es un error de tipeo inofensivo: un trigger de la
                # base deja costo_compra_actual = costo_unitario apenas se
                # guarda la factura, sin chequear que sea mayor a 0 -- eso le
                # pone el precio de venta de ese artículo en $0 al toque.
                if cantidad <= 0:
                    return "La cantidad debe ser mayor a 0."
                if costo_unitario <= 0:
                    return (
                        "El costo unitario debe ser mayor a 0 -- si lo dejás en $0, "
                        "el precio de venta de ese artículo queda en $0 apenas guardés la factura."
                    )
                return None

            def _campo_costo_sin_con_iva(key_prefix, iva, col_sin, col_con):
                """
                Dos campos enlazados -- costo unitario SIN IVA y CON IVA -- para
                cuando la factura del proveedor no discrimina el IVA: completás
                cualquiera de los dos y el otro se calcula solo (mismo mecanismo
                que costo/margen/precio en "Actualizar precio / margen"). Lo que
                se guarda como costo_unitario de la factura es siempre el valor
                SIN IVA -- mismo criterio que usa costo_compra_actual en el
                resto del sistema. El IVA usado para convertir es el del
                artículo elegido (el mismo que ya se usa para calcular su
                precio de venta).
                """
                key_sin = f"{key_prefix}_sin"
                key_con = f"{key_prefix}_con"

                def _desde_sin(key_sin=key_sin, key_con=key_con, iva=iva):
                    sin = st.session_state.get(key_sin, 0.0)
                    st.session_state[key_con] = round(sin * (1 + iva), 2)

                def _desde_con(key_sin=key_sin, key_con=key_con, iva=iva):
                    con = st.session_state.get(key_con, 0.0)
                    st.session_state[key_sin] = round(con / (1 + iva), 2)

                ayuda = (
                    "Completá SOLO uno de los dos -- el otro se calcula solo con el IVA del artículo. "
                    "No hace falta llenar ambos."
                )
                costo_sin = col_sin.number_input(
                    "Costo unitario SIN IVA", min_value=0.0, step=1.0, key=key_sin,
                    on_change=_desde_sin, help=ayuda
                )
                col_con.number_input(
                    "Costo unitario CON IVA", min_value=0.0, step=1.0, key=key_con,
                    on_change=_desde_con, help=ayuda
                )
                return st.session_state.get(key_sin, costo_sin)

            def _iva_de_articulo(df_articulos, articulo_id, default=0.21):
                if articulo_id is None:
                    return default
                fila = df_articulos[df_articulos["id"] == articulo_id]
                if fila.empty or pd.isna(fila["iva"].values[0]):
                    return default
                return float(fila["iva"].values[0])

            def _campo_actualiza_precio(key):
                """
                Casillero para marcar un ítem como compra puntual (descuento del
                proveedor, oferta única): no actualiza el costo de referencia ni
                el precio de venta del artículo -- el stock sube igual y la
                factura queda con el costo real pagado.
                """
                return st.checkbox(
                    "Actualizar el costo de referencia y el precio de venta con este costo",
                    value=True, key=key,
                    help=(
                        "Destildalo si esta compra fue puntual (descuento del proveedor, "
                        "oferta única, etc.) y no refleja el costo habitual -- el stock sube "
                        "igual y esta factura queda con el costo real que pagaste, pero el "
                        "costo de referencia y el precio de venta del artículo no se tocan. "
                        "Tildado (lo normal), además el precio de venta se reajusta para "
                        "quedar redondo a la centena más cercana."
                    )
                )

            modo_carga = st.radio(
                "¿Cómo querés agregar el ítem?",
                ["Elegir de mi catálogo", "Escribir el nombre tal como figura en la factura"],
                horizontal=True
            )

            if modo_carga == "Elegir de mi catálogo":
                c1, c2, c3, c4, c5 = st.columns([2.4, 0.9, 1.1, 1.1, 0.9])
                articulo_id = c1.selectbox(
                    "Artículo", articulos["id"],
                    format_func=etiquetador(articulos),
                    key="sel_articulo"
                )
                cantidad = c2.number_input("Cantidad", min_value=0.0, step=1.0, key="sel_cantidad")
                costo_unitario = _campo_costo_sin_con_iva(
                    "sel_costo", _iva_de_articulo(articulos, articulo_id), c3, c4
                )
                actualiza_precio_sel = _campo_actualiza_precio("actualiza_precio_sel")
                if c5.button("Agregar ítem"):
                    error = _error_item_factura(cantidad, costo_unitario)
                    if error:
                        st.error(error)
                    else:
                        nombre_art = articulos[articulos["id"] == articulo_id]["nombre"].values[0]
                        st.session_state.items_factura.append({
                            "articulo_id": int(articulo_id), "nombre": nombre_art,
                            "cantidad": cantidad, "costo_unitario": costo_unitario,
                            "actualiza_precio": actualiza_precio_sel
                        })

            else:
                st.caption(
                    "Útil cuando el proveedor nombra el producto distinto a como lo tenés en tu catálogo "
                    "(ej: 'ACEITE COCINERO X1.5L' vs 'Aceite Girasol 1.5L')."
                )
                nombre_factura = st.text_input("Nombre tal como figura en la factura", key="nombre_factura_texto")

                if nombre_factura:
                    # 1) ¿Ya vimos este nombre exacto de este proveedor antes?
                    alias_match = db.buscar_alias_exacto(int(proveedor_id), nombre_factura)
                    if alias_match and not alias_match.get("activo", True):
                        # El alias apunta a un artículo dado de baja: no se le carga stock.
                        reemplazo_id = alias_match.get("reemplazado_por")
                        reemplazo = (articulos[articulos["id"] == int(reemplazo_id)]
                                     if pd.notna(reemplazo_id) else articulos.iloc[0:0])
                        if not reemplazo.empty:
                            r_art = reemplazo.iloc[0]
                            st.warning(
                                f"Este nombre estaba vinculado a **{alias_match['nombre']}**, que está dado de baja "
                                f"y fue reemplazado por **{r_art['nombre']}**. Se carga en el reemplazo (y la "
                                "próxima vez se reconoce solo)."
                            )
                            alias_match = dict(r_art)
                            alias_match["_actualizar_alias"] = True
                        else:
                            st.warning(
                                f"Este nombre estaba vinculado a **{alias_match['nombre']}**, que está dado de baja. "
                                "Elegí abajo a qué artículo corresponde ahora."
                            )
                            alias_match = None
                    if alias_match:
                        if not alias_match.get("_actualizar_alias"):
                            st.success(f"✅ Reconocido automáticamente como: **{alias_match['nombre']}** "
                                       "(ya visto antes de este proveedor)")
                        c1, c2, c3, c4 = st.columns([1, 1.3, 1.3, 1])
                        cantidad = c1.number_input("Cantidad", min_value=0.0, step=1.0, key="cant_alias")
                        iva_alias = float(alias_match["iva"]) if alias_match.get("iva") is not None else 0.21
                        costo_unitario = _campo_costo_sin_con_iva("costo_alias", iva_alias, c2, c3)
                        actualiza_precio_alias = _campo_actualiza_precio("actualiza_precio_alias")
                        if c4.button("Agregar ítem", key="btn_agregar_alias"):
                            error = _error_item_factura(cantidad, costo_unitario)
                            if error:
                                st.error(error)
                            else:
                                if alias_match.get("_actualizar_alias"):
                                    db.guardar_alias(int(alias_match["id"]), int(proveedor_id), nombre_factura)
                                st.session_state.items_factura.append({
                                    "articulo_id": int(alias_match["id"]), "nombre": alias_match["nombre"],
                                    "cantidad": cantidad, "costo_unitario": costo_unitario,
                                    "actualiza_precio": actualiza_precio_alias
                                })
                                st.rerun()
                    else:
                        # 2) Buscar artículos parecidos por similitud de texto
                        sugerencias = db.sugerir_articulos_similares(nombre_factura)
                        if not sugerencias.empty:
                            st.write("¿Es alguno de estos artículos que ya tenés cargados?")
                            opciones = {
                                f"{row['nombre']}  ·  {row['similitud']*100:.0f}% parecido": row["id"]
                                for _, row in sugerencias.iterrows()
                            }
                            opciones["Ninguno — es un artículo nuevo"] = None
                            eleccion = st.radio("Coincidencias sugeridas", list(opciones.keys()), key="eleccion_match")
                            articulo_elegido_id = opciones[eleccion]
                        else:
                            st.info("No encontré artículos parecidos en tu catálogo.")
                            articulo_elegido_id = None

                        c1, c2, c3, c4 = st.columns([1, 1.3, 1.3, 1])
                        cantidad = c1.number_input("Cantidad", min_value=0.0, step=1.0, key="cant_match")
                        costo_unitario = _campo_costo_sin_con_iva(
                            "costo_match", _iva_de_articulo(articulos, articulo_elegido_id), c2, c3
                        )
                        actualiza_precio_match = _campo_actualiza_precio("actualiza_precio_match")

                        if articulo_elegido_id is not None:
                            if c4.button("Confirmar coincidencia y agregar", key="btn_confirmar_match"):
                                error = _error_item_factura(cantidad, costo_unitario)
                                if error:
                                    st.error(error)
                                else:
                                    nombre_art = articulos[articulos["id"] == articulo_elegido_id]["nombre"].values[0]
                                    db.guardar_alias(int(articulo_elegido_id), int(proveedor_id), nombre_factura)
                                    st.session_state.items_factura.append({
                                        "articulo_id": int(articulo_elegido_id), "nombre": nombre_art,
                                        "cantidad": cantidad, "costo_unitario": costo_unitario,
                                        "actualiza_precio": actualiza_precio_match
                                    })
                                    avisar("success", "Alias guardado — la próxima vez lo va a reconocer solo.")
                                    st.rerun()
                        else:
                            st.warning(
                                "Este nombre no coincide con ningún artículo existente. "
                                "Andá a la sección 'Artículos' y creálo primero con este nombre "
                                f"('{nombre_factura}' o el que prefieras), después volvé acá a cargarlo."
                            )

            if st.session_state.items_factura:
                hc1, hc2, hc3, hc4, hc5, hc6 = st.columns([3, 1.2, 1.5, 1.5, 1.6, 0.6])
                hc1.markdown("**Artículo**")
                hc2.markdown("**Cantidad**")
                hc3.markdown("**Costo unitario**")
                hc4.markdown("**Subtotal**")
                hay_items_sin_actualizar = any(
                    not i.get("actualiza_precio", True) for i in st.session_state.items_factura
                )
                ya_en_destino = set(info_destino["articulos"]) if agregar_a is not None else set()
                # Tanda 18: vencimiento del renglón. Para los artículos con días disponibles
                # (propios o de su categoría) se muestra la fecha estimada; se cambia solo si
                # la mercadería vino con menos vida de la habitual.
                con_venc_fac = db.hay_tanda18()
                dias_disp_fac = db.dias_disponibles([i["articulo_id"] for i in st.session_state.items_factura]) \
                    if con_venc_fac else {}
                fecha_base_fac = (info_destino["fecha"] if agregar_a is not None else fecha_factura)
                todos_venc = con_venc_fac and st.checkbox(
                    "Cargar fecha de vencimiento en todos los renglones", key="fac_venc_todos",
                    help="Por defecto solo se pide en los artículos que tienen días disponibles para la venta.")
                if con_venc_fac:
                    hc5.markdown("**Vence**")
                for idx, item in enumerate(st.session_state.items_factura):
                    c1, c2, c3, c4, c5, c6 = st.columns([3, 1.2, 1.5, 1.5, 1.6, 0.6])
                    subtotal = item["cantidad"] * item["costo_unitario"]
                    nombre_mostrar = item["nombre"]
                    if not item.get("actualiza_precio", True):
                        nombre_mostrar += " 🔖"
                    if item["articulo_id"] in ya_en_destino:
                        nombre_mostrar += " ⚠️ ya está en la factura"
                    c1.write(nombre_mostrar)
                    c2.write(f"{item['cantidad']:g}")
                    c3.write(md_pesos(item['costo_unitario']))
                    c4.write(md_pesos(subtotal))
                    dias_it = dias_disp_fac.get(item["articulo_id"])
                    if con_venc_fac and (dias_it or todos_venc):
                        estimada = (pd.Timestamp(fecha_base_fac) + pd.Timedelta(days=int(dias_it))).date() \
                            if dias_it else None
                        elegida = c5.date_input(
                            "Vence", value=item.get("fecha_vencimiento") or estimada, format="DD/MM/YYYY",
                            key=f"venc_item_factura_{idx}_{item['articulo_id']}", label_visibility="collapsed",
                            help=(f"Estimada: compra + {dias_it} días disponibles. Cambiala solo si vino con otra "
                                  "fecha." if dias_it else "Opcional."))
                        # se guarda solo si difiere de la estimada (la estimada se recalcula sola)
                        item["fecha_vencimiento"] = elegida if (elegida and elegida != estimada) else None
                    if c6.button("✕", key=f"quitar_item_factura_{idx}"):
                        st.session_state.items_factura.pop(idx)
                        st.rerun()
                if hay_items_sin_actualizar:
                    st.caption(
                        "🔖 = compra puntual -- al guardar, sube el stock pero NO actualiza el "
                        "costo de referencia ni el precio de venta de ese artículo."
                    )
                if any(i["articulo_id"] in ya_en_destino for i in st.session_state.items_factura):
                    st.caption("⚠️ = ese artículo ya figura en la factura: si lo agregás, su stock se suma otra vez. "
                               "Agregalo solo si es otro renglón de verdad.")

                # Tanda 12: mercadería que quizás ya se sumó a mano al recibirla.
                descuentos = elegir_descuentos_suma_a_mano(
                    [(i["articulo_id"], i["cantidad"], i["nombre"]) for i in st.session_state.items_factura],
                    "manual")
                for idx, item in enumerate(st.session_state.items_factura):
                    item["descontar_ya_sumado"] = descuentos.get(idx, 0)

                total_factura = sum(i["cantidad"] * i["costo_unitario"] for i in st.session_state.items_factura)
                st.metric("Total factura", pesos_venta(total_factura))

                colA, colB = st.columns(2)
                etiqueta_guardar = (f"➕ Agregar a la factura #{agregar_a}" if agregar_a is not None
                                    else "💾 Guardar factura")
                if colA.button(etiqueta_guardar, type="primary"):
                    try:
                        if agregar_a is not None:
                            avisos_reprecio = db.agregar_items_factura(agregar_a, st.session_state.items_factura)
                            mensaje = (f"{len(st.session_state.items_factura)} ítem(s) agregado(s) a la factura "
                                       f"#{agregar_a}. Stock y costos actualizados.")
                            st.session_state.pop("fac_agregar_a", None)
                        else:
                            _, avisos_reprecio = db.crear_factura(
                                int(proveedor_id), numero_factura, fecha_factura,
                                st.session_state.items_factura, origen="manual"
                            )
                            mensaje = "Factura guardada. Stock y costos actualizados."
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        st.session_state.items_factura = []
                        avisar("success", mensaje)
                        for aviso in avisos_reprecio:
                            avisar("warning", aviso)
                        st.rerun()
                if colB.button("🗑️ Vaciar ítems"):
                    st.session_state.items_factura = []
                    st.rerun()


    st.divider()
    st.subheader("📋 Facturas cargadas")
    facturas = db.listar_facturas()
    if facturas.empty:
        st.info("Todavía no hay facturas cargadas.")
    else:
        st.dataframe(
            facturas,
            column_config={
                "id": "N° en la app", "proveedor": "Proveedor", "numero": "Número",
                "fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"), "origen": "Origen",
                "total": st.column_config.NumberColumn("Total s/IVA ($)", format=FMT_PESOS),
            },
            hide_index=True, width="stretch",
        )
        c1, c2 = st.columns([2, 1])
        factura_a_borrar = c1.selectbox(
            "Factura", facturas["id"],
            format_func=lambda x: f"#{x} — {facturas[facturas['id']==x]['proveedor'].values[0]} — {facturas[facturas['id']==x]['fecha'].values[0]}",
            key="sel_factura_borrar"
        )
        with st.expander("Ver ítems de esta factura"):
            st.dataframe(
                db.detalle_de_factura(int(factura_a_borrar)).drop(columns=["id"]),
                column_config={
                    "articulo": "Artículo", "cantidad": st.column_config.NumberColumn("Cantidad", format="%g"),
                    "costo_unitario": st.column_config.NumberColumn("Costo unitario s/IVA ($)", format=FMT_PESOS),
                    "subtotal": st.column_config.NumberColumn("Subtotal ($)", format=FMT_PESOS),
                },
                hide_index=True, width="stretch")

        def _cb_agregar_a_factura(fid=int(factura_a_borrar)):
            st.session_state["fac_agregar_a"] = fid
            st.session_state["modo_cargar_factura"] = "✍️ A mano"

        c1.button("➕ Agregar ítems a esta factura", on_click=_cb_agregar_a_factura,
                  help="Para renglones que faltaron al cargarla: se suman a esta misma factura (no se crea otra).")
        if st.session_state.get("fac_agregar_a") == int(factura_a_borrar):
            c1.success("⬆️ Listo: subí hasta la carga manual, ya está preparada para sumar ítems a esta factura.")
        st.caption("Al eliminar, se resta del stock lo que esta factura había sumado.")
        if c2.button("🗑️ Eliminar factura", type="secondary"):
            fila_f = facturas[facturas["id"] == factura_a_borrar].iloc[0]
            confirmar_eliminacion(
                f"la factura #{int(factura_a_borrar)} de {fila_f['proveedor']}",
                "Se resta del stock lo que esta factura había sumado. El costo de los artículos NO vuelve "
                "al anterior: si era la última compra, revisá el precio a mano. No se puede deshacer "
                "(queda registrado en el Historial de cambios).",
                lambda: db.eliminar_factura(int(factura_a_borrar)),
                "Factura eliminada y stock ajustado.",
            )

# ============================================================
# FRACCIONAR (abrir una bolsa cerrada -> producto suelto)
# ============================================================
elif pagina == "⚖️ Fraccionar":
    st.title("⚖️ Fraccionar")
    st.caption(
        "Convertí una bolsa cerrada en el producto suelto que vendés por kg o por paquetitos. "
        "El costo del producto suelto se recalcula solo a partir del costo actual de la bolsa."
    )

    bolsas = db.listar_articulos_bolsa()
    if bolsas.empty:
        st.info("Todavía no hay artículos en unidades enteras para fraccionar. Creá uno en 'Artículos'.")
    else:
        bolsa_id = st.selectbox(
            "Bolsa a abrir", bolsas["id"],
            format_func=etiquetador(bolsas),
            key="sel_bolsa_fraccionar"
        )
        bolsa = bolsas[bolsas["id"] == bolsa_id].iloc[0]
        derivado = db.obtener_derivado(int(bolsa_id))

        if derivado is None:
            st.info(
                "Esta bolsa todavía no tiene un producto suelto vinculado. Creá uno abajo — "
                "no hace falta ir a 'Artículos', queda todo cargado desde acá."
            )
            with st.expander("➕ Crear producto fraccionado para esta bolsa", expanded=True):
                with st.form("nuevo_derivado", enter_to_submit=False):
                    nombre_derivado = st.text_input(
                        "Nombre del producto suelto",
                        value=f"{bolsa['nombre']} (suelto)"
                    )
                    tipo = st.radio(
                        "¿Cómo se vende suelto?",
                        ["kg", "paquetito"],
                        format_func=lambda t: "Por peso (kg) — ej: fiambres, quesos" if t == "kg"
                        else "Por paquetitos (unidades) — ej: golosinas, snacks chicos",
                        horizontal=True
                    )
                    factor_label = "¿Cuánto pesa la bolsa, en kg?" if tipo == "kg" else "¿Cuántos paquetitos trae la bolsa?"
                    factor_fraccion = st.number_input(
                        factor_label, min_value=0.0,
                        step=0.01 if tipo == "kg" else 1.0,
                        value=0.0
                    )
                    st.caption(
                        "💡 El costo de venta de este producto se calcula solo, dividiendo el costo "
                        "de la bolsa por este factor. Se actualiza automáticamente cada vez que "
                        "cambie el costo de la bolsa — no se carga a mano."
                    )
                    if st.form_submit_button("Crear producto fraccionado"):
                        if not nombre_derivado:
                            st.error("El nombre es obligatorio.")
                        elif factor_fraccion <= 0:
                            st.error(
                                "Ingresá cuánto pesa la bolsa (o cuántos paquetitos trae) para poder "
                                "calcular el costo del producto suelto."
                            )
                        else:
                            marca_bolsa = bolsa.get("marca")
                            categoria_bolsa = bolsa.get("categoria")
                            db.crear_producto_fraccionado(
                                int(bolsa_id), nombre_derivado, tipo, factor_fraccion,
                                marca=marca_bolsa if pd.notna(marca_bolsa) else None,
                                categoria=categoria_bolsa if pd.notna(categoria_bolsa) else None
                            )
                            avisar("success", f"Producto '{nombre_derivado}' creado.")
                            st.rerun()
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Stock de bolsas cerradas", f"{bolsa['stock_actual']:.0f}")
            c2.metric("Costo actual por bolsa", pesos_venta(float(bolsa['costo_compra_actual'])))
            with c3:
                st.markdown("**Producto suelto vinculado**")
                unidad_txt = "kg" if derivado["unidad_medida"] == "kg" else "u."
                st.caption(f"{derivado['nombre']}  ·  stock actual: {derivado['stock_actual']:.2f} {unidad_txt}")

            st.divider()

            # obtener_derivado() devuelve un dict crudo (vía SQLAlchemy .mappings()),
            # a diferencia de bolsa (que viene de un DataFrame de pandas): sus campos
            # numéricos llegan como Decimal, no como float, y Decimal + float rompe.
            # Se castean acá todos los que se usan en cuentas más abajo.
            stock_bolsa = float(bolsa["stock_actual"])
            costo_bolsa = float(bolsa["costo_compra_actual"])
            stock_derivado = float(derivado["stock_actual"])
            factor = float(derivado["factor_fraccion"])

            cantidad_bolsas = st.number_input(
                "¿Cuántas bolsas vas a abrir?", min_value=1.0, value=1.0, step=1.0, key="cantidad_a_fraccionar"
            )

            cantidad_generada = cantidad_bolsas * factor
            costo_derivado_nuevo = costo_bolsa / factor if factor else 0.0
            precio_venta_sin_iva = costo_derivado_nuevo * (1 + float(bolsa["porcentaje_ganancia"]) / 100)
            iva_bolsa = (float(bolsa["costo_venta_con_iva"]) / float(bolsa["costo_venta_sin_iva"]) - 1) if bolsa["costo_venta_sin_iva"] else 0
            precio_venta_con_iva = precio_venta_sin_iva * (1 + iva_bolsa)

            with st.container(border=True):
                st.markdown("**Vista previa del resultado**")
                unidad_txt = "kg" if derivado["unidad_medida"] == "kg" else "u."
                cc1, cc2 = st.columns(2)
                cc1.write(f"Bolsas cerradas: **{stock_bolsa:.0f} → {stock_bolsa - cantidad_bolsas:.0f}**")
                cc2.write(f"{derivado['nombre']}: **{stock_derivado:.2f} → {stock_derivado + cantidad_generada:.2f} {unidad_txt}**")
                st.caption(f"Nuevo costo de compra: {md_pesos(costo_derivado_nuevo)} por {unidad_txt}  ·  Precio de venta c/IVA: {md_pesos(precio_venta_con_iva)} por {unidad_txt}")

            if st.button("🔓 Confirmar fraccionamiento", type="primary"):
                if cantidad_bolsas <= 0:
                    st.error("La cantidad debe ser mayor a 0.")
                else:
                    try:
                        _, aviso = db.fraccionar(int(bolsa_id), cantidad_bolsas)
                        avisar("success", "Fraccionamiento registrado. Stock y costo actualizados.")
                        if aviso:
                            avisar("warning", aviso)
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

    st.divider()
    st.subheader("🔁 Últimos fraccionamientos")
    fraccionamientos = db.listar_fraccionamientos()
    if fraccionamientos.empty:
        st.info("Todavía no se hizo ningún fraccionamiento.")
    else:
        st.dataframe(
            fraccionamientos.drop(columns=["id"]),
            column_config={
                "bolsa": "Bolsa abierta", "cantidad_bolsas": st.column_config.NumberColumn("Bolsas", format="%g"),
                "producto_suelto": "Producto suelto",
                "cantidad_generada": st.column_config.NumberColumn("Cantidad generada", format="%g"),
                "unidad_medida": "Unidad", "fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
            },
            hide_index=True, width="stretch")

# ============================================================
# PROMOCIONES
# ============================================================
elif pagina == "🎁 Promociones":
    st.title("🎁 Promociones")
    st.caption(
        "Una promoción es un combo de varios artículos a un precio fijo. Se vende "
        "igual que cualquier producto, desde 'Registrar Venta'. Hay dos formas de "
        "manejar su stock: 'armado físico' (la armás vos, tiene su propio contador) "
        "o 'combo virtual' (comparte el stock con los artículos originales, sin armar)."
    )

    tab_crear, tab_editar, tab_armar, tab_listado = st.tabs(
        ["➕ Crear promoción", "✏️ Editar promoción", "🔧 Armar / Renovar", "📋 Listado"])

    articulos_todos = db.listar_articulos()
    articulos_no_promo = articulos_todos[articulos_todos["categoria"] != db.CATEGORIA_PROMOCION]

    with tab_crear:
        if articulos_no_promo.empty:
            st.info("Primero cargá artículos en 'Artículos' para poder combinarlos en una promoción.")
        else:
            nombre_promo = st.text_input("Nombre de la promoción", placeholder="ej: Combo Fernet + Coca 1.5L")

            tipo_promo = st.radio(
                "¿Cómo se maneja el stock de esta promoción?",
                ["virtual", "fisica"],
                format_func=lambda t: (
                    "Combo virtual — comparte el stock con los artículos originales, se vende directo (ej: Coca + Fernet)"
                    if t == "virtual" else
                    "Armado físico — la armás vos con 'Armar / Renovar', tiene su propio stock (ej: una canasta pre-armada)"
                ),
                key="promo_tipo_sel"
            )

            if "promo_componentes" not in st.session_state:
                st.session_state.promo_componentes = []

            st.markdown("**Artículos que componen la promoción**")
            c1, c2, c3 = st.columns([3, 1, 1])
            comp_articulo_id = c1.selectbox(
                "Artículo", articulos_no_promo["id"],
                format_func=etiquetador(articulos_no_promo),
                key="promo_sel_componente"
            )
            art_info_sel = articulos_no_promo[articulos_no_promo["id"] == comp_articulo_id].iloc[0]
            componente_es_kg = art_info_sel["unidad_medida"] == "kg"
            if componente_es_kg:
                comp_gramos = c2.number_input(
                    "Gramos por combo", min_value=1, value=200, step=50, key="promo_gramos_componente"
                )
                comp_cantidad = comp_gramos / 1000.0
            else:
                comp_cantidad = c2.number_input(
                    "Cantidad por combo", min_value=0.01, value=1.0, step=1.0, key="promo_cant_componente"
                )
            if c3.button("➕ Agregar", key="promo_agregar_componente"):
                art_info = articulos_no_promo[articulos_no_promo["id"] == comp_articulo_id].iloc[0]
                existente = next(
                    (c for c in st.session_state.promo_componentes if c["articulo_id"] == int(comp_articulo_id)), None
                )
                if existente:
                    existente["cantidad_por_unidad"] += comp_cantidad
                else:
                    st.session_state.promo_componentes.append({
                        "articulo_id": int(comp_articulo_id),
                        "nombre": formatear_articulo(articulos_no_promo, comp_articulo_id),
                        "cantidad_por_unidad": float(comp_cantidad),
                        "costo_unitario": float(art_info["costo_compra_actual"]),
                        "unidad_medida": art_info["unidad_medida"]
                    })
                st.rerun()

            if st.session_state.promo_componentes:
                costo_total_combo = 0.0
                for idx, comp in enumerate(st.session_state.promo_componentes):
                    subtotal = comp["cantidad_por_unidad"] * comp["costo_unitario"]
                    costo_total_combo += subtotal
                    cantidad_txt = (
                        f"{comp['cantidad_por_unidad'] * 1000:.0f} g" if comp.get("unidad_medida") == "kg"
                        else f"{comp['cantidad_por_unidad']:g} u."
                    )
                    cc1, cc2, cc3 = st.columns([3, 2, 1])
                    cc1.write(f"**{comp['nombre']}**")
                    cc2.write(f"{cantidad_txt} · costo {md_pesos(subtotal)}")
                    if cc3.button("✕", key=f"promo_quitar_{idx}"):
                        st.session_state.promo_componentes.pop(idx)
                        st.rerun()

                st.caption(f"Costo total del combo (según costo de compra actual de cada componente): {md_pesos(costo_total_combo)}")

                precio_venta_promo = st.number_input(
                    "Precio de venta del combo (con IVA)", min_value=0.0,
                    value=round(costo_total_combo * 1.4 * 1.21, 2), step=10.0,
                    help="El margen de ganancia de la promoción se calcula solo, a partir de este precio."
                )

                if st.button("✅ Crear promoción", type="primary"):
                    if not nombre_promo:
                        st.error("Ponele un nombre a la promoción.")
                    else:
                        db.crear_promocion(
                            nombre_promo, precio_venta_promo, st.session_state.promo_componentes,
                            tipo=tipo_promo
                        )
                        st.session_state.promo_componentes = []
                        if tipo_promo == "virtual":
                            avisar("success", f"Promoción '{nombre_promo}' creada. Ya se puede vender desde 'Registrar Venta'.")
                        else:
                            avisar("success", f"Promoción '{nombre_promo}' creada. Ahora armá stock en la pestaña 'Armar / Renovar'.")
                        st.rerun()
            else:
                st.info("Agregá al menos un artículo componente arriba.")

    with tab_editar:
        promos_editar = db.listar_promociones()
        if promos_editar.empty:
            st.info("Todavía no creaste ninguna promoción.")
        else:
            pe_id = int(st.selectbox(
                "Promoción a editar", promos_editar["id"],
                format_func=lambda x: promos_editar.loc[promos_editar["id"] == x, "nombre"].values[0]
                + ("" if promos_editar.loc[promos_editar["id"] == x, "activo"].values[0] else " (inactiva)"),
                key="pe_sel"
            ))
            promo_e = promos_editar[promos_editar["id"] == pe_id].iloc[0]
            # Se versionan las claves: al cambiar de promoción o después de
            # guardar, todo se vuelve a leer de la base (nunca se arrastra lo
            # que quedó en pantalla de otra promoción).
            pe_ver = st.session_state.get("pe_ver", 0)
            pe_sufijo = f"{pe_id}_{pe_ver}"
            catalogo_e = db.listar_articulos(incluir_inactivos=True)
            costos_e = dict(zip(catalogo_e["id"].astype(int), catalogo_e["costo_compra_actual"].astype(float)))
            activos_e = set(catalogo_e.loc[catalogo_e["activo"], "id"].astype(int))
            if st.session_state.get("pe_cargada") != pe_sufijo:
                comps_db = db.obtener_componentes_promocion(pe_id)
                st.session_state.pe_componentes = [{
                    "articulo_id": int(r["articulo_id"]),
                    "nombre": r["nombre"] + (f" ({r['marca']})" if pd.notna(r["marca"]) and r["marca"] else ""),
                    "cantidad_por_unidad": float(r["cantidad_por_unidad"]),
                    "unidad_medida": r["unidad_medida"],
                } for _, r in comps_db.iterrows()]
                st.session_state.pe_cargada = pe_sufijo
            comps_e = st.session_state.pe_componentes

            nombre_e = st.text_input("Nombre", value=promo_e["nombre"], key=f"pe_nombre_{pe_sufijo}")
            tipo_actual = promo_e["promo_tipo"] or "fisica"
            tipo_e = st.radio(
                "¿Cómo se maneja el stock de esta promoción?", ["virtual", "fisica"],
                index=0 if tipo_actual == "virtual" else 1,
                format_func=lambda t: (
                    "Combo virtual — comparte el stock con los artículos originales, se vende directo"
                    if t == "virtual" else
                    "Armado físico — la armás vos con 'Armar / Renovar', tiene su propio stock"
                ),
                key=f"pe_tipo_{pe_sufijo}"
            )
            armadas = float(promo_e["stock_actual"] or 0) if tipo_actual == "fisica" else 0.0
            devolver_e = False
            if tipo_e != tipo_actual:
                if tipo_actual == "fisica" and armadas > 0:
                    guardados = db.obtener_componentes_promocion(pe_id)
                    vuelven = ", ".join(
                        f"{r['nombre']} +{float(r['cantidad_por_unidad']) * armadas * 1000:g} g"
                        if r["unidad_medida"] == "kg" else
                        f"{r['nombre']} +{float(r['cantidad_por_unidad']) * armadas:g}"
                        for _, r in guardados.iterrows())
                    st.warning(f"⚠️ Hay **{armadas:g}** combo(s) ya armado(s). Al pasar a combo virtual, el "
                               "stock propio de la promoción queda en 0.\n\n"
                               f"Si los desarmás, vuelve al stock (según la composición guardada): {vuelven}. "
                               "Si cambiaste la composición después de armarlos, revisá que coincida.")
                    devolver_e = st.checkbox(
                        f"Desarmar los {armadas:g} combo(s) y devolver sus artículos al stock",
                        value=True, key=f"pe_devolver_{pe_sufijo}",
                        help="Destildalo solo si esos combos ya no existen (se vendieron sin registrar, se tiraron, etc.)."
                    )
                elif tipo_e == "fisica":
                    st.info("Al pasar a armado físico, la promoción arranca con stock 0: armala en 'Armar / Renovar'.")
            elif tipo_actual == "fisica" and armadas > 0:
                st.caption(f"Los {armadas:g} combo(s) ya armados no cambian. Si cambiás la composición, la nueva "
                           "se usa desde el próximo armado.")

            st.markdown("**Artículos que componen la promoción**")
            if not comps_e:
                st.info("La promoción no tiene artículos. Agregá al menos uno abajo.")
            costo_total_e = 0.0
            for comp in list(comps_e):
                es_kg = comp["unidad_medida"] == "kg"
                r1, r2, r3, r4 = st.columns([4, 2, 2, 1], vertical_alignment="center")
                aviso_baja = "" if comp["articulo_id"] in activos_e else " ⛔ dado de baja"
                r1.write(f"**{comp['nombre']}**{aviso_baja}")
                clave_cant = f"pe_cant_{pe_sufijo}_{comp['articulo_id']}"
                if es_kg:
                    gramos = r2.number_input("Gramos", min_value=1, step=50, key=clave_cant,
                                             value=int(round(comp["cantidad_por_unidad"] * 1000)),
                                             label_visibility="collapsed")
                    comp["cantidad_por_unidad"] = gramos / 1000.0
                    r2.caption("gramos por combo")
                else:
                    comp["cantidad_por_unidad"] = float(r2.number_input(
                        "Cantidad", min_value=0.01, step=1.0, key=clave_cant,
                        value=float(comp["cantidad_por_unidad"]), label_visibility="collapsed"))
                    r2.caption("unidades por combo")
                subtotal = comp["cantidad_por_unidad"] * costos_e.get(comp["articulo_id"], 0.0)
                costo_total_e += subtotal
                r3.write(f"costo {md_pesos(subtotal)}")
                if r4.button("✕", key=f"pe_quitar_{pe_sufijo}_{comp['articulo_id']}", help="Quitar de la promoción"):
                    st.session_state.pe_componentes = [c for c in comps_e if c["articulo_id"] != comp["articulo_id"]]
                    st.rerun()

            with st.expander("➕ Agregar un artículo", expanded=not comps_e):
                a1, a2, a3 = st.columns([3, 1, 1], vertical_alignment="bottom")
                nuevo_id = a1.selectbox("Artículo", articulos_no_promo["id"], format_func=etiquetador(articulos_no_promo),
                                        key=f"pe_nuevo_{pe_sufijo}")
                info_nuevo = articulos_no_promo[articulos_no_promo["id"] == nuevo_id].iloc[0]
                if info_nuevo["unidad_medida"] == "kg":
                    nueva_cant = a2.number_input("Gramos", min_value=1, value=200, step=50,
                                                 key=f"pe_nuevo_g_{pe_sufijo}") / 1000.0
                else:
                    nueva_cant = a2.number_input("Cantidad", min_value=0.01, value=1.0, step=1.0,
                                                 key=f"pe_nuevo_c_{pe_sufijo}")
                if a3.button("Agregar", key=f"pe_agregar_{pe_sufijo}"):
                    existente = next((c for c in comps_e if c["articulo_id"] == int(nuevo_id)), None)
                    if existente:
                        existente["cantidad_por_unidad"] += float(nueva_cant)
                        # la cantidad mostrada sale de su widget: se borra para que tome la suma
                        st.session_state.pop(f"pe_cant_{pe_sufijo}_{int(nuevo_id)}", None)
                    else:
                        comps_e.append({
                            "articulo_id": int(nuevo_id),
                            "nombre": formatear_articulo(articulos_no_promo, nuevo_id),
                            "cantidad_por_unidad": float(nueva_cant),
                            "unidad_medida": info_nuevo["unidad_medida"],
                        })
                    st.rerun()

            iva_e = float(promo_e["iva"])
            st.caption(f"Costo del combo con los costos de compra de hoy: **{md_pesos(costo_total_e)}** "
                       f"(antes {md_pesos(float(promo_e['costo_compra_actual']))}).")
            precio_e = st.number_input(
                "Precio de venta del combo (con IVA)", min_value=0.0, step=50.0,
                value=float(promo_e["costo_venta_con_iva"]), key=f"pe_precio_{pe_sufijo}",
                help="Si no lo tocás, el cliente sigue pagando lo mismo: el % de ganancia se recalcula con el costo nuevo."
            )
            if costo_total_e > 0:
                margen_e = precio_e / (costo_total_e * (1 + iva_e)) - 1
                if margen_e < 0:
                    st.warning(
                        f"⚠️ Con ese precio el combo se vende por debajo del costo. Se guardaría con 0% de "
                        f"ganancia, o sea a {md_pesos(costo_total_e * (1 + iva_e))}."
                    )
                else:
                    st.caption(f"Ganancia sobre el costo: **{margen_e:.1%}**.")

            if st.button("💾 Guardar cambios", type="primary", key=f"pe_guardar_{pe_sufijo}"):
                if not nombre_e.strip():
                    st.error("Ponele un nombre a la promoción.")
                elif not comps_e:
                    st.error("Una promoción necesita al menos un artículo.")
                else:
                    try:
                        cambio_tipo = db.editar_promocion(
                            pe_id, tipo_e, precio_e,
                            [{"articulo_id": c["articulo_id"], "cantidad_por_unidad": c["cantidad_por_unidad"]}
                             for c in comps_e],
                            nuevo_nombre=nombre_e.strip(), devolver_armadas=devolver_e,
                        )
                        mensaje = f"Promoción '{nombre_e.strip()}' guardada."
                        if cambio_tipo and tipo_e == "fisica":
                            mensaje += " Ahora es de armado físico: armala en 'Armar / Renovar'."
                        elif cambio_tipo and armadas > 0:
                            mensaje += (" Los combos armados se desarmaron y sus artículos volvieron al stock."
                                        if devolver_e else " Los combos armados se dieron por perdidos.")
                        avisar("success", mensaje)
                        st.session_state.pe_ver = pe_ver + 1
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
            st.caption("Las ventas que ya se hicieron de esta promoción no cambian: cada venta guarda con qué "
                       "artículos se vendió.")

    with tab_armar:
        promociones_todas = db.listar_promociones()
        promociones = promociones_todas[promociones_todas["promo_tipo"] == "fisica"] if not promociones_todas.empty else promociones_todas
        if promociones_todas.empty:
            st.info("Todavía no creaste ninguna promoción.")
        elif promociones.empty:
            st.info(
                "Todas tus promociones son 'combo virtual' -- se venden directo desde "
                "'Registrar Venta' y no necesitan armado."
            )
        else:
            promo_sel = st.selectbox(
                "Promoción", promociones["id"],
                format_func=lambda x: promociones[promociones["id"] == x]["nombre"].values[0],
                key="promo_sel_armar"
            )
            promo_actual = promociones[promociones["id"] == promo_sel].iloc[0]
            componentes_promo = db.obtener_componentes_promocion(int(promo_sel))

            st.metric("Stock actual de la promoción", f"{promo_actual['stock_actual']:.0f} unidades")
            st.markdown("**Composición** (stock disponible de cada componente)")
            st.dataframe(
                componentes_promo[["nombre", "marca", "cantidad_por_unidad", "stock_disponible", "unidad_medida"]],
                column_config={
                    "nombre": "Artículo", "marca": "Marca",
                    "cantidad_por_unidad": st.column_config.NumberColumn("Por combo", format="%g"),
                    "stock_disponible": st.column_config.NumberColumn("Stock", format="%g"),
                    "unidad_medida": "Unidad",
                },
                hide_index=True, width="stretch"
            )

            cantidad_armar = st.number_input("¿Cuántas unidades de la promoción armar?", min_value=1.0, value=1.0, step=1.0)

            if not componentes_promo.empty:
                alcanza_para = min(
                    (float(c["stock_disponible"]) / float(c["cantidad_por_unidad"]))
                    for _, c in componentes_promo.iterrows() if float(c["cantidad_por_unidad"]) > 0
                )
                st.caption(
                    f"Con el stock actual de los componentes alcanza para armar hasta **{alcanza_para:.0f}** "
                    "unidad(es) más sin quedar en negativo -- podés armar de todos modos si ya sabés que "
                    "vas a reponer ese componente pronto."
                )

            if st.button("🔧 Armar / Renovar", type="primary"):
                try:
                    avisos = db.armar_promocion(int(promo_sel), cantidad_armar)
                    avisar("success", f"{cantidad_armar:g} unidad(es) armada(s). Stock de componentes y promoción actualizado.")
                    for aviso in avisos:
                        avisar("warning", aviso)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    with tab_listado:
        promociones = db.listar_promociones()
        if promociones.empty:
            st.info("Todavía no creaste ninguna promoción.")
        else:
            tabla_promos = promociones[["id", "nombre", "promo_tipo", "costo_venta_con_iva", "stock_actual"]].rename(
                columns={"promo_tipo": "Tipo", "costo_venta_con_iva": "Precio de venta", "stock_actual": "Stock"}
            )
            tabla_promos["Tipo"] = tabla_promos["Tipo"].map({"virtual": "Combo virtual", "fisica": "Armado físico"})
            st.dataframe(
                tabla_promos.drop(columns=["id"]),
                column_config={
                    "nombre": "Promoción",
                    "Precio de venta": st.column_config.NumberColumn("Precio de venta ($)", format=FMT_PESOS),
                    "Stock": st.column_config.NumberColumn("Stock", format="%g"),
                },
                hide_index=True, width="stretch")
            st.caption(
                "Para los 'Combo virtual', el Stock es cuántos se pueden vender ahora mismo "
                "con el stock real de sus artículos (se recalcula solo, no hace falta armar)."
            )
            st.divider()
            st.subheader("🗑️ Eliminar una promoción")
            promo_borrar = st.selectbox(
                "Promoción a eliminar", promociones["id"],
                format_func=lambda x: promociones[promociones["id"] == x]["nombre"].values[0],
                key="promo_sel_borrar"
            )
            st.caption("Solo se puede borrar si todavía no se vendió ninguna unidad. Si ya se vendió, "
                       "dala de baja en Artículos → 'Editar artículo'.")
            if st.button("Eliminar promoción", type="secondary"):
                confirmar_eliminacion(
                    f"la promoción {promociones.loc[promociones['id'] == promo_borrar, 'nombre'].values[0]}",
                    "Se borra la promoción y su composición (queda registrado en el Historial de cambios).",
                    lambda: db.eliminar_promocion(int(promo_borrar)),
                    "Promoción eliminada.",
                )

# ============================================================
# REGISTRAR VENTA
# ============================================================
elif pagina == "💵 Registrar Venta":
    st.title("💵 Registrar Venta")

    # Ordenados por uso real (efectivo y transferencia son el 86 % de los tickets).
    MEDIOS_PAGO = ["Efectivo", "Transferencia / MercadoPago", "Tarjeta débito", "Tarjeta crédito", "Otro"]
    # Texto corto para los botones; lo que se GUARDA es siempre el valor de MEDIOS_PAGO.
    MEDIO_CORTO = {"Efectivo": "Efectivo", "Transferencia / MercadoPago": "Transf. / MP",
                   "Tarjeta débito": "Débito", "Tarjeta crédito": "Crédito", "Otro": "Otro"}
    MAS_VENDIDOS_SIN_BUSQUEDA = 24
    VENDER, SOBRANTE, ULTIMAS = "🛒 Vender", "🗑️ Sobrante del día", "📋 Últimas ventas"

    st.markdown("""
    <style>
    .st-key-grilla_venta .nombre { font-weight: 600; font-size: 0.9rem; line-height: 1.25; min-height: 2.5em; }
    .st-key-grilla_venta [data-testid="stCaptionContainer"] { margin-top: -0.4rem; }
    .st-key-carrito_venta_caja .total { font-size: 2.1rem; font-weight: 700; margin: 0.2rem 0 0.4rem 0; }
    .st-key-carrito_venta_caja .renglon { font-size: 0.9rem; }
    </style>
    """, unsafe_allow_html=True)

    # --------------------------------------------------------
    # Carrito — se acumulan ítems y el total, el stock recién se
    # mueve al confirmar toda la venta. El buscador filtra sobre TODO
    # el catálogo; sin texto se muestran los más vendidos.
    # --------------------------------------------------------
    if "carrito_venta" not in st.session_state:
        st.session_state.carrito_venta = []  # lista de dicts: articulo_id, nombre, cantidad, precio_unitario, unidad_medida

    # Solo se dibuja (y se consulta a la base) la sección abierta: antes
    # Sobrante y Últimas ventas / Editar venta corrían en cada clic del carrito.
    seccion_venta = st.segmented_control(
        "Sección", [VENDER, SOBRANTE, ULTIMAS], default=VENDER, key="seccion_venta",
        label_visibility="collapsed",
    ) or VENDER

    # Los botones del carrito usan on_click: el carrito se actualiza ANTES de
    # redibujar, así no hace falta st.rerun() y funciona igual si la corrida es
    # solo del fragmento o de toda la app.
    def _cb_agregar(articulo_id, nombre, precio, unidad_medida, clave_cantidad, divisor, paso):
        cantidad = round(float(st.session_state.get(clave_cantidad) or 0) / divisor, 3)
        if cantidad <= 0:
            return
        existente = next(
            (it for it in st.session_state.carrito_venta if it["articulo_id"] == articulo_id), None
        )
        if existente:
            existente["cantidad"] = round(existente["cantidad"] + cantidad, 3)
            existente["paso_cantidad"] = paso
        else:
            st.session_state.carrito_venta.append({
                "articulo_id": articulo_id,
                "nombre": nombre,
                "cantidad": cantidad,
                "precio_unitario": precio,
                "unidad_medida": unidad_medida,
                "paso_cantidad": paso,
            })

    def _cb_cambiar(idx, delta):
        carrito = st.session_state.carrito_venta
        if idx < len(carrito):
            it = carrito[idx]
            paso = it.get("paso_cantidad", 1.0)
            it["cantidad"] = round(max(it["cantidad"] + delta * paso, paso), 3)

    def _cb_quitar(idx):
        if idx < len(st.session_state.carrito_venta):
            st.session_state.carrito_venta.pop(idx)

    def _cb_vaciar():
        st.session_state.carrito_venta = []

    # @st.fragment: agregar, sumar o quitar del carrito vuelve a correr solo
    # esta parte de la página (buscador, productos y carrito), no la app entera.
    # Al confirmar se recarga la app completa para mostrar el aviso y actualizar
    # el resto.
    @st.fragment
    def _pantalla_vender():
        # Al tocar algo del carrito corre SOLO esta función, sin pasar por el
        # comienzo de la app donde se fija quién atiende. Sin esto, la venta se
        # grababa sin "Atendió" (21/10/2026).
        db.set_persona_actual(PERSONA["id"], PERSONA["nombre"])
        col_carrito, col_productos = st.columns([2, 3], gap="large")

        # ---------------- productos (derecha) ----------------
        with col_productos:
            busqueda_rapida = st.text_input(
                "🔍 Buscar producto por nombre o marca", placeholder="Buscar producto: aceite, queso, Cocinero…",
                key="busqueda_venta_rapida", label_visibility="collapsed",
            )
            if busqueda_rapida:
                productos_a_mostrar = db.buscar_articulos_para_venta(busqueda_rapida)
                if productos_a_mostrar.empty:
                    st.info("Ningún producto del catálogo coincide con esa búsqueda.")
                else:
                    st.caption(f"{len(productos_a_mostrar)} resultado(s) para “{busqueda_rapida}”")
            else:
                # Sin texto: solo los más vendidos (atajo sin escribir). La búsqueda
                # sigue llegando a todo el catálogo; si todavía no hay ventas (base
                # nueva), se muestra el catálogo.
                productos_a_mostrar = db.articulos_mas_vendidos(limite=MAS_VENDIDOS_SIN_BUSQUEDA)
                if not productos_a_mostrar.empty:
                    st.caption(f"⭐ Los {len(productos_a_mostrar)} más vendidos de los últimos 30 días. "
                               "Para cualquier otro producto, escribí en el buscador.")
                else:
                    productos_a_mostrar = db.listar_articulos()
                    if productos_a_mostrar.empty:
                        st.info("Todavía no hay artículos cargados. Creá el primero en 'Artículos' → Nuevo artículo.")

            etiqueta_prod = etiquetador(productos_a_mostrar)
            with st.container(height=640, border=False, key="grilla_venta"):
                cols = st.columns(3)
                for i, (_, art) in enumerate(productos_a_mostrar.iterrows()):
                    aid = int(art["id"])
                    nombre = etiqueta_prod(aid)
                    precio = float(art["costo_venta_con_iva"])
                    stock = float(art["stock_actual"])
                    es_kg = art["unidad_medida"] == "kg"
                    tiene_peso_bolsa = (
                        art["unidad_medida"] == "unidad"
                        and pd.notna(art.get("peso_bolsa_gramos"))
                        and float(art.get("peso_bolsa_gramos") or 0) > 0
                    )
                    # Un artículo sin precio cargado se vendería a $0: se bloquea.
                    sin_precio = precio <= 0
                    if es_kg:
                        stock_txt = f"stock {stock:.2f} kg"
                    elif tiene_peso_bolsa:
                        stock_txt = f"stock {stock:g} u. · 1 u. = {float(art['peso_bolsa_gramos']):.0f} g"
                    else:
                        stock_txt = f"stock {stock:g}"
                    if stock < 0:
                        stock_txt = "⚠️ " + stock_txt
                    precio_txt = ("⚠️ sin precio" if sin_precio
                                  else pesos_venta(precio) + (" /kg" if es_kg else " c/u"))

                    with cols[i % 3]:
                        with st.container(border=True):
                            nombre_html = nombre.replace("*", "&#42;")  # hay nombres con asteriscos
                            st.markdown(f"<div class='nombre'>{'⚖️ ' if es_kg else ''}{nombre_html}</div>",
                                        unsafe_allow_html=True)
                            st.caption(f"{precio_txt} · {stock_txt}")

                            # paso_cantidad: de a cuánto suman/restan los botones del
                            # carrito para este artículo (gramos, fracción de bolsa o unidad).
                            en_gramos = es_kg
                            if tiene_peso_bolsa:
                                modo = st.segmented_control(
                                    "Cómo se vende", ["entera", "fraccion"], default="entera",
                                    format_func={"entera": "Bolsa entera", "fraccion": "Por fracción"}.get,
                                    key=f"modo_venta_{aid}", label_visibility="collapsed",
                                ) or "entera"
                                en_gramos = modo == "fraccion"
                            if en_gramos:
                                c_cant, c_u, c_btn = st.columns([2.3, 0.35, 1], vertical_alignment="center")
                                c_u.markdown("**g**")
                                clave_cantidad = f"gramos_frac_{aid}" if tiene_peso_bolsa else f"gramos_{aid}"
                                c_cant.number_input("Gramos", min_value=10, value=250, step=50,
                                                    key=clave_cantidad, label_visibility="collapsed")
                                if tiene_peso_bolsa:
                                    peso_bolsa = float(art["peso_bolsa_gramos"])
                                    divisor, paso_cantidad = peso_bolsa, 50 / peso_bolsa
                                else:
                                    divisor, paso_cantidad = 1000.0, 0.050
                            else:
                                c_cant, c_btn = st.columns([2.6, 1], vertical_alignment="bottom")
                                clave_cantidad = f"unid_{aid}"
                                c_cant.number_input("Cantidad", min_value=1, value=1, step=1,
                                                    key=clave_cantidad, label_visibility="collapsed")
                                divisor, paso_cantidad = 1.0, 1.0
                            c_btn.button(
                                "", icon=":material/add_shopping_cart:", key=f"add_{aid}",
                                type="primary", width="stretch", disabled=sin_precio,
                                help=("Sin precio cargado: corregilo en Artículos antes de venderlo."
                                      if sin_precio else "Agregar al carrito"),
                                on_click=_cb_agregar,
                                args=(aid, nombre, precio, art["unidad_medida"], clave_cantidad, divisor, paso_cantidad),
                            )

        # ---------------- carrito (izquierda) ----------------
        with col_carrito:
            with st.container(border=True, key="carrito_venta_caja"):
                st.markdown("#### 🛒 Carrito")
                carrito = st.session_state.carrito_venta
                if not carrito:
                    st.caption("Vacío. Agregá productos de la derecha.")
                total_carrito = 0.0
                for idx, item in enumerate(carrito):
                    subtotal = item["cantidad"] * item["precio_unitario"]
                    total_carrito += subtotal
                    es_fraccion_de_unidad = (item["unidad_medida"] == "unidad"
                                             and item["cantidad"] != int(item["cantidad"]))
                    if item["unidad_medida"] == "kg":
                        cantidad_txt = f"{item['cantidad'] * 1000:.0f} g"
                    elif es_fraccion_de_unidad:
                        cantidad_txt = f"{item['cantidad']:.3f} u."
                    else:
                        cantidad_txt = f"{item['cantidad']:.0f}"
                    c1, c_menos, c_cant, c_mas, c2, c3 = st.columns([4.2, 1, 1.6, 1, 2.4, 1],
                                                                    vertical_alignment="center")
                    c1.markdown(f"<div class='renglon'>{item['nombre'].replace('*', '&#42;')}</div>",
                                unsafe_allow_html=True)
                    c_menos.button("", icon=":material/remove:", key=f"menos_{idx}", help="Uno menos",
                                   on_click=_cb_cambiar, args=(idx, -1))
                    c_cant.markdown(f"<div style='text-align:center'>{cantidad_txt}</div>", unsafe_allow_html=True)
                    c_mas.button("", icon=":material/add:", key=f"mas_{idx}", help="Uno más",
                                 on_click=_cb_cambiar, args=(idx, +1))
                    c2.markdown(f"<div style='text-align:right'><b>{pesos_venta(subtotal)}</b></div>",
                                unsafe_allow_html=True)
                    c3.button("", icon=":material/delete:", key=f"quitar_{idx}", help="Quitar del carrito",
                              on_click=_cb_quitar, args=(idx,))

                st.divider()
                st.markdown(f"<div class='total'>Total {pesos_venta(total_carrito)}</div>", unsafe_allow_html=True)

                # Consumo interno y fecha están plegados abajo; se leen antes para
                # que el resto del carrito ya se adapte.
                es_consumo_interno = bool(st.session_state.get("chk_consumo_interno", False))
                fecha_carrito = st.session_state.get("fecha_carrito") or hoy_argentina()

                medio_pago_carrito = "Consumo interno"
                if es_consumo_interno:
                    st.caption("📦 Consumo interno: no lleva medio de pago y no suma como ingreso.")
                else:
                    medio_pago_carrito = st.segmented_control(
                        "Medio de pago", MEDIOS_PAGO, default="Efectivo", format_func=MEDIO_CORTO.get,
                        key="medio_carrito",
                    )
                    if medio_pago_carrito == "Efectivo":
                        p1, p2 = st.columns(2, vertical_alignment="bottom")
                        # la clave cambia con cada venta confirmada: así el campo vuelve a quedar vacío
                        paga_con = p1.number_input("Paga con", min_value=0, value=None, step=500,
                                                   placeholder="¿Con cuánto paga?",
                                                   key=f"paga_con_{st.session_state.get('ventas_confirmadas', 0)}")
                        if paga_con is not None and carrito:
                            p2.metric("Vuelto", pesos_venta(paga_con - total_carrito)
                                      if paga_con >= total_carrito else "Falta plata")
                if fecha_carrito != hoy_argentina():
                    st.warning(f"Se va a registrar con fecha **{fecha_carrito:%d/%m/%Y}** (no es hoy).")

                # Tanda 12: cantidades fuera de lo normal piden confirmación (caso real:
                # 140 "unidades" de Paleta por tipear gramos, $1.960.000).
                revisar = db.controlar_cantidades(
                    carrito, cantidades_habituales_cache(tuple(sorted({int(i["articulo_id"]) for i in carrito})))
                ) if carrito else []
                cantidades_ok = True
                if revisar:
                    st.warning("⚠️ **Revisá antes de confirmar:**\n\n" + "\n".join(f"- {r}" for r in revisar))
                    firma = "|".join(f"{i['articulo_id']}:{i['cantidad']}" for i in carrito)
                    cantidades_ok = st.checkbox(
                        "Las cantidades están bien", key=f"ok_cantidades_{hashlib.md5(firma.encode()).hexdigest()[:10]}"
                    )
                etiqueta_boton = "✅ CONFIRMAR CONSUMO INTERNO" if es_consumo_interno else "✅ CONFIRMAR VENTA"
                if st.button(etiqueta_boton, type="primary", width="stretch",
                             disabled=not carrito or medio_pago_carrito is None or not cantidades_ok,
                             key="btn_confirmar_venta"):
                    items_para_confirmar = [
                        {
                            "articulo_id": it["articulo_id"], "cantidad": it["cantidad"],
                            "precio_unitario": it["precio_unitario"], "nombre": it["nombre"]
                        }
                        for it in st.session_state.carrito_venta
                    ]
                    try:
                        if es_consumo_interno:
                            ticket_id, costo, avisos = db.confirmar_consumo_interno(items_para_confirmar, fecha_carrito)
                            st.session_state.carrito_venta = []
                            avisar("success", f"Consumo interno #{ticket_id} registrado. Costo {pesos_venta(costo)} cargado como gasto. Stock actualizado.")
                        else:
                            ticket_id, total, avisos = db.confirmar_venta_carrito(items_para_confirmar, fecha_carrito, medio_pago_carrito)
                            st.session_state.carrito_venta = []
                            avisar("success", f"Venta #{ticket_id} confirmada por {pesos_venta(total)} ({medio_pago_carrito}). Stock actualizado.")
                            # Tanda 21: compra grande -> aviso del juego de las monedas (reemplaza al anterior)
                            st.session_state.pop("juego_monedas", None)
                            try:
                                if fecha_carrito == hoy_argentina() and desafio.juego_disponible(ticket_id):
                                    st.session_state["juego_monedas"] = {"ticket": ticket_id, "total": total}
                            except Exception:
                                pass  # el juego nunca traba una venta
                        for aviso in avisos:
                            avisar("warning", aviso)
                        st.session_state["ventas_confirmadas"] = st.session_state.get("ventas_confirmadas", 0) + 1
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
                    except Exception as e:
                        # La venta va en una sola transacción: si algo falló (por ejemplo,
                        # se cortó la conexión), no quedó nada grabado y el carrito sigue igual.
                        st.error(
                            "⚠️ No se pudo registrar: **no se grabó nada** y el carrito sigue igual. "
                            f"Probá de nuevo en unos segundos. ({type(e).__name__})"
                        )
                if carrito:
                    st.button("Vaciar carrito", icon=":material/remove_shopping_cart:", width="stretch",
                              key="btn_vaciar_carrito", on_click=_cb_vaciar)

                with st.expander("Otra fecha · consumo interno"):
                    st.date_input("Fecha de la venta", value=hoy_argentina(), key="fecha_carrito",
                                  format="DD/MM/YYYY")
                    st.checkbox(
                        "📦 Esto es consumo interno (no se vendió -- ej: se lo llevó el dueño, se rompió, se usó para el local)",
                        key="chk_consumo_interno",
                        help="Descuenta el stock igual que una venta, pero no suma como ingreso: se registra "
                             "en cambio como un gasto por el costo real de lo consumido."
                    )
                st.caption(f"Se registra a nombre de **{PERSONA['nombre']}** · la venta se graba entera o no se graba.")

    if seccion_venta == VENDER:
        juego = st.session_state.get("juego_monedas")
        if juego:
            # Tanda 21: Desafío Recreo, "Recreo con clientes". No frena la venta: si no se
            # contesta, se va con la venta siguiente.
            with st.container(border=True):
                cj1, cj2 = st.columns([1, 6], vertical_alignment="center")
                cj1.markdown(figuritas.svg("monedita", 72, titulo="Monedita"), unsafe_allow_html=True)
                cj2.markdown(f"**¡Compra grande! ¿Jugaron a las monedas?** (venta #{juego['ticket']}, "
                             f"{md_pesos(juego['total'])})")
                cj2.caption("Si el cliente tiró las monedas de chocolate, anotalo: salen del stock como regalo y "
                            "suma para la figurita Monedita.")
                cm1, cm2, cm3 = cj2.columns([1, 1, 1], vertical_alignment="bottom")
                monedas_j = cm1.number_input("Monedas que se llevó", min_value=1.0, step=1.0, format="%g",
                                             value=float(desafio.parametros()["monedas"]), key="juego_monedas_cant")
                if cm2.button("🪙 Sí, jugamos", type="primary", key="juego_si", width="stretch"):
                    try:
                        est = desafio.registrar_juego(PERSONA["id"], juego["ticket"], monedas_j, hoy_argentina())
                        avisar("success", f"¡Juego anotado! {monedas_j:g} monedas de regalo"
                                          + (f" · +{est} estrellitas" if est else "") + ".")
                    except ValueError as e:
                        avisar("warning", str(e))
                    st.session_state.pop("juego_monedas", None)
                    st.rerun()
                if cm3.button("Hoy no", key="juego_no", width="stretch"):
                    st.session_state.pop("juego_monedas", None)
                    st.rerun()
        _pantalla_vender()
    elif seccion_venta == SOBRANTE:
        # --------------------------------------------------------
        # SOBRANTE DEL DÍA (pan y perecederos que se regalan o se tiran)
        # Baja stock como consumo interno (medio 'Sobrante'), registra el costo
        # como gasto "Sobrante" y, para los artículos de compra diaria, el
        # pronóstico lo cuenta como sobrante (no como venta). Las compras se
        # siguen cargando por Cargar Factura.
        # --------------------------------------------------------
        st.subheader("🗑️ Sobrante del día")
        # Tanda 18: dos motivos. "Sobró" (pan del día que se regaló o tiró) y "Venció"
        # (cualquier artículo que se tiró por vencido). Los dos bajan stock y van a costo
        # como gasto; lo vencido además se mide aparte como merma y no cuenta como venta
        # para el pronóstico ni como movimiento para el stock inmovilizado.
        MOT_SOBRO, MOT_VENCIO = "Sobró (se regaló o se tiró)", "Venció (se tiró)"
        motivo_sob = st.radio("Motivo", [MOT_SOBRO, MOT_VENCIO], horizontal=True, key="sob_motivo")
        es_vencido = motivo_sob == MOT_VENCIO
        if es_vencido:
            st.caption("Lo que se tiró porque venció. Se descuenta del stock y queda como gasto \"Vencido\" (merma). "
                       "No cuenta como venta para el pronóstico.")
        else:
            st.caption("Lo que quedó sin vender y **ya** se regaló o se tiró (ej.: el pan del día). No es una venta: "
                       "se descuenta del stock y queda como gasto \"Sobrante\". Si todavía está en la estantería, "
                       "no lo cargues: sigue siendo stock.")
        arts_sob = db.listar_articulos()
        # Tanda 16: los fraccionados también (ej.: el "pan de ayer" que se pasa en
        # Fraccionar y al día siguiente se tira); van arriba junto a su artículo de
        # compra diaria. Para "Venció" van arriba los que tienen días disponibles.
        arts_sob = arts_sob[arts_sob["categoria"] != "Promocion"].copy()
        if es_vencido:
            arts_sob["_diaria"] = arts_sob["id"].isin(list(db.dias_disponibles(arts_sob["id"].tolist())))
            orden_txt = "los que vencen aparecen primero"
        else:
            ids_diaria_sob = db.ids_compra_diaria()
            arts_sob["_diaria"] = (arts_sob["id"].isin(ids_diaria_sob)
                                   | arts_sob["articulo_fraccion_de"].isin(ids_diaria_sob))
            orden_txt = "los de compra diaria aparecen primero"
        arts_sob = arts_sob.sort_values(["_diaria", "nombre"], ascending=[False, True])
        c1, c2 = st.columns([3, 1])
        aid_sob = c1.selectbox(
            "Artículo", arts_sob["id"].tolist(), index=None, placeholder=f"Elegí el artículo ({orden_txt})",
            format_func=lambda i: arts_sob.set_index("id").at[i, "nombre"], key="sob_art",
        )
        if es_vencido:
            fecha_sob = c2.date_input("¿Qué día se tiró?", value=hoy_argentina(), max_value=hoy_argentina(),
                                      key="sob_fecha_venc", format="DD/MM/YYYY")
        else:
            # Tanda 16: antes del mediodía lo que se carga es casi siempre lo que sobró
            # AYER (se tira a la mañana); con "hoy" por defecto quedaba en el día equivocado.
            fecha_def_sob = hoy_argentina() - (timedelta(days=1) if datetime.now(ZONA_ARG).hour < 12 else timedelta(0))
            fecha_sob = c2.date_input("¿De qué día es el sobrante?", value=fecha_def_sob, max_value=hoy_argentina(),
                                      key="sob_fecha", format="DD/MM/YYYY",
                                      help="El día en que se compró y no se vendió, no el día en que lo cargás. "
                                           "Antes del mediodía arranca en ayer.")
        if aid_sob is not None:
            ya = (db.vencido_del_dia(aid_sob, fecha_sob) if es_vencido
                  else db.movimientos_del_dia(aid_sob, fecha_sob)["sobrante"])
            stock_sob = db.stock_actual_por_id().get(int(aid_sob), 0.0)
            if es_vencido:
                lotes_sob = vencimientos_cacheados(str(hoy_argentina()), db.version_datos())
                lotes_sob = (lotes_sob[(lotes_sob["articulo_id"] == int(aid_sob)) & (lotes_sob["dias_para_vencer"] < 0)]
                             if not lotes_sob.empty else lotes_sob)
                st.caption(f"Stock ahora: {stock_sob:g}."
                           + (f" Según las compras y sus fechas, {lotes_sob['quedan'].sum():g} ya deberían haber "
                              "vencido." if not lotes_sob.empty else ""))
            else:
                # contexto del día elegido: con cuánto arrancó, cuánto se compró y vendió
                dia_sob = db.stock_diario_articulo(aid_sob, fecha_sob)
                dia_sob = dia_sob[pd.to_datetime(dia_sob["fecha"]).dt.date == fecha_sob] if not dia_sob.empty else dia_sob
                if not dia_sob.empty:
                    f_s = dia_sob.iloc[0]
                    st.caption(
                        f"El {fecha_sob:%d/%m}: arrancó con {max(f_s['inicio'], 0):g} del día anterior, se compraron "
                        f"{f_s['comprado']:g} y se vendieron {f_s['vendido']:g}. Stock ahora: {stock_sob:g}."
                    )
                else:
                    st.caption(f"Stock ahora: {stock_sob:g}.")
            c1, c2 = st.columns([3, 1])
            cant_sob = c1.number_input(
                "¿Cuánto se tiró por vencido ese día? (total)" if es_vencido else "¿Cuánto sobró ese día? (total)",
                min_value=0.0, step=1.0, format="%g", value=float(ya),
                key=f"sob_cant_{'v' if es_vencido else 's'}_{aid_sob}_{fecha_sob}",
                help="Total del día. Si ya habías cargado y te equivocaste, poné el número correcto y guardá: reemplaza al anterior.",
            )
            c2.write("")
            c2.write("")
            # Lo que se agrega baja el stock de AHORA: si es más de lo que hay, no puede
            # haberse tirado (o el stock está mal); se pide confirmar.
            excede = (cant_sob - float(ya)) > stock_sob + 1e-9
            confirma_exceso = True
            if excede:
                st.warning(f"Estás sumando {cant_sob - float(ya):g} y en stock hay {stock_sob:g}: el stock "
                           "quedaría negativo. Revisá el número o el día; si el stock está mal, corregilo con un "
                           "conteo en 📦 Artículos.")
                confirma_exceso = st.checkbox("Guardar igual", key=f"sob_conf_{aid_sob}_{fecha_sob}")
            if c2.button("💾 Guardar vencido" if es_vencido else "💾 Guardar sobrante", type="primary",
                         width="stretch", key="sob_guardar", disabled=not confirma_exceso):
                try:
                    hechos = (db.registrar_vencido(aid_sob, fecha_sob, cant_sob) if es_vencido
                              else db.registrar_dia_compra_diaria(aid_sob, fecha_sob, None, cant_sob))
                    if hechos:
                        seguimiento_cacheado.clear()
                        st.success(hechos[0])
                    else:
                        st.info("No había cambios.")
                except ValueError as e:
                    st.error(str(e))
            if ya:
                st.caption(f"Ya registrado para ese día: {ya:g}.")

    else:
        # --------------------------------------------------------
        # HISTORIAL + EDICION/ELIMINACION
        # --------------------------------------------------------
        st.subheader("Últimas ventas")
        ventas = db.listar_ventas()
        if ventas.empty:
            st.info("Todavía no hay ventas registradas.")
        else:
            st.dataframe(
                ventas[["id", "fecha", "articulo", "cantidad", "precio_unitario", "total", "medio_pago",
                        "es_consumo_interno", "registrado_por"]]
                .rename(columns={"es_consumo_interno": "Consumo interno", "registrado_por": "Atendió"}).head(20),
                hide_index=True, width="stretch"
            )

            if not ES_DUENO:
                st.caption("Para corregir o eliminar una venta ya cargada, avisale al dueño.")
            else:
                st.subheader("✏️ Editar o eliminar una venta")
                # Etiquetas armadas una sola vez: filtrar el DataFrame de ventas por
                # cada opción tardaba ~1,7 s por clic con 1.500 ventas (y crecía con
                # el cuadrado de la cantidad de ventas).
                etiqueta_venta = {
                    v_id: f"#{v_id} — {art} — ${tot:.0f} — {fec}"
                    for v_id, art, tot, fec in zip(ventas["id"].values, ventas["articulo"].values,
                                                   ventas["total"].values, ventas["fecha"].values)
                }
                venta_sel_id = st.selectbox(
                    "Venta", ventas["id"],
                    format_func=lambda x: etiqueta_venta.get(x, f"#{x}")
                )
                venta_actual = db.obtener_venta(int(venta_sel_id))
                # Con inactivos: una venta vieja puede ser de un artículo ya dado de baja.
                articulos_todos = db.listar_articulos(incluir_inactivos=True)

                with st.form("editar_venta_form", enter_to_submit=False):
                    medio_pago_edit = st.selectbox(
                        "Medio de pago", MEDIOS_PAGO,
                        index=MEDIOS_PAGO.index(venta_actual["medio_pago"]) if venta_actual["medio_pago"] in MEDIOS_PAGO else 0,
                        key=f"medio_edit_{venta_sel_id}"
                    )
                    fecha_edit = st.date_input("Fecha", value=venta_actual["fecha"], key=f"fecha_edit_{venta_sel_id}")

                    # posición en la lista (no el índice del DataFrame, que no tiene por qué coincidir)
                    ids_articulos = articulos_todos["id"].astype(int).tolist()
                    idx_default = (ids_articulos.index(int(venta_actual["articulo_id"]))
                                   if int(venta_actual["articulo_id"]) in ids_articulos else 0)
                    match = articulos_todos[articulos_todos["id"] == venta_actual["articulo_id"]]
                    articulo_id_edit = st.selectbox(
                        "Artículo", ids_articulos, index=idx_default,
                        format_func=etiquetador(articulos_todos), key=f"articulo_edit_{venta_sel_id}"
                    )
                    c1, c2 = st.columns(2)
                    # min_value 0,01 (no 0,1): hay ventas reales de 40-90 g de fiambre
                    # (0,04-0,09 kg); con 0,1 Streamlit no podía mostrar el valor guardado
                    # y la pantalla entera se caía (StreamlitValueBelowMinError).
                    unidad_venta = match["unidad_medida"].values[0] if not match.empty else "unidad"
                    cantidad_edit = c1.number_input(
                        "Cantidad (kg)" if unidad_venta == "kg" else "Cantidad", min_value=0.01,
                        value=max(float(venta_actual["cantidad"]), 0.01),
                        step=0.05 if unidad_venta == "kg" else 1.0, key=f"cantidad_edit_{venta_sel_id}"
                    )
                    precio_edit = c2.number_input(
                        "Precio unitario", min_value=0.0, value=float(venta_actual["precio_unitario"]),
                        key=f"precio_edit_{venta_sel_id}"
                    )

                    consumo_interno_edit = st.checkbox(
                        "📦 Es consumo interno (no cuenta como ingreso)",
                        value=bool(venta_actual.get("es_consumo_interno", False)),
                        key=f"consumo_interno_edit_{venta_sel_id}"
                    )

                    st.divider()
                    confirmar_eliminar = st.checkbox(
                        "⚠️ Sí, estoy seguro de que quiero eliminar esta venta",
                        key=f"confirmar_eliminar_venta_{venta_sel_id}",
                        help="Esta acción no se puede deshacer. El stock del artículo se repone automáticamente."
                    )

                    colA, colB = st.columns(2)
                    if colA.form_submit_button("💾 Guardar cambios", type="primary"):
                        try:
                            avisos = db.editar_venta(
                                int(venta_sel_id), fecha_edit, medio_pago_edit,
                                articulo_id=int(articulo_id_edit),
                                cantidad=cantidad_edit, precio_unitario=precio_edit,
                                es_consumo_interno=consumo_interno_edit
                            )
                            avisar("success", "Venta actualizada y stock ajustado.")
                            for aviso in avisos:
                                avisar("warning", aviso)
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                    if colB.form_submit_button("🗑️ Eliminar esta venta"):
                        if not confirmar_eliminar:
                            st.error("Tildá la casilla de confirmación de arriba antes de eliminar.")
                        else:
                            db.eliminar_venta(int(venta_sel_id))
                            avisar("success", "Venta eliminada y stock repuesto (si correspondía).")
                            st.rerun()

# ============================================================
# GASTOS
# ============================================================
elif pagina == "💸 Gastos":
    st.title("💸 Gastos")

    with st.form("nuevo_gasto", enter_to_submit=False):
        c1, c2 = st.columns(2)
        categoria = c1.selectbox("Categoría", ["Alquiler", "Servicios (luz/agua/gas)", "Sueldos", "Impuestos", "Mantenimiento", "Otro"])
        monto = c2.number_input("Monto", min_value=0.0, step=100.0)
        fecha_gasto = st.date_input("Fecha", value=hoy_argentina())
        descripcion = st.text_input("Descripción (opcional)")
        if st.form_submit_button("Registrar gasto", type="primary"):
            if monto <= 0:
                st.error("El monto debe ser mayor a 0.")
            else:
                db.registrar_gasto(categoria, monto, fecha_gasto, descripcion or None)
                avisar("success", "Gasto registrado.")
                st.rerun()

    st.divider()
    st.subheader("Historial de gastos")
    gastos = db.listar_gastos()
    if gastos.empty:
        st.info("Todavía no hay gastos cargados.")
    else:
        # Posible duplicado: otro gasto con la misma fecha, categoría y monto.
        gastos["posible_duplicado"] = gastos.duplicated(
            subset=["fecha", "categoria", "monto"], keep=False
        )
        st.dataframe(
            gastos[["id", "fecha", "categoria", "descripcion", "monto", "posible_duplicado"]],
            column_config={
                "fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
                "monto": st.column_config.NumberColumn("Monto ($)", format=FMT_PESOS),
                "posible_duplicado": st.column_config.CheckboxColumn(
                    "¿Duplicado?", help="Hay otro gasto con la misma fecha, categoría y monto."),
            },
            hide_index=True, width="stretch"
        )
        if gastos["posible_duplicado"].any():
            st.caption("⚠️ Hay gastos marcados como posible duplicado: revisalos abajo.")

        st.divider()
        st.subheader("🗑️ Eliminar un gasto")

        def _formato_gasto(x):
            g = gastos[gastos["id"] == x].iloc[0]
            texto = (f"#{x} — {pd.to_datetime(g['fecha']):%d/%m/%Y} — {g['categoria']} — "
                     f"{pesos_venta(float(g['monto']))}")
            if pd.notna(g["descripcion"]) and g["descripcion"]:
                texto += f" — {g['descripcion']}"
            if g["posible_duplicado"]:
                texto += " ⚠️ posible duplicado"
            return texto

        c1, c2 = st.columns([3, 1])
        gasto_a_borrar = c1.selectbox(
            "Gasto a eliminar", gastos["id"], format_func=_formato_gasto, key="sel_gasto_borrar"
        )
        if c2.button("🗑️ Eliminar gasto", type="secondary"):
            confirmar_eliminacion(
                f"el gasto {_formato_gasto(gasto_a_borrar)}",
                "Se borra definitivamente (queda registrado en el Historial de cambios).",
                lambda: db.eliminar_gasto(int(gasto_a_borrar)),
                "Gasto eliminado.",
            )

# ============================================================
# REPORTES
# ============================================================
elif pagina == "📈 Reportes":
    st.title("📈 Reportes")

    c1, c2 = st.columns(2)
    desde = c1.date_input("Desde", value=hoy_argentina().replace(day=1))
    hasta = c2.date_input("Hasta", value=hoy_argentina())

    tab_gg, tab_abc, tab_margenes, tab_calidad = st.tabs(
        PESTANIAS_REPORTES, default=st.session_state.pop("reportes_pestania", None))

    # --------------------------------------------------------
    # GANANCIAS Y GASTOS (contenido original de Reportes)
    # --------------------------------------------------------
    with tab_gg:
        reporte = db.reporte_ganancias_vs_gastos(desde, hasta)

        c1, c2, c3 = st.columns(3)
        c1.metric("Ingresos por ventas", pesos_venta(reporte['ingresos_totales']))
        c2.metric("Costo de mercadería vendida", pesos_venta(reporte['costo_mercaderia']))
        c3.metric("Ganancia bruta", pesos_venta(reporte['ganancia_bruta']))

        c4, c5 = st.columns(2)
        c4.metric("Gastos operativos", pesos_venta(reporte['gastos_totales']))
        c5.metric("💰 Ganancia neta", pesos_venta(reporte['ganancia_neta']))

        st.divider()
        ventas_periodo = db.listar_ventas(desde, hasta)
        gastos_periodo = db.listar_gastos(desde, hasta)

        st.subheader("Detalle de ventas del período")
        st.dataframe(
            ventas_periodo.drop(columns=["id"]).assign(registrado_por=ventas_periodo["registrado_por"].fillna("—")),
            column_config={
                "ticket_id": st.column_config.NumberColumn("Ticket", format="%d"),
                "fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"), "articulo": "Artículo",
                "cantidad": st.column_config.NumberColumn("Cantidad", format="%g"), "unidad_medida": "Unidad",
                "precio_unitario": st.column_config.NumberColumn("Precio ($)", format=FMT_PESOS),
                "total": st.column_config.NumberColumn("Total ($)", format=FMT_PESOS), "medio_pago": "Medio de pago",
                "es_consumo_interno": st.column_config.CheckboxColumn("Consumo interno / sobrante"),
                "registrado_por": "Atendió",
            },
            hide_index=True, width="stretch")

        st.subheader("Detalle de gastos del período")
        st.dataframe(
            gastos_periodo.drop(columns=["id"]),
            column_config={
                "fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"), "categoria": "Categoría",
                "descripcion": "Descripción", "monto": st.column_config.NumberColumn("Monto ($)", format=FMT_PESOS),
            },
            hide_index=True, width="stretch")

        st.divider()
        st.subheader("🔎 Ventas reales por artículo")
        st.caption(
            "A diferencia del detalle de arriba, acá las ventas de promociones se "
            "reparten entre los artículos que las componen (proporcional a su precio "
            "individual) -- para ver cuánto se vendió de verdad de cada producto, "
            "se haya vendido suelto o dentro de un combo."
        )
        ventas_reales = db.ventas_por_articulo_real(desde, hasta)
        if ventas_reales.empty:
            st.info("No hay ventas en este período.")
        else:
            st.dataframe(
                ventas_reales.rename(columns={
                    "articulo": "Artículo", "cantidad": "Cantidad",
                    "ingreso_atribuido": "Ingreso atribuido", "costo_atribuido": "Costo atribuido",
                    "margen_atribuido": "Margen atribuido"
                }),
                hide_index=True, width="stretch"
            )

        st.divider()
        st.subheader("📥 Exportar")

        def armar_excel():
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                resumen = pd.DataFrame([reporte])
                resumen.to_excel(writer, sheet_name="Resumen", index=False)
                ventas_periodo.to_excel(writer, sheet_name="Ventas", index=False)
                gastos_periodo.to_excel(writer, sheet_name="Gastos", index=False)
            buffer.seek(0)
            return buffer

        st.download_button(
            "📥 Descargar reporte en Excel",
            data=armar_excel(),
            file_name=f"reporte_{desde}_{hasta}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    # --------------------------------------------------------
    # CLASIFICACIÓN ABC + STOCK INMOVILIZADO
    # (núcleo analítico, Fase 1 -- ver claude/roadmap-nucleo-analitico.md)
    # --------------------------------------------------------
    with tab_abc:
        st.caption(
            "Clasificación de productos por contribución a las ventas reales del "
            "período elegido arriba (método de Pareto/ABC), incluyendo lo vendido "
            "dentro de combos. Clase A: el núcleo de productos que explica la mayor "
            "parte de la facturación -- son los que más conviene monitorear de cerca."
        )

        with st.expander("💡 ¿Qué significa esto? (explicación simple)", expanded=True):
            st.markdown(
                "Separamos los productos en **3 grupos**, según cuánta plata te hacen "
                "entrar en el período elegido arriba:\n\n"
                "- 🔵 **Clase A** -- son pocos productos, pero son los que más venden "
                "(en este ejemplo, menos de la mitad de los productos explican la "
                "mayor parte de lo facturado). **Nunca se te tienen que acabar**, y son "
                "los que más conviene revisar si el precio está bien calculado.\n"
                "- 🟠 **Clase B** -- venden una cantidad media. Importantes, pero no "
                "tan críticos como los de Clase A.\n"
                "- 🟢 **Clase C** -- son un montón de productos distintos, pero cada "
                "uno aporta poco a la caja. Son los primeros candidatos a revisar si "
                "conviene seguir teniendo tanto stock de todos ellos (ver más abajo, "
                "\"Stock inmovilizado\")."
            )

        with st.expander("⚙️ Ajustar cortes de clase (avanzado)"):
            cc1, cc2 = st.columns(2)
            corte_a = cc1.slider("Corte clase A (% acumulado)", 50, 95, 80)
            corte_b = cc2.slider("Corte clase B (% acumulado)", int(corte_a) + 1, 99, 95)

        abc = db.clasificacion_abc(desde, hasta, corte_a, corte_b)
        resumen_abc = db.resumen_abc(desde, hasta, corte_a, corte_b, df=abc)

        if abc.empty:
            st.info("No hay ventas en este período para clasificar.")
        else:
            cols = st.columns(len(resumen_abc))
            for col, (_, fila) in zip(cols, resumen_abc.iterrows()):
                col.metric(
                    f"Clase {fila['clase']}",
                    f"{int(fila['cantidad_articulos'])} artículos",
                    f"{fila['pct_ingreso']:.1f}% del ingreso"
                )

            st.caption("Cuánto entró, en pesos, de cada clase en el período -- una barra por clase, no una arriba de la otra.")
            # Se arma con Altair (no st.bar_chart) para fijar el color de cada
            # clase de forma explícita e inequívoca -- con st.bar_chart, pasar
            # los hex por columna no garantiza que cada clase quede pintada
            # con SU color (se probó y los colores terminaban mezclados entre
            # clases). Con un dominio/rango explícito no hay ambigüedad posible.
            grafico_abc = (
                alt.Chart(resumen_abc)
                .mark_bar(size=70)
                .encode(
                    x=alt.X("clase:N", title="Clase", sort=["A", "B", "C"],
                            axis=alt.Axis(labelAngle=0)),
                    y=alt.Y("ingreso_total:Q", title="Ingreso del período ($)"),
                    color=alt.Color(
                        "clase:N",
                        scale=alt.Scale(domain=["A", "B", "C"],
                                         range=[COLOR_CLASE["A"], COLOR_CLASE["B"], COLOR_CLASE["C"]]),
                        legend=None,
                    ),
                    tooltip=[
                        alt.Tooltip("clase:N", title="Clase"),
                        alt.Tooltip("cantidad_articulos:Q", title="Cantidad de artículos", format=",.0f"),
                        alt.Tooltip("ingreso_total:Q", title="Ingreso del período", format="$,.2f"),
                        alt.Tooltip("pct_ingreso:Q", title="% del ingreso", format=".1f"),
                    ],
                )
                .properties(height=260)
            )
            st.altair_chart(grafico_abc, width="stretch")

            abc = abc.copy()
            abc["articulo"] = [a if act else f"{a} — ⛔ inactivo"
                               for a, act in zip(abc["articulo"], abc["activo"])]
            if (~abc["activo"]).any():
                st.caption(
                    f"⛔ {int((~abc['activo']).sum())} artículo(s) del período ya están dados de baja: "
                    "se siguen clasificando porque esas ventas ocurrieron (sacarlas cambiaría el % de "
                    "todos los demás), pero no entran en la reposición."
                )
            st.dataframe(
                abc[["articulo", "clase", "cantidad", "ingreso_atribuido", "pct_ingreso", "pct_acumulado"]]
                .rename(columns={
                    "articulo": "Artículo", "clase": "Clase", "cantidad": "Cantidad",
                    "ingreso_atribuido": "Ingreso atribuido", "pct_ingreso": "% del ingreso",
                    "pct_acumulado": "% acumulado"
                })
                .style.format({"Ingreso atribuido": pesos_venta, "% del ingreso": "{:.2f}%", "% acumulado": "{:.2f}%"}),
                hide_index=True, width="stretch"
            )

        st.divider()
        st.subheader("💤 Stock inmovilizado")
        st.caption(
            "Artículos con stock que no se mueven (cuenta lo vendido suelto, en combos y el consumo "
            "interno) -- el complemento natural del ABC: son los que esa clasificación no llega a ver "
            "porque no vendieron nada. Se muestran en dos grupos: los que nunca se vendieron desde que "
            "se cargaron y los que se vendieron alguna vez pero no en los últimos días elegidos."
        )
        reg_ventas = db.registro_ventas()
        dias_reg = reg_ventas["dias"]
        # Por defecto, 60 días o los que haya de historia si son menos (con menos
        # historia, "60 días sin venta" no puede pasar todavía).
        dias_defecto = int(min(60, max(15, dias_reg))) if dias_reg else 60
        dias_sin_venta = st.slider(
            "Días sin venta para considerarlo inmovilizado", 15, 180, dias_defecto,
            help="Solo afecta a los que se vendieron alguna vez. Los que nunca se vendieron se "
                 "muestran siempre, con los días desde que se cargaron.")
        inmovilizado = db.stock_inmovilizado(dias_sin_venta)
        total_stock = db.valor_stock()["valor_costo"]

        def _pesos_inm(v):
            return f"${v:,.0f}".replace(",", ".")

        if dias_reg and dias_sin_venta > dias_reg:
            hasta_eval = pd.Timestamp(reg_ventas["primera_venta"]) + pd.Timedelta(days=dias_sin_venta)
            st.warning(
                f"La app registra ventas desde el {reg_ventas['primera_venta']:%d/%m/%Y} ({dias_reg} días). "
                f"Con {dias_sin_venta} días, \"se vendió pero no en los últimos {dias_sin_venta} días\" "
                f"todavía no puede pasar: ese grupo va a quedar vacío hasta el {hasta_eval:%d/%m/%Y}. "
                f"Para ver lo que se frenó, bajá a {dias_reg} días o menos. Lo que nunca se vendió "
                "se muestra igual."
            )

        if inmovilizado.empty:
            st.success(f"No hay stock inmovilizado: todo lo que tiene stock se vendió al menos una vez "
                       f"en los últimos {dias_sin_venta} días.")
        else:
            inmovilizado = inmovilizado.copy()
            # Lo recién cargado y lo que está en consignación se listan, pero no suman
            # como plata parada (el total de mercadería tampoco incluye la consignación).
            suma = ~inmovilizado["recien_cargado"].astype(bool) & ~inmovilizado["en_consignacion"].astype(bool)
            es_nunca = inmovilizado["grupo"] == "nunca"
            valor_nunca = float(inmovilizado.loc[suma & es_nunca, "valor_inmovilizado"].sum())
            valor_frenado = float(inmovilizado.loc[suma & ~es_nunca, "valor_inmovilizado"].sum())
            valor_inm = valor_nunca + valor_frenado
            pct_inm = (valor_inm / total_stock * 100) if total_stock > 0 else 0.0
            m1, m2, m3 = st.columns(3)
            m1.metric("💤 Inmovilizado", _pesos_inm(valor_inm))
            m2.metric("📦 Total en mercadería", _pesos_inm(total_stock))
            m3.metric("% de la mercadería parada", f"{pct_inm:.1f}%".replace(".", ","))
            st.markdown(
                f"De los **{_pesos_inm(total_stock)}** que tenés invertidos en mercadería, "
                f"**{_pesos_inm(valor_inm)} ({pct_inm:.0f}%)** está parado: "
                f"{_pesos_inm(valor_nunca)} en artículos que nunca se vendieron desde que se cargaron y "
                f"{_pesos_inm(valor_frenado)} en artículos que no se venden hace {dias_sin_venta} días "
                "o más.".replace("$", "\\$")
            )

            def _estado_inm(fila):
                if fila["en_consignacion"]:
                    return "🤝 Consignación (no suma)"
                if fila["recien_cargado"]:
                    return "🆕 Recién cargado (no suma)"
                return "Activo" if fila["activo"] else "⛔ Inactivo: liquidar"

            inmovilizado["estado"] = inmovilizado.apply(_estado_inm, axis=1)
            col_comunes = {
                "articulo": "Artículo", "estado": "Estado", "categoria": "Categoría",
                "stock_actual": st.column_config.NumberColumn("Stock", format="%g"),
                "costo_compra_actual": st.column_config.NumberColumn("Costo s/IVA ($)", format=FMT_PESOS),
                "valor_inmovilizado": st.column_config.NumberColumn("Valor a costo ($)", format=FMT_PESOS),
            }
            nunca = inmovilizado[es_nunca]
            frenado = inmovilizado[~es_nunca]
            tab_nunca, tab_frenado = st.tabs([
                f"🚫 Nunca se vendieron ({len(nunca)})",
                f"🐢 Sin venta hace {dias_sin_venta}+ días ({len(frenado)})",
            ])
            with tab_nunca:
                if nunca.empty:
                    st.info("Todos los artículos con stock se vendieron al menos una vez.")
                else:
                    st.caption(
                        f"Tienen stock y no registran ninguna venta desde que se cargaron en la app. "
                        f"Los dados de alta hace menos de {db.DIAS_RECIEN_CARGADO} días se marcan como "
                        "recién cargados y no suman en lo parado. Si un artículo de acá sí se vende, "
                        "revisá que se esté cobrando con ese artículo y no con otro parecido."
                    )
                    st.dataframe(
                        nunca[["articulo", "estado", "categoria", "stock_actual", "costo_compra_actual",
                               "valor_inmovilizado", "fecha_alta", "dias_desde_alta"]],
                        column_config={
                            **col_comunes,
                            "fecha_alta": st.column_config.DateColumn("Cargado el", format="DD/MM/YYYY"),
                            "dias_desde_alta": st.column_config.NumberColumn("Días desde que se cargó", format="%d"),
                        },
                        hide_index=True, width="stretch"
                    )
            with tab_frenado:
                if frenado.empty:
                    st.info(f"Todo lo que se vendió alguna vez tuvo al menos una venta en los últimos "
                            f"{dias_sin_venta} días.")
                else:
                    st.dataframe(
                        frenado[["articulo", "estado", "categoria", "stock_actual", "costo_compra_actual",
                                 "valor_inmovilizado", "fecha_ultima_venta", "dias_sin_venta"]],
                        column_config={
                            **col_comunes,
                            "fecha_ultima_venta": st.column_config.DateColumn("Última venta", format="DD/MM/YYYY"),
                            "dias_sin_venta": st.column_config.NumberColumn("Días sin venta", format="%d"),
                        },
                        hide_index=True, width="stretch"
                    )

    # --------------------------------------------------------
    # MÁRGENES REALES
    # (núcleo analítico, Fase 1 -- ver claude/roadmap-nucleo-analitico.md)
    # --------------------------------------------------------
    with tab_margenes:
        st.markdown("#### ¿Cuánto te dejó cada producto?")

        margenes = db.margenes_reales(desde, hasta)
        if margenes.empty:
            st.info("No hay ventas en este período para calcular márgenes.")
        else:
            por_categoria = db.margenes_reales_por_categoria(desde, hasta, df=margenes)
            promedio = db.margen_promedio(margenes)

            def pesos(v, signo=False):
                txt = f"${abs(v):,.0f}".replace(",", ".")
                if signo:
                    return ("+" if v > 0 else "−" if v < 0 else "") + txt
                return ("−" if v < 0 else "") + txt

            def pct(v):
                return "—" if v is None or pd.isna(v) else f"{v * 100:.1f}%".replace(".", ",")

            def md(texto):
                # "\$": sin escapar, Streamlit toma el texto entre dos "$" como fórmula LaTeX
                return texto.replace("$", "\\$")

            # 1. La respuesta en pesos ------------------------------------
            t1, t2, t3 = st.columns(3)
            t1.metric("Vendiste (sin IVA)", pesos(margenes["ingreso_neto"].sum()))
            t2.metric("Te quedó de ganancia", pesos(margenes["ganancia_real"].sum()),
                      help="Lo cobrado (sin IVA) menos lo que te costó la mercadería vendida.")
            t3.metric("Margen promedio", pct(promedio),
                      help="De cada $100 que vendiste (sin IVA), cuánto te quedó de ganancia.")
            st.caption("Montos sin IVA. No incluye consumo interno.")
            sin_costo = db.porcentaje_ventas_sin_costo_historico(desde, hasta)
            if sin_costo is None or sin_costo > 0:
                st.caption(
                    "ℹ️ " + ("Parte de las ventas de este período" if sin_costo is not None and sin_costo < 100
                            else "Las ventas de este período") +
                    " se cargaron antes de que el sistema guardara el costo de cada venta: para esas se usa "
                    "el costo actual, así que la ganancia de esas ventas es aproximada."
                )

            with st.expander("💡 ¿Para qué te sirve?", expanded=True):
                st.markdown(
                    "- 🏆 **Saber qué rubros te dejan plata**, para decidir qué conviene tener siempre y "
                    "qué empujar.\n"
                    "- 🔍 **Encontrar productos que venden mucho pero dejan poco**: a veces el que más "
                    "sale no es el que más te deja.\n"
                    "- 🎁 **Ver si los combos convienen**: una promo puede vender bien y dejar poca ganancia."
                )

            # 2. Por rubro ------------------------------------------------
            st.divider()
            st.subheader("¿Qué rubro te deja más?")
            st.caption("Ganancia del período por categoría. La etiqueta muestra la ganancia y el margen.")
            graf = por_categoria[["categoria", "ingreso_neto", "ganancia_real", "margen_real_sobre_venta"]].copy()
            graf["etiqueta"] = [f"{pesos(g)} · {pct(m)}" for g, m in zip(graf["ganancia_real"], graf["margen_real_sobre_venta"])]
            graf["margen_pct"] = pd.to_numeric(graf["margen_real_sobre_venta"], errors="coerce") * 100
            base = alt.Chart(graf).encode(
                y=alt.Y("categoria:N", sort="-x", title=None, axis=alt.Axis(labelLimit=160)),
            )
            lim_x = max(1.0, float(graf["ganancia_real"].max())) * 1.35
            barras = base.mark_bar(size=18, cornerRadius=3, color=COLOR_CLASE["A"]).encode(
                x=alt.X("ganancia_real:Q", title="Ganancia ($, sin IVA)", axis=alt.Axis(format="$,.0f"),
                        scale=alt.Scale(domain=[min(0.0, float(graf["ganancia_real"].min()) * 1.35), lim_x])),
                tooltip=[
                    alt.Tooltip("categoria:N", title="Categoría"),
                    alt.Tooltip("ingreso_neto:Q", title="Vendiste (sin IVA)", format="$,.0f"),
                    alt.Tooltip("ganancia_real:Q", title="Te quedó", format="$,.0f"),
                    alt.Tooltip("margen_pct:Q", title="Margen (%)", format=".1f"),
                ],
            )
            texto = base.mark_text(align="left", dx=5, fontWeight="bold").encode(
                x="ganancia_real:Q", text="etiqueta:N")
            # Alto por fila (alt.Step) y no un alto fijo: en Streamlit, un alto fijo
            # con categorías en el eje Y aplasta todas las barras en una sola fila.
            st.altair_chart((barras + texto).properties(height=alt.Step(36)), width="stretch")

            # 3. Qué revisar ----------------------------------------------
            st.divider()
            st.subheader("Venden mucho, pero dejan poco")
            st.caption(
                f"Productos de los que más vendiste (mitad de arriba) con un margen {db.MARGEN_BAJO_PP:.0f} "
                f"puntos o más por debajo de tu promedio ({pct(promedio)})."
            )
            bajos = db.articulos_que_dejan_poco(margenes)
            if bajos.empty:
                st.success("Ninguno de tus productos más vendidos deja un margen bajo. 👏")
            else:
                for _, fila in bajos.head(10).iterrows():
                    with st.container(border=True):
                        c1, c2 = st.columns([4, 1])
                        unidad = " kg" if fila["unidad_medida"] == "kg" else ""
                        cant = f"{fila['cantidad']:g}".replace(".", ",")
                        c1.markdown(f"**{fila['articulo']}** · {fila['categoria'] or 'Sin categoría'} · {cant}{unidad} vendidos")
                        c2.markdown(f"<div style='text-align:right'><b>{pct(fila['margen_real_sobre_venta'])}</b></div>",
                                    unsafe_allow_html=True)
                        st.markdown(md(
                            f"Vendiste **{pesos(fila['ingreso_neto'])}** y te dejó **{pesos(fila['ganancia_real'])}** "
                            f"(margen {pct(fila['margen_real_sobre_venta'])}; tu promedio es {pct(promedio)})."
                        ))
                        st.caption("→ " + db.pista_margen(fila, promedio))
                if len(bajos) > 10:
                    st.caption(f"Y {len(bajos) - 10} más -- ver el detalle técnico abajo.")

            combos = db.margenes_por_combo(desde, hasta)
            if not combos.empty:
                st.divider()
                st.subheader("🎁 ¿Los combos convienen?")
                st.caption("Cuánto dejó cada combo, comparado con tu margen promedio.")
                for _, c in combos.iterrows():
                    m = c["margen_real_sobre_venta"]
                    bajo = promedio is not None and pd.notna(m) and m < promedio - db.MARGEN_BAJO_PP / 100
                    icono = "🔻" if bajo else "✅"
                    st.markdown(md(
                        f"{icono} **{c['combo']}** · {c['unidades']:g} vendidos · vendiste {pesos(c['ingreso_neto'])}, "
                        f"te dejó {pesos(c['ganancia_real'])} (margen **{pct(m)}**)"
                        + (" -- deja menos que tu promedio: revisá el precio de la promo." if bajo else "")
                    ))

            # 4. Control de precio ----------------------------------------
            st.divider()
            st.subheader("¿Se cobró el precio del sistema?")
            controladas = margenes["cantidad_controlada"].sum()
            if controladas == 0:
                st.caption(
                    "Este control compara lo cobrado con el precio del sistema al momento de cada venta. "
                    "En este período no hay ventas con ese dato guardado todavía."
                )
            else:
                difs = margenes[margenes["dif_precio"].abs() >= db.UMBRAL_PRECIO]
                if difs.empty:
                    st.success(
                        "✅ Todas las ventas controladas se cobraron al precio que marcaba el sistema ese día. "
                        "El precio automático del carrito evita cobrar de menos."
                    )
                else:
                    st.warning(md(
                        f"Hay {len(difs)} producto(s) cobrados distinto del precio del sistema, por "
                        f"{pesos(difs['dif_precio'].sum(), signo=True)} en total (sin IVA). Suele ser una "
                        "venta editada a mano: revisalas en Registrar Venta."
                    ))
                    st.dataframe(
                        difs[["articulo", "cantidad_controlada", "precio_lista_prom", "precio_cobrado_prom", "dif_precio"]]
                        .rename(columns={
                            "articulo": "Artículo", "cantidad_controlada": "Cantidad",
                            "precio_lista_prom": "Precio del sistema (prom.)",
                            "precio_cobrado_prom": "Cobrado (prom.)", "dif_precio": "Diferencia (sin IVA)",
                        })
                        .style.format({"Precio del sistema (prom.)": pesos_tabla, "Cobrado (prom.)": pesos_tabla,
                                       "Diferencia (sin IVA)": pesos_tabla, "Cantidad": "{:g}"}),
                        hide_index=True, width="stretch",
                    )

            # 5. Detalle técnico -----------------------------------------
            st.divider()
            with st.expander("📊 Detalle técnico (para el informe)"):
                st.caption(
                    "Margen real / venta = (ingreso neto − costo) ÷ ingreso neto (margen bruto). Margen real / "
                    "costo = (ingreso neto − costo) ÷ costo, la misma fórmula del margen cargado en Artículos. "
                    "El desvío compara contra el margen configurado HOY: si el margen se cambió durante el "
                    "período, el desvío lo refleja."
                )
                st.markdown("**Por categoría**")
                st.dataframe(
                    por_categoria[["categoria", "ingreso_neto", "costo_real", "ganancia_real",
                                    "margen_real_sobre_costo", "margen_real_sobre_venta"]]
                    .rename(columns={
                        "categoria": "Categoría", "ingreso_neto": "Ingreso neto (sin IVA)",
                        "costo_real": "Costo real", "ganancia_real": "Ganancia",
                        "margen_real_sobre_costo": "Margen real / costo",
                        "margen_real_sobre_venta": "Margen real / venta"
                    })
                    .style.format({
                        "Ingreso neto (sin IVA)": pesos_tabla, "Costo real": pesos_tabla, "Ganancia": pesos_tabla,
                        "Margen real / costo": "{:.1%}", "Margen real / venta": "{:.1%}"
                    }, na_rep="—"),
                    hide_index=True, width="stretch"
                )
                st.markdown("**Por artículo**")
                st.dataframe(
                    margenes[["articulo", "categoria", "cantidad", "ingreso_neto", "costo_real", "ganancia_real",
                              "margen_real_sobre_venta", "margen_configurado", "margen_real_sobre_costo", "desvio_pp"]]
                    .rename(columns={
                        "articulo": "Artículo", "categoria": "Categoría", "cantidad": "Cantidad",
                        "ingreso_neto": "Ingreso neto", "costo_real": "Costo real", "ganancia_real": "Ganancia",
                        "margen_real_sobre_venta": "Margen real / venta",
                        "margen_configurado": "Margen configurado (hoy)", "margen_real_sobre_costo": "Margen real / costo",
                        "desvio_pp": "Desvío (p.p.)"
                    })
                    .style.format({
                        "Ingreso neto": pesos_tabla, "Costo real": pesos_tabla, "Ganancia": pesos_tabla,
                        "Margen real / venta": "{:.1%}", "Margen configurado (hoy)": "{:.1%}",
                        "Margen real / costo": "{:.1%}", "Desvío (p.p.)": "{:+.1f}", "Cantidad": "{:g}"
                    }, na_rep="—"),
                    hide_index=True, width="stretch"
                )

    # --------------------------------------------------------
    # CALIDAD DE DATOS
    # Condición previa del núcleo analítico: el ABC, los márgenes y la
    # reposición valen tanto como los datos que los alimentan.
    # --------------------------------------------------------
    with tab_calidad:
        st.caption(
            "Revisa todos los artículos con 6 chequeos automáticos. El indicador es el % "
            "de artículos que no cae en ninguno. **No depende del rango de fechas de arriba**: "
            "muestra el estado actual de los datos."
        )
        calidad = db.calidad_de_datos(hoy_argentina())
        alertas = calidad["alertas"]
        try:
            anterior = db.indicador_semana_anterior()
            db.guardar_snapshot_calidad(calidad)
            historial = db.historial_calidad()
        except Exception:
            anterior, historial = None, pd.DataFrame(columns=["semana", "indicador"])

        m1, m2, m3 = st.columns(3)
        m1.metric(
            "Artículos sin alertas", f"{calidad['indicador']:.0f}%",
            None if anterior is None else f"{calidad['indicador'] - anterior:+.1f} pts vs semana anterior",
        )
        m2.metric("Artículos con alertas", calidad["articulos_con_alerta"],
                  help=f"De {calidad['articulos_activos']} artículos evaluados (sin contar promociones "
                       f"ni {calidad['articulos_inactivos']} inactivos sin stock)")
        m3.markdown(f"#### {ICONO_ESTADO_CALIDAD[calidad['estado']]} {TEXTO_ESTADO_CALIDAD[calidad['estado']]}")
        st.caption(
            f"Semáforo: 🟢 ≥ {db.SEMAFORO_CALIDAD['verde']}% · 🟡 ≥ {db.SEMAFORO_CALIDAD['amarillo']}% · "
            f"🔴 < {db.SEMAFORO_CALIDAD['amarillo']}%"
        )

        st.divider()
        g1, g2 = st.columns(2)
        with g1:
            st.subheader("¿Dónde están los problemas?")
            conteo = (
                alertas.groupby("chequeo")["articulo_id"].nunique()
                .reindex(list(db.CHEQUEOS_CALIDAD.keys()), fill_value=0)
                .reset_index(name="articulos")
            )
            conteo["tipo"] = conteo["chequeo"].map(lambda k: db.CHEQUEOS_CALIDAD[k][0])
            barras = alt.Chart(conteo).mark_bar(color="#2a78d6").encode(
                x=alt.X("articulos:Q", title="Artículos afectados", axis=alt.Axis(tickMinStep=1)),
                y=alt.Y("tipo:N", sort="-x", title=None, axis=alt.Axis(labelLimit=220)),
                tooltip=[alt.Tooltip("tipo:N", title="Chequeo"),
                         alt.Tooltip("articulos:Q", title="Artículos")],
            ).properties(height=alt.Step(36))  # alto por fila: ver nota en Márgenes reales
            st.altair_chart(barras, width="stretch")

        with g2:
            st.subheader("Evolución semanal")
            if len(historial) < 2:
                st.info(
                    "Se guarda una foto del indicador por semana cada vez que se abre el "
                    "Dashboard o esta pestaña. La línea aparece a partir de la segunda semana."
                )
            else:
                linea = alt.Chart(historial).mark_line(point=True, color="#2a78d6").encode(
                    x=alt.X("semana:T", title="Semana", axis=alt.Axis(format="%d/%m")),
                    y=alt.Y("indicador:Q", title="% sin alertas", scale=alt.Scale(domain=[0, 100])),
                    tooltip=[alt.Tooltip("semana:T", title="Semana del", format="%d/%m/%Y"),
                             alt.Tooltip("indicador:Q", title="% sin alertas", format=".1f"),
                             alt.Tooltip("articulos_activos:Q", title="Artículos evaluados"),
                             alt.Tooltip("articulos_inactivos:Q", title="Inactivos fuera")],
                )
                objetivo = alt.Chart(pd.DataFrame({"y": [db.SEMAFORO_CALIDAD["verde"]]})).mark_rule(
                    strokeDash=[4, 4], color="#1baf7a").encode(y="y:Q")
                st.altair_chart((linea + objetivo).properties(height=230), width="stretch")
                st.caption(
                    f"Línea punteada: objetivo verde ({db.SEMAFORO_CALIDAD['verde']}%). Pasá el mouse "
                    "por cada punto para ver cuántos artículos se evaluaron: si el indicador sube "
                    "mientras bajan los evaluados, la mejora vino de dar de baja artículos, no de "
                    "corregir datos."
                )

        st.divider()
        st.subheader("Qué corregir")
        if alertas.empty:
            st.success("No hay alertas: todos los artículos pasan los 6 chequeos.")
        else:
            opciones = ["Todas"] + [v[0] for v in db.CHEQUEOS_CALIDAD.values()]
            filtro = st.selectbox("Tipo de alerta", opciones, key="filtro_calidad")
            tabla = alertas if filtro == "Todas" else alertas[alertas["tipo"] == filtro]
            st.dataframe(
                tabla[["articulo", "categoria", "tipo", "detalle", "accion"]].rename(columns={
                    "articulo": "Artículo", "categoria": "Categoría", "tipo": "Problema",
                    "detalle": "Detectado", "accion": "Acción sugerida",
                }),
                hide_index=True, width="stretch",
            )

        u = db.UMBRALES_CALIDAD
        with st.expander("💡 ¿Cómo se calcula?"):
            st.markdown(
                "- **Indicador:** % de artículos (sin contar promociones) que no tienen ninguna "
                "alerta. Se cuenta por artículo: si uno tiene dos problemas, cuenta una sola vez.\n"
                "- **Artículos inactivos** (dados de baja en Artículos → Editar artículo): quedan "
                "fuera del indicador porque ya no se gestionan. Si les queda stock, siguen adentro "
                "con la alerta *Inactivo con stock* (o *Stock negativo*): dar de baja no esconde "
                "un problema, solo saca lo que ya quedó en cero.\n"
                "- **Stock negativo:** el sistema tiene menos de cero (se vendió más de lo cargado como comprado).\n"
                f"- **Stock sin movimiento:** tiene stock y no se vendió en más de {u['dias_sin_venta']} días "
                "(si nunca se vendió, se cuenta desde que se dio de alta).\n"
                f"- **Venta atípica:** en los últimos {u['ventana_reciente_dias']} días, una venta mayor a "
                f"lo habitual + {u['desvios_anomalia']} desvíos para ese artículo (solo si tiene al menos "
                f"{u['min_ventas_historial']} ventas anteriores para comparar).\n"
                "- **Precio sin ganancia:** el margen cargado es 0 % o negativo (se vende al costo o por debajo).\n"
                "- **Sin costo cargado:** el costo de compra está en $0, lo que distorsiona márgenes y ABC.\n"
                "- **Inactivo con stock:** se dio de baja pero el sistema todavía tiene mercadería: "
                "liquidarla o ajustar el stock con un conteo.\n\n"
                "**Ojo:** que un artículo no tenga alertas no garantiza que sus datos estén bien, "
                "solo que no cayó en ninguna regla. El mejor control complementario es contar "
                "a mano algunos productos cada tanto y compararlos con el sistema."
            )

# ============================================================
# CIERRE DE CAJA
# ============================================================
elif pagina == "🧮 Cierre de Caja":
    st.title("🧮 Cierre de Caja")
    st.caption("Resumen de un día puntual, para cerrar caja: lo vendido por medio de pago, la plata que "
               "tiene que haber en el cajón y los gastos del día.")

    fecha_cierre = st.date_input("Fecha", value=hoy_argentina(), format="DD/MM/YYYY")
    cierre = db.cierre_caja(fecha_cierre)

    c1, c2, c3 = st.columns(3)
    c1.metric("💰 Vendido (todos los medios)", pesos_venta(cierre["total_ingresos"]))
    c2.metric("💸 Gastos pagados", pesos_venta(cierre["total_gastos_plata"]),
              help="Gastos en plata del día (alquiler, sueldos, otros). No incluye consumo interno ni "
                   "sobrante: eso es mercadería, va aparte abajo.")
    c3.metric("Neto del día", pesos_venta(cierre["neto_del_dia"]),
              help="Vendido menos gastos pagados. La mercadería consumida o tirada no se resta acá "
                   "porque no es plata que salió del local (ya cuenta como costo en Reportes).")
    if cierre["total_mercaderia"] > 0:
        st.caption(
            f"📦 Además salió mercadería por {md_pesos(cierre['total_mercaderia'])} a costo "
            "(consumo interno y sobrante del día). No es ingreso ni plata que salió de la caja."
        )

    # ---------------- Arqueo: cuánta plata tiene que haber en el cajón ----------------
    st.divider()
    st.subheader("🧾 Arqueo de caja (efectivo)")
    st.caption("Para controlar el cajón al cerrar. Con «Guardar el cierre del día» (abajo) queda anotado.")
    gastos_plata = cierre["gastos_plata"]
    gastos_de_caja = 0.0
    if not gastos_plata.empty:
        tabla_g = pd.DataFrame({
            "Salió del cajón": True,
            "Categoría": gastos_plata["categoria"],
            "Descripción": gastos_plata["descripcion"].fillna(""),
            "Monto": gastos_plata["monto"].astype(float),
        })
        editada_g = st.data_editor(
            tabla_g, hide_index=True, width="stretch", key=f"arqueo_gastos_{fecha_cierre}",
            disabled=["Categoría", "Descripción", "Monto"],
            column_config={
                "Salió del cajón": st.column_config.CheckboxColumn(
                    help="Destildá los gastos que se pagaron por transferencia o con otra plata."),
                "Monto": st.column_config.NumberColumn("Monto ($)", format=FMT_PESOS),
            },
        )
        gastos_de_caja = float(editada_g.loc[editada_g["Salió del cajón"], "Monto"].sum())
    a1, a2 = st.columns(2)
    fondo = a1.number_input("Fondo de caja al abrir (cambio)", min_value=0.0, step=1000.0,
                            key=f"arqueo_fondo_{fecha_cierre}")
    contado = a2.number_input("Efectivo contado al cerrar", min_value=0.0, step=100.0, value=None,
                              placeholder="Contá el cajón y cargalo acá", key=f"arqueo_contado_{fecha_cierre}")
    esperado = fondo + cierre["ventas_efectivo"] - gastos_de_caja
    m1, m2, m3 = st.columns(3)
    m1.metric("Ventas en efectivo", pesos_venta(cierre["ventas_efectivo"]))
    m2.metric("💵 Efectivo esperado en caja", pesos_venta(esperado),
              help="Fondo al abrir + ventas en efectivo − gastos que salieron del cajón.")
    if contado is not None:
        diferencia = contado - esperado
        if abs(diferencia) < 0.5:
            m3.metric("Diferencia", "$0", delta="Cierra justo", delta_color="off")
        else:
            m3.metric("Diferencia", pesos_venta(diferencia),
                      delta="Sobra plata" if diferencia > 0 else "Falta plata",
                      delta_color="normal" if diferencia > 0 else "inverse")

    # Tanda 21: el cierre del día se puede guardar (Desafío Recreo, materia Cuentas)
    if db.hay_cierres():
        guardado = db.cierre_guardado(fecha_cierre)
        with st.container(border=True):
            st.markdown("**💾 Guardar el cierre del día**")
            if guardado:
                st.caption(f"✅ Ya guardado ({guardado.get('persona') or '—'}): contado "
                           f"{md_pesos(float(guardado['contado']))}, diferencia {md_pesos(float(guardado['diferencia']))}"
                           + (", no sobró pan" if guardado["pan_sin_sobrante"] else "")
                           + ". Si lo volvés a guardar, se reemplaza.")
            else:
                st.caption("Queda el arqueo guardado: así se ve con el tiempo si la caja cierra. No importa si hoy "
                           "da diferencia: lo que cuenta es contarla y anotarla.")
            pan_ok = db.pan_resuelto(fecha_cierre)
            sin_pan = False
            if not pan_ok:
                st.warning("Hoy se compró pan y no se cargó sobrante. Si sobró, cargalo en 💵 Registrar Venta → "
                           "🗑️ Sobrante del día; si no sobró nada, marcalo acá.")
                sin_pan = st.checkbox("Hoy no sobró pan", key=f"cierre_sin_pan_{fecha_cierre}",
                                      value=bool(guardado and guardado["pan_sin_sobrante"]))
            if st.button("💾 Guardar el cierre del día", type="primary", key=f"cierre_guardar_{fecha_cierre}",
                         disabled=contado is None, help="Primero cargá el efectivo contado."):
                try:
                    dif = db.guardar_cierre_caja(fecha_cierre, fondo, cierre["ventas_efectivo"], gastos_de_caja,
                                                 contado, sin_pan)
                    avisar("success", f"Cierre del {fecha_cierre:%d/%m} guardado (diferencia {pesos_venta(dif)}).")
                    if desafio.hay_desafio():
                        for txt in desafio.evaluar_cierres(PERSONA["id"], hoy_argentina()):
                            avisar("success", f"🧮 Desafío Recreo: {txt}")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    st.divider()
    st.subheader("Por medio de pago")
    if cierre["por_medio_pago"].empty:
        st.info("No hay ventas registradas ese día.")
    else:
        st.dataframe(
            cierre["por_medio_pago"].rename(columns={"medio_pago": "Medio de pago", "total": "Total", "cantidad": "Cantidad de ventas"}),
            column_config={"Total": st.column_config.NumberColumn("Total ($)", format=FMT_PESOS)},
            hide_index=True, width="stretch"
        )

    st.divider()
    st.subheader("Detalle de ventas del día")
    ventas_det = cierre["ventas_detalle"]
    if not ventas_det.empty:
        ventas_det = ventas_det.assign(registrado_por=ventas_det["registrado_por"].fillna("—"))
    if ventas_det.empty:
        st.info("No hay ventas registradas ese día.")
    else:
        st.dataframe(
            ventas_det[["ticket_id", "articulo", "cantidad", "unidad_medida", "precio_unitario", "total",
                        "medio_pago", "es_consumo_interno", "registrado_por"]],
            column_config={
                "ticket_id": "Ticket", "articulo": "Artículo", "cantidad": "Cantidad",
                "unidad_medida": "Unidad",
                "precio_unitario": st.column_config.NumberColumn("Precio ($)", format=FMT_PESOS),
                "total": st.column_config.NumberColumn("Total ($)", format=FMT_PESOS),
                "medio_pago": "Medio de pago",
                "es_consumo_interno": st.column_config.CheckboxColumn("Consumo interno / sobrante"),
                "registrado_por": "Atendió",
            },
            hide_index=True, width="stretch"
        )

    st.subheader("Detalle de gastos del día")
    if cierre["gastos_detalle"].empty:
        st.info("No hay gastos registrados ese día.")
    else:
        st.dataframe(
            cierre["gastos_detalle"][["categoria", "descripcion", "monto"]],
            column_config={
                "categoria": "Categoría", "descripcion": "Descripción",
                "monto": st.column_config.NumberColumn("Monto ($)", format=FMT_PESOS),
            },
            hide_index=True, width="stretch"
        )

# ============================================================
# LISTA DE COMPRAS (reemplazó a la vieja "Sugerencia de reposición")
# ============================================================
elif pagina == "🛒 Lista de compras":
    st.title("🛒 Lista de compras")
    st.caption(
        "Qué comprar según lo que se espera vender (pronóstico por artículo), el stock actual y cómo se "
        "compra cada cosa: todos los días (pan y perecederos) o en el viaje al mayorista."
    )
    hoy_txt = str(hoy_argentina())
    ev = evaluacion_pronostico(hoy_txt, version=pron.VERSION_DATOS)
    params = db.obtener_parametros()
    R = int(params["frecuencia_viaje_dias"])
    pron_df = pronostico_para_comprar(hoy_txt, tuple(sorted(ev["eleccion"].items())), max(14, R),
                                      version=pron.VERSION_DATOS)
    Y_hist, _ = demanda_historica(hoy_txt, version=pron.VERSION_DATOS)
    meta_lc = lc.datos_articulos(db.engine)
    dispersion = dict(zip(ev["sigma"]["clase"], ev["sigma"]["dispersion"])) if ev["evaluada"] else {}

    # ---------------- Compra diaria ----------------
    st.subheader("🥖 Compra diaria")
    hora = datetime.now(ZONA_ARG).hour
    para = st.radio("Para", ["Hoy", "Mañana"], index=1 if hora >= 14 else 0, horizontal=True, key="lc_para")
    dia = hoy_argentina() if para == "Hoy" else hoy_argentina() + timedelta(days=1)
    diaria, viaje = lc.calcular(pron_df, meta_lc, params, dispersion, Y_hist, dia_compra_diaria=dia)
    hay_diarios = meta_lc["reposicion_dias"].fillna(0).eq(1).any()
    if dia not in set(pron_df["fecha_objetivo"]):
        st.warning("Todavía no hay pronóstico para ese día. Probá con la otra opción o recargá la página.")
    elif diaria.empty and hay_diarios:
        st.info("Los artículos de compra diaria no tienen venta esperada para ese día.")
    elif diaria.empty:
        st.info("No hay artículos marcados como compra diaria. Marcalos abajo, en ⚙️ Artículos de compra diaria.")
    else:
        st.dataframe(
            diaria.drop(columns=["articulo_id", "Proveedor", "Clase"]).style.format({
                "Venta esperada": "{:.1f}", "Tener para el día": "{:g}", "Ya tenés": "{:g}", "Comprar": "{:g}",
                "Sobrante esperado": "{:.1f}", "Prob. de agotarse": "{:.0%}", "Costo estimado": pesos_tabla,
            }),
            column_config={"Costo estimado": st.column_config.Column("Costo estimado (c/IVA)")},
            hide_index=True, width="stretch",
        )
        st.caption(
            "Cantidad que más ganancia deja en promedio: compara lo que se gana por cada unidad vendida con lo que "
            "se pierde por cada unidad que sobra"
            + ". Si preferís no quedarte sin, fijá un nivel de servicio propio abajo."
        )
        st.caption(
            "**Ya tenés** = stock actual que todavía se puede vender (dentro de su vida útil). "
            "Si vence en el día (vida útil de 1 día, como el pan), solo cuenta lo comprado hoy y solo para hoy. "
            "Si lo que sobró se regala o se tira, registralo en 💵 Registrar Venta → 🗑️ Sobrante del día: "
            "así sale del stock y no se descuenta de la compra."
            + (" Ojo: a esta hora todavía se puede vender parte de ese stock hoy." if para == "Mañana" and hora < 21 else "")
        )

    # ---------------- Viaje al mayorista ----------------
    st.subheader(f"🚚 Viaje al mayorista (cubre {R} días)")
    if viaje.empty:
        st.success("Con el stock actual no hace falta comprar nada para los próximos días.")
    else:
        c1, c2, c3 = st.columns(3)
        clases_lc = c1.multiselect("Clase", ["A", "B", "C"], default=["A", "B", "C"], key="lc_clases")
        provs = sorted(viaje["Proveedor"].unique())
        provs_sel = c2.multiselect("Proveedor", provs, default=provs, key="lc_provs")
        vista = viaje[viaje["Clase"].isin(clases_lc) & viaje["Proveedor"].isin(provs_sel)].copy()
        vista.insert(0, "Incluir", True)
        editada = st.data_editor(
            vista.drop(columns=["articulo_id"]),
            hide_index=True, width="stretch", height=420, key="lc_editor",
            disabled=[c for c in vista.columns if c not in ("Incluir", "Comprar")],
            column_config={
                "Incluir": st.column_config.CheckboxColumn("✔", width="small"),
                # step 0.1: con 1.0 la tabla mostraba redondeado lo que se compra por kg (0,4 kg -> "0")
                "Comprar": st.column_config.NumberColumn("Comprar", min_value=0.0, step=0.1, format="%g"),
                "Venta esperada": st.column_config.NumberColumn(f"Venta esperada ({R} d)", format="%.1f"),
                "Stock": st.column_config.NumberColumn(format="%g"),
                "Bulto": st.column_config.NumberColumn(format="%d"),
                "Alcanza (días)": st.column_config.NumberColumn(format="%.1f"),
                "Costo estimado": st.column_config.NumberColumn(
                    "Costo estimado c/IVA ($)", format=FMT_PESOS,
                    help="Comprar × costo de compra con IVA: lo que se paga en el mayorista."),
            },
        )
        elegida = editada[editada["Incluir"] & (editada["Comprar"] > 0)].copy()
        costo_unit = vista.loc[elegida.index, "articulo_id"].map(meta_lc.set_index("articulo_id")["costo_c_iva"])
        elegida["Costo estimado"] = elegida["Comprar"] * costo_unit.astype(float).fillna(0)
        c3.metric("Costo estimado del viaje (con IVA)", pesos_venta(round(elegida["Costo estimado"].sum())),
                  help="Lo que hay que llevar: costo de compra con IVA de lo seleccionado.")
        c3.caption(f"{len(elegida)} artículos seleccionados")
        st.caption(
            f"Para cada artículo: lo que se espera vender en {R} días + un margen de seguridad según su clase "
            f"(A {params['nivel_servicio_A']:.0%}, B {params['nivel_servicio_B']:.0%}, "
            f"C {params['nivel_servicio_C']:.0%} de probabilidad de no quedarse sin; lo que va en un combo toma "
            "la clase del combo si es mejor) − el stock actual, "
            "redondeado al bulto. Podés corregir la cantidad o destildar antes de copiar."
        )
        if (viaje["Aviso"].str.contains("negativo")).any():
            st.warning(
                f"{int(viaje['Aviso'].str.contains('negativo').sum())} artículos tienen stock negativo: hay compras "
                "sin cargar. La lista los toma como stock 0; conviene cargar esas facturas."
            )
        with st.expander("📋 Copiar para WhatsApp", expanded=False):
            st.code(lc.texto_whatsapp(elegida, diaria if para == "Hoy" else None,
                                      titulo=f"Lista de compras {hoy_argentina():%d/%m}"), language=None)

        # ---- Lista guardada (tanda 17, protocolo de validación) ----
        if db.hay_registro_viajes():
            hoy_l = hoy_argentina()
            ultima = db.listas_compra(1)
            guardada_hoy = (not ultima.empty) and pd.Timestamp(ultima["fecha"].iloc[0]).date() == hoy_l
            if st.button("🛒 Me voy al mayorista con esta lista", type="primary", key="lc_guardar_lista",
                         help="Guarda la lista tal como está (con tus cambios) para después compararla con las "
                              "facturas que cargues. Si la volvés a guardar hoy, reemplaza la anterior."):
                ed = editada.reindex(viaje.index)
                filas_l = pd.DataFrame({
                    "articulo_id": viaje["articulo_id"], "clase": viaje["Clase"], "proveedor": viaje["Proveedor"],
                    "stock": viaje["Stock"], "venta_esperada": viaje["Venta esperada"],
                    "sugerido": viaje["Comprar"].astype(float),
                    # lo que quedó afuera por los filtros de clase o proveedor cuenta como sacado
                    "cantidad": pd.to_numeric(ed["Comprar"], errors="coerce").fillna(0.0),
                    "incluido": ed["Incluir"].fillna(False).astype(bool),
                    "costo_c_iva": viaje["articulo_id"].map(meta_lc.set_index("articulo_id")["costo_c_iva"]).astype(float),
                })
                lid, reemplazo = db.guardar_lista_compra(hoy_l, filas_l)
                n_l = int((filas_l["incluido"] & (filas_l["cantidad"] > 0)).sum())
                avisar("success", f"Lista {'actualizada' if reemplazo else 'guardada'}: {n_l} artículos. "
                                  "Cuando vuelvas, cargá las facturas y anotá el viaje en 🧾 Registro de viajes (abajo).")
                _estrellitas_excursion()
                st.rerun()
            if guardada_hoy:
                hora_l = db.a_hora_argentina(ultima["actualizado_en"]).iloc[0]
                st.caption(f"✅ Ya guardaste la lista de hoy ({hora_l:%H:%M} h, {int(ultima['renglones'].iloc[0])} "
                           "artículos). Si la cambiás, tocá el botón otra vez: reemplaza la de hoy.")

    # ---------------- Registro de viajes y listas guardadas (tanda 17) ----------------
    if db.hay_registro_viajes():
        with st.expander("🧾 Registro de viajes y listas guardadas"):
            st.caption(
                "Anotá cada viaje al mayorista, también los de urgencia (cuando faltó algo y hubo que salir). "
                "Sirve para ver cuántas veces hay que salir de apuro y cuánto tiempo llevan los viajes."
            )
            listas_g = db.listas_compra(30)
            with st.form("nuevo_viaje", enter_to_submit=False, clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                fecha_v = c1.date_input("Fecha del viaje", value=hoy_argentina(), max_value=hoy_argentina(),
                                        format="DD/MM/YYYY", key="viaje_fecha")
                tipo_v = c2.radio("Tipo", ["Planificado", "De urgencia"], horizontal=True, key="viaje_tipo",
                                  help="De urgencia: no estaba previsto, salió porque faltaba algo.")
                horas_v = c3.number_input("Horas (ida, compra y vuelta)", min_value=0.0, max_value=24.0, step=0.5,
                                          value=None, placeholder="ej.: 3,5", key="viaje_horas")
                opciones_l = [None] + listas_g["id"].tolist()
                fechas_l = dict(zip(listas_g["id"], pd.to_datetime(listas_g["fecha"])))
                # por defecto, la lista guardada más reciente de los últimos 3 días
                recientes = [i for i in listas_g["id"]
                             if 0 <= (hoy_argentina() - fechas_l[i].date()).days <= 3]
                lista_v = st.selectbox(
                    "¿Con qué lista fuiste?", opciones_l,
                    index=opciones_l.index(recientes[0]) if recientes else 0,
                    format_func=lambda i: "No usé la lista de la app" if i is None else
                    f"Lista del {fechas_l[i]:%d/%m} ({int(listas_g.set_index('id').loc[i, 'renglones'])} artículos)",
                    # la clave cambia con cada lista nueva: si no, queda elegido "No usé la lista"
                    # de antes de guardarla
                    key=f"viaje_lista_{int(listas_g['id'].max()) if not listas_g.empty else 0}")
                nota_v = st.text_input("Nota (opcional)", placeholder="ej.: faltaba aceite", key="viaje_nota")
                if st.form_submit_button("💾 Guardar viaje"):
                    db.registrar_viaje(fecha_v, "urgencia" if tipo_v == "De urgencia" else "planificado",
                                       horas_v, lista_v, nota_v.strip())
                    avisar("success", f"Viaje del {fecha_v:%d/%m} guardado.")
                    _estrellitas_excursion()
                    st.rerun()

            viajes_g = db.listar_viajes()
            if not viajes_g.empty:
                st.markdown("**Viajes anotados**")
                vg = viajes_g.assign(
                    Fecha=pd.to_datetime(viajes_g["fecha"]).dt.strftime("%d/%m/%Y"),
                    Tipo=viajes_g["tipo"].map({"planificado": "Planificado", "urgencia": "De urgencia"}),
                    Horas=viajes_g["horas"].map(lambda h: "—" if pd.isna(h) else f"{h:g}"),
                    Lista=viajes_g["lista_fecha"].map(lambda f: "sin lista" if pd.isna(f) else
                                                      f"la del {pd.Timestamp(f):%d/%m}"),
                    Nota=viajes_g["nota"].fillna(""), Anotó=viajes_g["persona"].fillna("—"),
                )
                st.dataframe(vg[["Fecha", "Tipo", "Horas", "Lista", "Nota", "Anotó"]], hide_index=True, width="stretch")
                with st.popover("🗑️ Borrar un viaje cargado por error"):
                    borrar_v = st.selectbox("Viaje", viajes_g["id"].tolist(), key="viaje_borrar",
                                            format_func=lambda i: " · ".join(
                                                vg.set_index("id").loc[i, ["Fecha", "Tipo", "Horas"]].astype(str)))
                    if st.button("Borrar", key="viaje_borrar_btn"):
                        db.borrar_viaje(borrar_v)
                        avisar("success", "Viaje borrado.")
                        st.rerun()

            if listas_g.empty:
                st.info("Todavía no hay listas guardadas. Se guardan con el botón 🛒 Me voy al mayorista con esta lista.")
            else:
                st.markdown("**¿Se compró lo de la lista?**")
                st.caption(
                    f"Cada lista guardada contra las facturas de los días que cubre: ese día y los "
                    f"{db.dias_ventana_lista()} siguientes (o hasta la lista siguiente). Entran también los "
                    "distribuidores que traen al local; no cuentan la compra diaria ni la consignación."
                )
                filas_r = []
                for r in listas_g.head(8).itertuples():
                    _, res, ven = db.comparar_lista_compra(r.id)
                    abierta = ven["hasta"] >= hoy_argentina()
                    esperando = abierta and not res["hubo_compras"]
                    if esperando:   # todavía no se cargó ninguna factura: no es "0 % comprado"
                        res["pct_lista_comprada"] = res["pct_comprado_igual"] = None
                    filas_r.append({
                        "id": r.id, "Lista del": pd.Timestamp(r.fecha).strftime("%d/%m/%Y"),
                        "Sugeridos": res["sugeridos"], "Se llevó": res["llevados"],
                        "Cambió al armarla": res["cambiados_al_armar"],
                        "Se compró": "—" if res["pct_lista_comprada"] is None else f"{res['pct_lista_comprada']:.0%}",
                        "Igual a la lista": "—" if res["pct_comprado_igual"] is None else f"{res['pct_comprado_igual']:.0%}",
                        "Agregados": res["agregados"],
                        "Estado": "esperando facturas" if esperando else
                                  ("abierta (hasta el " + f"{ven['hasta']:%d/%m})" if abierta else "cerrada"),
                    })
                tabla_r = pd.DataFrame(filas_r)
                st.dataframe(tabla_r.drop(columns=["id"]), hide_index=True, width="stretch")
                ver_l = st.selectbox("Ver el detalle de", tabla_r["id"].tolist(), key="lista_detalle",
                                     format_func=lambda i: "lista del " + tabla_r.set_index("id").loc[i, "Lista del"])
                det_l, res_l, ven_l = db.comparar_lista_compra(ver_l)
                filtro_e = st.multiselect("Mostrar", lc.ESTADOS_LISTA, default=lc.ESTADOS_LISTA, key="lista_estados")
                vista_l = det_l[det_l["estado"].isin(filtro_e)].assign(
                    Llevó=lambda d: d["cantidad"].astype(float).where(d["llevado"].astype(bool), 0.0))
                st.dataframe(
                    vista_l[["nombre", "estado", "sugerido", "Llevó", "comprado"]].rename(columns={
                        "nombre": "Artículo", "estado": "Qué pasó", "sugerido": "Sugirió la app",
                        "comprado": "Se compró (facturas)"}),
                    hide_index=True, width="stretch",
                    column_config={c: st.column_config.NumberColumn(format="%g")
                                   for c in ["Sugirió la app", "Llevó", "Se compró (facturas)"]},
                )
                st.caption(
                    f"Facturas del {ven_l['desde']:%d/%m} al {ven_l['hasta']:%d/%m}. "
                    f"Lista: {pesos_venta(round(res_l['costo_lista']))} estimado · facturado "
                    f"{pesos_venta(round(res_l['monto_comprado']))} con IVA"
                    + (f" (de eso, {pesos_venta(round(res_l['monto_agregados']))} en agregados)"
                       if res_l["agregados"] else "") + "."
                )

    # ---------------- Configuración de compra diaria ----------------
    with st.expander("⚙️ Artículos de compra diaria y perecederos"):
        st.caption(
            "Marcá lo que comprás todos los días (pan, etc.) y cuántos días se puede vender antes de tirarlo o "
            "regalarlo. Abajo aparecen sugerencias: artículos que se compran seguido."
        )
        marcados = meta_lc[meta_lc["reposicion_dias"].notna() | meta_lc["vida_util_dias"].notna()]
        sugeridos = lc.candidatos_compra_diaria(db.engine)
        filas_cfg = pd.concat([
            marcados[["articulo_id", "nombre", "reposicion_dias", "vida_util_dias", "nivel_servicio"]],
            sugeridos[~sugeridos["articulo_id"].isin(marcados["articulo_id"])][["articulo_id", "nombre"]]
            .assign(reposicion_dias=None, vida_util_dias=None, nivel_servicio=None),
        ], ignore_index=True)
        cfg = pd.DataFrame({
            "articulo_id": filas_cfg["articulo_id"],
            "Artículo": filas_cfg["nombre"],
            "Compra diaria": pd.to_numeric(filas_cfg["reposicion_dias"], errors="coerce").fillna(0) == 1,
            "Vida útil (días)": pd.to_numeric(filas_cfg["vida_util_dias"], errors="coerce").astype("Float64"),
            "Nivel de servicio propio (%)": (pd.to_numeric(filas_cfg["nivel_servicio"], errors="coerce")
                                             * 100).astype("Float64"),
        })
        # Solo artículos que se compran: sin combos (se compran sus componentes)
        # ni sueltos fraccionados (se compra la bolsa).
        comprables = meta_lc[(meta_lc["categoria"] != "Promocion") & meta_lc["articulo_fraccion_de"].isna()
                             & ~meta_lc["articulo_id"].isin(cfg["articulo_id"])].sort_values("nombre")
        nombres = meta_lc.set_index("articulo_id")["nombre"]
        otro = st.selectbox(
            "Agregar otro artículo", [None] + comprables["articulo_id"].tolist(),
            format_func=lambda i: "—" if i is None else nombres.get(i, str(i)),
            key="lc_agregar",
        )
        if otro is not None:
            fila_nueva = pd.DataFrame([{
                "articulo_id": otro, "Artículo": nombres.get(otro, str(otro)), "Compra diaria": False,
                "Vida útil (días)": pd.NA, "Nivel de servicio propio (%)": pd.NA,
            }]).astype({"Vida útil (días)": "Float64", "Nivel de servicio propio (%)": "Float64"})
            cfg = pd.concat([cfg, fila_nueva], ignore_index=True)
        cfg_ed = st.data_editor(
            cfg, hide_index=True, width="stretch", key="lc_cfg",
            disabled=["articulo_id", "Artículo"],
            column_config={
                "articulo_id": None,
                "Vida útil (días)": st.column_config.NumberColumn(min_value=1, max_value=365, step=1, format="%d"),
                "Nivel de servicio propio (%)": st.column_config.NumberColumn(
                    min_value=50.0, max_value=99.0, step=1.0, format="%.0f",
                    help="Vacío = automático. Ej.: 80 % = quedarse sin solo 1 de cada 5 días."),
            },
        )
        if st.button("💾 Guardar", key="lc_guardar"):
            cambios = 0
            for _, r in cfg_ed.iterrows():
                cambios += db.actualizar_reposicion_articulo(
                    int(r["articulo_id"]),
                    1 if r["Compra diaria"] else None,
                    int(r["Vida útil (días)"]) if pd.notna(r["Vida útil (días)"]) else None,
                    float(r["Nivel de servicio propio (%)"]) / 100 if pd.notna(r["Nivel de servicio propio (%)"]) else None,
                )
            avisar("success", f"Guardado ({cambios} artículo/s modificado/s).")
            st.rerun()

# ============================================================
# PRECIOS POR PROVEEDOR
# ============================================================
elif pagina == "🏭 Proveedores":
    st.title("🏭 Proveedores")
    tab_des, tab_consig, tab_precios = st.tabs(["📊 Desempeño", "🤝 Consignación", "🏷️ Precios"])

    # ---------------- Desempeño (HU-025) ----------------
    with tab_des:
        st.caption(
            "Qué te deja cada proveedor, cuánta plata te tiene en mercadería, cada cuánto le comprás, "
            "cuánto te aumenta y si sus productos se quedan sin stock. Las ventas, la ganancia y el stock "
            "de cada artículo se reparten entre sus proveedores según cuánto le compraste a cada uno."
        )
        hoy_p = hoy_argentina()
        c1, c2 = st.columns(2)
        desde_p = c1.date_input("Desde", value=hoy_p - timedelta(days=30), format="DD/MM/YYYY", key="prov_desde")
        hasta_p = c2.date_input("Hasta", value=hoy_p, format="DD/MM/YYYY", key="prov_hasta")
        if desde_p > hasta_p:
            st.error("La fecha 'Desde' es posterior a 'Hasta'.")
        else:
            tabla_p, det_p = anprov.desempeno(db.engine, desde_p, hasta_p)
            if tabla_p.empty:
                st.info("No hay compras ni ventas en ese período.")
            else:
                con_compras = tabla_p[tabla_p["proveedor"] != anprov.SIN_PROVEEDOR]
                m1, m2, m3 = st.columns(3)
                m1.metric("Compraste (sin IVA)", pesos_venta(float(tabla_p["compras"].sum())),
                          help="Facturas de compra del período, sin IVA.")
                m2.metric("Ganancia de sus productos", pesos_venta(float(con_compras["ganancia"].sum())),
                          help="Ganancia real de lo vendido, repartida por proveedor. No incluye lo que nunca "
                               "tuvo factura de compra (stock de la carga inicial).")
                top = con_compras.head(3)
                if con_compras["ganancia"].sum() > 0 and not top.empty:
                    m3.metric("Los 3 que más te dejan", f"{top['ganancia'].sum() / con_compras['ganancia'].sum():.0%}",
                              help=", ".join(top["proveedor"]))
                def _txt(v, dec=1, suf=""):
                    # texto (no número) para que lo que no se puede medir se vea "—" y no "None"
                    if pd.isna(v):
                        return "—"
                    return f"{v:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".") + suf
                vista = tabla_p.assign(
                    proveedor=tabla_p["proveedor"] + tabla_p["consignacion"].map({True: " 🤝", False: ""}),
                    ganancia=tabla_p["ganancia"].round(0), venta_neta=tabla_p["venta_neta"].round(0),
                    compras=tabla_p["compras"].round(0), stock_costo=tabla_p["stock_costo"].round(0),
                    pct_compras=(tabla_p["pct_compras"] * 100).map(lambda v: _txt(v, 1, " %")),
                    margen=(tabla_p["margen"] * 100).map(lambda v: _txt(v, 1, " %")),
                    cada_dias=tabla_p["cada_dias"].map(lambda v: _txt(v, 1)),
                    cobertura_dias=tabla_p["cobertura_dias"].map(lambda v: _txt(v, 0)),
                    gmroi=tabla_p["gmroi"].map(lambda v: _txt(v, 1)),
                    aumento_mensual=(tabla_p["aumento_mensual"] * 100).map(lambda v: _txt(v, 1, " %")),
                    sobreprecio=(tabla_p["sobreprecio"] * 100).map(lambda v: _txt(v, 1, " %")),
                    dias_sin_stock=(tabla_p["dias_sin_stock"] * 100).map(lambda v: _txt(v, 1, " %")),
                )
                st.dataframe(
                    vista[["proveedor", "ganancia", "margen", "venta_neta", "compras", "pct_compras", "facturas",
                           "cada_dias", "stock_costo", "cobertura_dias", "gmroi", "aumento_mensual",
                           "sobreprecio", "dias_sin_stock", "articulos"]],
                    column_config={
                        "proveedor": "Proveedor",
                        "ganancia": st.column_config.NumberColumn("Ganancia ($)", format=FMT_PESOS),
                        "margen": st.column_config.TextColumn("Margen"),
                        "venta_neta": st.column_config.NumberColumn("Vendido sin IVA ($)", format=FMT_PESOS),
                        "compras": st.column_config.NumberColumn("Compraste sin IVA ($)", format=FMT_PESOS),
                        "pct_compras": st.column_config.TextColumn("De tus compras"),
                        "facturas": st.column_config.NumberColumn("Facturas", format="%d"),
                        "cada_dias": st.column_config.TextColumn(
                            "Le comprás cada (días)", help="— = una sola factura: todavía no hay frecuencia."),
                        "stock_costo": st.column_config.NumberColumn("En stock a costo ($)", format=FMT_PESOS),
                        "cobertura_dias": st.column_config.TextColumn("Stock alcanza (días)"),
                        "gmroi": st.column_config.TextColumn(
                            "Rinde por $1 en stock (al año)",
                            help="GMROI: ganancia de un año a este ritmo, dividida por lo que tenés de ese "
                                 "proveedor en stock a costo. 5 = cada $1 en su mercadería te deja $5 por año."),
                        "aumento_mensual": st.column_config.TextColumn(
                            "Aumento por mes",
                            help="Cuánto subió el costo, llevado a un mes, entre la primera y la última compra de "
                                 "cada artículo (al menos 7 días entre ambas). Vacío: no hay dos compras para comparar."),
                        "sobreprecio": st.column_config.TextColumn(
                            "Más caro que el más barato",
                            help="Solo artículos que te vende más de un proveedor. 0 = siempre el más barato."),
                        "dias_sin_stock": st.column_config.TextColumn(
                            "Días sin stock",
                            help="De los días del período, en cuántos sus productos arrancaron el día sin stock y "
                                 "no se vendieron. Mide disponibilidad: el faltante puede ser del proveedor o de no "
                                 "haberle comprado a tiempo."),
                        "articulos": st.column_config.NumberColumn("Artículos", format="%d"),
                    },
                    hide_index=True, width="stretch",
                )
                st.caption("🤝 = en consignación (se paga cuando se vende). "
                           f"\"{anprov.SIN_PROVEEDOR}\": productos que nunca tuvieron una factura cargada "
                           "(stock de la carga inicial); su ganancia es real pero no se le puede atribuir a nadie.")
                graf = con_compras[con_compras["ganancia"] != 0].head(12)
                if not graf.empty:
                    st.altair_chart(
                        alt.Chart(graf).mark_bar().encode(
                            x=alt.X("ganancia:Q", title="Ganancia del período ($)"),
                            y=alt.Y("proveedor:N", sort="-x", title=None),
                            tooltip=[alt.Tooltip("proveedor:N", title="Proveedor"),
                                     alt.Tooltip("ganancia:Q", title="Ganancia ($)", format=",.0f"),
                                     alt.Tooltip("compras:Q", title="Compras ($)", format=",.0f")],
                        ).properties(height=28 * len(graf) + 20),
                        width="stretch",
                    )
                with st.expander("Ver los artículos de un proveedor"):
                    elegido = st.selectbox("Proveedor", tabla_p["proveedor"].tolist(), key="prov_detalle")
                    d = det_p[det_p["proveedor"] == elegido].sort_values("ganancia", ascending=False)
                    st.dataframe(
                        d[["nombre", "share", "venta_neta", "ganancia", "stock_costo"]].assign(
                            share=d["share"] * 100, venta_neta=d["venta_neta"].round(0),
                            ganancia=d["ganancia"].round(0), stock_costo=d["stock_costo"].round(0)),
                        column_config={
                            "nombre": "Artículo",
                            "share": st.column_config.NumberColumn("Le corresponde (%)", format="%.0f",
                                                                   help="Parte del artículo comprada a este proveedor."),
                            "venta_neta": st.column_config.NumberColumn("Vendido sin IVA ($)", format=FMT_PESOS),
                            "ganancia": st.column_config.NumberColumn("Ganancia ($)", format=FMT_PESOS),
                            "stock_costo": st.column_config.NumberColumn("En stock a costo ($)", format=FMT_PESOS),
                        },
                        hide_index=True, width="stretch",
                    )
                st.caption("No se mide el plazo de entrega ni si cumple los pedidos: la app no registra pedidos, "
                           "solo lo que llega con factura.")

    # ---------------- Consignación (tanda 14) ----------------
    with tab_consig:
        if not db.hay_consignacion():
            st.info("Para usar consignación falta correr la migración de la tanda 14 (migracion_tanda14.py).")
        else:
            st.caption(
                "Proveedores que dejan mercadería y cobran recién lo vendido. La reposición se carga como una "
                "factura (así entra el stock); acá ves cuánto se le debe desde la última vez que cobró. "
                "El pago no se carga en Gastos: ese costo ya se descuenta en la ganancia de cada venta."
            )
            consig = db.proveedores_consignacion()
            if consig.empty:
                st.info("No hay proveedores en consignación.")
            else:
                pid_c = st.selectbox("Proveedor", consig["id"].tolist(),
                                     format_func=dict(zip(consig["id"], consig["nombre"])).get, key="consig_prov")
                ult = db.ultima_liquidacion(pid_c)
                desde_c = None
                if ult is None:
                    st.caption("Todavía no hay ningún pago registrado. Si hoy no se le debe nada, empezá en cero; "
                               "si se le debe, elegí desde cuándo.")
                    if st.button("✅ Está al día: empezar desde ahora", key=f"consig_al_dia_{pid_c}"):
                        db.registrar_corte_inicial(pid_c)
                        avisar("success", "Listo: desde ahora se cuenta lo que se vende de este proveedor.")
                        st.rerun()
                    desde_c = st.date_input(
                        "¿Desde cuándo se le debe? (la última vez que cobró)", value=hoy_argentina() - timedelta(days=30),
                        format="DD/MM/YYYY", key=f"consig_desde_{pid_c}",
                        help="Solo hace falta la primera vez: después cada pago marca hasta dónde se le pagó.")
                else:
                    st.caption(f"Última vez que cobró: {pd.to_datetime(ult['fecha']):%d/%m/%Y} "
                               f"({md_pesos(float(ult['monto_pagado']))}).")
                det_c, total_c, primera_c, ultima_c = db.consignacion_pendiente(pid_c, desde_c)
                st.metric("Se le debe (lo vendido a costo)", pesos_venta(total_c))
                if det_c.empty:
                    st.info("No se vendió nada suyo desde la última liquidación.")
                else:
                    st.dataframe(
                        det_c[["articulo", "cantidad", "a_pagar", "desde", "hasta"]],
                        column_config={
                            "articulo": "Artículo", "cantidad": st.column_config.NumberColumn("Vendido", format="%g"),
                            "a_pagar": st.column_config.NumberColumn("A pagar ($)", format=FMT_PESOS),
                            "desde": st.column_config.DateColumn("Desde", format="DD/MM/YYYY"),
                            "hasta": st.column_config.DateColumn("Hasta", format="DD/MM/YYYY"),
                        },
                        hide_index=True, width="stretch",
                    )
                    st.caption("Incluye lo vendido dentro de promos y lo usado como consumo interno: también se le debe. "
                               "El costo es el cargado en la factura (lo que te cobra por unidad).")
                    with st.form(f"form_liquidar_{pid_c}", enter_to_submit=False):
                        st.markdown("**Registrar que se le pagó**")
                        f1, f2 = st.columns(2)
                        pagado_c = f1.number_input("Le pagaste ($)", min_value=0.0, value=round(total_c, 2), step=100.0)
                        fecha_c = f2.date_input("Fecha del pago", value=hoy_argentina(), format="DD/MM/YYYY")
                        nota_c = st.text_input("Nota (opcional)", placeholder="ej.: contó 3 adaptógenos menos")
                        if st.form_submit_button("💾 Registrar pago", type="primary"):
                            db.registrar_liquidacion(pid_c, primera_c, ultima_c, total_c, pagado_c, fecha_c,
                                                     nota_c or None)
                            avisar("success", f"Pago de {pesos_venta(pagado_c)} registrado. La próxima cuenta "
                                              "arranca desde acá.")
                            st.rerun()
                with st.expander("📋 Para contar con el proveedor cuando pase"):
                    st.caption("Lo que dice la app que hay de cada producto suyo. Si él cuenta otra cosa, "
                               "corregí el stock en Artículos (y anotalo en la nota del pago).")
                    st.dataframe(db.stock_para_contar(pid_c)[["nombre", "stock_app"]],
                                 column_config={"nombre": "Artículo",
                                                "stock_app": st.column_config.NumberColumn("Según la app", format="%g")},
                                 hide_index=True, width="stretch")
                liqs = db.listar_liquidaciones(pid_c)
                if not liqs.empty:
                    with st.expander(f"Pagos anteriores ({len(liqs)})"):
                        st.dataframe(liqs[["fecha", "monto_calculado", "monto_pagado", "nota"]], column_config={
                            "fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
                            "monto_calculado": st.column_config.NumberColumn("Calculado ($)", format=FMT_PESOS),
                            "monto_pagado": st.column_config.NumberColumn("Pagado ($)", format=FMT_PESOS),
                            "nota": "Nota"}, hide_index=True, width="stretch")
            if ES_DUENO:
                with st.expander("Marcar o desmarcar un proveedor en consignación"):
                    todos_p = db.listar_proveedores()
                    pid_m = st.selectbox("Proveedor", todos_p["id"].tolist(),
                                         format_func=dict(zip(todos_p["id"], todos_p["nombre"].str.strip())).get,
                                         key="consig_marcar_prov")
                    actual_m = bool(todos_p.set_index("id").loc[pid_m].get("consignacion", False))
                    nuevo_m = st.checkbox("En consignación (se le paga lo vendido)", value=actual_m,
                                          key=f"consig_marcar_{pid_m}")
                    if nuevo_m != actual_m and st.button("Guardar", key=f"consig_guardar_{pid_m}"):
                        db.marcar_consignacion(pid_m, nuevo_m)
                        avisar("success", "Guardado.")
                        st.rerun()

    # ---------------- Precios (lo que antes era la pantalla entera) ----------------
    with tab_precios:
        st.caption(
            "Compara el costo de compra vigente de cada artículo entre los distintos proveedores "
            "que lo facturaron. Se completa solo a medida que cargás facturas -- por ahora solo "
            "muestra artículos con más de un proveedor cargado."
        )

        comparacion = db.comparar_precios_proveedores()
        if comparacion.empty:
            st.info(
                "Todavía no hay ningún artículo comprado a más de un proveedor. "
                "En cuanto cargues una factura de un segundo proveedor para el mismo artículo, va a aparecer acá."
            )
        else:
            # Sin color de fondo (con el tema oscuro el verde claro dejaba el texto
            # ilegible): el más barato se marca con texto y se muestra cuánto más
            # caro es cada uno de los otros.
            tabla_pp = comparacion[["articulo", "proveedor", "costo_compra", "actualizado_en", "es_mas_barato"]].copy()
            minimo_pp = tabla_pp.groupby("articulo")["costo_compra"].transform("min").astype(float)
            tabla_pp["vs_barato"] = (tabla_pp["costo_compra"].astype(float) / minimo_pp.where(minimo_pp > 0) - 1) * 100
            tabla_pp["es_mas_barato"] = tabla_pp["es_mas_barato"].map(lambda b: "✅ Más barato" if b else "")
            tabla_pp = tabla_pp.sort_values(["articulo", "costo_compra"])
            st.dataframe(
                tabla_pp[["articulo", "proveedor", "costo_compra", "vs_barato", "es_mas_barato", "actualizado_en"]],
                column_config={
                    "articulo": "Artículo", "proveedor": "Proveedor",
                    "costo_compra": st.column_config.NumberColumn("Costo de compra s/IVA ($)", format=FMT_PESOS),
                    "vs_barato": st.column_config.NumberColumn("Más caro que el más barato (%)", format=FMT_PESOS),
                    "es_mas_barato": "",
                    "actualizado_en": st.column_config.DatetimeColumn("Última actualización", format="DD/MM/YYYY"),
                },
                hide_index=True, width="stretch"
            )

# ============================================================
# PRONÓSTICO DE DEMANDA (Sprint 2, HU-014)
# ============================================================
elif pagina == "🔮 Pronóstico de demanda":
    st.title("🔮 Pronóstico de demanda")
    st.caption(
        "Cuánto se espera vender de cada artículo en los próximos días, según su historia de ventas "
        "(incluye lo que se va en combos y el consumo interno; lo suelto fraccionado cuenta en su bolsa)."
    )
    hoy_txt = str(hoy_argentina())
    ev = evaluacion_pronostico(hoy_txt, version=pron.VERSION_DATOS)
    eleccion = ev["eleccion"]
    nombre_modelo = {
        "promedio_30": "Promedio de 30 días (método anterior)",
        "ses_0.02": "Nivel suavizado, memoria larga (~100 días)",
        "ses_0.05": "Nivel suavizado, memoria media (~40 días)",
        "ses_0.10": "Nivel suavizado, memoria corta (~20 días)",
        "croston_0.05": "Croston-SBA (venta intermitente), memoria media",
        "croston_0.10": "Croston-SBA (venta intermitente), memoria corta",
    }

    st.subheader("¿Qué tan bien pronostica?")
    if not ev["evaluada"]:
        faltan = 28 + 7 + pron.MIN_ORIGENES_EVALUACION - 1 - ev["dias_historia"]
        st.info(
            f"Hay {ev['dias_historia']} días de ventas registradas. Para evaluar los modelos con datos "
            f"propios hacen falta unos {faltan} días más; mientras tanto se usa "
            f"**{nombre_modelo[pron.CONFIG_POR_DEFECTO].lower()}**, ajustado por día de semana y época del mes "
            "cuando haya 4 semanas de datos."
        )
    else:
        met = ev["metricas"]
        filas = []
        for clase in "ABC":
            elegido = eleccion.get(clase)
            m_el = met[(met["clase"] == clase) & (met["modelo"] == elegido)]
            m_ba = met[(met["clase"] == clase) & (met["modelo"] == "promedio_30")]
            if m_el.empty or m_ba.empty:
                continue
            e, b = float(m_el["wape_h_%"].iloc[0]), float(m_ba["wape_h_%"].iloc[0])
            filas.append({
                "Clase": clase, "Modelo elegido": nombre_modelo.get(elegido, elegido),
                "Error 7 días (modelo)": f"{e:.1f} %", "Error 7 días (método anterior)": f"{b:.1f} %",
                "Mejora": f"{(1 - e / b) * 100:.1f} %" if b else "-",
                "Sesgo": f"{float(m_el['sesgo_%'].iloc[0]):+.1f} %",
            })
        st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch")
        st.caption(
            f"Evaluación con {ev['dias_historia']} días de historia y {ev.get('origenes', 0)} días de prueba: "
            "cada día se pronosticaron los 7 siguientes usando solo los datos anteriores, y se comparó con lo "
            "que realmente se vendió. Error = error absoluto / unidades vendidas (WAPE) de la semana; "
            "sesgo positivo = sobreestima. Ojo: se compara con lo vendido, y cuando un artículo se quedó sin "
            "stock lo vendido es menos de lo que se quería comprar; por eso el pronóstico puede aparecer algo "
            "por encima de la venta sin estar equivocado."
        )
        fuera = ev.get("fuera_de_muestra")
        if fuera is not None and not fuera.empty:
            with st.expander("Control: error medido con días que la elección no vio"):
                st.dataframe(pd.DataFrame({
                    "Clase": fuera["clase"],
                    "Elegido con la 1.ª mitad": fuera["modelo_elegido"].map(lambda m: nombre_modelo.get(m, m)),
                    "Error en la 1.ª mitad": fuera["wape_seleccion_%"].map(lambda v: f"{v:.1f} %"),
                    "Error en la 2.ª mitad": fuera["wape_fuera_%"].map(lambda v: f"{v:.1f} %"),
                    "Mejora vs método anterior (2.ª mitad)": fuera["mejora_fuera_%"].map(
                        lambda v: f"{v:.1f} %" if pd.notna(v) else "-"),
                }), hide_index=True, width="stretch")
                st.caption("Elegir el modelo y medir su error con los mismos días da un número algo optimista. "
                           "Acá se elige con la primera mitad de los días de prueba y se mide con la segunda.")
    censura = ev.get("censura") or {}
    if censura.get("dias_censurados"):
        st.caption(
            f"🧺 Días sin stock: en {censura['corregidos']} artículos hubo {censura['dias_censurados']} días en que no se "
            "vendió porque no había. Esos días no cuentan como \"demanda cero\" para el pronóstico. "
            + (f"{censura['no_confiables']} artículos no se corrigen porque su stock no cierra (faltan compras cargadas)."
               if censura.get("no_confiables") else "")
        )

    st.subheader("¿Se cumplió el pronóstico?")
    seg = seguimiento_cacheado(hoy_txt, tuple(sorted(eleccion.items())), version=pron.VERSION_DATOS)
    if seg.empty:
        st.info("Todavía no hay días pasados para comparar.")
    else:
        # Gráfico (tanda 15): total de unidades por día, lo vendido contra lo que se
        # pronosticaba ese día, y a continuación el pronóstico de los próximos días.
        # Mismas series y colores que el "Detalle de un artículo" de más abajo.
        fut_total = pronostico_cacheado(hoy_txt, tuple(sorted(eleccion.items())), version=pron.VERSION_DATOS)
        clase_graf = st.segmented_control("Clase", ["Todas", "A", "B", "C"], default="Todas",
                                          key="seg_clase_graf") or "Todas"
        seg_g = seg if clase_graf == "Todas" else seg[seg["clase"] == clase_graf]
        fut_g = fut_total if (clase_graf == "Todas" or fut_total.empty) else fut_total[fut_total["clase"] == clase_graf]
        pasado_dia = seg_g.groupby("fecha", as_index=False)[["real", "pronostico"]].sum()
        futuro_dia = (fut_g.groupby("fecha_objetivo", as_index=False)["demanda"].sum()
                      if not fut_g.empty else pd.DataFrame(columns=["fecha_objetivo", "demanda"]))
        SERIES_SEG = ["Vendido", "Pronosticado ese día", "Pronóstico próximos días"]
        serie_seg = pd.concat([
            pd.DataFrame({"Fecha": pd.to_datetime(pasado_dia["fecha"]), "Unidades": pasado_dia["real"].astype(float),
                          "Serie": SERIES_SEG[0]}),
            pd.DataFrame({"Fecha": pd.to_datetime(pasado_dia["fecha"]), "Unidades": pasado_dia["pronostico"].astype(float),
                          "Serie": SERIES_SEG[1]}),
            pd.DataFrame({"Fecha": pd.to_datetime(futuro_dia["fecha_objetivo"]),
                          "Unidades": futuro_dia["demanda"].astype(float), "Serie": SERIES_SEG[2]}),
        ], ignore_index=True)
        oscuro_seg = getattr(getattr(st.context, "theme", None), "type", None) == "dark"
        colores_seg = ["#3987e5", "#199e70", "#d95926"] if oscuro_seg else ["#2a78d6", "#1baf7a", "#eb6834"]
        lineas_seg = alt.Chart(serie_seg).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=30)).encode(
            x=alt.X("Fecha:T", title=None, axis=alt.Axis(format="%d/%m", grid=False)),
            y=alt.Y("Unidades:Q", title="Unidades por día (todos los artículos)", axis=alt.Axis(gridOpacity=0.3)),
            color=alt.Color("Serie:N", scale=alt.Scale(domain=SERIES_SEG, range=colores_seg),
                            legend=alt.Legend(title=None, orient="top")),
            strokeDash=alt.StrokeDash("Serie:N", scale=alt.Scale(domain=SERIES_SEG,
                                                                 range=[[1, 0], [2, 3], [6, 4]]), legend=None),
            tooltip=[alt.Tooltip("Fecha:T", format="%A %d/%m/%Y"), "Serie:N",
                     alt.Tooltip("Unidades:Q", format=",.0f")],
        )
        hoy_marca = alt.Chart(pd.DataFrame({"Fecha": [pd.Timestamp(hoy_txt)]})).mark_rule(
            strokeDash=[2, 2], color="#8a8f98").encode(x="Fecha:T")
        hoy_texto = alt.Chart(pd.DataFrame({"Fecha": [pd.Timestamp(hoy_txt)], "t": ["hoy"]})).mark_text(
            align="left", dx=4, color="#8a8f98", fontSize=12).encode(x="Fecha:T", y=alt.value(8), text="t:N")
        st.altair_chart((lineas_seg + hoy_marca + hoy_texto).properties(height=300), width="stretch")
        st.caption(
            "A la izquierda de \"hoy\", cada punto verde es lo que el modelo pronosticaba para ese día con las "
            "ventas hasta el día anterior; si la línea verde va pareja con la azul, el pronóstico acompaña la "
            "venta real. A la derecha, lo que se espera vender. Son unidades sumadas de todos los artículos "
            "(o de la clase elegida): sirve para ver la tendencia y el sesgo, no para comparar productos."
        )

        res = pron.resumen_seguimiento(seg)
        st.dataframe(pd.DataFrame({
            "Clase": res["clase"], "Días": res["dias"], "Artículos": res["articulos"],
            "Vendido": res["vendido"].round(0), "Pronosticado": res["pronosticado"].round(0),
            "Sesgo": res["sesgo_%"].map(lambda v: f"{v:+.1f} %" if pd.notna(v) else "-"),
            "Error semanal": res["error_semanal_%"].map(lambda v: f"{v:.1f} %" if pd.notna(v) else "-"),
        }), hide_index=True, width="stretch")
        n_guard = int((seg["fuente"] == "guardado").sum())
        st.caption(
            f"Últimos {seg['fecha'].nunique()} días. Para cada día se compara lo vendido con lo que se pronosticaba "
            "ese día usando solo las ventas anteriores. Sesgo positivo = se pronosticó de más. Error semanal = "
            "diferencia entre lo vendido y lo pronosticado por artículo en cada semana, sobre lo vendido. "
            + (f"{n_guard} pronósticos son los que la app guardó ese día; el resto se reconstruyó con el modelo actual "
               "(puede salir algo mejor que el real, porque el modelo se ajustó con esos días)."
               if n_guard else "Todos reconstruidos con el modelo actual: los guardados por la app se suman día a día.")
        )

    df = pronostico_cacheado(hoy_txt, tuple(sorted(eleccion.items())), version=pron.VERSION_DATOS)
    if df.empty:
        st.info("Todavía no hay ventas registradas para pronosticar.")
    else:
        Y, arts_meta = demanda_historica(hoy_txt, version=pron.VERSION_DATOS)
        meta = arts_meta.set_index("articulo_id").copy()
        # El stock se lee en el momento (tanda 16): lo de demanda_historica se
        # guarda en memoria hasta una hora y mostraba un stock viejo.
        stock_ahora = db.stock_actual_por_id()
        meta["stock_actual"] = [stock_ahora.get(int(i), s) for i, s in zip(meta.index, meta["stock_actual"])]
        piv = df.pivot_table(index="articulo_id", columns="fecha_objetivo", values="demanda", aggfunc="sum")
        tabla = pd.DataFrame({
            "articulo_id": piv.index,
            "Artículo": meta.loc[piv.index, "nombre"].values,
            "Clase": df.groupby("articulo_id")["clase"].first().loc[piv.index].values,
            "Próx. 7 días": piv.iloc[:, :7].sum(axis=1).values,
            "Stock": meta.loc[piv.index, "stock_actual"].astype(float).values,
        })
        acumulado = piv.cumsum(axis=1).values
        cobertura = []
        for stock, fila in zip(tabla["Stock"].values, acumulado):
            if stock <= 0:
                cobertura.append("Sin stock")
            else:
                dias = int((fila > stock).argmax()) if (fila > stock).any() else None
                cobertura.append(f"{dias} días" if dias is not None else "Más de 14 días")
        tabla["Alcanza para"] = cobertura
        tabla = tabla[tabla["Próx. 7 días"] > 0.05]

        st.subheader("Próximos 7 días por artículo")
        c1, c2 = st.columns([2, 1])
        buscar = c1.text_input("Buscar artículo", key="pron_buscar")
        clases_sel = c2.multiselect("Clase", ["A", "B", "C"], default=["A", "B", "C"], key="pron_clases")
        vista = tabla[tabla["Clase"].isin(clases_sel)]
        if buscar:
            vista = vista[vista["Artículo"].str.contains(buscar, case=False, na=False)]
        orden_cob = vista["Alcanza para"].map(
            lambda t: -1 if t == "Sin stock" else (99 if t.startswith("Más") else int(t.split()[0])))
        vista = vista.assign(_o=orden_cob).sort_values(["_o", "Próx. 7 días"], ascending=[True, False])
        st.dataframe(
            vista.drop(columns=["articulo_id", "_o"]).style.format({"Próx. 7 días": "{:.1f}", "Stock": "{:g}"}),
            hide_index=True, width="stretch", height=360,
        )
        st.caption("\"Alcanza para\": días hasta que la venta pronosticada supera el stock actual. "
                   "Es una estimación: la lista de compras del viaje se arma en Reposición.")

        st.subheader("Detalle de un artículo")
        opciones = vista["articulo_id"].tolist() or tabla["articulo_id"].tolist()
        aid = st.selectbox("Artículo", opciones, format_func=lambda i: meta.at[i, "nombre"], key="pron_art")
        if aid is not None:
            hist = Y[aid].iloc[-60:] if aid in Y.columns else pd.Series(dtype=float)
            # no dibujar como "0 ventas" los días anteriores al alta del artículo
            alta_art = pd.to_datetime(meta.at[aid, "alta"]) if pd.notna(meta.at[aid, "alta"]) else None
            primera = hist[hist > 0].index.min() if (hist > 0).any() else None
            desde = min(d for d in (alta_art, primera) if d is not None) if (alta_art or primera) else None
            if desde is not None:
                hist = hist[hist.index >= desde]
            fut = df[df["articulo_id"] == aid]
            pasado = seg[seg["articulo_id"] == aid] if not seg.empty else seg
            if desde is not None and not pasado.empty:
                pasado = pasado[pd.to_datetime(pasado["fecha"]) >= desde]
            # Nombres pensados para el almacenero (tanda 19): qué es cada línea, sin jerga.
            SERIES = ["Lo que se vendió", "Lo que la app esperaba vender ese día", "Lo que se espera vender"]
            serie = pd.concat([
                pd.DataFrame({"Fecha": pd.to_datetime(hist.index), "Unidades": hist.values, "Serie": SERIES[0]}),
                pd.DataFrame({"Fecha": pd.to_datetime(pasado["fecha"]) if not pasado.empty else pd.Series(dtype="datetime64[ns]"),
                              "Unidades": pasado["pronostico"].values if not pasado.empty else [],
                              "Serie": SERIES[1]}),
                pd.DataFrame({"Fecha": pd.to_datetime(fut["fecha_objetivo"]), "Unidades": fut["demanda"].values,
                              "Serie": SERIES[2]}),
            ])
            oscuro = getattr(getattr(st.context, "theme", None), "type", None) == "dark"
            colores = ["#3987e5", "#199e70", "#d95926"] if oscuro else ["#2a78d6", "#1baf7a", "#eb6834"]
            grafico = alt.Chart(serie).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=36)).encode(
                x=alt.X("Fecha:T", title=None, axis=alt.Axis(format="%d/%m", grid=False)),
                y=alt.Y("Unidades:Q", title="Unidades por día", axis=alt.Axis(gridOpacity=0.3)),
                color=alt.Color("Serie:N", scale=alt.Scale(domain=SERIES, range=colores),
                                legend=alt.Legend(title=None, orient="top", direction="horizontal",
                                                  labelLimit=400, symbolStrokeWidth=3)),
                strokeDash=alt.StrokeDash("Serie:N", scale=alt.Scale(domain=SERIES,
                                                                     range=[[1, 0], [2, 3], [6, 4]]), legend=None),
                tooltip=[alt.Tooltip("Fecha:T", title="Día", format="%d/%m/%Y"), alt.Tooltip("Serie:N", title=" "),
                         alt.Tooltip("Unidades:Q", format=".1f")],
            ).properties(height=280, title=alt.TitleParams(
                "¿Cuánto se vendió y cuánto se espera vender? (unidades por día)", anchor="start", fontSize=14,
                fontWeight="normal"))
            # Se consulta directo a la base (no a la caché de demanda_historica,
            # que puede venir de una versión anterior sin esta columna).
            es_diaria_graf = aid in db.ids_compra_diaria()
            if es_diaria_graf:
                # Compra diaria (tanda 16): cada barra es lo DISPONIBLE ese día (lo que
                # quedó del anterior + lo comprado), partido en vendido, tirado/regalado
                # (solo lo registrado), faltante en un conteo y lo que quedó para el día
                # siguiente. Antes la barra era solo la compra del día y, si no había
                # sobrante cargado, pintaba "comprado - vendido" como sobrante aunque
                # siguiera en la estantería. Línea: pronóstico de venta.
                desde_d = max(pd.Timestamp(hoy_argentina()) - pd.Timedelta(days=21), desde or pd.Timestamp.min)
                dia_g = db.stock_diario_articulo(aid, desde_d.date())
                dia_g["Fecha"] = pd.to_datetime(dia_g["fecha"])
                dia_g["faltante"] = (-dia_g["ajuste"]).clip(lower=0)
                # lo que pasó a su producto vinculado en Fraccionar (ej.: pan -> pan de ayer)
                dia_g["paso"] = (-dia_g["fraccionado"]).clip(lower=0)
                dia_g["quedo"] = dia_g["fin"].clip(lower=0)
                derivado_g = db.obtener_derivado(int(aid))
                nombre_der_g = derivado_g["nombre"] if derivado_g else "otro artículo"
                dia_g["es_hoy"] = dia_g["Fecha"].dt.normalize() == pd.Timestamp(hoy_argentina())
                dia_g["nota"] = np.where(
                    dia_g["fin"] < -1e-9, "Stock negativo: se vendió más de lo cargado (falta una compra o un conteo)",
                    np.where(dia_g["es_hoy"], "Hoy: lo de \"quedó\" es lo que hay ahora", ""))
                C_VEND, C_TIR, C_PASO, C_FALT, C_QUEDO = (
                    "Vendido", "Tirado / regalado", "Pasó a " + nombre_der_g,
                    "Faltó en el conteo", "Quedó para el día siguiente")
                barras = pd.concat([
                    dia_g.assign(Concepto=C_VEND, Unidades=dia_g["vendido"], orden=0),
                    dia_g.assign(Concepto=C_TIR, Unidades=dia_g["tirado"], orden=1),
                    dia_g.assign(Concepto=C_PASO, Unidades=dia_g["paso"], orden=2),
                    dia_g.assign(Concepto=C_FALT, Unidades=dia_g["faltante"], orden=3),
                    dia_g.assign(Concepto=C_QUEDO, Unidades=dia_g["quedo"], orden=4),
                ], ignore_index=True)
                barras = barras[barras["Unidades"] > 1e-9]
                linea = pd.concat([
                    pd.DataFrame({"Fecha": pd.to_datetime(pasado["fecha"]) if not pasado.empty else pd.Series(dtype="datetime64[ns]"),
                                  "Unidades": pasado["pronostico"].values if not pasado.empty else []}),
                    pd.DataFrame({"Fecha": pd.to_datetime(fut["fecha_objetivo"]).iloc[:7],
                                  "Unidades": fut["demanda"].values[:7]}),
                ])
                linea = linea[linea["Fecha"] >= desde_d].assign(Concepto="Pronóstico de venta")
                # Eje por día (categorías) compartido: así cada punto queda justo sobre su barra.
                dias_eje = sorted(set(barras["Fecha"].dt.normalize()) | set(linea["Fecha"].dt.normalize()))
                orden_dias = [d.strftime("%d/%m") for d in dias_eje]
                barras["Día"] = barras["Fecha"].dt.strftime("%d/%m")
                linea["Día"] = linea["Fecha"].dt.strftime("%d/%m")
                eje_x = alt.X("Día:O", sort=orden_dias, title=None,
                              axis=alt.Axis(labelAngle=0, labelOverlap="greedy"))
                dominio = [C_VEND, C_TIR, C_PASO, C_FALT, C_QUEDO, "Pronóstico de venta"]
                cols_b = (["#3987e5", "#d95926", "#c99a2e", "#a35bd6", "#5b6370", "#199e70"] if oscuro
                          else ["#2a78d6", "#eb6834", "#d4a72c", "#9149c9", "#b4bac4", "#1baf7a"])
                escala = alt.Scale(domain=dominio, range=cols_b)
                capa_barras = alt.Chart(barras).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
                    x=eje_x,
                    y=alt.Y("sum(Unidades):Q", title="Unidades por día", stack="zero", axis=alt.Axis(gridOpacity=0.3)),
                    color=alt.Color("Concepto:N", scale=escala,
                                    legend=alt.Legend(title=None, orient="top", labelLimit=320)),
                    order=alt.Order("orden:Q"),
                    tooltip=[alt.Tooltip("Fecha:T", title="Día", format="%d/%m/%Y"),
                             alt.Tooltip("Concepto:N", title="Parte"), alt.Tooltip("Unidades:Q", format="g"),
                             alt.Tooltip("inicio:Q", title="Quedó del día anterior", format="g"),
                             alt.Tooltip("comprado:Q", title="Comprado", format="g"),
                             alt.Tooltip("ajuste:Q", title="Ajuste por conteo", format="+g"),
                             alt.Tooltip("vendido:Q", title="Vendido", format="g"),
                             alt.Tooltip("tirado:Q", title="Tirado / regalado", format="g"),
                             alt.Tooltip("paso:Q", title="Pasó a " + nombre_der_g, format="g"),
                             alt.Tooltip("fin:Q", title="Quedó al final", format="g"),
                             alt.Tooltip("nota:N", title=" ")],
                )
                capa_linea = alt.Chart(linea).mark_line(strokeWidth=2, strokeDash=[4, 3],
                                                        point=alt.OverlayMarkDef(size=40, filled=True)).encode(
                    x=eje_x, y="Unidades:Q",
                    color=alt.Color("Concepto:N", scale=escala),
                    tooltip=[alt.Tooltip("Fecha:T", format="%d/%m/%Y"), alt.Tooltip("Unidades:Q", title="Pronóstico", format=".1f")],
                )
                grafico = alt.layer(capa_barras, capa_linea).properties(height=300)
                st.altair_chart(grafico, width="stretch")
                st.caption("Cada barra es lo que hubo ese día: lo que quedó del anterior más lo comprado. Azul: "
                           "vendido. Naranja: tirado o regalado (solo lo cargado en Sobrante del día). Amarillo: lo "
                           f"que pasó a {nombre_der_g} en ⚖️ Fraccionar. Violeta: lo que faltó en un conteo. Gris: lo "
                           "que quedó para el día siguiente; en el día de hoy, lo que hay ahora. La línea verde es lo "
                           "que se pronosticaba vender: hacia atrás, lo que se pronosticó ese mismo día; hacia "
                           "adelante, los próximos 7 días.")
                hoy_g = dia_g[dia_g["es_hoy"]]
                # pan de ayer que todavía no se pasó a "de ayer" ni se cargó como tirado
                pendiente_g = (float(hoy_g["inicio"].iloc[0] - hoy_g["paso"].iloc[0] - hoy_g["tirado"].iloc[0])
                               if not hoy_g.empty else 0.0)
                if pendiente_g > 1e-9:
                    destino_txt = (f"pasalos a {nombre_der_g} en ⚖️ Fraccionar" if derivado_g
                                   else "pasalos a su artículo \"de ayer\" en ⚖️ Fraccionar")
                    st.caption(f"Hoy arrancó con {pendiente_g:g} del día anterior sin pasar. Si se siguen vendiendo, "
                               f"{destino_txt}; si se tiraron, cargalos en 💵 Registrar Venta → 🗑️ Sobrante del día "
                               "con la fecha de ayer.")
            else:
                # Tanda 19: panel de stock debajo, con el mismo eje de fechas (no un segundo
                # eje: el stock suele ser mucho mayor que la venta diaria). Hacia atrás, el
                # stock al final de cada día (reconstruido desde el stock actual con compras,
                # ventas, fraccionamientos y ajustes: db.stock_diario_articulo); hacia
                # adelante, el stock proyectado si no se compra (stock de hoy - venta esperada).
                stock_hoy_g = float(db.stock_actual_por_id().get(int(aid), 0.0))
                inicio_st = pd.Timestamp(serie["Fecha"].min()) if not serie.empty else pd.Timestamp(hoy_argentina())
                hist_st = db.stock_diario_articulo(aid, inicio_st.date())
                hoy_ts = pd.Timestamp(hoy_argentina())
                S_HIST, S_PROY, S_CERO = ("Stock al cierre de cada día", "Stock que quedaría si no se compra",
                                          "Terminó el día sin stock")
                partes_st = []
                if not hist_st.empty:
                    partes_st.append(pd.DataFrame({"Fecha": pd.to_datetime(hist_st["fecha"]),
                                                   "Stock": hist_st["fin"].astype(float), "Serie": S_HIST}))
                fut_desp = fut[pd.to_datetime(fut["fecha_objetivo"]) > hoy_ts].sort_values("fecha_objetivo")
                if not fut_desp.empty:
                    proy = (stock_hoy_g - fut_desp["demanda"].astype(float).cumsum()).clip(lower=0)
                    partes_st.append(pd.DataFrame({
                        "Fecha": [hoy_ts] + list(pd.to_datetime(fut_desp["fecha_objetivo"])),
                        "Stock": [max(stock_hoy_g, 0.0)] + list(proy.values), "Serie": S_PROY}))
                stock_g = pd.concat(partes_st, ignore_index=True) if partes_st else pd.DataFrame(
                    columns=["Fecha", "Stock", "Serie"])
                if stock_g.empty:
                    st.altair_chart(grafico, width="stretch")
                else:
                    gris = "#c3c2b7" if oscuro else "#52514e"
                    rojo = "#e66767" if oscuro else "#e34948"
                    dom_st, col_st = [S_HIST, S_PROY, S_CERO], [gris, gris, rojo]
                    eje_x_st = alt.X("Fecha:T", title=None, axis=alt.Axis(format="%d/%m", grid=False))
                    leyenda_st = alt.Legend(title=None, orient="top", direction="horizontal", labelLimit=400,
                                            symbolStrokeWidth=3)
                    lineas_st = alt.Chart(stock_g).mark_line(strokeWidth=2, interpolate="step-after").encode(
                        x=eje_x_st,
                        y=alt.Y("Stock:Q", title="Unidades", axis=alt.Axis(gridOpacity=0.3)),
                        color=alt.Color("Serie:N", scale=alt.Scale(domain=dom_st, range=col_st), legend=leyenda_st),
                        strokeDash=alt.StrokeDash("Serie:N", scale=alt.Scale(domain=dom_st,
                                                                             range=[[1, 0], [6, 4], [1, 0]]),
                                                  legend=None),
                        tooltip=[alt.Tooltip("Fecha:T", title="Día", format="%d/%m/%Y"),
                                 alt.Tooltip("Serie:N", title=" "), alt.Tooltip("Stock:Q", format="g")],
                    )
                    sin_stock = stock_g[(stock_g["Serie"] == S_HIST) & (stock_g["Stock"] <= 1e-9)].assign(Serie=S_CERO)
                    puntos_cero = alt.Chart(sin_stock).mark_point(filled=True, size=55, opacity=1).encode(
                        x=eje_x_st, y="Stock:Q",
                        color=alt.Color("Serie:N", scale=alt.Scale(domain=dom_st, range=col_st), legend=None),
                        tooltip=[alt.Tooltip("Fecha:T", title="Día", format="%d/%m/%Y"),
                                 alt.Tooltip("Stock:Q", format="g", title="Cerró sin stock")],
                    )
                    regla_cero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=rojo, opacity=0.35).encode(y="y:Q")
                    panel_st = alt.layer(lineas_st, regla_cero, puntos_cero).properties(
                        height=170, title=alt.TitleParams("¿Cuánto stock hubo y cuánto va a quedar?", anchor="start",
                                                          fontSize=14, fontWeight="normal"))
                    # línea "hoy": separa lo que ya pasó (azul, verde, gris lleno) de lo que viene
                    # (naranja, gris punteado)
                    hoy_df = pd.DataFrame({"Fecha": [hoy_ts], "t": ["hoy"]})
                    regla_hoy = alt.Chart(hoy_df).mark_rule(strokeDash=[3, 3], color=gris, opacity=0.8).encode(
                        x="Fecha:T")
                    texto_hoy = alt.Chart(hoy_df).mark_text(align="left", dx=4, color=gris, fontSize=12).encode(
                        x="Fecha:T", y=alt.value(6), text="t:N")
                    st.altair_chart(alt.vconcat(alt.layer(grafico, regla_hoy, texto_hoy),
                                                alt.layer(panel_st, regla_hoy), spacing=28).resolve_scale(
                        x="shared", color="independent", strokeDash="independent"), width="stretch")
                st.markdown(
                    "<small>**Cómo leerlo**<br>"
                    "🔵 **Azul:** lo que se vendió cada día.<br>"
                    "🟢 **Verde:** lo que la app había calculado para ese día, con las ventas hasta el día anterior. "
                    "No es lo que se espera vender: sirve para comparar con la azul y ver si la app acierta.<br>"
                    "🟠 **Naranja:** lo que se espera vender en los próximos días (a la derecha de \"hoy\"). Con esto "
                    "se arma la lista de compras.<br>"
                    "⬜ **Gris (abajo):** el stock al cierre de cada día; punteado, lo que quedaría si no se compra nada.<br>"
                    "🔴 **Rojo (abajo, \"Terminó el día sin stock\"):** ese día cerró sin stock (o en negativo). Si ese día no se vendió, fue porque no "
                    "había, y la app no lo toma como que nadie lo quiso.</small>", unsafe_allow_html=True)
                if not stock_g.empty:
                    agota = fut_desp.assign(_s=(stock_hoy_g - fut_desp["demanda"].astype(float).cumsum()))
                    agota = agota[agota["_s"] <= 0]
                    dias_cero = int((hist_st["fin"].astype(float) <= 1e-9).sum()) if not hist_st.empty else 0
                    partes_txt = []
                    if dias_cero:
                        partes_txt.append(f"terminó {dias_cero} día(s) sin stock en este período")
                    if not hist_st.empty and float(hist_st["fin"].min()) < -1e-9:
                        partes_txt.append("el stock quedó negativo (se vendió más de lo cargado: falta cargar una "
                                          "compra o hacer un conteo), así que esta curva no es confiable hasta corregirlo")
                    if stock_hoy_g <= 0:
                        partes_txt.append("hoy no hay stock")
                    elif not agota.empty:
                        partes_txt.append(f"sin comprar, se agotaría hacia el "
                                          f"{pd.Timestamp(agota['fecha_objetivo'].iloc[0]):%d/%m}")
                    if partes_txt:
                        st.caption("📦 " + "; ".join(partes_txt).capitalize() + ".")
            fila = fut.iloc[0] if not fut.empty else None
            venta7 = float(fut["demanda"].iloc[:7].sum())
            por_dia = venta7 / 7 if venta7 else 0.0
            stock_a = float(meta.at[aid, "stock_actual"])
            clase_a = fila["clase"] if fila is not None else "-"
            c1, c2, c3 = st.columns(3)
            c1.metric("Se espera vender en los próximos 7 días", f"{venta7:.0f} u.",
                      help="Suma de lo pronosticado para los próximos 7 días (la línea punteada).")
            c1.caption(f"≈ {por_dia:.1f} por día".replace(".", ","))
            c2.metric("Clase ABC", clase_a,
                      help="A: los artículos que juntos hacen el 80 % de lo que se factura (los más importantes). "
                           "B: el 15 % siguiente. C: el resto.")
            c2.caption({"A": "De los que más facturan", "B": "Importancia media",
                        "C": "De los que menos facturan"}.get(clase_a, ""))
            c3.metric("Stock actual", f"{stock_a:g} u.")
            if stock_a <= 0:
                c3.caption("Sin stock: si no se repone, esas ventas se pierden")
            elif por_dia > 0:
                c3.caption(f"Alcanza para ≈ {stock_a / por_dia:.0f} días")
            if fila is not None:
                st.caption(f"Modelo: {nombre_modelo.get(str(fila['modelo']).split('+')[0], fila['modelo'])}"
                           + (" (artículo nuevo: se combina un poco con el promedio de su categoría hasta que tenga 4 semanas)"
                              if "+categoria" in str(fila["modelo"]) else ""))

            if not pasado.empty:
                st.markdown("**Día por día: ¿se cumplió?**")
                tabla_dia = pasado[["fecha", "pronostico", "real", "fuente"]].rename(columns={
                    "fecha": "Fecha", "pronostico": "Pronosticado", "real": "Vendido", "fuente": "Fuente"})
                tabla_dia["Diferencia"] = tabla_dia["Vendido"] - tabla_dia["Pronosticado"]
                es_diaria = es_diaria_graf
                if es_diaria:
                    # Tanda 16: con la reconstrucción del stock día por día. Antes se suponía
                    # que cada día arrancaba sin pan (comprado - vendido = sobrante), y no es
                    # así: lo que no se tira pasa al día siguiente.
                    mov = db.stock_diario_articulo(aid, tabla_dia["Fecha"].min())
                    mov["fecha"] = pd.to_datetime(mov["fecha"]).dt.date
                    mov["paso"] = (-mov["fraccionado"]).where(mov["fraccionado"] != 0, 0.0)  # sin "-0"
                    tabla_dia = tabla_dia.merge(mov[["fecha", "inicio", "comprado", "ajuste", "tirado", "paso", "fin"]],
                                                left_on="Fecha", right_on="fecha", how="left").drop(columns=["fecha"])
                    tabla_dia = tabla_dia.rename(columns={"inicio": "Quedó del anterior", "comprado": "Comprado",
                                                          "ajuste": "Ajuste por conteo",
                                                          "tirado": "Tirado / regalado", "paso": "Fraccionado",
                                                          "fin": "Quedó al final"})
                    tabla_dia["¿Se agotó?"] = np.where(
                        tabla_dia["Quedó al final"] < -1e-9, "Stock negativo",
                        np.where(tabla_dia["Quedó al final"] <= 1e-9, "Sí", "No"))
                columnas = ["Fecha", "Pronosticado", "Vendido", "Diferencia", "Fuente"] + (
                    ["Quedó del anterior", "Comprado", "Ajuste por conteo", "Tirado / regalado", "Fraccionado",
                     "Quedó al final", "¿Se agotó?"]
                    if es_diaria else [])
                st.dataframe(
                    tabla_dia.sort_values("Fecha", ascending=False)[columnas].style.format(
                        {"Pronosticado": "{:.1f}", "Vendido": "{:g}", "Diferencia": "{:+.1f}",
                         "Quedó del anterior": "{:g}", "Comprado": "{:g}",
                         "Ajuste por conteo": lambda v: f"{v:+g}" if pd.notna(v) and v != 0 else "0",
                         "Tirado / regalado": "{:g}", "Fraccionado": "{:g}", "Quedó al final": "{:g}"}, na_rep="-"),
                    hide_index=True, width="stretch",
                )
                if es_diaria:
                    st.caption("Cada fila cierra: quedó del anterior + comprado + ajuste − vendido − tirado − fraccionado "
                               "= quedó al final. \"Fraccionado\" es lo que pasó a su producto vinculado en ⚖️ Fraccionar "
                               "(ej.: pan de ayer). \"Sí\" en ¿Se agotó? quiere decir que la venta real pudo ser mayor; "
                               "\"Stock negativo\", que se vendió más de lo cargado (falta una compra o un conteo). Lo que "
                               "se tira, cargalo en Sobrante del día con la fecha en que se compró.")

# ============================================================
# HISTORIAL DE CAMBIOS
# ============================================================
elif pagina == "🏫 Desafío Recreo":
    st.title("🏫 Desafío Recreo")
    st.caption("Cada día, unos pocos artículos para mirar en la góndola: la fecha de vencimiento más cercana que "
               "veas. Suma estrellitas, completa el boletín de la semana y desbloquea figuritas. Con esas fechas la "
               "app aprende cuánto duran de verdad los productos.")
    if not desafio.hay_desafio():
        st.info("Falta correr `python migracion_tanda21.py` (con la tienda cerrada, después del respaldo).")
        st.stop()

    hoy_d = hoy_argentina()
    pid = int(PERSONA["id"])
    juega_d = desafio.es_participante(pid)
    if juega_d:
        for sem in desafio.otorgar_boletines(pid, hoy_d):
            st.balloons()
            avisar("success", f"🎓 ¡Boletín de la semana del {sem:%d/%m}! +{desafio.PUNTOS_BOLETIN} estrellitas.")
        for nom, valor, est in desafio.evaluar_rescates(pid, hoy_d):
            avisar("success", f"🛟 Rescate: {nom} se vendió antes de vencer ({pesos_venta(valor)} salvados"
                              + (f", +{est} estrellitas" if est else "") + ").")
        for txt in desafio.otorgar_excursiones(pid, hoy_d) + desafio.evaluar_cierres(pid, hoy_d):
            avisar("success", f"🚌 {txt}" if "lista" in txt or "viaje" in txt else f"🧮 {txt}")
    mostrar_avisos()
    premios_d = desafio.premios()

    tabs_d = st.tabs(["📝 Tarea del día", "🖼️ Álbum de figuritas"] + (["🔒 Configuración"] if ES_DUENO else []))

    with tabs_d[0]:
        if not juega_d:
            st.info("No estás anotado en el desafío: podés controlar fechas igual (se guardan y sirven), pero no "
                    "suman estrellitas. El dueño elige quién juega en 🔒 Configuración.")
        est_d = desafio.estado(pid, hoy_d)

        # Boletín de la semana pasada (el hito)
        lunes_pasado = desafio.lunes(hoy_d) - timedelta(days=7)
        if desafio._ref_dia(lunes_pasado) in est_d["boletines"]:
            rs = desafio.resumen_semana(pid, lunes_pasado)
            with st.container(border=True):
                b1, b2 = st.columns([1, 5], vertical_alignment="center")
                b1.markdown(figuritas.svg("guardapolvo", 88, titulo="Guardapolvo"), unsafe_allow_html=True)
                b2.markdown(f"#### 🎓 Boletín de la semana del {lunes_pasado:%d/%m} al "
                            f"{lunes_pasado + timedelta(days=6):%d/%m}: ¡7 de 7!")
                b2.markdown(f"{rs['controles']} fechas anotadas · {rs['heladera']} de la heladera · "
                            f"{rs['hallazgos']} hallazgos · {md_pesos(rs['plata_salvada'])} salvados · "
                            f"**{rs['estrellitas']} estrellitas**")
                lb = desafio.libreta(pid, lunes_pasado)
                b2.markdown(" · ".join(f"{nom}: **{lb[c]['nota'] if lb[c]['nota'] is not None else '—'}**"
                                       for c, nom in desafio.MATERIAS))
                if premios_d.get("boletin"):
                    b2.success(f"🎁 Premio: {premios_d['boletin']}")

        niv = est_d["nivel"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("⭐ Estrellitas", f"{est_d['estrellitas']:,}".replace(",", "."), help="Nivel: " + niv["nombre"])
        m2.metric("🔥 Racha", f"{est_d['racha']['dias']} días",
                  help=f"Desde {desafio.DIAS_RACHA} días seguidos, las estrellitas del día valen ×{desafio.MULT_RACHA:g}.")
        comod = desafio.parametros()["comodines"]
        m3.metric("📅 Esta semana", f"{est_d['dias_semana']} de 7",
                  help=f"Comodines usados: {est_d['racha']['comodines_usados']} de {comod} (un día salteado no corta la racha).")
        m4.metric("🛟 Plata salvada", pesos_venta(est_d["plata_salvada"]),
                  help="Costo de lo que estaba por vencer y se vendió a tiempo después de verlo.")
        if niv["meta"]:
            avance = (est_d["estrellitas"] - niv["desde"]) / max(niv["meta"] - niv["desde"], 1)
            st.progress(min(max(avance, 0.0), 1.0),
                        text=f"**{niv['nombre']}** · faltan {niv['meta'] - est_d['estrellitas']} para {niv['siguiente']}")
        else:
            st.progress(1.0, text=f"**{niv['nombre']}** · ¡nivel máximo!")
        st.markdown(" ".join(
            ("🟩" if s["cumplido"] else ("🟨" if s["hoy"] else ("⬜" if s["futuro"] else "🟥"))) + f" {s['letra']}"
            for s in est_d["semana"]))

        col_tarea, col_lado = st.columns([3, 2], gap="large")
        with col_tarea:
            st.subheader(f"📝 Tarea de hoy · {hoy_d:%d/%m}")
            tarea_todo = desafio.tarea_del_dia(pid, hoy_d)
            tarea = tarea_todo[tarea_todo["tipo"] == "gondola"]
            dato = tarea_todo[tarea_todo["tipo"] == "dato"]
            if tarea.empty:
                st.info("Hoy no hay artículos para controlar (no hay stock de artículos con vencimiento).")
            hechos = int(tarea["control_id"].notna().sum()) if not tarea.empty else 0
            if not tarea.empty:
                st.caption(f"{hechos} de {len(tarea)} hechos. Mirá la góndola (y el depósito) y anotá la fecha "
                           "**más cercana** que veas. +10 estrellitas cada uno, +15 los perecederos, +5 «No hay» o "
                           "«No vence», +5 si además contás, +20 al completar (con el dato del día).")

            # Corrección de stock después de un conteo que no coincidió
            corr = st.session_state.get("desafio_corregir")
            if corr:
                with st.container(border=True):
                    st.markdown(f"**🔢 {corr['nombre']}: contaste {corr['conteo']:g} y la app tenía {corr['stock']:g}.**")
                    st.caption("Si estás seguro del conteo (góndola + depósito), corregí el stock de la app. Si no, "
                               "dejalo: el conteo queda guardado igual.")
                    k1, k2 = st.columns(2)
                    if k1.button(f"✅ Corregir el stock a lo contado", type="primary", key="desafio_corr_si",
                                 width="stretch"):
                        try:
                            nuevo_st = desafio.corregir_por_conteo(corr["control_id"])
                            avisar("success", f"Stock de {corr['nombre']} corregido"
                                              + (f" a {nuevo_st:g}." if nuevo_st is not None else "."))
                        except ValueError as e:
                            avisar("warning", str(e))
                        st.session_state.pop("desafio_corregir", None)
                        st.rerun()
                    if k2.button("Lo dejo así", key="desafio_corr_no", width="stretch"):
                        st.session_state.pop("desafio_corregir", None)
                        st.rerun()

            MOTIVOS = {"perecedero": "🥛 perecedero", "stock": "📦 mucho stock", "nunca": "🆕 nunca controlado",
                       "repaso": "🔁 repaso"}
            for r in tarea.itertuples():
                aid = int(r.articulo_id)
                cuenta_c = bool(r.pide_conteo)
                with st.container(border=True):
                    if pd.notna(r.control_id):
                        if r.vence is not None and pd.notna(r.vence):
                            txt = f"vence {pd.Timestamp(r.vence):%d/%m/%Y}"
                        elif r.hay_stock is False or r.hay_stock == False:  # noqa: E712
                            txt = "no hay en la góndola"
                        else:
                            txt = "no vence"
                        if pd.notna(r.conteo):
                            txt += f" · contaste {r.conteo:g}" + (
                                " ✔️" if r.conteo_coincide else
                                (" (stock corregido)" if r.corregido else f" (la app tenía {r.stock_al_controlar:g})"))
                        st.markdown(f"✅ **{r.nombre}** · {txt}"
                                    + (f" · ⭐ +{int(r.estrellitas)}" if r.estrellitas else ""))
                        continue
                    # conteo a ciegas: no se muestra el stock de la app
                    st.markdown(f"**{r.nombre}** · {r.categoria or '-'} · "
                                + ("🔢 **contar**" if cuenta_c else f"stock {r.stock:g}")
                                + f" · _{MOTIVOS.get(r.motivo, r.motivo)}_")
                    with st.form(f"ctrl_{hoy_d:%Y%m%d}_{aid}", enter_to_submit=False, border=False):
                        if cuenta_c:
                            f1, f2, f3 = st.columns([2, 1, 1])
                        else:
                            f1, f2 = st.columns([2, 1])
                        vence_in = f1.date_input("Fecha más cercana", value=None, format="DD/MM/YYYY",
                                                 key=f"ctrl_v_{aid}")
                        cant_in = f2.number_input("Cuántas vencen esa fecha (opcional)", min_value=0.0, step=1.0,
                                                  value=None, format="%g", key=f"ctrl_c_{aid}")
                        conteo_in = (f3.number_input("¿Cuántos hay en total?", min_value=0.0, step=1.0, value=None,
                                                     format="%g", key=f"ctrl_n_{aid}",
                                                     help="Góndola + depósito. Contá sin mirar la app.")
                                     if cuenta_c else None)
                        g1, g2, g3 = st.columns(3)
                        guardar_c = g1.form_submit_button("💾 Guardar", type="primary", width="stretch")
                        no_hay_c = g2.form_submit_button("🚫 No hay", width="stretch")
                        no_vence_c = g3.form_submit_button("♾️ No vence", width="stretch",
                                                           help="El envase no trae vencimiento: sale de la tarea.")
                    if guardar_c or no_hay_c or no_vence_c:
                        try:
                            if guardar_c and vence_in is None:
                                raise ValueError("Elegí la fecha que viste (o tocá «No hay»).")
                            res = desafio.registrar_control(
                                pid, aid, hoy_d, hay_stock=not no_hay_c,
                                vence=vence_in if guardar_c else None,
                                cantidad=cant_in if guardar_c else None, no_vence=bool(no_vence_c),
                                conteo=conteo_in)
                            msg = f"{r.nombre}: guardado" + (f" · +{res['estrellitas']} ⭐" if res["estrellitas"] else "")
                            avisar("success", msg + ".")
                            for m in res["mensajes"]:
                                if m.startswith("Contaste"):
                                    continue
                                avisar("warning" if res["hallazgo"] else "info", m)
                            if res["conteo_coincide"] is False and not no_hay_c:
                                st.session_state["desafio_corregir"] = {
                                    "control_id": res["control_id"], "nombre": r.nombre,
                                    "conteo": res["conteo"], "stock": res["stock_app"]}
                            if res["bonus"]:
                                avisar("success", f"🎉 ¡Tarea del día completa! +{res['bonus']} estrellitas.")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

            # Orden: el dato del día
            for r in dato.itertuples():
                aid = int(r.articulo_id)
                titulo_d, texto_d = desafio.TEXTO_CHEQUEO.get(r.motivo, (r.motivo, ""))
                st.markdown("##### 🔍 Arreglá un dato")
                with st.container(border=True):
                    if pd.notna(r.dato_id):
                        st.markdown(f"✅ **{r.nombre}** · {titulo_d} · {r.dato_detalle.split(':')[0]}"
                                    + (f" · ⭐ +{int(r.dato_estrellitas)}" if r.dato_estrellitas else ""))
                        continue
                    st.markdown(f"**{r.nombre}** · {titulo_d}")
                    st.caption(texto_d + f" +{desafio.PUNTOS_DATO} ⭐ si la app ve que quedó arreglado.")
                    try:
                        if r.motivo == "stock_negativo":
                            with st.form(f"dato_{hoy_d:%Y%m%d}_{aid}", enter_to_submit=False, border=False):
                                n_dato = st.number_input("¿Cuántos hay? (góndola + depósito)", min_value=0.0, step=1.0,
                                                         value=None, format="%g", key=f"dato_n_{aid}")
                                d1, d2 = st.columns(2)
                                listo_d = d1.form_submit_button("✅ Corregir el stock y listo", type="primary",
                                                                width="stretch")
                                nopude_d = d2.form_submit_button("Hoy no pude", width="stretch")
                            if listo_d:
                                if n_dato is None:
                                    raise ValueError("Anotá cuántos hay.")
                                db.corregir_stock(aid, n_dato)
                        elif r.motivo == "sin_costo":
                            with st.form(f"dato_{hoy_d:%Y%m%d}_{aid}", enter_to_submit=False, border=False):
                                costo_dato = st.number_input("Costo sin IVA, como en la factura ($)", min_value=0.0,
                                                             step=10.0, value=None, format="%.2f", key=f"dato_c_{aid}")
                                d1, d2 = st.columns(2)
                                listo_d = d1.form_submit_button("✅ Guardar el costo y listo", type="primary",
                                                                width="stretch")
                                nopude_d = d2.form_submit_button("Hoy no pude", width="stretch")
                            if listo_d:
                                if not costo_dato:
                                    raise ValueError("Anotá el costo.")
                                db.actualizar_precio_articulo(aid, nuevo_costo=round(float(costo_dato), 2))
                        else:
                            d1, d2 = st.columns(2)
                            listo_d = d1.button("👀 Lo revisé", type="primary", key=f"dato_rev_{aid}", width="stretch",
                                                help="Las ventas de ese artículo están bien cargadas (o ya las corregiste).")
                            nopude_d = d2.button("Hoy no pude", key=f"dato_no_{aid}", width="stretch")
                        if listo_d or nopude_d:
                            accion = ("no_pude" if nopude_d else
                                      ("revisado" if r.motivo == "venta_atipica" else "listo"))
                            res = desafio.registrar_dato(pid, aid, hoy_d, accion)
                            avisar("success" if res["estrellitas"] else "info",
                                   f"{r.nombre}: " + ("¡arreglado!" if accion == "listo" else
                                                      "revisado." if accion == "revisado" else "queda para otro día.")
                                   + (f" +{res['estrellitas']} ⭐" if res["estrellitas"] else ""))
                            if res["bonus"]:
                                avisar("success", f"🎉 ¡Tarea del día completa! +{res['bonus']} estrellitas.")
                            st.rerun()
                    except ValueError as e:
                        st.error(str(e))

        with col_lado:
            with st.container(border=True):
                st.markdown("**📒 Libreta de esta semana**")
                lb_hoy = est_d["libreta"]
                for c_m, nom_m in desafio.MATERIAS:
                    m_ = lb_hoy[c_m]
                    st.markdown(f"{nom_m}: **{m_['nota'] if m_['nota'] is not None else '—'}** · {m_['texto']}")
            with st.container(border=True):
                st.markdown("**🚌 Excursión al mayorista**")
                st.caption(f"En 🛒 Lista de compras: +{desafio.PUNTOS_LISTA} ⭐ por guardar la lista antes de ir, "
                           f"+{desafio.PUNTOS_VIAJE} por anotar el viaje (también los de urgencia) y "
                           f"+{desafio.PUNTOS_LISTA_CUMPLIDA} si después se compró al menos el "
                           f"{desafio.PCT_LISTA_CUMPLIDA:.0%} de la lista.")
                st.progress(min(est_d["viajes_lista"] / desafio.VIAJES_EXCURSION, 1.0),
                            text=f"{est_d['viajes_lista']} de {desafio.VIAJES_EXCURSION} viajes con la lista "
                                 "para el Micro de la excursión")
            with st.container(border=True):
                st.markdown("**🧮 Cierre del día**")
                cierre_hoy = db.cierre_guardado(hoy_d) if db.hay_cierres() else None
                if cierre_hoy:
                    st.caption(f"✅ Guardado hoy: diferencia de caja {md_pesos(float(cierre_hoy['diferencia']))}.")
                else:
                    st.caption(f"Al cerrar, en 🧮 Cierre de Caja: contá el cajón y tocá «Guardar el cierre del día» "
                               f"(+{desafio.PUNTOS_CIERRE} ⭐, no importa si cierra justo). Si se compró pan, cargá el "
                               "sobrante o marcá que no sobró.")
                st.progress(min(est_d["cierres"] / desafio.CIERRES_ALCANCIA, 1.0),
                            text=f"{est_d['cierres']} de {desafio.CIERRES_ALCANCIA} cierres para la Alcancía")
            with st.container(border=True):
                st.markdown("**🎁 Premios que puso el comerciante**")
                lineas = [(n, premios_d.get(c)) for c, n, _ in desafio.FIGURITAS] + [("Boletín de la semana", premios_d.get("boletin"))]
                lineas = [f"- **{n}**: {p}" for n, p in lineas if p]
                st.markdown("\n".join(lineas) if lineas else "_Todavía sin premios: se cargan en 🔒 Configuración._")
            with st.container(border=True):
                st.markdown("**🪙 Recreo con clientes**")
                st.caption(f"Después de una compra desde {md_pesos(desafio.parametros()['umbral'])}, Registrar Venta "
                           f"pregunta si jugaron a las monedas: +{desafio.PUNTOS_JUEGO} ⭐ (hasta "
                           f"{desafio.JUEGOS_QUE_SUMAN_POR_DIA} por día).")
                st.progress(min(est_d["juegos"] / desafio.JUEGOS_MONEDITA, 1.0),
                            text=f"{est_d['juegos']} de {desafio.JUEGOS_MONEDITA} juegos para la Monedita "
                                 f"· esta semana {est_d['juegos_semana']}")
            with st.container(border=True):
                mp = est_d["mapa"]
                st.markdown("**🗺️ Mapa completo**")
                st.progress(mp["listas"] / mp["categorias"] if mp["categorias"] else 0.0,
                            text=f"{mp['listas']} de {mp['categorias']} categorías con {desafio.CONTROLES_MAPA} fechas o más")
                if mp["faltan"]:
                    st.caption("Faltan: " + ", ".join(mp["faltan"]))
            with st.container(border=True):
                st.markdown("**🏅 Insignias**")
                st.markdown("\n".join(f"- {'🏅' if ok else '▫️'} {n}" for n, ok in est_d["insignias"].items()))

    with tabs_d[1]:
        est_a = desafio.estado(pid, hoy_d)
        ganadas = sum(f["ganada"] for f in est_a["figuritas"])
        st.caption(f"{ganadas} de {len(est_a['figuritas'])} figuritas. Cada una es un hito; si el comerciante le puso premio, "
                   "aparece al ganarla.")
        cols_a = st.columns(4)
        for i, f in enumerate(est_a["figuritas"]):
            with cols_a[i % 4]:
                with st.container(border=True):
                    st.markdown(f"<div style='text-align:center'>{figuritas.svg(f['clave'], 112, f['ganada'], f['nombre'])}</div>",
                                unsafe_allow_html=True)
                    st.markdown(f"**{f['nombre'] if f['ganada'] else '🔒 ' + f['nombre']}**")
                    st.caption(f["como"])
                    if f["ganada"] and premios_d.get(f["clave"]):
                        st.success(f"🎁 {premios_d[f['clave']]}")

    if ES_DUENO:
        with tabs_d[2]:
            st.caption("🔒 Solo la ve el dueño. Acá se ponen los premios, cuánto pide la tarea, el juego de las monedas "
                       "y quién juega.")
            st.subheader("🎁 Premios")
            filas_p = [{"hito": c, "Hito": n, "Premio": premios_d.get(c, "")} for c, n, _ in desafio.FIGURITAS]
            filas_p.append({"hito": "boletin", "Hito": "Cada boletín de la semana", "Premio": premios_d.get("boletin", "")})
            ed_p = st.data_editor(pd.DataFrame(filas_p), hide_index=True, width="stretch", key="desafio_premios_ed",
                                  column_config={"hito": None, "Hito": st.column_config.TextColumn(disabled=True),
                                                 "Premio": st.column_config.TextColumn(
                                                     "Premio", help="Lo que se gana: «un alfajor», «salir 1 h antes»...")})
            if st.button("Guardar premios", type="primary", key="desafio_premios_btn"):
                desafio.guardar_premios(dict(zip(ed_p["hito"], ed_p["Premio"].fillna(""))))
                avisar("success", "Premios guardados.")
                st.rerun()

            st.subheader("⚙️ Tarea y juego de las monedas")
            prm = desafio.parametros()
            arts_m = db.listar_articulos()
            arts_m = arts_m[arts_m["categoria"] != "Promocion"] if "categoria" in arts_m else arts_m
            ids_m = [None] + arts_m["id"].astype(int).tolist()
            nombres_m = dict(zip(arts_m["id"].astype(int), arts_m["nombre"]))
            with st.form("desafio_param", enter_to_submit=False):
                p1, p2 = st.columns(2)
                n_dia = p1.number_input("Artículos por día", 1, 20, prm["articulos_dia"], 1)
                n_com = p2.number_input("Comodines por semana", 0, 3, prm["comodines"], 1,
                                        help="Días que se pueden saltear sin cortar la racha ni perder el boletín.")
                n_cont = st.number_input("De esos, cuántos se cuentan (conteo a ciegas)", 0, 20, prm["conteos_dia"], 1,
                                         help="En esos artículos no se muestra el stock de la app: se cuenta y se compara.")
                p3, p4, p5 = st.columns(3)
                umbral_m = p3.number_input("Juego desde una compra de ($)", 0.0, value=prm["umbral"], step=1000.0, format="%g")
                monedas_m = p4.number_input("Monedas por juego", 1.0, value=prm["monedas"], step=1.0, format="%g")
                art_m = p5.selectbox("Artículo de las monedas", ids_m,
                                     index=ids_m.index(prm["articulo_monedas"]) if prm["articulo_monedas"] in ids_m else 0,
                                     format_func=lambda i: "— elegir —" if i is None else nombres_m.get(i, str(i)))
                if st.form_submit_button("Guardar", type="primary"):
                    try:
                        n = desafio.guardar_parametros(n_dia, n_com, umbral_m, monedas_m, art_m, n_cont)
                        avisar("success", f"Guardado ({n} cambios). La tarea nueva cambia desde mañana.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

            st.subheader("👥 Quién juega")
            part = desafio.participantes()
            elegidos = st.multiselect("Participantes", part["persona_id"].tolist(),
                                      default=part.loc[part["participa"], "persona_id"].tolist(),
                                      format_func=dict(zip(part["persona_id"], part["nombre"])).get,
                                      key="desafio_part")
            if st.button("Guardar participantes", key="desafio_part_btn"):
                desafio.guardar_participantes(elegidos)
                avisar("success", "Participantes guardados.")
                st.rerun()

            st.subheader("📏 Lo que vamos aprendiendo: días disponibles por categoría")
            st.caption("Días entre la compra más vieja que quedaba y la fecha vista en la góndola. Es el dato que "
                       "usa la alerta de vencimientos. Solo cuentan los controles con compra conocida (el stock "
                       "inicial sin factura no la tiene).")
            dt_tip = desafio.dias_tipicos()
            if dt_tip.empty:
                st.info("Todavía no hay controles con fecha de compra conocida.")
            else:
                st.dataframe(dt_tip.rename(columns={"categoria": "Categoría", "controles": "Controles",
                                                    "articulos": "Artículos", "mediana": "Mediana (días)",
                                                    "minimo": "Mín.", "maximo": "Máx.", "configurado": "Configurado"}),
                             hide_index=True, width="stretch")
                listas_t = dt_tip[dt_tip["controles"] >= 3]
                if len(listas_t) and st.button(f"Usar la mediana en {len(listas_t)} categorías (con 3 controles o más)",
                                               key="desafio_copiar_mediana"):
                    try:
                        n = db.guardar_vida_util_categorias(
                            {r.categoria: int(round(r.mediana)) for r in listas_t.itertuples()})
                        avisar("success", f"Vida útil actualizada en {n} categorías.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

elif pagina == "📜 Historial de Cambios":
    st.title("📜 Historial de Cambios")
    st.caption(
        "Registro de ediciones sensibles: cambios de precio/margen, correcciones de stock a mano, "
        "promociones creadas, editadas o armadas, y todo lo que se elimina (ventas, facturas, gastos, artículos)."
    )

    TABLAS_HIST = {"Todas": "Todas", "articulos": "Artículos y promociones", "ventas": "Ventas",
                   "facturas": "Facturas", "gastos": "Gastos", "personas": "Usuarios",
                   "parametros": "Parámetros"}
    c1, c2, c3 = st.columns(3)
    desde_hist = c1.date_input("Desde", value=hoy_argentina().replace(day=1), key="hist_desde", format="DD/MM/YYYY")
    hasta_hist = c2.date_input("Hasta", value=hoy_argentina(), key="hist_hasta", format="DD/MM/YYYY")
    tabla_hist = c3.selectbox("Tabla", list(TABLAS_HIST), format_func=TABLAS_HIST.get, key="hist_tabla")

    historial = db.listar_historial(
        desde=desde_hist, hasta=hasta_hist,
        tabla=None if tabla_hist == "Todas" else tabla_hist
    )
    if historial.empty:
        st.info("No hay cambios registrados en ese período.")
    else:
        st.dataframe(
            historial[["fecha", "usuario", "tabla", "accion", "detalle"]]
            .rename(columns={"fecha": "Fecha", "usuario": "Quién", "tabla": "Tabla", "accion": "Acción",
                             "detalle": "Detalle"}),
            hide_index=True, width="stretch",
            # hora argentina (la base guarda en UTC; db.listar_historial la convierte)
            column_config={"Fecha": st.column_config.DatetimeColumn("Fecha", format="DD/MM/YYYY HH:mm")},
        )

# ============================================================
# CONFIGURACIÓN (Sprint 2): usuarios, permisos y parámetros. Solo dueño.
# ============================================================
elif pagina == "⚙️ Configuración":
    st.title("⚙️ Configuración")

    def nombre_pantalla(clave):
        return PAGINAS_POR_CLAVE.get(clave, clave)

    st.subheader("👤 Usuarios")
    personas_df = db.listar_personas(incluir_inactivas=True)
    st.dataframe(
        pd.DataFrame({
            "Nombre": personas_df["nombre"],
            "Usuario": personas_df["usuario"],
            "Rol": personas_df["rol"].map(db.ROLES_PERSONA),
            "Pantallas": [
                "Todas" if r == "dueño" else (", ".join(nombre_pantalla(c) for c in p) or "Ninguna")
                for r, p in zip(personas_df["rol"], personas_df["permisos"])
            ],
            "Contraseña": personas_df["tiene_clave"].map({True: "✔", False: "Falta"}),
            "Activo": personas_df["activo"].map({True: "Sí", False: "No"}),
        }),
        hide_index=True, width="stretch",
    )

    tab_nuevo, tab_editar = st.tabs(["➕ Nuevo usuario", "✏️ Editar usuario"])

    with tab_nuevo:
        with st.form("form_nuevo_usuario", clear_on_submit=True):
            c1, c2 = st.columns(2)
            nombre_nuevo = c1.text_input("Nombre", placeholder="ej: Laura")
            usuario_nuevo = c2.text_input("Usuario para entrar", placeholder="ej: laura")
            c1, c2 = st.columns(2)
            clave_nueva = c1.text_input("Contraseña inicial (mínimo 8)", type="password")
            rol_nuevo = c2.selectbox("Rol", list(db.ROLES_PERSONA), format_func=db.ROLES_PERSONA.get, index=1)
            permisos_nuevos = st.multiselect(
                "Pantallas que puede ver (solo para rol Atención; el dueño ve todo)",
                PAGINAS_HABILITABLES, default=["registrar_venta"], format_func=nombre_pantalla,
            )
            if st.form_submit_button("Crear usuario", type="primary"):
                try:
                    db.crear_persona(nombre_nuevo, usuario_nuevo, clave_nueva, rol_nuevo, permisos_nuevos)
                    # la tabla de usuarios de arriba ya se dibujó: se recarga para que aparezca
                    avisar("success", f"Usuario '{usuario_nuevo.strip().lower()}' creado. Pasale la contraseña "
                                      "en persona; si hay que cambiarla, se cambia desde acá.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    with tab_editar:
        por_id = personas_df.set_index("id")
        pid = st.selectbox(
            "Usuario", personas_df["id"].tolist(), key="config_persona",
            format_func=lambda i: f"{por_id.at[i, 'nombre']} ({por_id.at[i, 'usuario']})"
                                  + ("" if por_id.at[i, "activo"] else " — inactivo"),
        )
        fila = por_id.loc[pid]
        with st.form("form_permisos"):
            rol_ed = st.selectbox("Rol", list(db.ROLES_PERSONA), format_func=db.ROLES_PERSONA.get,
                                  index=list(db.ROLES_PERSONA).index(fila["rol"]), key=f"rol_ed_{pid}")
            permisos_ed = st.multiselect(
                "Pantallas que puede ver (si es Atención)", PAGINAS_HABILITABLES,
                default=[c for c in fila["permisos"] if c in PAGINAS_HABILITABLES],
                format_func=nombre_pantalla, key=f"perm_ed_{pid}",
            )
            if st.form_submit_button("💾 Guardar rol y pantallas"):
                try:
                    db.actualizar_persona(pid, rol_ed, permisos_ed)
                    st.success("Guardado. Se aplica la próxima vez que esa persona toque algo en la app.")
                except ValueError as e:
                    st.error(str(e))

        c1, c2 = st.columns(2)
        with c1.form("form_reset_clave", clear_on_submit=True):
            nueva_clave = st.text_input("Nueva contraseña para este usuario", type="password")
            if st.form_submit_button("🔑 Asignar contraseña"):
                try:
                    db.cambiar_clave(pid, nueva_clave)
                    st.success("Contraseña asignada.")
                except ValueError as e:
                    st.error(str(e))
        c2.write("")
        if c2.button("🚫 Desactivar (no puede entrar)" if fila["activo"] else "✅ Reactivar",
                     width="stretch", key="btn_estado_persona"):
            try:
                db.cambiar_estado_persona(pid, not fila["activo"])
                st.rerun()
            except ValueError as e:
                c2.error(str(e))
        c2.caption("Desactivar no borra nada: sus ventas y cambios siguen a su nombre.")

    st.divider()
    st.subheader("🔧 Parámetros de reposición")
    st.caption("Los usa la lista de compras y las alertas de reposición.")
    params = db.obtener_parametros()
    with st.form("form_parametros"):
        c1, c2 = st.columns(2)
        frecuencia = c1.number_input(
            "Días entre viajes al mayorista", min_value=1, max_value=60, step=1,
            value=int(params["frecuencia_viaje_dias"]),
            help="Cada cuántos días se hace la compra planificada (hoy, una vez por semana = 7).",
        )
        capacidad = c2.number_input(
            "Capacidad del depósito (litros)", min_value=1, step=100,
            value=int(params["capacidad_litros"]),
            help="Espacio útil para guardar mercadería. Estimado inicial: 2.000 L.",
        )
        st.markdown("**Nivel de servicio objetivo por clase ABC** (probabilidad de no quedarse sin stock entre viajes)")
        cA, cB, cC = st.columns(3)
        nivel = {}
        for col, clase in zip((cA, cB, cC), "ABC"):
            nivel[clase] = col.number_input(
                f"Clase {clase} (%)", min_value=50.0, max_value=99.9, step=1.0,
                value=round(params[f"nivel_servicio_{clase}"] * 100, 1), key=f"ns_{clase}",
            )
        if st.form_submit_button("💾 Guardar parámetros", type="primary"):
            try:
                cambios = sum([
                    db.actualizar_parametro("frecuencia_viaje_dias", frecuencia),
                    db.actualizar_parametro("capacidad_litros", capacidad),
                    *[db.actualizar_parametro(f"nivel_servicio_{c}", nivel[c] / 100) for c in "ABC"],
                ])
                st.success(f"Guardado ({cambios} cambio(s))." if cambios else "No había cambios.")
            except ValueError as e:
                st.error(str(e))

    # ---------------- Días disponibles para la venta por categoría (tanda 18) ----------------
    st.divider()
    st.subheader("⏰ Días disponibles para la venta por categoría")
    st.caption(
        "Cuántos días se puede vender un producto desde que entra con la factura. Con eso la app estima cuándo "
        "vence cada compra, sin anotar lotes. Se usa para los artículos que no tienen sus propios días cargados "
        "(🛒 Lista de compras → Artículos de compra diaria y perecederos). Vacío = la categoría no vence o no "
        "se controla. Si un producto vino con menos vida de la habitual, cargá la fecha real en la factura."
    )
    if not db.hay_tanda18():
        st.info("Para usar esto hay que correr una vez `python migracion_tanda18.py`.")
    else:
        vida_cat = db.vida_util_por_categoria()
        cats_cfg = sorted(c for c in db.listar_articulos()["categoria"].dropna().unique() if c != "Promocion")
        tabla_vida = pd.DataFrame({
            "Categoría": cats_cfg,
            "Días disponibles": [float(vida_cat[c]) if c in vida_cat else np.nan for c in cats_cfg],
        })
        vida_ed = st.data_editor(
            tabla_vida, hide_index=True, width="stretch", key="cfg_vida_cat", disabled=["Categoría"],
            column_config={"Días disponibles": st.column_config.NumberColumn(min_value=1, max_value=3650, step=1,
                                                                            format="%d")},
        )
        if st.button("💾 Guardar días por categoría", key="cfg_vida_guardar"):
            try:
                n = db.guardar_vida_util_categorias(
                    {r["Categoría"]: (None if pd.isna(r["Días disponibles"]) else int(r["Días disponibles"]))
                     for _, r in vida_ed.iterrows()})
                avisar("success", f"Guardado ({n} categoría/s modificada/s)." if n else "No había cambios.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))
