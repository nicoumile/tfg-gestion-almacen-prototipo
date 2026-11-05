# Sistema de Gestión de Almacén — MVP (Hitos 1 y 2)

MVP funcional probado de punta a punta: base de datos PostgreSQL + app en
Streamlit para cargar facturas de compra, registrar ventas y gastos, y ver
alertas de stock/vencimiento/variación de precios.

## Qué incluye

- `schema.sql` — schema completo de PostgreSQL: tablas, vistas y triggers
  que actualizan stock y costo automáticamente al cargar una compra o venta.
- `db.py` — capa de acceso a datos y lógica de negocio (cálculo de precios,
  alertas, lista de compras con pronóstico, reporte de ganancias vs gastos).
- `app.py` — interfaz Streamlit con 14 pantallas, agrupadas en el menú por secciones (Mostrador, Resumen y análisis, Compras, Stock, Administración).
- `requirements.txt` — dependencias Python.

## Instalación con la base de demostración (Entregable 4)

Para probar el prototipo con datos: la base simulada oficial (12 meses, semilla 42) sobre el esquema actual.

```bash
pip install -r requirements.txt
createdb -U postgres almacen
pg_restore -U postgres -d almacen --no-owner base_demo_entregable4.dump
```

Copiar `.streamlit/secrets.toml.example` como `.streamlit/secrets.toml` y completar `DB_HOST = "localhost"`, `DB_PORT = 5432`, `DB_NAME = "almacen"`, `DB_USER = "postgres"` y `DB_PASSWORD`. Después:

```bash
python preparar_demo.py
streamlit run app.py
```

`preparar_demo.py` corre las fechas de la base en semanas enteras para que la última venta quede en la última semana (si no, el pronóstico ve un mes sin ventas) y crea el usuario de demostración: usuario `demo`, contraseña `demo-tfg-2026`, rol dueño. Con `--usuario` y `--clave` se eligen otros. Se niega a correr contra la base de producción.

## Instalación desde cero (base vacía)

1. Crear la base de datos y aplicar el schema (con tu Postgres ya corriendo):
   ```bash
   createdb almacen
   psql -d almacen -f schema.sql
   ```

2. Instalar dependencias de Python:
   ```bash
   pip install -r requirements.txt
   ```

3. Configurar la conexión (si tu Postgres no usa los valores por defecto).
   Hay dos formas, y la app prueba primero `st.secrets` y si no encuentra
   nada cae a variables de entorno:

   - **Recomendado**: copiar `.streamlit/secrets.toml.example` a
     `.streamlit/secrets.toml` (ese archivo está en `.gitignore`, nunca se
     sube al repo) y completar ahí `DB_HOST`, `DB_PORT`, `DB_NAME`,
     `DB_USER`, `DB_PASSWORD`.
   - **Alternativa**: variables de entorno:
     ```bash
     export DB_HOST=localhost
     export DB_PORT=5432
     export DB_NAME=almacen
     export DB_USER=postgres
     export DB_PASSWORD=tu_password
     ```

   Si no configurás nada, usa esos mismos valores por defecto (pensados
   para un Postgres local de desarrollo).

   En Streamlit Community Cloud no se usa ningún archivo: los mismos
   nombres de clave se cargan en el panel "Secrets" de la app, apuntando
   ahí a la base en la nube (Neon/Supabase) en vez de a la de desarrollo.

4. Correr la app:
   ```bash
   streamlit run app.py
   ```
   Se abre en el navegador en `http://localhost:8501`.

## Pantallas

- **Dashboard**: resumen de alertas (stock bajo, vencimientos, variación de
  precios ≥5%).
- **Artículos**: alta de artículos, listado con costo/precio/margen
  calculados automáticamente, actualización de precio o margen.
- **Cargar Factura (Compra)**: alta de proveedores + carga de factura con
  varios ítems. Al guardar, actualiza stock y costo vigente automáticamente
  (vía triggers de la base).
  Arriba se elige el modo: **📥 Desde la planilla** (facturas leídas por el
  lector automático, ver sección más abajo) o **✍️ A mano**.
- **Registrar Venta**: descuenta stock automáticamente, avisa si se vende
  más de lo que hay disponible.
- **Gastos**: alquiler, servicios, sueldos, etc. — separado de compras de
  mercadería.
- **Reportes**: ganancia bruta (ventas − costo de mercadería vendida) y
  ganancia neta (restando además los gastos operativos) por período.
- **Lista de compras**: usa el pronóstico de demanda por artículo para
  armar el pedido del próximo viaje al mayorista (ver más abajo).

## Cómo se calcula cada cosa

- `costo_venta_sin_iva = costo_compra × (1 + margen)`
- `costo_venta_con_iva = costo_venta_sin_iva × (1 + iva)`
- **Variación de precio**: compara cada compra con la anterior del mismo
  artículo y proveedor (usando `LAG` en SQL), calcula el % de variación.
- **Stock bajo**: `stock_actual <= stock_minimo`.
- **Vencimiento cercano**: `fecha_vencimiento` dentro de los próximos 15 días.
- **Lista de compras**: ver la sección "🛒 Lista de compras" y
  `lista_compras.py` (pronóstico + stock de seguridad por clase ABC).

## Novedades de esta versión

- **Productos fraccionables (fiambres, quesos)**: cada artículo tiene ahora
  una `unidad_medida` (`unidad` o `kg`). Los productos en `kg` se compran y
  venden con cantidades decimales — una horma de 4,5kg se carga como
  cantidad=4.5 en la factura, y una venta de 500g se carga como cantidad=0.5.
  El stock, el costo y el precio de venta quedan expresados "por kg" para
  esos artículos automáticamente. Por ahora la identificación de qué
  artículo es fraccionable es manual (un selector al crearlo), con una
  sugerencia automática básica si la categoría contiene palabras como
  "fiambre", "queso", "embutido", etc. — un primer paso simple hacia la
  detección automática, mejorable a futuro con reglas más finas.
- **Carrito de venta real**: la pestaña "⚡ Venta rápida" dejó de confirmar
  cada producto al toque. Ahora se arma un carrito: se van agregando
  productos (unidades enteras o gramos, según corresponda), el total se
  acumula en pantalla en tiempo real con el precio ya calculado (markup +
  IVA), y **recién al tocar "Confirmar venta" se descuenta el stock de
  todos los ítems juntos** — como una caja registradora real. Se puede
  quitar ítems del carrito antes de confirmar. Los ítems de una misma
  venta quedan agrupados por un `ticket_id` para poder verlos juntos
  después.

## Alertas de quiebre en el Dashboard (Sprint 2, HU-016 — 20/10/2026)

- Tarjeta **🚨 Alertas de quiebre** en el Dashboard: qué artículos se quedan sin stock antes del próximo viaje al mayorista, según el pronóstico de demanda (no hace falta migración).
- **Próximo viaje** = última compra de mercadería que no es de compra diaria + "días entre viajes" (Configuración). Si esa fecha ya pasó, se toma hoy. Cuando exista el registro de viajes (Sprint 3) se usará ese dato.
- **Semáforo** (venta acumulada pronosticada como Poisson; para lo que se vende por kg, Gamma con la varianza de una Poisson compuesta según el tamaño típico de cada venta): 🔴 es más probable agotarse que no antes del próximo viaje; 🟡 llega al viaje pero no alcanza hasta el siguiente (tiene que ir en la lista); 🟢 alcanza. Muestra la venta en riesgo en pesos.
- **"Conviene adelantar el viaje"** cuando 3 o más artículos de clase A están en rojo.
- No entran el pan ni lo de compra diaria (se reponen todos los días) ni los artículos con stock negativo (primero hay que corregirles el stock).
- Lógica en `lista_compras.alertas_quiebre`; se recalcula cada 15 minutos.

## Seguimiento del pronóstico (19/10/2026)

- En **🔮 Pronóstico de demanda**, sección **¿Se cumplió el pronóstico?**: por clase, lo vendido vs lo pronosticado, sesgo y error semanal de los últimos 28 días. Cada día pasado se compara con lo que se pronosticaba ese día con las ventas hasta el día anterior (pronóstico guardado por la app desde el 18/10; antes, reconstruido con el modelo actual).
- Gráfico (tanda 15): unidades por día de todos los artículos (o de una clase): vendido, pronosticado ese día y, después de "hoy", el pronóstico de los próximos 14 días.
- Detalle de artículo: tabla día por día. Para los de compra diaria (tanda 16), gráfico de barras con lo **disponible** cada día (lo que quedó del anterior + lo comprado), partido en vendido, tirado/regalado (lo registrado), lo que pasó a su producto "de ayer" en Fraccionar, faltante en un conteo y lo que quedó para el día siguiente, con la línea del pronóstico de venta.
- **Pan de ayer (decisión del 22/10/2026):** el pan que sobra se pasa cada mañana a "PAN DE AYER NEWEN 500G" con ⚖️ Fraccionar (producto vinculado tipo paquetito, factor 1: mueve stock y costo, sin gasto falso ni corrección manual). Lo que no se vende al día siguiente se carga como Sobrante del día sobre el pan de ayer. Para el pronóstico, lo vendido de pan de ayer cuenta como demanda de pan y lo tirado no (`pronostico.demanda_diaria`). El stock de cada día se reconstruye hacia atrás desde el stock actual (`db.stock_diario_articulo`: compras, ventas, fraccionamientos y correcciones manuales). Antes se suponía que cada día arrancaba sin pan y se pintaba "comprado − vendido" como sobrante aunque siguiera en la estantería.
- Detalle de artículo (tanda 19): debajo del gráfico de ventas, un panel de stock con el mismo eje de fechas: el stock al final de cada día (reconstruido con `db.stock_diario_articulo`), el stock proyectado si no se compra (stock de hoy − venta esperada) y en rojo los días que terminaron sin stock (los que el pronóstico no cuenta como venta cero). Avisa si el stock quedó negativo.
- El "Stock actual" y la columna Stock / "Alcanza para" de la pantalla se leen en el momento (`db.stock_actual_por_id`); antes venían de los datos del pronóstico guardados en memoria hasta 1 hora.
- **💵 Registrar Venta → 🗑️ Sobrante del día**: lo que **ya** se regaló o se tiró (ej.: pan). Baja stock como consumo interno (medio "Sobrante") y registra el costo como gasto "Sobrante"; guardar de nuevo para el mismo día reemplaza el total. "¿De qué día es el sobrante?" arranca en ayer antes del mediodía (tanda 16). Muestra con cuánto arrancó ese día, lo comprado y vendido, y pide confirmar si el sobrante supera el stock actual. Las compras se siguen cargando por Cargar Factura.
- Para los artículos de compra diaria, el consumo interno cuenta como **sobrante**, no como venta.

## Lista de compras (Sprint 2, HU-015 — 18/10/2026)

- **🛒 Lista de compras** reemplaza a "Sugerencia de Reposición" (mismo permiso). Usa el pronóstico por artículo y separa:
  - **🥖 Compra diaria** (artículos marcados como compra diaria, ej.: el pan que el comerciante compra todos los días): cantidad que más ganancia deja en promedio (problema del vendedor de diarios): se compara lo que se gana por unidad vendida con lo que se pierde por unidad que sobra. Precio y costo se comparan con IVA los dos (lo que cobra y lo que paga un monotributista; para un responsable inscripto da igual). Con precio $2.000, costo $1.000 + IVA y lo que sobra regalado, el cociente es (2.000 − 1.210) / 2.000 ≈ 0,40: conviene tener un poco menos que la venta esperada del día (percentil 40). Muestra sobrante esperado y probabilidad de agotarse; se puede fijar un nivel de servicio propio. Lo que sobra solo cuenta para el día siguiente si se aprovecha (parámetro `valor_recupero_pct`). **Ya tenés** (tanda 23): si el artículo vence en el día (vida útil de 1 día), cuenta solo lo comprado hoy que sigue en stock (lo de ayer se vende primero) y solo para la compra de hoy; para la de mañana, nada. Antes no contaba nada, y después de cargar la compra de la tarde la lista volvía a pedir todo.
  - **🚚 Viaje al mayorista** (el resto): venta esperada hasta el próximo viaje (Configuración → días entre viajes) + margen de seguridad según la clase ABC (A 95 %, B 90 %, C 80 %) − stock, redondeado al bulto del proveedor. Si vence antes del próximo viaje, cubre solo su vida útil. Se puede corregir la cantidad o destildar, y copiar el texto para WhatsApp agrupado por proveedor, con costo estimado con IVA (lo que hay que llevar).
  - **⚙️ Artículos de compra diaria y perecederos**: marcar compra diaria, vida útil y nivel de servicio propio; sugiere artículos que se compran seguido.
- **Lógica en `lista_compras.py`**; el margen de seguridad usa raíz(venta esperada) corregida por la dispersión medida en la evaluación del pronóstico de cada clase.
- **Requiere** correr una vez `python migracion_lista_compras.py` (agrega `articulos.reposicion_dias`, `vida_util_dias`, `nivel_servicio` y el parámetro `valor_recupero_pct`), con respaldo previo.

## Pronóstico de demanda por artículo (Sprint 2, HU-014 — 18/10/2026)

- **🔮 Pronóstico de demanda** (pantalla nueva, la ve el dueño; se puede habilitar a otros): cuánto se espera vender de cada artículo en los próximos 7 y 14 días, cuántos días alcanza el stock actual, y el gráfico de ventas reales vs pronóstico por artículo. Si hay historia suficiente (≈ 48 días), muestra también el error de cada clase ABC comparado con el método anterior.
- **Lógica en `pronostico.py`** (pandas/numpy, 2 consultas, sin una consulta por artículo): demanda diaria desde `v_ventas_analitica` (combos resueltos en sus componentes, consumo interno incluido, sueltos fraccionados contados en su bolsa, reemplazos sumados al artículo vigente); modelos: promedio de 30 días (línea base), nivel suavizado y Croston-SBA, ajustados por día de semana y bloque del mes; la configuración se elige **por clase ABC** con un backtest de origen móvil (60 días, horizonte de 7 días, sin fuga de datos).
- **Pronóstico guardado**: la primera vez que el dueño entra cada día se guarda el pronóstico de los próximos 14 días en `pronostico_snapshot`, para medir el error real durante la validación.
- **`evaluar_pronostico.py`**: reproduce la evaluación para el documento (tablas CSV + figura). Ej.: `python evaluar_pronostico.py 2026-10-05` sobre la base simulada (usa los datos hasta el día anterior a la fecha indicada).
- **Demanda censurada (21/10/2026):** un día sin venta en que el artículo arrancó sin stock no cuenta como "demanda 0": no se vendió porque no había. El stock de cada día se reconstruye hacia atrás desde el stock actual (compras, ventas, combos, fraccionamientos y correcciones manuales). No se corrigen los artículos cuyo stock reconstruido queda negativo (faltan compras cargadas) ni los que tienen menos de 7 días con stock. En la base simulada (13 % de la demanda perdida por quiebres) baja el sesgo contra la demanda verdadera de −15,6 % a −4,5 % en la clase A (`experimento_censura.py`).
- **Error sin sesgo de selección:** además del error con el que se elige el modelo, la pantalla muestra el error elegido con la primera mitad de los días de prueba y medido con la segunda.
- El error se mide contra lo vendido: con quiebres, lo vendido es menos que la demanda, así que un pronóstico que corrige la censura puede aparecer "por encima".

## Usuarios, permisos y base del Sprint 2 (18/10/2026)

- **Ingreso con usuario y contraseña.** Quien entra es quien "atiende": cada venta guarda `ventas.registrado_por` y cada cambio del historial guarda `historial_cambios.usuario`. Las contraseñas se guardan con hash PBKDF2-SHA256 y sal (nunca en texto plano). 5 intentos fallidos bloquean el ingreso 60 segundos en esa sesión.
- **Roles.** *Dueño*: ve todas las pantallas y es el único que entra a **⚙️ Configuración**. *Atención*: ve solo las pantallas que el dueño le habilita (por defecto, Registrar Venta) y no puede editar ni eliminar ventas ya cargadas.
- **⚙️ Configuración**: crear usuarios, habilitar pantallas, asignar contraseñas, desactivar (no borra nada) y parámetros de reposición (días entre viajes al mayorista, capacidad del depósito en litros, nivel de servicio por clase ABC). Solo el dueño cambia contraseñas: la suya desde el menú lateral y la de cualquier usuario desde Configuración; atención no puede cambiar la suya.
- **Nuevas tablas/columnas**: `personas`, `parametros`, `pronostico_snapshot` (pronósticos guardados para medir el error real), `articulos.volumen_litros`.
- **Sesión recordada (tanda 10, 21/10/2026):** al entrar, el navegador guarda una cookie firmada (`almacen_sesion`) que vence a las 5 de la mañana del día siguiente: recargar la página o abrir otra pestaña no pide volver a entrar. La firma usa una clave derivada de la contraseña de la base (o `SESION_SECRETO` en los secrets, si se define) e incluye una huella de la contraseña de la persona: cambiarla o desactivar a la persona invalida la sesión recordada. **Salir** la borra. En una computadora compartida, cada uno tiene que tocar Salir al terminar.
- **Menú por secciones (tanda 10):** cada pantalla tiene su dirección (por ejemplo `/venta`, `/caja`, `/lista-de-compras`; la de inicio es la raíz) y al recargar se queda en la misma. Atención solo ve, y solo puede abrir por dirección, las pantallas habilitadas.
- **Límites conocidos**: la URL de la app sigue siendo pública, la protección es la contraseña. El bloqueo por 5 intentos fallidos todavía vive en la sesión del navegador (tanda 12).
- **Instalación en una base existente** (con respaldo previo): `python respaldo_produccion.py` y después `python migracion_sprint2.py` (pide la contraseña inicial del comerciante, usuario `dueno`). Recién después, subir la versión nueva de la app.
- Corrección: la pantalla **🏷️ Precios por Proveedor** fallaba cuando había artículos con más de un proveedor (buscaba una columna por su nombre anterior al renombrarla).

## Importar Facturas desde la planilla (16/10/2026)

- En **🧾 Cargar Factura**, modo **📥 Desde la planilla (lector automático)** (el modo **✍️ A mano** es la carga de siempre): lista las facturas que el lector automático (Drive + Apps Script + Gemini) ya leyó y se confirmaron en la planilla "Registro de facturas" del comerciante, y las carga como compras con un clic.
- **Qué pasa a la app**: proveedor (se reconoce por CUIT; si no existe se crea o se le asigna el CUIT a uno ya cargado), número, fecha, link al PDF y, por cada renglón que es stock (se excluyen promociones y lo marcado "Es stock = NO"), la cantidad y el costo unitario sin IVA. El resto (CAE, IVA por renglón, validaciones) queda solo en la planilla.
- **Vinculación de artículos**: igual que en Cargar Factura (alias exacto → similitud → confirmación), pero ahora el alias guarda también el **factor**: cuántas unidades del artículo trae cada unidad facturada (caja de 60 que se vende suelta → 60). Cantidad = facturada × factor; costo unitario = costo total sin IVA ÷ cantidad. La primera vez se propone el factor decidido en la planilla; después el alias lo recuerda y la factura se reconoce sola.
- **Sin duplicados**: cada factura importada guarda `ref_externa = 'planilla:C-000xx'` (única en la base) y se marca en la planilla (columna "Factura app"). Si ya existe una factura del mismo proveedor y número cargada a mano, avisa y permite marcarla sin volver a sumar stock.
- **Diccionario de la planilla (23/10/2026)**: al importar, la app escribe en la hoja "Diccionario" (fila del proveedor + código) el artículo vinculado (`id_articulo_app` y `Artículo app`, el nombre en el catálogo) y el `Factor pack`. En las facturas siguientes cada renglón se reconoce primero **por el código del proveedor** (📗), aunque la descripción venga escrita distinto; después por alias exacto (✅) y por último por similitud (⭐). Si el código coincide pero la descripción es de otro producto, no se usa el código. El lector también aprovecha el Factor pack para proponer el pack la próxima vez. Si la planilla no responde, la factura se importa igual y se avisa.
- `origen = 'llm'` en las facturas importadas, para poder distinguirlas de las manuales en análisis posteriores.
- Requiere: correr una vez `python migracion_importador_facturas.py`, agregar `gspread` (ya está en `requirements.txt`) y configurar en *Secrets* `PLANILLA_FACTURAS_ID` + `[gcp_service_account]` (ver `.streamlit/secrets.toml.example` e `importador_planilla.py`).

## Stock inmovilizado (Reportes → Clasificación ABC; corregido en la tanda 15, 22/10/2026)

- Dos grupos: **nunca se vendieron** desde que se cargaron (no depende del slider) y **se vendieron alguna vez pero no en los últimos N días**.
- Antes, lo que nunca se vendió se contaba desde la fecha de alta y aparecía recién pasados N días. Como el catálogo se cargó todo junto el 28/09/2026, con 60 días no aparecía ninguno (171 artículos, ~$1,7 M a costo).
- El slider arranca en 60 días o en los días de ventas registradas, si son menos. Si se elige más que eso, avisa que ese grupo no se puede evaluar todavía.
- "Venta" incluye combos y consumo interno. Para una bolsa, también lo vendido de su suelto y los fraccionamientos.
- No suman en lo parado: lo **recién cargado** (alta de menos de 7 días, `db.DIAS_RECIEN_CARGADO`) y lo que está **en consignación** (tampoco suma en el total de mercadería).
- Lógica en `db.stock_inmovilizado` y `db.registro_ventas`.

## Artículos inactivos (14/10/2026)

- En **Artículos → Editar artículo** hay un interruptor **Artículo activo**. Al apagarlo se elige el motivo (discontinuado, reemplazado por otro artículo, o temporal) y, si fue reemplazado, por cuál. No se borra nada: las ventas y compras del artículo siguen en los reportes, y se puede reactivar desde el mismo lugar.
- Un artículo inactivo no aparece en Registrar Venta, Cargar Factura, Promociones, Fraccionar ni Reposición. En el Listado se ven con "Mostrar también los artículos inactivos".
- **Calidad de datos**: los inactivos sin stock quedan fuera del indicador. Si les queda stock, siguen adentro con la alerta "Inactivo con stock" hasta que se liquide o se ajuste a 0. La foto semanal guarda cuántos artículos se evaluaron y cuántos quedaron fuera por inactivos.
- **ABC**: los inactivos se siguen clasificando (sus ventas del período ocurrieron) y se marcan como tales.
- **Reposición**: el artículo nuevo suma la venta reciente del que reemplazó.
- Requiere correr una vez `python migracion_articulos_inactivos.py` antes de usar la versión nueva.

## Novedades de la versión anterior

- **Venta rápida por botones + venta genérica (modelo híbrido)**: se detectó que en el mostrador real no hay forma de identificar qué producto se lleva cada cliente (sin código de barras ni QR). La pantalla "Registrar Venta" tenía dos modos: carrito por producto y "otra venta (monto libre)" sin producto.
  - **Actualización 10/2026**: la venta genérica (sin producto) se dio de baja. Toda venta se carga con su artículo, porque una venta sin producto queda fuera del análisis (ABC, márgenes, reposición, calidad de datos). Ver `migracion_quitar_venta_generica.py`.
  - Se agregó también el campo **medio de pago** (Efectivo, tarjeta, transferencia, etc.), que antes no se registraba.

## Novedades de la versión anterior

- **Eliminar facturas**: en "Cargar Factura" hay un listado de facturas
  cargadas con opción de borrar. Al borrar, un trigger (`trg_revertir_compra`)
  resta automáticamente el stock que esa factura había sumado. Nota: no
  revierte el `costo_compra_actual` del artículo — si era la última compra
  cargada, conviene revisar el precio a mano después de borrar.
- **Editar/eliminar ventas**: en "Registrar Venta" hay un formulario para
  corregir cantidad/precio/fecha/artículo de una venta ya cargada, o
  eliminarla. Los triggers `trg_editar_venta` y `trg_revertir_venta` ajustan
  el stock automáticamente en ambos casos.
- **Buscador y filtro de artículos**: en la pestaña "Listado" de Artículos,
  por nombre y por categoría.
- **Exportar a Excel**: en Reportes, botón de descarga con 3 hojas (Resumen,
  Ventas, Gastos) del período seleccionado.
- **Fuzzy matching de artículos (Hito 5)**: al cargar una factura, opción de
  escribir el nombre tal como figura en el documento del proveedor. El
  sistema busca coincidencias por similitud de texto (`pg_trgm`) contra tu
  catálogo, y si confirmás cuál es, guarda ese alias (`articulo_alias`) para
  reconocerlo automáticamente la próxima vez que ese mismo proveedor lo
  facture con ese nombre.

## Qué falta (backlog del Entregable 3, actualizado el 27/10/2026)

La ingesta automática de facturas y su revisión humana (los antiguos
Hitos 3 y 4) ya están resueltas por el importador desde la planilla
(HU-002, ver "Importar Facturas desde la planilla"): las facturas entran con
origen `llm` y `ref_externa`, que impide importarlas dos veces.

- **HU-021 — Comparar políticas de reposición**: hecha en la tanda 22 (ver
  "Comparación de políticas de reposición").
- **HU-023 — Validación con el comerciante**: en curso (`validacion.py`,
  Desafío Recreo); corte parcial en el Entregable 4 (9/11/2026) y resultados
  completos al cumplirse el trimestre.
- **Fuera del alcance actual** (descartadas el 27/10/2026): HU-017 (ajustar la
  lista al espacio del depósito; quedan `articulos.volumen_litros` y el
  parámetro `capacidad_litros` sin uso), HU-019 (comparar canales por costo
  puesto) y HU-020 (precios relevados sin compra).
- **Numeración**: el desempeño de proveedores (tanda 14) es la HU-025; no
  estaba en la Tabla 3 del Entregable 3.
- **Ideas a futuro**: QR/código de barras (el modelo de "venta rápida por
  producto" ya sirve: solo cambiaría cómo se elige el artículo), editar ítems
  sueltos de una factura ya cargada (hoy se borra la factura entera y se
  vuelve a cargar) y exportar reportes a PDF.

## Proveedores (HU-025, tanda 14, 10/2026)

La pantalla **🏭 Proveedores** (antes "Precios por Proveedor") tiene tres pestañas.

**Desempeño** (`proveedores.py`): una fila por proveedor para el período elegido.
- Compras sin IVA, % del total, facturas y cada cuántos días se le compra.
- Venta neta, ganancia real y margen de sus productos.
- Stock a costo, días de cobertura y rendimiento por peso en stock (GMROI anualizado).
- Aumento mensual de costo, sobreprecio frente al más barato y % de días sin stock.

Cada artículo se reparte entre sus proveedores según cuánto se le compró a cada uno. Lo que nunca tuvo factura queda en "Sin factura de compra". La ganancia total cuadra con Márgenes reales y el stock con el valor de la mercadería. No se mide el plazo de entrega, porque la app no registra pedidos.

**Consignación:** proveedores que dejan mercadería y cobran recién lo vendido (FUSION FUNGA, `migracion_tanda14.py`).
- La reposición se carga como una factura.
- La pestaña muestra cuánto se le debe: el costo de lo vendido desde la última liquidación, incluidas promos y consumo interno.
- Permite registrar el pago, que **no** va en Gastos porque ese costo ya está en la ganancia.
- Muestra el stock para contar con el proveedor.

Esa mercadería no suma en "Invertido en stock" (se muestra aparte). En la lista de compras aparece para pedírsela al proveedor, sin costo en el viaje.

**Precios:** comparación de costo entre proveedores, igual que antes.

## Controles al cargar (tanda 12, 10/2026)

- **Bloqueo de ingreso:** después de 5 contraseñas incorrectas seguidas, ese usuario queda bloqueado 1 minuto. Cada bloqueo seguido dura el doble, hasta 15 minutos. Se guarda en la base (`intentos_ingreso`, `migracion_tanda12.py`), así que recargar la página no lo saltea.
- **Cantidades fuera de lo normal:** en Registrar Venta, un renglón pide "Las cantidades están bien" antes de confirmar si se da alguno de estos casos:
  - vale $150.000 o más;
  - es 20 veces o más lo que se vende normalmente de ese artículo;
  - sin historial del artículo: 50 unidades o más, o 10 kg o más.
  Caso que lo motivó: 140 "unidades" de Paleta. Sobre las ventas reales hasta el 21/10 solo se habría frenado esa.
- **Stock sumado a mano antes de la factura:** al cargar una factura, a mano o desde la planilla, avisa si en los últimos 7 días se subió a mano el stock de ese artículo en una cantidad parecida. Si se tilda "era esta mercadería", al guardar se descuenta esa suma y el stock queda contado una sola vez (queda en el Historial como AJUSTE_FACTURA).
- **Pendientes de ajuste:** no lista los combos de stock compartido, porque su stock se calcula de los componentes.

## Protocolo de validación: lista guardada, viajes e indicadores (tanda 17, 10/2026)

Instrumentos del documento "Protocolo de validación con el comerciante" (20/10/2026 – 20/01/2027).

- **🛒 Lista de compras → "Me voy al mayorista con esta lista"**: guarda la lista del viaje tal como quedó. Registra lo que sugirió la app y lo que se llevó: cantidades corregidas y renglones destildados. Lo que quedó afuera por los filtros de clase o proveedor cuenta como sacado. Una lista por día: guardar de nuevo reemplaza la de hoy.
- **🧾 Registro de viajes y listas guardadas** (en la misma pantalla):
  - cada viaje se anota con fecha, planificado o de urgencia, horas, con qué lista fue y una nota;
  - **¿Se compró lo de la lista?**: cada lista contra las facturas de los días que cubre, es decir, ese día y los `frecuencia_viaje_dias − 1` siguientes, o hasta la lista siguiente;
  - cada renglón queda como comprado igual (±10 %, mínimo 1 unidad o 0,1 kg), otra cantidad, no se compró, sacado, sacado pero se compró o agregado;
  - no cuentan la compra diaria ni la consignación.
- El próximo viaje de las alertas de quiebre usa el último viaje **planificado** anotado, si es más reciente que la última compra.
- **`validacion.py`**: indicadores semana por semana (lunes a domingo) desde los CSV de un respaldo. No se conecta a la base. Calcula:
  - % de ventas con "Atendió", días sin ventas, correcciones manuales de stock;
  - % de días sin stock en clase A (con la reconstrucción de stock del pronóstico) y calidad de datos;
  - pan comprado, vendido y tirado por día, viajes;
  - error real del pronóstico guardado a 7 días por clase (WAPE y sesgo), listas guardadas;
  - foto del momento: stock negativo y capital parado.

  Deja CSV en `respaldos/validacion/`, que no va al repositorio.
- Tablas nuevas (`migracion_tanda17.py`): `listas_compra`, `listas_compra_detalle`, `viajes_compra`. Sin la migración, la app funciona igual y no muestra el botón ni el registro.

## Vencimientos por lote implícito y alertas con binomial negativa (tanda 18, 10/2026)

- **Días disponibles para la venta**: por artículo (🛒 Lista de compras → Artículos de compra diaria y perecederos) o por categoría (⚙️ Configuración, tabla `vida_util_categoria`). El del artículo manda.
- **Lotes sin anotar lotes**: cada renglón de factura es un lote. Vence en la fecha cargada en el renglón (columna opcional "Vence" en Cargar Factura, `detalle_factura.fecha_vencimiento`) o, si no, en fecha de la factura + días disponibles. Se supone que se vende primero lo más viejo (`db.lotes_vencimiento`). El stock que no viene de facturas (carga inicial) queda "sin fecha".
- **Riesgo de vencer** (`lista_compras.riesgo_vencimiento`): para cada lote, venta esperada hasta que vence, probabilidad de que sobre algo y unidades que se tirarían, con la misma distribución de las alertas. Dashboard → ⏰ Vencimientos (lotes de los próximos 30 días) y tarjeta "En riesgo de vencer".
- **Motivo "Vencido"** en 💵 Registrar Venta → 🗑️ Sobrante del día: baja stock como consumo interno y va a costo como gasto "Vencido" (mercadería: no resta del neto de caja). No cuenta como demanda para el pronóstico ni como movimiento para el stock inmovilizado. Merma del mes en el Dashboard (`db.merma_vencido`).
- **Lista de compras**: si el artículo (o su categoría) vence antes del próximo viaje, cubre solo esos días (antes, solo con la vida útil del artículo).
- **Alertas de quiebre**: la probabilidad de agotarse usa una binomial negativa con la dispersión que mide la evaluación (la misma del stock de seguridad de la lista). Con dispersión 1 (mientras no hay evaluación propia, ~48 días de historia) es exactamente la Poisson de antes. En la base simulada la Poisson subestimaba a la mitad los riesgos bajos y medios; con la dispersión el error de calibración bajó de 1,9 a 0,6-1,0 puntos.
- Requiere `python migracion_tanda18.py`. Sin la migración la app funciona igual que antes (la pestaña de vencimientos no aparece y Configuración avisa).

## Hora argentina y clase heredada de los combos (tanda 20, 10/2026)

- **Hora:** las fechas y horas (`creado_en`, `actualizado_en`, `historial_cambios.fecha`) se guardan en **UTC**. Ya era así en producción: el `SET TIME ZONE` de Argentina no llegaba a las consultas por el pooler de Neon. Ahora se fija UTC en cada conexión, para que valga igual en todos lados, y:
  - el Historial muestra la hora argentina y filtra por día argentino (`db.listar_historial`, `db.SQL_HORA_AR`, `db.a_hora_argentina`);
  - "hoy" adentro de las consultas es `db.HOY_SQL`, el día argentino. Antes era `CURRENT_DATE`, que en UTC pasa a "mañana" después de las 21 h. Afectaba el stock inmovilizado, la calidad semanal, la lista de compras y las liquidaciones;
  - las correcciones de stock hechas de noche cuentan para su día argentino en la reconstrucción del stock (pronóstico, gráfico del pan y `validacion.py`);
  - el fraccionamiento se guarda con el día argentino.
- **Clase heredada de los combos:** en la lista de compras, las alertas de quiebre y el riesgo de vencimiento, cada componente de un combo usa la mejor clase entre la suya y la de los combos que integra (`db.clase_combos`, `lista_compras.heredar_clase_combos`). Solo sube de clase, nunca baja. La lista lo indica en "Aviso". La elección del modelo de pronóstico sigue por la clase propia.

## Desafío Recreo: control de vencimientos en góndola (tanda 21, 10/2026)

- **Por qué:** las facturas no traen vencimientos, así que la fecha real se mira en la góndola. Para que el control diario no sea una carga, se arma como un juego con la temática del almacén ("Recreo"). Pantalla **🏫 Desafío Recreo** (menú Stock).
- **Tarea del día** (`desafio.armar_tarea`): 6 artículos con stock que vencen (sin fraccionados, compra diaria, Limpieza, Tabaco, Librería, Otros, bebidas blancas ni los marcados "No vence"). La mitad son perecederos, el resto por plata en stock. Un mismo artículo no vuelve antes de 7 días. A veces hay un **repaso**, que repite un artículo ya controlado: la fecha más cercana no puede ser anterior a la vista antes, y si lo es, no suma y queda para revisar.
- **Control** (`controles_vencimiento`, `desafio.registrar_control`): la fecha más cercana que se ve, la cantidad opcional, "No hay" o "No vence". Guarda también el stock de la app y la fecha de la compra más vieja que queda (FEFO). Con eso, `desafio.dias_tipicos` mide los días disponibles reales por categoría y 🔒 Configuración puede copiar la mediana a la vida útil de la categoría.
- **Vencimientos:** el último control con fecha pasa al lote más viejo del artículo (fuente "Góndola" en Dashboard → ⏰ Vencimientos, `db.lotes_vencimiento`).
- **Estrellitas** (`desafio_eventos`):
  - +10 por control, +15 si es perecedero, +20 la tarea completa;
  - desde 5 días de racha, ×1,5 las del día;
  - **boletín de la semana** +100: lunes a domingo cubiertos, con 1 comodín por semana;
  - rescate: 1 cada $1.000 de costo de lo que estaba por vencer (≤ 15 días) y se vendió a tiempo;
  - niveles: Buen alumno 300, Escolta 800, Abanderado 1.400.
- **Figuritas** (`figuritas.py`, pixel art propio en SVG): Timbre, Mochi, Gato del Recreo, Guardapolvo, Escolta, Abanderado y Monedita. Los premios los escribe el dueño en la pestaña 🔒 Configuración, que solo ve él. Ahí también se ponen los artículos por día, los comodines, el juego de las monedas y quién participa.
- **Recreo con clientes:**
  - Después de una venta de $10.000 o más (configurable), 💵 Registrar Venta pregunta "¿Jugaron a las monedas?". No frena la venta.
  - Si jugaron, las monedas de chocolate salen del stock como regalo: consumo interno con medio "Regalo" y gasto "Regalo a clientes" a costo. Como mercadería, no resta del neto de caja.
  - Suma +5 estrellitas, hasta 3 juegos por día. Con 10 juegos se gana la Monedita.
- **Libreta con cuatro materias** (`desafio.libreta`, nota de 1 a 10 por semana). La regla es premiar la acción que la app puede verificar, nunca el indicador: no suma tener menos alertas, menos viajes de urgencia ni una caja que cierra justo.
  - 🧺 **Góndola:** la tarea de vencimientos. En 2 de los 6 artículos (configurable) pide además un **conteo a ciegas**: ¿cuántos hay?, sin mostrar el stock de la app. Suma +5.
    - El conteo coincide si es la misma cantidad por unidad, o hasta 5 % por kg.
    - Si no coincide, se puede corregir el stock a lo contado (`desafio.corregir_por_conteo`). Se suman los movimientos posteriores y queda en el historial con el texto de siempre, "Stock corregido a mano…", que lee la reconstrucción.
    - Mide la **exactitud del inventario**.
  - 🔍 **Orden: "Arreglá un dato".** Uno por día de la tabla Qué corregir del indicador de calidad. La prioridad es stock negativo, después sin costo, después venta atípica.
    - Se arregla en la misma pantalla: contar y corregir el stock, o cargar el costo.
    - Suma +15 solo si la app verifica que quedó arreglado. La venta atípica suma +5 porque no se puede verificar. "Hoy no pude" deja completar la tarea, pero suma 0.
  - 🚌 **Excursión** (`desafio.otorgar_excursiones`):
    - +10 por guardar la lista;
    - +10 por anotar el viaje, planificado o de urgencia igual;
    - +20 por "lista cumplida": cerrada la semana de la lista, se compró al menos el 80 % de lo llevado.
  - 🧮 **Cuentas: cierre del día.** En 🧮 Cierre de Caja, «Guardar el cierre del día» guarda el arqueo (`cierres_caja`: esperado, contado y diferencia). Si se compró pan y no se cargó sobrante, pide marcar "no sobró" (`db.pan_resuelto`). Suma +10 si se guarda el mismo día con el efectivo contado.
  - Figuritas nuevas: Ábaco (20 conteos), Lupa (10 datos arreglados), Micro de la excursión (4 viajes con la lista) y Alcancía (10 cierres). Insignias nuevas: Semana en verde (indicador de calidad ≥ 95 %) y Libreta completa (las cuatro materias con 7 o más).
- **Validación** (`validacion.py`), por semana:
  - días con la tarea cumplida, controles y artículos con fecha vista;
  - conteos, % de exactitud del inventario y conteos corregidos;
  - datos asignados y arreglados;
  - cierres guardados y diferencia de caja promedio.
- Requiere `python migracion_tanda21.py`. Sin la migración, la pantalla avisa y el resto de la app no cambia.

## Comparación de políticas de reposición (HU-021, tanda 22, 10/2026)

Responde si comprar con la lista de compras de la app conviene más que como se compraba. No cambia la app ni la base: son tres scripts que corren aparte.

- **Demanda verdadera** (`demanda_sim.py`): un año de lo que los clientes quieren llevar, por día y artículo, sobre el catálogo real (`catalogo_real_export.csv` y `promo_componentes_export.csv`, que no van al repo). Incluye lo que no se vendería por falta de stock. Dos escenarios: popularidad **estable** y **cambiante** (paseo aleatorio, artículos nuevos y discontinuados, saltos).
- **Simulación** (`simular_politicas.py`): la misma demanda se resuelve contra el stock de cada política. Venta = mín(demanda, stock); lo que falta es venta perdida. Un viaje por semana (lunes). Los primeros 90 días todas usan la regla actual y se evalúa del día 91 al 365. Las políticas ABC y por pronóstico solo ven lo vendido, como la app.
  1. **Regla actual**: si el stock baja de max(mínimo, 2), reponer hasta max(stock inicial, umbral × 4, 8). Es la regla con la que se generó la base simulada oficial.
  2. **ABC fija**: cubrir la venta promedio de 90 días por 7 días del viaje más días de seguridad por clase (A 15, B 8, C 3), sin pronóstico.
  3. **Por pronóstico**: la lista de compras de la app, sin cambios (`pronostico.pronosticar` con el modelo elegido por clase y la corrección de censura, y `lista_compras.calcular` con nivel de servicio A 95 %, B 90 %, C 80 %).
- **Informe** (`analizar_politicas.py`): por escenario y política, mediana y p5-p95 entre semillas de **ventas perdidas** (% de la demanda en pesos), **nivel de servicio** (% de unidades atendidas) y **capital inmovilizado** (stock promedio a costo), también por clase. Las diferencias se miden pareadas, semilla por semilla. Arma CSV, una tabla de texto y `figura_politicas.svg`.
- **Resultado (40 años simulados, 27/10/2026)**: la política por pronóstico pierde **3,3-3,5 %** de las ventas, contra **12,9-15,4 %** de la regla actual y **4,1-4,5 %** de la ABC fija. Sirve el **95 %** de lo pedido, contra 87-89 % y 90-91 %. Inmoviliza **16-22 % menos capital** que la regla actual y **17-20 % menos** que la ABC. Mejora en los 40 años contra las dos. La ABC fija protege la clase A (98 %), pero a costa de la C (80 %); el pronóstico queda parejo (A 97 %, B 95 %, C 92-93 %).
- Uso:
  ```
  python simular_politicas.py estable 1,2,...,20 "Claude outputs/tanda22/corridas"     (~30 s por semilla)
  python simular_politicas.py cambiante 1,2,...,20 "Claude outputs/tanda22/corridas"
  python analizar_politicas.py "Claude outputs/tanda22/corridas" "Claude outputs/tanda22"
  ```
- Antecedente: la misma comparación se corrió el 23-24/10 fuera del repo con el pronóstico de la tanda 16 (ahí figuraba como "HU-018", con la numeración vieja). Con el código actual dio los mismos resultados, con diferencias de décimas.

## Notas de testing

**Desde la tanda 11 (10/2026):** cada tanda se prueba antes de subirla con el kit de
`pruebas/` (ver `pruebas/README.md`), sobre una copia local de producción armada
desde un respaldo. El kit compara el comportamiento de la versión vieja y la nueva:
reportes, análisis, pronóstico y un escenario fijo de ventas, facturas y gastos.
También abre todas las pantallas y prueba en un navegador real el ingreso, el menú,
los permisos y que la venta guarde quién atendió.

**Índices (tanda 11):** `schema.sql` trae índices para las consultas frecuentes. En
una base existente se agregan con `python migracion_tanda11_indices.py`, previo
`python respaldo_produccion.py`. Solo agrega índices y no cambia datos.

Antes de la tanda 11:

Este MVP fue probado end-to-end (creación de proveedores/artículos, carga
de 2 facturas del mismo artículo con precios distintos para validar la
detección de variación, ventas con triggers de stock, gastos, y reporte de
ganancias) antes de la entrega. La base se resetea vacía para que arranques
de cero con datos reales del almacén.
