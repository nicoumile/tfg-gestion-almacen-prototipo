"""
Abre cada pantalla de la app (como dueño y como atención) con AppTest de
Streamlit contra una copia de producción y registra si alguna tira error.
Uso: python pantallas.py <carpeta_codigo> <base>
"""
import os
import sys

carpeta, base = os.path.abspath(sys.argv[1]), sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
config.usar_base(base)
sys.path.insert(0, carpeta)
os.chdir(carpeta)

from streamlit.testing.v1 import AppTest  # noqa: E402

# usuario de atención para la prueba (si no existe)
c = config.conectar(base)
c.autocommit = True
cur = c.cursor()
cur.execute("""INSERT INTO personas (nombre, usuario, rol, permisos)
               SELECT 'Prueba', 'prueba', 'atencion', ARRAY['registrar_venta','cierre_caja']
               WHERE NOT EXISTS (SELECT 1 FROM personas WHERE usuario = 'prueba')""")
cur.execute("SELECT id FROM personas WHERE rol = 'dueño' ORDER BY id LIMIT 1")
dueno = cur.fetchone()[0]
cur.execute("SELECT id FROM personas WHERE usuario = 'prueba'")
atencion = cur.fetchone()[0]
c.close()

PANTALLAS = ["📊 Dashboard", "📦 Artículos", "🧾 Cargar Factura (Compra)", "⚖️ Fraccionar", "🎁 Promociones",
             "💵 Registrar Venta", "💸 Gastos", "📈 Reportes", "🧮 Cierre de Caja", "🛒 Lista de compras",
             "🔮 Pronóstico de demanda", "🏭 Proveedores", "🏫 Desafío Recreo", "📜 Historial de Cambios", "⚙️ Configuración"]

fallas = 0
for rol, pid, pantallas in (("dueño", dueno, PANTALLAS), ("atención", atencion, ["💵 Registrar Venta", "🧮 Cierre de Caja"])):
    for p in pantallas:
        at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=180)
        at.session_state["persona_id"] = pid
        at.session_state["pagina_nav"] = p
        try:
            at.run()
            errores = [e.value for e in at.exception]
        except Exception as e:  # timeout u otro error del runner
            errores = [f"{type(e).__name__}: {e}"]
        titulo = at.title[0].value if len(at.title) else "(sin título)"
        estado = "OK " if not errores else "ERR"
        fallas += bool(errores)
        print(f"{estado} [{rol}] {p} -> título '{titulo}'" + ("" if not errores else f"  {errores[0][:200]}"))

# El selector de atención no debe ofrecer pantallas no habilitadas
at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=180)
at.session_state["persona_id"] = atencion
at.run()
opciones = list(at.sidebar.radio[0].options) if len(at.sidebar.radio) else []
print("Menú de atención:", opciones)
sys.exit(1 if fallas else 0)
