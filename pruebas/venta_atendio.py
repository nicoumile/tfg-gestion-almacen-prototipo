"""
Vende un producto desde el carrito en un navegador real y controla que la
venta quede grabada con quién atendió (error del 21/10/2026: el carrito es un
fragmento y no fijaba la persona).

Uso: python venta_atendio.py <base>   (con la app corriendo en PRUEBAS_URL contra esa base)
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

BASE = sys.argv[1]
URL = os.environ.get("PRUEBAS_URL", "http://localhost:8501")
CLAVE = os.environ.get("PRUEBAS_CLAVE", "prueba1234")
CHROME = os.environ.get("PRUEBAS_CHROME")


async def quieto(pg):
    await pg.wait_for_timeout(300)
    await pg.wait_for_function("() => !document.querySelector('[data-testid=\"stStatusWidget\"]')", timeout=60000)
    await pg.wait_for_timeout(800)


def ultima_venta():
    c = config.conectar(BASE)
    with c.cursor() as cur:
        cur.execute("SELECT v.id, p.nombre FROM ventas v LEFT JOIN personas p ON p.id = v.registrado_por "
                    "ORDER BY v.id DESC LIMIT 1")
        fila = cur.fetchone()
    c.close()
    return fila


async def main():
    antes = ultima_venta()
    async with async_playwright() as p:
        b = await p.chromium.launch(**({"executable_path": CHROME} if CHROME else {}))
        pg = await b.new_page(viewport={"width": 1700, "height": 1000})
        await pg.goto(URL, wait_until="networkidle")
        await pg.wait_for_timeout(3000)
        await pg.get_by_label("Usuario").fill("dueno")
        await pg.get_by_label("Contraseña").fill(CLAVE)
        await pg.get_by_role("button", name="Entrar").click()
        await quieto(pg)
        await pg.locator("[data-testid='stSidebar']").get_by_text("Registrar Venta").first.click()
        await quieto(pg)
        await pg.locator(".st-key-grilla_venta [class*='st-key-add_'] button:visible").nth(0).click()
        await quieto(pg)
        await pg.get_by_role("button", name="CONFIRMAR VENTA").click()
        await quieto(pg)
        await b.close()
    despues = ultima_venta()
    ok = despues[0] != antes[0] and despues[1] == "Dueño"
    print(("OK   " if ok else "FALLA") + f" venta #{despues[0]} grabada con Atendió = {despues[1]}")
    sys.exit(0 if ok else 1)


asyncio.run(main())
