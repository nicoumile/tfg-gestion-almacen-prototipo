"""
Controles de la tanda 12 sobre una copia de producción: bloqueo por intentos
fallidos, aviso de cantidades fuera de lo normal, stock sumado a mano antes de
la factura y Pendientes de ajuste sin combos de stock compartido.
La base tiene que tener la migración de la tanda 12 (o usar --sin-tabla en una
que no la tenga). ESCRIBE en la base: usar solo una copia local.

Uso: python pruebas/controles_tanda12.py <carpeta_codigo> <base> [--sin-tabla]
"""
import os
import sys

carpeta, base = os.path.abspath(sys.argv[1]), sys.argv[2]
sin_tabla = "--sin-tabla" in sys.argv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
config.usar_base(base)
sys.path.insert(0, carpeta)
os.chdir(carpeta)
from sqlalchemy import text  # noqa: E402

import db  # noqa: E402

fallas = 0


def ok(nombre, cond, det=""):
    global fallas
    fallas += not cond
    print(("OK    " if cond else "FALLA ") + nombre + (f"  -- {det}" if det else ""))


def q(sql, **p):
    with db.engine.begin() as c:
        r = c.execute(text(sql), p)
        return r.scalar() if r.returns_rows else None


db.set_persona_actual(1, "Dueño")

# ---------- 12.1 bloqueo ----------
if sin_tabla:
    ok("sin la tabla: segundos_bloqueo devuelve None", db.segundos_bloqueo("dueno") is None)
    ok("sin la tabla: registrar_intento_fallido devuelve None", db.registrar_intento_fallido("dueno") is None)
    db.registrar_ingreso_correcto("dueno")
    ok("sin la tabla: registrar_ingreso_correcto no falla", True)
    sys.exit(1 if fallas else 0)

q("DELETE FROM intentos_ingreso WHERE usuario IN ('dueno', 'noexiste', 'viejo', 'atencion')")
bloqueos_antes = q("SELECT count(*) FROM historial_cambios WHERE accion='INGRESO_BLOQUEADO'")
res = [db.registrar_intento_fallido("Dueño ") for _ in range(5)]
ok("4 fallidos no bloquean, el 5.º bloquea 60 s", res == [0, 0, 0, 0, 60], str(res))
s = db.segundos_bloqueo("dueno")
ok("bloqueado (otra sesión / recarga: se lee de la base)", 55 <= s <= 60, str(s))
ok("mayúsculas y espacios cuentan como el mismo usuario", db.segundos_bloqueo("  DUENO") == s)
ok("otro usuario no queda bloqueado", db.segundos_bloqueo("atencion") == 0)
ok("queda en el historial", q("SELECT count(*) FROM historial_cambios WHERE accion='INGRESO_BLOQUEADO'") == bloqueos_antes + 1)
q("UPDATE intentos_ingreso SET bloqueado_hasta = now() - interval '1 second' WHERE usuario='dueno'")
ok("vencido el bloqueo se puede intentar", db.segundos_bloqueo("dueno") == 0)
res = [db.registrar_intento_fallido("dueno") for _ in range(5)]
ok("el segundo bloqueo seguido dura el doble (120 s)", res[-1] == 120, str(res))
q("UPDATE intentos_ingreso SET bloqueado_hasta = now() - interval '1 second' WHERE usuario='dueno'")
for _ in range(10):
    for _ in range(5):
        r = db.registrar_intento_fallido("dueno")
    q("UPDATE intentos_ingreso SET bloqueado_hasta = now() - interval '1 second' WHERE usuario='dueno'")
ok("el bloqueo no pasa de 15 minutos", r == 900, str(r))
db.registrar_ingreso_correcto("dueno")
ok("un ingreso correcto borra el contador", q("SELECT count(*) FROM intentos_ingreso WHERE usuario='dueno'") == 0)
res = [db.registrar_intento_fallido("noexiste") for _ in range(5)]
ok("un usuario que no existe también se bloquea (no delata cuáles existen)", res[-1] == 60)
q("INSERT INTO intentos_ingreso (usuario, fallidos, ultimo_fallo) VALUES ('viejo', 4, now() - interval '2 hours')")
ok("4 fallidos de hace 2 horas: el contador arranca de nuevo", db.registrar_intento_fallido("viejo") == 0
   and q("SELECT fallidos FROM intentos_ingreso WHERE usuario='viejo'") == 1)

# ---------- 12.2 cantidades ----------
hab = db.cantidades_habituales([138, 163, 169, 3])
pal = {"articulo_id": 138, "cantidad": 140, "precio_unitario": 14000, "nombre": "PALETA", "unidad_medida": "unidad"}
r = db.controlar_cantidades([pal], hab)
ok("140 Paletas (lo habitual 0,2): pide revisar", len(r) == 1, r[0] if r else "")
pal_ok = dict(pal, cantidad=0.14)
ok("0,14 de Paleta: normal", db.controlar_cantidades([pal_ok], hab) == [])
queso = {"articulo_id": 163, "cantidad": 0.25, "precio_unitario": 9500, "nombre": "CREMOSO", "unidad_medida": "kg"}
ok("250 g de Cremoso: normal", db.controlar_cantidades([queso], hab) == [])
r = db.controlar_cantidades([dict(queso, cantidad=140)], hab)
ok("140 kg de Cremoso: pide revisar (y muestra gramos)", len(r) == 1 and "g por" in r[0], r[0] if r else "")
# todas las ventas reales de producción salvo la #1834: ninguna se hubiera frenado
with db.engine.connect() as c:
    filas = c.execute(text("""SELECT v.id, v.articulo_id, v.cantidad, v.precio_unitario, a.nombre, a.unidad_medida
                              FROM ventas v JOIN articulos a ON a.id = v.articulo_id
                              WHERE NOT v.es_consumo_interno""")).mappings().all()
hab_todos = db.cantidades_habituales({f["articulo_id"] for f in filas}, dias=3650)
marcadas = [f["id"] for f in filas if db.controlar_cantidades([dict(f)], hab_todos)]
ok("sobre las ventas reales solo se habría frenado la #1834", marcadas == [1834], str(marcadas))

# ---------- 12.3 suma a mano + factura ----------
art = 192  # TAPAS TARTA HOJALDRADAS YEYO
st0 = float(q("SELECT stock_actual FROM articulos WHERE id=:i", i=art))
ajustes0 = {i: q("SELECT count(*) FROM historial_cambios WHERE accion='AJUSTE_FACTURA' AND registro_id=:i", i=i)
            for i in (art, 163)}
a = q("SELECT row_to_json(a)::text FROM articulos a WHERE id=:i", i=art)
import json  # noqa: E402

a = json.loads(a)
db.actualizar_articulo(art, a["nombre"], a["marca"], a["categoria"], a["unidad_medida"], a["fecha_vencimiento"],
                       st0 + 12)
sumas = db.sumas_a_mano_recientes([art])
previa = db.posible_suma_duplicada(12, sumas.get(art))
ok("detecta la suma a mano de 12 recién hecha", previa is not None and previa["suma"] == 12, str(sumas))
ok("una factura de 40 no se confunde con esa suma de 12", db.posible_suma_duplicada(40, sumas.get(art)) is None)
prov = q("SELECT id FROM proveedores ORDER BY id LIMIT 1")
fid, _ = db.crear_factura(prov, "T12-A", "2026-10-21", [
    {"articulo_id": art, "cantidad": 12, "costo_unitario": 1700.0, "descontar_ya_sumado": 12}])
st1 = float(q("SELECT stock_actual FROM articulos WHERE id=:i", i=art))
ok("con 'era esta mercadería': el stock queda sumado UNA vez", abs(st1 - (st0 + 12)) < 1e-9, f"{st0} -> {st1}")
ok("queda el ajuste en el historial",
   q("SELECT count(*) FROM historial_cambios WHERE accion='AJUSTE_FACTURA' AND registro_id=:i", i=art) == ajustes0[art] + 1)
fid2, _ = db.crear_factura(prov, "T12-B", "2026-10-21", [{"articulo_id": art, "cantidad": 5, "costo_unitario": 1700.0}])
st2 = float(q("SELECT stock_actual FROM articulos WHERE id=:i", i=art))
ok("sin tildar: suma normal", abs(st2 - (st1 + 5)) < 1e-9)
db.agregar_items_factura(fid2, [{"articulo_id": 163, "cantidad": 3, "costo_unitario": 7000.0,
                                 "descontar_ya_sumado": 3}])
ok("agregar ítems a una factura también descuenta",
   q("SELECT count(*) FROM historial_cambios WHERE accion='AJUSTE_FACTURA' AND registro_id=163") == ajustes0[163] + 1)
# borrar la factura devuelve lo que sumó el trigger; el ajuste queda (es un movimiento aparte)

# ---------- 12.4 pendientes ----------
neg = db.articulos_stock_negativo()
ok("Pendientes de ajuste sin combos de stock compartido", not (neg["promo_tipo"] == "virtual").any(),
   ", ".join(neg["nombre"].head(6)))
negativos = q("SELECT count(*) FROM articulos WHERE stock_actual < 0 AND promo_tipo IS DISTINCT FROM 'virtual'")
ok("Pendientes lista todos los demás artículos con stock negativo", len(neg) == negativos, f"{len(neg)} de {negativos}")
sys.exit(1 if fallas else 0)
