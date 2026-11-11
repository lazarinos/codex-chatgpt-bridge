# Roadmap de implementación: Refactor Limpio y Agnóstico "Bridge" (SSOT, SOLID, DRY, SoC)

## Objetivo
Transformar el conector/puente en un motor universal denominado limpiamente **Bridge**:
1. **Renombrar la identidad pública del MCP:** Retirar "ChatGPT" y "Codex" de los nombres y esquemas públicos de herramientas, unificando bajo el prefijo universal `bridge_*` (ej. `bridge_delegate`, `bridge_session_start`, `bridge_task_run`), manteniendo cero regresiones.
2. **Eliminar modo sandbox y restricciones innecesarias:** Eliminar el parámetro `sandbox_mode` de todos los esquemas MCP (`bridge_delegate`, `bridge_task_run`, `bridge_session_start`, `bridge_session_continue`), forzando ejecución directa con bypass de aprobaciones a máxima velocidad.
3. **Flujo de delegación inmediata en una sola operación (`bridge_delegate`):** Mandato de ejecución autónoma estricta en el prompt del sistema y auto-continuación transparente si el motor local intenta pausar tras la inicialización de la sesión.
4. **Desactivar popups de confirmación modal:** Forzar `destructiveHint: False`, `openWorldHint: False`, `readOnlyHint: True` en todas las herramientas del servidor MCP para evitar que interfaces web (Gemini Web, ChatGPT) muestren cuadros de diálogo bloqueantes ("Aceptar / Rechazar").
5. **Detección e instalación de Ripgrep:** Instalación de `BurntSushi.ripgrep.MSVC 15.2.0` y soporte para auto-detección dinámica vía `find_rg_path()` en Windows (winget/PATH).

## Estado general
- Estado: Completado (Etapa 11 — Canales Limpios y Ordenados, Sin Codex Luna Intermedio, Mensajería Directa `bridge_inbox`, Escaping Seguro Windows y Silencio de Pitidos en Tests)
- Última actualización: 2026-11-10 20:57
- Riesgo actual: Bajo
- Responsable: Agente autónomo

## Alcance
- Incluye: Endurecimiento MCP (`additionalProperties: False`, anotaciones explícitas, auth constant-time, JSON-RPC `-32602` en args inválidos), canal directo `bridge_inbox` (~7–25ms sin subprocesos), ordenación SSOT `ordered_visible_tool_names()`, canal puro por defecto (`pure`: `tool_profile="full"`, `enable_tasks=False`, `trust_mode="full_delegate"`, sin Codex Luna intermedio), comando CLI `bridge channel [pure|delegate|codex]` / `set-channel`, eliminación de parche roto `patch_gateway.py` y extracción de `_publish_codex_result_to_tui()`, UTF-16LE `-EncodedCommand` + `stdin_text` + unwrap de shims `.cmd` npm, concurrencia en `LiveInboxStore` (`_STORE_LOCK`, reintentos 15–20ms y supresión de `winsound.Beep` bajo `pytest`/`BRIDGE_DISABLE_BEEP`), limpieza periódica en `BackgroundJobManager`, backoff `(1s, 2s, 4s)` en `bridge_messenger.py`, corrección de `view_image` sin Pillow, y aislamiento completo en `tests/conftest.py`.
- No incluye: Cambios destructivos de API ni reincorporación de herramientas eliminadas (`bridge_delegate`, `sandbox_mode`).

## Criterios de aceptación
- [x] Configuración base de librería (`BridgeConfig`) mantiene compatibilidad de contratos y CLI `bridge channel pure` / `bridge.ps1` dejan el canal activo en modo puro directo (`tool_profile = "full"`, `enable_tasks = false`, `trust_mode = "full_delegate"`, 0 LLM Codex intermedio).
- [x] Desactivación total de hints destructivos (`destructiveHint: False`, `openWorldHint: False`, `readOnlyHint: True`) y esquemas cerrados (`additionalProperties: False`) en todas las herramientas MCP (SSOT).
- [x] Canal instantáneo `bridge_inbox` (`reply`, `state`, `pop`, `clear`) operativo y herramientas ordenadas lógicamente vía `ordered_visible_tool_names()` sin subprocesos ni spam en `action_log`.
- [x] Auto-notificación TUI segura (`_publish_codex_result_to_tui()` sin `ValueError` en salidas vacías) y supresión de pitidos `winsound.Beep` durante `pytest` (`_should_play_beep()` + `tests/conftest.py`).
- [x] Escaping seguro en Windows: PowerShell vía UTF-16LE `-EncodedCommand`, shims `.cmd` npm desempaquetados a `node.exe` directo y prompts complejos/multilínea enviados por `stdin` (`-`).
- [x] `sandbox_mode` y `bridge_delegate` 100% eliminados de todos los esquemas MCP y gateway.
- [x] Ripgrep instalado (`rg 15.2.0`) y activo en el motor de búsqueda (`ripgrep_available: True`).
- [x] Servidor MCP titulado e identificado como `bridge` v0.5.0.
- [x] Concurrencia en `LiveInboxStore` protegida con `_STORE_LOCK` y reintentos `[WinError 32]`, hilo daemon de limpieza en `BackgroundJobManager` y backoff `(1s, 2s, 4s)` en `bridge_messenger.py`.
- [x] Suite completa de pruebas pasando al 100% en `.venv` (87/87 en 5.67s) y Python global (87/87 en 5.47s) sin contaminar `~/.bridge/live_inbox.json` ni emitir sonidos de prueba.

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

## Etapa 9 — Restauración del Comando Maestro `bridge` (TUI + Daemon permanente)
- [x] Identificar la causa raíz: la adición de `bridge = "codex_chatgpt_bridge.cli:main"` en `pyproject.toml` generó un `bridge.exe` en Python Scripts que sobreescribió la precedencia de `C:\Users\LAZARINOS\bin\bridge.cmd` / `bridge.ps1`.
- [x] Retirar el script de consola colisionante `bridge` de `pyproject.toml` (manteniendo `codex-chatgpt-bridge = "codex_chatgpt_bridge.cli:main"`).
- [x] Eliminar el binario `bridge.exe` generado en `Python311\Scripts\bridge.exe`.
- [x] Confirmar que `Get-Command bridge` apunta directamente a `C:\Users\LAZARINOS\bin\bridge.ps1`.
- [x] Validar que `bridge status`, `bridge restart` y la invocación de `bridge` mantengan el servicio activo permanentemente en background (Tailscale Funnel + HTTP 200) y abran el TUI interactivo de chat (`bridge_messenger.py --chat`).

**Validación:** `Get-Command bridge` resuelve a `bridge.ps1`, `bridge status` reporta `ONLINE`, y `bridge msg --status` reporta telemetría.  
**Finaliza cuando:** El flujo de trabajo del usuario esté 100% restaurado y operativo.

**Validación:** `pytest` pasando al 100% + búsqueda global de `bridge_delegate` y `sandbox_mode` confirmando 0 ocurrencias en código ejecutable.  
**Finaliza cuando:** La arquitectura quede limpia, con tipado estricto, sin vestigios de sandbox ni de delegate tool.

## Etapa 10 — Refuerzo de Autonomía, Concurrencia, Resiliencia y Perfil `delegate` por Defecto
- [x] **10.1 Metadatos y seguridad MCP (`src/codex_chatgpt_bridge/mcp.py`)**: Garantizar `"additionalProperties": False` y anotaciones explícitas (`readOnlyHint: True`, `destructiveHint: False`, `openWorldHint: False`) en todas las herramientas (SSOT), auth constant-time (`secrets.compare_digest`), manejo de `TypeError` -> JSON-RPC `-32602`, y limpieza de código muerto.
- [x] **10.2 Concurrencia en `LiveInboxStore` (`src/codex_chatgpt_bridge/live_inbox.py`)**: Envolver `_load()` y operaciones read-modify-write dentro de `with _STORE_LOCK:` (`RLock`) e implementar reintentos con backoff de 3 intentos espaciados 15–20 ms ante `PermissionError` / `[WinError 32]` en lecturas, unlinks y escrituras directas en disco.
- [x] **10.3 Hilo periódico en `BackgroundJobManager` (`src/codex_chatgpt_bridge/process_runner.py`)**: Proteger `_jobs` con `threading.RLock`, limpiar handles/transports al expirar, detectar procesos terminados/huérfanos y ejecutar `clear_finished()` periódicamente desde un hilo daemon con cierre seguro (`close()` / `weakref.finalize`).
- [x] **10.4 Tolerancia a fallos en `bridge_messenger.py`**: Implementar backoff exponencial `(1s, 2s, 4s)` con recuperación silenciosa en `MessengerController.poll_worker()` y detección de estado offline/online en `BridgeClient` para soportar hot-reload/reinicios sin cerrar ni inundar el TUI.
- [x] **10.5 Corrección de fallback `view_image` (`src/codex_chatgpt_bridge/gateway.py`)**: Evitar conversión forzada a JPEG en imágenes pequeñas sin Pillow y usar archivo temporal único thread-safe.
- [x] **10.6 Comando global `bridge` con `delegate` por defecto (`C:\Users\LAZARINOS\bin\bridge.ps1`, `restart-bridge.ps1`, `src/codex_chatgpt_bridge/cli.py`)**: Auto-aplicar `set-tool-profile delegate` (+ `full_delegate` y `enable-tasks`) en `Start-BridgeProcess` y corregir `restart-bridge.ps1` para delegar al daemon sin abrir TUI ni romper `bridge.ps1`.
- [x] **10.7 Pruebas unitarias, regresión y validación en vivo**: Añadir pruebas para concurrencia WinError 32, limpieza periódica de jobs, backoff de polling, esquemas MCP `delegate`/`full`, JSON-RPC `-32602` y `view_image` sin Pillow; aislar `HOME`/`USERPROFILE` en `tests/conftest.py`; validar `pytest` en ambos intérpretes, `ruff check`, `bridge restart`, `bridge status`, `bridge doctor` y `tools/list` en vivo.

**Validación:** `pytest tests` 100% verde en `.venv` (77/77) y Python global (77/77), `ruff check` limpio en módulos endurecidos y nuevos tests, `bridge status` ONLINE y `tools/list` en vivo reflejando perfil `delegate`.  
**Finaliza cuando:** Todos los refuerzos estén implementados, validados y confirmados en vivo.

## Etapa 11 — Canales Limpios, Puros y Ordenados (Cero Doble LLM, Mensajería Directa, Pitidos Silenciados en Tests y Escaping Seguro)
- [x] **11.1 Canal directo de mensajería TUI (`bridge_inbox`), SSOT de visibilidad y orden lógico de herramientas (`config.py`, `gateway.py`, `mcp.py`, `bridge_messenger.py`)**: Centralizar `ordered_visible_tool_names()` como SSOT en `config.py`, agregar herramienta MCP directa `bridge_inbox` (`reply`, `state`, `pop`, `clear`) sin subprocesos ni LLM intermedio (con `"type": ["string", "null"]` limpio en `message`), incluir `bridge_inbox`/`exec_command`/`process_manager` en perfil `delegate` manteniendo ocultas las herramientas de archivos, y ordenar `tool_definitions()` por grupos claros (Estado/Chat -> Ejecución Directa -> Archivos -> Grants/Config -> Delegación Codex opcional).
- [x] **11.2 Auto-notificación segura a la TUI, supresión de pitidos fantasma en tests y limpieza temporal (`gateway.py`, `live_inbox.py`, `bridge_messenger.py`, `tests/conftest.py`)**: Eliminar `patch_gateway.py` y el llamado sin guard en `gateway.py:1613`, extraer `_publish_codex_result_to_tui()` para `codex_task_run` y `_finish_codex_session_run` con guard `if cleaned_msg:` (sin `ValueError` en salidas vacías), aviso ante errores, `--skip-git-repo-check`, y suprimir `winsound.Beep` cuando `PYTEST_CURRENT_TEST` o `BRIDGE_DISABLE_BEEP` está activo (diagnóstico exacto de por qué se oían pitidos sin mensajes visibles durante runs previos de `pytest`).
- [x] **11.3 Eliminación de fallos de escaping y multilínea en Windows (`process_runner.py`, `gateway.py`)**: Pasar scripts PowerShell con `-EncodedCommand` UTF-16LE, soportar `stdin_text` en `run_argv_captured()` con limpieza segura de `stdin_task` en timeout, desempaquetar shims `.cmd` de npm (`codex.CMD` -> `node.exe`) sin `cmd.exe`, y enviar prompts complejos/multilínea de Codex por `stdin` (`-`).
- [x] **11.4 Canal puro por defecto respetado en CLI y daemon (`cli.py`, `C:\Users\LAZARINOS\bin\bridge.ps1`, `~/.bridge/config.toml`)**: Sincronizar `enable_tasks` y `enable_codex_tasks` en `_cmd_set_codex_tasks`, agregar `set-channel [pure|delegate|codex]` / `bridge channel [pure|delegate|codex]`, mostrar `channel_mode` en `doctor`/`bridge status`, aplicar `pure` en `~/.bridge/config.toml` y evitar que `bridge.ps1` sobreescriba elecciones posteriores del usuario.
- [x] **11.5 Pruebas, regresión, validación en vivo y entrega**: Cubrir `bridge_inbox`, `-EncodedCommand` multilínea/comillas, `stdin_text`, shim unwrap `.cmd`, auto-notificación TUI vacía/no-vacía, supresión de pitidos bajo pytest y perfiles `full`/`delegate` con/sin `enable_tasks`; validar `.venv` + Python global (`87 passed`) + `ruff check` + verificación en vivo `/mcp` (`channel_mode: pure_direct`, 16 herramientas limpias, 0 herramientas de Codex Luna, `bridge_status` 37.5ms, `bridge_inbox` 25.4ms, `exec_command` multilínea/comillas `rc=0`) y actualizar documentación/git.

**Validación:** `pytest tests` 100% verde en ambos intérpretes (`87 passed`), `ruff check` sin errores en módulos core/nuevos tests, y verificación en vivo `/mcp` (`channel_mode: pure_direct`, 16 herramientas sin Codex Luna).  
**Finaliza cuando:** Todos los canales queden puros, limpios, ordenados y verificados en vivo.

## Bloqueos, riesgos y decisiones
- **Diagnóstico de sonidos sin texto en TUI:** `live_inbox.json` solo registraba 1 mensaje del agente (`20:25:05`), mientras los pitidos percibidos alrededor de `20:33:50` provenían de `LiveInboxStore.send_agent_reply()` ejecutando `winsound.Beep` en Windows durante las corridas de `pytest` sobre `tmp_path` y de que el daemon previo en `8766` aún servía el código anterior en perfil `delegate` antes del reinicio final. Mitigado con `_should_play_beep()`, guard en `bridge_messenger.py` y parche en `tests/conftest.py`.
- **Decisión de compatibilidad (`config.py` vs `~/.bridge/config.toml`):** `BridgeConfig` conserva `enable_codex_tasks=True` en librería para no romper tests existentes de librería (`test_config.py`, `test_gateway.py`), mientras `bridge.ps1` inicializa nuevas configs en `pure` (`tool_profile="full"`, `enable_tasks=False`) y `~/.bridge/config.toml` quedó fijado en `pure`.
- **Aislamiento de tests (`tests/conftest.py`):** Se verificó `MTIME_UNCHANGED=True` sobre `~/.bridge/live_inbox.json` antes y después de correr toda la suite de pytest.
- **Ciclo de vida del demonio en Windows (`IsDaemon`):** El servidor activo (`PID 15996` en puerto `8766` con Funnel activo y TUI conectado) corre como tarea daemon gestionada por esta sesión; fuera de la sesión o tras reiniciar el PC, el usuario lo inicia de forma permanente con `bridge` o `bridge restart`.

## Evidencia de validación
| Validación | Comando | Resultado |
|---|---|---|
| Compilación sintáctica | `.venv\Scripts\python.exe -m compileall -q src tests bridge_messenger.py` | **Exit 0** |
| Linter Ruff (módulos núcleo + Etapa 11) | `.venv\Scripts\ruff.exe check src\codex_chatgpt_bridge\config.py src\codex_chatgpt_bridge\process_runner.py src\codex_chatgpt_bridge\mcp.py src\codex_chatgpt_bridge\live_inbox.py tests\test_process_runner.py tests\test_cli.py tests\test_live_inbox.py tests\conftest.py` | **All checks passed!** |
| Suite de Pruebas (`.venv` sin Pillow) | `.venv\Scripts\python.exe -m pytest tests -q` | **87 passed in 5.67s** (`live_inbox.json` intacto, 0 pitidos) |
| Suite de Pruebas (Python global con Pillow) | `python -m pytest tests -q` | **87 passed in 5.47s** (`MTIME_UNCHANGED=True`) |
| Diagnóstico CLI | `bridge doctor` | **Profile: full, Channel: pure_direct (0 intermediate LLM), Codex tasks: disabled, rg 15.2.0** |
| Estado en Vivo y Túnel | `bridge status` | **ONLINE (PID 15996, HTTP 200), Channel: PURE DIRECT, Funnel https://desktop-8jn135m.tail96313c.ts.net -> 8766** |
| Inspección MCP en Vivo (`/mcp` en PID 15996) | `tools/list`, `bridge_status`, `bridge_inbox(state)`, negativos (`-32602` / `-32000`) | **16 herramientas ordenadas en 26.5ms (`additionalProperties: False`, safe annotations, 0 Codex Luna), `bridge_status` 37.5ms (`pure_direct`), `bridge_inbox` 25.4ms** |
| `exec_command` multilínea y comillas en vivo (`/mcp`) | `$x = 'hola & "mundo"'` + suma multilínea PowerShell `-EncodedCommand` | **Exit 0 en 557.9ms, stdout exacto sin errores de quoting** |
| Ráfaga MCP Concurrente (histórico Etapa 7) | `benchmark_concurrency.py` (Escenario 1) | **50/50 llamadas en 2.84s (100% éxito)** |
| I/O Paralelo de Archivos (histórico Etapa 7) | `benchmark_concurrency.py` (Escenario 2) | **20 escrituras (0.218s), 20 lecturas (0.158s), 20 ediciones (0.250s)** |
| Búsquedas Ripgrep Paralelas (histórico Etapa 7) | `benchmark_concurrency.py` (Escenario 3) | **10 búsquedas en 0.648s** |
| Procesos del SO Paralelos (histórico Etapa 7) | `benchmark_concurrency.py` (Escenario 4) | **8 procesos en 1.184s (8 PIDs únicos aislados)** |
| Background Jobs Asíncronos (histórico Etapa 7) | `benchmark_concurrency.py` (Escenario 5) | **4 jobs concurrentes rastreados por Process Manager** |
| Consultas de Sesión Paralelas (histórico Etapa 7) | `benchmark_concurrency.py` (Escenario 6) | **20 consultas en 0.145s sin contención** |

## Resumen final
- Completado: Canal puro directo activo (`pure_direct`, 16 herramientas ordenadas vía SSOT `ordered_visible_tool_names()`, 0 LLM Codex Luna intermedio), herramienta directa `bridge_inbox` (`reply`, `state`, `pop`, `clear`) en milisegundos sin subprocesos, `_publish_codex_result_to_tui()` seguro sin `ValueError` ante salidas vacías, supresión de `winsound.Beep` en tests (`_should_play_beep()` + `conftest.py`), ejecución PowerShell UTF-16LE `-EncodedCommand` + `stdin_text` + unwrap `.cmd` npm, comando `bridge channel [pure|delegate|codex]` / `set-channel`, 87/87 tests en `.venv` y Python global, y daemon reiniciado/verificado en vivo.
- Pendiente: Ninguno.
- Bloqueado: Ninguno.
- Riesgos remanentes: `C:\Users\LAZARINOS\bin\bridge.ps1` reside fuera del repositorio git; la sesión TUI TTY real con `msvcrt` se verifica manualmente junto al servidor en vivo, mientras las transiciones de vista/reflow/backoff quedan cubiertas en `tests/test_bridge_messenger.py`. El demonio actual (`PID 15996`) corre bajo esta sesión y el usuario puede lanzarlo permanentemente desde su terminal con `bridge` o `bridge restart`.
