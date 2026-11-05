"""
Hace las MISMAS ventas desde la pantalla de Registrar Venta (vieja o nueva) con AppTest
y guarda lo que quedó grabado, para comparar versión vieja contra nueva.
Uso: python probar_venta_ui.py <carpeta_codigo> <base> <salida.pkl> <vieja|nueva>
"""
import os
import pickle
import sys

carpeta, base, salida, version = os.path.abspath(sys.argv[1]), sys.argv[2], os.path.abspath(sys.argv[3]), sys.argv[4]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
config.usar_base(base)
sys.path.insert(0, carpeta)
os.chdir(carpeta)
import datetime as dt  # noqa: E402

import pandas as pd  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

c = config.conectar(base)
c.autocommit = True
cur = c.cursor()
cur.execute("SELECT id FROM personas WHERE rol = 'atencion' AND activo ORDER BY id LIMIT 1")
CAJERO = cur.fetchone()[0]
cur.execute("SELECT COALESCE(MAX(id),0) FROM ventas"); VENTA0 = cur.fetchone()[0]
cur.execute("SELECT COALESCE(MAX(id),0) FROM gastos"); GASTO0 = cur.fetchone()[0]
cur.execute("SELECT COALESCE(MAX(id),0) FROM ventas_ticket"); TICKET0 = cur.fetchone()[0]

at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=180)
at.session_state["persona_id"] = CAJERO
at.session_state["pagina_nav"] = "💵 Registrar Venta"
at.run()
errores = []


def chequear(paso):
    if at.exception:
        errores.append(f"{paso}: {at.exception[0].value.splitlines()[0][:150]}")


def buscar(texto):
    at.text_input(key="busqueda_venta_rapida").set_value(texto)
    at.run(); chequear(f"buscar {texto}")


def boton(key):
    return [b for b in at.button if b.key == key][0]


def agregar_unidades(aid, nombre, n):
    buscar(nombre)
    at.number_input(key=f"unid_{aid}").set_value(n); at.run()
    boton(f"add_{aid}").click(); at.run(); chequear(f"agregar {aid}")


def agregar_gramos(aid, nombre, g):
    buscar(nombre)
    at.number_input(key=f"gramos_{aid}").set_value(g); at.run()
    boton(f"add_{aid}").click(); at.run(); chequear(f"agregar kg {aid}")


def agregar_fraccion(aid, nombre, g):
    buscar(nombre)
    if version == "vieja":
        boton(f"btn_fraccion_{aid}").click(); at.run()
    else:
        [s for s in at.button_group if s.key == f"modo_venta_{aid}"][0].set_value("fraccion"); at.run()
    at.number_input(key=f"gramos_frac_{aid}").set_value(g); at.run()
    boton(f"add_{aid}").click(); at.run(); chequear(f"agregar fracción {aid}")


def elegir_medio(medio):
    if version == "vieja":
        at.selectbox(key="medio_carrito").set_value(medio)
    else:
        [s for s in at.button_group if s.key == "medio_carrito"][0].set_value(medio)
    at.run()


def confirmar():
    etiqueta = [b for b in at.button if "confirmar" in b.label.lower()][0]
    etiqueta.click(); at.run(); chequear("confirmar")
    return [s.value for s in at.success]


# 1) Venta en efectivo: unidad + kg + fracción de bolsa + combo virtual
agregar_unidades(3, "ACEITE GIRASOL CAÑUELAS 900CC", 2)
agregar_gramos(163, "QUESO CREMOSO LA PAMPA", 250)
agregar_fraccion(530, "CAFE MOLIDO SUPER CABRALES", 150)
agregar_unidades(491, "PROMO FERNET", 1)
carrito_1 = [(it["articulo_id"], round(it["cantidad"], 4), it["precio_unitario"]) for it in at.session_state["carrito_venta"]]
elegir_medio("Efectivo")
ok1 = confirmar()

# 2) Venta con tarjeta de débito
agregar_unidades(112, "MAYONESA NATURA 118", 3)
elegir_medio("Tarjeta débito")
ok2 = confirmar()

# 3) Consumo interno
agregar_unidades(244, "ARROZ LARGO FINO", 1)
at.checkbox(key="chk_consumo_interno").check(); at.run()
ok3 = confirmar()
if any(x.key == "chk_consumo_interno" for x in at.checkbox):
    at.checkbox(key="chk_consumo_interno").uncheck(); at.run()

# 4) Venta cargada con otra fecha (atrasada)
agregar_unidades(309, "HARINA 0000", 2)
at.date_input(key="fecha_carrito").set_value(dt.date(2026, 10, 19)); at.run()
elegir_medio("Transferencia / MercadoPago")
ok4 = confirmar()

q = lambda sql: pd.read_sql(sql, c)  # noqa: E731
resultado = {
    "errores": errores,
    "carrito_1": carrito_1,
    "mensajes": [ok1, ok2, ok3, ok4],
    "ventas": q(f"""SELECT articulo_id, ticket_id - {TICKET0} AS ticket, fecha, cantidad, precio_unitario, monto_total,
                          medio_pago, es_consumo_interno, costo_unitario, precio_lista, registrado_por
                   FROM ventas WHERE id > {VENTA0} ORDER BY id"""),
    "tickets": q(f"SELECT id - {TICKET0} AS ticket, fecha, medio_pago FROM ventas_ticket WHERE id > {TICKET0} ORDER BY id"),
    "gastos": q(f"SELECT fecha, categoria, monto, descripcion FROM gastos WHERE id > {GASTO0} ORDER BY id"),
    "stock": q("SELECT id, stock_actual FROM articulos WHERE id IN (3,163,530,112,244,309) "
               "OR id IN (SELECT componente_articulo_id FROM promocion_componentes WHERE promocion_articulo_id = 491) "
               "ORDER BY id"),
    "historial": q("SELECT tabla, accion, detalle, usuario FROM historial_cambios "
                   "WHERE fecha > now() - interval '1 hour' ORDER BY id"),
}
pickle.dump(resultado, open(salida, "wb"))
print(f"{version}: errores={errores}")
print(f"{version}: ventas grabadas={len(resultado['ventas'])}, tickets={len(resultado['tickets'])}, "
      f"gastos={len(resultado['gastos'])}")
print(f"{version}: mensajes de confirmación vistos={[bool(m) for m in resultado['mensajes']]}")
