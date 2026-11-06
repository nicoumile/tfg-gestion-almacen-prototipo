import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))  # raíz del repositorio
# Script de uso único: exporta una "foto" del catálogo real (tabla articulos,
# vía la misma vista v_articulos que usa la app) a un CSV, para usar como base
# de la data simulada del Entregable 3 (ver claude/criterios-base-datos-
# simulada-entregable3.md en el proyecto Seminario II).
#
# No modifica nada en Neon: es una lectura (SELECT) de solo consulta.
#
# Cómo correrlo: parado en la carpeta TFG (donde están app.py/db.py), con el
# mismo entorno/virtualenv que ya usás para correr la app:
#
#     python superados/utilidades/exportar_catalogo_real.py
#
# Va a crear "catalogo_real_export.csv" en esa misma carpeta.

import db

df = db.listar_articulos()
df.to_csv("catalogo_real_export.csv", index=False, encoding="utf-8-sig")
print(f"Listo: {len(df)} artículos exportados a catalogo_real_export.csv")
