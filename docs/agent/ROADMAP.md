# Roadmap de implementación: Sonidos en Vivo y Reducción de Historial al Inicio

## Objetivo
1. **Silenciar ráfaga de sonidos al entrar:**
   - Eliminar el carácter de campana ASCII `\a` de `format_agent_message`.
   - Sembrar el 100% de los mensajes preexistentes (`init_msgs`) en `seen_ids` al inicializar la TUI, de modo que `poll_worker` nunca considere los mensajes pasados como "nuevos" ni active pitidos repetitivos (`winsound.Beep`).
2. **Historial inicial conciso:**
   - Limitar la muestra visual de historial al arrancar a los últimos 3 mensajes más recientes (`init_msgs[-3:]`), evitando imprimir "desde el nacimiento de Cristo".
3. **Sonidos exclusivamente para eventos nuevos:**
   - `winsound.Beep` solo se ejecuta cuando un mensaje nuevo del agente llega en tiempo real mientras el chat está abierto.

## Estado general
- Estado: Completado
- Última actualización: 2026-11-04 04:24
- Riesgo actual: Bajo
- Responsable: Agente autónomo

## Criterios de aceptación
- [x] Al iniciar la TUI, no suena ningún pitido de los mensajes antiguos en el historial.
- [x] La campana `\a` está eliminada de las funciones de formato de texto.
- [x] Todos los mensajes del historial inicial se marcan como vistos (`seen_ids`) para evitar que el hilo de sondeo los procese como recién llegados.
- [x] El historial inicial solo muestra un máximo de 3 mensajes recientes.
- [x] La suite de pruebas de `bridge_messenger` (8 tests) pasa al 100%.

## Tareas completadas
- [x] Eliminar `\a` de `TuiRenderer.format_agent_message`.
- [x] En `MessengerController.run()`, registrar todos los `id` de `init_msgs` en `seen_ids` y `delivered_ids`.
- [x] Ajustar la visualización inicial de historial a `init_msgs[-3:]`.
- [x] Añadir pruebas unitarias en `tests/test_bridge_messenger.py`.

## Evidencia de validación
- `pytest tests/test_bridge_messenger.py -v`: 8 passed in 0.32s.
- `python -m py_compile bridge_messenger.py`: Aprobado (0 errores).
