"""
Migración (12/10/2026): guardar el costo de cada venta.

REEMPLAZADA por migracion_precio_lista_en_venta.py (13/10/2026), que hace
todo lo que hacía esta y además guarda el precio de lista. Se deja este
archivo para que, si se corre, haga lo mismo que la nueva.

    python superados/migraciones/migracion_costo_en_venta.py
"""
import migracion_precio_lista_en_venta

if __name__ == "__main__":
    migracion_precio_lista_en_venta.main()
