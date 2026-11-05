"""
Controles de la tanda 17 (protocolo de validación): botón "Me voy al mayorista con esta
lista", registro de viajes y comparación lista contra facturas, con AppTest.
La base tiene que tener migracion_tanda17.py aplicada. ESCRIBE en la base (borra y crea
listas y viajes): usar solo una copia local.

Uso: python pruebas/controles_tanda17.py <carpeta_codigo> <base>
"""
import os, sys
carpeta, base = os.path.abspath(sys.argv[1]), sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
config.usar_base(base)
sys.path.insert(0, carpeta); os.chdir(carpeta)
from sqlalchemy import text  # noqa
from streamlit.testing.v1 import AppTest  # noqa
import db  # noqa
fallas = 0
def ok(n, c, d=""):
    global fallas; fallas += not c; print(("OK    " if c else "FALLA ") + n + (f"  -- {d}" if d else ""))
def q(sql):
    with db.engine.connect() as c: return c.execute(text(sql)).all()

with db.engine.begin() as c:
    c.execute(text("TRUNCATE viajes_compra, listas_compra_detalle, listas_compra RESTART IDENTITY CASCADE"))


def abrir():
    at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=300)
    at.session_state["persona_id"] = 1
    at.session_state["pagina_nav"] = "🛒 Lista de compras"
    at.run(); return at

at = abrir()
ok("abre sin errores", not at.exception, str([e.value[:300] for e in at.exception]))
b = [x for x in at.button if x.key == "lc_guardar_lista"]
ok("está el botón 'Me voy al mayorista con esta lista'", len(b) == 1)
b[0].click().run()
ok("guardar no tira error", not at.exception, str([e.value[:300] for e in at.exception]))
r = q("SELECT id, fecha, renglones FROM listas_compra ORDER BY id DESC LIMIT 1")[0]
det = q(f"SELECT count(*), sum(CASE WHEN incluido THEN 1 ELSE 0 END), sum(CASE WHEN sugerido<>cantidad THEN 1 ELSE 0 END) FROM listas_compra_detalle WHERE lista_id={r[0]}")[0]
ok("lista guardada", r[2] > 0, f"#{r[0]} {r[1]} renglones {r[2]}")
ok("detalle: todos los sugeridos, todos incluidos, sin cambios", det[0] > 0 and det[1] == det[0] and det[2] == 0, str(det))
ok("aparece el aviso 'Ya guardaste la lista de hoy'", any("Ya guardaste la lista de hoy" in c.value for c in at.caption))
# guardar otra vez: reemplaza
[x for x in at.button if x.key == "lc_guardar_lista"][0].click().run()
ok("guardar de nuevo reemplaza (sigue habiendo una lista ese día)",
   q(f"SELECT count(*) FROM listas_compra WHERE fecha = '{r[1]}'")[0][0] == 1)
# registrar viaje desde el formulario
at = abrir()
at.number_input(key="viaje_horas").set_value(3.5)
at.radio(key="viaje_tipo").set_value("Planificado")
at.text_input(key="viaje_nota").set_value("prueba UI")
[x for x in at.button if "Guardar viaje" in x.label][0].click().run()
ok("guardar viaje sin errores", not at.exception, str([e.value[:300] for e in at.exception]))
v = q("SELECT fecha, tipo, horas, lista_id, nota FROM viajes_compra ORDER BY id DESC LIMIT 1")[0]
ok("viaje con horas, tipo y la lista de hoy por defecto", v[1] == "planificado" and float(v[2]) == 3.5 and v[3] == r[0], str(v))
at = abrir()
tablas = [d.value for d in at.dataframe]
ok("se ve la tabla de viajes y la de listas", any("Tipo" in t.columns for t in tablas) and any("Se compró" in t.columns for t in tablas))
lst = [t for t in tablas if "Se compró" in t.columns]
print(lst[0].to_string() if lst else "")

# comparación lista contra compras (función pura): un caso de cada estado
import pandas as pd  # noqa
import lista_compras as lc  # noqa
det = pd.DataFrame({"articulo_id": [1, 2, 3, 4, 5], "nombre": list("abcde"), "unidad_medida": ["unidad"] * 4 + ["kg"],
                    "sugerido": [10, 10, 10, 10, 2.0], "cantidad": [10, 10, 10, 0, 2.0],
                    "incluido": [True, True, True, False, True], "costo_c_iva": [1.0] * 5})
compras = pd.DataFrame({"articulo_id": [1, 2, 4, 5, 9, 10], "nombre": list("abdeij"), "unidad_medida": ["unidad"] * 6,
                        "comprado": [11, 20, 3, 2.15, 1, 1], "monto_c_iva": [1.0] * 6})
d, res = lc.comparar_lista(det, compras, excluir={10})
esperado = {1: "Comprado igual", 2: "Comprado, otra cantidad", 3: "No se compró", 4: "Sacado, pero se compró",
            5: "Comprado igual", 9: "Agregado (no estaba en la lista)"}
ok("estados de la comparación", dict(zip(d["articulo_id"], d["estado"])) == esperado, str(dict(zip(d["articulo_id"], d["estado"]))))
ok("resumen: 4 llevados, 3 comprados (75 %), 1 agregado (el excluido no cuenta)",
   res["llevados"] == 4 and res["comprados_de_la_lista"] == 3 and res["agregados"] == 1, str(res))
import datetime as dt  # noqa
v = lc.ventanas_listas(pd.DataFrame({"id": [1, 2], "fecha": [dt.date(2026, 10, 15), dt.date(2026, 10, 18)]}), 6)
ok("ventana: hasta el día anterior a la lista siguiente, o 6 días", list(v["hasta"]) == [dt.date(2026, 10, 17), dt.date(2026, 10, 24)])
sys.exit(1 if fallas else 0)
