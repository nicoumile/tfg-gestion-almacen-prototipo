"""
Borra DE RAIZ un artículo y TODO lo relacionado: sus ventas ya registradas,
las líneas de factura donde se compró, su uso como componente de alguna
promoción/combo, sus fraccionamientos, su alias de proveedor, su historial
de precios por proveedor y su historial de cambios. Es IRREVERSIBLE: borra
ventas/ingresos reales, no solo el artículo.

Pensado para artículos que quedaron mal cargados desde el inicio y no
representan ventas/compras reales (por eso se borra todo, no solo el
artículo). Si en cambio un artículo dejó de venderse pero sus ventas
pasadas SÍ son reales, no uses este script: usá "Editar artículo -> Dar de
baja" en la app (no borra nada, solo lo saca de circulación conservando el
historial para el ABC y los márgenes).

Qué hace, en este orden, sin escribir nada hasta pedir la confirmación
final:

  1. Identifica el artículo: le podés pasar el ID como argumento
     (`python eliminar_articulo.py 533`), o correrlo sin argumentos y
     escribir el ID o un texto para buscarlo por nombre (no hace falta
     escribir el nombre completo ni respetar mayúsculas/tildes). Si hay
     más de un artículo que coincide, los lista y pedís el ID correcto.
  2. Junta TODO lo que depende de ese artículo: cuántas ventas tiene y por
     cuánta plata, en cuántas facturas aparece, si se usa como componente
     de alguna promoción (y cuáles), si participa de algún fraccionamiento,
     si algún otro artículo está "fraccionado de" este o lo tiene cargado
     como "reemplazado por", su alias y su historial de precios por
     proveedor, y sus entradas en el historial de cambios.
  3. Muestra un resumen COMPLETO de todo lo que se va a borrar (plata
     incluida) y pide escribir la palabra exacta ELIMINAR para continuar.
     Cualquier otra cosa cancela sin tocar la base.
  4. Recién ahí borra todo, en el orden correcto, dentro de una única
     transacción (todo o nada):
       - Las ventas del artículo (se revierte el stock que habían
         afectado, como al borrar una venta normal).
       - Las líneas de factura donde se compró (ídem con el stock).
       - Su uso como componente de combos/promociones.
       - Los fraccionamientos donde participa.
       - Si otro artículo estaba "fraccionado de" este, o lo tenía como
         "reemplazado por", se lo desvincula (ese otro artículo NO se
         borra, solo pierde esa referencia).
       - Sus entradas de historial de cambios.
       - El artículo en sí (esto de paso borra en cascada su alias de
         proveedor y su historial de precio por proveedor).
     Al final deja una única línea en el historial de cambios dejando
     constancia de que el artículo fue eliminado por completo, con el
     resumen de lo que tenía.

Se puede cortar en cualquier momento con Ctrl+C sin que quede nada a
medias (nada se escribe hasta el paso 4).

Se corre localmente:

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python eliminar_articulo.py                  (pide el ID o el nombre)
    python eliminar_articulo.py 533               (va directo a ese ID)
"""
import sys
import unicodedata

import db
from sqlalchemy import text

CONFIRMACION_REQUERIDA = "ELIMINAR"


def _sin_tildes(txt):
    return "".join(
        c for c in unicodedata.normalize("NFD", txt or "")
        if unicodedata.category(c) != "Mn"
    )


def elegir_articulo():
    entrada = sys.argv[1].strip() if len(sys.argv) > 1 else input(
        "Escribí el ID del artículo a eliminar, o un texto para buscarlo por nombre: "
    ).strip()

    if entrada.isdigit():
        return int(entrada)

    palabras = _sin_tildes(entrada).lower().split()
    if not palabras:
        print("*** No escribiste nada. Abortado. ***")
        sys.exit(1)

    with db.engine.connect() as conn:
        filas = conn.execute(text("""
            SELECT id, nombre, marca, categoria, activo, stock_actual, costo_compra_actual
            FROM articulos
            ORDER BY id
        """)).mappings().all()

    candidatos = [
        f for f in filas
        if all(p in _sin_tildes(f["nombre"]).lower() for p in palabras)
    ]

    if not candidatos:
        print(f"*** No encontré ningún artículo cuyo nombre contenga: {' '.join(palabras)}. ***")
        print("Probá con menos palabras, o escribí directamente el ID si ya lo sabés.")
        sys.exit(1)

    if len(candidatos) == 1:
        f = candidatos[0]
        print(f"Artículo encontrado: id={f['id']} -- {f['nombre']} ({f['marca'] or 's/marca'})")
        return int(f["id"])

    print(f"Encontré {len(candidatos)} artículos que coinciden:")
    for f in candidatos:
        estado = "" if f["activo"] else "  [INACTIVO]"
        print(f"  id={f['id']:<5} {f['nombre']:<35} marca={f['marca'] or '-':<15} "
              f"stock={f['stock_actual']} costo=${f['costo_compra_actual']:,.2f}{estado}")
    elegido = input("Escribí el ID correcto: ").strip()
    return int(elegido)


def juntar_impacto(conn, articulo_id):
    art = conn.execute(
        text("SELECT * FROM articulos WHERE id = :id"), {"id": articulo_id}
    ).mappings().first()
    if not art:
        print(f"*** El id {articulo_id} no existe en articulos. Abortado. ***")
        sys.exit(1)

    ventas = conn.execute(text("""
        SELECT COUNT(*) AS cant, COALESCE(SUM(monto_total), 0) AS total
        FROM ventas WHERE articulo_id = :id
    """), {"id": articulo_id}).mappings().first()

    compras = conn.execute(text("""
        SELECT COUNT(*) AS cant, COUNT(DISTINCT factura_id) AS facturas,
               COALESCE(SUM(cantidad * costo_unitario), 0) AS total
        FROM detalle_factura WHERE articulo_id = :id
    """), {"id": articulo_id}).mappings().first()

    combos = conn.execute(text("""
        SELECT a.id, a.nombre
        FROM promocion_componentes pc
        JOIN articulos a ON a.id = pc.promocion_articulo_id
        WHERE pc.componente_articulo_id = :id
    """), {"id": articulo_id}).mappings().all()

    fracc_bolsa = conn.execute(text("""
        SELECT f.id, a.nombre AS derivado, f.cantidad_bolsas, f.cantidad_generada, f.fecha
        FROM fraccionamientos f JOIN articulos a ON a.id = f.articulo_derivado_id
        WHERE f.articulo_bolsa_id = :id
    """), {"id": articulo_id}).mappings().all()

    fracc_derivado = conn.execute(text("""
        SELECT f.id, a.nombre AS bolsa, f.cantidad_bolsas, f.cantidad_generada, f.fecha
        FROM fraccionamientos f JOIN articulos a ON a.id = f.articulo_bolsa_id
        WHERE f.articulo_derivado_id = :id
    """), {"id": articulo_id}).mappings().all()

    dependientes_fraccion = conn.execute(text("""
        SELECT id, nombre FROM articulos WHERE articulo_fraccion_de = :id
    """), {"id": articulo_id}).mappings().all()

    dependientes_reemplazo = conn.execute(text("""
        SELECT id, nombre FROM articulos WHERE reemplazado_por = :id
    """), {"id": articulo_id}).mappings().all()

    alias = conn.execute(text("""
        SELECT COUNT(*) AS cant FROM articulo_alias WHERE articulo_id = :id
    """), {"id": articulo_id}).mappings().first()

    precios_proveedor = conn.execute(text("""
        SELECT COUNT(*) AS cant FROM articulo_proveedor_precio WHERE articulo_id = :id
    """), {"id": articulo_id}).mappings().first()

    historial = conn.execute(text("""
        SELECT COUNT(*) AS cant FROM historial_cambios WHERE tabla = 'articulos' AND registro_id = :id
    """), {"id": articulo_id}).mappings().first()

    propios_componentes = conn.execute(text("""
        SELECT COUNT(*) AS cant FROM promocion_componentes WHERE promocion_articulo_id = :id
    """), {"id": articulo_id}).mappings().first()

    return {
        "articulo": art, "ventas": ventas, "compras": compras, "combos": combos,
        "fracc_bolsa": fracc_bolsa, "fracc_derivado": fracc_derivado,
        "dependientes_fraccion": dependientes_fraccion,
        "dependientes_reemplazo": dependientes_reemplazo,
        "alias": alias, "precios_proveedor": precios_proveedor, "historial": historial,
        "propios_componentes": propios_componentes,
    }


def mostrar_resumen(impacto):
    art = impacto["articulo"]
    print("=" * 70)
    print("ESTO SE VA A BORRAR POR COMPLETO -- NO SE PUEDE DESHACER")
    print("=" * 70)
    estado = "activo" if art["activo"] else "YA ESTABA DADO DE BAJA (inactivo)"
    print(f"Artículo: {art['nombre']} (id={art['id']}, marca={art['marca'] or '-'}, "
          f"categoría={art['categoria'] or '-'}, {estado})")
    print(f"Stock actual: {art['stock_actual']}  |  Costo de compra: ${art['costo_compra_actual']:,.2f}")
    print("-" * 70)

    v = impacto["ventas"]
    print(f"VENTAS: {v['cant']} venta(s) registradas, por un total de ${float(v['total']):,.2f} "
          f"en ingresos reales que se van a borrar.")

    c = impacto["compras"]
    print(f"COMPRAS: {c['cant']} línea(s) de factura, en {c['facturas']} factura(s) distinta(s), "
          f"por ${float(c['total']):,.2f} en costo de compra.")

    if impacto["combos"]:
        nombres = ", ".join(f"'{r['nombre']}' (id={r['id']})" for r in impacto["combos"])
        print(f"⚠️  COMBOS/PROMOCIONES AFECTADAS: este artículo es componente de {len(impacto['combos'])} "
              f"combo(s): {nombres}. Esos combos van a quedar SIN este componente (van a dejar de "
              f"funcionar bien hasta que los edites).")
    else:
        print("Combos/promociones: no se usa como componente de ninguno.")

    if impacto["propios_componentes"]["cant"]:
        print(f"Este artículo es a su vez un combo con {impacto['propios_componentes']['cant']} "
              f"componente(s) propio(s) -- esos vínculos se borran junto con él (normal, es su propia "
              f"receta).")

    if impacto["fracc_bolsa"]:
        print(f"FRACCIONAMIENTOS como bolsa de origen: {len(impacto['fracc_bolsa'])} registro(s) "
              f"(productos derivados de este). Se revierte el stock que generaron.")
    if impacto["fracc_derivado"]:
        print(f"FRACCIONAMIENTOS como producto derivado: {len(impacto['fracc_derivado'])} registro(s). "
              f"Se revierte el stock que habían consumido de la bolsa de origen.")

    if impacto["dependientes_fraccion"]:
        nombres = ", ".join(f"'{r['nombre']}' (id={r['id']})" for r in impacto["dependientes_fraccion"])
        print(f"⚠️  OTROS ARTÍCULOS que dependen de este para 'Fraccionar': {nombres}. "
              f"NO se borran, pero van a quedar sin su artículo de origen (hay que revisarlos a mano).")

    if impacto["dependientes_reemplazo"]:
        nombres = ", ".join(f"'{r['nombre']}' (id={r['id']})" for r in impacto["dependientes_reemplazo"])
        print(f"⚠️  OTROS ARTÍCULOS que tienen a este cargado como 'reemplazado por': {nombres}. "
              f"NO se borran, pero pierden esa referencia (hay que revisarlos a mano).")

    print(f"Alias de proveedor guardados: {impacto['alias']['cant']} (se borran).")
    print(f"Historial de precio por proveedor: {impacto['precios_proveedor']['cant']} registro(s) (se borran).")
    print(f"Entradas en el historial de cambios: {impacto['historial']['cant']} (se borran).")
    print("=" * 70)


def eliminar(conn, articulo_id, impacto):
    art = impacto["articulo"]
    resumen_historial = (
        f"Eliminación total de '{art['nombre']}' (id={articulo_id}): "
        f"{impacto['ventas']['cant']} venta(s) por ${float(impacto['ventas']['total']):,.2f}, "
        f"{impacto['compras']['cant']} línea(s) de compra, "
        f"{len(impacto['combos'])} combo(s) afectado(s)."
    )

    # 1) Desvincular artículos que dependían de este para "Fraccionar" o
    #    que lo tenían cargado como "reemplazado por" (no se borran esos
    #    artículos, solo pierden la referencia).
    conn.execute(text("""
        UPDATE articulos SET articulo_fraccion_de = NULL, factor_fraccion = NULL
        WHERE articulo_fraccion_de = :id
    """), {"id": articulo_id})
    conn.execute(text("""
        UPDATE articulos SET reemplazado_por = NULL WHERE reemplazado_por = :id
    """), {"id": articulo_id})

    # 2) Sacarlo como componente de cualquier combo/promoción, y del desglose
    #    guardado de ventas de combos ya hechas (tabla de la migración tanda 5;
    #    si todavía no existe, se saltea).
    conn.execute(text("""
        DELETE FROM promocion_componentes WHERE componente_articulo_id = :id
    """), {"id": articulo_id})
    if conn.execute(text("SELECT to_regclass('public.venta_componentes')")).scalar():
        conn.execute(text("""
            DELETE FROM venta_componentes WHERE componente_articulo_id = :id
        """), {"id": articulo_id})

    # 3) Fraccionamientos donde participa (como bolsa o como derivado).
    conn.execute(text("""
        DELETE FROM fraccionamientos
        WHERE articulo_bolsa_id = :id OR articulo_derivado_id = :id
    """), {"id": articulo_id})

    # 4) Líneas de factura (compras) donde se cargó.
    conn.execute(text("""
        DELETE FROM detalle_factura WHERE articulo_id = :id
    """), {"id": articulo_id})

    # 5) Ventas ya registradas.
    conn.execute(text("""
        DELETE FROM ventas WHERE articulo_id = :id
    """), {"id": articulo_id})

    # 6) Su propio historial de cambios (antes de borrarlo, para no dejar
    #    entradas viejas mezcladas con la nueva de "eliminado").
    conn.execute(text("""
        DELETE FROM historial_cambios WHERE tabla = 'articulos' AND registro_id = :id
    """), {"id": articulo_id})

    # 7) El artículo en sí (cascada automática: su alias de proveedor, su
    #    historial de precio por proveedor, y -- si era un combo -- su
    #    propia lista de componentes).
    conn.execute(text("DELETE FROM articulos WHERE id = :id"), {"id": articulo_id})

    # 8) Dejar constancia de que se borró, con el resumen completo.
    db.registrar_historial("articulos", articulo_id, "ELIMINACION_TOTAL", resumen_historial, conn=conn)


def main():
    articulo_id = elegir_articulo()

    with db.engine.connect() as conn:
        impacto = juntar_impacto(conn, articulo_id)

    mostrar_resumen(impacto)

    confirmar = input(
        f"\nEscribí exactamente '{CONFIRMACION_REQUERIDA}' para borrar todo esto de forma "
        "irreversible (cualquier otra cosa cancela): "
    ).strip()
    if confirmar != CONFIRMACION_REQUERIDA:
        print("Cancelado -- no se borró nada.")
        return

    with db.engine.begin() as conn:
        eliminar(conn, articulo_id, impacto)

    print("\n✅ Artículo y todo lo relacionado fue eliminado por completo.")
    if impacto["combos"]:
        nombres = ", ".join(f"'{r['nombre']}'" for r in impacto["combos"])
        print(f"⚠️  Recordá revisar estos combos que se quedaron sin este componente: {nombres}")
    if impacto["dependientes_fraccion"]:
        nombres = ", ".join(f"'{r['nombre']}'" for r in impacto["dependientes_fraccion"])
        print(f"⚠️  Recordá revisar estos artículos que quedaron sin su origen de fraccionamiento: {nombres}")
    if impacto["dependientes_reemplazo"]:
        nombres = ", ".join(f"'{r['nombre']}'" for r in impacto["dependientes_reemplazo"])
        print(f"⚠️  Recordá revisar estos artículos que perdieron su 'reemplazado por': {nombres}")


if __name__ == "__main__":
    main()
