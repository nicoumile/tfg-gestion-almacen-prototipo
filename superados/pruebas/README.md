# Pruebas de la app (antes de subir una tanda)

Scripts para comprobar que una versión nueva de la app **hace lo mismo que la anterior** y que las pantallas abren, sobre una **copia local** de producción. Nunca se corren contra Neon: escriben ventas, facturas y gastos de prueba. `config.py` se niega a correr si el host parece de Neon.

Esta carpeta no trae datos. La copia se arma con un respaldo de `respaldo_produccion.py`, que queda en `respaldos/` y no va al repo.

## Qué hace falta

- PostgreSQL 16 instalado en la compu (local).
- `pip install -r requirements.txt playwright psycopg2-binary` y `playwright install chromium`.
- Variables de entorno, si no usás los valores por defecto:

| Variable | Por defecto | Para qué |
|---|---|---|
| `PRUEBAS_DB_HOST` / `PRUEBAS_DB_PORT` | localhost / 5432 | PostgreSQL local |
| `PRUEBAS_DB_USER` / `PRUEBAS_DB_PASSWORD` | postgres / postgres | |
| `PRUEBAS_URL` | http://localhost:8501 | app corriendo, para las pruebas en navegador |
| `PRUEBAS_CLAVE` | prueba1234 | contraseña de los usuarios de prueba |
| `PRUEBAS_CHROME` | (vacío) | ruta de otro Chrome/Chromium, si no se usa el de Playwright |

## Pasos

Desde la raíz del repositorio:

```
# 1. Copia local de producción, una por versión (vieja = lo que está subido, nueva = la tanda)
python superados/pruebas/cargar_copia.py respaldos/<carpeta_del_respaldo> copia_vieja
python superados/pruebas/cargar_copia.py respaldos/<carpeta_del_respaldo> copia_nueva

# 2. Foto del comportamiento: lecturas (reportes, ABC, márgenes, pronóstico, lista de
#    compras) y un escenario fijo de escrituras (venta de carrito, factura, sobrante,
#    editar y borrar venta, armar promo, gasto). Después, comparar.
python superados/pruebas/foto.py <carpeta_version_vieja> copia_vieja foto_vieja.pkl
python superados/pruebas/foto.py <carpeta_version_nueva> copia_nueva foto_nueva.pkl
python superados/pruebas/comparar.py foto_vieja.pkl foto_nueva.pkl
#    Muestra solo lo que cambió. Lo esperable es que cambie únicamente lo que la tanda
#    dice que cambia.

# 3. Todas las pantallas, como dueño y como atención (sin navegador)
python superados/pruebas/pantallas.py <carpeta_version_nueva> copia_nueva

# 4. La pantalla de venta: mismas ventas en las dos versiones
python superados/pruebas/probar_venta_ui.py <carpeta_version_vieja> copia_vieja venta_vieja.pkl vieja
python superados/pruebas/probar_venta_ui.py <carpeta_version_nueva> copia_nueva venta_nueva.pkl nueva

# 5. En navegador real: levantar la app contra la copia y probar
python superados/pruebas/preparar_usuarios.py copia_nueva
set DB_NAME=copia_nueva& set DB_HOST=localhost& streamlit run app.py
python superados/pruebas/navegador.py          # menú, direcciones, sesión, permisos
python superados/pruebas/venta_atendio.py copia_nueva   # la venta del carrito guarda quién atendió

# 6. Controles de la tanda 12 (en una copia con python superados/migraciones/migracion_tanda12.py aplicada)
python superados/pruebas/controles_tanda12.py <carpeta_version_nueva> copia_nueva
```

Para el paso 5, `app.py` lee la base de `DB_HOST`, `DB_NAME`, etc. Si existe `.streamlit/secrets.toml` (el de producción), tiene prioridad sobre las variables. Por eso hay que correr la app desde una copia de la carpeta sin ese archivo.

## Notas

- El escenario de `foto.py` usa artículos fijos (ids 3, 112, 163, 244, 309, 491, 499) y la fecha 21/10/2026. Sirve con cualquier respaldo de producción posterior al 20/10/2026. Para otra base hay que cambiar esos ids.
- Si un respaldo trae tablas de control de migraciones que `schema.sql` no tiene (`migracion_respaldo_objetos`), `cargar_copia.py` las saltea con un aviso.
- `calidad` e `inmovilizado` dependen de la fecha del día: las dos fotos se sacan el mismo día.
- Los archivos `.pkl` y las capturas `.png` que generan las pruebas no se suben al repo.
