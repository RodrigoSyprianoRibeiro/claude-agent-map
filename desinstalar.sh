#!/usr/bin/env bash
# Desfaz o instalar.sh: para e desabilita o servico, apaga a unit, o atalho e o icone e tira os hooks
# do ~/.claude/settings.json (o statusLine original, se havia, volta).
# NAO apaga ~/.config/mapa-agentes (config, posicoes e perfil do navegador) nem os anexos.
set -uo pipefail

PASTA="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
systemctl --user disable --now mapa-agentes.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/mapa-agentes.service" \
      "$HOME/.local/share/applications/mapa-agentes.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/mapa-agentes.svg" \
      "$HOME"/.local/share/icons/hicolor/*x*/apps/mapa-agentes.png
systemctl --user daemon-reload
systemctl --user reset-failed mapa-agentes.service 2>/dev/null || true
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database "$HOME/.local/share/applications" || true; fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -f -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true; fi
echo "Hooks do Claude Code:"
python3 "$PASTA/configurar_claude.py" remover
echo "Mapa de agentes desinstalado (config, posicoes e perfil do navegador mantidos em ~/.config/mapa-agentes)."
