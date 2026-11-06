# Análisis y validación

Scripts que respaldan los resultados del TFG. La app no los necesita para funcionar.
Se corren desde la raíz del repositorio, por ejemplo `python analisis/validacion.py`.

- `generar_dataset_oficial.py`, `generar_demanda_verdadera.py`, `demanda_sim.py`: base simulada y demanda verdadera.
- `evaluar_pronostico.py`, `experimento_censura.py`: evaluación del pronóstico.
- `simular_politicas.py`, `analizar_politicas.py`, `simular_variantes.py`, `analizar_variantes.py`: comparación de políticas (Anexo A).
- `validacion.py`, `respaldo_produccion.py`, `exportar_esquema.py`: validación con datos reales y respaldos, solo de lectura.
- `robustez_calibracion/`: scripts, tablas y figuras del apartado Robustez y Calibración.
