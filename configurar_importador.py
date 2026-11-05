"""
Configura el importador de facturas en .streamlit/secrets.toml (uso local).

Lee el .json de la cuenta de servicio de Google y lo agrega, con el ID de la
planilla "Registro de facturas", al final de .streamlit/secrets.toml. Antes
hace una copia de seguridad (secrets.toml.bak). Los datos de la base (DB_*)
no se tocan.

Uso (desde la carpeta TFG):

    python configurar_importador.py "C:\\ruta\\al\\archivo.json" "ID_DE_LA_PLANILLA"

El ID de la planilla es lo que va entre /d/ y /edit en su URL:
    https://docs.google.com/spreadsheets/d/ESTE_ES_EL_ID/edit

Después, para Streamlit Community Cloud: abrí .streamlit/secrets.toml y
copiá TODO su contenido (ya incluye los DB_* de siempre) en el panel Secrets
de la app (Manage app -> Settings -> Secrets), reemplazando lo que había.
"""
import json
import os
import shutil
import sys

RUTA_SECRETS = os.path.join(".streamlit", "secrets.toml")
CAMPOS = ["type", "project_id", "private_key_id", "private_key", "client_email", "client_id",
          "auth_uri", "token_uri", "auth_provider_x509_cert_url", "client_x509_cert_url", "universe_domain"]


def toml_str(valor):
    # Las cadenas JSON con comillas dobles son cadenas básicas válidas en TOML (mismos escapes: \n, \", \\).
    return json.dumps(str(valor), ensure_ascii=False)


def linea_planilla(planilla_id):
    # Va al PRINCIPIO del archivo: en TOML, una clave escrita debajo de una sección [algo]
    # pertenece a esa sección, y la app no la encontraría.
    return ("# --- Importar Facturas (agregado por configurar_importador.py) ---\n"
            f"PLANILLA_FACTURAS_ID = {toml_str(planilla_id)}\n\n")


def bloque_cuenta_servicio(cred):
    lineas = ["", "[gcp_service_account]"]
    for campo in CAMPOS:
        if campo in cred:
            lineas.append(f"{campo} = {toml_str(cred[campo])}")
    return "\n".join(lineas) + "\n"


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    ruta_json, planilla_id = sys.argv[1], sys.argv[2].strip()
    if "/d/" in planilla_id:  # pegaron la URL completa en vez del ID
        planilla_id = planilla_id.split("/d/")[1].split("/")[0]

    with open(ruta_json, encoding="utf-8") as f:
        cred = json.load(f)
    if cred.get("type") != "service_account" or "private_key" not in cred:
        print("*** Ese archivo no parece la clave JSON de una cuenta de servicio. ***")
        sys.exit(1)

    os.makedirs(".streamlit", exist_ok=True)
    actual = ""
    if os.path.exists(RUTA_SECRETS):
        with open(RUTA_SECRETS, encoding="utf-8") as f:
            actual = f.read()
        if "[gcp_service_account]" in actual or "PLANILLA_FACTURAS_ID" in actual:
            print("*** secrets.toml ya tiene la configuración del importador. ***")
            print("Si querés reemplazarla, borrá a mano esas líneas y volvé a correr el script.")
            sys.exit(1)
        shutil.copyfile(RUTA_SECRETS, RUTA_SECRETS + ".bak")

    nuevo = linea_planilla(planilla_id) + actual
    if actual and not actual.endswith("\n"):
        nuevo += "\n"
    nuevo += bloque_cuenta_servicio(cred)
    with open(RUTA_SECRETS, "w", encoding="utf-8") as f:
        f.write(nuevo)

    # Verificación: el archivo tiene que seguir siendo TOML válido y tener los datos.
    try:
        import tomllib
    except ImportError:
        tomllib = None
    if tomllib:
        with open(RUTA_SECRETS, "rb") as f:
            datos = tomllib.load(f)
        assert datos["PLANILLA_FACTURAS_ID"] == planilla_id
        assert datos["gcp_service_account"]["private_key"] == cred["private_key"]
    print("Listo: configuración agregada a .streamlit/secrets.toml (copia anterior en secrets.toml.bak).")
    print(f"Cuenta de servicio: {cred.get('client_email')}")
    print("Recordá compartir la planilla con ese mail como Editor.")


if __name__ == "__main__":
    main()
