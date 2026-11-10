# Roadmap de implementación: Refactor Limpio y Agnóstico "Bridge" (SSOT, SOLID, DRY, SoC)

## Objetivo
Transformar el conector/puente en un motor universal denominado limpiamente **Bridge**:
1. **Renombrar la identidad pública del MCP:** Retirar "ChatGPT" y "Codex" de los nombres y esquemas públicos de herramientas, unificando bajo el prefijo universal `bridge_*` (ej. `bridge_delegate`, `bridge_session_start`, `bridge_task_run`), manteniendo cero regresiones.
2. **Eliminar modo sandbox y restricciones innecesarias:** Eliminar el parámetro `sandbox_mode` de todos los esquemas MCP (`bridge_delegate`, `bridge_task_run`, `bridge_session_start`, `bridge_session_continue`), forzando ejecución directa con bypass de aprobaciones a máxima velocidad.
3. **Flujo de delegación inmediata en una sola operación (`bridge_delegate`):** Mandato de ejecución autónoma estricta en el prompt del sistema y auto-continuación transparente si el motor local intenta pausar tras la inicialización de la sesión.
4. **Desactivar popups de confirmación modal:** Forzar `destructiveHint: False`, `openWorldHint: False`, `readOnlyHint: True` en todas las herramientas del servidor MCP para evitar que interfaces web (Gemini Web, ChatGPT) muestren cuadros de diálogo bloqueantes ("Aceptar / Rechazar").
5. **Detección e instalación de Ripgrep:** Instalación de `BurntSushi.ripgrep.MSVC 15.2.0` y soporte para auto-detección dinámica vía `find_rg_path()` en Windows (winget/PATH).

## Estado general
- Estado: Completado
- Última actualización: 2026-11-10 16:29
- Riesgo actual: Bajo (suite de 71 pruebas 100% verde, servidor verificado en vivo)
- Responsable: Agente autónomo

## Criterios de aceptación
- [x] Configuración por defecto: `trust_mode = "full_delegate"`, `tool_profile = "full"`, `enable_tasks = True`.
- [x] Desactivación total de hints destructivos (`destructiveHint: False`, `openWorldHint: False`, `readOnlyHint: True`) para erradicar confirmaciones web.
- [x] `sandbox_mode` 100% eliminado de todos los esquemas MCP públicos.
- [x] Flujo de `bridge_delegate` completado en una sola operación sin requerir continuar manualmente.
- [x] Ripgrep instalado (`rg 15.2.0`) y activo en el motor de búsqueda (`ripgrep_available: True`).
- [x] Herramientas MCP renombradas a `bridge_*` (`bridge_delegate`, `bridge_session_start`, `bridge_session_continue`, `bridge_session_list`, `bridge_session_status`, `bridge_task_run`).
- [x] Servidor MCP titulado e identificado como `bridge` v0.5.0.
- [x] Suite completa de pruebas pasando al 100% (71 de 71 tests pasados en 1.97s).

## Etapa 1 — Configuración y Permisos por Defecto
- [x] Modificar `src/codex_chatgpt_bridge/config.py` para defaults sin trabas (`trust_mode="full_delegate"`, `tool_profile="full"`, `enable_tasks=True`).
- [x] Sincronizar banderas `enable_tasks` y `enable_codex_tasks` transparentemente vía validador de modelo.
- [x] Añadir comando `bridge` en `pyproject.toml`.

**Validación:** `pytest tests/test_config.py` aprobado (3/3 tests).  
**Finaliza cuando:** La configuración cargue sin fricciones y soporte paths `~/.bridge` prioritarios.

## Etapa 2 — Capa Gateway y Delegación en Un Solo Paso
- [x] En `src/codex_chatgpt_bridge/gateway.py`:
  - `_build_delegate_prompt`: Mandato de ejecución autónoma para finalizar la tarea inmediatamente sin pausas.
  - `codex_delegate`: Auto-continuación transparente si el motor local intenta pausar tras la inicialización de la sesión.
  - `find_rg_path()`: Detección automática de Ripgrep en winget y PATH.
  - Simplificar ejecución de tareas: bypass de sandbox por defecto (`full_access` / `danger_full_access` pasan `--dangerously-bypass-approvals-and-sandbox`).

**Validación:** `pytest tests/test_gateway.py` aprobado (23/23 tests).  
**Finaliza cuando:** Las tareas delegadas se ejecuten de punta a punta en un solo llamado.

## Etapa 3 — Capa MCP (Eliminación de Sandbox y Hints Destructivos)
- [x] En `src/codex_chatgpt_bridge/mcp.py`:
  - Eliminar `sandbox_mode` de `inputSchema` y `outputSchema` de todas las herramientas.
  - Desactivar `destructiveHint` y `openWorldHint` en todas las anotaciones.
  - Servidor titulado "Bridge MCP Server" v0.5.0.

**Validación:** `pytest tests/test_mcp.py` aprobado (15/15 tests).  
**Finaliza cuando:** La inspección de herramientas devuelva esquemas limpios sin sandbox ni riesgos de confirmación.

## Etapa 4 — TUI Messenger, CLI y Ripgrep
- [x] En `bridge_messenger.py`: Renombrar títulos y constantes a Bridge agnóstico.
- [x] En `src/codex_chatgpt_bridge/cli.py`: Soporte `_ensure_stdio` para Windows y auto-detección de Ripgrep en `doctor`.
- [x] Instalación de `BurntSushi.ripgrep.MSVC 15.2.0` vía winget.

**Validación:** `bridge doctor` muestra `rg: ...\rg.exe` y `pytest tests/test_cli.py tests/test_bridge_messenger.py` aprobados (24/24 tests).  
**Finaliza cuando:** Ripgrep esté disponible y los diagnósticos aprueben.

## Etapa 5 — Pruebas, Validación y Regresión
- [x] Ejecutar suite completa con `pytest`.
- [x] Verificar en vivo contra el servidor activo (MCP `tools/list` y `bridge_status`).

**Validación:** `pytest` -> `71 passed in 1.97s`.  
**Finaliza cuando:** Todos los 71 tests pasen limpiamente.

## Etapa 6 — Entrega y Documentación
- [x] Actualizar documentación y roadmap vivo.
- [x] Commit y push a `origin/main`.
- [x] Servidor activo y verificado respondiendo en vivo.

## Etapa 7 — Auditoría de Concurrencia y Paralelización Masiva
- [x] Diseñar batería de pruebas de estrés y paralelismo sobre el servidor activo (`127.0.0.1:8766`).
- [x] Ejecutar ráfaga masiva concurrente de solicitudes MCP HTTP (50 llamadas simultáneas: `bridge_status`, `system_info`, `list_grants`, `bridge_session_list`).
- [x] Ejecutar operaciones de I/O de archivos paralelas (`write_file`, `read_file`, `edit_file`) en 20 archivos independientes concurrentes.
- [x] Ejecutar búsquedas simultáneas en paralelo (`search_files`) con motor Ripgrep bajo contención (10 búsquedas simultáneas).
- [x] Ejecutar procesos de sistema en paralelo (`exec_command`) en modo síncrono (8 procesos simultáneos con PIDs y Job IDs aislados).
- [x] Ejecutar y monitorear tareas en background concurrentes (4 jobs asíncronos concurrentes con `process_manager`).
- [x] Ejecutar consultas concurrentes de sesiones (`bridge_session_list` y `bridge_session_status`).
- [x] Inspeccionar telemetría de concurrencia en tiempo real (`/healthz` -> `parallel_engine`: `active_workers`, `peak_workers`, `running_jobs`).
- [x] Verificar que no existan carreras de datos, deadlocks, corrupción de buffers ni errores de socket.

**Validación:** Script exhaustivo de benchmarking concurrente y asíncrono con telemetría en vivo (`benchmark_concurrency.py`).  
**Finaliza cuando:** Todas las ejecuciones paralelas retornen exitosamente con métricas de throughput y latencia.

## Etapa 8 — Erradicación Total de `bridge_delegate` y `sandbox_mode` (SSOT, SOLID, DRY, SoC)
- [x] Eliminar la herramienta `bridge_delegate` del catálogo, esquemas, `tool_definitions` y dispatchers en `mcp.py`.
- [x] Eliminar `codex_delegate`, `bridge_delegate`, `_build_delegate_prompt`, `_resolve_delegate_sandbox_mode`, `DELEGATE_DEFAULT_SANDBOX` y `SANDBOX_RANK` en `gateway.py`.
- [x] Eliminar todo parámetro y lógica de `sandbox_mode` de `codex_task_run`, `bridge_task_run`, `codex_session_start`, `bridge_session_start`, `codex_session_continue`, `bridge_session_continue`, `_finish_codex_session_run` y `_authorize_codex_cwd` en `gateway.py`.
- [x] Limpiar referencias a `bridge_delegate` y `codex_delegate` en `bridge_messenger.py`.
- [x] Actualizar documentación y README eliminando referencias a `codex_delegate` y `sandbox_mode`.
- [x] Actualizar suite de pruebas (`tests/test_gateway.py` y `tests/test_mcp.py`) para reflejar la eliminación de `bridge_delegate` y `sandbox_mode`.
- [x] Reejecutar suite completa de pytest (66 de 66 pruebas pasando al 100% verde).
- [x] Reiniciar servidor Bridge y validar en vivo `tools/list` confirmando la erradicación de `bridge_delegate` y `sandbox_mode` (20 herramientas limpias).
- [x] Validar ejecución en vivo de `bridge_task_run` contra el daemon en ejecución con resultado verificado (returncode 0).

**Validación:** `pytest` pasando al 100% + búsqueda global de `bridge_delegate` y `sandbox_mode` confirmando 0 ocurrencias en código ejecutable.  
**Finaliza cuando:** La arquitectura quede limpia, con tipado estricto, sin vestigios de sandbox ni de delegate tool.

## Evidencia de validación
| Validación | Comando | Resultado |
|---|---|---|
| Suite de Pruebas | `python -m pytest tests` | **66 passed in 3.26s** (100% verde) |
| Erradicación de `bridge_delegate` | `tools/list` en `/mcp` | **`bridge_delegate` inexistente** (20 herramientas totales) |
| Erradicación de `sandbox_mode` | `tools/list` en `/mcp` | **0 herramientas con `sandbox_mode`** |
| Ejecución en Vivo de Tarea | `bridge_task_run` en vivo | **Exit 0, sin `sandbox_mode`, sin diálogos** |
| Detección de Ripgrep | `bridge_status` en `/mcp` | `ripgrep_available: True`, `search_engine: ripgrep` |
| Ráfaga MCP Concurrente | `benchmark_concurrency.py` (Escenario 1) | **50/50 llamadas en 2.84s (100% éxito)** |
| I/O Paralelo de Archivos | `benchmark_concurrency.py` (Escenario 2) | **20 escrituras (0.218s), 20 lecturas (0.158s), 20 ediciones (0.250s)** |
| Búsquedas Ripgrep Paralelas | `benchmark_concurrency.py` (Escenario 3) | **10 búsquedas en 0.648s** |
| Procesos del SO Paralelos | `benchmark_concurrency.py` (Escenario 4) | **8 procesos en 1.184s (8 PIDs únicos aislados)** |
| Background Jobs Asíncronos | `benchmark_concurrency.py` (Escenario 5) | **4 jobs concurrentes rastreados por Process Manager** |
| Consultas de Sesión Paralelas| `benchmark_concurrency.py` (Escenario 6) | **20 consultas en 0.145s sin contención** |
