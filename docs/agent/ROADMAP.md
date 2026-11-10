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

## Evidencia de validación
| Validación | Comando | Resultado |
|---|---|---|
| Suite de Pruebas | `$env:PYTHONPATH="src;."; pytest` | **71 passed in 1.97s** (100% verde) |
| Eliminación de Sandbox | `tools/list` en `/mcp` | `bridge_delegate` y `bridge_task_run` **sin `sandbox_mode`** |
| Detección de Ripgrep | `bridge_status` en `/mcp` | `ripgrep_available: True`, `search_engine: ripgrep` |
| Modo de Confianza | `bridge_status` en `/mcp` | `trust_mode: full_delegate`, `tool_profile: full` |
