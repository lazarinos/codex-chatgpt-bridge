# Ingeniería Inversa Comparativa: Claude Code (`@anthropic-ai/claude-code`) vs OpenAI Codex CLI (`@openai/codex`)

## 1. Arquitectura de TUI de Claude Code
- **Runtime:** Bun Single Executable Application (`claude.exe`, ~256MB).
- **Librería TUI:** React + `ink` (`useInput`, `Box`, `Text`).
- **Manejo de Tools:** `renderGroupedAgentToolUse`.
  - Agrupa ejecuciones consecutivas de herramientas de inspección (lecturas y búsquedas).
  - Reduce el scrollback saturado y muestra barras de progreso condensadas.
- **Buffer de Entrada:** Redibujado no destructivo que preserva el borrador en curso cuando el agente emite telemetría.

## 2. Arquitectura de TUI de OpenAI Codex CLI
- **Runtime:** Binario nativo compilado en Rust (`@openai/codex-win32-x64/vendor/.../bin/codex.exe`, ~298MB).
- **Librería TUI:** Rust `ratatui` + `crossterm`.
- **Manejo de Tools:**
  - Modelo de estado reactivo y eventos de terminal discretos (`crossterm::event`).
  - Renderizado sobrio: utiliza tipografía técnica de terminal (caracteres `*`, `|`, `+`, `-`), prescindiendo de emojis o símbolos gráficos pesados.
  - Actualización de estado en una línea dinámica durante la fase de lectura/búsqueda.

## 3. Síntesis y Arquitectura Aplicada a Bridge Chat
Combinamos lo mejor de ambos mundos eliminando la sobrecarga:
1. **SSOT (Single Source of Truth):**
   - Clase `Theme`: define toda la paleta ANSI y elementos de dibujo sobrios.
2. **SOLID & SoC (Separation of Concerns):**
   - `ActivityConsolidator`: agrupa automáticamente herramientas de sólo lectura (`read_file`, `search_files`, `list_dir`, `file_ops`) en una sola línea condensada:
     `* Inspected 4 files/locations (file1.py, file2.py, ...) | 04:20:00`
     evitando imprimir 20 bloques separados que saturan la terminal.
   - `TuiRenderer`: capa pura de renderizado tipográfico. Se eliminaron todos los emojis e iconos unicode pesados (`❓`, `✻`, `⚡`, `🔧`, `✓`, `⏳`, `⏺`, `●`), reemplazándolos por una notación sobria y técnica (`*`, `|`, `[ok]`, `[queued]`, `[Agent]`).
   - `TuiLineEditor`: control directo de buffer de entrada con cursor bidireccional (←/→, Inicio/Fin, Supr) e historial (^/v).
   - `BridgeClient`: cliente HTTP desacoplado con fallback atómico offline.
   - `MessengerController`: orquestador de ciclo de vida y tabla de despacho de slash commands.
3. **DRY:**
   - Cero duplicación de cadenas de escape ANSI, lógica de formateo unificada y centralización del cálculo de ancho de terminal.
