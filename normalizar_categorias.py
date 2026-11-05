"""
Normaliza el campo `categoria` de TODOS los artículos ya cargados en la
base, para que quede en formato "primera letra Mayúscula, el resto en
minúscula" (ej. "BEBIDAS " o "bebidas" -> "Bebidas"). Es la migración de
datos que corresponde después de agregar el desplegable de categoría en
"Nuevo artículo" / "Editar artículo" -- sin esto, el desplegable igual
funciona (arma su lista ya normalizada al vuelo), pero los datos crudos
en la base siguen con las variantes viejas hasta correr esto una vez.

Es seguro correrlo más de una vez (no hace nada si ya está todo
normalizado) y NO toca nombre, marca, costo, stock ni ningún otro campo
-- solo `categoria`.

Se corre UNA vez desde la carpeta del proyecto (donde están app.py y
db.py), con el mismo entorno/virtualenv que se usa para correr la app:

    cd C:\\Users\\nicou\\OneDrive\\Escritorio\\TFG
    python normalizar_categorias.py
"""
import sys

import db
from sqlalchemy import text


def probar_conexion():
    """Mismo chequeo previo que usa importar_productos.py, para no
    descubrir un problema de conexión a mitad de la migración."""
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
        sys.exit(1)


def main():
    probar_conexion()

    antes = db.listar_categorias()
    print(f"Categorías distintas ANTES de normalizar (ya deduplicadas): {len(antes)}")
    for c in antes:
        print(" -", c)

    cambios = db.normalizar_categorias_existentes()

    despues = db.listar_categorias()
    print(f"\nFilas de artículos con categoría corregida: {cambios}")
    print(f"Categorías distintas DESPUÉS de normalizar: {len(despues)}")
    for c in despues:
        print(" -", c)


if __name__ == "__main__":
    main()
