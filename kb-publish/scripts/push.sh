#!/usr/bin/env bash
# push.sh — hace push al repo principal y despliega el wiki a GitLab
# Uso: ./.agents/skills/kb-publish/scripts/push.sh [args de git push]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Este script vive dentro de una skill, así que su profundidad bajo la raíz del
# repo depende de dónde esté instalada. Subimos buscando los marcadores en lugar
# de contar niveles, que se rompe en cuanto la skill se mueve.
# Se exigen wiki/ Y .git: existe un directorio de skill llamado "wiki" bajo
# .agents/skills/, así que el marcador wiki/ por sí solo resuelve al directorio
# de skills en vez de a la raíz del repo.
REPO_DIR="$SCRIPT_DIR"
while [ "$REPO_DIR" != "/" ] && ! { [ -d "$REPO_DIR/wiki" ] && [ -e "$REPO_DIR/.git" ]; }; do
    REPO_DIR="$(dirname "$REPO_DIR")"
done
if ! { [ -d "$REPO_DIR/wiki" ] && [ -e "$REPO_DIR/.git" ]; }; then
    echo "❌ Raíz del repo no encontrada: ningún directorio superior tiene wiki/ y .git"
    exit 1
fi
cd "$REPO_DIR"

# Configuración del proyecto — nada específico de un proyecto vive en este script
CFG="$REPO_DIR/kb-config.json"
if [ ! -f "$CFG" ]; then
    echo "❌ Config no encontrada: $CFG"
    echo "   Copia kb-config.example.json del repo kb-skills y complétala."
    exit 1
fi
read_cfg() { python3 -c "
import json,sys
cfg=json.load(open('$CFG')).get('gitlab_wiki',{})
v=cfg.get('$1', '$2')
print('' if v is None else ('true' if v is True else ('false' if v is False else v)))"; }

VPN_REQUIRED=$(read_cfg vpn_required true)
VPN_HOST=$(read_cfg vpn_host "")
VPN_PREFIX=$(read_cfg vpn_private_prefix "10.")

# Verificar VPN — el host debe resolver a una IP privada
if [ "$VPN_REQUIRED" = "true" ]; then
    if [ -z "$VPN_HOST" ]; then
        echo "❌ gitlab_wiki.vpn_required es true pero falta vpn_host en kb-config.json"
        exit 1
    fi
    GIT_IP=$(host "$VPN_HOST" 2>/dev/null | awk '/has address/ {print $NF}' | head -1)
    if [ -z "$GIT_IP" ]; then
        echo "❌ No se puede resolver $VPN_HOST — ¿DNS funcionando?"
        exit 1
    fi
    case "$GIT_IP" in
        "$VPN_PREFIX"*)
            echo "🔒 VPN activa ($VPN_HOST → $GIT_IP)" ;;
        *)
            echo "⚠️  VPN NO activa — $VPN_HOST resuelve a $GIT_IP (fuera de $VPN_PREFIX*)"
            echo "   Conecta la VPN antes de hacer push."
            exit 1 ;;
    esac
fi

# Push al repo principal
echo "📦 Push al repo principal..."
git push "$@"

# Si hay cambios en wiki/, desplegar
CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null | grep "^wiki/" | head -1)
if [ -n "$CHANGED" ]; then
    echo "📋 Cambios en wiki/ detectados — desplegando a GitLab Wiki..."
    python3 "$SCRIPT_DIR/deploy-gitlab-wiki.py"
else
    echo "ℹ No hay cambios en wiki/ — skip deploy"
fi