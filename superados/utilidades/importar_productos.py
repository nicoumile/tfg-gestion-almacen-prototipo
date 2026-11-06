"""
Carga masiva de productos desde productos_a_importar.csv hacia la base
(Neon, misma conexion que usa la app via .streamlit/secrets.toml).

IMPORTANTE:
- Solo INSERTA articulos nuevos. Antes de crear cada uno, busca si ya existe
  un articulo con el mismo nombre (sin importar mayusculas/minusculas) y, si
  existe, lo SALTEA sin tocarlo -- nunca actualiza ni pisa nada ya cargado.
- Se corre UNA vez desde la carpeta del proyecto (donde estan app.py y
  db.py), con el mismo entorno/virtualenv que usas para correr la app, por
  ejemplo:

      cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
      python superados/utilidades/importar_productos.py

- Requiere que productos_a_importar.csv este en la misma carpeta.
- Todos los articulos se crean con stock_actual = 0 y stock_minimo = 0
  (los valores reales se cargan despues, durante el control de stock).
  Por eso es normal y esperable que la app los marque como "stock bajo"
  hasta que hagas ese control.
- Todos se crean con unidad_medida = 'unidad'. Si alguno en realidad se
  vende suelto por peso (fiambres/quesos cortados al mostrador), se puede
  ajustar despues -- avisame cuales son y armamos ese ajuste puntual.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
import csv
import sys

import db
from sqlalchemy import text

ARCHIVO_CSV = "productos_a_importar.csv"


def probar_conexion():
    """
    Muestra a qué base se está intentando conectar (sin la contraseña) y
    hace una consulta trivial para detectar problemas de conexión ANTES de
    procesar las 438 filas -- así, si algo está mal, falla una sola vez con
    un mensaje claro en vez de repetir el mismo error 438 veces.
    """
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
        orig = getattr(e, "orig", None)
        if orig is not None:
            print(f"Error original del driver: {orig!r}")
        print(
            "\nRevisa: host/db/user de arriba (¿es el de Neon o quedo en "
            "localhost/postgres por default?), que .streamlit/secrets.toml "
            "este en esta misma carpeta, y tu conexion a internet."
        )
        sys.exit(1)


def main():
    probar_conexion()

    creados = []
    salteados_ya_existian = []
    errores = []

    with open(ARCHIVO_CSV, newline="", encoding="utf-8") as f:
        lector = csv.DictReader(f)
        filas = list(lector)

    print(f"Leidas {len(filas)} filas de {ARCHIVO_CSV}.\n")

    nombres_ya_procesados_en_esta_corrida = set()

    for i, fila in enumerate(filas, start=1):
        nombre = (fila.get("nombre") or "").strip()
        if not nombre:
            errores.append((i, fila, "fila sin nombre"))
            continue

        clave = nombre.lower()
        if clave in nombres_ya_procesados_en_esta_corrida:
            # Duplicado dentro del propio CSV -- no debería pasar si se usó
            # productos_a_importar.csv tal cual se generó, pero por las dudas
            # no lo insertamos dos veces.
            salteados_ya_existian.append(nombre + " (duplicado dentro del CSV)")
            continue

        try:
            existentes = db.buscar_articulo_por_nombre_exacto(nombre)
        except Exception as e:
            errores.append((i, fila, f"error buscando duplicado: {type(e).__name__}: {e}"))
            continue

        if not existentes.empty:
            salteados_ya_existian.append(nombre)
            nombres_ya_procesados_en_esta_corrida.add(clave)
            continue

        marca = (fila.get("marca") or "").strip() or None
        categoria = (fila.get("categoria") or "").strip() or None

        try:
            costo = float(fila["costo"])
            margen = float(fila["margen"])
            iva = float(fila["iva"])
        except (KeyError, ValueError, TypeError) as e:
            errores.append((i, fila, f"costo/margen/iva invalido: {e}"))
            continue

        unidad_medida = (fila.get("unidad_medida") or "unidad").strip() or "unidad"

        try:
            db.crear_articulo(
                nombre=nombre,
                categoria=categoria,
                costo_compra=costo,
                margen=margen,
                iva=iva,
                stock_inicial=0,
                stock_minimo=0,
                fecha_vencimiento=None,
                acceso_rapido=False,
                unidad_medida=unidad_medida,
                marca=marca,
                peso_bolsa_gramos=None,
            )
            creados.append(nombre)
            nombres_ya_procesados_en_esta_corrida.add(clave)
        except Exception as e:
            errores.append((i, fila, f"error al crear: {type(e).__name__}: {e}"))

    print(f"Creados: {len(creados)}")
    print(f"Salteados (ya existian): {len(salteados_ya_existian)}")
    print(f"Errores: {len(errores)}")

    if salteados_ya_existian:
        print("\n--- Salteados (ya existian, no se tocaron) ---")
        for n in salteados_ya_existian:
            print(" -", n)

    if errores:
        print("\n--- Errores (revisar) ---")
        for i, fila, msg in errores:
            print(f" - fila {i} ({fila.get('nombre')}): {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
