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

Defaults del CLI que el wrapper tiene que pisar en macOS: `--device` viene
`cuda` y `--compute_type` viene `float16`. Ninguno de los dos existe acá; van
`cpu` e `int8`.

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

- **`whisperx_transcribe`** — wrapper. Los flags del CLI pasan tal cual. No toca
  Resolve.
- **`import_subtitles`** — mete los SRT en el timeline. Su forma depende de la
  sonda.

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
conversiones van en funciones puras, con tests propios, separadas del I/O.

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

La lógica va a `src/utils/media_analysis.py`, donde ya vive su familia. Las
tools MCP van a `src/granular/subtitles.py`, colgadas del `mcp` granular
(`src/granular/common.py:90`), que es el que sirve el puente in-process. Hay
precedente para que granular importe de utils: `src/granular/media_pool_item.py:4`.

El repo tiene **dos** instancias `FastMCP`: la granular (341 tools, la que sirve
el puente free) y la compound (`src/server.py:153`, 34 tools, donde hoy cuelga
media analysis). Sin esto, whisperX quedaría inalcanzable desde Resolve free.

Cinco enganches en `media_analysis.py`, copiando el patrón de `whisper_cli`:

| Línea | Qué agregar |
|---|---|
| `~1489` | entrada `whisperx` en `TOOL_REQUIREMENTS`, bundle `transcription`, verify `whisperx --version` |
| `~1617` | detección: `shutil.which("whisperx")` con override por `WHISPERX_BIN` — vive en venv propio, no en el PATH del server |
| `1676` | agregar `whisperx` a la lista de backends |
| nuevo | `_transcribe_with_whisperx(path, artifacts, transcription)`, firma idéntica a `:3881` y `:3916` |
| `3978` **y** `4018` | ramas de dispatch |

Los dos últimos son **dos** sitios, no uno. `_run_backend` (`:3972`) devuelve
`fallthrough` para lo que no conoce y el bloque externo (`:4014-4032`) decide qué
significa. Cablear solo uno deja el backend anunciado pero muerto. Es
exactamente lo que le pasa hoy a `whisper_cpp`, que está sin implementar a
propósito y lo declara con `status: "not_implemented"` en `:4020` — el patrón a
seguir, no un bug a copiar.

Se reusa sin tocar: `_normalize_transcript_payload` (`:3836`),
`_write_transcript_artifacts` (`:3872`), `segments_to_srt` (`:3301`),
`seconds_to_srt_time` (`:3289`), `_run_command` (`:2257`), el gate de
`allow_model_download`, el refusal por duración y el timeout de wall-clock.

## Diarización: el label se cae

Agregar el backend no alcanza. La normalización actual descarta el hablante:

- `_normalize_word_timestamps` (`:3813-3833`) arma cada palabra con
  `word/start/end` y solo conserva `probability`, `confidence`, `score`.
- `_normalize_transcript_payload` (`:3844-3848`) arma cada segmento con
  `start/end/text`.

`speaker` se pierde en los dos lados. Sin arreglar eso, la diarización entra por
una punta y sale por ninguna.

El cambio es aditivo: conservar `speaker` cuando está presente. No afecta a
`whisper_cli` ni a `mlx_whisper`, que nunca lo emiten.

Partir por hablante es post-proceso del JSON, código nuestro: whisperX escribe
**un** SRT con todos mezclados. Agrupar segmentos por `segment["speaker"]` y
emitir un SRT por grupo con `segments_to_srt`, que ya existe. Es poco código,
pero conviene nombrarlo: esta parte **no** es wrappear el CLI.

## Seguridad: el token de HF

El token va por **entorno** (`HF_TOKEN`), no por `--hf_token` en argv.

- Un token en la línea de comando lo ve cualquier proceso de la máquina con
  `ps aux`.
- `_transcribe_with_whisper_cli` (`:3906`) devuelve `stderr` crudo al campo de
  error; argparse puede eco de argumentos en un fallo.

`_run_command` (`:2257`) no loguea argv, lo cual ayuda, pero no cubre `ps`.

El token no se escribe en ningún artefacto, ni en el JSON de transcripción, ni
en el log. Lo provee el usuario; no se genera ni se pide interactivamente.

**Verificado** contra el código instalado (3.8.6), que era lo que faltaba: en
`whisperx/transcribe.py`, cuando `--hf_token` está ausente el propio whisperX
advierte que el token *"needs to be saved in environment variable"* y pasa
`use_auth_token=None` a `Pipeline.from_pretrained`, donde huggingface_hub cae al
token del entorno. La variable no es un rodeo: es el camino que whisperX
documenta en su propio warning.

## Lado Resolve — resuelto, rama A

Medido con `tools/probe_subtitle_import.py` en Resolve 21.0.3.7 free, timeline
de scratch:

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

No hay una sola línea en el repo que importe un `.srt`.

No hay una sola línea en el repo que importe un `.srt`, así que no había forma
de contestarlo leyendo código. Se contestó midiendo, con
`tools/probe_subtitle_import.py`, que se crea su propio timeline de scratch,
prueba las tres llamadas y borra todo en un `finally`.

Se guarda esto porque la conclusión —que la API acepta `.srt` aunque no lo
documente— es exactamente el tipo de cosa que alguien va a volver a dudar, y
porque la sonda sigue sirviendo para reconfirmarla contra una versión nueva de
Resolve.

## Testing

- **Backend:** un `whisperx` falso en el PATH que escupe JSON fijo con
  `speaker`. Sin descargar modelos, sin GPU, corre en CI. Cubre construcción de
  argv, defaults de macOS, parseo y que el token **no** aparezca en argv.
- **Cadena de tiempos:** funciones puras, tests de tabla. Incluye timeline a
  23.976 y un test que verifica que un clip con retime se **rechaza** en vez de
  producir tiempos que derivan.
- **Agrupado por hablante:** puro, sin I/O.
- **Lado Resolve:** al harness offline (`tools/fake_console.py`).

El repo tiene 1501 tests pasando; esto se suma a esa suite, no al lado.

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
