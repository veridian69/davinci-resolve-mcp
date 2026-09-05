# Subtítulos autogenerados con whisperX

**Fecha:** 2026-07-24
**Estado:** diseño aprobado y completo; el camino de import quedó medido, no supuesto
**Contexto previo:** [`2026-07-24-resolve-inproc-mcp-design.md`](2026-07-24-resolve-inproc-mcp-design.md)

## Objetivo

Generar subtítulos con timing por palabra y separación por hablante, desde la
edición gratuita de Resolve, sin depender de ninguna función de Studio.

Reemplaza funcionalmente a `Timeline.CreateSubtitlesFromAudio` (Studio-only) y a
los nueve tools de transcripción marcados `studio_risk` en la cartografía.

## Restricciones verificadas

Todo lo de esta tabla se comprobó contra la fuente, no contra la memoria.

| Restricción | Evidencia | Consecuencia |
|---|---|---|
| whisperX pide Python `>=3.10,<3.14` | `pyproject.toml` de whisperX | La consola de Resolve corre 3.14. **Imposible** instalarlo ahí. Subprocess obligatorio. |
| CTranslate2 no tiene backend Metal | [hardware_support](https://opennmt.net/CTranslate2/hardware_support.html): CPU x86-64 + ARM64, GPU **solo NVIDIA** | El ASR va por CPU en Apple Silicon. No hay whisperX "optimizado para M-series" y no puede haberlo mientras dependa de ctranslate2. |
| faster-whisper rechaza `mps` | `ValueError: unsupported device mps` | `--device mps` muere en la primera etapa. |
| El CLI tiene un solo `--device` | 49 flags leídos del `argparse` de `whisperx/__main__.py` | No se puede poner ASR en CPU y alineación en MPS desde el CLI. |
| `whisperx.align(segments, ...)` acepta segmentos de cualquier ASR | README de whisperX | Deja abierta una variante híbrida. Ver "Descartado por ahora". |
| `torch~=2.8` + `pyannote-audio>=4.0` | `pyproject.toml` | ~2-3 GB. Venv propio, fuera del repo y fuera de `.inproc/deps`. |
| Diarización necesita modelos gated de HF | `--hf_token`, `--diarize_model pyannote/speaker-diarization-community-1` | Credencial del usuario. Ver "Seguridad". |

El wrapper **no** pisa `--device` ni `--compute_type`. whisperX ya los
resuelve solo (`"cuda" if torch.cuda.is_available() else "cpu"`, y un
compute type `"default"` que significa float16 en GPU y float32 en CPU).
Mandar los nuestros anularía esa deteccion: clavar `cpu` apagaria una GPU que
existe, y clavar `int8` cambiaria precision por velocidad sin que nadie lo
pidiera. Pasan solo cuando el caller los nombra.

Esto estuvo mal en una version anterior de este spec, que afirmaba que los
defaults eran `cuda`/`float16`. Venia del README de whisperX, no del argparse.

## Arquitectura

```
timeline item
   -> ruta del archivo fuente + source in/out        [API de Resolve]
   -> ffmpeg -ss/-to -> WAV 16k mono                 [dir de análisis]
   -> whisperx --diarize                             [subprocess, venv propio, CPU]
   -> JSON: words con start/end/speaker              [tiempo de extracto]
   -> +offset -> tiempo de fuente -> tiempo de timeline
   -> agrupar por speaker -> un SRT por voz
   -> ImportMedia(srt) -> AppendToTimeline(trackIndex)  [una pista por hablante]
```

Dos tools MCP, no una:

- **`whisperx_transcribe_timeline_item`** — wrapper. Los flags del CLI pasan tal
  cual. No toca Resolve más que para resolver el archivo fuente y el rango.
- **`whisperx_import_subtitles`** — mete los SRT en el timeline. Su forma depende
  de la sonda.

Están separadas porque la transcripción tarda minutos y falla por razones
propias (modelo ausente, token de HF, memoria). Fusionarlas significaría que un
fallo de import obliga a re-transcribir todo.

### Por qué extraer el audio

whisperX ya decodifica internamente a 16k mono en memoria y reusa ese array
entre ASR, alineación y diarización. Extraer un WAV **completo** antes es
redundante.

El caso que sí lo justifica es el **rango**: si el timeline usa 8 segundos de un
clip de dos horas, apuntarle whisperX al archivo transcribe las dos horas. En
CPU, que es donde estamos clavados, esa es la diferencia entre usable e
inusable.

Por eso la extracción es opt-in: prendida por default cuando hay `source in/out`,
apagada cuando se transcribe un archivo suelto.

`AGENTS.md` prohíbe transcodificar o crear copias "amigables para análisis"
*salvo pedido explícito del usuario*. Este pedido existe y está registrado. El
WAV va al directorio de análisis, **nunca** al lado del material fuente, y el
original no se toca ni se renombra.

Lo que esto **no** resuelve: el audio mezclado del timeline. ffmpeg no lee un
timeline de Resolve; eso requeriría un render y queda fuera de alcance.

## La cadena de tres tiempos

Es donde va a estar el bug, no en whisperX.

| Tiempo | Origen | Convertir con |
|---|---|---|
| extracto | lo que devuelve whisperX | — |
| fuente | `extracto + offset_del_-ss` | el `-ss` que le pasamos a ffmpeg |
| timeline | `fuente - GetSourceStartFrame() + GetStart()` | ambos verificados en la referencia, `:437-438` |

Un error de un frame acá deriva a lo largo de todo el corte y se ve. Las
conversiones van en funciones puras, con tests propios, separadas del I/O:
`free_edition/subtitles/timing.py`, cero imports, aritmética y nada más.

Frames vs segundos: whisperX trabaja en segundos flotantes, Resolve en frames.
La conversión usa el frame rate del timeline, y redondea **una sola vez**, al
final. Redondear en cada paso acumula error.

### El mapeo lineal solo vale al 100%

La fórmula de arriba asume que un segundo de fuente es un segundo de timeline.
En un clip con retime **eso es falso**, y falso de una manera que crece: a 50%
de velocidad, la última palabra de un clip de un minuto cae treinta segundos
lejos de donde el subtítulo la pone.

`GetLeftOffset()` (`:435`) da la extensión pero no el factor de velocidad, y una
rampa de velocidad no es siquiera un factor único.

Decisión: **detectar el retime y negarse**, con un mensaje que nombre el clip.
Un subtítulo que deriva medio minuto es peor que un tool que dice que no puede.
Soportar retimes queda fuera de alcance hasta que haya un caso real que lo pida.

## Puntos de integración

Este spec se escribió cuando el plan era poner la lógica en
`src/utils/media_analysis.py` y las tools en `src/granular/subtitles.py`. **Esa
premisa se invirtió.** La regla del fork es cero archivos de upstream
modificados, así que nada de esto vive en `src/`:

| Qué | Dónde vive |
|---|---|
| Backend whisperX (argv, env, ejecutable, transcripción) | `free_edition/subtitles/whisperx.py` |
| Cadena de tiempos, funciones puras | `free_edition/subtitles/timing.py` |
| Agrupado por hablante y escritura de SRT | `free_edition/subtitles/srt.py` |
| Las dos tools MCP | `free_edition/subtitles/tools.py` |
| El registro contra upstream | `free_edition/integrate.py` |

Las tools siguen colgando del `mcp` granular (`src/granular/common.py:90`), que
es el que sirve el puente in-process — pero por `from src.granular.common import *`
desde `free_edition/subtitles/tools.py`, importado por `integrate.py` **después**
de `from src.granular import mcp`. Los dos decoradores `@mcp.tool()` disparan en
ese momento y aterrizan en la misma instancia de siempre. `src/granular/__init__.py`
no las menciona.

El repo tiene **dos** instancias `FastMCP`: la granular (341 tools de upstream,
343 con estas dos, la que sirve el puente free) y la compound (`src/server.py:153`,
34 tools, donde hoy cuelga media analysis). Sin esto, whisperX quedaría
inalcanzable desde Resolve free.

### Los cinco enganches, ahora en runtime

Eran cinco ediciones a `media_analysis.py`, copiando el patrón de `whisper_cli`.
Hoy son cinco mutaciones que `integrate.register_whisperx()` aplica sobre el
módulo ya importado:

| Enganche | Cómo se aplica en runtime |
|---|---|
| entrada `whisperx` en `TOOL_INSTALL` (el dict declarado en `:1400`), bundle `transcription`, verify `whisperx --version` | mutación del dict: `TOOL_INSTALL["whisperx"] = TOOL_INSTALL_ENTRY` |
| detección: `shutil.which("whisperx")` con override por `WHISPERX_BIN` — vive en venv propio, no en el PATH del server | wrapper de `detect_capabilities` que post-procesa el valor de retorno |
| agregar `whisperx` a la lista de backends | el mismo wrapper; `insert(0)`, no `append`, que es lo que lo vuelve el default implícito |
| `_transcribe_with_whisperx(path, artifacts, transcription)` | vive en `free_edition/subtitles/whisperx.py` |
| las dos ramas de dispatch | `media_analysis._transcribe = wrapper`, entero |

Dos advertencias que costaron encontrarse:

**`detect_capabilities` está capturado a nivel de módulo en cuatro lados**, no en
uno: `src/utils/media_analysis_jobs.py`, `src/batch_cli.py`,
`src/analysis_dashboard.py` y `src/server.py`. Rebindear solo el módulo original
deja tres copias viejas. `integrate.register_whisperx()` rebinde los cuatro.

**El dispatch se reemplaza entero, no por ramas.** `_run_backend` es un closure
definido dentro de `_transcribe`; nada desde afuera del módulo puede alcanzarlo.
Por eso el enganche es `media_analysis._transcribe = wrapper`. Funciona porque los
dos callers lo resuelven en tiempo de llamada: `media_analysis.py:5569` es un
lookup de global de módulo y `src/analysis_dashboard.py:13423` es un import local
por llamada.

El patrón original de dos sitios de dispatch —`_run_backend` devolviendo
`fallthrough` y el bloque externo decidiendo qué significa— sigue siendo el que
hay que entender para leer el código de upstream. Cablear solo uno dejaba el
backend anunciado pero muerto; es exactamente lo que le pasa hoy a `whisper_cpp`,
sin implementar a propósito y declarado con `status: "not_implemented"` — el
patrón a seguir, no un bug a copiar.

Se reusa de upstream, sin tocarlo, por import: `_normalize_transcript_payload`,
`_write_transcript_artifacts`, `segments_to_srt`, `seconds_to_srt_time`, el gate
de `allow_model_download`, el refusal por duración y el timeout de wall-clock.

La excepción es `_run_command`: la versión de upstream no acepta `env=`, y
whisperX necesita `HF_TOKEN` en el entorno del subprocess. Ensancharle la firma
sería editar un archivo de upstream por diez líneas de código, así que
`free_edition/subtitles/whisperx.py` se trae su propio runner. Los otros cuatro
call sites de upstream nunca pasan `env`, así que upstream no necesita nada.

## Diarización: el label se cae

Agregar el backend no alcanza. La normalización de upstream descarta el hablante:

- `_normalize_word_timestamps` arma cada palabra con `word/start/end` y solo
  conserva `probability`, `confidence`, `score`.
- `_normalize_transcript_payload` arma cada segmento con `start/end/text`.

`speaker` se pierde en los dos lados. Sin arreglar eso, la diarización entra por
una punta y sale por ninguna.

El plan original era hacerlo aditivo dentro de esas dos funciones: conservar
`speaker` cuando está presente. No afectaba a `whisper_cli` ni a `mlx_whisper`,
que nunca lo emiten — pero eran dos ediciones a un archivo de upstream.

La versión runtime lo resuelve **después**, no adentro: `_reattach_speaker_labels()`
corre como post-pass dentro de nuestro propio `_transcribe_with_whisperx`,
haciendo zip del payload crudo contra el normalizado y devolviendo el `speaker` a
su lugar. El alineamiento es exacto porque el loop de upstream no tiene guard de
`isinstance` ni `continue`: emite un segmento normalizado por cada segmento
crudo, en orden. Mismo resultado, cero archivos de upstream tocados.

Partir por hablante es post-proceso del JSON, código nuestro: whisperX escribe
**un** SRT con todos mezclados. Agrupar segmentos por `segment["speaker"]` y
emitir un SRT por grupo con `segments_to_srt`, que ya existe. Es poco código,
pero conviene nombrarlo: esta parte **no** es wrappear el CLI.

## Seguridad: el token de HF

El token va por **entorno** (`HF_TOKEN`), no por `--hf_token` en argv.

- Un token en la línea de comando lo ve cualquier proceso de la máquina con
  `ps aux`.
- `_transcribe_with_whisper_cli` devuelve `stderr` crudo al campo de error;
  argparse puede eco de argumentos en un fallo.

`_run_command` no loguea argv, lo cual ayuda, pero no cubre `ps`.

El token no se escribe en ningún artefacto, ni en el JSON de transcripción, ni
en el log. Lo provee el usuario; no se genera ni se pide interactivamente.

**Verificado** contra el código instalado (3.8.6), que era lo que faltaba: en
`whisperx/transcribe.py`, cuando `--hf_token` está ausente el propio whisperX
advierte que el token *"needs to be saved in environment variable"* y pasa
`use_auth_token=None` a `Pipeline.from_pretrained`, donde huggingface_hub cae al
token del entorno. La variable no es un rodeo: es el camino que whisperX
documenta en su propio warning.

## Lado Resolve — resuelto, rama A

Medido con `free_edition/tools/probe_subtitle_import.py` en Resolve 21.0.3.7
free, timeline de scratch:

| Llamada | Resultado |
|---|---|
| `AddTrack("subtitle")` | `True`; el contador pasa de 0 a 1 |
| `MediaPool.ImportMedia([srt])` | devuelve un MediaPoolItem con `Type: 'Subtitle'` |
| `AppendToTimeline([item])` | devuelve 1 item, y la pista queda con **3** |

El SRT de prueba tenía tres cues: Resolve lo importó como un clip y lo expandió
en tres items de subtítulo sobre la pista. `Duration` salió `00:00:01:12`, que a
24 fps es exactamente el 0.5s→2.0s del archivo. No solo lo aceptó, lo
interpretó bien.

**El camino es un archivo de texto, no miles de objetos de Fusion.** La rama B
queda descartada y no necesita spec propio.

Para una pista por hablante:
`AppendToTimeline([{"mediaPoolItem": item, "trackIndex": n, "recordFrame": f}])`
(`:223`), y `SetTrackName("subtitle", n, speaker)` (`:379`) para nombrarlas.

**Sin verificar todavía**, y es lo único que queda del lado Resolve: los
`mediaType` documentados son 1=video y 2=audio, sin valor para subtítulo, así
que falta comprobar que `trackIndex` rutee a la pista de subtítulos correcta
cuando el item es de tipo `Subtitle`. Es el argumento de una llamada, no una
decisión de arquitectura.

## Apéndice: la pregunta que estaba bloqueada

La referencia de la API documenta pistas de subtítulo (`AddTrack`,
`GetTrackCount`, `GetItemListInTrack` aceptan `"subtitle"`;
`TimelineItem.GetTrackTypeAndIndex()` devuelve `"subtitle"`) pero **no documenta
ninguna forma de crear un item de subtítulo**. `CreateSubtitlesFromAudio` es el
único productor documentado, y es Studio-only. `ImportIntoTimeline` es solo AAF.
`ImportTimelineFromFile` es AAF/EDL/XML/FCPXML/DRT/ADL/OTIO.

No hay una sola línea en el repo que importe un `.srt`, así que no había forma
de contestarlo leyendo código. Se contestó midiendo, con
`free_edition/tools/probe_subtitle_import.py`, que se crea su propio timeline de
scratch, prueba las tres llamadas y borra todo en un `finally`.

Se guarda esto porque la conclusión —que la API acepta `.srt` aunque no lo
documente— es exactamente el tipo de cosa que alguien va a volver a dudar, y
porque la sonda sigue sirviendo para reconfirmarla contra una versión nueva de
Resolve.

## Medido contra whisperX real

whisperx 3.8.6 sobre Python 3.12.12, macOS, CPU. Audio de prueba: cuatro turnos
alternados de dos voces sintéticas, 16.2 s.

| Qué | Resultado |
|---|---|
| Pipeline completo | VAD → transcripción → alineación → diarización, exit 0 |
| Diarización | 4 segmentos, 2 hablantes, **las cuatro asignaciones correctas** contra la construcción del audio |
| Etiquetas tras normalizar | 60/60 palabras y 4/4 segmentos conservan `speaker` |
| Agrupado | 2 pistas, 2 cues cada una, SRT bien formado |
| Extracción de rango | 5.0–11.0 s de un archivo de 21 s → 6.000000 s exactos, 16000 Hz, mono |

**El token por entorno quedó confirmado por observación.** whisperX loguea
*"No --hf_token provided, needs to be saved in environment variable"* y acto
seguido carga el modelo gated y diariza. El warning es sobre el flag ausente,
no sobre el token: huggingface_hub lo tomó de `HF_TOKEN`.

Dos cosas que solo aparecieron corriendo lo real:

1. **La etiqueta de una palabra puede discrepar de la de su segmento** en el
   borde de un turno. Era hipotético cuando se escribió el test; está observado.
2. **El VAD descarta audio que no reconoce como habla.** Un primer fixture usó
   una voz de personaje de macOS y whisper la alucinó como `"¡Suscríbete!"`,
   perdiendo 12 de 21 segundos. No es un fallo del pipeline, pero sí un aviso:
   material con audio pobre va a perder tramos en silencio.

**Sin medir:** velocidad sobre material real. El fixture es de 16 segundos.

## Testing

- **Backend:** un `whisperx` falso en el PATH que escupe JSON fijo con
  `speaker`. Sin descargar modelos, sin GPU, corre en CI. Cubre construcción de
  argv, defaults de macOS, parseo y que el token **no** aparezca en argv.
- **Cadena de tiempos:** funciones puras, tests de tabla. Incluye timeline a
  23.976 y un test que verifica que un clip con retime se **rechaza** en vez de
  producir tiempos que derivan.
- **Agrupado por hablante:** puro, sin I/O.
- **Enganches de runtime:** los tests del normalizador y los del dispatcher
  entran por `integrate.register_whisperx()`, no por los helpers de upstream. Un
  test que llame directo al helper pasa mientras producción se rompe en silencio,
  porque lo que puede fallar es el rebinde, no la función.
- **Lado Resolve:** al harness offline (`free_edition/tools/fake_console.py`).

La suite vive en `free_edition/tests/`. El repo tiene 1501 tests pasando; esto se
suma a esa suite, no al lado.

## Descartado por ahora

**Híbrido MLX.** `whisperx.align()` acepta segmentos de cualquier ASR, así que se
podría hacer el ASR en Metal con `mlx_whisper` (que ya es un backend de este
repo) y después alinear y diarizar con whisperX sobre MPS. Ganaría Metal en las
tres etapas que pueden usarlo.

Se descarta porque deja de ser un wrapper del CLI y pasa a ser uso de la API
Python de whisperX, con su propia gestión de modelos y dispositivo: más código
nuestro y más superficie para romperse cuando whisperX cambie internamente.

Queda como segunda entrada en el registro de backends si el CPU resulta
insoportable sobre material real. No se descarta por técnica sino por orden.

## Riesgos

1. **Velocidad en CPU sin medir.** No hay benchmark propio y no se inventa uno.
   El primer material real decide si esto es utilizable o si hay que ir al
   híbrido.
2. **La rama B puede duplicar el alcance.** Depende de un resultado que todavía
   no tenemos.
3. **Modelos gated de HF.** Requieren aceptar términos en la web una vez. No es
   automatizable y no debe serlo.
4. **Deriva de tiempos.** Mitigada con funciones puras y tests de tabla, pero es
   el fallo más probable y el más difícil de ver en una revisión de código.
5. **Los enganches de runtime no dan conflicto de merge.** Si upstream renombra
   `detect_capabilities`, `_transcribe` o `TOOL_INSTALL`, el registro falla en
   silencio en vez de romper el merge. Es el precio de no tocar `src/`; los tests
   de `free_edition/tests/` son la única alarma.
