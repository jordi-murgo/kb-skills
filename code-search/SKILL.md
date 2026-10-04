---
name: code-search
description: "Trigger: buscar código, localizar implementación, indexar proyectos, code search. Consulta léxica/vectorial local con rutas y líneas."
license: Apache-2.0
metadata:
  author: kb-skills
  version: "1.0"
argument-hint: "doctor | build | status | query TEXTO"
---

# code-search

## Activación

Usa este skill para localizar implementación en los proyectos del vault. Mantén las consultas de conocimiento Markdown en `wiki-semsearch`.

## Reglas

- Ejecuta `doctor --json` antes de utilizar el índice. Si devuelve dependencias ausentes, pregunta al usuario si desea instalarlas; presenta los comandos sugeridos y espera autorización. Nunca instales ni descargues modelos automáticamente.
- Conserva el estado del motor autocontenido en `.vault-meta/code-search/`; la excepción autorizada es el grafo externo de CodeGraph en `<proyecto>/.codegraph/`. No escribas código fuente, `.raw/` ni `wiki/`.
- Lee alcance, extensiones y endpoint de `kb-config.yaml`, sección `code_search`. Incluye `.` únicamente si se configura explícitamente. Respeta los `.gitignore`, también en submódulos; no sigas enlaces simbólicos.
- Envía fragmentos solo al endpoint de embeddings configurado. No cambies a un proveedor externo ante un fallo.
- El índice por líneas no es un grafo de llamadas ni sustituye al análisis de tipos. Inicializa CodeGraph solo tras autorización explícita para el proyecto elegido: `init` construye el grafo, no solo crea metadatos. Excluye `.codegraph/` de Git en cada repositorio o submódulo; no construyas grafos indiscriminados del vault ni inicialices índices externos de `zg`.

## Selección de herramienta

| Necesidad | Acción |
|---|---|
| Localizar implementación por significado | `query "TEXTO" --mode vector` |
| Conceptos y términos exactos combinados | `query "TEXTO" --mode hybrid` |
| Búsqueda léxica sin endpoint | `query "TEXTO" --mode bm25` |
| Definiciones, tipos o referencias | LSP del runtime, si está disponible |
| Patrones sintácticos | AST del runtime, si está disponible |
| Relaciones de código | Consulta CodeGraph disponible e indexado; si falta, solicita autorización para instalarlo o inicializar el proyecto |

Si falta una herramienta complementaria necesaria, pregunta si el usuario desea habilitarla o instalarla. No presentes coincidencias textuales ni relaciones heurísticas de CodeGraph como referencias tipadas verificadas; confirma estas últimas con LSP o evidencia de fuente antes de refactorizar.

## Ejecución

Desde la raíz del vault:

```bash
python3 .agents/skills/code-search/scripts/code-search.py doctor --json
python3 .agents/skills/code-search/scripts/code-search.py build --json
python3 .agents/skills/code-search/scripts/code-search.py status --json
python3 .agents/skills/code-search/scripts/code-search.py query "cifrado RSA de credenciales" --mode vector --top 5 --json
```

1. Comprueba dependencias y configuración con `doctor`; no realiza instalaciones ni llamadas de red y no diagnostica CodeGraph, AST ni LSP.
2. Si falta el índice o `status.needs_build` es verdadero, ejecuta `build`. Los errores de embeddings conservan el índice anterior; no hay fallback silencioso.
3. Consulta y lee las rutas y líneas devueltas antes de afirmar cómo funciona el código.

## Resultado

Devuelve archivos y rangos de líneas, modo efectivo, evidencia leída y límites de cobertura. Si hay bloqueo, nombra la dependencia o endpoint ausente y la autorización necesaria.

## Referencias

- [Configuración, almacenamiento y dependencias](references/configuracion.md).
- [Instalación y uso de CodeGraph, AST y LSP Java](references/herramientas-complementarias.md).
- [Script autocontenido](scripts/code-search.py).
