"""
Control de la tanda 13: en Artículos -> editar, elegir un artículo por unidad y
después uno por kg; el selector tiene que mostrar kg y guardar sin tocar nada no
puede cambiar la unidad (error del 21/10/2026: Cremoso y Tybo quedaron "por unidad").
ESCRIBE en la base (guarda el artículo): usar solo una copia local.

Uso: python pruebas/controles_tanda13.py <carpeta_codigo> <base>
"""
import os, sys
carpeta, base = os.path.abspath(sys.argv[1]), sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
config.usar_base(base)
sys.path.insert(0, carpeta); os.chdir(carpeta)
from sqlalchemy import text
from streamlit.testing.v1 import AppTest
import db
def unidad(i):
    with db.engine.connect() as c:
        return c.execute(text("SELECT unidad_medida FROM articulos WHERE id=:i"), {"i": i}).scalar()
fallas = 0
def ok(n, c, d=""):
    global fallas; fallas += not c; print(("OK    " if c else "FALLA ") + n + (f"  -- {d}" if d else ""))
UNI, KG = 183, 98   # SALCHICHAS PALADINI X6 (unidad), JAMON COCIDO (kg)
at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=180)
at.session_state["persona_id"] = 1
at.session_state["pagina_nav"] = "📦 Artículos"
at.run()
at.selectbox(key="sel_editar_articulo").set_value(UNI).run()
r = [x for x in at.radio if x.label == "Unidad de medida" and x.key and x.key.startswith("unidad_editar")]
ok("artículo por unidad: el selector dice unidad", r and r[0].value == "unidad", r[0].value if r else "sin radio")
at.selectbox(key="sel_editar_articulo").set_value(KG).run()
r = [x for x in at.radio if x.label == "Unidad de medida" and x.key and x.key.startswith("unidad_editar")]
ok("después, artículo por kg: el selector dice kg", r and r[0].value == "kg", r[0].value if r else "sin radio")
# guardar el formulario sin tocar nada
b = [x for x in at.button if x.label == "Guardar cambios"]
b[0].click().run()
ok("guardar sin tocar nada no cambia la unidad del jamón", unidad(KG) == "kg", f"quedó en {unidad(KG)}")
sys.exit(1 if fallas else 0)
