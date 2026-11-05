"""
Compara dos fotos de foto.py. Uso: python comparar.py base.pkl nueva.pkl [--ignorar clave1,clave2]
Muestra, para cada resultado, si es idéntico o en qué difiere.
"""
import pickle
import sys

import numpy as np
import pandas as pd


def normalizar(x):
    if isinstance(x, pd.DataFrame):
        d = x.copy()
        for c in d.columns:
            if d[c].dtype == object:
                try:
                    d[c] = pd.to_numeric(d[c])
                except (ValueError, TypeError):
                    pass
        return d.reset_index(drop=True)
    return x


def iguales(a, b, ruta=""):
    """Devuelve lista de diferencias (texto)."""
    a, b = normalizar(a), normalizar(b)
    if isinstance(a, pd.DataFrame) and isinstance(b, pd.DataFrame):
        if list(a.columns) != list(b.columns):
            return [f"{ruta}: columnas distintas {list(a.columns)} vs {list(b.columns)}"]
        if a.shape != b.shape:
            return [f"{ruta}: filas {len(a)} vs {len(b)}"]
        try:
            pd.testing.assert_frame_equal(a, b, check_dtype=False, check_exact=False, rtol=1e-9, atol=1e-6)
            return []
        except AssertionError as e:
            return [f"{ruta}: {str(e).splitlines()[0]} ... {' '.join(str(e).split())[:300]}"]
    if isinstance(a, dict) and isinstance(b, dict):
        difs = []
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                difs.append(f"{ruta}.{k}: falta en una de las dos")
            else:
                difs += iguales(a[k], b[k], f"{ruta}.{k}")
        return difs
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        if len(a) != len(b):
            return [f"{ruta}: largo {len(a)} vs {len(b)}"]
        difs = []
        for i, (x, y) in enumerate(zip(a, b)):
            difs += iguales(x, y, f"{ruta}[{i}]")
        return difs
    if isinstance(a, (float, np.floating)) and isinstance(b, (float, np.floating, int)):
        return [] if np.isclose(a, b, rtol=1e-9, atol=1e-6, equal_nan=True) else [f"{ruta}: {a} vs {b}"]
    try:
        eq = a == b
        if isinstance(eq, (bool, np.bool_)):
            return [] if eq else [f"{ruta}: {a!r} vs {b!r}"]
    except Exception:
        pass
    return [] if repr(a) == repr(b) else [f"{ruta}: {repr(a)[:120]} vs {repr(b)[:120]}"]


def main(p1, p2, ignorar=()):
    f1, f2 = (pickle.load(open(p, "rb")) for p in (p1, p2))
    total_dif = 0
    for seccion in ("lectura", "escritura"):
        print(f"\n== {seccion.upper()} ==")
        for k in sorted(set(f1[seccion]) | set(f2[seccion])):
            if k in ignorar:
                print(f"  ~ {k}: ignorado (cambio esperado)")
                continue
            if k not in f1[seccion] or k not in f2[seccion]:
                print(f"  ✗ {k}: solo en una foto"); total_dif += 1
                continue
            difs = iguales(f1[seccion][k], f2[seccion][k], k)
            if difs:
                total_dif += 1
                print(f"  ✗ {k}")
                for d in difs[:5]:
                    print("      ", d)
            else:
                print(f"  ✓ {k}")
    for nombre, f in (("base", f1), ("nueva", f2)):
        for k, v in f["errores"].items():
            print(f"  ERROR en {nombre}: {k}: {v.strip().splitlines()[-1]}")
            total_dif += 1
    print(f"\nResultado: {'IDÉNTICO' if total_dif == 0 else f'{total_dif} diferencia(s)'}")
    return total_dif


if __name__ == "__main__":
    ign = ()
    if "--ignorar" in sys.argv:
        ign = tuple(sys.argv[sys.argv.index("--ignorar") + 1].split(","))
    sys.exit(1 if main(sys.argv[1], sys.argv[2], ign) else 0)
