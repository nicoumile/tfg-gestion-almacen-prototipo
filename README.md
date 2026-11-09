# Sistema de gestión para un almacén de barrio — prototipo

Prototipo del Trabajo Final de Grado de Nicolás Umile (Universidad Siglo 21). Es una
app web en Streamlit sobre PostgreSQL para un almacén de barrio:

- ventas de mostrador y cierre de caja;
- compras por factura, a mano o desde la planilla del lector automático de facturas;
- stock con productos fraccionables, combos y vencimientos;
- pronóstico de demanda por artículo, lista de compras y alertas de quiebre;
- reportes de ganancia, clasificación ABC, calidad de datos y desempeño de proveedores;
- gastos, historial de cambios, usuarios y permisos;
- instrumentos para la validación con el comerciante, entre ellos el Desafío Recreo.

Las bases de `datos/` tienen ventas simuladas sobre el catálogo real del comercio,
publicado con su autorización. El repositorio no incluye credenciales: cada instalación configura su propia base.

## Estructura del repositorio

| Carpeta | Contenido |
|---|---|
| Raíz | Lo necesario para correr la app: `app.py`, sus módulos, `schema.sql`, `requirements.txt` y `preparar_demo.py` |
| `datos/` | Bases de demostración y catálogo |
| `analisis/` | Simulación, evaluación del pronóstico, comparación de políticas, validación y robustez |
| `superados/` | Migraciones, kit de pruebas y utilidades históricas; la app no los necesita |

Todos los comandos se corren desde la raíz del repositorio.

## Módulos

- `schema.sql`: esquema completo de PostgreSQL, con tablas, vistas, índices y triggers. Los triggers actualizan el stock y el costo al cargar una compra o una venta.
- `app.py`: interfaz Streamlit con 15 pantallas, agrupadas en el menú por secciones (Mostrador, Resumen y análisis, Compras, Stock, Administración).
- `db.py`: acceso a datos y lógica de negocio. Calcula precios, alertas, reportes de ganancia contra gastos, calidad de datos, stock diario reconstruido y lotes de vencimiento.
- `pronostico.py`: pronóstico de demanda por artículo y elección del modelo por clase ABC.
- `lista_compras.py`: lista de compras, alertas de quiebre y riesgo de vencimiento.
- `proveedores.py`: desempeño de proveedores y consignación.
- `desafio.py` y `figuritas.py`: Desafío Recreo.
- `importador_planilla.py`: importación de facturas desde la planilla del lector automático.
- `preparar_demo.py`: deja lista la base de demostración.
- `requirements.txt`: dependencias de Python.

## Instalación con la base de demostración

Esta instalación usa la base simulada oficial (12 meses, semilla 42) sobre el esquema actual.

Requisitos:

- Python de 3.12 a 3.14; se recomienda 3.13. numpy 2.5 exige 3.12 y pandas 3.0.5 no trae instalador para 3.15.
- PostgreSQL 18. El respaldo de `datos/` se generó con pg_dump 18, y un pg_restore anterior puede rechazarlo.
- Git.

```bash
pip install -r requirements.txt
createdb -U postgres almacen
pg_restore -U postgres -d almacen --no-owner datos/base_demo_entregable4.dump
```

Copiar `.streamlit/secrets.toml.example` como `.streamlit/secrets.toml` y completar `DB_HOST = "localhost"`, `DB_PORT = 5432`, `DB_NAME = "almacen"`, `DB_USER = "postgres"` y `DB_PASSWORD`. Después:

```bash
python preparar_demo.py
streamlit run app.py
```

`preparar_demo.py` hace lo siguiente:

- Corre las fechas de la base para que la última venta quede ayer. Si no, el pronóstico ve un mes sin ventas.
- Carga datos de ejemplo simulados para las pantallas que la simulación deja vacías:
  - gastos mensuales, marcados "dato de ejemplo";
  - un segundo precio de proveedor para parte de los artículos, para comparar en Proveedores → Precios.
- Suma el pan de compra diaria (PAN NEWEN 500G) y las milanesas de pollo, con la configuración que tienen en el negocio real y un año de historial simulado: compras, ventas, sobrante y vencidos.
- Crea el usuario de demostración: usuario `demo`, contraseña `demo-tfg-2026`, rol dueño. Con `--usuario` y `--clave` se eligen otros.
- Se niega a correr contra la base de producción.

## Instalación desde cero (base vacía)

1. Crear la base de datos y aplicar el esquema, con PostgreSQL ya corriendo:
   ```bash
   createdb almacen
   psql -d almacen -f schema.sql
   ```

2. Instalar las dependencias de Python:
   ```bash
   pip install -r requirements.txt
   ```

3. Configurar la conexión, si tu PostgreSQL no usa los valores por defecto. La app busca primero en `st.secrets` y, si no encuentra nada, usa variables de entorno:

   - **Recomendado:** copiar `.streamlit/secrets.toml.example` a `.streamlit/secrets.toml` y completar `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` y `DB_PASSWORD`. Ese archivo está en `.gitignore` y nunca se sube al repositorio.
   - **Alternativa:** variables de entorno:
     ```bash
     export DB_HOST=localhost
     export DB_PORT=5432
     export DB_NAME=almacen
     export DB_USER=postgres
     export DB_PASSWORD=tu_password
     ```

   Si no configurás nada, la app usa esos mismos valores por defecto, pensados para un PostgreSQL local de desarrollo.

   En Streamlit Community Cloud no se usa ningún archivo: las mismas claves se cargan en el panel "Secrets" de la app y apuntan a la base en la nube (Neon o Supabase).

4. Correr la app:
   ```bash
   streamlit run app.py
   ```
   Se abre en el navegador en `http://localhost:8501`.

`schema.sql` ya incluye todas las tablas. Las migraciones de `superados/migraciones/` solo sirven para actualizar una base creada con una versión anterior, siempre con un respaldo previo (`python analisis/respaldo_produccion.py`).

## Pantallas

**Mostrador**
- **Registrar Venta:** carrito por artículo, en unidades o en kilos, con medio de pago. El stock se descuenta al confirmar la venta. Tiene además una pestaña para el sobrante del día (lo regalado, tirado o vencido) y otra para corregir o eliminar ventas.
- **Cierre de Caja:** arqueo del efectivo por medio de pago y cierre del día guardado.

**Resumen y análisis**
- **Dashboard:** calidad de datos, ganancia del mes, alertas de quiebre antes del próximo viaje, vencimientos, stock bajo y variación de precios.
- **Reportes:** ganancia bruta y neta por período, clasificación ABC, márgenes reales, stock inmovilizado, calidad de datos y exportación a Excel.
- **Pronóstico de demanda:** venta esperada de cada artículo en los próximos 7 y 14 días, días que alcanza el stock, error de cada clase ABC y seguimiento del pronóstico.

**Compras**
- **Cargar Factura (Compra):** desde la planilla del lector automático o a mano. Actualiza el stock y el costo vigente con triggers, y permite eliminar facturas.
- **Lista de compras:** compra diaria (pan) y pedido para el viaje al mayorista, armados con el pronóstico. Incluye el registro de viajes y de listas guardadas.
- **Proveedores:** desempeño, consignación y comparación de precios.

**Stock**
- **Artículos:** alta, edición, artículos activos e inactivos, y listado con costo, precio y margen calculados.
- **Fraccionar:** pasa stock de una horma o bolsa a sus fracciones, o del pan del día al pan de ayer.
- **Promociones:** combos y su precio.
- **Desafío Recreo:** control diario de vencimientos en góndola, armado como juego.

**Administración**
- **Gastos:** alquiler, servicios, impuestos y otros, separados de las compras de mercadería.
- **Historial de Cambios:** quién cambió qué y cuándo, en hora argentina.
- **Configuración:** usuarios y permisos, parámetros de reposición y días disponibles para la venta por categoría. Solo la ve el dueño.

## Cómo se calcula cada cosa

- `costo_venta_sin_iva = costo_compra × (1 + margen)`
- `costo_venta_con_iva = costo_venta_sin_iva × (1 + iva)`
- **Variación de precio:** compara cada compra con la anterior del mismo artículo y proveedor (con `LAG` en SQL) y calcula el porcentaje de variación.
- **Stock bajo:** `stock_actual <= stock_minimo`.
- **Vencimiento cercano:** `fecha_vencimiento` dentro de los próximos 15 días.
- **Lista de compras:** pronóstico más un stock de seguridad por clase ABC (ver "Lista de compras" y `lista_compras.py`).

## Ventas y productos fraccionables

- **Unidad de medida:** cada artículo se vende por `unidad` o por `kg`. Los productos por kilo se compran y venden con cantidades decimales: una horma de 4,5 kg se carga como cantidad 4.5 en la factura, y una venta de 500 g como 0.5. El stock, el costo y el precio de esos artículos quedan expresados por kilo. Al crear un artículo se elige si es fraccionable, con una sugerencia automática si la categoría contiene palabras como "fiambre", "queso" o "embutido".
- **Carrito:** se agregan productos, el total se acumula en pantalla con el precio ya calculado (margen más IVA) y recién al confirmar se descuenta el stock de todos los ítems juntos, como en una caja registradora. Los ítems de una venta quedan agrupados por `ticket_id`.
- **Toda venta lleva su artículo:** en el mostrador no hay código de barras, así que la venta se arma con botones de los artículos más vendidos y un buscador. No hay venta por monto libre, porque una venta sin producto queda fuera del ABC, los márgenes, la reposición y la calidad de datos.
- **Medio de pago** en cada venta: efectivo, tarjeta, transferencia u otro.
- **Editar o eliminar ventas:** los triggers `trg_editar_venta` y `trg_revertir_venta` ajustan el stock en ambos casos.
- **Eliminar facturas:** el trigger `trg_revertir_compra` resta el stock que la factura había sumado. No revierte el `costo_compra_actual` del artículo; si era la última compra, conviene revisar el precio a mano.
- **Coincidencia aproximada de artículos:** al cargar una factura se puede escribir el nombre tal como figura en el documento del proveedor. El sistema busca coincidencias por similitud de texto (`pg_trgm`) y, al confirmar, guarda el alias (`articulo_alias`) para reconocerlo solo la próxima vez.

## Controles al cargar

- **Bloqueo de ingreso:** después de 5 contraseñas incorrectas seguidas, ese usuario queda bloqueado 1 minuto. Cada bloqueo seguido dura el doble, hasta 15 minutos. Se guarda en la base (`intentos_ingreso`), así que recargar la página no lo saltea.
- **Cantidades fuera de lo normal:** en Registrar Venta, antes de confirmar, un renglón pide "Las cantidades están bien" si:
  - vale $150.000 o más;
  - es 20 veces o más lo que se vende normalmente de ese artículo;
  - no hay historial del artículo y son 50 unidades o más, o 10 kg o más.

  El caso que lo motivó fueron 140 "unidades" de paleta.
- **Stock sumado a mano antes de la factura:** al cargar una factura, avisa si en los últimos 7 días se subió a mano el stock de ese artículo en una cantidad parecida. Si se tilda "era esta mercadería", se descuenta esa suma y el stock queda contado una sola vez (en el Historial figura como AJUSTE_FACTURA).
- **Pendientes de ajuste:** no lista los combos de stock compartido, porque su stock se calcula a partir de los componentes.

## Usuarios y permisos

- **Ingreso con usuario y contraseña.** Quien entra es quien atiende: cada venta guarda `ventas.registrado_por` y cada cambio guarda `historial_cambios.usuario`. Las contraseñas se guardan con hash PBKDF2-SHA256 y sal, nunca en texto plano.
- **Roles:**
  - *Dueño:* ve todas las pantallas y es el único que entra a Configuración.
  - *Atención:* ve solo las pantallas que el dueño le habilita (por defecto, Registrar Venta) y no puede editar ni eliminar ventas ya cargadas.
- **Configuración** sirve para:
  - crear usuarios, habilitar pantallas, asignar contraseñas y desactivar usuarios (no se borra nada);
  - fijar los parámetros de reposición: días entre viajes al mayorista y nivel de servicio por clase ABC.

  Solo el dueño cambia contraseñas: la suya desde el menú lateral y la de cualquier usuario desde Configuración.
- **Sesión recordada:** al entrar, el navegador guarda una cookie firmada (`almacen_sesion`) que vence a las 5 de la mañana del día siguiente. Recargar la página o abrir otra pestaña no pide volver a entrar.
  - La firma usa una clave derivada de la contraseña de la base (o `SESION_SECRETO` en los secrets, si se define) e incluye una huella de la contraseña de la persona. Cambiar esa contraseña o desactivar a la persona invalida la sesión.
  - **Salir** borra la cookie.
- **Menú por secciones:** cada pantalla tiene su dirección (por ejemplo `/venta`, `/caja`, `/lista-de-compras`; la de inicio es la raíz), y al recargar la app se queda en la misma. Atención solo ve, y solo puede abrir por dirección, las pantallas habilitadas.
- **Seguridad:** la URL de la app es pública; la protección es la contraseña.

## Pronóstico de demanda por artículo

- **Pantalla:** cuánto se espera vender de cada artículo en los próximos 7 y 14 días, cuántos días alcanza el stock actual y el gráfico de ventas reales contra el pronóstico. Con historia suficiente (unos 48 días) muestra también el error de cada clase ABC comparado con el promedio de 30 días. La ve el dueño y se puede habilitar a otros usuarios.
- **Lógica en `pronostico.py`** (pandas y numpy, con 2 consultas en total en vez de una por artículo):
  - **Demanda diaria:** sale de `v_ventas_analitica`, con los combos resueltos en sus componentes, el consumo interno incluido, los sueltos fraccionados contados en su bolsa y los reemplazos sumados al artículo vigente.
  - **Modelos:** promedio de 30 días (referencia), suavizado exponencial simple y Croston-SBA, ajustados por día de la semana y bloque del mes.
  - **Elección:** la configuración se elige por clase ABC con un backtest de origen móvil (60 días, horizonte de 7 días, sin fuga de datos).
  - **Historia corta:** con menos de 48 días se usa el suavizado con α = 0,05. Los artículos con menos de 28 días se mezclan con el promedio de su categoría, y los que nunca se vendieron pronostican cero.
- **Demanda censurada:** un día sin venta en que el artículo arrancó sin stock no cuenta como demanda cero, porque no se vendió por falta de mercadería.
  - El stock de cada día se reconstruye hacia atrás desde el stock actual, con compras, ventas, combos, fraccionamientos y correcciones manuales.
  - No se corrigen los artículos cuyo stock reconstruido queda negativo (faltan compras cargadas) ni los que tienen menos de 7 días con stock.
  - En la base simulada, con 13 % de la demanda perdida por quiebres, la corrección baja el sesgo contra la demanda verdadera de −15,6 % a −4,5 % en la clase A (`analisis/experimento_censura.py`).
- **Pronóstico guardado:** la primera vez que el dueño entra cada día se guarda el pronóstico de los próximos 14 días en `pronostico_snapshot`, para medir el error real.
- **Error sin sesgo de selección:** además del error con el que se elige el modelo, la pantalla muestra el error de un modelo elegido con la primera mitad de los días de prueba y medido con la segunda.
- El error se mide contra lo vendido. Con quiebres, lo vendido es menos que la demanda, así que un pronóstico que corrige la censura puede aparecer "por encima".
- **`analisis/evaluar_pronostico.py`** reproduce la evaluación (tablas CSV y figura). Por ejemplo, `python analisis/evaluar_pronostico.py 2026-10-05` usa los datos de la base simulada hasta el día anterior a esa fecha.

### ¿Se cumplió el pronóstico?

- **Resumen por clase:** lo vendido contra lo pronosticado, sesgo y error semanal de los últimos 28 días. Cada día pasado se compara con lo que se pronosticaba ese día con las ventas hasta el día anterior: el pronóstico guardado o, si no hay, uno reconstruido con el modelo actual.
- **Gráfico:** unidades por día de todos los artículos o de una clase, con lo vendido, lo pronosticado ese día y, después de hoy, el pronóstico de los próximos 14 días.
- **Detalle de artículo:**
  - **Tabla día por día.**
  - **Panel de stock**, con el mismo eje de fechas: el stock al final de cada día, el stock proyectado si no se compra y, en rojo, los días que terminaron sin stock.
  - **Compra diaria:** un gráfico de barras con lo disponible cada día (lo que quedó del anterior más lo comprado), partido en vendido, tirado o regalado, lo que pasó a su producto "de ayer", el faltante de un conteo y lo que quedó para el día siguiente, con la línea del pronóstico.
- **Pan de ayer:** el pan que sobra se pasa cada mañana a "PAN DE AYER NEWEN 500G" con Fraccionar, que mueve stock y costo sin gasto falso ni corrección manual. Lo que no se vende al día siguiente se carga como sobrante. Para el pronóstico, lo vendido de pan de ayer cuenta como demanda de pan y lo tirado no.

## Lista de compras

La lista usa el pronóstico de cada artículo y separa tres partes:

- **Compra diaria** (artículos marcados así, como el pan que se compra todos los días): busca la cantidad que más ganancia deja en promedio (problema del vendedor de diarios).
  - Compara lo que se gana por unidad vendida con lo que se pierde por unidad que sobra, con precio y costo con IVA. Con precio $2.000, costo $1.000 más IVA y el sobrante regalado, el cociente es (2.000 − 1.210) / 2.000 ≈ 0,40, así que conviene tener un poco menos que la venta esperada del día (percentil 40).
  - Muestra el sobrante esperado y la probabilidad de agotarse, y se puede fijar un nivel de servicio propio.
  - **Ya tenés:** si el artículo vence en el día, cuenta solo lo comprado hoy que sigue en stock, y solo para la compra de hoy.
- **Viaje al mayorista** (el resto):
  - **Cantidad:** venta esperada hasta el próximo viaje, más un margen de seguridad según la clase ABC (A 95 %, B 90 %, C 80 %), menos el stock, redondeado al bulto del proveedor.
  - **Perecederos:** si el artículo vence antes del próximo viaje, la lista cubre solo esos días.
  - **Ajustes:** se puede corregir la cantidad o destildar renglones.
  - **Para llevar:** se puede copiar el texto para WhatsApp agrupado por proveedor, con el costo estimado con IVA.
- **Artículos de compra diaria y perecederos:** marcar compra diaria, vida útil y nivel de servicio propio. Sugiere los artículos que se compran seguido.

El margen de seguridad usa la raíz de la venta esperada, corregida por la dispersión medida en la evaluación del pronóstico de cada clase.

### Lista guardada y registro de viajes

- **"Me voy al mayorista con esta lista"** guarda la lista del viaje tal como quedó: lo que sugirió la app y lo que se llevó, con las cantidades corregidas y los renglones destildados. Hay una lista por día; guardar de nuevo reemplaza la de hoy.
- **Registro de viajes:** cada viaje se anota con fecha, si fue planificado o de urgencia, horas, con qué lista fue y una nota.
- **¿Se compró lo de la lista?:** compara cada lista con las facturas de los días que cubre.
  - Cada renglón queda como: comprado igual (±10 %), otra cantidad, no se compró, sacado, sacado pero se compró, o agregado.
  - No cuentan la compra diaria ni la consignación.

## Alertas de quiebre y vencimientos

- **Alertas de quiebre** (Dashboard): qué artículos se quedan sin stock antes del próximo viaje al mayorista, según el pronóstico.
  - **Próximo viaje:** la fecha más reciente entre la última compra al mayorista y el último viaje planificado anotado, más los días entre viajes de Configuración. Si esa fecha ya pasó, se toma hoy.
  - **Semáforo:** 🔴 es más probable agotarse que no antes del próximo viaje; 🟡 llega al viaje pero no alcanza hasta el siguiente; 🟢 alcanza. Muestra la venta en riesgo en pesos.
  - **Probabilidad de agotarse:** se calcula con una binomial negativa con la dispersión que mide la evaluación (la misma del stock de seguridad). Para lo que se vende por kilo se usa una Gamma.
  - **"Conviene adelantar el viaje"** aparece cuando 3 o más artículos de clase A están en rojo.
  - **Fuera de las alertas:** quedan el pan y lo de compra diaria, y los artículos con stock negativo, porque primero hay que corregirles el stock.
  - **Código y frecuencia:** la lógica está en `lista_compras.alertas_quiebre` y se recalcula cada 15 minutos.
- **Días disponibles para la venta:** se fijan por artículo (Lista de compras) o por categoría (Configuración, tabla `vida_util_categoria`). Si están las dos, manda la del artículo.
- **Lotes implícitos:** cada renglón de factura es un lote. Vence en la fecha cargada en el renglón (columna opcional "Vence") o, si no, en la fecha de la factura más los días disponibles. Se supone que se vende primero lo más viejo (`db.lotes_vencimiento`).
- **Riesgo de vencer** (`lista_compras.riesgo_vencimiento`): para cada lote calcula la venta esperada hasta que vence, la probabilidad de que sobre algo y las unidades que se tirarían. Se ve en Dashboard → Vencimientos.
- **Motivo "Vencido"** en Registrar Venta → Sobrante del día: baja stock como consumo interno y registra el costo como gasto "Vencido". No cuenta como demanda para el pronóstico.
- **Clase heredada de los combos:** en la lista de compras, las alertas y el riesgo de vencimiento, cada componente de un combo usa la mejor clase entre la suya y la de los combos que integra. Solo sube de clase, nunca baja.

## Importar facturas desde la planilla

- **Origen de las facturas:** en Cargar Factura, el modo "Desde la planilla" lista las facturas que el lector automático (Drive + Apps Script + Gemini) ya leyó y se confirmaron en la planilla "Registro de facturas" del comerciante, y las carga como compras con un clic.
- **Qué pasa a la app:**
  - **Proveedor:** se reconoce por CUIT; si no existe, se crea.
  - **Datos de la factura:** número, fecha y link al PDF.
  - **Renglones:** por cada renglón que es stock, la cantidad y el costo unitario sin IVA.
  - **Lo que queda en la planilla:** CAE, IVA por renglón y validaciones.
- **Vinculación de artículos:**
  - **Orden de búsqueda:** primero por el código del proveedor (hoja "Diccionario" de la planilla), después por alias exacto y por último por similitud.
  - **Factor del alias:** el alias guarda cuántas unidades del artículo trae cada unidad facturada. Por ejemplo, una caja de 60 que se vende suelta tiene factor 60.
  - **Escritura en la planilla:** al importar, la app escribe en el Diccionario el artículo vinculado y el factor.
- **Sin duplicados:** cada factura importada guarda `ref_externa = 'planilla:C-000xx'`, única en la base, y se marca en la planilla. Si ya existe la misma factura cargada a mano, avisa y permite marcarla sin volver a sumar stock.
- **Origen distinguible:** las facturas importadas llevan `origen = 'llm'`, para distinguirlas de las cargadas a mano.
- **Configuración:** requiere configurar en *Secrets* `PLANILLA_FACTURAS_ID` y `[gcp_service_account]` (ver `.streamlit/secrets.toml.example` e `importador_planilla.py`). Sin esa configuración, la carga a mano funciona igual.

## Proveedores

La pantalla tiene tres pestañas.

**Desempeño** (`proveedores.py`): una fila por proveedor para el período elegido.
- Compras sin IVA, porcentaje del total, cantidad de facturas y cada cuántos días se le compra.
- Venta neta, ganancia real y margen de sus productos.
- Stock a costo, días de cobertura y rendimiento por peso en stock (GMROI anualizado).
- Aumento mensual de costo, sobreprecio frente al más barato y porcentaje de días sin stock.

Cada artículo se reparte entre sus proveedores según cuánto se le compró a cada uno. Lo que nunca tuvo factura queda en "Sin factura de compra". La ganancia total cuadra con Márgenes reales y el stock con el valor de la mercadería. No se mide el plazo de entrega, porque la app no registra pedidos.

**Consignación:** proveedores que dejan mercadería y cobran recién lo vendido.
- La reposición se carga como una factura.
- La pestaña muestra cuánto se le debe: el costo de lo vendido desde la última liquidación, incluidas promociones y consumo interno.
- Permite registrar el pago. El pago **no** va en Gastos, porque ese costo ya está en la ganancia.
- Muestra el stock para contarlo junto con el proveedor.

**Precios:** comparación del costo de cada artículo entre proveedores.

## Reportes: stock inmovilizado y artículos inactivos

- **Stock inmovilizado** (Reportes → Clasificación ABC), en dos grupos: lo que **nunca se vendió** desde que se cargó y lo que **se vendió alguna vez pero no en los últimos N días**.
  - **Qué cuenta como venta:** incluye combos y consumo interno, y para una bolsa también lo vendido de su suelto.
  - **Qué no suma:** lo cargado hace menos de 7 días y lo que está en consignación.
  - **Código:** la lógica está en `db.stock_inmovilizado`.
- **Artículos inactivos:** en Artículos → Editar artículo, el interruptor **Artículo activo** permite dar de baja un artículo, con su motivo (discontinuado, reemplazado o temporal).
  - **Qué se conserva:** no se borra nada; sus ventas y compras siguen en los reportes y se puede reactivar.
  - **Dónde deja de aparecer:** en la venta, las facturas, las promociones y el fraccionamiento.
  - **Qué hereda el reemplazo:** si fue reemplazado, el artículo nuevo suma la venta reciente del anterior para la reposición.

## Hora argentina

Las fechas y horas se guardan en **UTC** y se muestran en hora argentina:

- **Hoy en las consultas:** "hoy" es `db.HOY_SQL`, el día argentino. `CURRENT_DATE` en UTC pasa a mañana después de las 21 h.
- **Historial:** filtra por día argentino.
- **Correcciones de noche:** las correcciones de stock hechas de noche cuentan para su día argentino en la reconstrucción del stock.

## Desafío Recreo: control de vencimientos en góndola

- **Por qué:** las facturas no traen vencimientos, así que la fecha real se mira en la góndola. Para que el control diario no sea una carga, se arma como un juego con la temática del almacén ("Recreo").
- **Tarea del día** (`desafio.armar_tarea`): 6 artículos con stock que vencen.
  - **Cuáles:** la mitad son perecederos y el resto se elige por plata en stock.
  - **Repetición:** un mismo artículo no vuelve antes de 7 días.
  - **Repaso:** a veces repite un artículo ya controlado para verificar la fecha anterior.
- **Control** (`desafio.registrar_control`): la fecha más cercana que se ve, la cantidad (opcional), "No hay" o "No vence".
  - **Vencimientos:** el último control con fecha pasa al lote más viejo del artículo.
  - **Vida útil real:** `desafio.dias_tipicos` mide los días disponibles reales por categoría, que se pueden copiar a Configuración.
- **Estrellitas:**
  - **Por control:** +10, o +15 si es perecedero.
  - **Tarea completa:** +20.
  - **Racha:** desde 5 días de racha, las estrellitas del día valen ×1,5.
  - **Boletín de la semana:** +100.
  - **Niveles:** Buen alumno, Escolta y Abanderado.
- **Figuritas** (`figuritas.py`, pixel art propio en SVG). Los premios los define el dueño en la pestaña de Configuración del desafío, que solo ve él.
- **Libreta con cuatro materias**, con nota semanal de 1 a 10. La regla es premiar la acción que la app puede verificar, nunca el indicador.
  - **Góndola:** la tarea de vencimientos, con un **conteo a ciegas** en algunos artículos que mide la exactitud del inventario.
  - **Orden ("Arreglá un dato"):** uno por día de la tabla Qué corregir del indicador de calidad, verificado por la app.
  - **Excursión:** guardar la lista, anotar el viaje y cumplir la lista.
  - **Cuentas:** guardar el cierre de caja del día con el efectivo contado.
- **Recreo con clientes:** después de una venta grande, Registrar Venta pregunta "¿Jugaron a las monedas?". Las monedas de chocolate salen del stock como regalo a costo.

## Validación con el comerciante

`analisis/validacion.py` calcula los indicadores semana por semana (de lunes a domingo) a partir de los CSV de un respaldo. No se conecta a la base.

- **Uso del sistema:** porcentaje de ventas con "Atendió", días sin ventas y correcciones manuales de stock.
- **Stock y datos:** porcentaje de días sin stock en clase A y calidad de datos.
- **Pan y viajes:** pan comprado, vendido y tirado por día, y viajes al mayorista.
- **Pronóstico y listas:** error real del pronóstico guardado a 7 días por clase (WAPE y sesgo) y listas guardadas.
- **Desafío Recreo:** tareas cumplidas, conteos y exactitud del inventario, datos arreglados y cierres de caja.
- **Foto del momento:** stock negativo y capital parado.

Los CSV quedan en `respaldos/validacion/`, que no va al repositorio.

## Comparación de políticas de reposición (HU-021)

Responde si comprar con la lista de compras de la app conviene más que como se compraba. No cambia la app ni la base: son tres scripts que corren aparte.

- **Demanda verdadera** (`analisis/demanda_sim.py`): un año de lo que los clientes quieren llevar, por día y artículo, sobre el catálogo de `datos/`. Incluye lo que no se vendería por falta de stock. Tiene dos escenarios: popularidad **estable** y **cambiante** (paseo aleatorio, artículos nuevos y discontinuados, saltos).
- **Simulación** (`analisis/simular_politicas.py`): la misma demanda se resuelve contra el stock de cada política. La venta es el mínimo entre demanda y stock; lo que falta es venta perdida. Hay un viaje por semana (los lunes). Los primeros 90 días todas usan la regla actual, y se evalúa del día 91 al 365. Las políticas ABC y por pronóstico solo ven lo vendido, como la app. Se comparan tres políticas:
  1. **Regla actual:** si el stock baja de max(mínimo, 2), reponer hasta max(stock inicial, umbral × 4, 8). Es la regla con la que se generó la base simulada oficial.
  2. **ABC fija:** cubrir la venta promedio de 90 días por los 7 días del viaje, más días de seguridad por clase (A 15, B 8, C 3), sin pronóstico.
  3. **Por pronóstico:** la lista de compras de la app, sin cambios.
- **Informe** (`analisis/analizar_politicas.py`): por escenario y política, la mediana y el rango p5–p95 entre semillas de **ventas perdidas** (porcentaje de la demanda en pesos), **nivel de servicio** (porcentaje de unidades atendidas) y **capital inmovilizado** (stock promedio a costo), también por clase. Las diferencias se miden semilla por semilla.
- **Resultado (40 años simulados):**
  - **Ventas perdidas:** la política por pronóstico pierde el **3,3–3,5 %**, contra el **12,9–15,4 %** de la regla actual y el **4,1–4,5 %** de la ABC fija.
  - **Nivel de servicio:** sirve el **95 %** de lo pedido, contra el 87–89 % y el 90–91 %.
  - **Capital inmovilizado:** **16–22 % menos** que la regla actual y **17–20 % menos** que la ABC.
  - **Consistencia:** mejora en los 40 años contra las dos.
  - **Por clase:** la ABC fija protege la clase A (98 %) a costa de la C (80 %); el pronóstico queda parejo (A 97 %, B 95 %, C 92–93 %).
- **Uso:**
  ```
  python analisis/simular_politicas.py estable 1,2,...,20 resultados/corridas      (~30 s por semilla)
  python analisis/simular_politicas.py cambiante 1,2,...,20 resultados/corridas
  python analisis/analizar_politicas.py resultados/corridas resultados
  ```

## Alcance

- **Fuera del alcance de este prototipo:**
  - ajustar la lista de compras al espacio del depósito (quedan sin uso `articulos.volumen_litros` y el parámetro `capacidad_litros`);
  - comparar canales de compra por costo puesto;
  - relevar precios sin compra.
- **Límites conocidos:**
  - la venta no usa código de barras ni QR;
  - para cambiar un ítem de una factura ya cargada hay que borrarla y volver a cargarla;
  - los reportes se exportan a Excel, no a PDF.

## Pruebas

Antes de cada versión, los cambios se prueban con el kit de `superados/pruebas/` (ver su `README.md`) sobre una copia local armada desde un respaldo. El kit compara el comportamiento de la versión anterior y la nueva:

- **Comparación:** reportes, análisis, pronóstico y un escenario fijo de ventas, facturas y gastos.
- **Navegador:** abre todas las pantallas y prueba en un navegador real el ingreso, el menú, los permisos y que la venta guarde quién atendió.
