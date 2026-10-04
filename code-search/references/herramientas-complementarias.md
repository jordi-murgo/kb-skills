# Instalar y usar herramientas complementarias

El motor autocontenido de `code-search` sigue en `.vault-meta/code-search/`. CodeGraph, AST y LSP añaden capacidades distintas; no son requisitos para la búsqueda léxica/vectorial ni se instalan con el script. Su `doctor --json` comprueba Git, SQLite/FTS5, PyYAML cuando corresponde y la configuración básica: **no diagnostica estas cadenas opcionales**.

## Elegir la capacidad y obtener consentimiento

| Necesidad | Complemento | Límite |
|---|---|---|
| Símbolos y relaciones entre llamadas | CodeGraph | Requiere grafo del proyecto; no sustituye al análisis de tipos |
| Patrones de sintaxis Java u otros lenguajes | AST del runtime o ast-grep | Coincidencias estructurales, no referencias resueltas por tipos |
| Definiciones, tipos, referencias y diagnósticos Java | Cliente LSP con Eclipse JDT LS | Requiere servidor registrado y proyecto importado correctamente |

Reutiliza las capacidades disponibles. Si falta alguna, explica qué instalarás, dónde escribirá y qué configuración cambiará; pregunta al usuario y espera autorización. Distingue el consentimiento para instalar el binario, modificar MCP/permisos e indexar un proyecto: aceptar uno no autoriza los demás. No uses instalaciones silenciosas, `npx` que descargue paquetes por sorpresa ni `codegraph install --yes`/`-y`.

## CodeGraph: instalar el binario

### Opción recomendada: standalone fijado y revisado

Para macOS/Linux, el bundle incluye su propio Node y no requiere instalar Node/npm. Esta guía fija **v1.2.0**: no utiliza la rama `main`, `latest` ni una tubería de descarga a shell.

1. Presenta la descarga del instalador oficial y su versión; no descargues por sorpresa. Guarda el script en un temporal y revisa su contenido sin ejecutarlo:

   ```bash
   tmp_dir="$(mktemp -d)"
   script="$tmp_dir/install.sh"
   curl -fsSL \
     https://raw.githubusercontent.com/colbymchenry/codegraph/v1.2.0/install.sh \
     -o "$script"
   less "$script"
   ```

2. Antes de ejecutar, explica sus efectos y obtén aprobación explícita. Por defecto descarga el bundle de GitHub Releases a `~/.codegraph/versions/v1.2.0`, actualiza `~/.codegraph/current` y enlaza el ejecutable en `~/.local/bin/codegraph`. **Reemplaza el directorio de esa versión y elimina bundles anteriores**. Revisa también posibles valores existentes de `CODEGRAPH_INSTALL_DIR` y `CODEGRAPH_BIN_DIR`, que cambian los destinos. No crea por sí mismo el grafo de un proyecto ni conecta MCP.

3. Solo después de la aprobación, ejecuta en la misma terminal y retira el temporal:

   ```bash
   env CODEGRAPH_VERSION=v1.2.0 sh "$script"
   rm -f "$script"
   rmdir "$tmp_dir"
   ```

   Si falla la descarga o la instalación, conserva el error y retira igualmente el temporal; no cambies silenciosamente de versión o método.

4. Añade el directorio del ejecutable al `PATH` de esta terminal. Para hacerlo persistente, acuerda primero el cambio en el archivo de inicio de la shell:

   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   command -v codegraph
   env DO_NOT_TRACK=1 codegraph --version
   ```

   Con los destinos predeterminados, debe resolver `~/.local/bin/codegraph` y mostrar la versión `1.2.0`. Si aparece otra copia, revisa la precedencia del `PATH`; no desinstales nada automáticamente.

El [instalador oficial v1.2.0](https://raw.githubusercontent.com/colbymchenry/codegraph/v1.2.0/install.sh) define destinos, descarga y eliminación de bundles. El [README oficial de esa versión](https://github.com/colbymchenry/codegraph/blob/v1.2.0/README.md) explica el bundle y los pasos de integración.

### Alternativa: npm global

Si el usuario elige npm y autoriza la instalación:

```bash
npm install -g @colbymchenry/codegraph@1.2.0
```

El [manifiesto oficial v1.2.0](https://github.com/colbymchenry/codegraph/blob/v1.2.0/package.json) declara Node **`>=20.0.0 <25.0.0`** para el paquete npm. No interpretes la frase general del README sobre Node como permiso para ignorar ese rango. Comprueba el Node de esa máquina antes de elegir npm; si no cumple, propone el standalone en vez de cambiar el Node del usuario. Esta alternativa tampoco configura MCP ni construye grafos.

## CodeGraph: integrar, indexar y consultar

### Tres operaciones diferentes

| Operación | Efecto | Autorización |
|---|---|---|
| Instalador standalone o npm | Instala el binario y su runtime | Instalación y destinos explicados |
| `codegraph install` | Configura agentes seleccionados: MCP, instrucciones y permisos | Cambios concretos de agente/configuración, sin `--yes` ni `-y` |
| `codegraph init [path]` | Crea `.codegraph/` y construye el grafo inicial del código | Proyecto y alcance explícitamente elegidos |

Puedes consultar con la CLI sin configurar MCP. Si se autoriza la integración, ejecuta `codegraph install` de forma interactiva, revisa los agentes y permisos propuestos y no aceptes cambios adicionales sin consentimiento.

**Telemetría:** CodeGraph v1.2.0 la activa por defecto; saltarse el instalador interactivo no equivale a desactivarla. Según su [documentación oficial](https://github.com/colbymchenry/codegraph/blob/v1.2.0/TELEMETRY.md), envía estadísticas anónimas de comandos e indexación, no código, rutas, nombres ni consultas. Explica esa salida de red antes de usarlo. Los ejemplos siguientes usan `DO_NOT_TRACK=1` para desactivarla; `CODEGRAPH_TELEMETRY=0` es otra opción y `codegraph telemetry off` guarda la elección. Un cliente MCP debe recibir la misma decisión en el entorno de su servidor, mediante la configuración documentada de ese cliente.

### Alcance y almacenamiento del grafo

Prefiere la raíz de cada proyecto a un grafo indiscriminado de todo el vault. Elige la ruta con el usuario y explica que `init` **indexa inmediatamente**: no es un registro vacío ni requiere un `build` posterior para comenzar. No fuerces la inicialización del directorio personal o de la raíz del sistema.

Se permite `<proyecto>/.codegraph/` como excepción autorizada a `.vault-meta/code-search/`: contiene datos externos de CodeGraph, no el índice propio del script. No lo versiones. Antes de inicializar, comprueba que el `.gitignore` del repositorio o submódulo elegido incluya:

```gitignore
.codegraph/
```

No dependas únicamente del `.gitignore` del vault para proteger un submódulo. `init` también genera un `.gitignore` interno del directorio de datos; mantén explícita la exclusión en cada repositorio objetivo.

### Ejemplo con ruta de proyecto

Desde la raíz del vault, sustituye el proyecto de ejemplo por el que se haya autorizado. Consultar estado no crea el grafo:

```bash
env DO_NOT_TRACK=1 codegraph status projects/lambda-cifrado-credenciales --json
```

Si falta, solicita autorización para esta ruta antes de ejecutar:

```bash
env DO_NOT_TRACK=1 codegraph init projects/lambda-cifrado-credenciales
```

Una vez indexado, consulta símbolos y lee la evidencia devuelta:

```bash
env DO_NOT_TRACK=1 codegraph query CredentialCipher \
  --path projects/lambda-cifrado-credenciales --limit 5 --json
```

`status` informa del estado y las estadísticas; `query` busca símbolos indexados. Una consulta sin coincidencias no demuestra ausencia de una implementación, y un error por falta de grafo no autoriza `init`. No presupongas un watcher activo: comprueba el estado y acuerda cualquier actualización necesaria. La herramienta nativa CodeGraph del runtime solo opera sobre el **workspace actual** y no acepta una ruta de proyecto; la CLI sí permite elegirla. No uses la herramienta nativa del vault como si consultara automáticamente un submódulo.

## AST: consulta sintáctica, instalación opcional

Si el runtime ya ofrece AST/ast-grep para Java, úsalo sin instalar otra CLI. Proporciona el lenguaje `java`, la ruta del proyecto y el patrón `Cipher.getInstance($ALGORITHM)` al dispositivo nativo. No existe una equivalencia entre este patrón y `codegraph query`: son capacidades separadas.

Para disponer de la CLI externa, elige **una** opción y obtén autorización antes de ejecutarla:

```bash
# macOS con Homebrew
brew install ast-grep

# Alternativa con npm
npm install -g @ast-grep/cli

# Alternativa con Rust/Cargo
cargo install ast-grep --locked
```

Ejemplo de consulta, sin reescritura de fuentes:

```bash
ast-grep --pattern 'Cipher.getInstance($ALGORITHM)' --lang java \
  projects/lambda-cifrado-credenciales
```

Las comillas simples evitan que la shell expanda `$ALGORITHM`; la metavariable representa un nodo sintáctico. Usa el nombre `ast-grep`, no `sg`, que en Linux puede ser otro programa. Interpreta los resultados como llamadas con esa forma, no como prueba de resolución del símbolo `Cipher` ni del tipo del argumento. La [guía oficial de ast-grep](https://ast-grep.github.io/guide/quick-start.html) documenta instalación, patrones y comillas.

## LSP Java: JDT LS necesita un cliente

Eclipse JDT Language Server requiere **Java 21 o superior para ejecutar el servidor**. Ese runtime es independiente del objetivo Java 17 de una aplicación: no cambies su `pom.xml`, toolchain o nivel de compilación para arrancar el servidor. Usa el JDK del servidor mediante su `JAVA_HOME`/`PATH` y conserva la configuración Java del proyecto.

En macOS, una opción autorizada de instalación es:

```bash
brew install jdtls
```

La [fórmula oficial de Homebrew](https://formulae.brew.sh/formula/jdtls) documenta el comando y sus dependencias. Para otros sistemas, consulta las opciones de distribución del [proyecto oficial Eclipse JDT LS](https://github.com/eclipse-jdtls/eclipse.jdt.ls).

Instalar el ejecutable **no registra un servidor en el runtime**. Configura el cliente LSP real siguiendo su documentación: ejecutable `jdtls`, transporte stdio, entorno Java 21+, raíz absoluta del proyecto y argumentos de configuración/workspace. No inventes claves de configuración de omp ni presentes el servidor como un comando de búsqueda de shell.

Reserva rutas absolutas bajo el vault, distintas para cada proyecto:

- `<vault-absoluto>/.vault-meta/code-search/lsp/<proyecto>/configuration`
- `<vault-absoluto>/.vault-meta/code-search/lsp/<proyecto>/workspace`

Ejemplo del comando que debe lanzar **el cliente LSP**, no una búsqueda que se ejecuta manualmente:

```bash
jdtls \
  -configuration /ruta/absoluta/al/vault/.vault-meta/code-search/lsp/lambda-cifrado-credenciales/configuration \
  -data /ruta/absoluta/al/vault/.vault-meta/code-search/lsp/lambda-cifrado-credenciales/workspace
```

Sustituye la raíz de ejemplo por la ruta real antes de registrar el servidor. `-configuration` aísla el estado de configuración del usuario; `-data` almacena los metadatos del workspace y debe ser único por proyecto. Mantén `.vault-meta/` excluido de Git. Para stdio, el cliente no debe definir variables de conexión por socket como `CLIENT_PORT`/`CLIENT_HOST`.

Tras una configuración autorizada, pide al cliente su estado/diagnósticos y comprueba que ha importado el proyecto Maven/Gradle y resuelto las dependencias antes de tratar referencias o tipos como completos. La evidencia debe venir de operaciones LSP de definiciones, referencias o diagnósticos, no de coincidencias textuales. El `doctor` del motor de búsqueda no certifica este registro ni la salud de JDT LS.

## Registro OMP para un proyecto Maven anidado

OMP lee los servidores de `<cwd>/.omp/lsp.json`. Usa la entrada `servers.jdtls`; el filtro inicial de `rootMarkers` mira el directorio de trabajo, no busca proyectos hijos automáticamente. Para este esquema de vault, incluye tanto `pom.xml` como `projects/lambda-cifrado-credenciales/pom.xml`: el segundo activa el servidor desde el vault y el primero permite elegir la raíz Maven al abrir un archivo Java del submódulo. Adapta el nombre a cada proyecto.

- Establece `fileTypes: [".java"]` y `languageId: "java"`.
- Para fijar el JDK solo en el proceso hijo, configura `command: "/usr/bin/env"` y empieza `args` con `JAVA_HOME=<ruta-absoluta-del-JDK-del-servidor>` seguido de la ruta real de `jdtls`. No añadas un campo `env` inexistente al esquema ni cambies el `JAVA_HOME` global.
- Pasa `--jvm-arg=-Djava.import.generatesMetadataFilesAtProjectRoot=false` a JDT LS. En la versión verificada, esta protección es una propiedad JVM; poner únicamente un setting del cliente no evita metadatos Eclipse en las fuentes.
- Pasa `-configuration` y `-data` con las rutas absolutas de caché indicadas arriba. Configura `java.autobuild.enabled=false` tanto en `initOptions.settings` como en `settings`, para que llegue antes de importar el proyecto y no compile la aplicación automáticamente.
- Si se aísla Maven, conserva su configuración editable en `.omp/maven-settings.xml`, fuera de `.vault-meta/`, y apunta `java.configuration.maven.globalSettings` a ella. Su `localRepository` puede estar en `.vault-meta/code-search/lsp/<proyecto>/maven-repository`. Referencia los settings de usuario si son necesarios, sin copiar credenciales ni modificarlos. La caché debe poder borrarse sin perder configuración.

Tras guardar, ejecuta la herramienta LSP con `{"action":"reload","file":"*"}`. Verifica una definición entre archivos, referencias reales, resolución de una dependencia y diagnósticos de los archivos de producción. Comprueba que el proyecto importado mantiene su nivel Java original; no cambies el POM ni ejecutes tests/builds Maven como parte de activar el servidor.

## Cobertura y precisión de CodeGraph

No confundas `reindexRecommended=false` con cobertura completa: ese indicador puede estar al día aunque falten archivos de producción. Tras sincronizar, compara los archivos elegibles del proyecto con los registrados y prueba una relación concreta leyendo ambas fuentes.

La versión 1.2.0 verificada indexa archivos con `record` Java, pero no extrae todos sus nodos de tipo, constructores compactos ni accessors generados. También puede resolver por el nombre del método cuando falla la resolución por tipo: en este proyecto atribuyó `request.environment()` a un helper de test, mientras LSP lo resuelve al componente de `EncryptionRequest`.

Trata las relaciones del grafo como candidatos de exploración, no como referencias tipadas verificadas. Confirma con LSP o con evidencia de fuente antes de refactorizar o afirmar impacto; un índice sin cambios pendientes no elimina esas limitaciones del extractor. No alteres las fuentes, el código instalado de CodeGraph ni sus tablas SQL para ocultarlas.
