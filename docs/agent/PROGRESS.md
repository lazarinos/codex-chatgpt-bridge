# Registro de Progreso y Memoria Técnica del Proyecto

## Estado Actual
- **Proyecto:** Bridge MCP Server & Gateway
- **Versión:** 0.5.0
- **Modo:** Universal, Agnóstico, Alta Concurrencia
- **Fecha:** 2026-11-10

## Hitos Validados
1. **Identidad Agnóstica y SSOT (`bridge`):**
   - Retiro de referencias "Codex Sandbox" y prefijos "chatgpt" en favor de una API limpia y universal (`bridge_*`).
   - Comando CLI unificado `bridge` registrado en `pyproject.toml`.
2. **Eliminación Total de `sandbox_mode` y de `bridge_delegate`:**
   - La herramienta `bridge_delegate` fue completamente retirada del catálogo MCP, esquemas, handlers y métodos internos.
   - El parámetro `sandbox_mode` y los tipos/enums/ranks asociados (`SandboxMode`, `CodexSessionSandboxMode`, `DELEGATE_DEFAULT_SANDBOX`, `SANDBOX_RANK`) fueron erradicados en su totalidad.
   - La ejecución de tareas y sesiones (`bridge_task_run`, `bridge_session_start`, `bridge_session_continue`) opera de forma directa, no bloqueante y autónoma sin modal popups (`readOnlyHint: True`, `destructiveHint: False`).
   - El servidor MCP expone exactamente 20 herramientas limpias, verificadas en vivo.
3. **Flujo de Delegación Directa en Un Solo Paso (`bridge_delegate`):**
   - Mandato `AUTONOMOUS EXECUTION MANDATE` integrado en la construcción del prompt del CLI.
   - Auto-continuación transparente si el motor local intenta pausar tras la inicialización.
4. **Integración Nativa de Ripgrep (`rg`):**
   - Detección dinámica de `rg.exe` vía `find_rg_path()` en rutas de WinGet y PATH.
   - Búsquedas con motor `ripgrep` validadas en vivo.
5. **Auditoría Exhaustiva de Herramientas (21/21):**
   - 100% de las 21 herramientas del servidor MCP probadas exitosamente contra el daemon activo en `http://127.0.0.1:8766`.
6. **Pruebas de Estrés, Concurrencia y Paralelización Masiva:**
   - **Ráfaga MCP Concurrente:** 50 llamadas simultáneas completadas en 1.675s (throughput: 29.85 req/s, 0 errores).
   - **I/O Paralelo de Archivos:** 20 escrituras (0.134s), 20 lecturas (0.098s) y 20 ediciones (0.136s) concurrentes verificando integridad de datos.
   - **Búsquedas Ripgrep Paralelas:** 10 búsquedas concurrentes en 0.411s.
   - **Procesos del SO Simultáneos:** 8 procesos paralelos con aislamiento estricto de PIDs ([13140, 16944, 18044, 19220, 7200, 21120, 1580, 19812]) y buffers de salida deterministas.
   - **Background Jobs Asíncronos:** 4 jobs en background lanzados simultáneamente y monitoreados en tiempo real por `process_manager`.
   - **Consultas de Sesión Paralelas:** 20 consultas concurrentes en 0.114s.
   - **Telemetría `/healthz`:** 458 llamadas totales procesadas, escala dinámica de trabajadores paralelos (`max_workers=192`, `peak_workers=10`), 0 bloqueos, 0 memory leaks, 0 caídas.
7. **Refuerzo de Autonomía, Concurrencia, Resiliencia y Perfil `delegate` (Etapa 10):**
   - **MCP (`mcp.py`):** Esquemas cerrados (`additionalProperties: False`), anotaciones explícitas SSOT (`readOnlyHint: True`, `destructiveHint: False`, `openWorldHint: False`), comparación constant-time (`secrets.compare_digest`), manejo JSON-RPC `-32602` ante parámetros inesperados.
   - **Perfil `delegate` automático (`C:\Users\LAZARINOS\bin\bridge.ps1`, `restart-bridge.ps1`):** `Start-BridgeProcess` aplica `set-tool-profile delegate` (+ `full_delegate` y `enable-tasks`), reduciendo las herramientas visibles en `/mcp` a 4 (`bridge_status`, `bridge_task_run`, `bridge_session_list`, `bridge_session_status`) y respetando cambios explícitos mediante `bridge profile [delegate|full]`. Se corrigió también `restart-bridge.ps1` para delegar al daemon con `-Action restart`.
   - **Concurrencia Windows (`live_inbox.py`):** `LiveInboxStore._load()` envuelto en `_STORE_LOCK` (`RLock`) con reintentos (3 intentos, backoff `15 ms` y `20 ms`) frente a `[WinError 32]`/`PermissionError` sin silenciar fallos persistentes ni borrar historial.
   - **Ciclo de vida de procesos (`process_runner.py`):** `BackgroundJobManager._jobs` protegido con `RLock`, limpieza periódica automática vía hilo daemon (`clear_finished()`), liberación segura de transport/pipes y detección de PIDs huérfanos muertos.
   - **Resiliencia TUI (`bridge_messenger.py`):** `MessengerController.poll_worker()` con backoff exponencial `(1s, 2s, 4s)` ante caídas o reinicios y recuperación silenciosa; comandos interactivos protegidos ante errores de red/disco.
   - **Fallback `view_image` (`gateway.py`) y aislamiento de tests (`tests/conftest.py`):** El fallback GDI sin Pillow preserva PNG pequeños en lugar de convertirlos forzadamente a JPEG y utiliza archivo temporal único limpiado en `finally`; los tests aíslan `HOME`/`USERPROFILE` para no contaminar `~/.bridge/live_inbox.json`.
8. **Canales Limpios, Puros y Ordenados + Mensajería Directa y Escaping Seguro Windows (Etapa 11):**
   - **Canal Puro Directo (`cli.py`, `bridge.ps1`, `~/.bridge/config.toml`):** Se añadió `set-channel [pure|delegate|codex]` / `bridge channel [pure|delegate|codex]`, se sincronizaron `enable_tasks` y `enable_codex_tasks` en `_cmd_set_codex_tasks`, y `C:\Users\LAZARINOS\bin\bridge.ps1` dejó de sobreescribir preferencias existentes para permitir canal `pure_direct` (`tool_profile="full"`, `enable_tasks=False`, 16 herramientas sin LLM Codex intermedio).
   - **SSOT de Orden y `bridge_inbox` (`config.py`, `gateway.py`, `mcp.py`, `bridge_messenger.py`):** `ordered_visible_tool_names()` ordena lógicamente las herramientas visibles por categoría; `bridge_inbox` (`reply`, `state`, `pop`, `clear`) entrega mensajería bidireccional TUI sin subprocesos ni ruido en `action_log`.
   - **Auto-notificación TUI y Supresión de Pitidos en Tests (`gateway.py`, `live_inbox.py`, `bridge_messenger.py`, `tests/conftest.py`):** Se eliminó el parche roto en `gateway.py:1613`, se extrajo `_publish_codex_result_to_tui()` (sin `ValueError` en salidas vacías y con `--skip-git-repo-check`), y se implementó `_should_play_beep()` + parche en `tests/conftest.py` para silenciar pitidos hardware `winsound.Beep` durante corridas de `pytest`.
   - **Escaping Seguro Windows (`process_runner.py`, `gateway.py`):** `build_shell_command()` codifica PowerShell en UTF-16LE `-EncodedCommand`, `run_argv_captured()` soporta `stdin_text` con cancelación limpia en timeout, `_resolve_windows_command_argv()` desempaqueta shims `.cmd` npm a `node.exe` directo, y prompts complejos/multilínea de Codex se envían vía `stdin` (`-`).

## Suite de Pruebas
- `.venv\Scripts\python.exe -m pytest tests -q`: **87 passed in 5.67s** (100% verde, sin Pillow, `live_inbox.json` intacto, 0 pitidos)
- `python -m pytest tests -q`: **87 passed in 5.47s** (100% verde, con Pillow)

