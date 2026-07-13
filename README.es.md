# Panel de uso de tokens

[🇨🇳 简体中文](README.md) · [🇹🇼 繁體中文](README.zh-TW.md) · [🇺🇸 English](README.en.md) · [🇯🇵 日本語](README.ja.md) · [🇰🇷 한국어](README.ko.md) · 🇪🇸 **Español**

Panel web local para escritorio que registra el uso de tokens de **Claude Code**, **Codex**, **OpenCode**, **OpenClaw**, **Hermes** y **Grok**, con resúmenes diarios, semanales, mensuales y acumulados. Los registros se procesan de forma local, **sin dependencias de terceros ni llamadas a API externas, salvo el tipo de cambio**. No incluye adaptación para móviles.

## Fuentes de datos

| Fuente | Ruta | Método |
|---|---|---|
| Claude | `~/.claude/projects/**/*.jsonl` | Lee `message.usage` del assistant, elimina duplicados por `message.id` y separa `usage.iterations` de fallback por su modelo real |
| Codex | `~/.codex/sessions` + `archived_sessions` | Calcula diferencias consecutivas de `total_token_usage`; la primera instantánea de un archivo subagent derivado solo sirve como referencia |
| OpenCode | `~/.local/share/opencode/opencode.db` | Lee SQLite directamente y sincroniza por la marca de tiempo del mensaje; los reasoning tokens se incluyen en output |
| OpenClaw | `~/.openclaw/agents/main/sessions/*.jsonl` | Admite formatos trajectory y v3 y elimina llamadas idénticas duplicadas entre ambos |
| Hermes | `~/.hermes/state.db` | Lee filas acumuladas de session y sincroniza reemplazos; reasoning es un subconjunto de output y no se suma dos veces |
| Grok | `~/.grok/logs/unified.jsonl` | Lee tokens incrementales de `shell.turn.inference_done` generados por Grok CLI o transcripciones de la API de claude-mem; prioriza model/cwd incluidos en el evento y, si faltan, los conserva por sid |

Las reglas críticas de deduplicación, diferencias y referencia de fork de Claude y Codex tienen pruebas de regresión. El resultado depende del formato de registro y del historial local de cada herramienta. Usa la auditoría de ejecución para comprobar la actualidad de las fuentes y los modelos desconocidos.

> Las estadísticas de Grok requieren un `unified.jsonl` existente. Este proyecto solo lee el archivo; no instala los hooks de transcripción de Grok/claude-mem. Configura `TOKENSTAT_GROK_LOG` si se encuentra en otra ruta.

## Inicio rápido

Requiere Python 3.9 o posterior y solo utiliza la biblioteca estándar. **No hace falta ejecutar `pip install`.**

```bash
git clone https://github.com/XX888QM/tokentongji.git
cd tokentongji

# 1) Se recomienda una primera ingesta completa (depende del historial)
PYTHONPATH=src python3 -m tokenstat.ingest

# 2) Inicia el servicio web y la ingesta incremental cada 60 segundos
PYTHONPATH=src python3 -m tokenstat.server

# 3) Abre el panel
open http://127.0.0.1:8787
```

## Funciones del panel

- Tokens de hoy, últimos 7 días, mes actual y acumulado, con coste estimado en CNY y proporción por fuente
- Unidades numéricas chinas (`万 / 亿 / 万亿 / 京 / 垓`) y valor exacto al pasar el cursor
- Gráfico de tendencia de tokens por fuente durante 30 días
- Desglose por modelo y proyecto (cwd), con costes, cache tokens, totales y selector de periodo
- Auditoría de ejecución: rutas, progreso de ingesta, modelos desconocidos y sesiones con fuentes mezcladas
- Análisis de anomalías: mayores contribuciones por modelo/proyecto y comparación con referencias
- Las 10 sesiones más costosas con detalle por modelo y archivo de origen
- Actualización automática cada 30 segundos

Los costes se muestran en CNY. La página usa de inmediato el tipo de cambio almacenado (7,25 en el primer inicio), mientras el servidor actualiza USD→CNY desde `open.er-api.com` en segundo plano y lo guarda durante una hora. Los fallos de red no bloquean el panel.

## Inicio de sesión automático (opcional, solo macOS)

```bash
# Genera y carga un plist con la ruta actual del proyecto
bash scripts/install-launchd.sh

# Desinstala
bash scripts/uninstall-launchd.sh
```

Registros: `data/tokenstat.log` / `data/tokenstat.err.log`

El instalador solo define el puerto predeterminado y `PYTHONPATH`; no hereda otras variables `TOKENSTAT_*` exportadas en la terminal. Edita la plantilla plist y reinstala si launchd necesita una configuración personalizada. El reinicio KeepAlive puede ser inestable en algunas versiones de macOS; utiliza el inicio en primer plano u otro gestor de procesos cuando sea necesario. No inicies dos servicios en el mismo puerto.

## Configuración

| Variable | Predeterminado | Descripción |
|---|---|---|
| `TOKENSTAT_HOST` | 127.0.0.1 | Dirección de escucha |
| `TOKENSTAT_PORT` | 8787 | Puerto web; debe ser un entero positivo |
| `TOKENSTAT_INGEST_INTERVAL` | 60 | Intervalo de ingesta en segundo plano, en segundos; debe ser positivo |
| `TOKENSTAT_REFRESH` | 30 | Intervalo de actualización del panel, en segundos; debe ser positivo |
| `TOKENSTAT_STALE_DAYS` | 3 | Días sin datos nuevos, o de retraso frente a otras fuentes, antes de mostrar una alerta |
| `TOKENSTAT_DATA_DIR` | `./data` | Directorio de SQLite y registros |
| `TOKENSTAT_GROK_LOG` | `~/.grok/logs/unified.jsonl` | Ruta del registro unificado de Grok |

Los precios se configuran en `src/tokenstat/pricing.json` en USD por millón de tokens. Los modelos locales o autoalojados usan la sección `local` con tarifa cero. `codex-auto-review` y `gpt-5-codex` se estiman con el precio público de OpenAI Codex para `gpt-5.3-codex`.

**Nota:** Con suscripciones Claude Max, Codex o Grok, el uso de tokens no equivale directamente a un cargo. Todos los costes son estimaciones orientativas.

## Pruebas

Node.js solo es necesario para las pruebas de regresión del formato de importes del frontend. El panel solo necesita Python para funcionar.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Solución de problemas

- El panel solo muestra la estructura: abre `http://127.0.0.1:8787/api/health`. Si no responde, el servicio está detenido o el puerto está ocupado.
- Falta una fuente: comprueba la ruta correspondiente y la auditoría de ejecución. La ausencia de una fuente no impide cargar las demás.
- Dirección en uso: detén el servicio manual o de launchd existente, o configura otro `TOKENSTAT_PORT`.

## Arquitectura

```text
src/tokenstat/
  config.py      Rutas, puertos e intervalos
  models.py      Modelo UsageRecord normalizado
  db.py          Deduplicación SQLite y puntos de control de ingesta
  parsers/
    claude.py    Deduplicación message-id de Claude y fallback iterations
    codex.py     Diferencias de acumulados de Codex y conservación de contexto
    opencode.py  Lectura incremental directa de SQLite de OpenCode
    openclaw.py  Formatos trajectory y v3 de OpenClaw
    hermes.py    Sincronización completa de sessions SQLite de Hermes
    grok.py      Eventos inference_done de Grok y conservación por sid
  ingest.py      Ingesta incremental por byte offset
  pricing.py     Estimación de costes y normalización de modelos
  pricing.json   Tarifas anthropic / openai / deepseek / xai / local
  aggregate.py   Consultas diarias, semanales, mensuales y acumuladas
  server.py      API HTTP, archivos estáticos, tipos de cambio e hilo de ingesta
  static/        index.html / app.js / styles.css / chart.min.js
```

`docs/superpowers/` contiene registros fechados de diseño e implementación, no la guía de uso actual. El comportamiento vigente se define en el README principal, `CLAUDE.md`, el código y las pruebas.
