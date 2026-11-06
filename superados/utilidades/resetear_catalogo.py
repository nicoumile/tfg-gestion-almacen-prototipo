"""
Reinicio COMPLETO de la base + carga de la nueva lista de stock.

QUE HACE, EN ORDEN:
1) Se conecta y prueba la conexion (falla rapido y claro si algo esta mal
   configurado, en vez de arruinar todo a mitad de camino).
2) Guarda un BACKUP completo de TODAS las tablas (proveedores, articulos,
   articulo_alias, facturas, detalle_factura, ventas_ticket, ventas,
   gastos, fraccionamientos) en archivos CSV, en una carpeta nueva con
   fecha y hora, ANTES de tocar nada.
3) BORRA TODOS LOS DATOS de esas 9 tablas (TRUNCATE ... RESTART IDENTITY
   CASCADE) -- la base queda 100% vacia, como recien instalada. Los IDs
   vuelven a arrancar desde 1.
4) Carga los articulos de stock_a_cargar.csv (nombre, marca, categoria,
   unidad_medida, stock) SIN precios reales: costo_compra en 0, margen
   30% e IVA 21% (los valores por defecto de la base) -- se completan
   despues, articulo por articulo, desde "Actualizar precio/margen".

ES IRREVERSIBLE desde la app: la unica forma de volver atras es restaurar
a mano desde los CSV de backup que este mismo script genera en el paso 2.

Requiere que stock_a_cargar.csv este en la misma carpeta. Se corre UNA
vez desde la carpeta del proyecto, con el mismo entorno que se usa para
correr la app:

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python superados/utilidades/resetear_catalogo.py

Va a pedir que escribas literalmente BORRAR TODO para confirmar antes de
tocar nada -- si escribis cualquier otra cosa, se cancela sin cambiar
nada en la base.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
import csv
import os
import sys
from datetime import datetime

import db
from sqlalchemy import text

ARCHIVO_CSV = "stock_a_cargar.csv"

# Orden no importa (se usa CASCADE), pero se listan todas las tablas que
# tienen datos hoy en la base.
TABLAS = [
    "proveedores", "articulos", "articulo_alias", "facturas",
    "detalle_factura", "ventas_ticket", "ventas", "gastos", "fraccionamientos",
]


def probar_conexion():
    url = db.engine.url
    print(
        f"Conectando a: host={url.host!r} port={url.port!r} "
        f"db={url.database!r} user={url.username!r}"
    )
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("Conexion OK.\n")
    except Exception as e:
        print("\n*** No se pudo conectar a la base. Detalle completo: ***")
        print(f"{type(e).__name__}: {e}")
        sys.exit(1)


def backup_completo():
    carpeta = f"backup_antes_de_borrar_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(carpeta, exist_ok=True)
    print(f"Guardando backup de todas las tablas en la carpeta '{carpeta}'...")
    with db.engine.connect() as conn:
        for tabla in TABLAS:
            filas = conn.execute(text(f"SELECT * FROM {tabla}")).mappings().all()
            path = os.path.join(carpeta, f"{tabla}.csv")
            if filas:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
                    writer.writeheader()
                    for fila in filas:
                        writer.writerow(dict(fila))
                print(f"  {tabla}: {len(filas)} filas -> {path}")
            else:
                print(f"  {tabla}: 0 filas (no se crea archivo)")
    print(f"\nBackup completo. Si algo sale mal, los datos ANTERIORES quedan en '{carpeta}'.\n")
    return carpeta


def borrar_todo():
    with db.engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {', '.join(TABLAS)} RESTART IDENTITY CASCADE"))
    print("Listo: todas las tablas quedaron vacias.\n")


def cargar_stock():
    with open(ARCHIVO_CSV, newline="", encoding="utf-8") as f:
        filas = list(csv.DictReader(f))

    print(f"Cargando {len(filas)} articulos desde {ARCHIVO_CSV}...\n")
    creados = 0
    errores = []
    for i, fila in enumerate(filas, start=1):
        nombre = (fila.get("nombre") or "").strip()
        if not nombre:
            errores.append((i, "fila sin nombre"))
            continue

        marca = (fila.get("marca") or "").strip() or None
        categoria = (fila.get("categoria") or "").strip() or None
        unidad_medida = (fila.get("unidad_medida") or "unidad").strip() or "unidad"
        try:
            stock = float(fila["stock"])
        except (KeyError, ValueError, TypeError):
            stock = 0.0

        try:
            db.crear_articulo(
                nombre=nombre,
                categoria=categoria,
                costo_compra=0,
                margen=0.30,
                iva=0.21,
                stock_inicial=stock,
                stock_minimo=0,
                fecha_vencimiento=None,
                acceso_rapido=False,
                unidad_medida=unidad_medida,
                marca=marca,
                peso_bolsa_gramos=None,
            )
            creados += 1
        except Exception as e:
            errores.append((i, f"{nombre}: {type(e).__name__}: {e}"))

    print(f"Creados: {creados}")
    print(f"Errores: {len(errores)}")
    if errores:
        print("\n--- Errores (revisar) ---")
        for i, msg in errores:
            print(f"  fila {i}: {msg}")


def main():
    probar_conexion()

    print("=" * 70)
    print("ESTE SCRIPT VA A BORRAR TODOS LOS DATOS DE LA BASE:")
    print("articulos, facturas, detalle de facturas, ventas, tickets de venta,")
    print("proveedores, gastos operativos y fraccionamientos.")
    print("Antes de borrar, se guarda un backup completo en CSV en esta carpeta.")
    print("Esta accion NO se puede deshacer desde la app -- solo restaurando")
    print("a mano desde ese backup.")
    print("=" * 70)
    confirmacion = input("\nEscribi exactamente BORRAR TODO para continuar: ")
    if confirmacion.strip() != "BORRAR TODO":
        print("Cancelado -- no se borro ni se cargo nada.")
        sys.exit(0)

    backup_completo()
    borrar_todo()
    cargar_stock()
    print(
        "\nListo. El catalogo deberia tener los articulos nuevos, todos con "
        "costo $0 / margen 30% / IVA 21% por defecto -- hay que completar "
        "el costo y margen real de cada uno desde 'Actualizar precio/margen' "
        "cuando se tenga ese dato."
    )


if __name__ == "__main__":
    main()
