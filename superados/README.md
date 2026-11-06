# Superados

Archivos históricos. La app no los necesita: `schema.sql` ya contiene todos estos cambios.

- `migraciones/`: pasos que fueron actualizando una base ya existente, versión por versión.
- `pruebas/`: controles de cada versión, sobre una copia local de la base.
- `utilidades/`: scripts de carga y mantenimiento que se usaron una sola vez.

Se conservan como registro del desarrollo. Se corren desde la raíz del repositorio, por ejemplo
`python superados/migraciones/migracion_tanda21.py`, y siempre con un respaldo previo.
