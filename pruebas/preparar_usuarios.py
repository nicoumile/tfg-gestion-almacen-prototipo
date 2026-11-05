"""
Deja en la base de prueba los usuarios que usan las pruebas en navegador:
"dueno" (dueño) y "atencion" (atención, solo Registrar Venta), los dos con la
contraseña PRUEBAS_CLAVE (prueba1234). Solo para bases locales de prueba.

Uso: python preparar_usuarios.py <base>
"""
import os
import sys

base = sys.argv[1]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

config.usar_base(base)
import db  # noqa: E402

clave = db.hash_clave(os.environ.get("PRUEBAS_CLAVE", "prueba1234"))
c = config.conectar(base)
with c, c.cursor() as cur:
    cur.execute("UPDATE personas SET clave_hash = %s WHERE usuario = 'dueno'", (clave,))
    if cur.rowcount == 0:
        cur.execute("""INSERT INTO personas (nombre, usuario, clave_hash, rol, permisos)
                       VALUES ('Dueño', 'dueno', %s, 'dueño', ARRAY['registrar_venta'])""", (clave,))
    cur.execute("UPDATE personas SET clave_hash = %s, activo = true, permisos = ARRAY['registrar_venta'] "
                "WHERE usuario = 'atencion'", (clave,))
    if cur.rowcount == 0:
        cur.execute("""INSERT INTO personas (nombre, usuario, clave_hash, rol, permisos)
                       VALUES ('L. Gutiérrez', 'atencion', %s, 'atencion', ARRAY['registrar_venta'])""", (clave,))
c.close()
print(f"{base}: usuarios dueno y atencion listos.")
