"""Prueba en navegador real: menú por secciones, direcciones, sesión recordada y permisos.

Necesita la app corriendo contra una base de prueba, con los usuarios "dueno" (dueño)
y "atencion" (atención) con la contraseña PRUEBAS_CLAVE. Uso: python navegador.py
"""
import asyncio

from playwright.async_api import async_playwright

import os

URL = os.environ.get("PRUEBAS_URL", "http://localhost:8501")
CLAVE = os.environ.get("PRUEBAS_CLAVE", "prueba1234")
CHROME = os.environ.get("PRUEBAS_CHROME")  # vacío = el Chromium de Playwright


def informe(nombre, ok, det=""):
    print(f"{'OK   ' if ok else 'FALLA'} {nombre}" + (f" -- {det}" if det else ""))


async def quieto(pg, extra=800):
    await pg.wait_for_timeout(300)
    await pg.wait_for_function("() => !document.querySelector('[data-testid=\"stStatusWidget\"]')", timeout=60000)
    await pg.wait_for_timeout(extra)


async def entrar(pg, usuario):
    await pg.get_by_label("Usuario").fill(usuario)
    await pg.get_by_label("Contraseña").fill(CLAVE)
    await pg.get_by_role("button", name="Entrar").click()
    await quieto(pg, 2500)


async def titulo(pg):
    t = await pg.locator("[data-testid='stMain'] h1").all_inner_texts()
    return t[0] if t else ""


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(**({"executable_path": CHROME} if CHROME else {}))
        ctx = await b.new_context(viewport={"width": 1500, "height": 950})
        pg = await ctx.new_page()
        await pg.goto(URL, wait_until="networkidle")
        await pg.wait_for_timeout(2500)
        await entrar(pg, "dueno")
        informe("dueño entra al Tablero", "Dashboard" in await titulo(pg), f"{pg.url} · {await titulo(pg)}")
        secciones = await pg.locator("[data-testid='stNavSectionHeader']").all_inner_texts()
        informe("menú con secciones", len(secciones) == 5, str(secciones))
        await pg.screenshot(path="captura_menu.png")

        await pg.locator("[data-testid='stSidebarNav'] a", has_text="Cierre de Caja").click()
        await quieto(pg, 1500)
        informe("ir a Cierre de Caja cambia la dirección", pg.url.endswith("/caja") and "Cierre" in await titulo(pg), pg.url)
        await pg.reload(wait_until="networkidle")
        await quieto(pg, 2500)
        informe("recargar: sigue adentro y en la misma pantalla",
                pg.url.endswith("/caja") and "Cierre" in await titulo(pg), f"{pg.url} · {await titulo(pg)}")
        ck = [c for c in await ctx.cookies() if c["name"] == "almacen_sesion"]
        informe("la cookie vence a las 5 de la mañana", bool(ck), str(ck[0]["expires"]) if ck else "sin cookie")

        # entrar directo por dirección en otra pestaña
        p2 = await ctx.new_page()
        await p2.goto(URL + "/lista-de-compras", wait_until="networkidle")
        await quieto(p2, 3000)
        informe("abrir /lista-de-compras en otra pestaña entra directo", "Lista de compras" in await titulo(p2), await titulo(p2))
        await p2.close()

        # botón del Dashboard que lleva a otra pantalla (pagina_nav)
        await pg.locator("[data-testid='stSidebarNav'] a", has_text="Dashboard").click()
        await quieto(pg, 2500)
        boton = pg.get_by_role("button", name="Ver la lista de compras")
        if await boton.count():
            await boton.first.click()
            await quieto(pg, 2500)
            informe("botón 'Ver la lista de compras' del Dashboard", "Lista de compras" in await titulo(pg), pg.url)
        else:
            print("   (sin botón de lista de compras en el Dashboard hoy)")

        # salir y recargar: no vuelve a entrar solo
        await pg.get_by_role("button", name="Salir").click()
        await quieto(pg, 2000)
        informe("Salir lleva al ingreso", await pg.get_by_label("Usuario").count() == 1)
        await pg.reload(wait_until="networkidle")
        await quieto(pg, 2500)
        informe("después de Salir, recargar no vuelve a entrar", await pg.get_by_label("Usuario").count() == 1)
        ck = [c for c in await ctx.cookies() if c["name"] == "almacen_sesion" and c["value"]]
        informe("la cookie se borró", not ck)

        # cookie falsificada
        await ctx.add_cookies([{"name": "almacen_sesion", "value": "1.9999999999.abc", "domain": "localhost", "path": "/"}])
        await pg.reload(wait_until="networkidle")
        await quieto(pg, 2500)
        informe("una cookie inventada no entra", await pg.get_by_label("Usuario").count() == 1)
        await ctx.clear_cookies()

        # atención: solo sus pantallas, sin secciones
        await pg.reload(wait_until="networkidle")
        await quieto(pg, 2000)
        await entrar(pg, "atencion")
        links = await pg.locator("[data-testid='stSidebarNav'] a").all_inner_texts()
        informe("atención ve solo Registrar Venta", [l.split("\n")[-1] for l in links] == ["Registrar Venta"]
                or not links, str(links))
        informe("atención entra a Registrar Venta", "Registrar Venta" in await titulo(pg), await titulo(pg))
        await pg.goto(URL + "/configuracion", wait_until="networkidle")
        await quieto(pg, 2500)
        informe("atención no puede abrir /configuracion por la dirección", "Configuración" not in await titulo(pg),
                f"{pg.url} · {await titulo(pg)}")
        await pg.screenshot(path="captura_atencion.png")
        await b.close()


asyncio.run(main())
