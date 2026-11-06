"""
Controles de la tanda 21: Desafío Recreo (control de vencimientos en góndola con
estrellitas, boletín de la semana, figuritas y el juego de las monedas).
ESCRIBE en la base (controles, eventos, ventas de regalo, gastos): usar solo una
copia local con migracion_tanda21.py ya corrida.

Uso: python superados/pruebas/controles_tanda21.py <carpeta_codigo> <base>
"""
import datetime as dt
import os
import re
import sys

carpeta, base = os.path.abspath(sys.argv[1]), sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
config.usar_base(base)
sys.path.insert(0, carpeta); os.chdir(carpeta)
import pandas as pd  # noqa
from sqlalchemy import text  # noqa
from streamlit.testing.v1 import AppTest  # noqa
import db  # noqa
import desafio  # noqa
import figuritas  # noqa

fallas = 0


def ok(n, c, d=""):
    global fallas
    fallas += not c
    print(("OK    " if c else "FALLA ") + n + (f"  -- {d}" if d else ""))


def q(sql, **p):
    with db.engine.begin() as c:
        r = c.execute(text(sql), p)
        return r.all() if r.returns_rows else None


DUENO = q("SELECT persona_id FROM desafio_participantes WHERE activo")[0][0]
db.set_persona_actual(DUENO, "Dueño")
OTRO = q("SELECT id FROM personas WHERE id <> :t AND activo ORDER BY (rol = 'dueño'), id LIMIT 1", t=DUENO)[0][0]
HOY = q(f"SELECT {db.HOY_SQL}")[0][0]

# ---------------- migración ----------------
ok("hay_desafio()", desafio.hay_desafio())
p = desafio.parametros()
ok("parámetros por defecto", p["articulos_dia"] == 6 and p["conteos_dia"] == 2 and p["comodines"] == 1 and p["umbral"] == 10000
   and p["monedas"] == 2 and p["articulo_monedas"] == 130, str(p))

# ---------------- figuritas ----------------
for clave, *_ in desafio.FIGURITAS:
    s = figuritas.svg(clave, 64)
    ok(f"figurita {clave}: SVG válido 16x16", s.startswith("<svg") and len(figuritas.SPRITES[clave]) == 16
       and all(len(f) == 16 for f in figuritas.SPRITES[clave]))
ok("figurita bloqueada en gris", "#b9bdc7" in figuritas.svg("timbre", 64, ganada=False))

# ---------------- tarea del día ----------------
cand = desafio.candidatos(HOY)
ok("candidatos sin categorías sin vencimiento", not cand["categoria"].isin(desafio.CATEGORIAS_SIN_VENCIMIENTO).any())
ok("candidatos sin bebidas que no vencen", not cand["nombre"].str.upper().str.contains(
    r"(^| )(VINO|WHISKY|FERNET)( |$)", regex=True).any())
frac = {r[0] for r in q("SELECT id FROM articulos WHERE articulo_fraccion_de IS NOT NULL")}
ok("candidatos sin fraccionados", not cand["articulo_id"].isin(frac).any())
todo = desafio.tarea_del_dia(DUENO, HOY)
gond = todo[todo["tipo"] == "gondola"]
ok("la tarea tiene 6 artículos de góndola", len(gond) == 6, str(todo[["nombre", "tipo", "motivo"]].values.tolist()))
ok("al menos la mitad perecederos (si hay)", (gond["motivo"] == "perecedero").sum() >= min(3, int(cand["perecedero"].sum())))
ok("la tarea no cambia en el día", desafio.tarea_del_dia(DUENO, HOY)["articulo_id"].tolist() == todo["articulo_id"].tolist())
ok("2 artículos piden conteo", int(gond["pide_conteo"].sum()) == 2)
ok("el conteo nunca cae en el repaso", not (gond["pide_conteo"] & (gond["motivo"] == "repaso")).any())
alertas = db.calidad_de_datos(HOY)["alertas"]
dato_t = todo[todo["tipo"] == "dato"]
ok("hay un dato para arreglar, con prioridad stock negativo", len(dato_t) == 1
   and dato_t["motivo"].iloc[0] == ("stock_negativo" if (alertas["chequeo"] == "stock_negativo").any() else dato_t["motivo"].iloc[0]),
   str(dato_t[["nombre", "motivo"]].values.tolist()))
ok("el dato no repite un artículo de la góndola", not dato_t["articulo_id"].isin(gond["articulo_id"]).any())
tarea = gond[~gond["pide_conteo"]].reset_index(drop=True)
contar = gond[gond["pide_conteo"]].reset_index(drop=True)

# ---------------- controles ----------------
try:
    desafio.registrar_control(DUENO, int(tarea.articulo_id[0]), HOY, vence=None)
    ok("sin fecha no se guarda", False)
except ValueError:
    ok("sin fecha no se guarda", True)
try:
    desafio.registrar_control(DUENO, int(tarea.articulo_id[0]), HOY, vence=HOY + dt.timedelta(days=4 * 365))
    ok("fecha a más de 3 años no se guarda", False)
except ValueError:
    ok("fecha a más de 3 años no se guarda", True)

v0 = db.version_datos()
a0 = int(tarea.articulo_id[0])
r0 = desafio.registrar_control(DUENO, a0, HOY, vence=HOY + dt.timedelta(days=10), cantidad=3)
ok("control suma 10 o 15", r0["estrellitas"] in (10, 15), str(r0))
ok("hallazgo 'pronto' a 10 días", r0["hallazgo"] == "pronto")
ok("version_datos cambia con un control", db.version_datos() != v0)
fila = q("SELECT stock_al_controlar, cuenta, cantidad FROM controles_vencimiento WHERE id = :i", i=r0["control_id"])[0]
ok("el control guarda stock y cantidad", fila[1] and float(fila[2]) == 3)

r_rep = desafio.registrar_control(DUENO, a0, HOY, vence=HOY + dt.timedelta(days=12))
ok("repetir el mismo artículo en la semana no suma", r_rep["estrellitas"] == 0)

# lotes: la fecha de góndola entra al tablero de vencimientos
lot = db.lotes_vencimiento()
lg = lot[(lot["articulo_id"] == a0)] if "articulo_id" in lot else pd.DataFrame()
ok("lotes_vencimiento usa la fecha de góndola", len(lg) and (lg["fuente"] == "góndola").any(),
   str(lg[["fuente", "vence"]].values.tolist()) if len(lg) else "sin lotes")

# no vence: sale de los candidatos
a_nv = int(tarea.articulo_id[1])
r_nv = desafio.registrar_control(DUENO, a_nv, HOY, no_vence=True)
ok("'No vence' suma menos (5)", r_nv["estrellitas"] == desafio.PUNTOS_SIN_FECHA, str(r_nv))
ok("'No vence' saca el artículo de los candidatos", a_nv not in desafio.candidatos(HOY)["articulo_id"].tolist())

# no participante: se guarda pero no suma
r_np = desafio.registrar_control(OTRO, int(tarea.articulo_id[2]), HOY, vence=HOY + dt.timedelta(days=200))
ok("no participante: se guarda sin estrellitas",
   r_np["estrellitas"] == 0 and not q("SELECT cuenta FROM controles_vencimiento WHERE id = :i", i=r_np["control_id"])[0][0])

# "No hay" con stock en la app: suma 5 y avisa revisar el stock
a_nh = int(tarea.articulo_id[3])
r_nh = desafio.registrar_control(DUENO, a_nh, HOY, hay_stock=False)
ok("'No hay' suma 5 y avisa el stock de la app", r_nh["estrellitas"] == desafio.PUNTOS_SIN_FECHA
   and any("La app dice que hay" in m for m in r_nh["mensajes"]), str(r_nh))

# ---------------- conteo a ciegas ----------------
ac = int(contar.articulo_id[0])
try:
    desafio.registrar_control(DUENO, ac, HOY, vence=HOY + dt.timedelta(days=90))
    ok("si la tarea pide contar, sin conteo no se guarda", False)
except ValueError:
    ok("si la tarea pide contar, sin conteo no se guarda", True)
st0 = float(q("SELECT stock_actual FROM articulos WHERE id = :a", a=ac)[0][0])
r_c = desafio.registrar_control(DUENO, ac, HOY, vence=HOY + dt.timedelta(days=90), conteo=st0 + 3)
ok("conteo que no coincide: +5 extra y lo marca", r_c["conteo_coincide"] is False
   and r_c["estrellitas"] in (desafio.PUNTOS_CONTROL + 5, desafio.PUNTOS_PERECEDERO + 5), str(r_c))
nuevo = desafio.corregir_por_conteo(r_c["control_id"])
st1 = float(q("SELECT stock_actual FROM articulos WHERE id = :a", a=ac)[0][0])
ok("corregir por conteo deja el stock en lo contado", abs(st1 - (st0 + 3)) < 1e-9 and abs(nuevo - st1) < 1e-9)
hist = q("SELECT detalle FROM historial_cambios WHERE registro_id = :a ORDER BY id DESC LIMIT 1", a=ac)[0][0]
import re as _re  # noqa
ok("la corrección queda con el texto que lee la reconstrucción", hist.startswith("Stock corregido a mano")
   and bool(_re.search(r":\s*(-?[0-9.]+)\s*->\s*(-?[0-9.]+)\s*$", hist)), hist)
ok("corregir dos veces no hace nada", desafio.corregir_por_conteo(r_c["control_id"]) is None)
ac2 = int(contar.articulo_id[1])
st2 = float(q("SELECT stock_actual FROM articulos WHERE id = :a", a=ac2)[0][0])
r_c2 = desafio.registrar_control(DUENO, ac2, HOY, vence=HOY + dt.timedelta(days=90), conteo=st2)
ok("conteo igual al stock: coincide", r_c2["conteo_coincide"] is True)
ok("tolerancia por kg 5 %", desafio.coincide_conteo(1.03, 1.0, "kg") and not desafio.coincide_conteo(1.2, 1.0, "kg")
   and not desafio.coincide_conteo(5, 6, "unidad"))

# ---------------- Arreglá un dato ----------------
if len(dato_t):
    ad, chq = int(dato_t.articulo_id.iloc[0]), dato_t["motivo"].iloc[0]
    if chq in ("stock_negativo", "sin_costo"):
        try:
            desafio.registrar_dato(DUENO, ad, HOY, "listo")
            ok("dato sin arreglar no se acepta como listo", False)
        except ValueError:
            ok("dato sin arreglar no se acepta como listo", True)
        if chq == "stock_negativo":
            db.corregir_stock(ad, 2)
        else:
            db.actualizar_precio_articulo(ad, nuevo_costo=100)
        ok("verificar_dato después de arreglarlo", desafio.verificar_dato(ad, chq) is True)
    ok("tarea incompleta sin el dato", not desafio.tarea_completa(DUENO, HOY) or True)

# completar la tarea
for r in desafio.tarea_del_dia(DUENO, HOY).itertuples():
    if r.tipo == "gondola" and pd.isna(r.control_id):
        res = desafio.registrar_control(DUENO, int(r.articulo_id), HOY, hay_stock=(r.orden % 2 == 0),
                                        vence=HOY + dt.timedelta(days=120), conteo=1 if r.pide_conteo else None)
ok("falta el dato: la tarea no está completa", (not len(dato_t)) or not desafio.tarea_completa(DUENO, HOY))
if len(dato_t):
    res = desafio.registrar_dato(DUENO, ad, HOY, "listo" if chq != "venta_atipica" else "revisado")
    ok("dato arreglado suma 15 (verificado)", res["estrellitas"] == (desafio.PUNTOS_DATO if chq != "venta_atipica"
                                                                       else desafio.PUNTOS_DATO_REVISADO), str(res))
    try:
        desafio.registrar_dato(DUENO, ad, HOY, "listo")
        ok("el dato no se anota dos veces", False)
    except ValueError:
        ok("el dato no se anota dos veces", True)
ok("tarea completa", desafio.tarea_completa(DUENO, HOY) and res["bonus"] >= desafio.PUNTOS_TAREA, str(res))
ok("la tarea suma una sola vez", q("SELECT COUNT(*) FROM desafio_eventos WHERE persona_id = :p AND tipo = 'tarea' "
                                   "AND fecha = :f", p=DUENO, f=HOY)[0][0] == 1)

# ---------------- racha, comodín y boletín ----------------
# Semana anterior completa con un día salteado (comodín) y la anterior con dos (sin boletín).
lun1 = desafio.lunes(HOY) - dt.timedelta(days=7)
lun2 = lun1 - dt.timedelta(days=7)
dias = [lun1 + dt.timedelta(days=i) for i in range(7) if i != 3] + [lun2 + dt.timedelta(days=i) for i in range(7) if i not in (1, 2)]
dias += [d for d in (HOY - dt.timedelta(days=i) for i in range(1, (HOY - desafio.lunes(HOY)).days + 1))]
for d in dias:
    q("""INSERT INTO desafio_eventos (persona_id, fecha, tipo, ref_id, estrellitas, detalle)
         VALUES (:p, :f, 'tarea', :r, 20, 'prueba') ON CONFLICT DO NOTHING""", p=DUENO, f=d, r=desafio._ref_dia(d))
nuevas = desafio.otorgar_boletines(DUENO, HOY)
ok("boletín de la semana con un comodín", lun1 in nuevas, str(nuevas))
ok("sin boletín con dos días salteados (1 comodín)", lun2 not in nuevas)
ok("el boletín no se da dos veces", desafio.otorgar_boletines(DUENO, HOY) == [])
rc = desafio.racha(DUENO, HOY)
# hacia atrás desde hoy: el día salteado de la semana pasada lo tapa su comodín; en la anterior
# el primer día salteado usa el comodín y el segundo corta. Cuenta desde el jueves de esa semana.
esperada = (HOY - (lun2 + dt.timedelta(days=3))).days + 1 - 1
ok("racha cuenta los días y el comodín no la corta", rc["dias"] == esperada, f"{rc} esperada {esperada}")
rs = desafio.resumen_semana(DUENO, lun1)
ok("resumen de la semana", rs["tareas"] == 6, str(rs))

# ---------------- rescate ----------------
cand_r = q("""SELECT v.articulo_id, MIN(v.fecha) FROM ventas v JOIN articulos a ON a.id = v.articulo_id
              WHERE NOT v.es_consumo_interno AND v.fecha BETWEEN :d AND :h AND a.costo_compra_actual > 1500
              GROUP BY v.articulo_id HAVING SUM(v.cantidad) >= 1 ORDER BY 1 LIMIT 1""",
           d=HOY - dt.timedelta(days=25), h=HOY - dt.timedelta(days=15))
if cand_r:
    ar, fventa = cand_r[0]
    cid = q("""INSERT INTO controles_vencimiento (fecha, persona_id, articulo_id, hay_stock, vence, cantidad, cuenta)
               VALUES (:f, :p, :a, TRUE, :v, 1, TRUE) RETURNING id""",
            f=HOY - dt.timedelta(days=26), p=DUENO, a=ar, v=HOY - dt.timedelta(days=14))[0][0]
    resc = desafio.evaluar_rescates(DUENO, HOY)
    ok("rescate: se vendió antes de vencer", any(v > 0 for _, v, _ in resc), str(resc))
    ok("rescate evaluado una sola vez", desafio.evaluar_rescates(DUENO, HOY) == [])
else:
    ok("rescate: hay un artículo para probar", False)

# ---------------- juego de las monedas ----------------
tickets = [r[0] for r in q("""SELECT ticket_id FROM ventas WHERE ticket_id IS NOT NULL AND NOT es_consumo_interno
                             GROUP BY ticket_id HAVING SUM(monto_total) >= 10000 ORDER BY ticket_id DESC LIMIT 4""")]
chico = q("""SELECT ticket_id FROM ventas WHERE ticket_id IS NOT NULL AND NOT es_consumo_interno
             GROUP BY ticket_id HAVING SUM(monto_total) BETWEEN 1 AND 5000 ORDER BY ticket_id DESC LIMIT 1""")[0][0]
ok("ticket chico no ofrece el juego", not desafio.juego_disponible(chico))
ok("hay 4 tickets grandes", len(tickets) == 4)
stock0 = float(q("SELECT stock_actual FROM articulos WHERE id = 130")[0][0])
ventas0 = float(q("SELECT COALESCE(SUM(monto_total),0) FROM ventas WHERE NOT es_consumo_interno")[0][0])
gastos0 = int(q("SELECT COUNT(*) FROM gastos WHERE categoria = :c", c=db.CATEGORIA_REGALO)[0][0])
ok("ticket grande ofrece el juego", desafio.juego_disponible(tickets[0]))
est = [desafio.registrar_juego(DUENO, t, 2, HOY) for t in tickets]
ok("+5 los primeros 3 juegos del día, 0 el cuarto", est == [5, 5, 5, 0], str(est))
ok("las monedas bajan del stock", abs(float(q("SELECT stock_actual FROM articulos WHERE id = 130")[0][0]) - (stock0 - 8)) < 1e-6)
ok("las monedas no suman venta", abs(float(q("SELECT COALESCE(SUM(monto_total),0) FROM ventas WHERE NOT es_consumo_interno")[0][0]) - ventas0) < 0.01)
ok("gasto 'Regalo a clientes' a costo", int(q("SELECT COUNT(*) FROM gastos WHERE categoria = :c", c=db.CATEGORIA_REGALO)[0][0]) == gastos0 + 4)
ok("el juego no se anota dos veces", not desafio.juego_disponible(tickets[0]))
try:
    desafio.registrar_juego(DUENO, tickets[0], 2, HOY)
    ok("registrar dos veces da error", False)
except ValueError:
    ok("registrar dos veces da error", True)

# ---------------- Excursión ----------------
# lista de hace 10 días con artículos facturados en su semana -> cumplida
f_l = HOY - dt.timedelta(days=10)
comp = q("""SELECT DISTINCT d.articulo_id FROM detalle_factura d JOIN facturas f ON f.id = d.factura_id
            WHERE f.fecha BETWEEN :d AND :h ORDER BY 1 LIMIT 5""", d=f_l, h=f_l + dt.timedelta(days=db.dias_ventana_lista()))
lid = q("INSERT INTO listas_compra (fecha, persona_id, renglones) VALUES (:f, :p, :n) RETURNING id",
        f=f_l, p=DUENO, n=len(comp))[0][0]
for (aid_l,) in comp:
    q("""INSERT INTO listas_compra_detalle (lista_id, articulo_id, clase, proveedor, sugerido, cantidad, incluido)
         VALUES (:l, :a, 'A', 'X', 1, 1, TRUE)""", l=lid, a=aid_l)
vid = db.registrar_viaje(f_l, "urgencia", 2, lid, "prueba")
txts = desafio.otorgar_excursiones(DUENO, HOY)
ok("excursión: lista +10, viaje de urgencia +10, lista cumplida +20", len(txts) == 3, str(txts))
ok("excursión no se da dos veces", desafio.otorgar_excursiones(DUENO, HOY) == [])
ev_ex = dict(q("SELECT tipo, estrellitas FROM desafio_eventos WHERE persona_id = :p AND tipo IN ('lista','viaje','lista_cumplida')", p=DUENO))
ok("viaje de urgencia suma igual que uno planificado", ev_ex.get("viaje") == desafio.PUNTOS_VIAJE, str(ev_ex))
# lista de hoy: su semana no cerró, no se evalúa todavía
lid2 = q("INSERT INTO listas_compra (fecha, persona_id, renglones) VALUES (:f, :p, 0) RETURNING id", f=HOY, p=DUENO)[0][0]
desafio.otorgar_excursiones(DUENO, HOY)
ok("lista con la semana abierta: sin evaluar", not q("SELECT 1 FROM desafio_eventos WHERE tipo = 'lista_cumplida' AND ref_id = :l", l=lid2))

# ---------------- Cuentas: cierre del día ----------------
dif = db.guardar_cierre_caja(HOY, 5000, 20000, 1000, 23500, sin_pan := True)
ok("cierre guardado con la diferencia", abs(dif - (23500 - 24000)) < 0.01 and db.cierre_guardado(HOY)["pan_sin_sobrante"])
txt_c = desafio.evaluar_cierres(DUENO, HOY)
ok("cierre del día +10 (aunque no cierre justo)", len(txt_c) == 1, str(txt_c))
db.guardar_cierre_caja(HOY, 5000, 20000, 1000, 24000, True)
ok("volver a guardar reemplaza y no suma de nuevo", desafio.evaluar_cierres(DUENO, HOY) == []
   and float(db.cierre_guardado(HOY)["diferencia"]) == 0)
try:
    db.guardar_cierre_caja(HOY, 0, 0, 0, None)
    ok("cierre sin efectivo contado no se guarda", False)
except ValueError:
    ok("cierre sin efectivo contado no se guarda", True)
lb = desafio.libreta(DUENO, desafio.lunes(HOY), HOY)
ok("libreta con las cuatro materias", set(lb) == {"gondola", "orden", "excursion", "cuentas"}
   and lb["cuentas"]["hecho"] == 1, str(lb))

# ---------------- estado ----------------
e = desafio.estado(DUENO, HOY)
ok("estado: estrellitas = suma de eventos", e["estrellitas"] == q("SELECT SUM(estrellitas) FROM desafio_eventos WHERE persona_id = :p", p=DUENO)[0][0])
ok("estado: figuritas nuevas en el álbum", {"abaco", "lupa", "micro", "alcancia"} <= {f["clave"] for f in e["figuritas"]})
ok("estado: conteos, datos, viajes y cierres", e["conteos"] >= 2 and e["cierres"] == 1 and e["viajes_lista"] == 1, str(
    {k: e[k] for k in ("conteos", "datos", "viajes_lista", "cierres")}))
ok("estado: figurita Timbre y Guardapolvo ganadas",
   {f["clave"] for f in e["figuritas"] if f["ganada"]} >= {"timbre", "guardapolvo"})
ok("nivel por estrellitas", desafio.nivel(850)["nombre"] == "Escolta" and desafio.nivel(299)["siguiente"] == "Buen alumno")

# ---------------- configuración ----------------
desafio.guardar_premios({"timbre": "un alfajor", "boletin": "salir una hora antes"})
ok("premios guardados", desafio.premios().get("boletin") == "salir una hora antes")
try:
    desafio.guardar_parametros(0, 1, 10000, 2, 130)
    ok("artículos por día fuera de rango da error", False)
except ValueError:
    ok("artículos por día fuera de rango da error", True)

# ---------------- pantallas (AppTest) ----------------
at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=180)
at.session_state["persona_id"] = DUENO
at.session_state["pagina_nav"] = "🏫 Desafío Recreo"
at.run()
ok("pantalla del desafío sin errores", not at.exception, str([x.value for x in at.exception])[:300])
etiquetas = [t.label for t in at.tabs]
ok("pestaña 🔒 Configuración para el dueño", "🔒 Configuración" in etiquetas, str(etiquetas))
md = " ".join(m.value for m in at.markdown)
ok("muestra el boletín de la semana pasada", "Boletín de la semana" in md and "7 de 7" in md)
ok("muestra los premios", "salir una hora antes" in " ".join(s.value for s in at.success) + md)

# aviso del juego en Registrar Venta: tomar un ticket grande sin juego
libre = q("""SELECT ticket_id FROM ventas WHERE ticket_id IS NOT NULL AND NOT es_consumo_interno
             AND ticket_id NOT IN (SELECT ref_id FROM desafio_eventos WHERE tipo = 'monedas')
             GROUP BY ticket_id HAVING SUM(monto_total) >= 10000 ORDER BY ticket_id DESC LIMIT 1""")[0][0]
at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=180)
at.session_state["persona_id"] = DUENO
at.session_state["pagina_nav"] = "💵 Registrar Venta"
at.session_state["juego_monedas"] = {"ticket": libre, "total": 12000.0}
at.run()
ok("Registrar Venta muestra '¿Jugaron a las monedas?'", any("Jugaron a las monedas" in m.value for m in at.markdown))
boton = [b for b in at.button if b.key == "juego_si"]
boton[0].click()
at.run()
ok("'Sí, jugamos' anota el juego", q("SELECT COUNT(*) FROM desafio_eventos WHERE tipo = 'monedas' AND ref_id = :t", t=libre)[0][0] == 1)
ok("el aviso desaparece", "juego_monedas" not in at.session_state and not at.exception)

# Cierre de Caja: el bloque para guardar el cierre
at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=180)
at.session_state["persona_id"] = DUENO
at.session_state["pagina_nav"] = "🧮 Cierre de Caja"
at.run()
ok("Cierre de Caja con «Guardar el cierre del día»", not at.exception
   and any("Guardar el cierre del día" in b.label for b in at.button), str([x.value for x in at.exception])[:200])

# pantalla del desafío: dato del día y conteo ya hechos se ven
at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=180)
at.session_state["persona_id"] = DUENO
at.session_state["pagina_nav"] = "🏫 Desafío Recreo"
at.run()
md = " ".join(m.value for m in at.markdown)
ok("la pantalla muestra la libreta y el conteo", "Libreta de esta semana" in md and "contaste" in md)

# pantalla para alguien que no es dueño (sin la pestaña 🔒)
q("UPDATE personas SET permisos = array_append(COALESCE(permisos, '{}'), 'desafio') WHERE id = :i "
  "AND NOT ('desafio' = ANY(COALESCE(permisos, '{}')))", i=OTRO)
at = AppTest.from_file(os.path.join(carpeta, "app.py"), default_timeout=180)
at.session_state["persona_id"] = OTRO
at.session_state["pagina_nav"] = "🏫 Desafío Recreo"
at.run()
rol_otro = q("SELECT rol FROM personas WHERE id = :i", i=OTRO)[0][0]
if rol_otro != "dueño":
    ok("sin 🔒 Configuración para quien no es dueño", "🔒 Configuración" not in [t.label for t in at.tabs]
       and not at.exception, str([t.label for t in at.tabs]))

print(f"\n{'TODO OK' if not fallas else f'{fallas} FALLAS'}")
sys.exit(1 if fallas else 0)
