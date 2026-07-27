# Servidor MCP in-process para DaVinci Resolve (edición gratis)

Fecha: 2026-07-24
Estado: diseño aprobado, validado end-to-end con un prototipo
Autor: Juan + Claude

## Problema

`davinci-resolve-mcp` asume que el servidor MCP corre como proceso externo y se
conecta a Resolve por `DaVinciResolveScript.scriptapp("Resolve")`. Esa llamada
depende del script server interno de Fusion, que **la edición gratis desactiva**.
El autor del repo lo documenta: sin Studio, el MCP no conecta.

La limitación es real y no se puede eludir desde afuera.

## Por qué esto funciona igual

La consola de Resolve (Workspace → Console, dropdown Py3) corre **dentro** del
proceso de Resolve, con el objeto `resolve` ya inyectado en su namespace. No
carga `fusionscript`, no abre socket, no pasa por el gate. Es una puerta distinta
al mismo cuarto.

El diseño consiste en mudar el servidor MCP entero a ese lado de la puerta, en
vez de construir un túnel a través de ella.

## Decisiones

| Decisión | Elegido | Motivo |
|---|---|---|
| Topología | Servidor MCP in-process | Cero proxy: `resolve` es el objeto nativo. Elimina toda una clase de bugs de semántica |
| Superficie | Granular, 341 tools | Lectura literal de "todas las tools live". Además no usa `sys.executable`, así que no hereda el problema de subprocesos |
| Repo | Fork completo | Permite arreglar lo que haga falta sin contorsiones |
| Transporte | `streamable-http` en loopback + bearer token | Ya existe en el repo (`src/utils/mcp_transport.py`) |

### Alternativas descartadas

**Puente HTTP con registro de handles** (stdlib puro, proxy propio) y **RPyC**
(netrefs transparentes). Ambas reconstruyen por red el acceso a atributos que
in-process ya es nativo. El argumento fuerte del HTTP era "cero instalación en el
Python de Resolve", válido sólo mientras el cliente MCP viviera afuera.

El argumento que las liquidó: se midió `hasattr(resolve, 'NoExisteEsteMetodo')`
contra el objeto **nativo** y da `True`. `PyRemoteObject` devuelve un callable
para cualquier nombre. Todo el proxy inteligente con probe cacheado existía para
no ser peor que nativo — pero nativo ya es ambiguo. In-process hereda exactamente
el comportamiento contra el que el repo fue escrito y probado.

## Arquitectura

```
proceso DaVinci Resolve
|
+- main thread -- UI + consola Py3
|    `- free_edition/boot/resolve_console_boot.py
|       (lo unico que se pega en la consola)
|         1. parche UTF-8 de open() + locale
|         2. ConsoleSafeStream + FileHandler
|         3. teardown del server previo + purga de sys.modules
|         4. sys.path += deps vendorizadas, repo
|         5. sys.modules['DaVinciResolveScript'] = shim
|         6. import src.granular
|         7. free_edition.integrate: registro en runtime
|            (tools de subtitulos, backend whisperX, no-autolaunch)
|         8. install_threaded_tool_dispatch + _bridge_lock
|         9. uvicorn en daemon thread
|
`- server thread (daemon)
     uvicorn + streamable-http -> 127.0.0.1:8765 (bearer token)
                  ^
                  | HTTP
          Claude Code / cliente MCP
```

El orden importa y está determinado por fallos observados, no por estética:

- **1 y 2 van antes que todo lo demás.** Si algo falla en el teardown o el
  import, el traceback tiene que sobrevivir. El primer fallo de lifespan del
  prototipo se perdió exactamente por no tener esto puesto todavía.
- **5 va antes que 6.** `granular/common.py:73` se agrega solo el directorio
  `Modules/` al `sys.path` y recién en la línea 234 hace el import. Sin el shim
  en `sys.modules`, carga el módulo real, choca contra el gate, y `resolve` queda
  en `None`. Peor: `common.py` atrapa el `ImportError` y deja `dvr_script = None`
  a nivel módulo, así que `_try_connect()` después falla con `AttributeError`. El
  módulo queda envenenado hasta reimportar — de ahí la purga del paso 3.
- **7 va antes que 8.** `install_threaded_tool_dispatch` envuelve las tools que
  existen **en ese momento**. Registrar después deja las dos tools de subtítulos
  sin envolver: correrían en el event loop, sin el `_bridge_lock`, contra una API
  que no es thread-safe. El wrapper es idempotente (saltea toda tool que ya tenga
  `is_async`), así que una segunda llamada a `dispatch.install()` es el cinturón
  y tiradores si el orden no se puede garantizar — pero el orden se puede
  garantizar y es lo que hace el boot.
- **La purga del paso 3 cubre `src.*` y `free_edition.*`.** Purgar solo `src.*`
  dejaría un `free_edition.integrate` viejo apuntando al `src.utils.media_analysis`
  anterior, y el módulo recién importado perdería en silencio todos los registros
  de runtime.

## Componentes

### Nuevos

| Archivo | Responsabilidad |
|---|---|
| `free_edition/boot/resolve_console_boot.py` | Punto de entrada. Idempotente. **ASCII puro** |
| `free_edition/inproc/encoding.py` | Parche de `open()` a UTF-8 y `locale.getpreferredencoding` |
| `free_edition/inproc/streams.py` | `ConsoleSafeStream` |
| `free_edition/inproc/shim.py` | Módulo falso `DaVinciResolveScript` |
| `free_edition/inproc/launcher.py` | uvicorn en thread, sin signal handlers, con `stop()` |
| `free_edition/inproc/dispatch.py` | `_bridge_lock` + `install_threaded_tool_dispatch` extraídos de `server.py` |
| `free_edition/integrate.py` | Todos los enganches al repo, aplicados en runtime |
| `free_edition/tools/setup_inproc.py` | Construye `.inproc/deps` con `pip install --target` |

### Parches al repo: ninguno

La versión original de este diseño parcheaba una línea del repo:
`src/granular/common.py:326`, para neutralizar `_launch_resolve()`. Defensivo:
con el shim, `_try_connect()` siempre gana, pero si el handle se pone stale
abriría una segunda instancia de Resolve.

Eso ya no se hace editando el archivo. `free_edition/integrate.py` rebinde
`_launch_resolve` en runtime, después del import, y lo hace en **los dos** sitios
que tienen esa función: `src/granular/common.py` y su duplicado byte a byte en
`src/server.py`. El parche estático sólo cubría el primero, así que el boot
compound todavía podía abrir una segunda instancia y bloquear ~60 s. La versión
runtime es estrictamente mejor, no meramente equivalente.

Regla del fork, sin excepciones: **cero archivos de `src/` modificados**. Todo lo
que la edición gratis necesita del repo se aplica desde `free_edition/integrate.py`
sobre los módulos ya importados. Eso es lo que hace que `git diff` contra el punto
de fork no toque nada fuera de `free_edition/`, y que el fork siga siendo
mergeable con upstream.

### Dependencias

`.inproc/deps/`, ignorado por git, 53 MB, 35 paquetes (`mcp[cli]`). Construido con
`/Library/Frameworks/Python.framework/Versions/3.14/bin/python3` para que el ABI
coincida con el intérprete de la consola. Los binarios nativos salen
`cpython-314-darwin`. Sin sudo, sin ensuciar el framework.

El `.gitignore` de la raíz es un archivo de upstream y queda pristino, así que
`.inproc/` se ignora a sí mismo: `setup_inproc.py` escribe `.inproc/.gitignore`
con un único `*`, que tapa todo el directorio incluido ese mismo archivo.

El user site-packages **no está** en el `sys.path` de la consola, así que
`pip install --user` no se vería. De ahí `--target` + `sys.path.insert`.

## Restricciones del entorno de la consola

Todas medidas, no supuestas. Cada una rompió el prototipo antes de ser tratada.

| Restricción | Evidencia | Tratamiento |
|---|---|---|
| `open()` default es ASCII | `locale.getencoding()` = `US-ASCII` | Parche de `builtins.open` a UTF-8 en el paso 1 |
| `sys.stdout` es `fu_stdout`, atado al thread de la consola vía PyCapsule | Escribir desde otro thread da `SystemError: <built-in function write> returned a result with an exception set`. El `handleError` de logging escribe a stderr y vuelve a fallar, destruyendo el traceback original | `ConsoleSafeStream`: consola si el thread es el de la consola, archivo si no |
| uvicorn `dictConfig` sondea `sys.stdout.isatty()` | `AttributeError: module 'fu_stdout' has no attribute 'isatty'` | `log_config=None` |
| Signal handlers sólo en el main thread | — | `install_signal_handlers` y `capture_signals` neutralizados |
| Una excepción en un thread muere muda | El fallo de Stage D no reportaba causa | Captura explícita del traceback fuera del thread |
| `SystemExit` en el main thread puede cerrar Resolve | — | Excepción propia para abortar |
| `sys.executable` es el binario de Resolve, no un Python | `/Applications/DaVinci Resolve/.../MacOS/Resolve` | No aplica a granular; sí bloquearía la superficie compound |

El filesystem encoding ya es UTF-8, así que los **paths** con acentos nunca
fueron problema. Lo que rompía era leer el **contenido** de archivos UTF-8.

## Validación

Prototipo corrido end-to-end contra Resolve 21.0.3.7 free en macOS 26.5.2 arm64.

| Etapa | Resultado |
|---|---|
| Deps vendorizadas importan bajo la consola | `pydantic_core`, `pydantic`, `anyio`, `starlette`, `uvicorn`, `mcp` — todas desde `.inproc/deps` |
| Shim gana sobre el módulo real | `granular.resolve is <handle de consola>` → `True` |
| Import del repo | 0.3 s, **341 tools** registradas |
| Server en background | uvicorn sirviendo, 401 sin token |
| Cliente MCP externo | `DaVinciResolveMCP`, protocolo `2025-11-25`, 341 tools anunciadas |
| Lectura | `get_current_database`, `get_database_list`, `get_current_project_folder`, `get_color_groups_list`, `get_current_render_mode`, `get_current_render_format_and_codec` — todas con datos reales |
| **Escritura** | `create_timeline` → visible; `timeline_add_marker` → leído de vuelta con color y nombre; `timeline_delete_marker_at_frame`; `delete_timelines_by_id` por `unique_id`; proyecto restaurado al baseline |
| Recarga | Server viejo frenado, nuevo levantado, cliente externo sigue andando |

Los números de arriba son del prototipo, anteriores a las dos tools de
subtítulos: hoy el boot registra 343. Los contadores estáticos de upstream
siguen diciendo 341 y está bien que lo hagan — cuentan los decoradores bajo
`src/granular/`, y esas dos viven en `free_edition/subtitles/tools.py`.

Los scripts sueltos del prototipo (`validate_inproc.py`, `stage_e_client.py`,
`write_test.py`) quedaron reemplazados por lo que sí se versionó: la validación
in-process vive en `free_edition/inproc/selftest.py` y
`free_edition/tools/fake_console.py`, y el cliente externo en
`free_edition/tools/verify_live.py`. La sonda de la consola sigue siendo un
artefacto propio: `free_edition/tools/probe_console.py`.

## Riesgos

1. **Cobertura real en la edición gratis.** `hasattr` siempre da `True`, así que
   los `hasattr` guards del repo que sondean la existencia de un método
   (`src/` tiene 49 usos de `hasattr` en total, falta clasificar cuántos son
   guards de versión) **ya estaban rotos contra Resolve nativo**, incluso en
   Studio: siempre toman la rama "API nueva". En free faltan métodos y
   el fallo aparece en la llamada, no en el guard. Hay que medirlo tool por tool.
   No es un problema del puente y no se puede arreglar con ningún esquema de
   proxy.
2. **Concurrencia.** El prototipo corrió sin `_bridge_lock`: los cuerpos sync
   ejecutaron en el event loop. Funcionó, lo que prueba que la API responde desde
   el thread del server, pero una tool lenta congela el transporte y dos llamadas
   no están serializadas contra una API que no soporta concurrencia. El dispatch
   con lock es obligatorio en la versión real.
3. **`builtins.open` parcheado a nivel proceso.** Lo hereda cualquier otro script
   que se corra en la consola. Aceptado conscientemente: la alternativa es
   arreglar cada `open()` del repo.
4. **Ciclo de vida atado a Resolve.** Si Resolve cierra, el server muere.
   Recuperación: re-pegar la línea del boot.
5. **Acoplamiento a internos del SDK.** `install_threaded_tool_dispatch` toca
   `ToolManager._tools` y `Tool.fn` / `Tool.is_async`. Mismo riesgo que ya asume
   el repo hoy.
6. **Acoplamiento a internos de upstream por registro en runtime.** `integrate.py`
   rebinde nombres de módulo de `src/` (`_launch_resolve`, `detect_capabilities`,
   `_transcribe`, `HTML`, `_mcp_status_payload`). Renombrar cualquiera de ellos
   upstream rompe el enganche en silencio, sin conflicto de merge que lo avise —
   ese es el precio de no tocar los archivos. Mitigación: los tests de
   `free_edition/tests/` fallan si un rebinde no encuentra su objetivo.

## Fuera de alcance

- La superficie compound (32 tools de `src/server.py`). Su camino de ejecución de
  scripts lanza subprocesos con `sys.executable`, que in-process relanzaría
  Resolve. Requiere rediseñar esas tools para ejecución in-process, con la
  regresión de que un script colgado ya no se puede matar.

  *(Nota posterior: se hizo igual, con un boot propio en
  `free_edition/boot/compound_console_boot.py` sobre el puerto 8768. La
  advertencia sobre `sys.executable` sigue en pie para las tools que lanzan
  scripts; el resto de la superficie funciona.)*
- Windows y Linux. El diseño es macOS-específico en rutas y en el ABI de las deps.
- Arreglar los 49 `hasattr` guards. Es un bug preexistente del repo contra la API
  nativa; documentarlo, no arreglarlo acá.

## Criterio de terminado

1. El boot levanta el server con una sola línea pegada en la consola, y es
   idempotente.
2. Un cliente MCP externo lista 341 tools y ejecuta lectura y escritura.
3. El dispatch con lock está instalado y verificado bajo llamadas concurrentes.
4. Existe la matriz de cobertura de la edición gratis: qué tools andan, cuáles
   fallan, y por qué.
5. El token sale de `DAVINCI_MCP_TOKEN`, no del código.
6. La suite de tests existente sigue pasando fuera de Resolve.
7. `git diff` contra el punto de fork no lista un solo archivo fuera de
   `free_edition/`.
