#!/usr/bin/env bash
# Instala o Mapa de agentes como app Linux: servico systemd --user, icone, atalho (.desktop) e os
# hooks do Claude Code (uso do plano e alerta de "precisa de voce") no ~/.claude/settings.json.
# Idempotente: pode rodar de novo (atualiza arquivos e reinicia o servico so se algo mudou).
# Opcoes: --no-hooks (nao mexe no settings.json)
set -euo pipefail

PASTA="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PORTA="${MAPA_PORTA:-8767}"
UNIT_DIR="$HOME/.config/systemd/user"
APPS_DIR="$HOME/.local/share/applications"
HICOLOR="$HOME/.local/share/icons/hicolor"
ICON_DIR="$HICOLOR/scalable/apps"
UNIT="$UNIT_DIR/mapa-agentes.service"
DESKTOP="$APPS_DIR/mapa-agentes.desktop"
ICONE="$ICON_DIR/mapa-agentes.svg"
HOOKS=1
case "${1:-}" in --no-hooks|--sem-hooks) HOOKS=0 ;; esac

falta() { echo "ERRO: $1" >&2; exit 1; }
command -v python3 >/dev/null || falta "python3 not found"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || falta "Python 3.10 or newer is required"
python3 -c 'import aiohttp' 2>/dev/null || falta "aiohttp is missing: pip install --user aiohttp (or: sudo apt install python3-aiohttp)"
command -v systemctl >/dev/null || falta "systemd (user services) is required"
command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ] || echo "WARNING: 'claude' command not found. Install Claude Code and log in (run: claude) before using the map." >&2
# config.json invalido: melhor falhar aqui do que o servico ficar reiniciando
python3 -c "import sys; sys.path.insert(0, sys.argv[1]); import config; config.carregar()" "$PASTA" || falta "fix ~/.config/mapa-agentes/config.json"

mkdir -p "$UNIT_DIR" "$APPS_DIR" "$ICON_DIR"
MUDOU=0

# grava o arquivo so se o conteudo mudou; devolve 0 se mudou
gravar() {
  local destino="$1" tmp
  tmp="$(mktemp)"
  cat > "$tmp"
  if [ -f "$destino" ] && cmp -s "$tmp" "$destino"; then rm -f "$tmp"; return 1; fi
  mv "$tmp" "$destino"; chmod 644 "$destino"; return 0
}

gravar "$UNIT" <<UNIT_EOF && MUDOU=1 || true
[Unit]
Description=Mapa de agentes (mapa 2D das sessoes do Claude Code)
After=default.target

[Service]
ExecStart=/usr/bin/env python3 $PASTA/servidor.py --porta $PORTA
WorkingDirectory=$PASTA
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
# so o processo principal: nada que o app tenha iniciado (attach, daemon do claude) morre junto num restart
KillMode=process
RestartSec=3

[Install]
WantedBy=default.target
UNIT_EOF

# icone: SVG (fonte unica em static/icones) + PNG nos tamanhos padrao, que painel e alt-tab usam em tamanho pequeno
gravar "$ICONE" < "$PASTA/static/icones/mapa-agentes.svg" || true
for n in 16 24 32 48 64 128 256 512; do
  mkdir -p "$HICOLOR/${n}x${n}/apps"
  gravar "$HICOLOR/${n}x${n}/apps/mapa-agentes.png" < "$PASTA/static/icones/$n.png" || true
done

# atalho: abre tudo; o clique direito traz um atalho por workspace do config.json
ACOES="$(python3 - "$PASTA" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import config
ws = config.carregar().workspaces
print(';'.join(['tudo'] + [w.id for w in ws]) + ';')
for w in ws:
    print('\n[Desktop Action %s]\nName=Only %s\nExec=/usr/bin/env python3 "%s/servidor.py" --abrir --foco %s' % (w.id, w.nome, sys.argv[1], w.id))
PY
)"
gravar "$DESKTOP" <<DESK_EOF && MUDOU=1 || true
[Desktop Entry]
Type=Application
Version=1.0
Name=Agent Map
Comment=Live map of your Claude Code sessions
Exec=/usr/bin/env python3 "$PASTA/servidor.py" --abrir
Icon=mapa-agentes
Terminal=false
StartupWMClass=mapa-agentes
Categories=Development;
Actions=$(echo "$ACOES" | head -1)

[Desktop Action tudo]
Name=All
Exec=/usr/bin/env python3 "$PASTA/servidor.py" --abrir
$(echo "$ACOES" | tail -n +2)
DESK_EOF

if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database "$APPS_DIR" || true; fi
# cache refeito DEPOIS de gravar os icones e com -f: o GTK compara datas em segundos, e um cache gerado no
# mesmo segundo do arquivo novo vale como atualizado e esconde o icone
if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -f -q -t "$HICOLOR" 2>/dev/null || true; fi

if [ "$HOOKS" = 1 ]; then
  echo "Claude Code hooks (~/.claude/settings.json, backed up first):"
  python3 "$PASTA/configurar_claude.py" install
fi

systemctl --user daemon-reload
systemctl --user enable mapa-agentes.service >/dev/null 2>&1
if systemctl --user is-active --quiet mapa-agentes.service; then
  [ "$MUDOU" = 1 ] && systemctl --user restart mapa-agentes.service
else
  # a porta tem que estar livre: um servidor iniciado a mao (nao pelo systemd) impede o servico de subir
  if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$PORTA "; then
    echo "WARNING: port $PORTA is in use by another process (a server started by hand?). Stop it and run again." >&2
    exit 1
  fi
  systemctl --user start mapa-agentes.service
fi

echo "Agent Map installed: http://127.0.0.1:$PORTA"
echo "  service : systemctl --user status mapa-agentes"
echo "  launcher: applications menu > Agent Map"
echo "  config  : ~/.config/mapa-agentes/config.json (optional, see config.example.json; run this script again after changing it)"
