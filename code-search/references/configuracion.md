# Configurar code-search

El script se puede copiar con el skill completo: no importa módulos de otros skills. Requiere Python 3.10+, Git y SQLite con FTS5. PyYAML solo es necesario si la configuración es YAML.

## Configuración

Añade una sección al `kb-config.yaml` del vault:

```yaml
code_search:
  source_dirs:
    - projects
  # Opcional: limitar idiomas y configuración; los valores sustituyen la lista predeterminada.
  # extensions: [.java, .xml, .yaml, .json, .toml, .sh]
  # Opcionales: heredan embeddings.model y embeddings.endpoint si se omiten.
  # model: bge-m3
  # endpoint: ${WIKI_OLLAMA_URL}
```

| Clave | Contrato |
|---|---|
| `source_dirs` | Lista no vacía de directorios relativos al vault; por defecto `[projects]`. `.` incluye la raíz solamente si se indica. Se rechazan rutas absolutas, `~`, `..`, directorios ausentes y enlaces simbólicos. |
| `extensions` | Lista no vacía de sufijos con punto. Predeterminados: `.java`, `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.go`, `.rs`, `.c`, `.h`, `.cpp`, `.hpp`, `.cs`, `.kt`, `.kts`, `.swift`, `.sh`, `.ps1`, `.sql`, `.yaml`, `.yml`, `.json`, `.xml`, `.toml`. Incluye pruebas y configuración, no solo código de producción. Markdown requiere `.md` explícito. |
| `model` | Valor explícito → `embeddings.model` → `bge-m3`. |
| `endpoint` | URL base HTTP(S), sin credenciales, query ni fragmento; se añade `/api/embed`. Valor explícito → `embeddings.endpoint` → `http://127.0.0.1:11434`. |

Las macros `${VAR}` y `${VAR:-default}` se expanden recursivamente. Precedencia: entorno del proceso → `.env.local` → `.env`; estos ficheros solo aportan valores a las macros. La selección `embeddings.source_dirs` no modifica `code_search.source_dirs`.

El script resuelve la raíz buscando `wiki/` y `.git`, primero desde el directorio de trabajo. Para invocarlo desde fuera:

```bash
python3 <skill>/scripts/code-search.py --root <vault> doctor --json
```

## Datos derivados y cobertura

- Única ubicación de datos propios: `.vault-meta/code-search/index.db`. No contiene configuración editable y se puede eliminar para reconstruir desde el YAML.
- Asegura que `.vault-meta/` esté en `.gitignore`; no versiones fragmentos ni vectores derivados.
- Se aplican las reglas Git reales, incluidas negaciones y archivos ya versionados que coincidan con patrones ignorados. Cada repositorio anidado o submódulo usa sus propias reglas Git.
- Nunca se recorren `.git` ni `.vault-meta`; no se siguen enlaces simbólicos. Se contabilizan los archivos binarios y no UTF-8 omitidos sin mostrar su contenido.
- Fragmentación por líneas con solapamiento, límite de 80 líneas/4000 caracteres. Las líneas individuales mayores se dividen conservando su número; no es parsing AST.
- `build` reutiliza los embeddings de archivos cuyo SHA-256 no cambió y retira archivos borrados, ignorados o excluidos por configuración. Cambiar modelo/endpoint exige regenerar vectores. Un fallo del endpoint o de la transacción conserva el último índice válido.
- `status` compara la configuración y el contenido actual con el índice. Consulta `files`, `chunks`, `embedded`, `dimensions`, `new_paths`, `stale_paths` y `needs_build`.

## Dependencias y consentimiento

`doctor --json` es local y no escribe ni contacta al endpoint. Devuelve `ok`, `dependencies`, `missing_dependencies` con `name`/`install_hint`, y `errors`. Si no está listo, termina con código 2.

Ante una dependencia ausente, el agente debe preguntar: «Falta <dependencia>. ¿Deseas instalarla con <comando sugerido>?». No ejecute el comando hasta obtener autorización. Ofrece continuar sin esa capacidad si no es necesaria; no modifiques Python, Git ni SQLite de forma automática.

Si el endpoint no está disponible, corrige la configuración o pregunta antes de habilitar un servicio. No hagas `ollama pull`, no descargues modelos y no cambies de proveedor por tu cuenta. BM25 puede consultar un índice ya construido sin endpoint; construir un índice vectorial sí lo requiere.

LSP, AST y CodeGraph son complementos opcionales: el script no los instala y `doctor` no diagnostica sus cadenas de herramientas; comprueba las dependencias básicas Git, SQLite/FTS5 y PyYAML cuando corresponde, además de la configuración. Consulta CodeGraph si ya está disponible e indexado; si no, solicita autorización explícita antes de instalarlo, modificar permisos MCP o ejecutar `init` para el proyecto elegido. `init` crea y construye el grafo externo en `<proyecto>/.codegraph/`, excepción autorizada al estado del motor en `.vault-meta/code-search/`; exige excluirlo de Git en cada repositorio o submódulo. No construyas grafos indiscriminados del vault. Consulta [instalación y uso de herramientas complementarias](herramientas-complementarias.md).

## Registro local

Conserva el skill en `.agents/skills/code-search/` y, para Claude Code, crea el puente relativo `.claude/skills/code-search -> ../../.agents/skills/code-search`. Si existe Gentle AI, actualiza su índice con `gentle-ai skill-registry refresh`.

Un runtime puede conservar una lista de skills tomada al iniciar la sesión. Si `skill://code-search` aún no lo reconoce, carga `.agents/skills/code-search/SKILL.md` por su ruta real; no reinstales el skill ni regeneres el índice de código para corregir esa caché del runtime.
